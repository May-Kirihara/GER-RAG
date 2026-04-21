"""End-to-end tests for engine.merge() / engine.compact() with stub embedder."""
from __future__ import annotations

import time

import pytest

from ger_rag.config import GERConfig
from ger_rag.core.engine import GEREngine
from ger_rag.index.faiss_index import FaissIndex
from ger_rag.store.cache import CacheLayer
from ger_rag.store.sqlite_store import SqliteStore
from tests.integration.test_engine_archive_ttl import StubEmbedder


@pytest.fixture
async def engine(tmp_path):
    cfg = GERConfig(
        embedding_dim=32,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "ger.db"),
        faiss_index_path=str(tmp_path / "ger.faiss"),
        flush_interval_seconds=999.0,
        wave_initial_k=5,
        wave_max_depth=1,
    )
    eng = GEREngine(
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


async def test_manual_merge_combines_mass_and_archives_absorbed(engine):
    ids = await engine.index_documents([
        {"content": "uv tooling preference"},
        {"content": "uv tooling preference duplicate"},
    ])
    assert len(ids) == 2

    # Drive masses up so merge has interesting numbers
    for _ in range(3):
        await engine.query(text="uv tooling")

    state_a = engine.cache.get_node(ids[0])
    state_b = engine.cache.get_node(ids[1])
    initial_total = state_a.mass + state_b.mass

    outcomes = await engine.merge(ids)
    assert len(outcomes) == 1
    survivor_id = outcomes[0].survivor_id
    absorbed_id = outcomes[0].absorbed_id

    survivor_state = engine.cache.get_node(survivor_id)
    assert abs(survivor_state.mass - initial_total) < 1e-3
    assert engine.cache.get_node(absorbed_id) is None

    persisted = await engine.store.get_node_states([absorbed_id])
    assert persisted[absorbed_id].is_archived is True
    assert persisted[absorbed_id].merged_into == survivor_id
    assert persisted[absorbed_id].merge_count == 1


async def test_merge_with_explicit_keep(engine):
    ids = await engine.index_documents([
        {"content": "alpha note"},
        {"content": "beta note"},
        {"content": "gamma note"},
    ])
    outcomes = await engine.merge(ids, keep=ids[1])
    assert len(outcomes) == 2
    assert all(o.survivor_id == ids[1] for o in outcomes)


async def test_merge_skips_when_fewer_than_two_active_nodes(engine):
    ids = await engine.index_documents([{"content": "lone fact"}])
    outcomes = await engine.merge(ids)
    assert outcomes == []


async def test_compact_expires_ttl_and_rebuilds_faiss(engine):
    past = time.time() - 1.0
    ids = await engine.index_documents([
        {"content": "stale hypothesis", "expires_at": past},
        {"content": "fresh fact"},
    ])
    vectors_before = engine.faiss_index.size
    assert vectors_before == 2

    report = await engine.compact(expire_ttl=True, rebuild_faiss=True, auto_merge=False)
    assert report["expired"] == 1
    assert report["faiss_rebuilt"] is True
    assert report["vectors_after"] == 1

    # Stale node is gone from query results
    results = await engine.query(text="hypothesis", top_k=5)
    returned = {r.id for r in results}
    assert ids[0] not in returned


async def test_compact_auto_merge_collapses_duplicates(engine):
    # Identical content embeddings → identical vectors → cosine sim = 1.0
    ids = await engine.index_documents([
        {"content": "auto merge candidate"},
        {"content": "auto merge candidate"},
        {"content": "completely unrelated"},
    ])
    # Index dedup will skip the second identical content; force a near-duplicate
    # via a tiny token tweak that still scores ≥ 0.95 in the stub embedder.
    extra = await engine.index_documents([
        {"content": "auto merge candidate plus"},
    ])
    ids = ids + extra
    assert len(ids) >= 3

    report = await engine.compact(
        expire_ttl=False,
        rebuild_faiss=False,
        auto_merge=True,
        merge_threshold=0.85,
    )
    assert report["merged_pairs"] >= 1


async def test_find_duplicates_returns_clusters(engine):
    await engine.index_documents([
        {"content": "tidal force memo"},
        {"content": "tidal force memo extra"},
        {"content": "completely different topic"},
    ])
    clusters = engine.find_duplicates(threshold=0.7)
    assert any(len(c.ids) >= 2 for c in clusters)
