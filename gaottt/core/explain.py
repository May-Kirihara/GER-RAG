"""Observation Apparatus Refinement Stage 1 — reason-line generator.

Pure function that turns a :class:`ScoreBreakdown` into a short
human-readable string summarizing which factors dominated the score.
This is *observation layer only* — it never touches force, mass, or
displacement. The Phase M single-rule (source/class-blind physics) is
preserved by construction: ``explain_score`` reads breakdown fields and
returns a string. It does not feed back into ranking.
"""

from __future__ import annotations

from gaottt.core.types import ScoreBreakdown

_DOMINANCE_HINT = "possible dominance artifact"

# Explain-side minimum curvature for the "lensing pick" label (display
# layer — deliberately not a config knob since explain never feeds back
# into ranking). The ambient channel already gates its own picks at
# ``config.ambient_lensing_min_gap`` (0.05, production-calibrated bend),
# but on the engine query path ``lensing_gap`` doubles as
# ``virtual_cos_norm - raw_cos`` for *every* scored node once
# qualification ran (see ``ScoreBreakdown``), and displacement noise can
# push that difference slightly positive on nodes with no learned
# association at all. 0.02 keeps the label for genuinely bent
# associations (ambient picks at >= 0.05 always clear it) while
# sub-threshold query-path gaps fall through to the ordinary branches.
_LENSING_PICK_MIN_GAP = 0.02


def explain_score(
    breakdown: ScoreBreakdown,
    *,
    mass_dominance_threshold: float = 2.0,
    bm25_strong_threshold: float = 0.5,
) -> str | None:
    """Return a 60-100 char reason line, or ``None`` when nothing to say.

    Decision order (first match wins for the *prefix*, secondary factors
    are appended after ``+``):

    1. ``dormant_percentile`` is not None → "dormant surface (percentile=N)"
    2. ``qualified is False`` (Phase T Stage 3) → "gravity pick (below
       relevance floor)" + TTT-gated hint. Outranks the lensing branch:
       a fallback verdict is a fact about the node while a small
       query-path gap is displacement noise (Codex final review blocker #2)
    3. ``lensing_gap >= _LENSING_PICK_MIN_GAP`` (on a node that did not
       fail qualification) → "lensing pick (gap=+0.XX)"
    4. ``forced_inclusion`` → "forced via tag/persona_context"
    5. ``node_mass >= mass_dominance_threshold`` and ``virtual_cosine < 0.5``
       → "high mass persona proximity (mass=X.XX)" + dominance-artifact hint
    6. ``bm25_score >= bm25_strong_threshold`` → "bm25 strong lexical match"
    7. Fallback: "semantic match (cos=X.XX)" when ``virtual_cosine`` is
       the dominant additive term

    Returns ``None`` only when no signal is meaningful (all zero / cold).
    """
    parts: list[str] = []
    hints: list[str] = []

    # 1. dormant surface — wins outright (counter-importance sampling channel)
    if breakdown.dormant_percentile is not None:
        return (
            f"dormant surface (percentile={breakdown.dormant_percentile:.0f}, "
            f"mass={breakdown.node_mass:.2f}) — counter-importance sampling"
        )

    # 2. Phase T Stage 3 — unqualified fallback pick. Fires only when
    #    qualification ran (``qualified`` is not None) and the node failed
    #    every relevance axis: the field (mass/wave), not semantics, carried
    #    it into the presentation. Decided before the lensing branch —
    #    on the query path lensing_gap is populated for every scored node
    #    as virtual_cos_norm - raw_cos, so a noise-level positive gap must
    #    not mask the fallback verdict behind a "lensing pick" label.
    if breakdown.qualified is False:
        parts.append("gravity pick (below relevance floor)")
        hints.append("TTT update gated")

    # 3. lensing pick — wins outright (field-connected but semantically
    #    distant), but only when the curvature is meaningful (min-gap
    #    above) and the node is not an unqualified fallback (branch 2
    #    outranks it).
    if (
        breakdown.qualified is not False
        and breakdown.lensing_gap >= _LENSING_PICK_MIN_GAP
    ):
        return (
            f"lensing pick (gap=+{breakdown.lensing_gap:.2f}) — "
            "semantically distant but field-connected"
        )

    # 4. forced inclusion — informational prefix, may stack with other signals below
    if breakdown.forced_inclusion:
        parts.append("forced via tag/persona_context")

    # 5. high mass + weak cosine = Heavy Persona Dominance candidate
    mass_dominates = (
        breakdown.node_mass >= mass_dominance_threshold
        and breakdown.virtual_cosine < 0.5
    )
    if mass_dominates:
        parts.append(f"high mass persona proximity (mass={breakdown.node_mass:.2f})")
        hints.append(_DOMINANCE_HINT)

    # 6. BM25 strong match — works alongside other signals
    if breakdown.bm25_score >= bm25_strong_threshold:
        parts.append(f"bm25 strong lexical match ({breakdown.bm25_score:.2f})")
    elif breakdown.bm25_contributed and breakdown.bm25_score > 0:
        parts.append(f"bm25 lexical assist ({breakdown.bm25_score:.2f})")

    # 7. fallback: semantic match if nothing fired and virtual_cosine carries the score
    if not parts and breakdown.virtual_cosine >= 0.3:
        parts.append(f"semantic match (cos={breakdown.virtual_cosine:.2f})")

    if not parts:
        return None

    text = " + ".join(parts)
    if hints:
        text = f"{text} — {', '.join(hints)}"
    return text
