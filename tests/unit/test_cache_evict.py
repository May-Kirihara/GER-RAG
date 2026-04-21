"""Cache-level eviction used by archive/forget."""
from __future__ import annotations

import numpy as np

from ger_rag.core.types import NodeState
from ger_rag.store.cache import CacheLayer


def test_evict_drops_node_displacement_velocity_and_edges():
    cache = CacheLayer()
    cache.set_node(NodeState(id="a"))
    cache.set_node(NodeState(id="b"))
    cache.set_node(NodeState(id="c"))
    cache.set_displacement("a", np.zeros(4, dtype=np.float32))
    cache.set_velocity("a", np.zeros(4, dtype=np.float32))
    cache.set_edge("a", "b", 1.0)
    cache.set_edge("a", "c", 1.0)
    cache.set_edge("b", "c", 1.0)

    cache.evict_node("a")

    assert cache.get_node("a") is None
    assert cache.get_displacement("a") is None
    assert cache.get_velocity("a") is None
    edges = cache.get_all_edges()
    assert all("a" not in (e.src, e.dst) for e in edges)
    # Surviving edge is preserved.
    surviving = [e for e in edges if {e.src, e.dst} == {"b", "c"}]
    assert len(surviving) == 1


def test_evict_is_idempotent_for_unknown_id():
    cache = CacheLayer()
    cache.evict_node("never-existed")  # should not raise
    assert cache.get_node("never-existed") is None
