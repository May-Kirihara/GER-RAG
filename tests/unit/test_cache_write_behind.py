"""Regression tests for bugs reported in docs/research/multi-agent-experiment-2026-04-22.md.

B-01 (HIGH):
  - _write_behind_loop guard only checked dirty_nodes/dirty_edges, so
    displacement-only or velocity-only dirty sets never flushed until a
    node/edge happened to become dirty.
  - remove_edge left the (src, dst) key in dirty_edges, so flush_to_store
    re-materialized the dropped edge as a weight=0 zombie row.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from gaottt.core.types import CooccurrenceEdge, DirectedEdge, NodeState
from gaottt.store.base import StoreBase
from gaottt.store.cache import CacheLayer


class _RecordingStore(StoreBase):
    """Minimal StoreBase implementation that records flush calls."""

    def __init__(self) -> None:
        self.saved_edges: list[CooccurrenceEdge] = []
        self.deleted_pairs: list[tuple[str, str]] = []
        self.saved_displacements: dict[str, np.ndarray] = {}
        self.saved_velocities: dict[str, np.ndarray] = {}
        self.saved_node_states: list[NodeState] = []

    async def save_documents(self, docs: list[dict[str, Any]]) -> None: ...
    async def get_document(self, doc_id: str) -> dict[str, Any] | None: return None

    async def save_node_states(self, states: list[NodeState]) -> None:
        self.saved_node_states.extend(states)

    async def get_node_states(self, ids: list[str]) -> dict[str, NodeState]: return {}
    async def get_all_node_states(self) -> list[NodeState]: return []

    async def save_edges(self, edges: list[CooccurrenceEdge]) -> None:
        self.saved_edges.extend(edges)

    async def get_edges_for_node(self, node_id: str) -> list[CooccurrenceEdge]: return []

    async def delete_edges(self, pairs: list[tuple[str, str]]) -> int:
        normalized = [(min(a, b), max(a, b)) for a, b in pairs]
        self.deleted_pairs.extend(normalized)
        return len(normalized)

    async def get_all_edges(self) -> list[CooccurrenceEdge]: return []

    async def save_displacements(self, displacements: dict[str, np.ndarray]) -> None:
        self.saved_displacements.update(displacements)

    async def load_displacements(
        self, ids: list[str] | None = None,
    ) -> dict[str, np.ndarray]: return {}

    async def save_velocities(self, velocities: dict[str, np.ndarray]) -> None:
        self.saved_velocities.update(velocities)

    async def load_velocities(
        self, ids: list[str] | None = None,
    ) -> dict[str, np.ndarray]: return {}

    async def reset_dynamic_state(self) -> tuple[int, int]: return 0, 0
    async def set_archived(self, node_ids: list[str], archived: bool) -> int: return 0
    async def hard_delete_nodes(self, node_ids: list[str]) -> int: return 0
    async def expire_due_nodes(self, now: float) -> int: return 0
    async def upsert_directed_edge(self, edge: DirectedEdge) -> None: ...
    async def delete_directed_edge(
        self, src: str, dst: str, edge_type: str | None = None,
    ) -> int: return 0
    async def get_directed_edges(
        self,
        node_id: str | None = None,
        edge_type: str | None = None,
        direction: str = "out",
    ) -> list[DirectedEdge]: return []
    async def delete_directed_edges_for_node(self, node_id: str) -> int: return 0
    async def close(self) -> None: ...


@pytest.mark.asyncio
async def test_remove_edge_is_deleted_not_zombied_as_weight_zero():
    """B-01b: remove_edge should physically DELETE, not save as weight=0."""
    cache = CacheLayer()
    cache.set_edge("a", "b", 3.0)
    store = _RecordingStore()
    await cache.flush_to_store(store)
    assert len(store.saved_edges) == 1
    store.saved_edges.clear()

    cache.remove_edge("a", "b")
    await cache.flush_to_store(store)

    # Must not re-save the dropped edge (at any weight, including 0.0).
    assert store.saved_edges == []
    assert store.deleted_pairs == [("a", "b")]


@pytest.mark.asyncio
async def test_set_then_remove_within_same_flush_cycle_emits_delete_only():
    """Edge set and removed before flush resolves as a delete, not a save."""
    cache = CacheLayer()
    cache.set_edge("x", "y", 2.0)
    cache.remove_edge("x", "y")
    store = _RecordingStore()
    await cache.flush_to_store(store)

    assert store.saved_edges == []
    assert store.deleted_pairs == [("x", "y")]


def test_write_behind_guard_recognises_displacements_and_velocities():
    """B-01a: dirty displacements/velocities alone must trigger the flush guard."""
    cache = CacheLayer()
    cache.set_displacement("a", np.zeros(4, dtype=np.float32))
    assert cache.dirty_displacements == {"a"}
    assert not cache.dirty_nodes and not cache.dirty_edges

    # Mirror the condition used by _write_behind_loop.
    has_dirty = bool(
        cache.dirty_nodes
        or cache.dirty_edges
        or cache.dirty_displacements
        or cache.dirty_velocities
    )
    assert has_dirty

    cache2 = CacheLayer()
    cache2.set_velocity("b", np.zeros(4, dtype=np.float32))
    has_dirty2 = bool(
        cache2.dirty_nodes
        or cache2.dirty_edges
        or cache2.dirty_displacements
        or cache2.dirty_velocities
    )
    assert has_dirty2


@pytest.mark.asyncio
async def test_flush_persists_displacements_and_velocities_without_node_dirt():
    """Even with no node/edge dirt, displacement/velocity flushes must reach the store."""
    cache = CacheLayer()
    cache.set_displacement("a", np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32))
    cache.set_velocity("b", np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32))

    store = _RecordingStore()
    await cache.flush_to_store(store)

    assert "a" in store.saved_displacements
    assert "b" in store.saved_velocities
    assert not cache.dirty_displacements
    assert not cache.dirty_velocities
