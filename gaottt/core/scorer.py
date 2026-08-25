from __future__ import annotations

import math
import random


def compute_mass_boost(mass: float, alpha: float) -> float:
    return alpha * math.log(1.0 + mass)


def compute_decay(last_access: float, now: float, delta: float) -> float:
    return math.exp(-delta * (now - last_access))


def compute_semantic_factor(
    last_access: float,
    now: float,
    half_life_seconds: float,
    floor: float,
) -> float:
    """Phase T Stage 2 — semantic 項の half-life + floor 契約。

    ``factor = floor + (1 - floor) * 0.5 ** (age / half_life_seconds)``

    - age=0 → factor=1.0 (legacy ``compute_decay`` と一致)。
    - future timestamp (``last_access > now``) は ``max(0.0, age)`` で
      clamp して age=0 扱い。legacy path は clamp しない (bit-for-bit 維持)。
    - 極大 age では ``0.5 ** x`` が 0.0 へ underflow し factor は floor に
      漸近する (例外なし — floor 未満には落ちない)。
    - ``half_life_seconds > 0`` / ``0 <= floor <= 1`` は config 側で
      明示 reject される前提。
    """
    age = max(0.0, now - last_access)
    return floor + (1.0 - floor) * (0.5 ** (age / half_life_seconds))


def compute_temp_noise(temperature: float) -> float:
    if temperature <= 0.0:
        return 0.0
    return random.gauss(0.0, temperature)


def compute_emotion_boost(
    emotion_weight: float,
    alpha: float,
) -> float:
    """Bias score by absolute emotion magnitude (sign-agnostic).

    Both joyful successes (+) and frustrating failures (-) deserve to surface;
    the sign is informational metadata only.
    """
    return alpha * abs(emotion_weight)


def compute_certainty_boost(
    certainty: float,
    last_verified_at: float | None,
    now: float,
    alpha: float,
    half_life_seconds: float,
) -> float:
    """High-certainty memories that were recently re-verified score higher.

    Decays exponentially since last verification (half-life style).
    If ``last_verified_at`` is None, no decay applies — ``certainty`` is taken as-is.
    """
    if half_life_seconds <= 0:
        return alpha * certainty
    if last_verified_at is None:
        return alpha * certainty
    age = max(0.0, now - last_verified_at)
    decay = 0.5 ** (age / half_life_seconds)
    return alpha * certainty * decay


# -----------------------------------------------------------------------
# Phase T Stage 3/4 — direct relevance qualification (pure functions)
# -----------------------------------------------------------------------


def compute_lexical_strength(bm25_score: float, pool_top_score: float) -> float:
    """BM25 relative-ratio contract — corpus size independent.

    ``strength = bm25_score / pool_top_score``. A pool with no positive
    top score (empty pool / all-zero scores) yields 0.0 so the lexical
    axis can never self-certify from an empty measurement.
    """
    if pool_top_score <= 0.0:
        return 0.0
    return bm25_score / pool_top_score


def _axis_margin(score: float, threshold: float) -> float:
    """Normalized margin of ``score`` above ``threshold`` → [0, 1].

    ``threshold >= 1.0`` is degenerate (zero denominator) and resolves to
    a pure pass/fail so the confidence computation never divides by zero.
    """
    if threshold >= 1.0:
        return 1.0 if score >= threshold else 0.0
    margin = (score - threshold) / (1.0 - threshold)
    return max(0.0, min(1.0, margin))


def is_direct_qualified(
    raw_cos: float,
    virtual_cos_norm: float,
    bm25_score: float,
    lexical_strength: float,
    raw_min: float,
    virtual_min: float,
    bm25_absolute_min: float,
    bm25_relative_min: float,
) -> bool:
    """Phase T Stage 3 — does this node clear any relevance axis?

    OR over three axes; the lexical axis requires BOTH the absolute
    score (off-topic guard) and the relative pool ratio, so a
    relative-only hit in a low-scoring pool is rejected.
    """
    return (
        raw_cos >= raw_min
        or virtual_cos_norm >= virtual_min
        or (
            bm25_score >= bm25_absolute_min
            and lexical_strength >= bm25_relative_min
        )
    )


def qualification_confidence(
    raw_cos: float,
    virtual_cos_norm: float,
    bm25_score: float,
    lexical_strength: float,
    raw_min: float,
    virtual_min: float,
    bm25_absolute_min: float,
    bm25_relative_min: float,
) -> float:
    """Phase T Stage 4 — learning-signal strength for a qualified node.

    Max normalized margin over the *passing* axes only (deterministic;
    a plain clamp(score, 0, 1) would ignore how far above the floor the
    node actually is). The lexical margin uses ``lexical_strength``
    ([0, 1]), not the raw BM25 score (typically 14-58). Unqualified
    nodes return 0.0 — the caller excludes them from the learn set
    anyway, so this only scales growth for qualified nodes.
    """
    confidence = 0.0
    if raw_cos >= raw_min:
        confidence = max(confidence, _axis_margin(raw_cos, raw_min))
    if virtual_cos_norm >= virtual_min:
        confidence = max(confidence, _axis_margin(virtual_cos_norm, virtual_min))
    if (
        bm25_score >= bm25_absolute_min
        and lexical_strength >= bm25_relative_min
    ):
        confidence = max(
            confidence, _axis_margin(lexical_strength, bm25_relative_min),
        )
    return confidence
