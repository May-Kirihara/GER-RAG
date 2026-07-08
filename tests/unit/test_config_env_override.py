"""Regression: H5 — per-field env-var override layer.

Precedence (highest wins): GAOTTT_<FIELD> env > config.json > default.
Only scalar fields are env-settable; the bool branch must not fall into
the ``bool("false") is True`` trap.
"""
from __future__ import annotations

import pytest

from gaottt.config import GaOTTTConfig


def test_env_overrides_default_with_correct_type(monkeypatch):
    monkeypatch.setenv("GAOTTT_GAMMA", "0.8")
    monkeypatch.setenv("GAOTTT_TOP_K", "11")
    cfg = GaOTTTConfig.from_config_file()
    assert cfg.gamma == 0.8
    assert isinstance(cfg.gamma, float)
    assert cfg.top_k == 11
    assert isinstance(cfg.top_k, int)


@pytest.mark.parametrize(
    "raw,expected",
    [("false", False), ("0", False), ("", False), ("no", False),
     ("true", True), ("1", True), ("YES", True), ("On", True)],
)
def test_bool_env_does_not_fall_into_truthy_string_trap(monkeypatch, raw, expected):
    # bool("false") is True in Python — a naive cast would make
    # GAOTTT_DREAM_ENABLED=false enable the dream loop.
    monkeypatch.setenv("GAOTTT_DREAM_ENABLED", raw)
    cfg = GaOTTTConfig.from_config_file()
    assert cfg.dream_enabled is expected


def test_env_beats_config_file(monkeypatch):
    # Simulate a config.json that sets gamma=0.3; env must still win.
    monkeypatch.setattr(
        "gaottt.config._load_config_file", lambda: {"gamma": 0.3, "top_k": 7}
    )
    # No env → file value applies.
    monkeypatch.delenv("GAOTTT_GAMMA", raising=False)
    assert GaOTTTConfig.from_config_file().gamma == 0.3
    # Env present → env wins over file.
    monkeypatch.setenv("GAOTTT_GAMMA", "0.95")
    cfg = GaOTTTConfig.from_config_file()
    assert cfg.gamma == 0.95
    assert cfg.top_k == 7  # untouched file value preserved


def test_invalid_env_value_is_ignored_not_fatal(monkeypatch):
    monkeypatch.setenv("GAOTTT_TOP_K", "not-an-int")
    cfg = GaOTTTConfig.from_config_file()  # must not raise
    assert cfg.top_k == GaOTTTConfig().top_k  # fell back to default


def test_legacy_ger_rag_env_honored_with_warning(monkeypatch, caplog):
    import logging

    monkeypatch.delenv("GAOTTT_GAMMA", raising=False)
    monkeypatch.setenv("GER_RAG_GAMMA", "0.42")
    with caplog.at_level(logging.WARNING, logger="gaottt.config"):
        cfg = GaOTTTConfig.from_config_file()
    assert cfg.gamma == 0.42
    assert any("GER_RAG_GAMMA is deprecated" in r.message for r in caplog.records)


def test_gaottt_env_takes_precedence_over_legacy(monkeypatch):
    monkeypatch.setenv("GAOTTT_GAMMA", "0.11")
    monkeypatch.setenv("GER_RAG_GAMMA", "0.99")
    assert GaOTTTConfig.from_config_file().gamma == 0.11


# WP-1 — embedder lazy spawn knobs (supervisor lazily spawns a dedicated
# embedding service). Detail: docs/plans/embedder-auto-spawn-supervisor.md.
def test_embedder_lazy_spawn_defaults():
    cfg = GaOTTTConfig()
    assert cfg.supervisor_spawn_embedder is True
    assert cfg.embedder_spawn_idle_timeout_seconds == 300.0
    assert cfg.embedder_spawn_readiness_timeout_seconds == 90.0
    assert cfg.embedder_idle_watchdog_poll_seconds == 30.0


@pytest.mark.parametrize(
    "raw,expected",
    [("false", False), ("0", False), ("no", False),
     ("true", True), ("1", True), ("YES", True), ("On", True)],
)
def test_supervisor_spawn_embedder_bool_env(monkeypatch, raw, expected):
    monkeypatch.setenv("GAOTTT_SUPERVISOR_SPAWN_EMBEDDER", raw)
    cfg = GaOTTTConfig.from_config_file()
    assert cfg.supervisor_spawn_embedder is expected


def test_embedder_lazy_spawn_float_env_overrides(monkeypatch):
    monkeypatch.setenv("GAOTTT_EMBEDDER_SPAWN_IDLE_TIMEOUT_SECONDS", "120.5")
    monkeypatch.setenv("GAOTTT_EMBEDDER_SPAWN_READINESS_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("GAOTTT_EMBEDDER_IDLE_WATCHDOG_POLL_SECONDS", "15")
    cfg = GaOTTTConfig.from_config_file()
    assert cfg.embedder_spawn_idle_timeout_seconds == 120.5
    assert isinstance(cfg.embedder_spawn_idle_timeout_seconds, float)
    assert cfg.embedder_spawn_readiness_timeout_seconds == 45.0
    assert isinstance(cfg.embedder_spawn_readiness_timeout_seconds, float)
    assert cfg.embedder_idle_watchdog_poll_seconds == 15.0
    assert isinstance(cfg.embedder_idle_watchdog_poll_seconds, float)
