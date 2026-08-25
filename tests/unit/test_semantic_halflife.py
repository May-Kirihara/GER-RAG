"""Phase T Stage 2 — semantic decay の half-life + floor 契約 (unit)。

契約 (docs/wiki/Plans-Phase-T-Semantic-Requalification.md §3 Stage 2):

    factor = floor + (1 - floor) * 0.5 ** (age / half_life_seconds)

- age=0 → factor=1.0 (legacy ``compute_decay`` と一致 — 新規 node の
  既存 test は影響なし)
- future timestamp (last_access > now) は新契約側のみ clamp して age=0 扱い
- legacy path (``compute_decay``) は式も未来 timestamp 挙動も bit-for-bit
  維持 (clamp なし — >1 になり得る仕様保持)
- invalid config (half_life<=0, floor<0, floor>1) は明示 reject
"""
from __future__ import annotations

import math

import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.scorer import compute_decay, compute_semantic_factor

HALF_LIFE = 604800.0  # 7 days (provisional default)
FLOOR = 0.35


def test_factor_is_exactly_one_at_age_zero():
    now = 1_000_000.0
    assert compute_semantic_factor(now, now, HALF_LIFE, FLOOR) == 1.0


def test_one_half_life():
    expected = FLOOR + (1.0 - FLOOR) * 0.5
    assert compute_semantic_factor(0.0, HALF_LIFE, HALF_LIFE, FLOOR) == (
        pytest.approx(expected)
    )


def test_two_half_lives():
    expected = FLOOR + (1.0 - FLOOR) * 0.25
    assert compute_semantic_factor(0.0, 2.0 * HALF_LIFE, HALF_LIFE, FLOOR) == (
        pytest.approx(expected)
    )


def test_huge_age_underflows_safely_to_floor():
    # 0.5 ** huge は 0.0 に underflow する (OverflowError なし) —
    # factor は floor に漸近し、それ未満に落ちない。
    factor = compute_semantic_factor(0.0, 1e18 * HALF_LIFE, HALF_LIFE, FLOOR)
    assert factor == FLOOR


def test_future_timestamp_clamped_to_age_zero():
    now = 1_000_000.0
    factor = compute_semantic_factor(now + 3600.0, now, HALF_LIFE, FLOOR)
    assert factor == 1.0


def test_factor_stays_within_floor_and_one():
    for age in (1.0, 60.0, 3600.0, 86400.0, HALF_LIFE, 10.0 * HALF_LIFE):
        factor = compute_semantic_factor(0.0, age, HALF_LIFE, FLOOR)
        assert FLOOR <= factor <= 1.0


# --- legacy path は bit-for-bit 維持 ----------------------------------------


def test_legacy_compute_decay_formula_unchanged():
    assert compute_decay(100.0, 200.0, 0.01) == math.exp(-0.01 * 100.0)
    assert compute_decay(0.0, 604800.0, 0.01) == 0.0  # exp(-6048) underflow


def test_legacy_compute_decay_future_timestamp_exceeds_one():
    # 仕様保持: legacy path は clamp しない (flag off = 完全旧挙動)。
    now = 1_000_000.0
    assert compute_decay(now + 100.0, now, 0.01) > 1.0


# --- config knobs + validation ----------------------------------------------


def test_semantic_halflife_defaults():
    cfg = GaOTTTConfig()
    assert cfg.semantic_halflife_enabled is True
    assert cfg.semantic_half_life_seconds == 604800.0
    assert cfg.semantic_floor == 0.35


@pytest.mark.parametrize(
    "kwargs",
    [
        {"semantic_half_life_seconds": 0.0},
        {"semantic_half_life_seconds": -604800.0},
        {"semantic_floor": -0.01},
        {"semantic_floor": 1.01},
    ],
)
def test_invalid_semantic_halflife_config_is_rejected(kwargs):
    with pytest.raises(ValueError):
        GaOTTTConfig(**kwargs)


def test_env_overrides_semantic_halflife_knobs(monkeypatch):
    monkeypatch.setattr("gaottt.config._load_config_file", lambda: {})
    monkeypatch.setenv("GAOTTT_SEMANTIC_HALFLIFE_ENABLED", "false")
    monkeypatch.setenv("GAOTTT_SEMANTIC_FLOOR", "0.5")
    monkeypatch.setenv("GAOTTT_SEMANTIC_HALF_LIFE_SECONDS", "86400")
    cfg = GaOTTTConfig.from_config_file()
    assert cfg.semantic_halflife_enabled is False
    assert cfg.semantic_floor == 0.5
    assert cfg.semantic_half_life_seconds == 86400.0
    assert isinstance(cfg.semantic_half_life_seconds, float)


def test_invalid_domain_env_value_is_rejected_not_silently_used(monkeypatch):
    # unparseable env は既存どおり無視されるが、parse 可能で domain 外の
    # 値は明示 reject (rumtime で floor>1 の factor を生む前に fail-fast)。
    monkeypatch.setattr("gaottt.config._load_config_file", lambda: {})
    monkeypatch.setenv("GAOTTT_SEMANTIC_FLOOR", "1.5")
    with pytest.raises(ValueError):
        GaOTTTConfig.from_config_file()
