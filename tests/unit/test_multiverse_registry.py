"""MV3 Multiverse — config knobs + local registry tests (test-first / RED stage).

These tests assume the WP-1 contract:

  * 7 config knobs added to ``gaottt/config.py`` (``multiverse_root``,
    ``supervisor_port``, ``supervisor_admin_key``, ``universe_port_range_start``,
    ``universe_port_range_end``, ``supervisor_spawn_concurrency``,
    ``supervisor_readiness_timeout``) — all scalar, env-overridable via the
    generic ``GAOTTT_<FIELD>`` loop.

  * ``gaottt/multiverse/registry.py`` exposing::

        def hash_key(plaintext: str) -> str          # sha256 hex
        def generate_api_key() -> str                # secrets.token_urlsafe(32)
        def is_port_free(host: str, port: int) -> bool

        class MultiverseRegistry:
            def __init__(self, multiverse_root: Path): ...   # lazy DB
            async def initialize(self) -> None               # schema + reconcile
            async def allocate_port(self, range_start: int, range_end: int) -> int
            async def create_universe(self, universe_id, owner_label, port,
                                      embedder_id, embedder_version) -> str   # plaintext key
            async def verify_api_key(self, plaintext: str) -> str | None
            async def revoke_api_key(self, key_hash: str) -> None
            async def get_universe(self, universe_id: str) -> dict | None
            async def list_universes(self) -> list[dict]
            async def delete_universe(self, universe_id: str) -> None
            async def reconcile(self) -> None

  schema (registry.db at <multiverse_root>/registry.db):
    universes(universe_id PK, owner_label, port, status, embedder_id,
              embedder_version, created_at)
    api_keys(key_hash PK, universe_id, created_at, revoked_at)
    status ∈ {'active', 'orphan', 'deleted'}

To keep pytest collection intact (WP-1 learning: a module-top-level import of an
unimplemented module aborts collection for the whole suite), the unimplemented
module is imported **inside each test function**.
"""
from __future__ import annotations

import logging
import socket
from pathlib import Path

import pytest

from gaottt.config import GaOTTTConfig

UNIVERSES_SUBDIR = "universes"
TRASH_SUBDIR = "trash"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

async def _make_registry(root: Path):
    """Construct + initialize a registry rooted at ``root``."""
    from gaottt.multiverse.registry import MultiverseRegistry

    reg = MultiverseRegistry(root)
    await reg.initialize()
    return reg


def _grab_free_port() -> int:
    """Ask the OS for an ephemeral port and release it (racy but fine for tests)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


# ===========================================================================
# 1. config knobs — existence + defaults + env override
# ===========================================================================

def test_config_knobs_have_documented_defaults():
    """The 7 WP-1 knobs must exist on GaOTTTConfig with the plan's defaults."""
    cfg = GaOTTTConfig()
    assert cfg.multiverse_root == ""
    assert cfg.supervisor_port == 7880
    assert cfg.supervisor_admin_key == ""
    assert cfg.universe_port_range_start == 7890
    assert cfg.universe_port_range_end == 7989
    assert cfg.supervisor_spawn_concurrency == 3
    assert cfg.supervisor_readiness_timeout == 90.0


def test_config_knobs_env_overridable(monkeypatch):
    """Knobs are scalar, so the generic GAOTTT_<FIELD> loop wires them."""
    monkeypatch.setenv("GAOTTT_SUPERVISOR_PORT", "9999")
    monkeypatch.setenv("GAOTTT_MULTIVERSE_ROOT", "/tmp/mv-root")
    monkeypatch.setenv("GAOTTT_SUPERVISOR_ADMIN_KEY", "secret")
    monkeypatch.setenv("GAOTTT_SUPERVISOR_SPAWN_CONCURRENCY", "5")
    monkeypatch.setenv("GAOTTT_SUPERVISOR_READINESS_TIMEOUT", "45.5")

    cfg = GaOTTTConfig.from_config_file()
    assert cfg.supervisor_port == 9999
    assert cfg.multiverse_root == "/tmp/mv-root"
    assert cfg.supervisor_admin_key == "secret"
    assert cfg.supervisor_spawn_concurrency == 5
    assert cfg.supervisor_readiness_timeout == 45.5


def test_config_default_is_feature_off():
    """Empty multiverse_root default keeps the feature OFF (default 不変)."""
    cfg = GaOTTTConfig()
    assert cfg.multiverse_root == ""
    assert cfg.supervisor_admin_key == ""  # empty => supervisor fail-fast


# ===========================================================================
# 2. hash_key — deterministic, distinct
# ===========================================================================

def test_hash_key_same_plaintext_same_hash():
    from gaottt.multiverse.registry import hash_key

    assert hash_key("hello-secret") == hash_key("hello-secret")


def test_hash_key_different_plaintext_different_hash():
    from gaottt.multiverse.registry import hash_key

    assert hash_key("alpha") != hash_key("beta")


def test_hash_key_returns_hex_string():
    from gaottt.multiverse.registry import hash_key

    h = hash_key("x")
    # sha256 hex digest = 64 hex chars
    assert len(h) == 64
    int(h, 16)  # raises if not hex


# ===========================================================================
# 3. generate_api_key — entropy + uniqueness
# ===========================================================================

def test_generate_api_key_unique_across_calls():
    from gaottt.multiverse.registry import generate_api_key

    keys = {generate_api_key() for _ in range(50)}
    assert len(keys) == 50  # all distinct


def test_generate_api_key_has_sufficient_entropy():
    from gaottt.multiverse.registry import generate_api_key

    key = generate_api_key()
    # secrets.token_urlsafe(32) -> ~43 chars, ~256 bits of entropy.
    assert len(key) >= 32


# ===========================================================================
# 4. is_port_free — free vs occupied
# ===========================================================================

def test_is_port_free_true_for_unoccupied_port():
    from gaottt.multiverse.registry import is_port_free

    port = _grab_free_port()
    # No guarantee the port is still free after close, so retry once.
    assert is_port_free("127.0.0.1", port) or is_port_free("127.0.0.1", _grab_free_port())


def test_is_port_free_false_for_occupied_port():
    from gaottt.multiverse.registry import is_port_free

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    occupied = s.getsockname()[1]
    try:
        assert is_port_free("127.0.0.1", occupied) is False
    finally:
        s.close()


# ===========================================================================
# 5. allocate_port — registry + OS aware
# ===========================================================================

async def test_allocate_port_returns_lowest_free_in_range(tmp_path: Path):
    reg = await _make_registry(tmp_path)
    try:
        p = await reg.allocate_port(9000, 9010)
        assert p == 9000
    finally:
        await reg.close()


async def test_allocate_port_skips_registry_used_ports(tmp_path: Path):
    reg = await _make_registry(tmp_path)
    try:
        await reg.create_universe(
            "u1", "owner-a", 9000, "stub-emb", "v1"
        )
        # 9000 is now in the registry -> next alloc picks 9001
        p = await reg.allocate_port(9000, 9010)
        assert p == 9001
    finally:
        await reg.close()


async def test_allocate_port_skips_os_occupied_port(tmp_path: Path):
    reg = await _make_registry(tmp_path)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    occupied = s.getsockname()[1]
    try:
        # narrow range around the occupied port -> must return the neighbour
        p = await reg.allocate_port(occupied, occupied + 1)
        assert p == occupied + 1
    finally:
        s.close()
        await reg.close()


async def test_allocate_port_exhausted_raises(tmp_path: Path):
    reg = await _make_registry(tmp_path)
    try:
        # occupy the only port in the registry
        await reg.create_universe("u1", "owner", 9100, "emb", "v1")
        with pytest.raises(RuntimeError):
            await reg.allocate_port(9100, 9100)
    finally:
        await reg.close()


async def test_allocate_port_reuses_deleted_universe_port(tmp_path: Path):
    reg = await _make_registry(tmp_path)
    try:
        await reg.create_universe("u1", "owner", 9200, "emb", "v1")
        await reg.delete_universe("u1")
        # deleted universe frees its port for reallocation
        p = await reg.allocate_port(9200, 9210)
        assert p == 9200
    finally:
        await reg.close()


# ===========================================================================
# 6. create_universe — insert + plaintext key + conflict
# ===========================================================================

async def test_create_universe_returns_plaintext_key_and_persists(tmp_path: Path):
    reg = await _make_registry(tmp_path)
    try:
        plaintext = await reg.create_universe(
            "u-abc", "owner-x", 9300, "stub-embedder", "v2"
        )
        assert isinstance(plaintext, str)
        assert plaintext  # non-empty

        row = await reg.get_universe("u-abc")
        assert row is not None
        assert row["universe_id"] == "u-abc"
        assert row["owner_label"] == "owner-x"
        assert row["port"] == 9300
        assert row["status"] == "active"
        assert row["embedder_id"] == "stub-embedder"
        assert row["embedder_version"] == "v2"
        assert isinstance(row["created_at"], float)

        # the returned plaintext verifies back to this universe
        assert await reg.verify_api_key(plaintext) == "u-abc"
    finally:
        await reg.close()


async def test_create_universe_duplicate_id_conflicts(tmp_path: Path):
    reg = await _make_registry(tmp_path)
    try:
        await reg.create_universe("u-dup", "owner", 9400, "emb", "v1")
        with pytest.raises(Exception):
            await reg.create_universe("u-dup", "owner", 9401, "emb", "v1")
    finally:
        await reg.close()


async def test_create_universe_distinct_keys_per_universe(tmp_path: Path):
    reg = await _make_registry(tmp_path)
    try:
        k1 = await reg.create_universe("u1", "o", 9500, "e", "v")
        k2 = await reg.create_universe("u2", "o", 9501, "e", "v")
        assert k1 != k2
        assert await reg.verify_api_key(k1) == "u1"
        assert await reg.verify_api_key(k2) == "u2"
    finally:
        await reg.close()


# ===========================================================================
# 7. verify_api_key — correct / wrong / revoked
# ===========================================================================

async def test_verify_api_key_wrong_returns_none(tmp_path: Path):
    reg = await _make_registry(tmp_path)
    try:
        await reg.create_universe("u1", "o", 9600, "e", "v")
        assert await reg.verify_api_key("totally-wrong-key") is None
    finally:
        await reg.close()


async def test_verify_api_key_revoked_returns_none(tmp_path: Path):
    from gaottt.multiverse.registry import hash_key

    reg = await _make_registry(tmp_path)
    try:
        plaintext = await reg.create_universe("u1", "o", 9700, "e", "v")
        kh = hash_key(plaintext)
        await reg.revoke_api_key(kh)
        assert await reg.verify_api_key(plaintext) is None
    finally:
        await reg.close()


# ===========================================================================
# 8. revoke_api_key
# ===========================================================================

async def test_revoke_api_key_only_affects_target(tmp_path: Path):
    from gaottt.multiverse.registry import hash_key

    reg = await _make_registry(tmp_path)
    try:
        k1 = await reg.create_universe("u1", "o", 9800, "e", "v")
        k2 = await reg.create_universe("u2", "o", 9801, "e", "v")
        await reg.revoke_api_key(hash_key(k1))
        assert await reg.verify_api_key(k1) is None
        assert await reg.verify_api_key(k2) == "u2"
    finally:
        await reg.close()


async def test_revoke_api_key_idempotent(tmp_path: Path):
    from gaottt.multiverse.registry import hash_key

    reg = await _make_registry(tmp_path)
    try:
        plaintext = await reg.create_universe("u1", "o", 9850, "e", "v")
        kh = hash_key(plaintext)
        await reg.revoke_api_key(kh)
        # revoking an already-revoked key must not error
        await reg.revoke_api_key(kh)
    finally:
        await reg.close()


# ===========================================================================
# 9. list_universes / get_universe
# ===========================================================================

async def test_list_universes_returns_all_rows(tmp_path: Path):
    reg = await _make_registry(tmp_path)
    try:
        await reg.create_universe("u1", "o1", 10000, "e", "v")
        await reg.create_universe("u2", "o2", 10001, "e", "v")
        rows = await reg.list_universes()
        ids = {r["universe_id"] for r in rows}
        assert ids == {"u1", "u2"}
    finally:
        await reg.close()


async def test_get_universe_missing_returns_none(tmp_path: Path):
    reg = await _make_registry(tmp_path)
    try:
        assert await reg.get_universe("nope") is None
    finally:
        await reg.close()


# ===========================================================================
# 10. delete_universe — status + key revocation
# ===========================================================================

async def test_delete_universe_marks_deleted_and_revokes_keys(tmp_path: Path):
    reg = await _make_registry(tmp_path)
    try:
        plaintext = await reg.create_universe("u1", "o", 10100, "e", "v")
        await reg.delete_universe("u1")
        row = await reg.get_universe("u1")
        assert row is not None  # row preserved for audit
        assert row["status"] == "deleted"
        assert await reg.verify_api_key(plaintext) is None  # key revoked
    finally:
        await reg.close()


async def test_delete_universe_missing_is_noop(tmp_path: Path):
    reg = await _make_registry(tmp_path)
    try:
        # deleting a non-existent universe must not error
        await reg.delete_universe("ghost")
    finally:
        await reg.close()


# ===========================================================================
# 11. reconcile — directory vs registry alignment
# ===========================================================================

async def test_reconcile_dir_without_registry_warns_and_skips(tmp_path: Path, caplog):
    universes = tmp_path / UNIVERSES_SUBDIR
    universes.mkdir()
    (universes / "stray-universe").mkdir()  # dir present, registry empty

    reg = await _make_registry(tmp_path)  # initialize() calls reconcile()
    try:
        with caplog.at_level(logging.WARNING):
            await reg.reconcile()

        rows = await reg.list_universes()
        assert rows == []  # supervisor never auto-adds (manual intervention)
        warned = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("stray-universe" in r.getMessage() for r in warned), (
            "expected a WARNING naming the stray universe dir"
        )
    finally:
        await reg.close()


async def test_reconcile_registry_active_without_dir_becomes_orphan(tmp_path: Path):
    universes = tmp_path / UNIVERSES_SUBDIR
    universes.mkdir()

    reg = await _make_registry(tmp_path)
    try:
        # create a universe (DB only; no filesystem dir created)
        await reg.create_universe("u1", "o", 10200, "e", "v")
        assert (await reg.get_universe("u1"))["status"] == "active"

        # no <universes>/u1 dir exists -> reconcile orphans it
        await reg.reconcile()
        assert (await reg.get_universe("u1"))["status"] == "orphan"
    finally:
        await reg.close()


async def test_reconcile_dir_present_with_registry_stays_active(tmp_path: Path):
    universes = tmp_path / UNIVERSES_SUBDIR
    universes.mkdir()

    reg = await _make_registry(tmp_path)
    try:
        await reg.create_universe("u1", "o", 10300, "e", "v")
        (universes / "u1").mkdir()  # filesystem agrees with registry
        await reg.reconcile()
        assert (await reg.get_universe("u1"))["status"] == "active"
    finally:
        await reg.close()


async def test_reconcile_ignores_trash_dir(tmp_path: Path, caplog):
    # trash is a sibling of universes/, not inside it; it must never be scanned.
    (tmp_path / TRASH_SUBDIR).mkdir()
    (tmp_path / TRASH_SUBDIR / "old-universe").mkdir()
    universes = tmp_path / UNIVERSES_SUBDIR
    universes.mkdir()

    reg = await _make_registry(tmp_path)
    try:
        with caplog.at_level(logging.WARNING):
            await reg.reconcile()
        warned = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not any("trash" in r.getMessage() or "old-universe" in r.getMessage()
                       for r in warned), \
            "trash/ contents must not trigger reconciliation warnings"
    finally:
        await reg.close()


async def test_reconcile_does_not_reorphan_deleted(tmp_path: Path):
    """A deleted universe must not be flipped to 'orphan' by reconcile."""
    universes = tmp_path / UNIVERSES_SUBDIR
    universes.mkdir()

    reg = await _make_registry(tmp_path)
    try:
        await reg.create_universe("u1", "o", 10400, "e", "v")
        await reg.delete_universe("u1")
        await reg.reconcile()
        assert (await reg.get_universe("u1"))["status"] == "deleted"
    finally:
        await reg.close()


# ===========================================================================
# 12. persistence across re-open
# ===========================================================================

async def test_registry_persists_across_reopen(tmp_path: Path):
    # open, create, close
    reg1 = await _make_registry(tmp_path)
    plaintext = await reg1.create_universe("u1", "o", 10500, "e", "v")
    await reg1.close()

    # reopen -> data survives
    reg2 = await _make_registry(tmp_path)
    try:
        assert await reg2.verify_api_key(plaintext) == "u1"
        assert (await reg2.get_universe("u1"))["port"] == 10500
    finally:
        await reg2.close()


# ===========================================================================
# 13. B1 — port uniqueness (partial unique index on live universes)
# ===========================================================================

async def test_duplicate_active_port_rejected(tmp_path: Path):
    """B1: the unique index rejects two live universes sharing a port
    (defense-in-depth behind the supervisor create_lock — the DB must refuse
    the loser even if the application-level allocate+insert races)."""
    reg = await _make_registry(tmp_path)
    try:
        await reg.create_universe("u1", "o", 10600, "emb", "v1")
        # a second ACTIVE universe on the same port must fail
        with pytest.raises(Exception):
            await reg.create_universe("u2", "o", 10600, "emb", "v1")
    finally:
        await reg.close()


async def test_deleted_universe_port_is_reusable(tmp_path: Path):
    """The port unique index is partial (``status != 'deleted'``): a deleted
    universe's row is retained for audit but its port stays reusable by a new
    active universe. A plain UNIQUE would break this contract."""
    reg = await _make_registry(tmp_path)
    try:
        await reg.create_universe("u1", "o", 10700, "emb", "v1")
        await reg.delete_universe("u1")
        plaintext = await reg.create_universe("u2", "o", 10700, "emb", "v1")
        assert plaintext
        assert (await reg.get_universe("u2"))["port"] == 10700
    finally:
        await reg.close()
