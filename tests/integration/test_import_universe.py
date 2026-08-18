"""Multiverse importer — integration tests (test-first / RED stage).

Covers plan §4.2 + §4.3:

* **Group A — execute_import round-trip**: source fixture populated with a
  real engine → importer copies into multiverse → target engine.startup →
  recall.  Also covers copy/move, exception cleanup, retry, race, corrupted
  FAISS, empty DB, missing -wal/-shm.

* **Group B — CLI contract (subprocess)**: ``scripts/import_universe.py``
  exit codes for dry-run, uid format, non-TTY, WAL threshold, embedder
  service, embedder identity mismatch, disk capacity, source owner.lock.

* **Group C — @slow supervisor spawn e2e (§4.3)**: import a source DB →
  start supervisor → /route spawns backend → MCP recall returns original
  nodes.

All ``gaottt.multiverse.importer`` imports are deferred to keep collection
intact (WP-1 learning: module-top import of an unimplemented module aborts
collection).  The CLI tests invoke ``scripts/import_universe.py`` via
subprocess; collection succeeds and execution fails because the script does
not exist yet (WP-2).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.index.faiss_index import FaissIndex
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore

slow = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "import_universe.py"

CONTENT_MARKERS = [
    "hello world from the source universe",
    "another memory about gravity wells",
    "third note on test first development",
]

COPY_TARGETS = [
    "gaottt.db", "gaottt.db-shm", "gaottt.db-wal",
    "gaottt.faiss", "gaottt.faiss.ids",
    "gaottt.virtual.faiss", "gaottt.virtual.faiss.ids",
]

SKIP_NAMES = [
    "gaottt.db.bak",
    "gaottt.faiss.before-experiment",
    "manifest.json",
    "owner.lock",
    "backend.token",
    "registry.db",
    "memory.db",
]


# ---------------------------------------------------------------------------
# Stub embedder — keyword-overlap, deterministic, matches StubServiceEmbedder
# algorithm so the @slow test can use the same vectors via the HTTP service.
# ---------------------------------------------------------------------------

class StubEmbedder:
    """Deterministic md5-seeded keyword-overlap embedder.

    Same algorithm as ``StubServiceEmbedder`` in ``_supervisor_helpers`` but
    standalone so the source-fixture engine can use it without the HTTP
    service.  Constructed at ``dimension=32`` for fast non-@slow tests.
    """

    def __init__(self, dimension: int = 32, embedder_id: str = "stub-local"):
        self._dimension = dimension
        self._embedder_id = embedder_id
        self._token_cache: dict[str, np.ndarray] = {}

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def embedder_id(self) -> str:
        return self._embedder_id

    @property
    def embedder_version(self) -> str:
        return "stub-v0"

    def _token_vec(self, token: str) -> np.ndarray:
        cached = self._token_cache.get(token)
        if cached is not None:
            return cached
        seed = int.from_bytes(hashlib.md5(token.encode()).digest()[:8], "big")
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(self._dimension).astype(np.float32)
        v /= np.linalg.norm(v) + 1e-9
        self._token_cache[token] = v
        return v

    def _embed(self, text: str) -> np.ndarray:
        tokens = [t.lower() for t in text.split() if t.strip()]
        if not tokens:
            return np.zeros(self._dimension, dtype=np.float32)
        v = sum(self._token_vec(t) for t in tokens)
        norm = np.linalg.norm(v)
        return (v / norm).astype(np.float32) if norm > 0 else v.astype(np.float32)

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._embed(t) for t in texts])

    def encode_query(self, text: str) -> np.ndarray:
        return self._embed(text).reshape(1, -1)


# ---------------------------------------------------------------------------
# fixture helpers
# ---------------------------------------------------------------------------

async def _populate_source(
    source_dir: Path,
    markers: list[str] | None = None,
    *,
    dim: int = 32,
    model_name: str = "stub-local",
    add_backups: bool = False,
) -> list[str]:
    """Populate ``source_dir`` with a real engine + nodes, then shutdown.

    After shutdown the directory contains ``gaottt.db``, FAISS files, and a
    ``manifest.json`` (managed=False).  When ``add_backups`` is True, also
    writes ``.bak`` / ``.before-*`` / ``manifest.json`` / ``owner.lock`` etc.
    to exercise the skip-classification path.
    """
    markers = markers or CONTENT_MARKERS
    config = GaOTTTConfig(
        data_dir=str(source_dir),
        embedding_dim=dim,
        model_name=model_name,
        flush_interval_seconds=999.0,
        faiss_save_interval_seconds=0.0,
        virtual_faiss_save_interval_seconds=0.0,
        dream_enabled=False,
        hybrid_bm25_enabled=False,
    )
    embedder = StubEmbedder(dimension=dim, embedder_id=model_name)
    engine = GaOTTTEngine(
        config=config,
        embedder=embedder,
        faiss_index=FaissIndex(dimension=dim),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=config.db_path),
        virtual_faiss_index=FaissIndex(dimension=dim),
    )
    await engine.startup()
    ids = await engine.index_documents([
        {"content": m, "metadata": {"source": "user"}} for m in markers
    ])
    # Force-save FAISS so files land on disk before shutdown.
    engine.faiss_index.save(config.faiss_index_path)
    engine.virtual_faiss_index.save(config.virtual_faiss_index_path)
    await engine.cache.flush_to_store(engine.store)
    await engine.shutdown()

    if add_backups:
        for name in SKIP_NAMES:
            (source_dir / name).write_bytes(b"backup-residue")

    return ids


async def _make_registry(root: Path):
    from gaottt.multiverse.registry import MultiverseRegistry
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    reg = MultiverseRegistry(root)
    await reg.initialize()
    return reg


def _count_active_nodes(db_path: str) -> int:
    """Count non-archived nodes directly from SQLite (post-import sanity)."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE is_archived = 0"
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


def _run_cli(
    args: list[str],
    *,
    env: dict | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess:
    """Invoke ``scripts/import_universe.py`` via subprocess.

    Fails (non-zero returncode / FileNotFoundError) until WP-2 creates the
    script — this is the expected RED state for CLI-contract tests.
    """
    assert SCRIPT_PATH.exists(), (
        f"scripts/import_universe.py not yet implemented (WP-2). "
        f"Expected at {SCRIPT_PATH}"
    )
    full_env = os.environ.copy()
    # Isolate from the operator's production config file.
    full_env["GAOTTT_CONFIG_FILE"] = ""
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH)] + args,
        capture_output=True,
        text=True,
        env=full_env,
        timeout=timeout,
    )


# ===========================================================================
# GROUP A — execute_import round-trip (deferred import → ImportError RED)
# ===========================================================================

async def test_basic_round_trip(tmp_path):
    """Source → execute_import → target engine.startup → recall."""
    from gaottt.multiverse.importer import build_import_plan, execute_import

    source = tmp_path / "source"
    await _populate_source(source)

    mv_root = tmp_path / "mv"
    reg = await _make_registry(mv_root)
    try:
        config = GaOTTTConfig(
            multiverse_root=str(mv_root), embedding_dim=32,
        )
        plan = build_import_plan(source, "alice", config)
        await execute_import(plan, reg)

        # Open target with a fresh engine and recall.
        target_dir = mv_root / "universes" / plan.universe_id
        target_cfg = GaOTTTConfig(
            data_dir=str(target_dir), embedding_dim=32,
            flush_interval_seconds=999.0, dream_enabled=False,
            hybrid_bm25_enabled=False,
        )
        eng = GaOTTTEngine(
            config=target_cfg,
            embedder=StubEmbedder(dimension=32),
            faiss_index=FaissIndex(dimension=32),
            cache=CacheLayer(flush_interval=999.0),
            store=SqliteStore(db_path=target_cfg.db_path),
            virtual_faiss_index=FaissIndex(dimension=32),
        )
        await eng.startup()
        try:
            results = await eng.query(text="hello world", top_k=5)
            texts = [r.content for r in results]
            assert any("hello world" in t for t in texts), (
                f"original node not recalled; got {texts}"
            )
        finally:
            await eng.shutdown()
    finally:
        await reg.close()


async def test_copy_file_structure(tmp_path):
    """Target has gaottt.db / *.faiss / manifest.json (managed=True),
    no owner.lock, no backend.token, no .bak."""
    from gaottt.multiverse.importer import build_import_plan, execute_import

    source = tmp_path / "source"
    await _populate_source(source, add_backups=True)

    mv_root = tmp_path / "mv"
    reg = await _make_registry(mv_root)
    try:
        config = GaOTTTConfig(
            multiverse_root=str(mv_root), embedding_dim=32,
        )
        plan = build_import_plan(source, "alice", config)
        await execute_import(plan, reg)

        target = mv_root / "universes" / plan.universe_id
        # Must have the core data files.
        assert (target / "gaottt.db").exists()
        assert (target / "gaottt.faiss").exists()
        assert (target / "gaottt.faiss.ids").exists()
        # Manifest freshly written with managed=True.
        manifest = json.loads((target / "manifest.json").read_text())
        assert manifest["managed"] is True
        assert manifest["universe_id"] == plan.universe_id
        # Must NOT carry over operational / backup files.
        assert not (target / "owner.lock").exists()
        assert not (target / "backend.token").exists()
        assert not (target / "registry.db").exists()
        assert not any(target.glob("*.bak"))
        assert not any(target.glob("*.before-*"))
    finally:
        await reg.close()


async def test_registry_reflection(tmp_path):
    """list_universes includes the imported uid; verify_api_key passes."""
    from gaottt.multiverse.importer import build_import_plan, execute_import

    source = tmp_path / "source"
    await _populate_source(source)

    mv_root = tmp_path / "mv"
    reg = await _make_registry(mv_root)
    try:
        config = GaOTTTConfig(
            multiverse_root=str(mv_root), embedding_dim=32,
        )
        plan = build_import_plan(source, "alice", config)
        api_key = await execute_import(plan, reg)

        universes = await reg.list_universes()
        uids = {u["universe_id"] for u in universes}
        assert plan.universe_id in uids

        resolved = await reg.verify_api_key(api_key)
        assert resolved == plan.universe_id
    finally:
        await reg.close()


async def test_node_count_faiss_size_consistency(tmp_path):
    """Source and target have the same active node count and FAISS size."""
    from gaottt.multiverse.importer import build_import_plan, execute_import

    source = tmp_path / "source"
    await _populate_source(source)
    source_db = str(source / "gaottt.db")
    source_count = _count_active_nodes(source_db)
    source_faiss_size = (source / "gaottt.faiss").stat().st_size

    mv_root = tmp_path / "mv"
    reg = await _make_registry(mv_root)
    try:
        config = GaOTTTConfig(
            multiverse_root=str(mv_root), embedding_dim=32,
        )
        plan = build_import_plan(source, "alice", config)
        await execute_import(plan, reg)

        target = mv_root / "universes" / plan.universe_id
        target_count = _count_active_nodes(str(target / "gaottt.db"))
        target_faiss_size = (target / "gaottt.faiss").stat().st_size

        assert target_count == source_count
        assert target_faiss_size == source_faiss_size
    finally:
        await reg.close()


async def test_move_mode(tmp_path):
    """--move: source 7 files gone, target has them, backups remain."""
    from gaottt.multiverse.importer import build_import_plan, execute_import

    source = tmp_path / "source"
    await _populate_source(source, add_backups=True)

    mv_root = tmp_path / "mv"
    reg = await _make_registry(mv_root)
    try:
        config = GaOTTTConfig(
            multiverse_root=str(mv_root), embedding_dim=32,
        )
        plan = build_import_plan(source, "alice", config, move=True)
        await execute_import(plan, reg)

        target = mv_root / "universes" / plan.universe_id
        # Target has the core files.
        assert (target / "gaottt.db").exists()
        assert (target / "gaottt.faiss").exists()
        # Source's copy targets are gone (moved).
        assert not (source / "gaottt.db").exists()
        assert not (source / "gaottt.faiss").exists()
        # Backup files remain in source (not moved).
        assert (source / "gaottt.db.bak").exists()
        assert (source / "manifest.json").exists()  # old manifest remains
    finally:
        await reg.close()


async def test_dry_run_no_mutation(tmp_path):
    """--dry-run: no mutation to source, target, or registry."""
    from gaottt.multiverse.importer import build_import_plan

    source = tmp_path / "source"
    await _populate_source(source)

    mv_root = tmp_path / "mv"
    reg = await _make_registry(mv_root)
    try:
        config = GaOTTTConfig(
            multiverse_root=str(mv_root), embedding_dim=32,
        )
        plan = build_import_plan(source, "alice", config)

        # Dry-run: build_import_plan has no side effects.
        universes_before = await reg.list_universes()
        assert universes_before == []

        # No target directory created.
        target = mv_root / "universes" / plan.universe_id
        assert not target.exists()

        # Source unchanged.
        assert (source / "gaottt.db").exists()
    finally:
        await reg.close()


async def test_copy_exception_cleanup(tmp_path, monkeypatch):
    """Exception during copy → target dir cleaned, no registry INSERT."""
    from gaottt.multiverse.importer import build_import_plan, execute_import

    source = tmp_path / "source"
    await _populate_source(source)

    mv_root = tmp_path / "mv"
    reg = await _make_registry(mv_root)
    try:
        config = GaOTTTConfig(
            multiverse_root=str(mv_root), embedding_dim=32,
        )
        plan = build_import_plan(source, "alice", config)

        call_count = {"n": 0}
        orig_copy2 = shutil.copy2

        def failing_copy2(src, dst, *a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 3:
                raise OSError("simulated copy failure")
            return orig_copy2(src, dst, *a, **kw)

        monkeypatch.setattr(shutil, "copy2", failing_copy2)
        with pytest.raises(Exception):
            await execute_import(plan, reg)

        # Target directory must be cleaned up.
        target = mv_root / "universes" / plan.universe_id
        assert not target.exists(), "stray target dir after copy failure"

        # Registry must not contain the uid.
        universes = await reg.list_universes()
        assert plan.universe_id not in {u["universe_id"] for u in universes}
    finally:
        await reg.close()


async def test_disk_full_error(tmp_path, monkeypatch):
    """OSError(ENOSPC) during copy → user-friendly error, target cleaned."""
    import errno
    from gaottt.multiverse.importer import build_import_plan, execute_import

    source = tmp_path / "source"
    await _populate_source(source)

    mv_root = tmp_path / "mv"
    reg = await _make_registry(mv_root)
    try:
        config = GaOTTTConfig(
            multiverse_root=str(mv_root), embedding_dim=32,
        )
        plan = build_import_plan(source, "alice", config)

        def enospc_copy(src, dst, *a, **kw):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(shutil, "copy2", enospc_copy)
        with pytest.raises((OSError, RuntimeError, SystemExit)):
            await execute_import(plan, reg)

        target = mv_root / "universes" / plan.universe_id
        assert not target.exists()
    finally:
        await reg.close()


async def test_cross_filesystem_move_fallback(tmp_path, monkeypatch):
    """os.rename → OSError(EXDEV) → copy+unlink fallback succeeds."""
    import errno
    from gaottt.multiverse.importer import build_import_plan, execute_import

    source = tmp_path / "source"
    await _populate_source(source)

    mv_root = tmp_path / "mv"
    reg = await _make_registry(mv_root)
    try:
        config = GaOTTTConfig(
            multiverse_root=str(mv_root), embedding_dim=32,
        )
        plan = build_import_plan(source, "alice", config, move=True)

        def exdev_rename(old, new):
            # Force EXDEV so the importer falls back to copy+unlink.
            raise OSError(errno.EXDEV, "Cross-device link")

        # Patch os.rename but keep shutil.move working (it calls os.rename
        # internally then falls back; the importer should handle EXDEV).
        monkeypatch.setattr(os, "rename", exdev_rename)

        # execute_import must succeed despite the simulated EXDEV.  If the
        # implementation uses shutil.move directly it handles this; if it
        # uses os.rename it must catch EXDEV and copy+unlink.
        api_key = await execute_import(plan, reg)
        assert api_key  # got an API key back

        target = mv_root / "universes" / plan.universe_id
        assert (target / "gaottt.db").exists()
    finally:
        await reg.close()


async def test_post_copy_integrity_check_corrupt(tmp_path):
    """Corrupted source DB → execute_import raises IntegrityCheckFailed +
    cleanup (target dir gone, no registry INSERT).

    Codex final review T3: the pre-WP-4 version only called
    ``_verify_target_db`` directly. This version drives the full
    ``execute_import`` path so the cleanup contract and exception type are
    exercised end-to-end.
    """
    from gaottt.multiverse.importer import (
        IntegrityCheckFailed,
        build_import_plan,
        execute_import,
    )

    source = tmp_path / "source"
    await _populate_source(source)
    # Corrupt the source db after population so compute_file_plan still
    # picks it up but post-copy _verify_target_db rejects it.
    (source / "gaottt.db").write_bytes(b"corrupt data" * 100)

    mv_root = tmp_path / "mv"
    reg = await _make_registry(mv_root)
    try:
        config = GaOTTTConfig(
            multiverse_root=str(mv_root), embedding_dim=32,
        )
        plan = build_import_plan(source, "alice", config)
        with pytest.raises(IntegrityCheckFailed):
            await execute_import(plan, reg)

        # Codex final review T3: target dir must be cleaned up so reconcile
        # does not WARN about a stray unregistered dir.
        target = mv_root / "universes" / plan.universe_id
        assert not target.exists(), (
            "stray target dir after IntegrityCheckFailed (cleanup contract)"
        )
        # And no registry row must have been inserted.
        universes = await reg.list_universes()
        assert plan.universe_id not in {u["universe_id"] for u in universes}, (
            "registry row inserted despite IntegrityCheckFailed"
        )
    finally:
        await reg.close()


async def test_create_universe_integrity_error_retry(tmp_path, monkeypatch):
    """create_universe 1st IntegrityError, 2nd success → retry, no stray."""
    from gaottt.multiverse.importer import build_import_plan, execute_import

    source = tmp_path / "source"
    await _populate_source(source)

    mv_root = tmp_path / "mv"
    reg = await _make_registry(mv_root)
    try:
        config = GaOTTTConfig(
            multiverse_root=str(mv_root), embedding_dim=32,
        )
        plan = build_import_plan(source, "alice", config)

        call_count = {"n": 0}
        orig_create = reg.create_universe

        async def flaky_create(*a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                import aiosqlite
                raise aiosqlite.IntegrityError("simulated port race")
            return await orig_create(*a, **kw)

        monkeypatch.setattr(reg, "create_universe", flaky_create)
        api_key = await execute_import(plan, reg)
        assert api_key
        assert call_count["n"] >= 2  # retried at least once

        # Exactly one target dir (no stray from the failed attempt).
        uids_dir = mv_root / "universes"
        dirs = [d for d in uids_dir.iterdir() if d.is_dir()]
        assert len(dirs) == 1
    finally:
        await reg.close()


async def test_create_universe_retry_cap(tmp_path, monkeypatch):
    """Always IntegrityError → max retries → RuntimeError + cleanup."""
    import aiosqlite
    from gaottt.multiverse.importer import build_import_plan, execute_import

    source = tmp_path / "source"
    await _populate_source(source)

    mv_root = tmp_path / "mv"
    reg = await _make_registry(mv_root)
    try:
        config = GaOTTTConfig(
            multiverse_root=str(mv_root), embedding_dim=32,
        )
        plan = build_import_plan(source, "alice", config)

        async def always_fail(*a, **kw):
            raise aiosqlite.IntegrityError("persistent port race")

        monkeypatch.setattr(reg, "create_universe", always_fail)
        with pytest.raises((RuntimeError, Exception)):
            await execute_import(plan, reg)

        # No stray target dirs.
        uids_dir = mv_root / "universes"
        dirs = [d for d in uids_dir.iterdir() if d.is_dir()]
        assert dirs == []
    finally:
        await reg.close()


async def test_copy_success_then_create_failure_cleanup(tmp_path, monkeypatch):
    """Copy OK → create_universe generic exception → target cleaned."""
    from gaottt.multiverse.importer import build_import_plan, execute_import

    source = tmp_path / "source"
    await _populate_source(source)

    mv_root = tmp_path / "mv"
    reg = await _make_registry(mv_root)
    try:
        config = GaOTTTConfig(
            multiverse_root=str(mv_root), embedding_dim=32,
        )
        plan = build_import_plan(source, "alice", config)

        async def generic_fail(*a, **kw):
            raise RuntimeError("unexpected registry error")

        monkeypatch.setattr(reg, "create_universe", generic_fail)
        with pytest.raises(Exception):
            await execute_import(plan, reg)

        target = mv_root / "universes" / plan.universe_id
        assert not target.exists(), "stray target after create_universe failure"
    finally:
        await reg.close()


async def test_importer_supervisor_port_race(tmp_path, monkeypatch):
    """Concurrent supervisor grabs the same port → IntegrityError → retry
    with a different port → success (plan §4.2, acceptance #20).

    Simulated in-process: mock ``allocate_port`` to return a port that a
    concurrent supervisor already claimed, so ``create_universe`` raises
    IntegrityError on the partial UNIQUE INDEX.  The retry calls
    ``allocate_port`` again (returning a fresh port) and succeeds.
    """
    from gaottt.multiverse.importer import build_import_plan, execute_import

    source = tmp_path / "source"
    await _populate_source(source)

    mv_root = tmp_path / "mv"
    reg = await _make_registry(mv_root)
    try:
        config = GaOTTTConfig(
            multiverse_root=str(mv_root), embedding_dim=32,
            universe_port_range_start=7890,
            universe_port_range_end=7895,
        )
        plan = build_import_plan(source, "alice", config)

        # Pre-claim port 7890 as if a concurrent supervisor just inserted it.
        await reg.create_universe(
            "deadbeef0001", "supervisor", 7890, "stub-local", "stub-v0",
        )

        port_seq = iter([7890, 7891])  # 1st collides, 2nd is free

        async def mock_allocate(start, end):
            return next(port_seq)

        monkeypatch.setattr(reg, "allocate_port", mock_allocate)

        # execute_import: 1st attempt gets port 7890 → create_universe
        # IntegrityError (partial UNIQUE idx) → cleanup + retry → 2nd attempt
        # gets port 7891 → success.
        api_key = await execute_import(plan, reg)
        assert api_key

        row = await reg.get_universe(plan.universe_id)
        assert row is not None
        assert row["port"] == 7891  # retried with a different port
    finally:
        await reg.close()


async def test_corrupted_faiss_detected_by_engine(tmp_path):
    """Corrupted source FAISS → importer copies it → engine.startup detects.

    The importer itself just copies bytes; corruption detection is the
    engine's job at target startup (FAISS read raises).

    Codex final review T4: the pre-WP-4 version accepted both raise and
    succeed paths, so it did not strongly prove detection. This version
    asserts startup raises RuntimeError — the faiss.read_index failure on
    a corrupt index file is the detection contract.
    """
    from gaottt.multiverse.importer import build_import_plan, execute_import

    source = tmp_path / "source"
    await _populate_source(source)
    # Corrupt the FAISS index file with non-zero garbage so faiss.read_index
    # raises (0-byte files are silently skipped by FaissIndex.load).
    faiss_path = source / "gaottt.faiss"
    faiss_path.write_bytes(b"CORRUPTED_FAISS_INDEX" * 100)

    mv_root = tmp_path / "mv"
    reg = await _make_registry(mv_root)
    try:
        config = GaOTTTConfig(
            multiverse_root=str(mv_root), embedding_dim=32,
        )
        plan = build_import_plan(source, "alice", config)
        await execute_import(plan, reg)

        target = mv_root / "universes" / plan.universe_id
        # The corrupted FAISS was copied as-is.
        assert (target / "gaottt.faiss").exists()
        target_faiss = (target / "gaottt.faiss").read_bytes()
        assert b"CORRUPTED" in target_faiss
        # engine.startup on the target must raise — the corrupt index file
        # is non-zero so FaissIndex.load calls faiss.read_index which
        # rejects the unknown index type.
        target_cfg = GaOTTTConfig(
            data_dir=str(target), embedding_dim=32,
            flush_interval_seconds=999.0, dream_enabled=False,
            hybrid_bm25_enabled=False,
        )
        eng = GaOTTTEngine(
            config=target_cfg,
            embedder=StubEmbedder(dimension=32),
            faiss_index=FaissIndex(dimension=32),
            cache=CacheLayer(flush_interval=999.0),
            store=SqliteStore(db_path=target_cfg.db_path),
        )
        with pytest.raises(RuntimeError):
            await eng.startup()
    finally:
        await reg.close()


async def test_empty_source_db_detected(tmp_path):
    """Empty (schema-less) gaottt.db → integrity_check or engine.startup
    detects."""
    from gaottt.multiverse.importer import _verify_target_db

    source = tmp_path / "source"
    source.mkdir()
    # Write an empty file (not a valid SQLite DB at all).
    (source / "gaottt.db").write_bytes(b"")
    (source / "gaottt.faiss").write_bytes(b"")

    # _verify_target_db on an empty/garbage db must raise.
    with pytest.raises(Exception):
        _verify_target_db(source / "gaottt.db")


async def test_missing_wal_shm_normal(tmp_path):
    """Absence of -wal / -shm is normal (checkpoint completed). Import
    proceeds without them."""
    from gaottt.multiverse.importer import build_import_plan, execute_import

    source = tmp_path / "source"
    await _populate_source(source)
    # Ensure no -wal / -shm (simulate a clean checkpoint).
    for suffix in ("-wal", "-shm"):
        wal = source / f"gaottt.db{suffix}"
        if wal.exists():
            wal.unlink()

    mv_root = tmp_path / "mv"
    reg = await _make_registry(mv_root)
    try:
        config = GaOTTTConfig(
            multiverse_root=str(mv_root), embedding_dim=32,
        )
        plan = build_import_plan(source, "alice", config)
        api_key = await execute_import(plan, reg)
        assert api_key

        target = mv_root / "universes" / plan.universe_id
        assert (target / "gaottt.db").exists()
        assert not (target / "gaottt.db-wal").exists()
        assert not (target / "gaottt.db-shm").exists()
    finally:
        await reg.close()


# ===========================================================================
# GROUP B — CLI contract (subprocess → script-not-found RED)
# ===========================================================================

def test_cli_dry_run_no_side_effects(tmp_path):
    """--dry-run prints plan, creates nothing."""
    source = tmp_path / "source"
    asyncio.run(_populate_source(source))
    mv_root = tmp_path / "mv"

    result = _run_cli([
        "--source", str(source),
        "--owner-label", "alice",
        "--multiverse-root", str(mv_root),
        "--dry-run",
        "--yes",
    ])
    assert result.returncode == 0, result.stderr
    # No target dir created.
    assert not mv_root.exists() or not any((mv_root / "universes").iterdir()) \
        if (mv_root / "universes").exists() else True
    # Source unchanged.
    assert (source / "gaottt.db").exists()


def test_cli_dry_run_strict(tmp_path):
    """After dry-run: no directory / file / registry row under tmp_path
    beyond the source fixture."""
    source = tmp_path / "source"
    asyncio.run(_populate_source(source))
    mv_root = tmp_path / "mv"

    source_files_before = set(source.iterdir())
    result = _run_cli([
        "--source", str(source),
        "--owner-label", "alice",
        "--multiverse-root", str(mv_root),
        "--dry-run",
        "--yes",
    ])
    assert result.returncode == 0, result.stderr
    # No multiverse root created at all.
    assert not mv_root.exists()
    # Source is bit-identical.
    assert set(source.iterdir()) == source_files_before


def test_cli_uid_format_short_exit_2(tmp_path):
    """--universe-id with invalid format → exit 2."""
    source = tmp_path / "source"
    asyncio.run(_populate_source(source))
    result = _run_cli([
        "--source", str(source),
        "--owner-label", "alice",
        "--multiverse-root", str(tmp_path / "mv"),
        "--universe-id", "abc",
        "--yes",
    ])
    assert result.returncode == 2, result.stderr


def test_cli_uid_format_non_hex_exit_2(tmp_path):
    """--universe-id with non-hex chars → exit 2."""
    source = tmp_path / "source"
    asyncio.run(_populate_source(source))
    result = _run_cli([
        "--source", str(source),
        "--owner-label", "alice",
        "--multiverse-root", str(tmp_path / "mv"),
        "--universe-id", "gggggggggggg",
        "--yes",
    ])
    assert result.returncode == 2, result.stderr


def test_cli_non_tty_no_yes_exit_2(tmp_path):
    """Subprocess has no TTY; without --yes the importer must exit 2."""
    source = tmp_path / "source"
    asyncio.run(_populate_source(source))
    result = _run_cli([
        "--source", str(source),
        "--owner-label", "alice",
        "--multiverse-root", str(tmp_path / "mv"),
        # NO --yes, NO --dry-run
    ])
    assert result.returncode == 2, result.stderr


def test_cli_embedder_service_down_exit_4(tmp_path):
    """embedder_endpoint set + service unreachable → exit 4."""
    source = tmp_path / "source"
    asyncio.run(_populate_source(source))
    # Point at a dead port (nothing listening).
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()  # release so nothing is listening

    result = _run_cli([
        "--source", str(source),
        "--owner-label", "alice",
        "--multiverse-root", str(tmp_path / "mv"),
        "--yes",
    ], env={
        "GAOTTT_EMBEDDER_ENDPOINT": f"http://127.0.0.1:{dead_port}",
    })
    assert result.returncode == 4, result.stderr


def test_cli_wal_over_256mb_exit_5(tmp_path):
    """WAL > 256MB → hard reject exit 5."""
    source = tmp_path / "source"
    asyncio.run(_populate_source(source))
    # Create a sparse WAL file > 256MB.
    wal_path = source / "gaottt.db-wal"
    with open(wal_path, "wb") as f:
        f.truncate(257 * 1024 * 1024)

    result = _run_cli([
        "--source", str(source),
        "--owner-label", "alice",
        "--multiverse-root", str(tmp_path / "mv"),
        "--yes",
    ])
    assert result.returncode == 5, result.stderr


def test_cli_wal_over_64mb_with_yes_continues(tmp_path):
    """WAL > 64MB + --yes → WARNING but continues (exit 0)."""
    source = tmp_path / "source"
    asyncio.run(_populate_source(source))
    wal_path = source / "gaottt.db-wal"
    with open(wal_path, "wb") as f:
        f.truncate(65 * 1024 * 1024)

    result = _run_cli([
        "--source", str(source),
        "--owner-label", "alice",
        "--multiverse-root", str(tmp_path / "mv"),
        "--yes",
    ])
    assert result.returncode == 0, result.stderr


def test_cli_disk_capacity_exit_6(tmp_path):
    """Disk insufficient → exit 6 + capacity message."""
    source = tmp_path / "source"
    asyncio.run(_populate_source(source))
    # Create a very large sparse source file to simulate insufficient space
    # on the target filesystem.  The importer's disk check should reject.
    huge = source / "gaottt.faiss"
    with open(huge, "wb") as f:
        f.truncate(500 * 1024 * 1024 * 1024)  # 500GB sparse

    result = _run_cli([
        "--source", str(source),
        "--owner-label", "alice",
        "--multiverse-root", str(tmp_path / "mv"),
        "--yes",
    ])
    assert result.returncode == 6, result.stderr


def test_cli_source_active_owner_lock_exit_3(tmp_path):
    """Active owner.lock in source + no --force → exit 3."""
    source = tmp_path / "source"
    asyncio.run(_populate_source(source))
    # Write an active owner.lock (heartbeat = now).
    lock_data = {
        "owner_id": "foreign-process-abc",
        "pid": 999999,
        "hostname": "other-host",
        "started_at": time.time(),
        "heartbeat_at": time.time(),
        "takeover_count": 0,
    }
    (source / "owner.lock").write_text(json.dumps(lock_data))

    result = _run_cli([
        "--source", str(source),
        "--owner-label", "alice",
        "--multiverse-root", str(tmp_path / "mv"),
        "--yes",
    ])
    assert result.returncode == 3, result.stderr


def test_cli_source_stale_owner_lock_warning(tmp_path):
    """Stale owner.lock → WARNING only (takeover possible), exit 0."""
    source = tmp_path / "source"
    asyncio.run(_populate_source(source))
    # Write a stale owner.lock (heartbeat well past lease_stale_seconds=60).
    lock_data = {
        "owner_id": "dead-process-xyz",
        "pid": 999998,
        "hostname": "dead-host",
        "started_at": time.time() - 7200,
        "heartbeat_at": time.time() - 3600,  # 1 hour ago
        "takeover_count": 0,
    }
    (source / "owner.lock").write_text(json.dumps(lock_data))

    result = _run_cli([
        "--source", str(source),
        "--owner-label", "alice",
        "--multiverse-root", str(tmp_path / "mv"),
        "--yes",
    ])
    # Stale lock = WARNING only, import proceeds.
    assert result.returncode == 0, result.stderr


def test_cli_force_overrides_active_owner_lock(tmp_path):
    """Active owner.lock + --force → WARNING + continues (exit 0)."""
    source = tmp_path / "source"
    asyncio.run(_populate_source(source))
    lock_data = {
        "owner_id": "foreign-process-def",
        "pid": 999997,
        "hostname": "other-host",
        "started_at": time.time(),
        "heartbeat_at": time.time(),
        "takeover_count": 0,
    }
    (source / "owner.lock").write_text(json.dumps(lock_data))

    result = _run_cli([
        "--source", str(source),
        "--owner-label", "alice",
        "--multiverse-root", str(tmp_path / "mv"),
        "--force",
        "--yes",
    ])
    assert result.returncode == 0, result.stderr


def test_cli_corrupt_db_exit_8(tmp_path):
    """Corrupt source gaottt.db → post-copy integrity_check fails → exit 8.

    Codex final review T2 / issue #3: the pre-WP-4 _verify_target_db raised
    generic RuntimeError, which main() mapped to exit 7 — but docs promised
    exit 8. This test pins the IntegrityCheckFailed → exit 8 contract and
    verifies the cleanup (no stray target dir, no registry row).
    """
    source = tmp_path / "source"
    asyncio.run(_populate_source(source))
    # Corrupt the source db: write garbage that is non-empty (so the
    # schema-gate fires, not the 0-byte guard).
    (source / "gaottt.db").write_bytes(b"corrupt data" * 100)

    mv_root = tmp_path / "mv"
    result = _run_cli([
        "--source", str(source),
        "--owner-label", "alice",
        "--multiverse-root", str(mv_root),
        "--yes",
    ])
    assert result.returncode == 8, (
        f"expected exit 8 for corrupt db, got {result.returncode}; "
        f"stderr={result.stderr}"
    )
    # Target dir must be cleaned up (no stray unregistered universe).
    universes_dir = mv_root / "universes"
    if universes_dir.exists():
        dirs = [d for d in universes_dir.iterdir() if d.is_dir()]
        assert dirs == [], (
            f"stray target dir after exit 8: {dirs}"
        )
    # And no registry row must have been inserted.
    registry_db = mv_root / "registry.db"
    if registry_db.exists():
        import sqlite3
        conn = sqlite3.connect(str(registry_db))
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM universes WHERE status = 'active'"
            )
            assert cur.fetchone()[0] == 0, (
                "registry row inserted despite exit 8"
            )
        finally:
            conn.close()


def test_cli_uid_collision_exit_2(tmp_path):
    """--universe-id that already exists in registry → exit 2.

    Codex final review #5: the pre-WP-4 execute_import would retry the
    PRIMARY KEY IntegrityError as a port race (exit 9). With the
    UniverseAlreadyExistsError preflight, the CLI maps it to exit 2
    (argument error, same as validate_universe_id rejection).
    """
    from gaottt.multiverse.importer import (
        build_import_plan,
        execute_import,
    )

    source = tmp_path / "source"
    asyncio.run(_populate_source(source))

    mv_root = tmp_path / "mv"

    # First import succeeds and occupies the uid.
    async def _first_import():
        from gaottt.multiverse.registry import MultiverseRegistry
        mv_root.mkdir(parents=True, exist_ok=True)
        os.chmod(mv_root, 0o700)
        reg = MultiverseRegistry(mv_root)
        await reg.initialize()
        try:
            config = GaOTTTConfig(
                multiverse_root=str(mv_root), embedding_dim=32,
            )
            plan = build_import_plan(
                source, "alice", config, universe_id="aabbccddeeff",
            )
            await execute_import(plan, reg)
        finally:
            await reg.close()

    asyncio.run(_first_import())

    # Second CLI invocation with the same uid → exit 2.
    result = _run_cli([
        "--source", str(source),
        "--owner-label", "bob",
        "--multiverse-root", str(mv_root),
        "--universe-id", "aabbccddeeff",
        "--yes",
    ])
    assert result.returncode == 2, (
        f"expected exit 2 for uid collision, got {result.returncode}; "
        f"stderr={result.stderr}"
    )


def test_execute_import_uid_preflight_raises(tmp_path):
    """execute_import raises UniverseAlreadyExistsError before mutation when
    the uid is already registered (Codex final review #5).

    Unit-level check (no subprocess): verifies the preflight fires before
    any file copy, so move mode would not relocate files into a doomed
    target.
    """
    from gaottt.multiverse.importer import (
        UniverseAlreadyExistsError,
        build_import_plan,
        execute_import,
    )
    from gaottt.multiverse.registry import MultiverseRegistry

    source = tmp_path / "source"
    asyncio.run(_populate_source(source))

    mv_root = tmp_path / "mv"
    mv_root.mkdir(parents=True, exist_ok=True)
    os.chmod(mv_root, 0o700)
    reg = MultiverseRegistry(mv_root)
    asyncio.run(reg.initialize())

    async def _run():
        try:
            config = GaOTTTConfig(
                multiverse_root=str(mv_root), embedding_dim=32,
            )
            # Pre-register the uid so the second import hits the preflight.
            await reg.create_universe(
                "aabbccddeeff", "pre-existing", 7890,
                "stub-local", "stub-v0",
            )
            plan = build_import_plan(
                source, "alice", config, universe_id="aabbccddeeff",
            )
            # The target dir does not exist (different uid from the one
            # create_universe just registered, but same uid as plan).
            # build_import_plan only checks target dir existence, so this
            # proceeds to execute_import where the registry preflight fires.
            with pytest.raises(UniverseAlreadyExistsError):
                await execute_import(plan, reg)
        finally:
            await reg.close()

    asyncio.run(_run())


def test_execute_import_custom_port_range(tmp_path):
    """execute_import honors port_range kwarg (Codex final review #1).

    Verifies the allocated port falls inside the supplied range — the
    @slow supervisor spawn test also asserts this, but this unit-level
    test runs without @slow and without a real supervisor.
    """
    from gaottt.multiverse.importer import build_import_plan, execute_import

    source = tmp_path / "source"
    asyncio.run(_populate_source(source))

    mv_root = tmp_path / "mv"

    async def _run():
        from gaottt.multiverse.registry import MultiverseRegistry
        mv_root.mkdir(parents=True, exist_ok=True)
        os.chmod(mv_root, 0o700)
        reg = MultiverseRegistry(mv_root)
        await reg.initialize()
        try:
            config = GaOTTTConfig(
                multiverse_root=str(mv_root), embedding_dim=32,
            )
            plan = build_import_plan(source, "alice", config)
            await execute_import(
                plan, reg, port_range=(9000, 9020),
            )
            row = await reg.get_universe(plan.universe_id)
            assert row is not None
            assert 9000 <= row["port"] <= 9020, (
                f"port {row['port']} outside custom range 9000-9020"
            )
        finally:
            await reg.close()

    asyncio.run(_run())


def test_execute_import_default_port_range(tmp_path):
    """execute_import without port_range kwarg uses the 7890-7989 default
    (backward compat with the pre-WP-4 signature)."""
    from gaottt.multiverse.importer import build_import_plan, execute_import

    source = tmp_path / "source"
    asyncio.run(_populate_source(source))

    mv_root = tmp_path / "mv"

    async def _run():
        from gaottt.multiverse.registry import MultiverseRegistry
        mv_root.mkdir(parents=True, exist_ok=True)
        os.chmod(mv_root, 0o700)
        reg = MultiverseRegistry(mv_root)
        await reg.initialize()
        try:
            config = GaOTTTConfig(
                multiverse_root=str(mv_root), embedding_dim=32,
            )
            plan = build_import_plan(source, "alice", config)
            await execute_import(plan, reg)  # no port_range → default
            row = await reg.get_universe(plan.universe_id)
            assert row is not None
            assert 7890 <= row["port"] <= 7989, (
                f"port {row['port']} outside default range 7890-7989"
            )
        finally:
            await reg.close()

    asyncio.run(_run())


# ===========================================================================
# GROUP C — @slow supervisor spawn e2e (plan §4.3, B1 対応)
# ===========================================================================

@slow
@pytest.mark.timeout(90)
async def test_imported_universe_supervisor_spawn(tmp_path):
    """Full e2e: populate source → import → supervisor → /route → MCP recall.

    This is the most important test (QA B1): it proves the imported universe
    can actually serve traffic through the supervisor spawn path BEFORE the
    760MB production DB hits it.  Uses ``StubServiceEmbedder`` (dim=768) so
    the spawned backend's RemoteEmbedder + manifest gate both pass.
    """
    from gaottt.multiverse.importer import build_import_plan, execute_import
    from tests.integration._supervisor_helpers import (
        StubServiceEmbedder,
        make_config,
        make_supervisor,
        asgi_client,
        route_universe,
        mcp_call,
        start_uvicorn,
        stop_uvicorn,
    )
    from gaottt.embedding.service import create_app

    # --- 1. Start the embedder HTTP service (shared by spawned backends) ---
    service = StubServiceEmbedder(dimension=768, embedder_id="stub-service")
    app = create_app(service)
    server, thread, embedder_port = start_uvicorn(app)
    embedder_url = f"http://127.0.0.1:{embedder_port}"
    try:
        # --- 2. Populate source with StubServiceEmbedder-compatible vectors ---
        source = tmp_path / "source"
        marker = "hello-world-marker-from-import-source"
        source_ids = await _populate_source(
            source,
            markers=[marker],
            dim=768,
            model_name="stub-service",
        )
        assert source_ids

        # --- 3. Import source into multiverse ---
        mv_root = tmp_path / "mv"
        mv_root.mkdir(parents=True, exist_ok=True)
        os.chmod(mv_root, 0o700)
        (mv_root / "universes").mkdir(parents=True, exist_ok=True)
        (mv_root / "trash").mkdir(parents=True, exist_ok=True)

        reg = await _make_registry(mv_root)
        try:
            import_config = GaOTTTConfig(
                multiverse_root=str(mv_root),
                embedding_dim=768,
                model_name="stub-service",
                embedder_endpoint=embedder_url,
                universe_port_range_start=8200,
                universe_port_range_end=8220,
            )
            plan = build_import_plan(
                source, "import-owner", import_config,
                embedder_id_override="stub-service",
                embedder_version_override="stub-v0",
            )
            api_key = await execute_import(
                plan, reg,
                port_range=(
                    import_config.universe_port_range_start,
                    import_config.universe_port_range_end,
                ),
            )

            # Codex final review T1: the registry port must fall inside the
            # configured range. This catches the hardcoded-7890 regression.
            registry_row = await reg.get_universe(plan.universe_id)
            assert registry_row is not None, "registry row missing after import"
            allocated_port = registry_row["port"]
            assert 8200 <= allocated_port <= 8220, (
                f"allocated port {allocated_port} outside the configured "
                f"range 8200-8220 (hardcoded-7890 regression)"
            )

            # --- 4. Start supervisor over the imported multiverse ---
            sup_config = make_config(
                mv_root, embedder_url,
                port_range=(8200, 8220),
            )
            sup_app, sup_reg = await make_supervisor(sup_config)
            try:
                async with asgi_client(sup_app) as client:
                    # --- 5. /route spawns the backend ---
                    routed = await route_universe(client, api_key)
                    assert routed["url"]
                    assert routed["token"]

                    # --- 6. MCP recall returns the original node ---
                    def _tool_text(result) -> str:
                        chunks = []
                        for block in getattr(result, "content", []) or []:
                            text = getattr(block, "text", None)
                            if text:
                                chunks.append(text)
                        return "\n".join(chunks)

                    recalled = _tool_text(await mcp_call(
                        routed["url"], routed["token"], "recall",
                        {"query": "hello-world-marker", "top_k": 5},
                    ))
                    assert marker in recalled, (
                        f"original node not recalled via spawned backend; "
                        f"got: {recalled[:500]}"
                    )
            finally:
                await sup_reg.close()
        finally:
            await reg.close()
    finally:
        stop_uvicorn(server, thread)
