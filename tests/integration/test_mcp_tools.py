"""Round-trip tests for the MCP tool wrappers (forget/restore/auto_remember/remember TTL).

The decorated tools are still plain callables; we patch the engine singleton
in ``gaottt.server.mcp_server`` to a stub-backed engine and invoke the tools
directly.
"""
from __future__ import annotations

import time

import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.index.faiss_index import FaissIndex
from gaottt.server import mcp_server as srv
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore
from tests.integration.test_engine_archive_ttl import StubEmbedder


@pytest.fixture
async def engine_singleton(tmp_path, monkeypatch):
    cfg = GaOTTTConfig(
        embedding_dim=32,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "ger.db"),
        faiss_index_path=str(tmp_path / "ger.faiss"),
        flush_interval_seconds=999.0,
        default_hypothesis_ttl_seconds=60.0,
    )
    eng = GaOTTTEngine(
        config=cfg,
        embedder=StubEmbedder(dimension=32),
        faiss_index=FaissIndex(dimension=32),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=cfg.db_path),
    )
    await eng.startup()
    monkeypatch.setattr(srv, "_engine", eng)
    try:
        yield eng
    finally:
        monkeypatch.setattr(srv, "_engine", None)
        await eng.shutdown()


async def test_remember_then_forget_then_restore_roundtrip(engine_singleton):
    out = await srv.remember(
        content="user prefers uv over pip",
        source="user",
        tags=["preference"],
    )
    assert "Remembered" in out
    node_id = out.split("ID: ")[1].split()[0]

    forget_out = await srv.forget([node_id])
    assert "Archived 1" in forget_out

    recall_out = await srv.recall(query="uv", top_k=5)
    assert "uv over pip" not in recall_out  # archived → filtered

    restore_out = await srv.restore([node_id])
    assert "Restored 1" in restore_out

    recall_out = await srv.recall(query="uv", top_k=5)
    assert "uv over pip" in recall_out


async def test_remember_hypothesis_assigns_default_ttl(engine_singleton):
    out = await srv.remember(
        content="hypothesis: gravity collision could merge similar memories",
        source="hypothesis",
    )
    assert "expires" in out

    node_id = out.split("ID: ")[1].split()[0]
    states = await engine_singleton.store.get_node_states([node_id])
    state = states[node_id]
    assert state.expires_at is not None
    # Within (now, now + default_hypothesis_ttl_seconds + small buffer)
    now = time.time()
    assert now < state.expires_at <= now + 65.0


async def test_remember_explicit_ttl_overrides_default(engine_singleton):
    out = await srv.remember(
        content="explicit short-lived note",
        source="agent",
        ttl_seconds=10.0,
    )
    node_id = out.split("ID: ")[1].split()[0]
    states = await engine_singleton.store.get_node_states([node_id])
    state = states[node_id]
    now = time.time()
    assert state.expires_at is not None
    assert now < state.expires_at <= now + 11.0


async def test_forget_hard_delete_removes_document(engine_singleton):
    out = await srv.remember(content="ephemeral fact to be hard-deleted")
    node_id = out.split("ID: ")[1].split()[0]

    forget_out = await srv.forget([node_id], hard=True)
    assert "Hard-deleted 1" in forget_out

    assert (await engine_singleton.store.get_document(node_id)) is None
    assert (await srv.restore([node_id])) == "Restored 0 of 1 requested memories."


async def test_auto_remember_extracts_candidates_without_saving(engine_singleton):
    transcript = (
        "ok\n"
        "ユーザー: pip禁止。uvを使ってください\n"
        "失敗: numpyにor演算子でValueError。原因はbool変換の曖昧さ\n"
        "今日はいい天気ですね\n"
    )
    out = await srv.auto_remember(transcript=transcript, max_candidates=5)
    assert "Extracted" in out
    assert "uv" in out
    assert "ValueError" in out

    # Nothing should be persisted as a side effect
    states = await engine_singleton.store.get_all_node_states()
    assert states == []


async def test_auto_remember_returns_friendly_message_when_empty(engine_singleton):
    out = await srv.auto_remember(transcript="\n".join(["ok", "thanks", "了解"]))
    assert "No save-worthy candidates" in out


async def test_merge_combines_two_memories(engine_singleton):
    out_a = await srv.remember(content="tidal duplicate one")
    out_b = await srv.remember(content="tidal duplicate one extra")
    id_a = out_a.split("ID: ")[1].split()[0]
    id_b = out_b.split("ID: ")[1].split()[0]

    merge_out = await srv.merge(node_ids=[id_a, id_b])
    assert "Merged 1 node" in merge_out

    states = await engine_singleton.store.get_node_states([id_a, id_b])
    archived = [s for s in states.values() if s.is_archived]
    survivors = [s for s in states.values() if not s.is_archived]
    assert len(archived) == 1
    assert len(survivors) == 1
    assert archived[0].merged_into == survivors[0].id


async def test_merge_respects_keep_argument(engine_singleton):
    out_a = await srv.remember(content="alpha collide")
    out_b = await srv.remember(content="alpha collide variant")
    id_a = out_a.split("ID: ")[1].split()[0]
    id_b = out_b.split("ID: ")[1].split()[0]

    await srv.merge(node_ids=[id_a, id_b], keep=id_b)
    states = await engine_singleton.store.get_node_states([id_a, id_b])
    assert states[id_a].is_archived is True
    assert states[id_b].is_archived is False


async def test_compact_reports_expired_and_rebuilds(engine_singleton):
    import time as _t
    await srv.remember(content="ephemeral note", source="hypothesis", ttl_seconds=0.1)
    _t.sleep(0.2)
    out = await srv.compact(expire_ttl=True, rebuild_faiss=True, auto_merge=False)
    assert "TTL-expired:" in out
    assert "FAISS rebuilt:  True" in out


async def test_reflect_duplicates_lists_clusters_or_friendly_message(engine_singleton):
    await srv.remember(content="duplicate cluster sample")
    await srv.remember(content="duplicate cluster sample extra")
    out = await srv.reflect(aspect="duplicates", limit=5)
    assert "Cluster" in out or "No near-duplicate" in out


async def test_remember_persists_emotion_and_certainty(engine_singleton):
    out = await srv.remember(
        content="emotional and certain memo",
        emotion=-0.8,
        certainty=0.6,
    )
    node_id = out.split("ID: ")[1].split()[0]
    state = engine_singleton.cache.get_node(node_id)
    assert state.emotion_weight == pytest.approx(-0.8)
    assert state.certainty == pytest.approx(0.6)
    assert state.last_verified_at is not None


async def test_revalidate_updates_certainty_and_timestamp(engine_singleton):
    out = await srv.remember(content="fact to revalidate", certainty=0.5)
    node_id = out.split("ID: ")[1].split()[0]

    msg = await srv.revalidate(node_id, certainty=0.95, emotion=0.3)
    assert "Revalidated" in msg
    state = engine_singleton.cache.get_node(node_id)
    assert state.certainty == pytest.approx(0.95)
    assert state.emotion_weight == pytest.approx(0.3)


async def test_revalidate_returns_friendly_message_for_unknown_id(engine_singleton):
    msg = await srv.revalidate("00000000-0000-0000-0000-000000000000", certainty=1.0)
    assert "not found" in msg or "archived" in msg


async def test_relate_unrelate_get_relations_roundtrip(engine_singleton):
    out_a = await srv.remember(content="old judgment about API design")
    out_b = await srv.remember(content="new judgment about API design")
    id_old = out_a.split("ID: ")[1].split()[0]
    id_new = out_b.split("ID: ")[1].split()[0]

    r = await srv.relate(
        src_id=id_new, dst_id=id_old, edge_type="supersedes",
        metadata={"reason": "user feedback"},
    )
    assert "supersedes" in r

    listing = await srv.get_relations(node_id=id_new, direction="out")
    assert "supersedes" in listing

    n = await srv.unrelate(src_id=id_new, dst_id=id_old)
    assert "Removed" in n
    assert (await srv.get_relations(node_id=id_new, direction="out")).startswith(
        "No directed relations"
    )


async def test_reflect_relations_summarizes_typed_edges(engine_singleton):
    out_a = await srv.remember(content="a")
    out_b = await srv.remember(content="b")
    id_a = out_a.split("ID: ")[1].split()[0]
    id_b = out_b.split("ID: ")[1].split()[0]
    await srv.relate(src_id=id_a, dst_id=id_b, edge_type="supersedes")
    out = await srv.reflect(aspect="relations", limit=5)
    assert "supersedes" in out


async def test_prefetch_then_recall_hits_cache(engine_singleton):
    await srv.remember(content="prefetch test memo about tidal")
    msg = await srv.prefetch(query="tidal", top_k=3)
    assert "Scheduled prefetch" in msg

    # Drain background pool so the cache is populated deterministically
    await engine_singleton.prefetch_pool.drain()

    # First recall: cache hit
    await srv.recall(query="tidal", top_k=3)
    status = await srv.prefetch_status()
    assert "hit_rate:" in status

    cache_stats = engine_singleton.prefetch_cache.stats()
    assert cache_stats["hits"] >= 1


async def test_recall_force_refresh_bypasses_cache(engine_singleton):
    await srv.remember(content="force refresh memo")
    await srv.recall(query="force refresh", top_k=3)        # primes cache
    pre_hits = engine_singleton.prefetch_cache.stats()["hits"]

    await srv.recall(query="force refresh", top_k=3, force_refresh=True)
    post_hits = engine_singleton.prefetch_cache.stats()["hits"]
    assert post_hits == pre_hits


async def test_prefetch_status_reports_pool_and_cache_lines(engine_singleton):
    out = await srv.prefetch_status()
    assert "Prefetch cache:" in out
    assert "Prefetch pool:" in out
    assert "max_concurrent" in out or "in_flight" in out
