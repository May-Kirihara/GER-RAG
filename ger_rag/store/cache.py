from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from ger_rag.core.types import CooccurrenceEdge, NodeState
from ger_rag.store.base import StoreBase

logger = logging.getLogger(__name__)


class CacheLayer:
    def __init__(self, flush_interval: float = 5.0, flush_threshold: int = 100):
        self.node_cache: dict[str, NodeState] = {}
        self.graph_cache: dict[str, dict[str, float]] = {}
        self.displacement_cache: dict[str, np.ndarray] = {}
        self.velocity_cache: dict[str, np.ndarray] = {}
        self.dirty_nodes: set[str] = set()
        self.dirty_edges: set[tuple[str, str]] = set()
        self.dirty_displacements: set[str] = set()
        self.dirty_velocities: set[str] = set()
        self._flush_interval = flush_interval
        self._flush_threshold = flush_threshold
        self._write_behind_task: asyncio.Task | None = None

    # --- Node state ---

    def get_node(self, node_id: str) -> NodeState | None:
        return self.node_cache.get(node_id)

    def set_node(self, state: NodeState, dirty: bool = True) -> None:
        self.node_cache[state.id] = state
        if dirty:
            self.dirty_nodes.add(state.id)

    def get_all_nodes(self) -> list[NodeState]:
        return list(self.node_cache.values())

    # --- Displacement ---

    def get_displacement(self, node_id: str) -> np.ndarray | None:
        return self.displacement_cache.get(node_id)

    def set_displacement(self, node_id: str, displacement: np.ndarray) -> None:
        self.displacement_cache[node_id] = displacement
        self.dirty_displacements.add(node_id)

    # --- Velocity ---

    def get_velocity(self, node_id: str) -> np.ndarray | None:
        return self.velocity_cache.get(node_id)

    def set_velocity(self, node_id: str, velocity: np.ndarray) -> None:
        self.velocity_cache[node_id] = velocity
        self.dirty_velocities.add(node_id)

    # --- Graph edges ---

    def get_neighbors(self, node_id: str) -> dict[str, float]:
        return self.graph_cache.get(node_id, {})

    def set_edge(self, src: str, dst: str, weight: float, dirty: bool = True) -> None:
        self.graph_cache.setdefault(src, {})[dst] = weight
        self.graph_cache.setdefault(dst, {})[src] = weight
        if dirty:
            key = (min(src, dst), max(src, dst))
            self.dirty_edges.add(key)

    def remove_edge(self, src: str, dst: str) -> None:
        if src in self.graph_cache:
            self.graph_cache[src].pop(dst, None)
        if dst in self.graph_cache:
            self.graph_cache[dst].pop(src, None)
        key = (min(src, dst), max(src, dst))
        self.dirty_edges.add(key)

    def get_all_edges(self) -> list[CooccurrenceEdge]:
        seen: set[tuple[str, str]] = set()
        edges: list[CooccurrenceEdge] = []
        for src, neighbors in self.graph_cache.items():
            for dst, weight in neighbors.items():
                key = (min(src, dst), max(src, dst))
                if key not in seen:
                    seen.add(key)
                    edges.append(
                        CooccurrenceEdge(src=key[0], dst=key[1], weight=weight, last_update=time.time())
                    )
        return edges

    # --- Load from store ---

    async def load_from_store(self, store: StoreBase) -> None:
        states = await store.get_all_node_states()
        for s in states:
            self.node_cache[s.id] = s

        edges = await store.get_all_edges()
        for e in edges:
            self.graph_cache.setdefault(e.src, {})[e.dst] = e.weight
            self.graph_cache.setdefault(e.dst, {})[e.src] = e.weight

        self.displacement_cache = await store.load_displacements()
        self.velocity_cache = await store.load_velocities()

        logger.info(
            "Cache loaded: %d nodes, %d edges, %d displacements, %d velocities",
            len(self.node_cache), len(edges),
            len(self.displacement_cache), len(self.velocity_cache),
        )

    # --- Flush to store ---

    async def flush_to_store(self, store: StoreBase) -> None:
        if self.dirty_nodes:
            dirty_states = [
                self.node_cache[nid]
                for nid in self.dirty_nodes
                if nid in self.node_cache
            ]
            if dirty_states:
                await store.save_node_states(dirty_states)
            self.dirty_nodes.clear()

        if self.dirty_displacements:
            dirty_disp = {
                nid: self.displacement_cache[nid]
                for nid in self.dirty_displacements
                if nid in self.displacement_cache
            }
            if dirty_disp:
                await store.save_displacements(dirty_disp)
            self.dirty_displacements.clear()

        if self.dirty_velocities:
            dirty_vel = {
                nid: self.velocity_cache[nid]
                for nid in self.dirty_velocities
                if nid in self.velocity_cache
            }
            if dirty_vel:
                await store.save_velocities(dirty_vel)
            self.dirty_velocities.clear()

        if self.dirty_edges:
            dirty_edge_list: list[CooccurrenceEdge] = []
            for src, dst in self.dirty_edges:
                weight = self.graph_cache.get(src, {}).get(dst, 0.0)
                dirty_edge_list.append(
                    CooccurrenceEdge(src=src, dst=dst, weight=weight, last_update=time.time())
                )
            if dirty_edge_list:
                await store.save_edges(dirty_edge_list)
            self.dirty_edges.clear()

    # --- Write-behind background task ---

    async def _write_behind_loop(self, store: StoreBase) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            try:
                if self.dirty_nodes or self.dirty_edges:
                    await self.flush_to_store(store)
            except Exception:
                logger.exception("Write-behind flush failed")

    def start_write_behind(self, store: StoreBase) -> None:
        if self._write_behind_task is None:
            self._write_behind_task = asyncio.create_task(self._write_behind_loop(store))

    async def stop_write_behind(self) -> None:
        if self._write_behind_task is not None:
            self._write_behind_task.cancel()
            try:
                await self._write_behind_task
            except asyncio.CancelledError:
                pass
            self._write_behind_task = None

    # --- Reset ---

    def reset(self) -> None:
        for state in self.node_cache.values():
            state.mass = 1.0
            state.temperature = 0.0
            state.last_access = time.time()
            state.sim_history = []
        self.dirty_nodes.update(self.node_cache.keys())
        self.graph_cache.clear()
        self.dirty_edges.clear()
        self.displacement_cache.clear()
        self.dirty_displacements.clear()
        self.velocity_cache.clear()
        self.dirty_velocities.clear()
