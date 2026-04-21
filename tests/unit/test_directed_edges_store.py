"""SqliteStore directed-edge CRUD tests (F3)."""
from __future__ import annotations

import pytest

from ger_rag.core.types import DirectedEdge
from ger_rag.store.sqlite_store import SqliteStore


@pytest.fixture
async def store(tmp_path):
    s = SqliteStore(db_path=str(tmp_path / "ger.db"))
    await s.initialize()
    yield s
    await s.close()


async def test_upsert_and_round_trip(store):
    edge = DirectedEdge(
        src="new", dst="old", edge_type="supersedes",
        weight=0.8, metadata={"reason": "factually wrong"},
    )
    await store.upsert_directed_edge(edge)
    out = await store.get_directed_edges(node_id="new", direction="out")
    assert len(out) == 1
    assert out[0].edge_type == "supersedes"
    assert out[0].weight == pytest.approx(0.8)
    assert out[0].metadata == {"reason": "factually wrong"}


async def test_unique_per_type_allows_multiple_edges_between_pair(store):
    await store.upsert_directed_edge(DirectedEdge(
        src="a", dst="b", edge_type="supersedes",
    ))
    await store.upsert_directed_edge(DirectedEdge(
        src="a", dst="b", edge_type="derived_from",
    ))
    out = await store.get_directed_edges(node_id="a")
    types = {e.edge_type for e in out}
    assert types == {"supersedes", "derived_from"}


async def test_upsert_replaces_same_type(store):
    await store.upsert_directed_edge(DirectedEdge(
        src="a", dst="b", edge_type="supersedes", weight=1.0,
    ))
    await store.upsert_directed_edge(DirectedEdge(
        src="a", dst="b", edge_type="supersedes", weight=2.5,
    ))
    out = await store.get_directed_edges(node_id="a")
    assert len(out) == 1
    assert out[0].weight == pytest.approx(2.5)


async def test_direction_filters(store):
    await store.upsert_directed_edge(DirectedEdge(
        src="x", dst="y", edge_type="supersedes",
    ))
    await store.upsert_directed_edge(DirectedEdge(
        src="z", dst="x", edge_type="derived_from",
    ))
    assert len(await store.get_directed_edges(node_id="x", direction="out")) == 1
    assert len(await store.get_directed_edges(node_id="x", direction="in")) == 1
    assert len(await store.get_directed_edges(node_id="x", direction="both")) == 2


async def test_delete_directed_edge_targets_specific_type(store):
    await store.upsert_directed_edge(DirectedEdge(
        src="a", dst="b", edge_type="supersedes",
    ))
    await store.upsert_directed_edge(DirectedEdge(
        src="a", dst="b", edge_type="derived_from",
    ))
    n = await store.delete_directed_edge("a", "b", "supersedes")
    assert n == 1
    remaining = await store.get_directed_edges(node_id="a")
    assert len(remaining) == 1
    assert remaining[0].edge_type == "derived_from"


async def test_delete_directed_edges_for_node_clears_both_directions(store):
    await store.upsert_directed_edge(DirectedEdge(src="x", dst="y", edge_type="supersedes"))
    await store.upsert_directed_edge(DirectedEdge(src="z", dst="x", edge_type="contradicts"))
    n = await store.delete_directed_edges_for_node("x")
    assert n == 2
    assert await store.get_directed_edges(node_id="x", direction="both") == []
