import math

from gaottt.core.scorer import compute_certainty_boost, compute_emotion_boost


def test_emotion_boost_uses_absolute_value():
    pos = compute_emotion_boost(0.8, alpha=0.04)
    neg = compute_emotion_boost(-0.8, alpha=0.04)
    zero = compute_emotion_boost(0.0, alpha=0.04)
    assert pos == neg
    assert pos > zero
    assert math.isclose(pos, 0.04 * 0.8)


def test_certainty_boost_no_decay_when_never_verified():
    out = compute_certainty_boost(
        certainty=0.9,
        last_verified_at=None,
        now=1000.0,
        alpha=0.02,
        half_life_seconds=86400.0,
    )
    assert math.isclose(out, 0.02 * 0.9)


def test_certainty_boost_decays_exponentially_after_verification():
    half_life = 86400.0  # 1 day
    fresh = compute_certainty_boost(
        certainty=1.0,
        last_verified_at=1000.0,
        now=1000.0,
        alpha=0.02,
        half_life_seconds=half_life,
    )
    one_half_life = compute_certainty_boost(
        certainty=1.0,
        last_verified_at=1000.0,
        now=1000.0 + half_life,
        alpha=0.02,
        half_life_seconds=half_life,
    )
    assert math.isclose(fresh, 0.02)
    assert math.isclose(one_half_life, 0.01, rel_tol=1e-6)


def test_certainty_boost_zero_half_life_disables_decay():
    out = compute_certainty_boost(
        certainty=0.5,
        last_verified_at=0.0,
        now=10**9,
        alpha=0.1,
        half_life_seconds=0.0,
    )
    assert math.isclose(out, 0.05)
