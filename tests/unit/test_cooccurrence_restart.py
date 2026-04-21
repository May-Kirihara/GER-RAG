"""Regression test for B-02: co-occurrence counter reset on restart.

The in-memory `_cooccurrence_counts` is defaultdict(int) and is not persisted.
After a process restart, existing edges should still be *strengthened* on each
subsequent co-occurrence, even though the counter starts from zero. Before the
fix, strengthening required re-hitting `edge_threshold` from scratch, silently
dropping the first N-1 events for every previously-formed edge.
"""
from __future__ import annotations

from gaottt.config import GaOTTTConfig
from gaottt.graph.cooccurrence import CooccurrenceGraph
from gaottt.store.cache import CacheLayer


def test_existing_edge_is_strengthened_immediately_after_restart():
    config = GaOTTTConfig(edge_threshold=5)
    cache = CacheLayer()
    cache.set_edge("a", "b", 4.0)  # Pretend this was loaded from DB after restart.

    graph = CooccurrenceGraph(config, cache)
    # Counter starts at 0, but the edge already exists — the first co-occurrence
    # must still strengthen the edge rather than waiting for 5 events.
    graph.update_cooccurrence(["a", "b"])

    assert cache.get_neighbors("a")["b"] == 5.0


def test_pending_pair_still_requires_threshold():
    """For brand-new pairs (no existing edge), threshold gating is preserved."""
    config = GaOTTTConfig(edge_threshold=3)
    cache = CacheLayer()
    graph = CooccurrenceGraph(config, cache)

    graph.update_cooccurrence(["x", "y"])  # count=1, no edge
    graph.update_cooccurrence(["x", "y"])  # count=2, no edge
    assert "y" not in cache.get_neighbors("x")

    graph.update_cooccurrence(["x", "y"])  # count=3, edge forms
    assert cache.get_neighbors("x")["y"] == 1.0

    graph.update_cooccurrence(["x", "y"])  # edge strengthens immediately
    assert cache.get_neighbors("x")["y"] == 2.0
