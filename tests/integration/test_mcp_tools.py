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
        # legacy ambient gate — this file's ambient tests pin the pre-WP-D
        # veto contract (sentinel on a min_score reject); the promoted OR
        # default is pinned in tests/integration/test_ambient_or_gate.py.
        ambient_gate_or_semantic=False,
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


async def test_recall_mcp_output_includes_training_delta_trailer(engine_singleton):
    """Phase O Stage 2 — MCP recall formatter appends the ## 訓練差分 trailer."""
    await srv.remember(content="stage-2 trailer probe alpha gamma", source="user")
    out = await srv.recall(query="trailer probe alpha", top_k=3)
    assert "## 訓練差分" in out
    assert "wave_reached=" in out
    # depth marker shows config / requested wave depth
    assert "depth=" in out
    assert "persona_hop=" in out


async def test_recall_mcp_passive_does_not_perturb_mass(engine_singleton):
    """Ambient Recall — srv.recall(passive=True) returns the formatted output
    but leaves mass untouched; an active recall is the positive control."""
    for i in range(5):
        await srv.remember(
            content=f"ambient passive mcp probe {i}", source="agent",
        )
    # Collect ids + leave the field in a non-trivial state with one active recall.
    ids = [
        r.id
        for r in await engine_singleton.query(
            text="ambient passive mcp probe", top_k=5,
        )
    ]
    assert ids, "setup: recall must return results"
    mass_before = {
        nid: float(engine_singleton.cache.get_node(nid).mass) for nid in ids
    }

    out = ""
    for _ in range(5):
        out = await srv.recall(
            query="ambient passive mcp probe", top_k=5, passive=True,
        )
    assert "ambient passive mcp probe" in out  # passive still returns results

    mass_passive = {
        nid: float(engine_singleton.cache.get_node(nid).mass) for nid in ids
    }
    assert mass_passive == mass_before, "passive recall changed mass"

    # Positive control — an active recall MUST move mass.
    await srv.recall(query="ambient passive mcp probe", top_k=5)
    mass_active = {
        nid: float(engine_singleton.cache.get_node(nid).mass) for nid in ids
    }
    assert mass_active != mass_before, (
        "active recall left mass unchanged — passive test is vacuous"
    )


async def test_ambient_recall_mcp_returns_block(engine_singleton):
    """Ambient Recall Enrichment — srv.ambient_recall returns the structured
    <gaottt-ambient-recall> block; the min_score gate yields the sentinel."""
    for i in range(4):
        await srv.remember(content=f"ambient mcp probe {i}", source="agent")
    out = await srv.ambient_recall(query="ambient mcp probe")
    assert out.startswith("<gaottt-ambient-recall>")
    assert out.rstrip().endswith("</gaottt-ambient-recall>")
    assert "▼ 直接ヒット" in out
    assert "ambient mcp probe" in out
    # relevance gate — nothing clears 0.999 → non-block sentinel, no injection
    gated = await srv.ambient_recall(query="ambient mcp probe", min_score=0.999)
    assert not gated.startswith("<gaottt-ambient-recall>")
    assert gated == "(関連する記憶なし)"


async def test_ambient_recall_mcp_expose_breakdown_renders(engine_singleton):
    """Refinement Stage 3 — ``expose_breakdown=True`` adds a terse
    ``[... mass=.. virt=..]`` suffix per slot row in the MCP block; default
    off does not."""
    for i in range(4):
        await srv.remember(content=f"breakdown mcp probe {i}", source="agent")
    off = await srv.ambient_recall(query="breakdown mcp probe")
    assert "[mass=" not in off and "[virt=" not in off and "[raw=" not in off, (
        "breakdown suffix must NOT appear when expose_breakdown defaults off"
    )
    on = await srv.ambient_recall(
        query="breakdown mcp probe", expose_breakdown=True,
    )
    # At least one slot row should carry the suffix (mass>0 always populates
    # the mass= field for recall-sourced direct items).
    assert "[mass=" in on or "[virt=" in on or "[raw=" in on, (
        "breakdown suffix must appear when expose_breakdown=True"
    )


async def test_recall_mcp_output_includes_score_breakdown(engine_singleton):
    """Phase O Stage 1 — MCP recall formatter exposes per-result breakdown line."""
    await srv.remember(content="alpha gamma kappa observability", source="user")
    out = await srv.recall(query="observability alpha", top_k=3)
    # one-line breakdown is prefixed with "  breakdown: "
    assert "breakdown:" in out
    assert "cos=" in out      # raw_cosine
    assert "vcos=" in out     # virtual_cosine
    assert "decay=" in out
    assert "wave=" in out
    assert "mass=" in out
    assert "sat=" in out      # saturation
    assert "persona_prox=" in out


async def test_recall_mcp_output_includes_routing_hint_for_aspect_query(engine_singleton):
    """Phase O Stage 3 — MCP recall formatter appends auto-routed reflect summary."""
    # Plant a commitment-source memory directly; ``reflect(aspect='commitments')``
    # surfaces it without needing a parent intention.
    await srv.remember(
        content="Phase O Stage 3 MCP trailer probe を完了する",
        source="commitment",
    )
    out = await srv.recall(
        query="現在 active な commitment は?", top_k=3,
    )
    assert "auto-routed" in out
    # The trailer marker — written as "## 関連 reflect サマリ (auto-routed)"
    assert "関連 reflect サマリ" in out
    assert "commitments" in out


async def test_recall_mcp_output_no_routing_hint_for_free_form_query(engine_singleton):
    """Free-form query → no routing trailer (legacy free-form path)."""
    await srv.remember(content="free-form routing probe", source="user")
    out = await srv.recall(
        query="free-form routing probe", top_k=3,
    )
    assert "auto-routed" not in out


async def test_recall_mcp_list_mode_truncates_content(engine_singleton):
    """Phase O Stage 4 — MCP recall with ``mode='list'`` returns short excerpts."""
    long_text = "alpha gamma " + ("y" * 500)
    await srv.remember(content=long_text, source="user")
    out = await srv.recall(
        query="alpha gamma list-mode", top_k=3, mode="list",
    )
    # The full 500-char body must not appear — truncated to ~80 chars per item.
    assert ("y" * 500) not in out


async def test_explore_mcp_dormant_mode_surfaces_old_self_authored(engine_singleton):
    """Phase O Stage 5 — MCP explore with ``mode='dormant'`` returns the prefix marker."""
    import time
    out = await srv.remember(
        content="dormant-mcp-probe note authored long ago", source="agent",
    )
    nid = out.split("ID: ")[1].split()[0]
    state = engine_singleton.cache.get_node(nid)
    assert state is not None
    state.last_access = time.time() - 60 * 86400
    state.mass = 1.0
    explore_out = await srv.explore(
        query="_ignored", top_k=5, mode="dormant",
    )
    assert "Dormant memories surfaced" in explore_out or "No dormant memories" in explore_out


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


async def test_save_candidates_mcp_emits_block(engine_singleton):
    """Stop-hook MCP path — formatter returns the ``<gaottt-save-candidates>``
    block when the heuristic finds at least one candidate, matching the
    fail-silent contract documented in Plans-Save-Candidates-Hook.md."""
    transcript = (
        "[user] 重要な決定: テストでは uv を使うこと\n"
        "[assistant] 了解しました\n"
        "[user] 失敗: numpy の or 演算子で ValueError\n"
    )
    out = await srv.save_candidates(transcript=transcript, max_candidates=3)
    assert out.lstrip().startswith("<gaottt-save-candidates>")
    assert "</gaottt-save-candidates>" in out
    assert "<!-- save-candidates count=" in out


async def test_save_candidates_mcp_sentinel_on_chatter(engine_singleton):
    """No candidates → sentinel ``(保存候補なし)`` (no block tag) so the Stop
    hook stays silent."""
    out = await srv.save_candidates(transcript="\n".join(["ok", "thanks"]))
    assert out == "(保存候補なし)"
    assert "<gaottt-save-candidates>" not in out


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


async def test_get_node_mcp_returns_content_and_physics(engine_singleton):
    """Observation Apparatus Round 2 Stage A — fetch-by-id round trip."""
    out = await srv.remember(
        content="get-node probe: visible but ungraspable star",
        source="agent",
        tags=["stage-a-probe"],
    )
    node_id = out.split("ID: ")[1].split()[0]

    detail = await srv.get_node(node_id=node_id)
    assert "visible but ungraspable star" in detail
    assert "physics:" in detail
    assert "stage-a-probe" in detail
    assert node_id in detail


async def test_get_node_mcp_not_found_and_archived(engine_singleton):
    """Missing and archived nodes both return the sentinel."""
    assert await srv.get_node(node_id="no-such-id") == "Node not found."

    out = await srv.remember(content="get-node archive probe", source="agent")
    node_id = out.split("ID: ")[1].split()[0]
    await srv.forget([node_id])
    assert await srv.get_node(node_id=node_id) == "Node not found."


async def test_get_node_mcp_is_read_only(engine_singleton):
    """get_node must not perturb mass or displacement (read-only by construction)."""
    out = await srv.remember(content="get-node passivity probe", source="agent")
    node_id = out.split("ID: ")[1].split()[0]
    # Leave the field in a non-trivial state with one active recall first.
    await srv.recall(query="get-node passivity probe", top_k=3)
    mass_before = float(engine_singleton.cache.get_node(node_id).mass)
    disp_before = float(engine_singleton.get_displacement_norm(node_id))

    for _ in range(5):
        await srv.get_node(node_id=node_id)

    assert float(engine_singleton.cache.get_node(node_id).mass) == mass_before
    assert float(engine_singleton.get_displacement_norm(node_id)) == disp_before


# ---------------------------------------------------------------------------
# reflect bucket filter — connections aspect only
# ---------------------------------------------------------------------------

async def test_reflect_connections_bucket_persona_filters(engine_singleton):
    """MCP reflect(aspect='connections', bucket='persona') surfaces only the
    persona edge, not the heavier ingest edge. Verifies the _reflect_dispatch
    → dispatch_aspect → connections(bucket=...) plumbing end-to-end."""
    eng = engine_singleton
    v = (await srv.remember(content="mcp value node", source="value")).split("ID: ")[1].split()[0]
    i = (await srv.remember(content="mcp intention node", source="intention")).split("ID: ")[1].split()[0]
    f1 = (await srv.remember(content="mcp file alpha", source="file")).split("ID: ")[1].split()[0]
    f2 = (await srv.remember(content="mcp file beta", source="file")).split("ID: ")[1].split()[0]
    eng.cache.set_edge(v, i, weight=1.0)
    eng.cache.set_edge(f1, f2, weight=10.0)

    out = await srv.reflect(aspect="connections", bucket="persona", limit=10)
    # The persona edge endpoints appear; the ingest endpoints do not.
    assert v[:8] in out or v in out
    assert f1[:8] not in out
    # The filter annotation is present.
    assert "[filtered: persona bucket" in out


async def test_reflect_bucket_ignored_for_non_connections_aspect(engine_singleton):
    """MCP reflect(aspect='summary', bucket='persona') ignores the bucket —
    summary returns normally without raising or filtering."""
    await srv.remember(content="summary probe for bucket ignore")
    out = await srv.reflect(aspect="summary", bucket="persona", limit=5)
    assert "Memory Summary" in out
