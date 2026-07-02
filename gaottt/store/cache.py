from __future__ import annotations

import asyncio
import logging
import math
import time

import numpy as np

from gaottt.core.types import CooccurrenceEdge, NodeState
from gaottt.store.base import StoreBase

logger = logging.getLogger(__name__)


def _percentile_sorted(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolated percentile of an ascending-sorted list.

    ``p`` in [0, 100]. Used by Stage 8's optional hub-degree cut. Empty
    input returns +inf so an absent distribution cuts nothing.
    """
    if not sorted_vals:
        return float("inf")
    if p <= 0:
        return sorted_vals[0]
    if p >= 100:
        return sorted_vals[-1]
    pos = (p / 100.0) * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


class CacheLayer:
    def __init__(self, flush_interval: float = 5.0, flush_threshold: int = 100):
        self.node_cache: dict[str, NodeState] = {}
        self.graph_cache: dict[str, dict[str, float]] = {}
        # Lateral Association Stage 8 — lazily-built per-node co-occurrence
        # degree map (deg(x) = Σ weights of x's edges) + total unique-edge
        # weight, used by get_association_strength to discount promiscuous
        # hubs. None = stale; rebuilt on first use after any edge mutation
        # (set_edge / remove_edge / load_from_store / evict_node / reset all
        # invalidate). Only ever built when the normalization knob is active,
        # so the legacy "none" path pays nothing.
        self._degree_cache: dict[str, float] | None = None
        self._total_weight: float = 0.0
        # Synaptic Pruning — per-edge last-reinforcement time (key = sorted
        # pair) for the half-life decay, loaded from the store's edges table
        # (which records last_update as the last co-recall flush) and bumped
        # to now on every set_edge. Plus a separate decayed-degree cache keyed
        # by the active half-life: the decayed degree map is time-dependent,
        # so it is recomputed when the half-life changes or after a mutation;
        # within-session time drift is negligible against a days-scale T½.
        self.edge_last_update: dict[tuple[str, str], float] = {}
        self._decayed_degree_cache: dict[str, float] | None = None
        self._decayed_total: float = 0.0
        self._decayed_degree_half_life: float | None = None
        self.displacement_cache: dict[str, np.ndarray] = {}
        self.velocity_cache: dict[str, np.ndarray] = {}
        # Phase H Stage 2: id → metadata.source. Populated on load_from_store
        # and on index_documents. Lets propagate_gravity_wave apply
        # source_filter at the seed step without per-node store fetches.
        self.source_by_id: dict[str, str] = {}
        # Phase J Stage 1: in-memory mirror of directed_edges so that
        # persona-anchored gravity boost can perform graph traversal in the
        # sync propagate_gravity_wave path without per-recall DB hits.
        # Each entry holds (other_id, edge_type). Loaded on startup, kept
        # in sync by engine.relate / unrelate / forget(hard) / compact paths.
        self.directed_out: dict[str, list[tuple[str, str]]] = {}
        self.directed_in: dict[str, list[tuple[str, str]]] = {}
        # Phase J Stage 2: tag reverse index for the tag_filter additive
        # seed injection. ``tag_to_ids[tag_substring]`` does NOT live here
        # — instead we keep the per-node tag list so callers can do
        # substring matching at recall time (substring patterns vary by
        # query). ``tags_by_id[node_id]`` = list of tag strings as written
        # in documents.metadata.tags.
        self.tags_by_id: dict[str, list[str]] = {}
        # Phase M Stage 1 — Mass conservation: per-node structural identifiers
        # used to detect "self-force" co-occurrence (same source document,
        # same supernova cohort). Mirrored from documents.metadata.
        # original_id_by_id[nid]: id of the source document/object the node
        #   was chunked from (single-doc remember uses node_id itself, file
        #   chunks share the file's stable id, csv rows use the csv id).
        # cohort_id_by_id[nid]: id of the supernova cohort the node was born
        #   in (set on every Phase K batch; absent for singleton remember).
        self.original_id_by_id: dict[str, str] = {}
        self.cohort_id_by_id: dict[str, str] = {}
        self.dirty_nodes: set[str] = set()
        self.dirty_edges: set[tuple[str, str]] = set()
        self.dirty_displacements: set[str] = set()
        self.dirty_velocities: set[str] = set()
        # Phase H Stage 4 (cont.) — virtual FAISS write-behind tracker.
        # Flipped True when displacement changes (the only input to
        # compute_virtual_position aside from raw embedding + temperature,
        # and temperature mutations always co-occur with displacement
        # mutations via update_orbital_state). The engine's virtual-FAISS
        # save loop reads this flag, rebuilds + saves, then clears it.
        # Kept on the cache (not the engine) so unit tests that mutate
        # displacement without an engine still set it consistently.
        self.virtual_faiss_dirty: bool = False
        # MV2 — lease-loss read-only latch. Flipped True by the engine when
        # its heartbeat detects the owner changed. ``flush_to_store`` honours
        # it so dirty cache never reaches SQLite after a takeover (prevents
        # reverse-overwriting the new owner's state). Write-behind loop,
        # shutdown final flush, and every explicit flush all route through
        # ``flush_to_store``, so this single gate covers them.
        self.persist_blocked: bool = False
        self._flush_interval = flush_interval
        self._flush_threshold = flush_threshold
        self._write_behind_task: asyncio.Task | None = None

    # --- Node state ---

    def get_node(self, node_id: str) -> NodeState | None:
        return self.node_cache.get(node_id)

    def set_node(self, state: NodeState, dirty: bool = True) -> None:
        self.node_cache[state.id] = state
        if dirty:
            # H2 — advance the per-node revision so a stale flush from
            # another process is rejected by the conditional upsert in
            # SqliteStore.save_node_states (excluded.rev >= nodes.rev).
            state.rev += 1
            self.dirty_nodes.add(state.id)

    def get_all_nodes(self) -> list[NodeState]:
        return list(self.node_cache.values())

    # --- Source lookup (Phase H Stage 2) ---

    def get_source(self, node_id: str) -> str | None:
        return self.source_by_id.get(node_id)

    def set_source(self, node_id: str, source: str) -> None:
        self.source_by_id[node_id] = source

    # --- Displacement ---

    def get_displacement(self, node_id: str) -> np.ndarray | None:
        return self.displacement_cache.get(node_id)

    def set_displacement(self, node_id: str, displacement: np.ndarray) -> None:
        self.displacement_cache[node_id] = displacement
        self.dirty_displacements.add(node_id)
        self.virtual_faiss_dirty = True

    # --- Velocity ---

    def get_velocity(self, node_id: str) -> np.ndarray | None:
        return self.velocity_cache.get(node_id)

    def set_velocity(self, node_id: str, velocity: np.ndarray) -> None:
        self.velocity_cache[node_id] = velocity
        self.dirty_velocities.add(node_id)

    # --- Graph edges ---

    def get_neighbors(self, node_id: str) -> dict[str, float]:
        return self.graph_cache.get(node_id, {})

    def _invalidate_degree_caches(self) -> None:
        """Stage 8 / Synaptic Pruning — drop the raw + decayed degree maps
        after any edge mutation; both are rebuilt lazily on next read."""
        self._degree_cache = None
        self._decayed_degree_cache = None

    def set_edge(self, src: str, dst: str, weight: float, dirty: bool = True,
                 last_update: float | None = None) -> None:
        self.graph_cache.setdefault(src, {})[dst] = weight
        self.graph_cache.setdefault(dst, {})[src] = weight
        key = (min(src, dst), max(src, dst))
        # Synaptic Pruning: a set is a reinforcement → reset the decay clock.
        # ``last_update`` lets load_from_store seed the historical timestamp;
        # live reinforcements pass None and stamp now.
        self.edge_last_update[key] = last_update if last_update is not None else time.time()
        self._invalidate_degree_caches()
        if dirty:
            self.dirty_edges.add(key)

    def remove_edge(self, src: str, dst: str) -> None:
        if src in self.graph_cache:
            self.graph_cache[src].pop(dst, None)
        if dst in self.graph_cache:
            self.graph_cache[dst].pop(src, None)
        key = (min(src, dst), max(src, dst))
        self.edge_last_update.pop(key, None)
        self._invalidate_degree_caches()
        self.dirty_edges.add(key)

    def get_all_edges(self) -> list[CooccurrenceEdge]:
        seen: set[tuple[str, str]] = set()
        edges: list[CooccurrenceEdge] = []
        for src, neighbors in self.graph_cache.items():
            for dst, weight in neighbors.items():
                key = (min(src, dst), max(src, dst))
                if key not in seen:
                    seen.add(key)
                    # Synaptic Pruning: persist the tracked last-reinforcement
                    # time (the decay clock), not a fabricated "now", so the
                    # half-life survives a reload. Falls back to now for edges
                    # with no tracked timestamp (e.g. set before this field).
                    lu = self.edge_last_update.get(key, time.time())
                    edges.append(
                        CooccurrenceEdge(src=key[0], dst=key[1], weight=weight, last_update=lu)
                    )
        return edges

    # --- Association strength (Lateral Association Stage 8) + Synaptic Pruning ---

    def _edge_decay_factor(
        self, a: str, b: str, now: float, half_life: float,
    ) -> float:
        """Synaptic Pruning — half-life decay of an edge by its age since the
        last reinforcement. ``0.5 ** (age / half_life)``; ``1.0`` when there is
        no tracked timestamp or the edge was just reinforced."""
        key = (a, b) if a < b else (b, a)
        last = self.edge_last_update.get(key)
        if last is None:
            return 1.0
        age = now - last
        if age <= 0.0:
            return 1.0
        return 0.5 ** (age / half_life)

    def _degrees(
        self, decay_half_life: float | None, now: float | None,
    ) -> tuple[dict[str, float], float]:
        """Return ``(degree_map, doubled_total)``.

        ``deg(x) = Σ_n w(x, n)``. ``graph_cache`` is symmetric, so summing
        every node's adjacency double-counts each undirected edge; the second
        return value is that doubled sum (callers halve it for the PMI
        normalizer ``W``).

        Without Synaptic Pruning the raw map is cached and mutation-invalidated
        (the legacy fast path). With it, weights are decayed by edge age — the
        decayed map is time-dependent, so it is cached per active half-life and
        rebuilt on mutation; within-session time drift is negligible against a
        days-scale half-life (see the cache-field note).
        """
        if not decay_half_life or decay_half_life <= 0.0:
            if self._degree_cache is None:
                deg: dict[str, float] = {}
                doubled = 0.0
                for src, neighbors in self.graph_cache.items():
                    s = 0.0
                    for w in neighbors.values():
                        s += w
                    deg[src] = s
                    doubled += s
                self._degree_cache = deg
                self._total_weight = doubled
            return self._degree_cache, self._total_weight

        if (
            self._decayed_degree_cache is not None
            and self._decayed_degree_half_life == decay_half_life
        ):
            return self._decayed_degree_cache, self._decayed_total
        if now is None:
            now = time.time()
        deg = {}
        doubled = 0.0
        for src, neighbors in self.graph_cache.items():
            s = 0.0
            for dst, w in neighbors.items():
                s += w * self._edge_decay_factor(src, dst, now, decay_half_life)
            deg[src] = s
            doubled += s
        self._decayed_degree_cache = deg
        self._decayed_total = doubled
        self._decayed_degree_half_life = decay_half_life
        return deg, doubled

    def get_degree(
        self, node_id: str, *,
        decay_half_life: float | None = None, now: float | None = None,
    ) -> float:
        """Co-occurrence degree (Σ incident edge weights) of a node. With
        ``decay_half_life`` set, weights are aged by Synaptic Pruning first."""
        deg, _ = self._degrees(decay_half_life, now)
        return deg.get(node_id, 0.0)

    def get_association_strength(
        self, node_id: str, *, mode: str = "cosine",
        hub_degree_cut: float | None = None,
        decay_half_life: float | None = None, now: float | None = None,
    ) -> dict[str, float]:
        """Stage 8 — degree-normalized co-occurrence weights for a node.

        ``mode``:
          - ``"none"``   raw co-recall counts (legacy, identical to
            ``get_neighbors`` when Synaptic Pruning is off).
          - ``"cosine"`` ``w(a,b) / sqrt(deg(a)·deg(b))`` — co-occurrence
            cosine; a promiscuous hub's high ``deg`` divides its association
            to everyone down, a rare specific neighbour stays high.
          - ``"pmi"``    ``max(0, log(w·W / (deg(a)·deg(b))))`` — positive
            pointwise mutual information; ``W`` = total unique-edge weight.

        ``hub_degree_cut`` (percentile in [0,100], or None): drop neighbours
        whose degree exceeds that percentile of the active degree
        distribution before returning — an explicit anti-hub floor on top of
        the soft normalization. Unknown ``mode`` falls back to raw counts.

        Synaptic Pruning: when ``decay_half_life`` is set (>0), each raw weight
        is first multiplied by its half-life decay factor (before any
        normalization, so it applies even in ``mode="none"``), and the degree
        map used for normalization / the hub cut is the decayed one. ``None``
        ⇒ no decay (bit-exact legacy).
        """
        raw = self.graph_cache.get(node_id, {})
        if not raw:
            return {}
        if decay_half_life and decay_half_life > 0.0:
            if now is None:
                now = time.time()
            raw = {
                j: w * self._edge_decay_factor(node_id, j, now, decay_half_life)
                for j, w in raw.items()
            }

        if mode == "none":
            scored = dict(raw)
        else:
            deg, doubled_total = self._degrees(decay_half_life, now)
            deg_a = deg.get(node_id, 0.0)
            if deg_a <= 0.0:
                scored = dict(raw)
            elif mode == "cosine":
                scored = {}
                for j, w in raw.items():
                    denom = math.sqrt(deg_a * deg.get(j, 0.0))
                    scored[j] = (w / denom) if denom > 0.0 else 0.0
            elif mode == "pmi":
                big_w = doubled_total / 2.0  # undo symmetric double-count
                scored = {}
                for j, w in raw.items():
                    denom = deg_a * deg.get(j, 0.0)
                    if denom > 0.0 and big_w > 0.0 and w > 0.0:
                        val = math.log((w * big_w) / denom)
                        scored[j] = val if val > 0.0 else 0.0
                    else:
                        scored[j] = 0.0
            else:
                scored = dict(raw)  # unknown mode → legacy

        if hub_degree_cut is not None and scored:
            deg, _ = self._degrees(decay_half_life, now)
            degrees = sorted(deg.values())
            cut = _percentile_sorted(degrees, hub_degree_cut)
            scored = {j: s for j, s in scored.items() if deg.get(j, 0.0) <= cut}
        return scored

    # --- Directed edges (Phase J Stage 1) ---

    def set_directed_edge(self, src: str, dst: str, edge_type: str) -> None:
        """Mirror an upserted directed edge into the in-memory cache.

        SQLite is the SoT (upsert / delete go through SqliteStore). This cache
        is read-only authoritative for the sync recall path — engine.relate /
        unrelate / forget(hard) call this after writing to the store so the
        next recall sees the change without waiting for a cache reload.
        """
        pair = (dst, edge_type)
        out_list = self.directed_out.setdefault(src, [])
        if pair not in out_list:
            out_list.append(pair)
        in_list = self.directed_in.setdefault(dst, [])
        rev = (src, edge_type)
        if rev not in in_list:
            in_list.append(rev)

    def remove_directed_edge(self, src: str, dst: str, edge_type: str | None = None) -> None:
        """Drop an edge from the cache. ``edge_type=None`` removes all
        edges between the pair."""
        if src in self.directed_out:
            self.directed_out[src] = [
                (d, et)
                for (d, et) in self.directed_out[src]
                if d != dst or (edge_type is not None and et != edge_type)
            ]
            if not self.directed_out[src]:
                self.directed_out.pop(src, None)
        if dst in self.directed_in:
            self.directed_in[dst] = [
                (s, et)
                for (s, et) in self.directed_in[dst]
                if s != src or (edge_type is not None and et != edge_type)
            ]
            if not self.directed_in[dst]:
                self.directed_in.pop(dst, None)

    def get_outgoing(self, node_id: str) -> list[tuple[str, str]]:
        """Return [(dst_id, edge_type), ...] for edges going out of node_id."""
        return self.directed_out.get(node_id, [])

    def get_incoming(self, node_id: str) -> list[tuple[str, str]]:
        """Return [(src_id, edge_type), ...] for edges coming into node_id."""
        return self.directed_in.get(node_id, [])

    # --- Tag index (Phase J Stage 2) ---

    def set_tags(self, node_id: str, tags: list[str]) -> None:
        """Mirror the tags of a (just-indexed) document into the cache. Empty
        ``tags`` list is dropped (no need to keep an entry that never matches)."""
        clean = [t for t in tags if isinstance(t, str) and t]
        if clean:
            self.tags_by_id[node_id] = clean
        else:
            self.tags_by_id.pop(node_id, None)

    def get_tags(self, node_id: str) -> list[str]:
        return self.tags_by_id.get(node_id, [])

    # --- Cohort / original lookup (Phase M Stage 1) ---

    def get_original(self, node_id: str) -> str | None:
        """Stable id of the document/object the node was chunked from. None
        when absent — callers must treat this as "no self-force linkage"."""
        return self.original_id_by_id.get(node_id)

    def set_original(self, node_id: str, original_id: str) -> None:
        if original_id:
            self.original_id_by_id[node_id] = original_id

    def get_cohort(self, node_id: str) -> str | None:
        """Supernova cohort id, or None for singleton remember."""
        return self.cohort_id_by_id.get(node_id)

    def set_cohort(self, node_id: str, cohort_id: str) -> None:
        if cohort_id:
            self.cohort_id_by_id[node_id] = cohort_id

    def find_ids_by_tag_filter(self, tag_substrings: list[str]) -> set[str]:
        """OR-match: return ids whose tag list contains any string that
        contains any of the requested substrings (substring match per
        Phase J Stage 2 §「設計判断 2」). Returns an empty set on no
        filters / no hits."""
        if not tag_substrings:
            return set()
        # Pre-lowercase substrings? Phase H Stage 2 source_filter is case-
        # sensitive; mirror that for consistency.
        hits: set[str] = set()
        for nid, tags in self.tags_by_id.items():
            for tag in tags:
                if any(sub in tag for sub in tag_substrings):
                    hits.add(nid)
                    break
        return hits

    # --- Load from store ---

    async def load_from_store(self, store: StoreBase) -> None:
        states = await store.get_all_node_states()
        archived_ids: set[str] = set()
        for s in states:
            if s.is_archived:
                archived_ids.add(s.id)
                continue
            self.node_cache[s.id] = s

        edges = await store.get_all_edges()
        loaded_edges = 0
        for e in edges:
            if e.src in archived_ids or e.dst in archived_ids:
                continue
            self.graph_cache.setdefault(e.src, {})[e.dst] = e.weight
            self.graph_cache.setdefault(e.dst, {})[e.src] = e.weight
            # Synaptic Pruning: seed the decay clock from the store's recorded
            # last_update (= last reinforcement time), so decay is retroactive
            # for stale cliques rather than restarting from this load.
            key = (e.src, e.dst) if e.src < e.dst else (e.dst, e.src)
            self.edge_last_update[key] = e.last_update
            loaded_edges += 1
        self._invalidate_degree_caches()  # Stage 8: rebuilt lazily from fresh graph

        self.displacement_cache = await store.load_displacements()
        self.velocity_cache = await store.load_velocities()
        self.source_by_id = await store.get_all_sources()

        # Phase J Stage 2: tag reverse index for tag_filter additive injection.
        tags_map = await store.get_all_tags()
        # Drop archived ids so tag_filter does not surface zombie nodes.
        self.tags_by_id = {
            nid: tags for nid, tags in tags_map.items() if nid not in archived_ids
        }

        # Phase M Stage 1 — mass-conservation identifiers.
        original_map = await store.get_all_originals()
        self.original_id_by_id = {
            nid: oid for nid, oid in original_map.items() if nid not in archived_ids
        }
        cohort_map = await store.get_all_cohorts()
        self.cohort_id_by_id = {
            nid: cid for nid, cid in cohort_map.items() if nid not in archived_ids
        }

        # Phase J Stage 1: mirror directed edges into the in-memory cache.
        # Skip edges that touch archived nodes so the sync recall path never
        # traverses zombie connections.
        self.directed_out.clear()
        self.directed_in.clear()
        loaded_directed = 0
        directed = await store.get_directed_edges()
        for e in directed:
            if e.src in archived_ids or e.dst in archived_ids:
                continue
            self.directed_out.setdefault(e.src, []).append((e.dst, e.edge_type))
            self.directed_in.setdefault(e.dst, []).append((e.src, e.edge_type))
            loaded_directed += 1

        logger.info(
            "Cache loaded: %d active nodes (%d archived skipped), %d edges, "
            "%d directed_edges, %d displacements, %d velocities, %d sources",
            len(self.node_cache), len(archived_ids), loaded_edges,
            loaded_directed,
            len(self.displacement_cache), len(self.velocity_cache),
            len(self.source_by_id),
        )

    # --- Archive (F4 + F5) ---

    def evict_node(self, node_id: str) -> None:
        """Drop a node and its associated state from the in-memory cache.

        Used when a node is archived or deleted; the canonical state still
        lives in the store (or is removed there separately for hard delete).
        """
        self.node_cache.pop(node_id, None)
        self.displacement_cache.pop(node_id, None)
        self.velocity_cache.pop(node_id, None)
        self.source_by_id.pop(node_id, None)
        self.tags_by_id.pop(node_id, None)
        self.original_id_by_id.pop(node_id, None)
        self.cohort_id_by_id.pop(node_id, None)
        self.dirty_nodes.discard(node_id)
        self.dirty_displacements.discard(node_id)
        self.dirty_velocities.discard(node_id)
        # Active-set membership changed → virtual FAISS needs rebuild.
        self.virtual_faiss_dirty = True
        neighbors = self.graph_cache.pop(node_id, {})
        for other in neighbors:
            self.graph_cache.get(other, {}).pop(node_id, None)
            key = (node_id, other) if node_id < other else (other, node_id)
            self.edge_last_update.pop(key, None)
        if neighbors:
            self._invalidate_degree_caches()  # Stage 8: degrees changed
        # Phase J Stage 1: prune directed edges touching this node so the
        # cache stays consistent with what the persona traversal expects.
        outgoing = self.directed_out.pop(node_id, [])
        for dst, _et in outgoing:
            if dst in self.directed_in:
                self.directed_in[dst] = [
                    (s, et) for (s, et) in self.directed_in[dst] if s != node_id
                ]
                if not self.directed_in[dst]:
                    self.directed_in.pop(dst, None)
        incoming = self.directed_in.pop(node_id, [])
        for src, _et in incoming:
            if src in self.directed_out:
                self.directed_out[src] = [
                    (d, et) for (d, et) in self.directed_out[src] if d != node_id
                ]
                if not self.directed_out[src]:
                    self.directed_out.pop(src, None)

    # --- Flush to store ---

    async def flush_to_store(self, store: StoreBase) -> None:
        if self.persist_blocked:
            return  # read-only: lease lost, no writes to SQLite
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
            removed_pairs: list[tuple[str, str]] = []
            for src, dst in self.dirty_edges:
                if dst in self.graph_cache.get(src, {}):
                    weight = self.graph_cache[src][dst]
                    # Synaptic Pruning: persist the tracked reinforcement time
                    # (the decay clock), not a fabricated "now". ``dirty_edges``
                    # keys are already sorted (min,max).
                    lu = self.edge_last_update.get((src, dst), time.time())
                    dirty_edge_list.append(
                        CooccurrenceEdge(src=src, dst=dst, weight=weight, last_update=lu)
                    )
                else:
                    removed_pairs.append((src, dst))
            if dirty_edge_list:
                await store.save_edges(dirty_edge_list)
            if removed_pairs:
                await store.delete_edges(removed_pairs)
            self.dirty_edges.clear()

    # --- Write-behind background task ---

    async def _write_behind_loop(self, store: StoreBase) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            try:
                if (
                    self.dirty_nodes
                    or self.dirty_edges
                    or self.dirty_displacements
                    or self.dirty_velocities
                ):
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
        self.edge_last_update.clear()
        self._invalidate_degree_caches()  # Stage 8: graph emptied
        self.dirty_edges.clear()
        self.displacement_cache.clear()
        self.dirty_displacements.clear()
        self.velocity_cache.clear()
        self.dirty_velocities.clear()
