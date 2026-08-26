"""Phase U WP-2 — multiverse supervisor の runtime-tuning env allowlist 単体 test。

検証対象は ``gaottt/multiverse/tuning_env.py`` の 3要素:

1. ``RUNTIME_TUNING_ENV_ALLOWLIST`` — 完全名 (exact-name) の閉じた allowlist。
   prefix/wildcard 一致は存在しないこと (未来の knob は自動 deny)。
2. ``validate_tuning_env`` — bool/int/float を ``GaOTTTConfig._coerce_env``
   と同じ coercion 規則で検証し、人間可読 error list を返す
   (NaN/Infinity / 非 bool token / 不正 int / 空値を拒否)。
3. ``filter_tuning_env`` — allowlisted key のみ通し、不正値なら
   ``TuningEnvValidationError`` で raise (fail-fast)。

staleness guard: allowlist 項目が実 ``GaOTTTConfig`` field に対応しない
場合は検証 error になる (allowlist が実 field を追跡することを強制)。
"""
from __future__ import annotations

import dataclasses

import pytest

import gaottt.multiverse.tuning_env as tuning_env
from gaottt.config import GaOTTTConfig
from gaottt.multiverse.tuning_env import (
    RUNTIME_TUNING_ENV_ALLOWLIST,
    TuningEnvValidationError,
    filter_tuning_env,
    validate_tuning_env,
)


def _config_env_field_types() -> dict[str, type]:
    """``_resolve_overrides`` と同じ規則で env 名 → field type を組み立てる。

    test 側 introspection: allowlist の各名前が *実* config field に
    対応することを、tuning_env module の実装とは独立に確認するため。
    """
    out: dict[str, type] = {}
    for f in dataclasses.fields(GaOTTTConfig):
        if f.default is dataclasses.MISSING:
            continue
        target = type(f.default)
        if target not in (bool, int, float, str):
            continue
        out[f"GAOTTT_{f.name.upper()}"] = target
    return out


_FIELD_TYPES = _config_env_field_types()
_VALID_SAMPLE = {bool: "false", int: "7", float: "0.5", str: "or"}


# ---------------------------------------------------------------------------
# 1. allowlist の閉集合性 — 実 field 追跡 (staleness guard)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(RUNTIME_TUNING_ENV_ALLOWLIST))
def test_allowlist_entry_maps_to_real_config_field(name):
    """allowlist の全項目が実 GaOTTTConfig scalar field に対応すること。"""
    assert name in _FIELD_TYPES, (
        f"{name} has no matching GaOTTTConfig scalar field — "
        f"stale allowlist entry"
    )


def test_empty_env_validates_clean():
    """空 env では staleness 以外の error は出ない (= allowlist 全体が健全)。"""
    assert validate_tuning_env({}) == []


def test_expected_knobs_are_in_allowlist():
    """Phase T/U tuning knob 28 件が過不足なく登録されていること。"""
    expected = {
        "GAOTTT_SEMANTIC_HALFLIFE_ENABLED",
        "GAOTTT_SEMANTIC_HALF_LIFE_SECONDS",
        "GAOTTT_SEMANTIC_FLOOR",
        "GAOTTT_DIRECT_QUALIFICATION_ENABLED",
        "GAOTTT_TTT_QUALIFICATION_ENABLED",
        "GAOTTT_DIRECT_RAW_COSINE_MIN",
        "GAOTTT_DIRECT_VIRTUAL_COSINE_MIN",
        "GAOTTT_DIRECT_BM25_RELATIVE_MIN",
        "GAOTTT_DIRECT_BM25_ABSOLUTE_MIN",
        "GAOTTT_DIRECT_BM25_POOL_SIZE",
        "GAOTTT_DIRECT_RESCUE_RAW_RANK",
        "GAOTTT_EXPLORE_DIVERSIFIED_PRESENTATION_ENABLED",
        "GAOTTT_EXPLORE_COHORT_PENALTY",
        "GAOTTT_EXPLORE_MIN_SEMANTIC",
        "GAOTTT_EXPLORE_DIVERSITY_POOL_MULTIPLIER",
        "GAOTTT_AMBIENT_GATE_OR_SEMANTIC",
        "GAOTTT_AMBIENT_SEMANTIC_RAW_MIN",
        "GAOTTT_AMBIENT_BM25_MIN_SCORE",
        "GAOTTT_AMBIENT_MIN_SCORE",
        "GAOTTT_AMBIENT_GATE_USE_BM25",
        # Phase U WP-3 — ambient composite gate (reference filename は
        # deployment 構造選択なので allowlist 外 = config-file only)
        "GAOTTT_AMBIENT_GATE_MODE",
        "GAOTTT_AMBIENT_SEMANTIC_PERCENTILE_MIN",
        "GAOTTT_AMBIENT_MARGIN_MIN",
        "GAOTTT_AMBIENT_RAW_FLOOR_COMPOSITE",
        "GAOTTT_AMBIENT_COMPOSITE_COUNT_DRIFT_MAX",
        # Phase U WP-6b/6c/6d (staged readiness / BM25 build / snapshot)
        "GAOTTT_READINESS_PROTOCOL_ENABLED",
        "GAOTTT_BM25_BACKGROUND_BUILD_ENABLED",
        "GAOTTT_BM25_SNAPSHOT_ENABLED",
    }
    assert RUNTIME_TUNING_ENV_ALLOWLIST == frozenset(expected)


def test_reference_filename_knob_is_not_allowlisted():
    """WP-8: GAOTTT_AMBIENT_COMPOSITE_REFERENCE_FILENAME は deployment の
    構造選択 (config-file only) なので allowlist に含めない。"""
    assert "GAOTTT_AMBIENT_COMPOSITE_REFERENCE_FILENAME" not in (
        RUNTIME_TUNING_ENV_ALLOWLIST
    )


def test_identity_and_config_env_names_are_not_allowlisted():
    """identity 系 4 key + GAOTTT_CONFIG は allowlist 外 (明示上書き専出)。"""
    for name in (
        "GAOTTT_DATA_DIR", "GAOTTT_EMBEDDER_ENDPOINT",
        "GAOTTT_OWNER_LEASE_ENABLED", "GAOTTT_BACKEND_TOKEN",
        "GAOTTT_CONFIG",
    ):
        assert name not in RUNTIME_TUNING_ENV_ALLOWLIST


def test_stale_allowlist_entry_is_hard_error(monkeypatch):
    """field の無い allowlist 項目は env に値が無くても常時 error (hard)。"""
    monkeypatch.setattr(
        tuning_env, "RUNTIME_TUNING_ENV_ALLOWLIST",
        frozenset({"GAOTTT_NO_SUCH_KNOB_XYZ"}),
    )
    errors = validate_tuning_env({})
    assert len(errors) == 1
    assert "GAOTTT_NO_SUCH_KNOB_XYZ" in errors[0]


# ---------------------------------------------------------------------------
# 2. 値検証 — bool / int / float / empty
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(RUNTIME_TUNING_ENV_ALLOWLIST))
def test_valid_value_passes_filter(name):
    """型に応じた有効値なら error ゼロ・そのまま通過。"""
    target = _FIELD_TYPES[name]
    value = _VALID_SAMPLE[target]
    env = {name: value}
    assert validate_tuning_env(env) == []
    assert filter_tuning_env(env) == {name: value}


@pytest.mark.parametrize(
    "raw", ["1", "true", "yes", "on", "0", "false", "no", "off",
            "TRUE", "False", "Yes", "OFF", " true "],
)
def test_bool_tokens_accepted(raw):
    env = {"GAOTTT_DIRECT_QUALIFICATION_ENABLED": raw}
    assert validate_tuning_env(env) == []


@pytest.mark.parametrize("raw", ["banana", "maybe", "2", "-1", "enabled=true"])
def test_non_bool_token_rejected(raw):
    errors = validate_tuning_env(
        {"GAOTTT_DIRECT_QUALIFICATION_ENABLED": raw})
    assert len(errors) == 1
    assert "GAOTTT_DIRECT_QUALIFICATION_ENABLED" in errors[0]


@pytest.mark.parametrize(
    "raw", ["nan", "NaN", "inf", "-inf", "+inf", "Infinity", "-Infinity"],
)
def test_non_finite_float_rejected(raw):
    errors = validate_tuning_env({"GAOTTT_SEMANTIC_FLOOR": raw})
    assert len(errors) == 1
    assert "GAOTTT_SEMANTIC_FLOOR" in errors[0]
    assert "nan" in errors[0].lower() or "finite" in errors[0].lower()


@pytest.mark.parametrize("raw", ["0.35", "-0.1", "1e3", " 0.5 ", "604800"])
def test_finite_floats_accepted(raw):
    assert validate_tuning_env({"GAOTTT_SEMANTIC_FLOOR": raw}) == []


@pytest.mark.parametrize("raw", ["abc", "3.5", "1e3", "many"])
def test_garbage_int_rejected(raw):
    errors = validate_tuning_env({"GAOTTT_DIRECT_BM25_POOL_SIZE": raw})
    assert len(errors) == 1
    assert "GAOTTT_DIRECT_BM25_POOL_SIZE" in errors[0]


@pytest.mark.parametrize("raw", ["", "   "])
def test_empty_value_rejected(raw):
    errors = validate_tuning_env({"GAOTTT_SEMANTIC_FLOOR": raw})
    assert len(errors) == 1
    assert "empty" in errors[0]


# ---------------------------------------------------------------------------
# 2b. str enum knob — GAOTTT_AMBIENT_GATE_MODE (WP-8)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["or", "composite"])
def test_gate_mode_valid_tokens_accepted(raw):
    """ambient_gate_mode は "or"/"composite" のみ通過 (str への _coerce_env
    は identity なので空白・大小文字の宥和は無い = config 挙動と同一)。"""
    assert validate_tuning_env({"GAOTTT_AMBIENT_GATE_MODE": raw}) == []
    assert filter_tuning_env({"GAOTTT_AMBIENT_GATE_MODE": raw}) == {
        "GAOTTT_AMBIENT_GATE_MODE": raw,
    }


@pytest.mark.parametrize(
    "raw", ["banana", "OR", "Composite", " or ", "or|composite", "3"],
)
def test_gate_mode_invalid_token_rejected(raw):
    """許容集合外の値は spawn 時点で拒否 (config __post_init__ との二重検査 —
    spawn-refusal は parse error と enum 違反の両方で効く)。"""
    errors = validate_tuning_env({"GAOTTT_AMBIENT_GATE_MODE": raw})
    assert len(errors) == 1
    assert "GAOTTT_AMBIENT_GATE_MODE" in errors[0]
    assert "'or' or 'composite'" in errors[0]


def test_multiple_invalid_values_all_reported_in_sorted_order():
    env = {
        "GAOTTT_SEMANTIC_FLOOR": "nan",
        "GAOTTT_DIRECT_BM25_POOL_SIZE": "abc",
        "GAOTTT_AMBIENT_GATE_OR_SEMANTIC": "maybe",
    }
    errors = validate_tuning_env(env)
    assert len(errors) == 3
    # 名前順で決定的 (test・log の両方で読める)
    assert [e.split("=")[0] for e in errors] == sorted(env)


def test_non_allowlisted_gaottt_vars_are_ignored_not_errors():
    """allowlist 外の GAOTTT_* は error にせず単に無視 (strip 側の管轄)。"""
    env = {
        "GAOTTT_FUTURE_KNOB": "garbage",
        "GAOTTT_DATA_DIR": "/attacker",
        "GAOTTT_CONFIG": "/attacker.json",
        "PATH": "/usr/bin",
    }
    assert validate_tuning_env(env) == []


# ---------------------------------------------------------------------------
# 3. filter — 通過 / 拒否
# ---------------------------------------------------------------------------

def test_filter_returns_only_allowlisted_keys_present_in_env():
    env = {
        "GAOTTT_DATA_DIR": "/attacker",
        "GAOTTT_CONFIG": "/attacker.json",
        "GAOTTT_FUTURE_KNOB": "1",
        "GAOTTT_SEMANTIC_FLOOR": "0.4",
        "PATH": "/usr/bin",
    }
    assert filter_tuning_env(env) == {"GAOTTT_SEMANTIC_FLOOR": "0.4"}


def test_filter_empty_env_returns_empty_dict():
    assert filter_tuning_env({}) == {}


def test_filter_raises_with_error_list_on_invalid():
    with pytest.raises(TuningEnvValidationError) as excinfo:
        filter_tuning_env({"GAOTTT_SEMANTIC_FLOOR": "nan"})
    assert excinfo.value.errors, "error list must be carried on the exception"
    assert any("GAOTTT_SEMANTIC_FLOOR" in e for e in excinfo.value.errors)
    assert "GAOTTT_SEMANTIC_FLOOR" in str(excinfo.value)


def test_validation_error_is_value_error_subclass():
    """supervisor の既存 error 経路 (ValueError 系) と共存できる継承関係。"""
    assert issubclass(TuningEnvValidationError, ValueError)
