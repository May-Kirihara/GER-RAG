"""Gravitational dynamics: displacement, velocity, orbital mechanics.

Documents attract each other in embedding space through Newtonian gravity.
Velocity vectors give nodes inertia, enabling orbits, cometary trajectories,
and gravitational slingshots.
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


# -----------------------------------------------------------------------
# Virtual position
# -----------------------------------------------------------------------

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
    norm = np.linalg.norm(pos)
    if norm > 0:
        pos = pos / norm
    return pos


def clamp_vector(vec: np.ndarray, max_norm: float) -> np.ndarray:
    """Clamp vector to maximum L2 norm."""
    norm = float(np.linalg.norm(vec))
    if norm > max_norm:
        return (vec * (max_norm / norm)).astype(np.float32)
    return vec


# Legacy alias
clamp_displacement = clamp_vector


# -----------------------------------------------------------------------
# Orbital mechanics (Stage 1-2-3)
# -----------------------------------------------------------------------

def compute_bh_acceleration(
    pos_i: np.ndarray,
    node_id: str,
    temperature_i: float,
    cache: "CacheLayer",
    all_positions: dict[str, np.ndarray],
    config: GERConfig,
) -> np.ndarray:
    """Compute gravitational acceleration from co-occurrence cluster black hole.

    The BH is located at the weighted centroid of co-occurrence neighbors.
    Its mass is proportional to log(1 + total_edge_weight).

    Two dampening mechanisms:
    - Presentation saturation: neighbors returned often to LLM contribute less to BH mass
    - Thermal escape: high-temperature nodes resist BH capture
    """
    neighbors = cache.get_neighbors(node_id)
    if not neighbors:
        return np.zeros_like(pos_i)

    total_weight = 0.0
    centroid = np.zeros_like(pos_i)
    for neighbor_id, weight in neighbors.items():
        pos_j = all_positions.get(neighbor_id)
        if pos_j is None:
            continue
        # Saturation: neighbors returned often contribute less to BH
        neighbor_state = cache.get_node(neighbor_id)
        rc = neighbor_state.return_count if neighbor_state else 0.0
        saturation = 1.0 / (1.0 + rc * config.saturation_rate)
        effective_weight = weight * saturation

        centroid = centroid + effective_weight * pos_j
        total_weight += effective_weight

    if total_weight < 1e-8:
        return np.zeros_like(pos_i)

    centroid = centroid / total_weight
    bh_mass = config.bh_mass_scale * math.log(1.0 + total_weight)

    diff = centroid - pos_i
    distance_sq = float(np.dot(diff, diff)) + config.gravity_epsilon
    distance = math.sqrt(distance_sq)
    G = config.bh_gravity_G if config.bh_gravity_G > 0 else config.gravity_G
    magnitude = G * bh_mass / distance_sq
    direction = diff / distance

    # Thermal escape: high temperature nodes resist BH capture
    escape_factor = 1.0 / (1.0 + temperature_i * config.thermal_escape_scale)

    return (magnitude * direction * escape_factor).astype(np.float32)


def compute_acceleration(
    pos_i: np.ndarray,
    original_pos_i: np.ndarray,
    displacement_i: np.ndarray,
    neighbors: list[tuple[np.ndarray, float]],
    config: GERConfig,
    node_id: str | None = None,
    cache: "CacheLayer | None" = None,
    all_positions: dict[str, np.ndarray] | None = None,
) -> np.ndarray:
    """Compute total gravitational acceleration on node i.

    Three components:
    1. Neighbor gravity: a = Σ_j [ G * m_j / (r² + ε) * direction(i→j) ]
    2. Anchor restoring force: a = -k * displacement (Hooke's law)
    3. Co-occurrence BH gravity: attraction toward cluster centroid
    """
    acc = np.zeros_like(pos_i)

    # 1. Neighbor gravitational acceleration
    for pos_j, mass_j in neighbors:
        diff = pos_j - pos_i
        distance_sq = float(np.dot(diff, diff)) + config.gravity_epsilon
        distance = math.sqrt(distance_sq)
        magnitude = config.gravity_G * mass_j / distance_sq
        direction = diff / distance
        acc = acc + magnitude * direction

    # 2. Anchor restoring force (Hooke's law)
    acc = acc - config.orbital_anchor_strength * displacement_i

    # 3. Co-occurrence black hole gravity (with saturation + thermal escape)
    if node_id is not None and cache is not None and all_positions is not None:
        node_state = cache.get_node(node_id)
        temp_i = node_state.temperature if node_state else 0.0
        acc = acc + compute_bh_acceleration(pos_i, node_id, temp_i, cache, all_positions, config)

    return acc.astype(np.float32)


def update_velocity(
    velocity: np.ndarray,
    acceleration: np.ndarray,
    last_access: float,
    now: float,
    config: GERConfig,
) -> np.ndarray:
    """Update velocity: v += a*dt, apply friction, clamp.

    Two friction sources:
    - Constant: v *= (1 - friction)
    - Age-based: older nodes get additional friction
    """
    # Stage 2a: Apply acceleration (dt = 1.0 per query step)
    v = velocity + acceleration

    # Stage 2b: Constant friction
    v = v * (1.0 - config.orbital_friction)

    # Stage 2c: Age-based friction (unaccessed nodes slow down more)
    age = now - last_access
    age_friction = config.orbital_friction_age_factor * (1.0 - math.exp(-config.displacement_age_delta * age))
    v = v * (1.0 - age_friction)

    # Stage 2d: Clamp velocity
    v = clamp_vector(v, config.orbital_max_velocity)

    return v.astype(np.float32)


def update_orbital_state(
    node_ids: list[str],
    original_embeddings: dict[str, np.ndarray],
    displacements: dict[str, np.ndarray],
    velocities: dict[str, np.ndarray],
    masses: dict[str, float],
    last_accesses: dict[str, float],
    now: float,
    config: GERConfig,
    cache: "CacheLayer | None" = None,
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Full orbital mechanics step for all nodes.

    Stage 1: Compute accelerations (neighbor gravity + anchor + co-occurrence BH)
    Stage 2: Update velocities (+ friction)
    Stage 3: Update displacements (+ clamp)

    Returns: (updated_displacements, updated_velocities)
    """
    dim = config.embedding_dim

    # Gather current virtual positions
    positions: dict[str, np.ndarray] = {}
    for nid in node_ids:
        emb = original_embeddings.get(nid)
        if emb is None:
            continue
        disp = displacements.get(nid, np.zeros(dim, dtype=np.float32))
        positions[nid] = emb + disp

    active_ids = [nid for nid in node_ids if nid in positions]
    if len(active_ids) < 2:
        return displacements, velocities

    # Stage 1: Compute acceleration for each node
    accelerations: dict[str, np.ndarray] = {}
    for nid in active_ids:
        neighbors = [
            (positions[other], masses.get(other, 1.0))
            for other in active_ids
            if other != nid
        ]
        disp = displacements.get(nid, np.zeros(dim, dtype=np.float32))
        accelerations[nid] = compute_acceleration(
            positions[nid], original_embeddings[nid], disp, neighbors, config,
            node_id=nid, cache=cache, all_positions=positions,
        )

    # Stage 2 & 3: Update velocity then displacement
    new_displacements: dict[str, np.ndarray] = {}
    new_velocities: dict[str, np.ndarray] = {}

    for nid in active_ids:
        old_vel = velocities.get(nid, np.zeros(dim, dtype=np.float32))
        old_disp = displacements.get(nid, np.zeros(dim, dtype=np.float32))
        last_access = last_accesses.get(nid, now)

        # Stage 2: velocity update
        new_vel = update_velocity(old_vel, accelerations[nid], last_access, now, config)

        # Stage 3: position update (displacement += velocity * dt)
        new_disp = old_disp + new_vel  # dt = 1.0
        new_disp = clamp_vector(new_disp, config.max_displacement_norm)

        new_velocities[nid] = new_vel
        new_displacements[nid] = new_disp

    return new_displacements, new_velocities


# -----------------------------------------------------------------------
# Legacy functions (kept for backward compatibility)
# -----------------------------------------------------------------------

def compute_gravitational_force(
    pos_i: np.ndarray, pos_j: np.ndarray,
    mass_i: float, mass_j: float, config: GERConfig,
) -> np.ndarray:
    """Legacy: compute force vector (used by old displacement-only model)."""
    diff = pos_j - pos_i
    distance_sq = float(np.dot(diff, diff)) + config.gravity_epsilon
    distance = math.sqrt(distance_sq)
    force_magnitude = config.gravity_G * mass_i * mass_j / distance_sq
    direction = diff / distance
    delta = config.gravity_eta * force_magnitude * direction
    return delta.astype(np.float32)


def apply_displacement_decay(
    displacement: np.ndarray, displacement_decay: float,
    last_access: float, now: float, age_delta: float,
) -> np.ndarray:
    """Legacy: direct displacement decay (replaced by friction in orbital model)."""
    age_factor = math.exp(-age_delta * (now - last_access))
    return (displacement * displacement_decay * age_factor).astype(np.float32)


# -----------------------------------------------------------------------
# Gravity Wave Propagation (unchanged)
# -----------------------------------------------------------------------

def propagate_gravity_wave(
    query_vector: np.ndarray,
    faiss_index: "FaissIndex",
    cache: "CacheLayer",
    config: GERConfig,
    wave_k: int | None = None,
    wave_depth: int | None = None,
) -> dict[str, float]:
    """Propagate gravity wave recursively through embedding space."""
    initial_k = wave_k if wave_k is not None else config.wave_initial_k
    max_depth = wave_depth if wave_depth is not None else config.wave_max_depth

    qv = query_vector[0] if query_vector.ndim == 2 else query_vector

    seeds = faiss_index.search(qv.reshape(1, -1), initial_k)
    if not seeds:
        return {}

    reached: dict[str, float] = {}
    frontier: list[tuple[str, float]] = [(nid, 1.0) for nid, _ in seeds]

    for nid, force in frontier:
        reached[nid] = max(reached.get(nid, 0.0), force)

    for depth in range(1, max_depth + 1):
        next_frontier: list[tuple[str, float]] = []

        for node_id, parent_force in frontier:
            state = cache.get_node(node_id)
            mass = state.mass if state else 1.0

            node_k = config.compute_node_top_k(mass)
            attenuation = config.compute_effective_attenuation(mass)
            child_force = parent_force * attenuation

            if child_force < 0.001:
                continue

            min_sim = config.compute_gravity_radius(mass)
            neighbors = faiss_index.search_by_id(node_id, node_k + 1)

            for neighbor_id, sim_score in neighbors:
                if neighbor_id == node_id:
                    continue
                if sim_score < min_sim:
                    continue

                old_force = reached.get(neighbor_id, 0.0)
                new_force = old_force + child_force
                reached[neighbor_id] = new_force

                if old_force == 0.0:
                    next_frontier.append((neighbor_id, child_force))

        frontier = next_frontier

    return reached
