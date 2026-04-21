"""Gravitational collision & merger — F2.1.

Two nodes whose virtual positions are close enough collide and merge.
The heavier node survives; the lighter is absorbed. Physical quantities
are composed conserving (approximately) total mass and momentum:

    m_new = min(m_keep + m_absorbed, m_max)
    v_new = (v_keep * m_keep + v_absorbed * m_absorbed) / (m_keep + m_absorbed)

The absorbed node is marked ``is_archived=True`` with ``merged_into`` set
to the survivor's id, so history is preserved and recall filters it out.
Co-occurrence edges from the absorbed node are re-targeted to the survivor.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np

from ger_rag.config import GERConfig
from ger_rag.core.types import NodeState
from ger_rag.store.cache import CacheLayer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MergeOutcome:
    """Result of a single A+B → survivor merger."""
    survivor_id: str
    absorbed_id: str
    mass_before: float
    mass_after: float
    absorbed_mass: float


def pick_survivor(a: NodeState, b: NodeState) -> tuple[NodeState, NodeState]:
    """Return (survivor, absorbed). Heavier wins; ties broken by last_access (newer wins)."""
    if a.mass > b.mass:
        return a, b
    if b.mass > a.mass:
        return b, a
    return (a, b) if a.last_access >= b.last_access else (b, a)


def compose_mass(survivor_mass: float, absorbed_mass: float, m_max: float) -> float:
    """Mass addition with saturation cap."""
    return min(survivor_mass + absorbed_mass, m_max)


def compose_velocity(
    v_survivor: np.ndarray,
    m_survivor: float,
    v_absorbed: np.ndarray,
    m_absorbed: float,
) -> np.ndarray:
    """Momentum-conserving weighted average of velocities."""
    total = m_survivor + m_absorbed
    if total <= 0:
        return v_survivor.copy()
    return ((v_survivor * m_survivor + v_absorbed * m_absorbed) / total).astype(np.float32)


def compose_displacement(
    d_survivor: np.ndarray,
    m_survivor: float,
    d_absorbed: np.ndarray,
    m_absorbed: float,
    max_norm: float,
) -> np.ndarray:
    """Mass-weighted displacement with norm clipping."""
    total = m_survivor + m_absorbed
    if total <= 0:
        return d_survivor.copy()
    d = (d_survivor * m_survivor + d_absorbed * m_absorbed) / total
    norm = float(np.linalg.norm(d))
    if norm > max_norm:
        d = d * (max_norm / norm)
    return d.astype(np.float32)


def merge_pair(
    survivor: NodeState,
    absorbed: NodeState,
    cache: CacheLayer,
    config: GERConfig,
    now: float | None = None,
) -> MergeOutcome:
    """Apply the collision mechanics in-place against the cache.

    Side effects:
      - ``survivor`` gains mass, its velocity/displacement are recomputed,
        merge_count is incremented; marked dirty.
      - ``absorbed`` becomes is_archived=True, merged_into=survivor.id,
        merge_count incremented, merged_at set; evicted from cache after
        being marked so the dirty write persists the archive state.
      - Co-occurrence edges touching absorbed are re-targeted to survivor
        (max-weight composition on collision; no double edges).
    """
    if now is None:
        now = time.time()
    mass_before = survivor.mass
    absorbed_mass = absorbed.mass
    dim = config.embedding_dim

    v_s = cache.get_velocity(survivor.id)
    v_s = v_s.copy() if v_s is not None else np.zeros(dim, dtype=np.float32)
    v_a = cache.get_velocity(absorbed.id)
    v_a = v_a.copy() if v_a is not None else np.zeros(dim, dtype=np.float32)

    d_s = cache.get_displacement(survivor.id)
    d_s = d_s.copy() if d_s is not None else np.zeros(dim, dtype=np.float32)
    d_a = cache.get_displacement(absorbed.id)
    d_a = d_a.copy() if d_a is not None else np.zeros(dim, dtype=np.float32)

    new_mass = compose_mass(mass_before, absorbed_mass, config.m_max)
    new_v = compose_velocity(v_s, mass_before, v_a, absorbed_mass)
    new_d = compose_displacement(
        d_s, mass_before, d_a, absorbed_mass, config.max_displacement_norm,
    )

    survivor.mass = new_mass
    survivor.merge_count += 1
    survivor.last_access = now
    cache.set_node(survivor, dirty=True)
    cache.set_velocity(survivor.id, new_v)
    cache.set_displacement(survivor.id, new_d)

    absorbed.is_archived = True
    absorbed.merged_into = survivor.id
    absorbed.merged_at = now
    absorbed.merge_count += 1
    # Persist the archive+merge flags to DB: save node state BEFORE evicting
    # from cache so the flush picks it up. We re-insert into cache marked
    # dirty and let a downstream flush or engine.merge() call handle it.
    cache.set_node(absorbed, dirty=True)

    _redirect_edges(absorbed.id, survivor.id, cache)

    logger.info(
        "Merged %s → %s (mass %.3f → %.3f, +%.3f)",
        absorbed.id, survivor.id, mass_before, new_mass, absorbed_mass,
    )
    return MergeOutcome(
        survivor_id=survivor.id,
        absorbed_id=absorbed.id,
        mass_before=mass_before,
        mass_after=new_mass,
        absorbed_mass=absorbed_mass,
    )


def _redirect_edges(absorbed_id: str, survivor_id: str, cache: CacheLayer) -> None:
    """Move every edge touching absorbed_id onto survivor_id (max weight)."""
    neighbors = dict(cache.graph_cache.get(absorbed_id, {}))
    for other, weight in neighbors.items():
        if other == survivor_id:
            continue
        existing = cache.graph_cache.get(survivor_id, {}).get(other, 0.0)
        cache.set_edge(survivor_id, other, max(existing, weight), dirty=True)
        cache.remove_edge(absorbed_id, other)
    # Remove any direct absorbed<->survivor edge
    cache.remove_edge(absorbed_id, survivor_id)
    # Drop absorbed from graph_cache entirely (evict_node will clean remnants)
    cache.graph_cache.pop(absorbed_id, None)
