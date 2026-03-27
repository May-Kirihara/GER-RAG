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
