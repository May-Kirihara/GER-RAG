"""Phase U §10 R3 follow-up — 3-arm composite-gate primitives (unit).

``gaottt.services.ambient_composite`` is deliberately engine-free: the
3-arm decision function, the corpus digest, and the reference-artifact
(load/build) round-trip are all pure so they can be unit-tested without
an engine. The runtime glue (artifact validation against the live
engine) lives in ``services.memory`` and is covered by
``tests/integration/test_ambient_composite_gate.py``.

3-arm contract (Plans-Phase-U-Review-Hardening.md §10)::

    accept = bm25_strong (>= ambient_bm25_min_score)
          OR (virt_top1 >= ambient_composite_virt_hi)
          OR (bm25_top >= ambient_composite_bm25_mid
              AND virt_top1 >= ambient_composite_virt_mid)

The reference distribution no longer feeds the decision (percentile /
margin axes were dropped with the v2 evidence — the narrow band made
them inseparable); the artifact still fail-closes the two semantic arms
via ``reference_available``.
"""
from __future__ import annotations

import json

import pytest

from gaottt.services.ambient_composite import (
    COMPOSITE_ARTIFACT_FORMAT,
    COMPOSITE_ARTIFACT_SCHEMA_VERSION,
    CompositeGateThresholds,
    CompositeReferenceError,
    CompositeVerdict,
    build_artifact_payload,
    composite_gate_decision,
    compute_corpus_digest,
    load_composite_reference,
)


# --- composite_gate_decision (3-arm) ---------------------------------------------


def _thresholds(**kw) -> CompositeGateThresholds:
    base = dict(virt_hi=0.85, bm25_mid=22.0, virt_mid=0.845)
    base.update(kw)
    return CompositeGateThresholds(**base)


def _decision(**kw) -> CompositeVerdict:
    base = dict(
        bm25_top=5.0,                 # weak (threshold 32.0 above)
        bm25_threshold=32.0,
        virt_top1=0.90,
        reference_available=True,
        thresholds=_thresholds(),
        pool_size=10,
    )
    base.update(kw)
    return composite_gate_decision(**base)


# arm 1 — bm25_strong -----------------------------------------------------------


def test_bm25_strong_accepts_even_without_reference():
    v = _decision(bm25_top=40.0, reference_available=False)
    assert v.accepted is True
    assert v.signal == "bm25_strong"
    assert v.reason is None


def test_bm25_strong_at_threshold_accepts():
    # >= semantics: bm25_top exactly at ambient_bm25_min_score passes.
    v = _decision(bm25_top=32.0)
    assert v.accepted is True
    assert v.signal == "bm25_strong"


def test_pool_too_small_does_not_override_bm25_strong():
    v = _decision(bm25_top=40.0, pool_size=1)
    assert v.accepted is True
    assert v.signal == "bm25_strong"


# arm 2 — virt_hi ---------------------------------------------------------------


def test_virt_hi_arm_accepts_with_weak_bm25():
    # virt_top1=0.90 >= 0.85 clears the semantic-only arm; bm25 stays weak.
    v = _decision()
    assert v.accepted is True
    assert v.signal == "virt_hi"
    assert v.reason is None
    assert v.virt_top1 == pytest.approx(0.90)
    assert v.bm25_top == pytest.approx(5.0)


def test_virt_hi_boundary_accepts():
    # >= semantics: virt_top1 exactly at virt_hi passes.
    v = _decision(virt_top1=0.85)
    assert v.accepted is True
    assert v.signal == "virt_hi"


def test_virt_hi_just_below_rejects_when_arm3_dead():
    # 0.8499 < virt_hi AND bm25 5.0 < bm25_mid → both semantic arms miss.
    v = _decision(virt_top1=0.8499)
    assert v.accepted is False
    assert v.signal == "composite_reject"
    assert v.reason == "composite_reject"


def test_bm25_unusable_still_allows_virt_hi():
    # bm25_top None (gate index absent) → arm1/arm3 cannot fire, but the
    # semantic-only arm decides on its own — not a reject.
    v = _decision(bm25_top=None)
    assert v.accepted is True
    assert v.signal == "virt_hi"


# arm 3 — bm25_virt_mid ---------------------------------------------------------


def test_bm25_virt_mid_arm_accepts():
    # virt below virt_hi but at/above virt_mid, bm25 mid-strong but below
    # the bm25_strong threshold → the conjunction arm fires.
    v = _decision(bm25_top=25.0, virt_top1=0.846)
    assert v.accepted is True
    assert v.signal == "bm25_virt_mid"
    assert v.reason is None


def test_bm25_virt_mid_boundaries_accept():
    # >= semantics on BOTH axes of the conjunction.
    v = _decision(bm25_top=22.0, virt_top1=0.845)
    assert v.accepted is True
    assert v.signal == "bm25_virt_mid"


def test_bm25_virt_mid_requires_both_axes():
    # bm25 just below bm25_mid → conjunction fails even with virt mid-band.
    v = _decision(bm25_top=21.9, virt_top1=0.846)
    assert v.accepted is False
    assert v.reason == "composite_reject"
    # virt just below virt_mid → conjunction fails even with bm25 mid-strong.
    v = _decision(bm25_top=25.0, virt_top1=0.8449)
    assert v.accepted is False
    assert v.reason == "composite_reject"


def test_virt_hi_takes_precedence_over_mid_arm():
    # both semantic arms fire → the stronger semantic-only arm is reported.
    v = _decision(bm25_top=25.0, virt_top1=0.90)
    assert v.accepted is True
    assert v.signal == "virt_hi"


def test_bm25_missing_blocks_arm3_but_not_reject_overall():
    # bm25 None + virt below virt_hi → arm3 cannot verify bm25_mid → reject
    # (a missing axis never clears a threshold).
    v = _decision(bm25_top=None, virt_top1=0.846)
    assert v.accepted is False
    assert v.reason == "composite_reject"


# reject reasons (edge-case contract) --------------------------------------------


def test_reference_unavailable_rejects_semantic_arms():
    # fail-closed: no usable reference artifact → BM25 is the only accept
    # path, even for a virt_top1 that would clear virt_hi.
    v = _decision(reference_available=False, virt_top1=0.95)
    assert v.accepted is False
    assert v.signal == "composite_reference_unavailable"
    assert v.reason == "composite_reference_unavailable"


def test_pool_too_small_rejects():
    v = _decision(pool_size=1)
    assert v.accepted is False
    assert v.signal == "composite_pool_too_small"
    assert v.reason == "composite_pool_too_small"


def test_missing_virt_axis_rejects():
    v = _decision(virt_top1=None)
    assert v.accepted is False
    assert v.signal == "composite_reject"
    assert v.reason == "composite_reject"
    assert "virtual" in (v.detail or "")


def test_nonfinite_inputs_reject():
    v = _decision(virt_top1=float("nan"))
    assert v.accepted is False and v.reason == "composite_reject"
    v = _decision(bm25_top=float("inf"))
    assert v.accepted is False and v.reason == "composite_reject"
    v = _decision(thresholds=_thresholds(virt_hi=float("nan")))
    assert v.accepted is False and v.reason == "composite_reject"
    v = _decision(thresholds=_thresholds(bm25_mid=float("-inf")))
    assert v.accepted is False and v.reason == "composite_reject"
    v = _decision(thresholds=_thresholds(virt_mid=float("nan")))
    assert v.accepted is False and v.reason == "composite_reject"
    v = _decision(bm25_threshold=float("nan"))
    assert v.accepted is False and v.reason == "composite_reject"


def test_diagnostics_axes_populated_on_reject():
    # virt/bm25 echoes still surface for triage on a semantic reject.
    v = _decision(virt_top1=0.8499)
    assert v.virt_top1 == pytest.approx(0.8499)
    assert v.bm25_top == pytest.approx(5.0)


def test_verdict_is_immutable():
    v = _decision()
    with pytest.raises(Exception):
        v.accepted = False  # type: ignore[misc]


# --- compute_corpus_digest -------------------------------------------------------


def test_corpus_digest_deterministic_and_order_independent():
    contents = {"b": "beta", "a": "alpha", "c": "gamma"}
    d1, n1 = compute_corpus_digest(contents, ["a", "b", "c"])
    d2, n2 = compute_corpus_digest(
        {"c": "gamma", "a": "alpha", "b": "beta"}, ["c", "b", "a"],
    )
    assert d1 == d2
    assert n1 == n2 == 3


def test_corpus_digest_changes_on_content_and_membership():
    base = {"a": "alpha", "b": "beta"}
    d0, _ = compute_corpus_digest(base, ["a", "b"])
    d1, _ = compute_corpus_digest({"a": "alpha!", "b": "beta"}, ["a", "b"])
    d2, n2 = compute_corpus_digest(base, ["a"])  # b no longer active
    assert d0 != d1
    assert d0 != d2
    assert n2 == 1


def test_corpus_digest_skips_ids_missing_content():
    contents = {"a": "alpha", "b": ""}
    d, n = compute_corpus_digest(contents, ["a", "b"])
    assert n == 1  # empty-content doc does not count


# --- reference artifact load / build round-trip ----------------------------------


def _payload(**kw) -> dict:
    base = dict(
        embedder_id="stub-embedder",
        embedder_version="v1",
        corpus_digest="0" * 64,
        active_count=20,
        virt_top1_distribution=[0.70, 0.72, 0.75, 0.78, 0.81],
        thresholds={
            "ambient_bm25_min_score": 32.0,
            "ambient_composite_virt_hi": 0.85,
            "ambient_composite_bm25_mid": 22.0,
            "ambient_composite_virt_mid": 0.845,
        },
        provenance={"script": "tests"},
    )
    base.update(kw)
    return build_artifact_payload(**base)


def test_artifact_build_load_roundtrip(tmp_path):
    path = tmp_path / "ambient_composite_reference.json"
    path.write_text(json.dumps(_payload()), encoding="utf-8")
    ref = load_composite_reference(path)
    assert ref.embedder_id == "stub-embedder"
    assert ref.embedder_version == "v1"
    assert ref.corpus_digest == "0" * 64
    assert ref.active_count == 20
    assert ref.virt_top1_distribution == [0.70, 0.72, 0.75, 0.78, 0.81]
    assert ref.thresholds_echo["ambient_composite_virt_hi"] == 0.85
    assert ref.thresholds_echo["ambient_composite_virt_mid"] == 0.845
    assert ref.schema_version == COMPOSITE_ARTIFACT_SCHEMA_VERSION


def test_artifact_missing_3arm_threshold_echo_raises(tmp_path):
    """thresholds echo は 3-arm key 一式を要求 — v2 旧 schema (percentile /
    margin / raw_floor echo) の artifact は load 時点で fail-closed。"""
    payload = _payload()
    del payload["thresholds"]["ambient_composite_virt_mid"]
    path = tmp_path / "old_echo.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CompositeReferenceError):
        load_composite_reference(path)


def test_artifact_missing_file_raises(tmp_path):
    with pytest.raises(CompositeReferenceError):
        load_composite_reference(tmp_path / "absent.json")


def test_artifact_corrupt_json_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CompositeReferenceError):
        load_composite_reference(path)


def test_artifact_wrong_schema_version_raises(tmp_path):
    payload = _payload()
    payload["schema_version"] = 999
    path = tmp_path / "bad_ver.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CompositeReferenceError):
        load_composite_reference(path)


def test_artifact_wrong_format_raises(tmp_path):
    payload = _payload()
    payload["format"] = "something-else"
    path = tmp_path / "bad_fmt.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CompositeReferenceError):
        load_composite_reference(path)


def test_artifact_missing_fingerprint_raises(tmp_path):
    payload = _payload()
    del payload["fingerprint"]["corpus_digest"]
    path = tmp_path / "no_digest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CompositeReferenceError):
        load_composite_reference(path)


def test_artifact_nonfinite_distribution_raises(tmp_path):
    payload = _payload()
    payload["reference_distribution"]["virt_top1"] = [0.7, float("nan")]
    path = tmp_path / "nan_dist.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CompositeReferenceError):
        load_composite_reference(path)


def test_artifact_empty_distribution_raises(tmp_path):
    payload = _payload()
    payload["reference_distribution"]["virt_top1"] = []
    path = tmp_path / "empty_dist.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CompositeReferenceError):
        load_composite_reference(path)


def test_artifact_nonpositive_active_count_raises(tmp_path):
    payload = _payload()
    payload["fingerprint"]["active_count"] = 0
    path = tmp_path / "zero_count.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CompositeReferenceError):
        load_composite_reference(path)


def test_artifact_format_constant():
    # The format tag is part of the schema contract — a rename would orphan
    # every production artifact, so pin it.
    assert COMPOSITE_ARTIFACT_FORMAT == "gaottt-ambient-composite-reference"
