"""Gravitational displacement computation.

Documents attract each other in embedding space through gravitational force
proportional to their mass and inversely proportional to distance squared.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from ger_rag.index.faiss_index import FaissIndex
    from ger_rag.store.cache import CacheLayer

from ger_rag.config import GERConfig

logger = logging.getLogger(__name__)


def compute_virtual_position(
    original_emb: np.ndarray,
    displacement: np.ndarray | None,
    temperature: float = 0.0,
) -> np.ndarray:
    """Compute virtual position = normalize(original + displacement + thermal noise)."""
    pos = original_emb.copy()
    if displacement is not None:
        pos = pos + displacement
    if temperature > 0.001:
        noise = np.random.randn(*pos.shape).astype(np.float32) * temperature
        pos = pos + noise
    # L2 normalize to maintain unit sphere (cosine sim = inner product)
    norm = np.linalg.norm(pos)
    if norm > 0:
        pos = pos / norm
    return pos


def compute_gravitational_force(
    pos_i: np.ndarray,
    pos_j: np.ndarray,
    mass_i: float,
    mass_j: float,
    config: GERConfig,
) -> np.ndarray:
    """Compute gravitational force vector on node i from node j.

    Returns the displacement delta to apply to node i.
    """
    diff = pos_j - pos_i
    distance_sq = float(np.dot(diff, diff)) + config.gravity_epsilon
    distance = math.sqrt(distance_sq)

    # F = G * m_i * m_j / d^2
    force_magnitude = config.gravity_G * mass_i * mass_j / distance_sq

    # Direction: i → j (normalized)
    direction = diff / distance

    # Displacement delta = η * F * direction
    delta = config.gravity_eta * force_magnitude * direction
    return delta.astype(np.float32)


def update_displacements_for_cooccurrence(
    result_ids: list[str],
    original_embeddings: dict[str, np.ndarray],
    displacements: dict[str, np.ndarray],
    masses: dict[str, float],
    config: GERConfig,
) -> dict[str, np.ndarray]:
    """Update displacement vectors for all co-retrieved document pairs.

    For each pair (i, j) in the result set, i is pulled toward j and vice versa.
    Returns updated displacements for all affected nodes.
    """
    updated: dict[str, np.ndarray] = {}
    dim = config.embedding_dim

    for node_id in result_ids:
        if node_id not in updated:
            updated[node_id] = displacements.get(
                node_id, np.zeros(dim, dtype=np.float32)
            ).copy()

    for i, id_i in enumerate(result_ids):
        emb_i = original_embeddings.get(id_i)
        if emb_i is None:
            continue
        pos_i = emb_i + updated[id_i]

        for j in range(i + 1, len(result_ids)):
            id_j = result_ids[j]
            emb_j = original_embeddings.get(id_j)
            if emb_j is None:
                continue
            pos_j = emb_j + updated[id_j]

            mass_i = masses.get(id_i, 1.0)
            mass_j = masses.get(id_j, 1.0)

            # i pulled toward j
            delta_i = compute_gravitational_force(pos_i, pos_j, mass_i, mass_j, config)
            updated[id_i] = updated[id_i] + delta_i

            # j pulled toward i
            delta_j = compute_gravitational_force(pos_j, pos_i, mass_j, mass_i, config)
            updated[id_j] = updated[id_j] + delta_j

    # Clamp displacement norms
    for node_id in updated:
        updated[node_id] = clamp_displacement(updated[node_id], config.max_displacement_norm)

    return updated


def apply_displacement_decay(
    displacement: np.ndarray,
    displacement_decay: float,
    last_access: float,
    now: float,
    age_delta: float,
) -> np.ndarray:
    """Decay displacement toward zero (return to original position).

    Two decay factors:
    - displacement_decay: constant per-step decay
    - age_factor: stronger decay for nodes not recently accessed
    """
    age_factor = math.exp(-age_delta * (now - last_access))
    return (displacement * displacement_decay * age_factor).astype(np.float32)


def clamp_displacement(displacement: np.ndarray, max_norm: float) -> np.ndarray:
    """Clamp displacement vector to maximum L2 norm."""
    norm = float(np.linalg.norm(displacement))
    if norm > max_norm:
        return (displacement * (max_norm / norm)).astype(np.float32)
    return displacement


# -----------------------------------------------------------------------
# Gravity Wave Propagation
# -----------------------------------------------------------------------

def propagate_gravity_wave(
    query_vector: np.ndarray,
    faiss_index: "FaissIndex",
    cache: "CacheLayer",
    config: GERConfig,
    wave_k: int | None = None,
    wave_depth: int | None = None,
) -> dict[str, float]:
    """Propagate gravity wave recursively through embedding space.

    Starting from FAISS top-k seed nodes, recursively expand each node's
    neighbors. Mass determines how many neighbors each node attracts.
    Force attenuates with depth.

    Args:
        query_vector: Query embedding (1D or 2D)
        faiss_index: FAISS index for neighbor searches
        cache: Cache layer for node state access
        config: GER configuration
        wave_k: Override initial top-k (default: config.wave_initial_k)
        wave_depth: Override max depth (default: config.wave_max_depth)

    Returns:
        Dict mapping node_id -> total accumulated force
    """
    initial_k = wave_k if wave_k is not None else config.wave_initial_k
    max_depth = wave_depth if wave_depth is not None else config.wave_max_depth

    qv = query_vector[0] if query_vector.ndim == 2 else query_vector

    # Step 1: Seed nodes from query
    seeds = faiss_index.search(qv.reshape(1, -1), initial_k)
    if not seeds:
        return {}

    # node_id -> total accumulated force
    reached: dict[str, float] = {}
    # Current frontier: [(node_id, force)]
    frontier: list[tuple[str, float]] = [(nid, 1.0) for nid, _ in seeds]

    for nid, force in frontier:
        reached[nid] = max(reached.get(nid, 0.0), force)

    # Step 2: Recursive expansion
    for depth in range(1, max_depth + 1):
        next_frontier: list[tuple[str, float]] = []

        for node_id, parent_force in frontier:
            # Get node mass to determine top-k and attenuation
            state = cache.get_node(node_id)
            mass = state.mass if state else 1.0

            node_k = config.compute_node_top_k(mass)
            attenuation = config.compute_effective_attenuation(mass)
            child_force = parent_force * attenuation

            # Skip if force is negligible
            if child_force < 0.001:
                continue

            # Find neighbors of this node
            neighbors = faiss_index.search_by_id(node_id, node_k + 1)  # +1 to exclude self

            for neighbor_id, _ in neighbors:
                if neighbor_id == node_id:
                    continue

                # Accumulate force (sum from multiple paths)
                old_force = reached.get(neighbor_id, 0.0)
                new_force = old_force + child_force
                reached[neighbor_id] = new_force

                # Only expand nodes that are newly reached or significantly boosted
                if old_force == 0.0:
                    next_frontier.append((neighbor_id, child_force))

        frontier = next_frontier

    return reached
