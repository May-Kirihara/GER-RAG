"""Phase T Stage 3/4 — direct relevance qualification pure functions (unit).

Covers the qualification contract from
docs/wiki/Plans-Phase-T-Semantic-Requalification.md §3:

- ``compute_lexical_strength`` — relative-ratio contract (corpus size
  independent) with the empty-pool guard
- ``is_direct_qualified`` — per-axis OR truth table; the lexical axis
  requires BOTH the absolute (off-topic guard) and the relative
  (pool-ratio) condition, so a relative-only hit is a rejected
  false positive
- ``qualification_confidence`` — max normalized margin over the
  *passing* axes only (deterministic; no plain clamp(score, 0, 1))
"""
from __future__ import annotations

import pytest

from gaottt.core.scorer import (
    compute_lexical_strength,
    is_direct_qualified,
    qualification_confidence,
)

# Provisional thresholds from the WP-1 baseline
# (docs/notes/phase-t/score-baseline-before.json): raw p50=0.764 → 0.75
# keeps ~70% qualified; BM25 on-topic top scores are 14-58 → absolute 8.0
# is the off-topic guard.
RAW_MIN = 0.75
VIRT_MIN = 0.75
ABS_MIN = 8.0
REL_MIN = 0.40


def _qualified(
    raw: float,
    virt: float,
    bm25: float,
    lex: float,
    *,
    raw_min: float = RAW_MIN,
    virt_min: float = VIRT_MIN,
    abs_min: float = ABS_MIN,
    rel_min: float = REL_MIN,
) -> bool:
    return is_direct_qualified(raw, virt, bm25, lex, raw_min, virt_min, abs_min, rel_min)


def _confidence(
    raw: float,
    virt: float,
    bm25: float,
    lex: float,
    *,
    raw_min: float = RAW_MIN,
    virt_min: float = VIRT_MIN,
    abs_min: float = ABS_MIN,
    rel_min: float = REL_MIN,
) -> float:
    return qualification_confidence(
        raw, virt, bm25, lex, raw_min, virt_min, abs_min, rel_min,
    )


# --- compute_lexical_strength -------------------------------------------


def test_lexical_strength_relative_ratio():
    assert compute_lexical_strength(12.0, 30.0) == pytest.approx(0.4)
    assert compute_lexical_strength(30.0, 30.0) == pytest.approx(1.0)
    assert compute_lexical_strength(0.0, 30.0) == pytest.approx(0.0)


def test_lexical_strength_empty_pool_guard():
    # Empty pool (no BM25 hits / index empty) → strength 0, never a
    # division by zero.
    assert compute_lexical_strength(5.0, 0.0) == 0.0
    assert compute_lexical_strength(0.0, 0.0) == 0.0


# --- is_direct_qualified truth table ------------------------------------


def test_raw_axis_alone_qualifies():
    assert _qualified(raw=0.80, virt=0.10, bm25=0.0, lex=0.0)


def test_virtual_axis_alone_qualifies():
    # displacement-bent node: raw below floor but normalized virtual above
    assert _qualified(raw=0.60, virt=0.80, bm25=0.0, lex=0.0)


def test_lexical_axis_requires_both_absolute_and_relative():
    # strong lexical hit: absolute AND relative satisfied
    assert _qualified(raw=0.10, virt=0.10, bm25=20.0, lex=0.60)


def test_relative_only_is_rejected_false_positive():
    # relative ratio passes but the absolute score is below the off-topic
    # guard → NOT qualified (a low-scoring pool must not self-certify)
    assert not _qualified(raw=0.10, virt=0.10, bm25=6.0, lex=0.50)


def test_absolute_only_is_rejected():
    # absolute passes but the node is a weak tail of the pool → NOT qualified
    assert not _qualified(raw=0.10, virt=0.10, bm25=10.0, lex=0.20)


def test_all_axes_below_is_not_qualified():
    assert not _qualified(raw=0.30, virt=0.30, bm25=2.0, lex=0.05)


def test_boundary_equality_qualifies():
    # thresholds are inclusive (>=) on every axis
    assert _qualified(raw=RAW_MIN, virt=0.0, bm25=0.0, lex=0.0)
    assert _qualified(raw=0.0, virt=VIRT_MIN, bm25=0.0, lex=0.0)
    assert _qualified(raw=0.0, virt=0.0, bm25=ABS_MIN, lex=REL_MIN)


# --- qualification_confidence -------------------------------------------


def test_confidence_raw_margin():
    # margin = (0.90 - 0.75) / (1 - 0.75) = 0.6
    assert _confidence(raw=0.90, virt=0.0, bm25=0.0, lex=0.0) == pytest.approx(0.6)


def test_confidence_zero_at_threshold():
    assert _confidence(raw=RAW_MIN, virt=0.0, bm25=0.0, lex=0.0) == pytest.approx(0.0)


def test_confidence_zero_when_unqualified():
    # no axis passes → no learning signal even if some axis is "close"
    assert _confidence(raw=0.70, virt=0.70, bm25=6.0, lex=0.50) == pytest.approx(0.0)


def test_confidence_takes_max_over_passing_axes():
    # raw margin = (0.80-0.75)/0.25 = 0.2 ; virtual margin = (0.90-0.75)/0.25 = 0.6
    assert _confidence(raw=0.80, virt=0.90, bm25=0.0, lex=0.0) == pytest.approx(0.6)


def test_confidence_lexical_margin_uses_lexical_strength():
    # lexical axis passes (abs 20 ≥ 8, rel 0.70 ≥ 0.40):
    # margin = (0.70 - 0.40) / (1 - 0.40) = 0.5 — the *strength*, not the
    # raw BM25 score, feeds the margin (scores are 14-58, far outside [0,1]).
    assert _confidence(raw=0.1, virt=0.1, bm25=20.0, lex=0.70) == pytest.approx(0.5)


def test_confidence_monotonic_in_margin():
    prev = -1.0
    for raw in (0.75, 0.80, 0.85, 0.95, 1.0):
        conf = _confidence(raw=raw, virt=0.0, bm25=0.0, lex=0.0)
        assert conf >= prev
        prev = conf
    assert prev == pytest.approx(1.0)


def test_confidence_threshold_one_is_safe():
    # degenerate threshold = 1.0 must not divide by zero
    assert _confidence(raw=1.0, virt=0.0, bm25=0.0, lex=0.0, raw_min=1.0) == pytest.approx(1.0)
    assert _confidence(raw=0.99, virt=0.0, bm25=0.0, lex=0.0, raw_min=1.0) == pytest.approx(0.0)
