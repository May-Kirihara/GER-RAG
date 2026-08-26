"""Phase U §10 R3 follow-up — ambient composite gate 3-arm (integration).

``ambient_gate_mode="composite"`` end-to-end through a real engine
(deterministic TokenEmbedder). Corpus shape is crafted to exercise each
3-arm outcome (measured virtual top-1 values in brackets):

- docs = FILLER8 + COMMON2 + 2 distinctive tokens — every doc shares a
  10-token backbone, so any backbone-only query lands in a *high-but-flat*
  cosine band (the RURI narrow-band pathology in miniature: the penguin
  profile [N_FLAT ≈ 0.889]).
- ``P`` (incident-style positive) = backbone + doc_3's distinctive pair →
  token-identical to doc_3 [≈ 1.0] → clears the semantic-only ``virt_hi``
  arm with BM25 weak.
- ``MID`` (paraphrase-grade positive) = backbone + doc_3's single
  distinctive token [≈ 0.961] — below ``virt_hi`` (0.98) but above
  ``virt_mid`` (0.92) → needs the ``bm25_virt_mid`` conjunction arm.
- ``N_FLAT`` (penguin-profile negative) = backbone + 1 absent token
  [≈ 0.889] → below both semantic thresholds → rejected even though the
  band is high.
- The synthesized reference artifact is built from the *live* engine
  (manifest identity + real corpus digest), so fingerprint validation is
  exercised for real, then broken deliberately for the fail-closed tests.
  The 3-arm decision does not read the distribution values — the artifact
  is the fail-closed/provenance contract — so the fixture distribution is
  inert by design.

BM25 gate inputs are forced via ``_bm25_gate_top`` monkeypatch (arm1
threshold stays the 32.0 default) so verdicts don't depend on trigram
scores — same convention as ``test_ambient_or_gate.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.index.faiss_index import FaissIndex
from gaottt.services import formatters
from gaottt.services import memory as memory_service
from gaottt.services.ambient_composite import (
    build_artifact_payload,
    compute_corpus_digest,
    write_reference_artifact,
)
from gaottt.store.cache import CacheLayer
from gaottt.store.manifest import load_manifest
from gaottt.store.sqlite_store import SqliteStore
from tests.integration.test_engine_ambient_recall import TokenEmbedder

SENTINEL = "(関連する記憶なし)"
BM25_REJECT = 5.0     # < 20.0 fixture bm25_mid → arm3 dead too
BM25_MID = 25.0       # ≥ 20.0 (arm3) but < 32.0 (arm1) → mid band
BM25_ACCEPT = 40.0    # ≥ 32.0 → arm1 (bm25_strong)

FILLER = " ".join(f"filler{i}" for i in range(8))   # shared backbone (8)
COMMON = "comA comB"                                # shared backbone (2)
N_DOCS = 12


def _doc(i: int) -> str:
    return f"{FILLER} {COMMON} omega{i} uniq{i}"


# incident-style positive: backbone + doc_3's distinctive pair (virt ≈ 1.0)
P_QUERY = f"{FILLER} {COMMON} omega3 uniq3"
# paraphrase-grade positive: backbone + doc_3's single distinctive token
# (virt ≈ 0.961 — below virt_hi, above virt_mid)
MID_QUERY = f"{FILLER} {COMMON} omega3"
# penguin-profile negative: backbone + one absent token → flat high band
# (virt ≈ 0.889 — below both semantic thresholds)
N_FLAT_QUERY = f"{FILLER} {COMMON} absentQ"
# weak-band negative (shares only part of the backbone)
N_LOW_QUERY = " ".join(f"filler{i}" for i in range(4)) + " zeta9"

# Fixture thresholds — deterministic separation of the measured bands
# (1.0000 / 0.9607 / 0.8886): virt_hi sits between P and MID, virt_mid
# between MID and N_FLAT, bm25_mid inside the forced-BM25 mid band.
VIRT_HI = 0.98
BM25_MID = 20.0
VIRT_MID = 0.92

# Reference distribution shaped like the calibration population. The 3-arm
# decision never reads it (artifact = fail-closed/provenance contract), so
# these values are inert; they ride the artifact for recalibration
# provenance exactly as production artifacts will.
REFERENCE_DISTRIBUTION = [0.83, 0.85, 0.86, 0.87, 0.87, 0.88, 0.88, 0.89]


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
        ambient_gate_mode="composite",
        # 3-arm thresholds — fixture-tuned to the measured virtual bands
        # (see VIRT_HI / BM25_MID / VIRT_MID above).
        ambient_composite_virt_hi=VIRT_HI,
        ambient_composite_bm25_mid=BM25_MID,
        ambient_composite_virt_mid=VIRT_MID,
    )
    base_kwargs.update(overrides)
    config = GaOTTTConfig(**base_kwargs)
    return GaOTTTEngine(
        config=config,
        embedder=TokenEmbedder(dim=config.embedding_dim),
        faiss_index=FaissIndex(dimension=config.embedding_dim),
        cache=CacheLayer(flush_interval=config.flush_interval_seconds),
        store=SqliteStore(db_path=config.db_path),
        # No ambient gate index: BM25 verdicts are forced via _bm25_gate_top.
    )


async def _seed_docs(engine: GaOTTTEngine, n: int = N_DOCS) -> None:
    for i in range(n):
        await memory_service.remember(engine, content=_doc(i), source="agent")


async def _write_valid_artifact(engine: GaOTTTEngine) -> None:
    """Build the reference artifact from the LIVE engine state — manifest
    identity, real corpus digest, real active count — so fingerprint
    validation passes for real (fail-closed tests then corrupt it).
    ``active_count`` follows the runtime drift-guard convention
    (``len(cache.node_cache)``, see ``_composite_reference_for``)."""
    manifest = load_manifest(Path(engine.config.data_dir))
    contents = await engine.store.get_all_contents()
    digest, _ = compute_corpus_digest(contents, engine.cache.node_cache.keys())
    payload = build_artifact_payload(
        embedder_id=manifest.embedder_id,
        embedder_version=manifest.embedder_version,
        corpus_digest=digest,
        active_count=len(engine.cache.node_cache),
        virt_top1_distribution=list(REFERENCE_DISTRIBUTION),
        thresholds={
            "ambient_bm25_min_score": engine.config.ambient_bm25_min_score,
            "ambient_composite_virt_hi": engine.config.ambient_composite_virt_hi,
            "ambient_composite_bm25_mid": engine.config.ambient_composite_bm25_mid,
            "ambient_composite_virt_mid": engine.config.ambient_composite_virt_mid,
        },
        provenance={"script": "tests/integration/test_ambient_composite_gate.py"},
    )
    out = Path(engine.config.data_dir) / engine.config.ambient_composite_reference_filename
    write_reference_artifact(out, payload)


def _corrupt_artifact(engine: GaOTTTEngine, mutate) -> None:
    """Load the artifact JSON, apply ``mutate(data)``, write it back."""
    out = Path(engine.config.data_dir) / engine.config.ambient_composite_reference_filename
    data = json.loads(out.read_text(encoding="utf-8"))
    mutate(data)
    out.write_text(json.dumps(data), encoding="utf-8")


async def _start_seeded(tmp_path, **overrides):
    engine = _make_engine(tmp_path, **overrides)
    await engine.startup()
    await _seed_docs(engine)
    await _write_valid_artifact(engine)
    return engine


def _force_bm25(monkeypatch, value: float | None):
    monkeypatch.setattr(
        memory_service, "_bm25_gate_top",
        staticmethod(lambda e, q: value),
    )


# ① arm 2 (virt_hi) — incident-style positive with BM25 weak ---------------------


@pytest.mark.asyncio
async def test_composite_virt_hi_accepts_incident_style(tmp_path, monkeypatch):
    engine = await _start_seeded(tmp_path)
    try:
        _force_bm25(monkeypatch, BM25_REJECT)
        resp = await memory_service.ambient_recall(engine, P_QUERY, direct_k=2)
        assert resp.count >= 1, "virt_hi arm should accept the incident-style query"
        diag = resp.gate_diagnostics
        assert diag is not None
        assert diag.bm25_gate is False
        assert diag.composite_signal == "virt_hi"
        assert diag.empty_reason is None
        assert diag.virt_top1 is not None and diag.virt_top1 >= VIRT_HI
        assert diag.bm25_top == BM25_REJECT
        # P is token-identical to doc_3 → the virtual axis top-1 sits at 1.0,
        # well clear of the flat backbone band (~0.889)
        assert diag.semantic_max_virtual >= 0.99
    finally:
        await engine.shutdown()


# ①b arm 3 (bm25_virt_mid) — paraphrase-grade positive ---------------------------


@pytest.mark.asyncio
async def test_composite_bm25_virt_mid_accepts_paraphrase_style(tmp_path, monkeypatch):
    """MID (virt ≈ 0.961) is below virt_hi (0.98) — only the conjunction
    arm (bm25 ≥ 20 ∧ virt ≥ 0.92) can accept it, with BM25 in the mid
    band (25 < 32 strong threshold)."""
    engine = await _start_seeded(tmp_path)
    try:
        _force_bm25(monkeypatch, BM25_MID)
        resp = await memory_service.ambient_recall(engine, MID_QUERY, direct_k=2)
        assert resp.count >= 1, "bm25_virt_mid arm should accept the paraphrase query"
        diag = resp.gate_diagnostics
        assert diag.composite_signal == "bm25_virt_mid"
        assert diag.empty_reason is None
        assert diag.virt_top1 is not None and VIRT_MID <= diag.virt_top1 < VIRT_HI
        assert diag.bm25_top == BM25_MID
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_composite_arm3_requires_bm25_mid(tmp_path, monkeypatch):
    """Arm-3 conjunction boundary: same MID query, BM25 dropped below
    bm25_mid (5 < 20) → arm3 cannot fire and virt (0.961) is below
    virt_hi → reject."""
    engine = await _start_seeded(tmp_path)
    try:
        _force_bm25(monkeypatch, BM25_REJECT)
        resp = await memory_service.ambient_recall(engine, MID_QUERY, direct_k=2)
        assert resp.count == 0
        diag = resp.gate_diagnostics
        assert diag.empty_reason == "composite_reject"
        assert diag.composite_signal == "composite_reject"
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_composite_arm3_requires_virt_mid(tmp_path, monkeypatch):
    """Arm-3 conjunction boundary (other side): mid-band BM25 (25 ≥ 20)
    with a virt below virt_mid (N_FLAT ≈ 0.889 < 0.92) → still reject —
    a decent BM25 score alone must not accept off-topic."""
    engine = await _start_seeded(tmp_path)
    try:
        _force_bm25(monkeypatch, BM25_MID)
        resp = await memory_service.ambient_recall(engine, N_FLAT_QUERY, direct_k=2)
        assert resp.count == 0
        diag = resp.gate_diagnostics
        assert diag.empty_reason == "composite_reject"
        assert diag.composite_signal == "composite_reject"
        assert diag.virt_top1 is not None and diag.virt_top1 < VIRT_MID
    finally:
        await engine.shutdown()


# ② off-topic rejection ----------------------------------------------------------


@pytest.mark.asyncio
async def test_composite_rejects_flat_band_off_topic(tmp_path, monkeypatch):
    engine = await _start_seeded(tmp_path)
    try:
        _force_bm25(monkeypatch, BM25_REJECT)
        resp = await memory_service.ambient_recall(engine, N_FLAT_QUERY, direct_k=2)
        assert resp.count == 0
        diag = resp.gate_diagnostics
        assert diag.empty_reason == "composite_reject"
        assert diag.composite_signal == "composite_reject"
        # the virtual axis sits in the *high* band (penguin profile) —
        # the absolute virt thresholds are what reject it
        assert diag.virt_top1 is not None and diag.virt_top1 >= 0.88
        assert diag.virt_top1 < VIRT_HI
        assert formatters.format_ambient(resp, config=engine.config) == SENTINEL
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_composite_rejects_weak_band_off_topic(tmp_path, monkeypatch):
    """Partial-backbone negative: below every semantic threshold —
    rejects through the same composite_reject reason."""
    engine = await _start_seeded(tmp_path)
    try:
        _force_bm25(monkeypatch, BM25_REJECT)
        resp = await memory_service.ambient_recall(engine, N_LOW_QUERY, direct_k=2)
        assert resp.count == 0
        diag = resp.gate_diagnostics
        assert diag.empty_reason == "composite_reject"
        assert diag.composite_signal == "composite_reject"
    finally:
        await engine.shutdown()


# ③ arm 1 (bm25_strong) ---------------------------------------------------------


@pytest.mark.asyncio
async def test_composite_bm25_strong_accepts_without_artifact(tmp_path, monkeypatch):
    """No artifact at all (fail-closed corpus) — BM25 strong still accepts:
    fail-closed means 'BM25-only', not 'silent'."""
    engine = _make_engine(tmp_path)
    await engine.startup()
    try:
        await _seed_docs(engine)
        _force_bm25(monkeypatch, BM25_ACCEPT)
        resp = await memory_service.ambient_recall(engine, P_QUERY, direct_k=2)
        assert resp.count >= 1
        assert resp.gate_diagnostics.composite_signal == "bm25_strong"
        assert resp.gate_diagnostics.empty_reason is None
    finally:
        await engine.shutdown()


# ④ fail-closed degradation ------------------------------------------------------


@pytest.mark.asyncio
async def test_composite_fail_closed_missing_artifact(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path)
    await engine.startup()
    try:
        await _seed_docs(engine)
        _force_bm25(monkeypatch, BM25_REJECT)
        resp = await memory_service.ambient_recall(engine, P_QUERY, direct_k=2)
        assert resp.count == 0
        diag = resp.gate_diagnostics
        assert diag.empty_reason == "composite_reference_unavailable"
        assert diag.composite_signal == "composite_reference_unavailable"
        # the decision axes still surface for triage — the reference only
        # gates whether the semantic arms may fire
        assert diag.virt_top1 is not None and diag.virt_top1 >= VIRT_HI
        assert diag.bm25_top == BM25_REJECT
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_composite_fail_closed_digest_mismatch(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path)
    await engine.startup()
    try:
        await _seed_docs(engine)
        await _write_valid_artifact(engine)
        _corrupt_artifact(
            engine, lambda d: d["fingerprint"].__setitem__("corpus_digest", "f" * 64),
        )
        _force_bm25(monkeypatch, BM25_REJECT)
        resp = await memory_service.ambient_recall(engine, P_QUERY, direct_k=2)
        assert resp.count == 0
        assert resp.gate_diagnostics.empty_reason == "composite_reference_unavailable"
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_composite_fail_closed_count_drift(tmp_path, monkeypatch):
    """Artifact validates (first call accepted), then the corpus drifts past
    ``ambient_composite_count_drift_max`` (12 → 14 = 16.7% > 5%) → stale →
    fail-closed on the next call."""
    engine = await _start_seeded(tmp_path)
    try:
        _force_bm25(monkeypatch, BM25_REJECT)
        ok = await memory_service.ambient_recall(engine, P_QUERY, direct_k=2)
        assert ok.count >= 1, "sanity: artifact valid on first use"

        for i in range(N_DOCS, N_DOCS + 2):
            await memory_service.remember(engine, content=_doc(i), source="agent")

        resp = await memory_service.ambient_recall(engine, P_QUERY, direct_k=2)
        assert resp.count == 0
        assert resp.gate_diagnostics.empty_reason == "composite_reference_unavailable"
        assert resp.gate_diagnostics.composite_signal == "composite_reference_unavailable"
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_composite_fail_closed_embedder_identity_mismatch(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path)
    await engine.startup()
    try:
        await _seed_docs(engine)
        await _write_valid_artifact(engine)
        _corrupt_artifact(
            engine,
            lambda d: d["fingerprint"].__setitem__("embedder_id", "someone-elses-embedder"),
        )
        _force_bm25(monkeypatch, BM25_REJECT)
        resp = await memory_service.ambient_recall(engine, P_QUERY, direct_k=2)
        assert resp.count == 0
        assert resp.gate_diagnostics.empty_reason == "composite_reference_unavailable"
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_composite_fail_closed_v2_style_artifact_rejected(tmp_path, monkeypatch):
    """An artifact echoing the dropped v2 threshold knobs (percentile /
    margin / raw_floor) fails the 3-arm echo validation → fail-closed.
    Old-calibration artifacts must demand re-calibration, not silently
    ride along."""
    engine = _make_engine(tmp_path)
    await engine.startup()
    try:
        await _seed_docs(engine)
        await _write_valid_artifact(engine)
        _corrupt_artifact(
            engine,
            lambda d: d.__setitem__(
                "thresholds",
                {
                    "ambient_bm25_min_score": 32.0,
                    "ambient_semantic_percentile_min": 85.0,
                    "ambient_margin_min": 0.02,
                    "ambient_raw_floor_composite": 0.80,
                },
            ),
        )
        _force_bm25(monkeypatch, BM25_REJECT)
        resp = await memory_service.ambient_recall(engine, P_QUERY, direct_k=2)
        assert resp.count == 0
        assert resp.gate_diagnostics.empty_reason == "composite_reference_unavailable"
    finally:
        await engine.shutdown()


# ⑤ breakdown 非依存 -------------------------------------------------------------


@pytest.mark.asyncio
async def test_composite_breakdown_independence(tmp_path, monkeypatch):
    """``expose_score_breakdown`` must not change the composite decision or
    its axes — the 3-arm reads virt_top1 from ``item.raw_score`` (always
    populated) and bm25_top from the gate index, never from per-item
    breakdowns (Phase T 'raw axis missing' regression fence, now true by
    construction — the old own-FAISS raw axis was removed with the
    raw-floor arm)."""
    engine = await _start_seeded(tmp_path)
    try:
        _force_bm25(monkeypatch, BM25_REJECT)
        for query, expected_count in ((P_QUERY, ">=1"), (N_FLAT_QUERY, "==0")):
            off = await memory_service.ambient_recall(
                engine, query, direct_k=2, expose_breakdown=False,
            )
            on = await memory_service.ambient_recall(
                engine, query, direct_k=2, expose_breakdown=True,
            )
            d_off, d_on = off.gate_diagnostics, on.gate_diagnostics
            assert off.count == on.count
            if expected_count == ">=1":
                assert off.count >= 1
            else:
                assert off.count == 0
            assert d_on.composite_signal == d_off.composite_signal
            assert d_on.virt_top1 == d_off.virt_top1
            assert d_on.bm25_top == d_off.bm25_top
            # breakdowns truly absent in the off-run, present in the on-run
            if off.direct:
                assert off.direct[0].breakdown is None
                assert on.direct[0].breakdown is not None
    finally:
        await engine.shutdown()


# ⑥ persona/lensing は direct reject を反転させない --------------------------------


@pytest.mark.asyncio
async def test_composite_persona_cannot_flip_rejection(tmp_path, monkeypatch):
    """A declared persona exists (ambient persona slot enabled), but the
    gate decision precedes slot composition — a rejected query returns
    empty with no persona slot at all.

    The persona is declared BEFORE the artifact is written: declaring a
    value adds a corpus node, which would otherwise trip the count-drift
    staleness guard (itself correct behavior — see
    ``test_composite_fail_closed_count_drift``)."""
    from gaottt.services import phase_d
    engine = _make_engine(tmp_path)
    await engine.startup()
    try:
        await _seed_docs(engine)
        await phase_d.declare_value(engine, "always ground answers in evidence")
        assert engine.config.ambient_persona_enabled  # fixture sanity
        # artifact fingerprint covers the corpus INCLUDING the persona node
        await _write_valid_artifact(engine)

        _force_bm25(monkeypatch, BM25_REJECT)
        resp = await memory_service.ambient_recall(engine, N_FLAT_QUERY, direct_k=2)
        assert resp.count == 0
        assert resp.persona is None, "persona slot must not compose past a gate reject"
        assert resp.direct == [] and resp.lensing == []
        assert resp.gate_diagnostics.empty_reason == "composite_reject"
    finally:
        await engine.shutdown()


# ⑦ pool < 2 → pool 統計を信頼しない ----------------------------------------------


@pytest.mark.asyncio
async def test_composite_pool_too_small(tmp_path, monkeypatch):
    """Tag-exclusion whittles the pool to a single item → the semantic
    arms are not trusted → ``composite_pool_too_small`` (BM25 weak)."""
    engine = _make_engine(tmp_path)
    await engine.startup()
    try:
        for i in range(4):
            await memory_service.remember(
                engine, content=_doc(i), source="agent",
                tags=["whittle"] if i > 0 else None,
            )
        await _write_valid_artifact(engine)
        _force_bm25(monkeypatch, BM25_REJECT)
        resp = await memory_service.ambient_recall(
            engine, P_QUERY, direct_k=2, exclude_tags=["whittle"],
        )
        assert resp.count == 0
        diag = resp.gate_diagnostics
        assert diag.after_tag_exclusion == 1
        assert diag.empty_reason == "composite_pool_too_small"
        assert diag.composite_signal == "composite_pool_too_small"
    finally:
        await engine.shutdown()


# ⑧ formatter gate line — additive composite segments ----------------------------


@pytest.mark.asyncio
async def test_composite_gate_line_segments(tmp_path, monkeypatch):
    engine = await _start_seeded(tmp_path)
    try:
        _force_bm25(monkeypatch, BM25_REJECT)
        resp = await memory_service.ambient_recall(
            engine, P_QUERY, direct_k=2, expose_breakdown=True,
        )
        assert resp.count >= 1
        out = formatters.format_ambient(resp, config=engine.config)
        gate_line = next(ln for ln in out.splitlines() if ln.startswith("gate:"))
        assert "gate: passed (" in gate_line
        assert "sig=virt_hi" in gate_line
        assert "virt=" in gate_line
        assert "bm25=" in gate_line
        # dropped v1 segments must not reappear
        assert "pct=" not in gate_line
        assert "margin=" not in gate_line
        assert "raw=" not in gate_line
        # existing segments survive
        assert "candidates=" in gate_line
        assert "virt_max=" in gate_line

        # reject carries the reason in the line too
        rej = await memory_service.ambient_recall(
            engine, N_FLAT_QUERY, direct_k=2, expose_breakdown=True,
        )
        rej_line = next(
            ln for ln in formatters.format_ambient(
                rej, config=engine.config,
            ).splitlines() if ln.startswith("gate:")
        )
        assert rej_line.startswith("gate: composite_reject")
        assert "sig=composite_reject" in rej_line
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_or_mode_line_has_no_composite_segments(tmp_path, monkeypatch):
    """mode="or": composite fields stay None and the gate line carries no
    composite segment — the legacy wire format is bit-for-bit."""
    engine = await _start_seeded(tmp_path, ambient_gate_mode="or")
    try:
        _force_bm25(monkeypatch, BM25_REJECT)
        resp = await memory_service.ambient_recall(
            engine, P_QUERY, direct_k=2, expose_breakdown=True,
        )
        assert resp.count >= 1  # OR-gate semantics accept via virtual axis
        diag = resp.gate_diagnostics
        assert diag.composite_signal is None
        assert diag.virt_top1 is None
        assert diag.bm25_top is None
        out = formatters.format_ambient(resp, config=engine.config)
        assert "sig=" not in out
        assert "pct=" not in out
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_or_mode_default_value_is_rollback():
    """WP acceptance: the shipped default is "or" (promotion to composite
    is a PM decision gated on the v3 pre-registered calibration criteria).
    The 3-arm provisional defaults echo the v2 exploratory analysis."""
    cfg = GaOTTTConfig(embedding_dim=32)
    assert cfg.ambient_gate_mode == "or"
    assert cfg.ambient_composite_virt_hi == 0.85
    assert cfg.ambient_composite_bm25_mid == 22.0
    assert cfg.ambient_composite_virt_mid == 0.845
    assert cfg.ambient_composite_reference_filename == (
        "ambient_composite_reference.json"
    )
    assert cfg.ambient_composite_count_drift_max == 0.05


@pytest.mark.asyncio
async def test_composite_ignores_or_semantic_flag(tmp_path, monkeypatch):
    """mode="composite" takes precedence over ``ambient_gate_or_semantic``:
    the legacy early BM25 veto must not fire (the composite judgment needs
    pool stats), and a weak-BM25 strong-semantic query is accepted."""
    engine = await _start_seeded(tmp_path, ambient_gate_or_semantic=False)
    try:
        _force_bm25(monkeypatch, BM25_REJECT)
        resp = await memory_service.ambient_recall(engine, P_QUERY, direct_k=2)
        assert resp.count >= 1, (
            "legacy veto must not fire in composite mode even with "
            "ambient_gate_or_semantic=False"
        )
        assert resp.gate_diagnostics.composite_signal == "virt_hi"
    finally:
        await engine.shutdown()


# ⑨ passive 維持 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_composite_passive_does_not_perturb_field(tmp_path, monkeypatch):
    engine = await _start_seeded(tmp_path)
    try:
        ids = [
            nid for nid in engine.cache.node_cache
        ]
        before = {
            nid: (
                engine.cache.get_node(nid).mass,
                engine.get_displacement_norm(nid),
                engine.cache.get_node(nid).last_access,
            )
            for nid in ids
        }
        _force_bm25(monkeypatch, BM25_REJECT)
        ok = await memory_service.ambient_recall(engine, P_QUERY, direct_k=2)
        rej = await memory_service.ambient_recall(engine, N_FLAT_QUERY, direct_k=2)
        assert ok.count >= 1 and rej.count == 0
        for nid in ids:
            state = engine.cache.get_node(nid)
            b_mass, b_disp, b_access = before[nid]
            assert state.mass == b_mass
            assert engine.get_displacement_norm(nid) == b_disp
            assert state.last_access == b_access
    finally:
        await engine.shutdown()
