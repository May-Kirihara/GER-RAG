"""End-to-end tests for emotion / certainty scoring (F7)."""
from __future__ import annotations

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
        emotion_alpha=0.5,            # exaggerate for test signal
        certainty_alpha=0.5,
        certainty_half_life_seconds=86400.0,
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


async def test_emotion_weighted_memory_ranks_higher_than_neutral(engine):
    ids_neutral = await engine.index_documents([
        {"content": "neutral note about uv tooling preference"},
    ])
    ids_emotional = await engine.index_documents([
        {"content": "emotional note about uv tooling preference", "emotion": 0.9},
    ])
    results = await engine.query(text="uv tooling", top_k=5)
    by_id = {r.id: r for r in results}
    assert ids_emotional[0] in by_id
    assert ids_neutral[0] in by_id
    assert by_id[ids_emotional[0]].final_score > by_id[ids_neutral[0]].final_score


async def test_negative_emotion_persists_with_correct_sign(engine):
    # Sign-agnostic boost is unit-tested in test_scorer_f7. Here we just verify
    # the negative value is faithfully persisted through index_documents.
    ids = await engine.index_documents([
        {"content": "frustrating bug memory", "emotion": -0.6},
    ])
    state = engine.cache.get_node(ids[0])
    assert state.emotion_weight == pytest.approx(-0.6)


async def test_certainty_decays_then_revalidate_restores_score(engine):
    ids = await engine.index_documents([
        {"content": "certainty test fact about gravity", "certainty": 1.0},
    ])
    fresh_results = await engine.query(text="gravity", top_k=3)
    fresh_score = next(r.final_score for r in fresh_results if r.id == ids[0])

    # Manually age last_verified_at by one half-life to simulate decay
    state = engine.cache.get_node(ids[0])
    assert state.last_verified_at is not None
    state.last_verified_at -= engine.config.certainty_half_life_seconds
    engine.cache.set_node(state, dirty=True)

    decayed_results = await engine.query(text="gravity", top_k=3)
    decayed_score = next(r.final_score for r in decayed_results if r.id == ids[0])
    assert decayed_score < fresh_score

    await engine.revalidate(ids[0])
    revalidated_results = await engine.query(text="gravity", top_k=3)
    revalidated_score = next(r.final_score for r in revalidated_results if r.id == ids[0])
    # Re-verification refreshes certainty timestamp; should rise back close to fresh
    assert revalidated_score > decayed_score


async def test_revalidate_returns_none_for_archived_node(engine):
    ids = await engine.index_documents([{"content": "to be archived"}])
    await engine.archive(ids)
    result = await engine.revalidate(ids[0], certainty=0.5)
    assert result is None


async def test_revalidate_clamps_inputs_to_valid_range(engine):
    ids = await engine.index_documents([{"content": "clamp test"}])
    state = await engine.revalidate(ids[0], certainty=2.0, emotion=-5.0)
    assert state is not None
    assert state.certainty == 1.0
    assert state.emotion_weight == -1.0
