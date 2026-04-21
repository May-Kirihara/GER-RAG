from __future__ import annotations

import math
import random


def compute_mass_boost(mass: float, alpha: float) -> float:
    return alpha * math.log(1.0 + mass)


def compute_decay(last_access: float, now: float, delta: float) -> float:
    return math.exp(-delta * (now - last_access))


def compute_temp_noise(temperature: float) -> float:
    if temperature <= 0.0:
        return 0.0
    return random.gauss(0.0, temperature)


def compute_final_score(
    raw_score: float,
    mass_boost: float,
    decay: float,
    temp_noise: float,
    graph_boost: float,
) -> float:
    return raw_score * decay + mass_boost + temp_noise + graph_boost


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
