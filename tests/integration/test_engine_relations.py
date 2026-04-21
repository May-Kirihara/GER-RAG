"""End-to-end tests for engine relate/unrelate/get_relations + compact orphan cleanup."""
from __future__ import annotations

import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.index.faiss_index import FaissIndex
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore
from tests.integration.test_engine_archive_ttl import StubEmbedder


@pytest.fixture
async def engine(tmp_path):
    cfg = GaOTTTConfig(
        embedding_dim=32,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "ger.db"),
        faiss_index_path=str(tmp_path / "ger.faiss"),
        flush_interval_seconds=999.0,
    )
    eng = GaOTTTEngine(
        config=cfg,
        embedder=StubEmbedder(dimension=32),
        faiss_index=FaissIndex(dimension=32),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=cfg.db_path),
    )
    await eng.startup()
    try:
        yield eng
    finally:
        await eng.shutdown()


async def test_relate_creates_typed_directed_edge(engine):
    ids = await engine.index_documents([
        {"content": "old judgment about thing"},
        {"content": "new revised judgment about thing"},
    ])
    edge = await engine.relate(
        src_id=ids[1], dst_id=ids[0], edge_type="supersedes",
        metadata={"reason": "found counter-evidence"},
    )
    assert edge.edge_type == "supersedes"
    assert edge.metadata == {"reason": "found counter-evidence"}

    fetched = await engine.get_relations(ids[1])
    assert len(fetched) == 1
    assert fetched[0].dst == ids[0]


async def test_relate_rejects_self_loop(engine):
    ids = await engine.index_documents([{"content": "lone fact"}])
    with pytest.raises(ValueError):
        await engine.relate(ids[0], ids[0], edge_type="supersedes")


async def test_unrelate_removes_specific_type(engine):
    ids = await engine.index_documents([
        {"content": "node a"}, {"content": "node b"},
    ])
    await engine.relate(ids[0], ids[1], edge_type="supersedes")
    await engine.relate(ids[0], ids[1], edge_type="derived_from")

    n = await engine.unrelate(ids[0], ids[1], edge_type="supersedes")
    assert n == 1
    remaining = await engine.get_relations(ids[0])
    assert len(remaining) == 1
    assert remaining[0].edge_type == "derived_from"


async def test_get_relations_direction_filters(engine):
    ids = await engine.index_documents([
        {"content": "a"}, {"content": "b"}, {"content": "c"},
    ])
    await engine.relate(ids[0], ids[1], edge_type="supersedes")
    await engine.relate(ids[2], ids[0], edge_type="contradicts")

    out_only = await engine.get_relations(ids[0], direction="out")
    in_only = await engine.get_relations(ids[0], direction="in")
    both = await engine.get_relations(ids[0], direction="both")
    assert {e.dst for e in out_only} == {ids[1]}
    assert {e.src for e in in_only} == {ids[2]}
    assert len(both) == 2


async def test_hard_delete_cascades_directed_edges(engine):
    ids = await engine.index_documents([
        {"content": "doomed"}, {"content": "survivor"},
    ])
    await engine.relate(ids[0], ids[1], edge_type="supersedes")
    await engine.forget([ids[0]], hard=True)
    remaining = await engine.get_relations(ids[1], direction="both")
    assert remaining == []


async def test_compact_removes_orphan_relations(engine):
    # Direct-insert an edge whose endpoints don't exist (simulates an external
    # orphan, e.g. from a botched migration). hard_delete_nodes already cascades
    # so we can't use it to produce an orphan.
    from gaottt.core.types import DirectedEdge
    await engine.store.upsert_directed_edge(DirectedEdge(
        src="ghost-src", dst="ghost-dst", edge_type="supersedes",
    ))

    report = await engine.compact(rebuild_faiss=False, expire_ttl=False)
    assert report["orphan_relations_removed"] >= 1
    remaining = await engine.store.get_directed_edges()
    assert all(e.src != "ghost-src" for e in remaining)


async def test_filter_by_edge_type(engine):
    ids = await engine.index_documents([
        {"content": "a"}, {"content": "b"}, {"content": "c"},
    ])
    await engine.relate(ids[0], ids[1], edge_type="supersedes")
    await engine.relate(ids[0], ids[2], edge_type="derived_from")

    out = await engine.get_relations(ids[0], edge_type="supersedes")
    assert len(out) == 1
    assert out[0].dst == ids[1]
