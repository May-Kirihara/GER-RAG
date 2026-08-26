"""Phase U WP-3 — pure composite-gate primitives (unit).

``gaottt.services.ambient_composite`` is deliberately engine-free: the
decision function, the midrank percentile, the corpus digest, and the
reference-artifact (load/build) round-trip are all pure so they can be
unit-tested without an engine. The runtime glue (raw FAISS axis, artifact
validation against the live engine) lives in ``services.memory`` and is
covered by ``tests/integration/test_ambient_composite_gate.py``.
"""
from __future__ import annotations

import json
import math

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
    percentile_of,
)


# --- percentile_of (midrank empirical CDF) --------------------------------------


def test_percentile_midrank_with_ties():
    """ref=[1,2,2,3]: value=2 → #{<2}=1, #{==2}=2 → (1+0.5·2)/4·100 = 50."""
    assert percentile_of(2.0, [1.0, 2.0, 2.0, 3.0]) == pytest.approx(50.0)


def test_percentile_boundaries():
    ref = [0.1, 0.2, 0.3, 0.4]
    assert percentile_of(0.05, ref) == pytest.approx(0.0)
    assert percentile_of(0.5, ref) == pytest.approx(100.0)
    # equals the minimum → midrank below 50%
    assert percentile_of(0.1, ref) == pytest.approx(100.0 * 0.5 / 4)
    # equals the maximum → midrank above 50%
    assert percentile_of(0.4, ref) == pytest.approx(100.0 * 3.5 / 4)


def test_percentile_monotone_non_decreasing():
    ref = [0.5, 0.6, 0.7, 0.8, 0.9]
    vals = [percentile_of(v, ref) for v in (0.55, 0.65, 0.75, 0.85)]
    assert vals == sorted(vals)


def test_percentile_all_tied():
    """Every reference value equal → any tie lands at exactly 50."""
    assert percentile_of(0.7, [0.7] * 5) == pytest.approx(50.0)
    # strictly below / above a fully-tied reference
    assert percentile_of(0.69, [0.7] * 5) == pytest.approx(0.0)
    assert percentile_of(0.71, [0.7] * 5) == pytest.approx(100.0)


def test_percentile_empty_reference_raises():
    with pytest.raises(ValueError):
        percentile_of(0.5, [])


def test_percentile_nonfinite_value_raises():
    with pytest.raises(ValueError):
        percentile_of(float("nan"), [0.1, 0.2])
    with pytest.raises(ValueError):
        percentile_of(float("inf"), [0.1, 0.2])


# --- composite_gate_decision -----------------------------------------------------


def _thresholds(**kw) -> CompositeGateThresholds:
    base = dict(percentile_min=85.0, margin_min=0.02, raw_floor=0.80)
    base.update(kw)
    return CompositeGateThresholds(**base)


def _decision(**kw) -> CompositeVerdict:
    base = dict(
        bm25_top=5.0,                 # weak (threshold 32.0 below)
        bm25_threshold=32.0,
        virt_top1=0.95,
        virt_median=0.70,
        raw_top1=0.86,
        reference=[0.70, 0.72, 0.74, 0.76, 0.78, 0.80, 0.82, 0.84, 0.86, 0.88],
        thresholds=_thresholds(),
        pool_size=10,
    )
    base.update(kw)
    return composite_gate_decision(**base)


def test_bm25_strong_accepts_even_without_reference():
    v = _decision(bm25_top=40.0, reference=None)
    assert v.accepted is True
    assert v.signal == "bm25_strong"
    assert v.reason is None


def test_semantic_composite_accept():
    # virt_top1=0.95 sits above the 85th percentile of the reference;
    # margin 0.25 >= 0.02; raw 0.86 >= 0.80 → all three axes pass.
    v = _decision()
    assert v.accepted is True
    assert v.signal == "semantic_composite"
    assert v.reason is None
    assert v.virt_percentile == pytest.approx(100.0)
    assert v.margin == pytest.approx(0.25)
    assert v.raw_top1 == pytest.approx(0.86)


def test_semantic_reject_below_percentile():
    # virt_top1 inside the reference band → percentile below 85 → reject.
    v = _decision(virt_top1=0.78, virt_median=0.70)
    assert v.accepted is False
    assert v.signal == "composite_reject"
    assert v.reason == "composite_reject"
    assert v.virt_percentile is not None and v.virt_percentile < 85.0


def test_semantic_reject_flat_margin():
    # penguin-profile shape: high top-1 but flat pool → margin below floor.
    v = _decision(virt_top1=0.84, virt_median=0.835)
    assert v.accepted is False
    assert v.reason == "composite_reject"
    assert v.margin == pytest.approx(0.005)


def test_semantic_reject_raw_below_floor():
    v = _decision(raw_top1=0.75)
    assert v.accepted is False
    assert v.reason == "composite_reject"


def test_margin_exactly_at_threshold_accepts():
    # >= semantics: margin exactly margin_min passes (with other axes clear).
    v = _decision(virt_top1=0.90, virt_median=0.88, thresholds=_thresholds(margin_min=0.02))
    assert v.accepted is True


def test_pool_too_small_rejects():
    v = _decision(pool_size=1)
    assert v.accepted is False
    assert v.signal == "composite_pool_too_small"
    assert v.reason == "composite_pool_too_small"


def test_pool_too_small_does_not_override_bm25_strong():
    v = _decision(bm25_top=40.0, pool_size=1)
    assert v.accepted is True
    assert v.signal == "bm25_strong"


def test_missing_raw_axis_rejects():
    v = _decision(raw_top1=None)
    assert v.accepted is False
    assert v.signal == "composite_reject"
    assert v.reason == "composite_reject"
    assert "raw" in (v.detail or "")


def test_reference_unavailable_rejects_semantic_arm():
    # fail-closed: no reference → BM25 is the only accept path.
    v = _decision(reference=None)
    assert v.accepted is False
    assert v.signal == "composite_reference_unavailable"
    assert v.reason == "composite_reference_unavailable"


def test_empty_reference_is_unavailable():
    v = _decision(reference=[])
    assert v.accepted is False
    assert v.reason == "composite_reference_unavailable"


def test_nonfinite_inputs_reject():
    v = _decision(virt_top1=float("nan"))
    assert v.accepted is False and v.reason == "composite_reject"
    v = _decision(bm25_top=float("inf"))
    assert v.accepted is False and v.reason == "composite_reject"
    v = _decision(raw_top1=float("-inf"))
    assert v.accepted is False and v.reason == "composite_reject"
    v = _decision(thresholds=_thresholds(percentile_min=float("nan")))
    assert v.accepted is False and v.reason == "composite_reject"


def test_bm25_unusable_falls_to_semantic_arm():
    # bm25_top None (gate index absent) → semantic arm decides, not a reject.
    v = _decision(bm25_top=None)
    assert v.accepted is True
    assert v.signal == "semantic_composite"


def test_diagnostics_axes_populated_on_reject():
    # margin / percentile still surface for triage on a semantic reject.
    v = _decision(virt_top1=0.78, virt_median=0.70)
    assert v.virt_percentile is not None
    assert v.margin == pytest.approx(0.08)
    assert v.raw_top1 == pytest.approx(0.86)


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
            "ambient_semantic_percentile_min": 85.0,
            "ambient_margin_min": 0.02,
            "ambient_raw_floor_composite": 0.80,
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
    assert ref.thresholds_echo["ambient_margin_min"] == 0.02
    assert ref.schema_version == COMPOSITE_ARTIFACT_SCHEMA_VERSION


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


def test_verdict_is_immutable():
    v = _decision()
    with pytest.raises(Exception):
        v.accepted = False  # type: ignore[misc]


def test_percentile_matches_math_isfinite_guard():
    # sanity: percentile_of never emits NaN for finite inputs
    v = percentile_of(0.5, [0.4, 0.5, 0.6])
    assert math.isfinite(v)
