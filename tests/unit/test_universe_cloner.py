from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from gaottt.multiverse.cloner import (
    CloneConflict,
    InsufficientCloneStorage,
    clone_universe_files,
)
from gaottt.store.manifest import UniverseManifest, load_manifest, write_manifest


def _managed_source(tmp_path: Path, uid: str = "aaaaaaaaaaaa") -> Path:
    source = tmp_path / uid
    source.mkdir()
    write_manifest(
        source,
        UniverseManifest(
            universe_id=uid,
            embedder_id="test-embedder",
            embedder_version="v1",
            embedding_dim=8,
            created_at=1.0,
            managed=True,
        ),
    )
    return source


def _write_db(path: Path, value: str = "source-row") -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, content TEXT)")
    conn.execute("INSERT INTO nodes VALUES ('n1', ?)", (value,))
    conn.commit()
    conn.close()


def test_clone_copies_allowlist_and_regenerates_manifest(tmp_path: Path):
    source = _managed_source(tmp_path)
    _write_db(source / "gaottt.db")
    (source / "gaottt.faiss").write_bytes(b"faiss")
    (source / "backend.token").write_text("secret")
    (source / "owner.lock").write_text("lease")
    (source / "backup.bak").write_text("backup")

    target = tmp_path / "bbbbbbbbbbbb"
    result = clone_universe_files(source, target, "bbbbbbbbbbbb")

    assert set(result.copied_files) == {"gaottt.db", "gaottt.faiss"}
    assert (target / "gaottt.faiss").read_bytes() == b"faiss"
    assert not (target / "backend.token").exists()
    assert not (target / "owner.lock").exists()
    assert not (target / "backup.bak").exists()
    manifest = load_manifest(target)
    assert manifest is not None
    assert manifest.universe_id == "bbbbbbbbbbbb"
    assert manifest.embedder_id == "test-embedder"
    assert manifest.managed is True

    conn = sqlite3.connect(target / "gaottt.db")
    assert conn.execute("SELECT content FROM nodes").fetchone()[0] == "source-row"
    conn.close()


def test_clone_empty_never_started_universe(tmp_path: Path):
    source = _managed_source(tmp_path)
    target = tmp_path / "bbbbbbbbbbbb"

    result = clone_universe_files(source, target, "bbbbbbbbbbbb")

    assert result.copied_files == ()
    assert load_manifest(target).universe_id == "bbbbbbbbbbbb"


def test_clone_rejects_unmanaged_or_mismatched_manifest(tmp_path: Path):
    source = _managed_source(tmp_path)
    manifest = load_manifest(source)
    write_manifest(source, manifest.model_copy(update={"managed": False}))
    with pytest.raises(CloneConflict, match="managed"):
        clone_universe_files(source, tmp_path / "target-a", "bbbbbbbbbbbb")

    write_manifest(
        source,
        manifest.model_copy(update={"universe_id": "cccccccccccc"}),
    )
    with pytest.raises(CloneConflict, match="does not match"):
        clone_universe_files(source, tmp_path / "target-b", "bbbbbbbbbbbb")


def test_clone_rejects_indexes_without_db(tmp_path: Path):
    source = _managed_source(tmp_path)
    (source / "gaottt.faiss").write_bytes(b"orphan")
    with pytest.raises(CloneConflict, match="no gaottt.db"):
        clone_universe_files(source, tmp_path / "target", "bbbbbbbbbbbb")


def test_clone_capacity_gate_precedes_target_creation(tmp_path: Path):
    source = _managed_source(tmp_path)
    _write_db(source / "gaottt.db")
    usage = type("Usage", (), {"free": 0})()
    target = tmp_path / "target"
    with patch("gaottt.multiverse.cloner.shutil.disk_usage", return_value=usage):
        with pytest.raises(InsufficientCloneStorage):
            clone_universe_files(source, target, "bbbbbbbbbbbb")
    assert not target.exists()


def test_clone_integrity_failure_cleans_partial_target(tmp_path: Path):
    source = _managed_source(tmp_path)
    (source / "gaottt.db").write_bytes(b"not sqlite")
    target = tmp_path / "target"
    with pytest.raises(Exception, match="integrity_check|database"):
        clone_universe_files(source, target, "bbbbbbbbbbbb")
    assert not target.exists()
