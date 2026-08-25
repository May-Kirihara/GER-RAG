"""Phase T final review — reason-line regression tests (blocker #2).

Complements ``tests/unit/test_explain_score.py`` (unchanged): the
``explain_score`` branch ordering when the query-path ``lensing_gap``
signal (``virtual_cos_norm − raw_cos``, populated for every scored node
once qualification ran — see ``ScoreBreakdown``) collides with the
Phase T Stage 3 unqualified-fallback verdict, plus the explain-side
min-gap below which a positive gap is treated as displacement noise.
"""
from __future__ import annotations

from gaottt.core.explain import explain_score
from gaottt.core.types import ScoreBreakdown


def test_unqualified_fallback_outranks_lensing_pick() -> None:
    """qualified=False + meaningful gap → gravity pick, never lensing.

    The masking regression: displacement noise can push the query-path
    gap positive on a node that failed every relevance axis — the
    fallback verdict is the fact, the gap is the artifact.
    """
    b = ScoreBreakdown(
        virtual_cosine=0.40,
        node_mass=3.0,           # would also fire the dominance hint
        bm25_score=1.5,
        lensing_gap=0.05,        # ≥ explain min-gap: lensing would fire unmasked
        qualified=False,
    )
    reason = explain_score(b)
    assert reason is not None
    assert "gravity pick (below relevance floor)" in reason
    assert "lensing pick" not in reason
    assert "TTT update gated" in reason


def test_lensing_pick_wins_for_qualified_node() -> None:
    """qualified=True + meaningful gap → legacy lensing label."""
    b = ScoreBreakdown(
        virtual_cosine=0.40,
        lensing_gap=0.05,
        qualified=True,
    )
    reason = explain_score(b)
    assert reason is not None
    assert reason.startswith("lensing pick")
    assert "gap=+0.05" in reason


def test_lensing_pick_wins_when_qualification_silent() -> None:
    """qualified=None (flags off / legacy callers) → legacy lensing label."""
    b = ScoreBreakdown(
        virtual_cosine=0.40,
        lensing_gap=0.05,
    )
    reason = explain_score(b)
    assert reason is not None
    assert reason.startswith("lensing pick")
    assert "gap=+0.05" in reason


def test_subthreshold_gap_is_treated_as_noise() -> None:
    """0 < gap < explain min-gap → not a lensing pick; falls through to
    the ordinary signal branches (here: the semantic-match fallback)."""
    b = ScoreBreakdown(
        virtual_cosine=0.62,
        lensing_gap=0.01,
    )
    reason = explain_score(b)
    assert reason is not None
    assert "lensing pick" not in reason
    assert "semantic match" in reason


def test_dormant_still_wins_over_unqualified_fallback() -> None:
    """The dormant channel keeps its own outright label — its picks are
    deliberately below the relevance floor, so the dormant label is the
    more informative one."""
    b = ScoreBreakdown(
        virtual_cosine=0.30,
        node_mass=1.5,
        dormant_percentile=12.0,
        qualified=False,
    )
    reason = explain_score(b)
    assert reason is not None
    assert reason.startswith("dormant surface")
    assert "gravity pick" not in reason


def test_unqualified_fallback_is_primary_over_forced_prefix() -> None:
    """Forced + unqualified: the fallback verdict is the top-priority
    label; the forced marker stays as a stacked factor."""
    b = ScoreBreakdown(
        virtual_cosine=0.50,
        forced_inclusion=True,
        qualified=False,
    )
    reason = explain_score(b)
    assert reason is not None
    assert reason.startswith("gravity pick")
    assert "forced via tag/persona_context" in reason
