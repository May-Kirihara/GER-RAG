"""Filesystem snapshot primitive for cloning a managed universe."""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from gaottt.multiverse.importer import (
    IntegrityCheckFailed,
    _verify_target_db,
    compute_file_plan,
)
from gaottt.store.manifest import UniverseManifest, load_manifest, write_manifest


class CloneConflict(RuntimeError):
    """The source layout is not safe or valid to clone."""


class InsufficientCloneStorage(RuntimeError):
    """The target filesystem cannot hold a complete clone."""


@dataclass(frozen=True)
class CloneSnapshot:
    copied_files: tuple[str, ...]
    total_bytes: int
    manifest: UniverseManifest


def _checkpoint_source_db(source_db: Path) -> None:
    """Fold a stopped source's WAL into the main DB before immutable copy checks."""
    if not source_db.exists():
        return
    try:
        conn = sqlite3.connect(source_db, timeout=30.0)
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row is not None and row[0] != 0:
                raise CloneConflict(
                    f"source WAL checkpoint remained busy: {row!r}"
                )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise CloneConflict(f"source WAL checkpoint failed: {exc}") from exc


def clone_universe_files(
    source_dir: Path,
    target_dir: Path,
    target_universe_id: str,
) -> CloneSnapshot:
    """Copy one stopped managed universe into a fresh target directory.

    The caller owns source lifecycle locking. This function enforces the file
    allowlist, capacity gate, cleanup-on-failure, integrity check, and fresh
    manifest identity.
    """
    source = Path(source_dir)
    target = Path(target_dir)
    try:
        source_manifest = load_manifest(source)
    except (OSError, ValueError) as exc:
        raise CloneConflict(f"source manifest is unreadable: {exc}") from exc
    if source_manifest is None or not source_manifest.managed:
        raise CloneConflict("source must have a managed universe manifest")
    if source_manifest.universe_id != source.name:
        raise CloneConflict(
            "source manifest universe_id does not match its directory"
        )

    _checkpoint_source_db(source / "gaottt.db")
    file_plan = compute_file_plan(source)
    files = tuple(file_plan.copy_files)
    if "gaottt.db" not in files and any(name != "gaottt.db" for name in files):
        raise CloneConflict("source has index files but no gaottt.db")

    target_parent = target.parent
    target_parent.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(target_parent).free
    reserve = max(16 * 1024 * 1024, file_plan.total_bytes // 20)
    required = file_plan.total_bytes + reserve
    if free_bytes < required:
        raise InsufficientCloneStorage(
            f"clone requires {required} bytes including safety reserve; "
            f"only {free_bytes} bytes free"
        )

    target.mkdir(parents=False, exist_ok=False)
    os.chmod(target, 0o700)
    try:
        for name in files:
            shutil.copy2(source / name, target / name)

        if "gaottt.db" in files:
            _verify_target_db(target / "gaottt.db")

        manifest = UniverseManifest(
            universe_id=target_universe_id,
            embedder_id=source_manifest.embedder_id,
            embedder_version=source_manifest.embedder_version,
            embedding_dim=source_manifest.embedding_dim,
            created_at=time.time(),
            managed=True,
        )
        write_manifest(target, manifest)
        os.chmod(target / "manifest.json", 0o600)
        return CloneSnapshot(files, file_plan.total_bytes, manifest)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


__all__ = [
    "CloneConflict",
    "CloneSnapshot",
    "InsufficientCloneStorage",
    "IntegrityCheckFailed",
    "clone_universe_files",
]
