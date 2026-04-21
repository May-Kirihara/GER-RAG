"""End-to-end tests for engine.prefetch + query(use_cache) integration (F6)."""
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
        prefetch_cache_size=8,
        prefetch_ttl_seconds=60.0,
        prefetch_max_concurrent=2,
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


async def test_query_without_cache_does_not_populate_cache(engine):
    await engine.index_documents([{"content": "uv tooling note"}])
    results = await engine.query(text="uv tooling", top_k=3)
    assert len(results) >= 1
    assert engine.prefetch_cache.stats()["size"] == 0


async def test_query_with_cache_populates_on_miss(engine):
    await engine.index_documents([{"content": "uv tooling note"}])
    results = await engine.query(text="uv tooling", top_k=3, use_cache=True)
    assert len(results) >= 1
    stats = engine.prefetch_cache.stats()
    assert stats["size"] == 1
    assert stats["misses"] == 1
    assert stats["hits"] == 0


async def test_query_with_cache_hits_after_first_call(engine):
    await engine.index_documents([{"content": "tidal force memo"}])
    first = await engine.query(text="tidal force", top_k=3, use_cache=True)
    second = await engine.query(text="tidal force", top_k=3, use_cache=True)
    assert first == second  # same list ref or equivalent
    stats = engine.prefetch_cache.stats()
    assert stats["hits"] == 1


async def test_prefetch_pre_warms_cache_for_subsequent_query(engine):
    await engine.index_documents([{"content": "lagrange point bridge"}])
    task = engine.prefetch(text="lagrange", top_k=3)
    await task  # deterministic completion for the test

    # Now query with cache; expect a hit
    results = await engine.query(text="lagrange", top_k=3, use_cache=True)
    assert len(results) >= 1
    stats = engine.prefetch_cache.stats()
    assert stats["hits"] == 1


async def test_prefetch_pool_caps_concurrency(engine):
    # Fire many prefetches; cap is 2.
    await engine.index_documents([
        {"content": f"doc number {i} unique-token-{i}"} for i in range(5)
    ])
    tasks = [engine.prefetch(text=f"unique-token-{i}", top_k=2) for i in range(5)]
    import asyncio as _asyncio
    await _asyncio.gather(*tasks)
    pool_stats = engine.prefetch_status()["pool"]
    assert pool_stats["completed"] == 5
    assert pool_stats["max_concurrent"] == 2


async def test_force_refresh_bypasses_cache(engine):
    """Equivalent to MCP recall(force_refresh=True): use_cache=False."""
    await engine.index_documents([{"content": "force refresh test note"}])
    await engine.query(text="force refresh", top_k=3, use_cache=True)
    # Now call without cache — should still work, and not increment hits
    pre_hits = engine.prefetch_cache.stats()["hits"]
    await engine.query(text="force refresh", top_k=3, use_cache=False)
    assert engine.prefetch_cache.stats()["hits"] == pre_hits


async def test_prefetch_status_reports_cache_and_pool(engine):
    status = engine.prefetch_status()
    assert "cache" in status and "pool" in status
    assert status["cache"]["max_size"] == 8
    assert status["pool"]["max_concurrent"] == 2
