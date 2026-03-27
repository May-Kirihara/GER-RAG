"""Gravitational displacement computation.

Documents attract each other in embedding space through gravitational force
proportional to their mass and inversely proportional to distance squared.
"""

from __future__ import annotations

import math

import numpy as np

from ger_rag.config import GERConfig


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
