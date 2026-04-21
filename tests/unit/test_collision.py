import time

import numpy as np

from gaottt.config import GaOTTTConfig
from gaottt.core.collision import (
    compose_displacement,
    compose_mass,
    compose_velocity,
    merge_pair,
    pick_survivor,
)
from gaottt.core.types import NodeState
from gaottt.store.cache import CacheLayer


def test_pick_survivor_prefers_heavier():
    a = NodeState(id="a", mass=2.0, last_access=100.0)
    b = NodeState(id="b", mass=5.0, last_access=50.0)
    survivor, absorbed = pick_survivor(a, b)
    assert survivor.id == "b"
    assert absorbed.id == "a"


def test_pick_survivor_breaks_tie_by_recency():
    a = NodeState(id="a", mass=3.0, last_access=200.0)
    b = NodeState(id="b", mass=3.0, last_access=100.0)
    survivor, absorbed = pick_survivor(a, b)
    assert survivor.id == "a"


def test_compose_mass_caps_at_max():
    assert compose_mass(40.0, 30.0, 50.0) == 50.0
    assert compose_mass(2.0, 3.0, 50.0) == 5.0


def test_compose_velocity_is_momentum_weighted():
    v_s = np.array([1.0, 0.0], dtype=np.float32)
    v_a = np.array([0.0, 1.0], dtype=np.float32)
    out = compose_velocity(v_s, 3.0, v_a, 1.0)
    assert np.allclose(out, [0.75, 0.25])


def test_compose_displacement_is_norm_clipped():
    d_s = np.array([10.0, 0.0], dtype=np.float32)
    d_a = np.array([10.0, 0.0], dtype=np.float32)
    out = compose_displacement(d_s, 1.0, d_a, 1.0, max_norm=0.3)
    assert np.linalg.norm(out) <= 0.3 + 1e-6


def test_merge_pair_archives_absorbed_and_redirects_edges():
    cfg = GaOTTTConfig(embedding_dim=4)
    cache = CacheLayer()
    survivor = NodeState(id="s", mass=5.0, last_access=time.time())
    absorbed = NodeState(id="x", mass=2.0, last_access=time.time())
    other = NodeState(id="y", mass=1.0)
    cache.set_node(survivor)
    cache.set_node(absorbed)
    cache.set_node(other)
    cache.set_edge("x", "y", 3.0)
    cache.set_edge("x", "s", 7.0)  # will be removed (would create self-loop)

    merge_pair(survivor, absorbed, cache, cfg)

    assert absorbed.is_archived is True
    assert absorbed.merged_into == "s"
    assert absorbed.merge_count == 1
    assert survivor.merge_count == 1
    assert survivor.mass == 7.0  # 5 + 2

    edges = cache.get_all_edges()
    edge_pairs = {tuple(sorted([e.src, e.dst])) for e in edges}
    assert ("s", "y") in edge_pairs
    # Direct s<->x edge should not survive
    assert ("s", "x") not in edge_pairs and ("x", "s") not in edge_pairs


def test_merge_pair_preserves_velocity_momentum():
    cfg = GaOTTTConfig(embedding_dim=2)
    cache = CacheLayer()
    s = NodeState(id="s", mass=3.0)
    a = NodeState(id="a", mass=1.0)
    cache.set_node(s)
    cache.set_node(a)
    cache.set_velocity("s", np.array([1.0, 0.0], dtype=np.float32))
    cache.set_velocity("a", np.array([0.0, 1.0], dtype=np.float32))

    merge_pair(s, a, cache, cfg)

    out_v = cache.get_velocity("s")
    assert np.allclose(out_v, [0.75, 0.25])
