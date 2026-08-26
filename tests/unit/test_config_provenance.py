"""Phase U WP-1 — config resolution provenance unit tests.

``GaOTTTConfig.resolve_config_with_sources`` records, at resolution time,
where each field's effective value came from (``env`` / ``file`` /
``default``). The true-source property matters most where a heuristic
diff against a default instance fails: an env override whose value
happens to equal the default must still report ``env``.
"""
from __future__ import annotations

import json
import os
from dataclasses import fields

import pytest

import gaottt.config as config_module
from gaottt.config import GaOTTTConfig


def _purge_gaottt_env(monkeypatch) -> None:
    """Delete every GAOTTT_* / GER_RAG_* env var (monkeypatch restores
    them afterward). Provenance assertions demand a hermetic environment;
    in the full-suite order other tests leak tuning env vars, and a
    leaked ``GAOTTT_<FIELD>`` would surface as a bogus ``env`` source."""
    for name in list(os.environ):
        if name.startswith("GAOTTT_") or name.startswith("GER_RAG_"):
            monkeypatch.delenv(name, raising=False)


def test_clean_environment_all_defaults(tmp_path, monkeypatch):
    """No env / no config file → every field reports ``default`` and the
    config equals the plain ``from_config_file`` resolution."""
    _purge_gaottt_env(monkeypatch)
    monkeypatch.setattr(
        config_module, "_CONFIG_FILE_PATHS", [tmp_path / "nonexistent.json"],
    )
    cfg, sources = GaOTTTConfig.resolve_config_with_sources()
    all_names = {f.name for f in fields(GaOTTTConfig)}
    assert set(sources) == all_names
    assert set(sources.values()) == {"default"}
    assert cfg == GaOTTTConfig.from_config_file()


def test_env_override_records_env_even_when_equal_to_default(monkeypatch, tmp_path):
    """The true-source discriminator: GAOTTT_TTT_QUALIFICATION_ENABLED=true
    is the same *value* as the promoted default but a different *source*."""
    _purge_gaottt_env(monkeypatch)
    monkeypatch.setattr(
        config_module, "_CONFIG_FILE_PATHS", [tmp_path / "nonexistent.json"],
    )
    monkeypatch.setenv("GAOTTT_TTT_QUALIFICATION_ENABLED", "true")
    monkeypatch.setenv("GAOTTT_SEMANTIC_FLOOR", "0.2")
    cfg, sources = GaOTTTConfig.resolve_config_with_sources()
    assert sources["ttt_qualification_enabled"] == "env"
    assert sources["semantic_floor"] == "env"
    assert sources["direct_qualification_enabled"] == "default"
    assert cfg.ttt_qualification_enabled is True  # value == default, source != default
    assert cfg.semantic_floor == pytest.approx(0.2)


def test_config_file_records_file_and_env_wins(monkeypatch, tmp_path):
    """file-only field → ``file``; a field set by both → ``env`` (the H5
    precedence), with the env value effective."""
    _purge_gaottt_env(monkeypatch)
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({
        "semantic_floor": 0.4,          # file only
        "explore_cohort_penalty": 0.2,  # overridden by env below
        "not_a_field": "ignored",       # unknown keys are dropped
    }), encoding="utf-8")
    monkeypatch.setattr(config_module, "_CONFIG_FILE_PATHS", [conf])
    monkeypatch.setenv("GAOTTT_EXPLORE_COHORT_PENALTY", "0.3")
    cfg, sources = GaOTTTConfig.resolve_config_with_sources()
    assert sources["semantic_floor"] == "file"
    assert cfg.semantic_floor == pytest.approx(0.4)
    assert sources["explore_cohort_penalty"] == "env"
    assert cfg.explore_cohort_penalty == pytest.approx(0.3)
    assert "not_a_field" not in sources


def test_invalid_env_override_dropped_and_stays_default(monkeypatch, tmp_path):
    """Unparseable env value is logged and ignored — field stays
    ``default`` and keeps the default value (H5 behaviour unchanged)."""
    _purge_gaottt_env(monkeypatch)
    monkeypatch.setattr(
        config_module, "_CONFIG_FILE_PATHS", [tmp_path / "nonexistent.json"],
    )
    monkeypatch.setenv("GAOTTT_SEMANTIC_HALF_LIFE_SECONDS", "not-a-float")
    cfg, sources = GaOTTTConfig.resolve_config_with_sources()
    assert sources["semantic_half_life_seconds"] == "default"
    assert cfg.semantic_half_life_seconds == pytest.approx(604800.0)


def test_legacy_ger_rag_env_records_env(monkeypatch, tmp_path):
    _purge_gaottt_env(monkeypatch)
    monkeypatch.setattr(
        config_module, "_CONFIG_FILE_PATHS", [tmp_path / "nonexistent.json"],
    )
    monkeypatch.setenv("GER_RAG_SEMANTIC_FLOOR", "0.25")
    cfg, sources = GaOTTTConfig.resolve_config_with_sources()
    assert sources["semantic_floor"] == "env"
    assert cfg.semantic_floor == pytest.approx(0.25)
