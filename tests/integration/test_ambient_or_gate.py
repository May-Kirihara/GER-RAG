"""Phase T Stage 5 — ambient BM25 veto → OR gate + staged diagnostics (WP-4).

With ``ambient_gate_or_semantic`` a word-BM25 gate reject no longer vetoes
``ambient_recall`` outright: the passive recall still runs and a semantic
axis (virtual_score ≥ min_score OR raw cosine ≥
``ambient_semantic_raw_min``) can approve the injection. Off-topic
suppression survives — both axes must miss for the empty return. Every
return (empty or not) carries ``gate_diagnostics``; every empty return
carries a discrete ``empty_reason``.

Deterministic StubEmbedder (token-bag, cosine = token overlap). The BM25
gate is forced via ``_bm25_gate_top`` monkeypatch so gate verdicts do not
depend on trigram scores: 5.0 < 32.0 (default threshold) → False,
40.0 → True, None → gate unavailable.
"""
from __future__ import annotations

import numpy as np
import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.core.gravity import compute_virtual_position
from gaottt.index.faiss_index import FaissIndex
from gaottt.services import formatters
from gaottt.services import memory as memory_service
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore
from tests.integration.test_engine_ambient_recall import TokenEmbedder

SENTINEL = "(関連する記憶なし)"
# Gate inputs forced via monkeypatch (threshold stays the 32.0 default).
BM25_REJECT = 5.0
BM25_ACCEPT = 40.0


def _make_engine(tmp_path, **overrides) -> GaOTTTEngine:
    base_kwargs: dict = dict(
        embedding_dim=64,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "test.db"),
        faiss_index_path=str(tmp_path / "test.faiss"),
        genesis_kick_enabled=False,
        dream_enabled=False,
        faiss_save_interval_seconds=0.0,
        flush_interval_seconds=999.0,
        # Comfortable margins for the token-bag embedder: near-exact
        # paraphrases land ~0.8+ on both axes, disjoint vocabulary ~0.
        ambient_min_score=0.5,
    )
    base_kwargs.update(overrides)
    config = GaOTTTConfig(**base_kwargs)
    return GaOTTTEngine(
        config=config,
        embedder=TokenEmbedder(dim=config.embedding_dim),
        faiss_index=FaissIndex(dimension=config.embedding_dim),
        cache=CacheLayer(flush_interval=config.flush_interval_seconds),
        store=SqliteStore(db_path=config.db_path),
        # No ambient gate index: the gate is forced through _bm25_gate_top,
        # and the dormant slot (which needs the real index) stays off.
    )


async def _remember_topic_docs(engine, n: int = 3) -> list[str]:
    ids = []
    for i in range(n):
        r = await memory_service.remember(
            engine,
            content=f"quantum gravity lecture notes relativity draft {i}",
            source="agent",
        )
        ids.append(r.id)
    return ids


async def _start(engine):
    await engine.startup()
    return engine


# ① near-exact query 復帰 ---------------------------------------------------------


@pytest.mark.asyncio
async def test_or_gate_restores_near_exact_query(tmp_path, monkeypatch):
    """BM25 reject + semantically strong corpus → OR flag ON surfaces the
    direct slot; the same reject under legacy (flag OFF) stays empty with
    ``empty_reason="bm25_veto"``."""
    engine = _make_engine(tmp_path / "or", ambient_gate_or_semantic=True)
    await _start(engine)
    try:
        await _remember_topic_docs(engine)
        query = "quantum gravity lecture notes relativity summary"
        monkeypatch.setattr(
            memory_service, "_bm25_gate_top",
            staticmethod(lambda e, q: BM25_REJECT),
        )
        resp = await memory_service.ambient_recall(engine, query, direct_k=2)
        assert resp.count >= 1, "OR gate should let a semantic hit through"
        assert resp.direct, "direct slot should host the near-exact node"
        diag = resp.gate_diagnostics
        assert diag is not None
        assert diag.bm25_gate is False
        assert diag.bm25_top_score == BM25_REJECT
        assert diag.semantic_max_virtual is not None
        assert diag.semantic_max_virtual >= engine.config.ambient_min_score
        assert diag.empty_reason is None
        assert diag.candidates_generated >= resp.count
        assert diag.direct_selected == len(resp.direct)
        assert diag.lensing_selected == len(resp.lensing)
    finally:
        await engine.shutdown()

    legacy = _make_engine(tmp_path / "legacy", ambient_gate_or_semantic=False)
    await _start(legacy)
    try:
        await _remember_topic_docs(legacy)
        monkeypatch.setattr(
            memory_service, "_bm25_gate_top",
            staticmethod(lambda e, q: BM25_REJECT),
        )
        resp = await memory_service.ambient_recall(
            legacy, "quantum gravity lecture notes relativity summary",
            direct_k=2,
        )
        assert resp.count == 0
        assert resp.gate_diagnostics.empty_reason == "bm25_veto"
        # veto fires BEFORE the recall — no candidates were ever generated
        assert resp.gate_diagnostics.candidates_generated == 0
    finally:
        await legacy.shutdown()


# ② off-topic 抑制維持 ------------------------------------------------------------


@pytest.mark.asyncio
async def test_or_gate_suppresses_off_topic(tmp_path, monkeypatch):
    """Unrelated corpus + unrelated query: BM25 reject AND both semantic
    axes below threshold → still empty, with the fused reason."""
    engine = _make_engine(tmp_path, ambient_gate_or_semantic=True)
    await _start(engine)
    try:
        for i in range(3):
            await memory_service.remember(
                engine,
                content=f"gardening tools soil compost pruning {i}",
                source="agent",
            )
        monkeypatch.setattr(
            memory_service, "_bm25_gate_top",
            staticmethod(lambda e, q: BM25_REJECT),
        )
        resp = await memory_service.ambient_recall(
            engine, "quantum gravity lecture notes relativity", direct_k=2,
        )
        assert resp.count == 0
        diag = resp.gate_diagnostics
        assert diag.empty_reason == "bm25_and_semantic_below_threshold"
        # the pool existed — it was the gate that emptied the response
        assert diag.candidates_generated >= 1
        assert diag.semantic_max_virtual is not None
        assert diag.semantic_max_virtual < engine.config.ambient_min_score
        assert formatters.format_ambient(resp, config=engine.config) == SENTINEL
    finally:
        await engine.shutdown()


# ③ empty_reason の離散値 ----------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_reason_no_candidates(tmp_path):
    """Empty corpus (gate index unusable → None) → recall returns nothing →
    "no_candidates" (unified OR path)."""
    engine = _make_engine(tmp_path, ambient_gate_or_semantic=True)
    await _start(engine)
    try:
        resp = await memory_service.ambient_recall(
            engine, "quantum gravity lecture notes", direct_k=2,
        )
        assert resp.count == 0
        diag = resp.gate_diagnostics
        assert diag is not None
        assert diag.empty_reason == "no_candidates"
        assert diag.candidates_generated == 0
        assert diag.bm25_gate is None  # no gate index at all
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_empty_reason_all_tag_excluded(tmp_path, monkeypatch):
    """Every recalled candidate carries an excluded tag → pool empties at
    the tag stage → "all_tag_excluded"."""
    engine = _make_engine(tmp_path)
    await _start(engine)
    try:
        for i in range(2):
            await memory_service.remember(
                engine,
                content=f"quantum gravity lecture notes {i}",
                source="agent", tags=["smoke-test"],
            )
        monkeypatch.setattr(
            memory_service, "_bm25_gate_top",
            staticmethod(lambda e, q: BM25_ACCEPT),
        )
        resp = await memory_service.ambient_recall(
            engine, "quantum gravity lecture notes", direct_k=2,
            exclude_tags=["smoke-test"],
        )
        assert resp.count == 0
        diag = resp.gate_diagnostics
        assert diag.candidates_generated >= 1
        assert diag.after_tag_exclusion == 0
        assert diag.empty_reason == "all_tag_excluded"
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_empty_reason_all_dump_filtered(tmp_path, monkeypatch):
    """Symbol-dominated content survives the recall but dies at the E2 dump
    filter → "all_dump_filtered"."""
    engine = _make_engine(tmp_path)
    await _start(engine)
    try:
        for i in range(2):
            await memory_service.remember(
                engine, content=f"!!!???###$$$%%%&&& {i}", source="agent",
            )
        monkeypatch.setattr(
            memory_service, "_bm25_gate_top",
            staticmethod(lambda e, q: BM25_ACCEPT),
        )
        resp = await memory_service.ambient_recall(
            engine, "!!! ??? ###", direct_k=2,
        )
        assert resp.count == 0
        diag = resp.gate_diagnostics
        assert diag.candidates_generated >= 1
        assert diag.after_tag_exclusion >= 1
        assert diag.after_dump_filter == 0
        assert diag.empty_reason == "all_dump_filtered"
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_empty_reason_bm25_veto_legacy(tmp_path, monkeypatch):
    """Legacy flag OFF: a BM25 reject is an immediate veto with staged
    counts all zero (the recall never ran)."""
    engine = _make_engine(tmp_path, ambient_gate_or_semantic=False)
    await _start(engine)
    try:
        await _remember_topic_docs(engine)
        monkeypatch.setattr(
            memory_service, "_bm25_gate_top",
            staticmethod(lambda e, q: BM25_REJECT),
        )
        resp = await memory_service.ambient_recall(
            engine, "quantum gravity lecture notes", direct_k=2,
        )
        assert resp.count == 0
        diag = resp.gate_diagnostics
        assert diag.empty_reason == "bm25_veto"
        assert diag.bm25_gate is False
        assert diag.candidates_generated == 0
    finally:
        await engine.shutdown()


# ④ sentinel 一字不変 (integration — real empty responses) -------------------------


@pytest.mark.asyncio
async def test_sentinel_immutable_on_real_empty_returns(tmp_path, monkeypatch):
    """Legacy veto and OR below-threshold both format to the exact legacy
    sentinel when expose_breakdown is off; with it on, the sentinel line is
    unchanged and the triage line follows it."""
    engine = _make_engine(tmp_path, ambient_gate_or_semantic=True)
    await _start(engine)
    try:
        for i in range(3):
            await memory_service.remember(
                engine, content=f"gardening tools soil compost {i}",
                source="agent",
            )
        monkeypatch.setattr(
            memory_service, "_bm25_gate_top",
            staticmethod(lambda e, q: BM25_REJECT),
        )
        for expose in (False, True):
            resp = await memory_service.ambient_recall(
                engine, "quantum gravity lecture notes", direct_k=2,
                expose_breakdown=expose,
            )
            assert resp.count == 0
            assert resp.expose_breakdown is expose
            out = formatters.format_ambient(resp, config=engine.config)
            lines = out.splitlines()
            assert lines[0] == SENTINEL
            if expose:
                assert lines[1].startswith("gate: bm25_and_semantic_below_threshold")
            else:
                assert out == SENTINEL
    finally:
        await engine.shutdown()


# ⑤ 診断行は expose 時のみ / gate_diagnostics は常に --------------------------------


@pytest.mark.asyncio
async def test_gate_diagnostics_always_populated(tmp_path, monkeypatch):
    """A SUCCESSFUL injection still carries gate_diagnostics (staged counts
    complete) — and the formatted block only shows the triage line when
    expose_breakdown was requested."""
    engine = _make_engine(tmp_path)
    await _start(engine)
    try:
        await _remember_topic_docs(engine)
        monkeypatch.setattr(
            memory_service, "_bm25_gate_top",
            staticmethod(lambda e, q: BM25_ACCEPT),
        )
        resp = await memory_service.ambient_recall(
            engine, "quantum gravity lecture notes relativity", direct_k=2,
        )
        assert resp.count >= 1
        diag = resp.gate_diagnostics
        assert diag is not None, "diagnostics must ride even non-empty returns"
        assert diag.empty_reason is None
        assert diag.bm25_gate is True
        assert diag.candidates_generated >= resp.count
        assert diag.after_tag_exclusion == diag.candidates_generated
        assert diag.after_dump_filter == diag.after_tag_exclusion
        assert diag.semantic_qualified >= 1
        assert diag.direct_selected == len(resp.direct)
        # expose off → wire format unchanged (no gate line anywhere)
        assert "gate:" not in formatters.format_ambient(resp, config=engine.config)

        exposed = await memory_service.ambient_recall(
            engine, "quantum gravity lecture notes relativity", direct_k=2,
            expose_breakdown=True,
        )
        assert exposed.gate_diagnostics is not None
        out = formatters.format_ambient(exposed, config=engine.config)
        assert "gate: passed (" in out
        lines = out.splitlines()
        assert lines[-1] == "</gaottt-ambient-recall>"
        assert lines[-2].startswith("<!-- ambient-ids ")  # manifest stays last
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_expose_off_response_still_carries_diagnostics(tmp_path, monkeypatch):
    """REST/programmatic callers get gate_diagnostics on the response model
    regardless of the formatter's expose flag."""
    engine = _make_engine(tmp_path, ambient_gate_or_semantic=False)
    await _start(engine)
    try:
        await _remember_topic_docs(engine)
        monkeypatch.setattr(
            memory_service, "_bm25_gate_top",
            staticmethod(lambda e, q: BM25_REJECT),
        )
        # legacy veto path, expose off — diagnostics still populated
        resp = await memory_service.ambient_recall(
            engine, "quantum gravity lecture notes relativity", direct_k=2,
        )
        assert resp.expose_breakdown is False
        assert resp.gate_diagnostics is not None
        assert resp.gate_diagnostics.empty_reason == "bm25_veto"
    finally:
        await engine.shutdown()


# ⑥ passive 維持 ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_or_gate_passive_does_not_perturb_field(tmp_path, monkeypatch):
    """An OR-approved ambient recall is still a passive observation — no
    mass, displacement, or last_access change on any node."""
    engine = _make_engine(tmp_path, ambient_gate_or_semantic=True)
    await _start(engine)
    try:
        ids = await _remember_topic_docs(engine)
        before = {
            nid: (
                engine.cache.get_node(nid).mass,
                engine.get_displacement_norm(nid),
                engine.cache.get_node(nid).last_access,
            )
            for nid in ids
        }
        monkeypatch.setattr(
            memory_service, "_bm25_gate_top",
            staticmethod(lambda e, q: BM25_REJECT),
        )
        resp = await memory_service.ambient_recall(
            engine, "quantum gravity lecture notes relativity summary",
            direct_k=2,
        )
        assert resp.count >= 1, "sanity: the OR gate approved the injection"
        for nid in ids:
            state = engine.cache.get_node(nid)
            b_mass, b_disp, b_access = before[nid]
            assert state.mass == b_mass
            assert engine.get_displacement_norm(nid) == b_disp
            assert state.last_access == b_access
    finally:
        await engine.shutdown()


# ⑧ WP-3 持越し — _enrich_breakdown の lensing_gap 上書き修正 ----------------------


@pytest.mark.asyncio
async def test_engine_lensing_gap_survives_enrich_breakdown(tmp_path):
    """The engine-populated query-path gap (virtual_cos_norm − raw_cos) must
    survive ``_enrich_breakdown`` on the recall→ambient direct path. The
    legacy bug clobbered it to 0.0; a nonzero displacement makes the gap
    measurably nonzero before the recall runs."""
    engine = _make_engine(
        tmp_path,
        expose_score_breakdown=True,
        direct_qualification_enabled=True,
    )
    await _start(engine)
    try:
        ids = await _remember_topic_docs(engine, n=1)
        nid = ids[0]
        # bend the node off its raw position so the gap is materially > 0
        engine.cache.set_displacement(nid, np.full(64, 0.05, dtype=np.float32))

        query = "quantum gravity lecture notes relativity summary"
        resp = await memory_service.ambient_recall(
            engine, query, direct_k=1, expose_breakdown=True,
        )
        direct = next(m for m in resp.direct if m.id == nid)
        bd = direct.breakdown
        assert bd is not None

        # independent recomputation of the normalized virtual cosine
        q_emb = engine.embedder.encode_query(query)[0]
        raw_vec = engine.faiss_index.get_vectors([nid])[nid]
        disp = engine.cache.get_displacement(nid)
        state = engine.cache.get_node(nid)
        virtual = compute_virtual_position(raw_vec, disp, state.temperature)
        vcos_norm = float(np.dot(q_emb, virtual)) / (
            float(np.linalg.norm(q_emb)) * float(np.linalg.norm(virtual))
        )

        assert bd.lensing_gap == pytest.approx(vcos_norm - bd.raw_cosine, abs=1e-6)
        assert abs(bd.lensing_gap) > 1e-3, (
            "gap collapsed to ~0 — _enrich_breakdown clobbered the "
            "engine-populated value"
        )
    finally:
        await engine.shutdown()


# ⑨ WP-D — default 昇格の pin -----------------------------------------------------


@pytest.mark.asyncio
async def test_or_gate_enabled_by_default(tmp_path, monkeypatch):
    """WP-D default promotion (2026-08-25) — a config built WITHOUT the
    flag must carry the OR gate ON: a BM25 reject no longer vetoes when
    the semantic axis is strong. The legacy-veto rollback contract
    (``ambient_gate_or_semantic=False``) is pinned by the explicit-legacy
    tests above."""
    engine = _make_engine(tmp_path)  # flag NOT passed → dataclass default
    assert engine.config.ambient_gate_or_semantic is True
    await _start(engine)
    try:
        await _remember_topic_docs(engine)
        monkeypatch.setattr(
            memory_service, "_bm25_gate_top",
            staticmethod(lambda e, q: BM25_REJECT),
        )
        resp = await memory_service.ambient_recall(
            engine, "quantum gravity lecture notes relativity summary",
            direct_k=2,
        )
        assert resp.count >= 1, (
            "default config must let the semantic axis approve past a BM25 reject"
        )
        assert resp.gate_diagnostics.empty_reason is None
    finally:
        await engine.shutdown()
