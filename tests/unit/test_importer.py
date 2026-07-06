"""Multiverse importer — pure-function unit tests (test-first / RED stage).

WP-1 contract: these tests assume ``gaottt/multiverse/importer.py`` exposes::

    COPY_FILENAMES: tuple[str, ...]   # 7 canonical copy targets

    @dataclass
    class FilePlan:
        copy_files: list[str]
        skipped: list[tuple[str, str]]
        total_bytes: int

    @dataclass
    class ImportPlan:
        universe_id: str
        source: Path
        target: Path
        owner_label: str
        port: int | None
        embedder_id: str
        embedder_version: str
        embedding_dim: int
        file_plan: FilePlan
        mode: str  # "copy" | "move"

    def compute_file_plan(source: Path) -> FilePlan
    def resolve_embedder_identity(
        source_manifest, config, override_id, override_version,
    ) -> tuple[str, str, int]
    def validate_universe_id(uid: str) -> bool
    def build_import_plan(
        source, owner_label, config, *, universe_id=None, move=False,
        embedder_id_override=None, embedder_version_override=None,
    ) -> ImportPlan
    def _verify_target_db(target_db: Path) -> None

To keep pytest collection intact (WP-1 learning: a module-top-level import of
an unimplemented module aborts collection for the whole suite), the
unimplemented module is imported **inside each test function**. Collection
succeeds; every test fails with ImportError until WP-2 creates the module.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from gaottt.config import GaOTTTConfig
from gaottt.store.manifest import UniverseManifest

# The 7 canonical copy targets (plan §2.4 / §2.3 step 6).
EXPECTED_COPY_FILENAMES = {
    "gaottt.db",
    "gaottt.db-shm",
    "gaottt.db-wal",
    "gaottt.faiss",
    "gaottt.faiss.ids",
    "gaottt.virtual.faiss",
    "gaottt.virtual.faiss.ids",
}

# Files that must be excluded from copy and recorded in ``skipped``.
SKIP_FILENAMES = [
    "gaottt.db.before-chat-reingest",
    "gaottt.faiss.bak",
    "gaottt.faiss.post-shutdown",
    "gaottt.db.broken-nuked",
    "gaottt.db.tmp",
    "manifest.json",
    "owner.lock",
    "backend.token",
    "registry.db",
    "memory.db",
]

UID_RE = re.compile(r"^[0-9a-f]{12}$")


# ---------------------------------------------------------------------------
# COPY_FILENAMES constant
# ---------------------------------------------------------------------------

def test_copy_filenames_constant():
    from gaottt.multiverse.importer import COPY_FILENAMES
    assert set(COPY_FILENAMES) == EXPECTED_COPY_FILENAMES
    assert len(COPY_FILENAMES) == 7


# ---------------------------------------------------------------------------
# dataclass shapes
# ---------------------------------------------------------------------------

def test_file_plan_dataclass_fields():
    from gaottt.multiverse.importer import FilePlan
    fields = {f.name for f in dataclasses.fields(FilePlan)}
    assert fields == {"copy_files", "skipped", "total_bytes"}


def test_file_plan_construction():
    from gaottt.multiverse.importer import FilePlan
    fp = FilePlan(copy_files=["gaottt.db"], skipped=[("x.bak", "backup")],
                  total_bytes=42)
    assert fp.copy_files == ["gaottt.db"]
    assert fp.skipped == [("x.bak", "backup")]
    assert fp.total_bytes == 42


def test_import_plan_dataclass_fields():
    from gaottt.multiverse.importer import ImportPlan
    fields = {f.name for f in dataclasses.fields(ImportPlan)}
    assert fields == {
        "universe_id", "source", "target", "owner_label", "port",
        "embedder_id", "embedder_version", "embedding_dim",
        "file_plan", "mode",
    }


# ---------------------------------------------------------------------------
# compute_file_plan
# ---------------------------------------------------------------------------

def test_compute_file_plan_all_seven_present(tmp_path):
    from gaottt.multiverse.importer import compute_file_plan
    for name in EXPECTED_COPY_FILENAMES:
        (tmp_path / name).write_bytes(b"x" * 100)
    plan = compute_file_plan(tmp_path)
    assert set(plan.copy_files) == EXPECTED_COPY_FILENAMES
    assert plan.skipped == []
    assert plan.total_bytes == 700  # 7 files × 100 bytes


def test_compute_file_plan_excludes_backups(tmp_path):
    from gaottt.multiverse.importer import compute_file_plan
    (tmp_path / "gaottt.db").write_bytes(b"db")
    (tmp_path / "gaottt.faiss").write_bytes(b"faiss")
    for name in SKIP_FILENAMES:
        (tmp_path / name).write_bytes(b"skip")
    plan = compute_file_plan(tmp_path)
    assert set(plan.copy_files) == {"gaottt.db", "gaottt.faiss"}
    skipped_names = {fname for fname, _ in plan.skipped}
    assert set(SKIP_FILENAMES).issubset(skipped_names)
    # Every skipped entry has a non-empty reason string.
    for _fname, reason in plan.skipped:
        assert isinstance(reason, str) and reason


def test_compute_file_plan_empty_dir(tmp_path):
    from gaottt.multiverse.importer import compute_file_plan
    plan = compute_file_plan(tmp_path)
    assert plan.copy_files == []
    assert plan.skipped == []
    assert plan.total_bytes == 0


def test_compute_file_plan_nonexistent_dir(tmp_path):
    from gaottt.multiverse.importer import compute_file_plan
    with pytest.raises((FileNotFoundError, NotADirectoryError)):
        compute_file_plan(tmp_path / "does-not-exist")


def test_compute_file_plan_missing_wal_shm(tmp_path):
    """Absence of -wal / -shm is normal (checkpoint completed)."""
    from gaottt.multiverse.importer import compute_file_plan
    (tmp_path / "gaottt.db").write_bytes(b"db")
    (tmp_path / "gaottt.faiss").write_bytes(b"f")
    (tmp_path / "gaottt.faiss.ids").write_bytes(b"i")
    plan = compute_file_plan(tmp_path)
    assert set(plan.copy_files) == {"gaottt.db", "gaottt.faiss",
                                    "gaottt.faiss.ids"}
    # b"db" (2) + b"f" (1) + b"i" (1) = 4 bytes.
    assert plan.total_bytes == 4


def test_compute_file_plan_total_bytes_accurate(tmp_path):
    from gaottt.multiverse.importer import compute_file_plan
    sizes = {"gaottt.db": 1000, "gaottt.faiss": 500, "gaottt.faiss.ids": 20}
    for name, sz in sizes.items():
        (tmp_path / name).write_bytes(b"x" * sz)
    plan = compute_file_plan(tmp_path)
    assert plan.total_bytes == sum(sizes.values())


# ---------------------------------------------------------------------------
# resolve_embedder_identity
# ---------------------------------------------------------------------------

def _make_manifest(embedder_id="manifest-model", embedder_version="manifest-v1",
                   dim=768):
    return UniverseManifest(
        universe_id="test",
        embedder_id=embedder_id,
        embedder_version=embedder_version,
        embedding_dim=dim,
        created_at=0.0,
    )


def test_resolve_embedder_identity_override_wins():
    from gaottt.multiverse.importer import resolve_embedder_identity
    config = GaOTTTConfig(model_name="config-model", embedding_dim=768)
    manifest = _make_manifest(embedder_id="manifest-model")
    eid, ever, dim = resolve_embedder_identity(
        manifest, config,
        override_id="override-model",
        override_version="override-v2",
    )
    assert eid == "override-model"
    assert ever == "override-v2"
    assert dim == 768


def test_resolve_embedder_identity_manifest_over_config():
    from gaottt.multiverse.importer import resolve_embedder_identity
    config = GaOTTTConfig(model_name="config-model", embedding_dim=768)
    manifest = _make_manifest(
        embedder_id="manifest-model", embedder_version="manifest-v1",
    )
    eid, ever, dim = resolve_embedder_identity(manifest, config, None, None)
    assert eid == "manifest-model"
    assert ever == "manifest-v1"
    assert dim == 768


def test_resolve_embedder_identity_config_fallback():
    from gaottt.multiverse.importer import resolve_embedder_identity
    config = GaOTTTConfig(model_name="config-model", embedding_dim=768)
    eid, ever, dim = resolve_embedder_identity(None, config, None, None)
    assert eid == "config-model"
    assert ever == "unpinned"
    assert dim == 768


def test_resolve_embedder_identity_dim_always_config():
    from gaottt.multiverse.importer import resolve_embedder_identity
    # Manifest says 512, config says 768 — dim must follow config (runtime
    # expectation, not a proof of source FAISS dimensionality).
    config = GaOTTTConfig(model_name="m", embedding_dim=768)
    manifest = _make_manifest(dim=512)
    _eid, _ever, dim = resolve_embedder_identity(manifest, config, None, None)
    assert dim == 768


# ---------------------------------------------------------------------------
# validate_universe_id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("uid", [
    "0123456789ab",
    "abcdef012345",
    "000000000000",
    "fedcba987654",
])
def test_validate_universe_id_valid(uid):
    from gaottt.multiverse.importer import validate_universe_id
    assert validate_universe_id(uid) is True


@pytest.mark.parametrize("uid", [
    "0123456789a",     # 11 chars
    "0123456789abc",   # 13 chars
    "ABCDEF012345",    # uppercase
    "gggggggggggg",    # non-hex
    "",                # empty
    "zzzzzzzzzzzz",    # non-hex
    "0123456789a!",    # special char
])
def test_validate_universe_id_invalid(uid):
    from gaottt.multiverse.importer import validate_universe_id
    assert validate_universe_id(uid) is False


# ---------------------------------------------------------------------------
# build_import_plan
# ---------------------------------------------------------------------------

def _seed_source(source: Path) -> None:
    """Write a minimal set of copy targets into ``source``."""
    source.mkdir(parents=True, exist_ok=True)
    (source / "gaottt.db").write_bytes(b"db")
    (source / "gaottt.faiss").write_bytes(b"faiss")


def test_build_import_plan_auto_uid(tmp_path):
    from gaottt.multiverse.importer import build_import_plan
    source = tmp_path / "source"
    _seed_source(source)
    config = GaOTTTConfig(
        multiverse_root=str(tmp_path / "mv"), embedding_dim=32,
    )
    plan = build_import_plan(source, "alice", config)
    assert UID_RE.match(plan.universe_id)
    assert plan.source == source
    expected_target = Path(config.multiverse_root) / "universes" / plan.universe_id
    assert plan.target == expected_target
    assert plan.owner_label == "alice"
    assert plan.port is None  # port is decided at execute time
    assert plan.mode == "copy"


def test_build_import_plan_explicit_uid(tmp_path):
    from gaottt.multiverse.importer import build_import_plan
    source = tmp_path / "source"
    _seed_source(source)
    config = GaOTTTConfig(
        multiverse_root=str(tmp_path / "mv"), embedding_dim=32,
    )
    uid = "aabbccddeeff"
    plan = build_import_plan(source, "bob", config, universe_id=uid)
    assert plan.universe_id == uid


def test_build_import_plan_invalid_uid_raises(tmp_path):
    from gaottt.multiverse.importer import build_import_plan
    source = tmp_path / "source"
    _seed_source(source)
    config = GaOTTTConfig(
        multiverse_root=str(tmp_path / "mv"), embedding_dim=32,
    )
    with pytest.raises((ValueError, SystemExit)):
        build_import_plan(source, "eve", config, universe_id="short")


def test_build_import_plan_uid_dup_target_dir_exists(tmp_path):
    from gaottt.multiverse.importer import build_import_plan
    source = tmp_path / "source"
    _seed_source(source)
    mv_root = tmp_path / "mv"
    config = GaOTTTConfig(multiverse_root=str(mv_root), embedding_dim=32)
    uid = "aabbccddeeff"
    # Pre-create the target directory to simulate a collision.
    target_dir = mv_root / "universes" / uid
    target_dir.mkdir(parents=True)
    with pytest.raises((ValueError, FileExistsError)):
        build_import_plan(source, "alice", config, universe_id=uid)


def test_build_import_plan_mode_copy(tmp_path):
    from gaottt.multiverse.importer import build_import_plan
    source = tmp_path / "source"
    _seed_source(source)
    config = GaOTTTConfig(
        multiverse_root=str(tmp_path / "mv"), embedding_dim=32,
    )
    plan = build_import_plan(source, "alice", config, move=False)
    assert plan.mode == "copy"


def test_build_import_plan_mode_move(tmp_path):
    from gaottt.multiverse.importer import build_import_plan
    source = tmp_path / "source"
    _seed_source(source)
    config = GaOTTTConfig(
        multiverse_root=str(tmp_path / "mv"), embedding_dim=32,
    )
    plan = build_import_plan(source, "alice", config, move=True)
    assert plan.mode == "move"


def test_build_import_plan_embedder_override(tmp_path):
    from gaottt.multiverse.importer import build_import_plan
    source = tmp_path / "source"
    _seed_source(source)
    config = GaOTTTConfig(
        multiverse_root=str(tmp_path / "mv"), embedding_dim=32,
    )
    plan = build_import_plan(
        source, "alice", config,
        embedder_id_override="custom-embedder",
        embedder_version_override="v9",
    )
    assert plan.embedder_id == "custom-embedder"
    assert plan.embedder_version == "v9"
    assert plan.embedding_dim == 32


def test_build_import_plan_file_plan_attached(tmp_path):
    from gaottt.multiverse.importer import build_import_plan, FilePlan
    source = tmp_path / "source"
    _seed_source(source)
    config = GaOTTTConfig(
        multiverse_root=str(tmp_path / "mv"), embedding_dim=32,
    )
    plan = build_import_plan(source, "alice", config)
    assert isinstance(plan.file_plan, FilePlan)
    assert "gaottt.db" in plan.file_plan.copy_files
    assert plan.file_plan.total_bytes > 0


# ---------------------------------------------------------------------------
# _verify_target_db
# ---------------------------------------------------------------------------

def test_verify_target_db_ok(tmp_path):
    """A freshly-created valid SQLite DB must pass integrity_check."""
    import sqlite3
    from gaottt.multiverse.importer import _verify_target_db
    db_path = tmp_path / "gaottt.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    # Must NOT raise.
    _verify_target_db(db_path)


def test_verify_target_db_corrupt_raises(tmp_path):
    from gaottt.multiverse.importer import _verify_target_db
    db_path = tmp_path / "gaottt.db"
    db_path.write_bytes(b"\x00" * 512)  # garbage, not a valid SQLite file
    with pytest.raises((RuntimeError, Exception)):
        _verify_target_db(db_path)


def test_verify_target_db_missing_raises(tmp_path):
    from gaottt.multiverse.importer import _verify_target_db
    db_path = tmp_path / "nonexistent.db"
    with pytest.raises((RuntimeError, FileNotFoundError, Exception)):
        _verify_target_db(db_path)


# ---------------------------------------------------------------------------
# Exception classes — IntegrityCheckFailed / UniverseAlreadyExistsError
# ---------------------------------------------------------------------------

def test_integrity_check_failed_is_runtime_error():
    """IntegrityCheckFailed must subclass RuntimeError so callers that catch
    RuntimeError still see it (backward compat with the pre-WP-4 contract)
    while the CLI can match the specific type for exit 8."""
    from gaottt.multiverse.importer import IntegrityCheckFailed
    assert issubclass(IntegrityCheckFailed, RuntimeError)


def test_universe_already_exists_is_runtime_error():
    """UniverseAlreadyExistsError must subclass RuntimeError for the same
    backward-compat reason, while letting the CLI map it to exit 2."""
    from gaottt.multiverse.importer import UniverseAlreadyExistsError
    assert issubclass(UniverseAlreadyExistsError, RuntimeError)


def test_verify_target_db_raises_integrity_check_failed(tmp_path):
    """Corrupt db → IntegrityCheckFailed (not generic RuntimeError).

    This pins the WP-4 contract: the CLI relies on the specific exception
    type to map to exit 8 instead of string-matching.
    """
    from gaottt.multiverse.importer import IntegrityCheckFailed, _verify_target_db
    db_path = tmp_path / "gaottt.db"
    db_path.write_bytes(b"\x00" * 512)  # garbage, not a valid SQLite file
    with pytest.raises(IntegrityCheckFailed):
        _verify_target_db(db_path)


def test_verify_target_db_missing_raises_integrity_check_failed(tmp_path):
    """Missing db after copy → IntegrityCheckFailed (not generic RuntimeError)."""
    from gaottt.multiverse.importer import IntegrityCheckFailed, _verify_target_db
    with pytest.raises(IntegrityCheckFailed):
        _verify_target_db(tmp_path / "does-not-exist.db")


def test_verify_target_db_empty_raises_integrity_check_failed(tmp_path):
    """0-byte file (schemaless) → IntegrityCheckFailed from the user-table
    gate, not a generic RuntimeError."""
    from gaottt.multiverse.importer import IntegrityCheckFailed, _verify_target_db
    db_path = tmp_path / "gaottt.db"
    db_path.write_bytes(b"")
    with pytest.raises(IntegrityCheckFailed):
        _verify_target_db(db_path)


def test_exceptions_exported_in_all():
    """Both exception classes must be in __all__ so they are part of the
    public API surface (importable via ``from gaottt.multiverse.importer
    import IntegrityCheckFailed``)."""
    from gaottt.multiverse import importer
    assert "IntegrityCheckFailed" in importer.__all__
    assert "UniverseAlreadyExistsError" in importer.__all__
