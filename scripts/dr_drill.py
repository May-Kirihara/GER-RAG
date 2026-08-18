#!/usr/bin/env python3
"""MV5 WP-3 — Disaster-recovery drill for a standalone universe.

Proves the core DR claim of MV5:

    A standalone (managed=False) universe can be recovered to a working
    engine — FAISS rebuilt, startup diagnostics green, deterministic top-1
    recall preserved — from the 2-point backup set { gaottt.db,
    manifest.json } plus the matching embedder artifact.

The drill is **litestream-binary-independent by default**: backup/restore
is plain file copy. ``--with-litestream`` is a **best-effort exercise of
the litestream binary** when it is on PATH: failures (binary absent or
non-zero exit) are logged at ERROR but do NOT fail the drill, because the
default raw-copy path is the authoritative proof of the DR claim. A
strict WAL-restore e2e with a real litestream binary is a manual
pre-production check (Operations-Backup-Multiverse.md §商用導入前チェックリスト).

Scope boundary (Codex review B3): this drill covers **standalone universe
recovery only**. Managed-universe recovery (lease takeover, backend token
regeneration, registry row, control-plane re-sync) is out of scope and is
documented in Operations-Backup-Multiverse.md as a manual runbook.

What the drill does, in order (fail-fast):
  1. Create a standalone universe dir + manifest (managed=False).
  2. Build an engine against it (via the embedding service), startup,
     index a few documents, run a query, remember the deterministic top-1.
     Shutdown.
  3. Backup: copy gaottt.db AND manifest.json to a backup dir (the 2-point
     set). With --with-litestream + binary present, also take a litestream
     snapshot to a tmp replica.
  4. Destroy the universe dir.
  5. Restore: recreate the dir, copy both files back.
  6. FAISS rebuild: ``scripts/rebuild_faiss_from_db.py --apply`` then
     ``--check`` via subprocess, pointing GAOTTT_DATA_DIR + the embedder
     endpoint at the stub service so re-embedding is deterministic.
  7. Startup diagnostics: build engine again, startup, run
     run_startup_checks — assert no ERRORs. Assert the top-1 query from
     step 2 still matches (determinism fence, assumption 2).
  8. Cleanup, return 0.

Usage::

    .venv/bin/python scripts/dr_drill.py
    .venv/bin/python scripts/dr_drill.py --root /tmp/my-drill --with-litestream
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

# The drill drives a real (but stub-backed) embedding service so that
# build_engine + rebuild_faiss_from_db both use the SAME deterministic
# embedder. StubServiceEmbedder is the same algorithm the supervisor
# integration tests use (md5-seeded per-token unit vectors).
from tests.integration._supervisor_helpers import StubServiceEmbedder, start_uvicorn

REPO_ROOT = Path(__file__).resolve().parent.parent
REBUILD_SCRIPT = REPO_ROOT / "scripts" / "rebuild_faiss_from_db.py"
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

# Drill corpus — deterministic content. The top-1 query for "alpha" must be
# the "alpha note" doc both before and after the rebuild (assumption 2 fence).
DRILL_DOCS = [
    {"content": "alpha note about the drill recovery flow", "metadata": {"source": "drill"}},
    {"content": "beta note about sqlite and faiss rebuild", "metadata": {"source": "drill"}},
    {"content": "gamma note about manifest identity gate", "metadata": {"source": "drill"}},
]
DRILL_QUERY = "alpha"
EMBEDDER_DIM = 32
EMBEDDER_ID = "stub"
EMBEDDER_VERSION = "test"


def _log(msg: str) -> None:
    print(f"[dr_drill] {msg}", flush=True)


def _err(msg: str) -> None:
    print(f"[dr_drill] ERROR: {msg}", file=sys.stderr, flush=True)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def run_drill(root: Path, with_litestream: bool = False) -> int:
    """Run the full DR drill against ``<root>``. Returns 0 on success.

    ``root`` is used as the multiverse root: the universe lives at
    ``<root>/universes/<uid>/`` and the backup at ``<root>/drill-backup/``.
    """
    root = Path(root)
    print(
        "DR DRILL — standalone universe (managed=False). Managed-universe "
        "recovery (lease/token/registry/control-plane) is out of scope; see "
        "Operations-Backup-Multiverse.md runbook.",
        flush=True,
    )
    _log(f"root = {root}")

    # Start the stub embedding service once. All engine builds (populate,
    # rebuild subprocess, post-restore) point at it via GAOTTT_EMBEDDER_ENDPOINT
    # so the embedder identity (embedder_id='stub', dim=32) is consistent
    # across every phase — this is what makes rebuild_faiss_from_db's
    # build_engine path work without loading real RURI (768-dim), which
    # would trip the FAISS dimension guard against a 32-dim manifest.
    app = __import_embedding_app()
    server, thread, port = start_uvicorn(app)
    embedder_url = f"http://127.0.0.1:{port}"
    try:
        return asyncio.run(_run_drill_async(root, embedder_url, with_litestream))
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def __import_embedding_app():
    from gaottt.embedding.service import create_app

    return create_app(
        StubServiceEmbedder(
            dimension=EMBEDDER_DIM,
            embedder_id=EMBEDDER_ID,
            embedder_version=EMBEDDER_VERSION,
        )
    )


async def _run_drill_async(root: Path, embedder_url: str, with_litestream: bool) -> int:
    from gaottt.config import GaOTTTConfig
    from gaottt.services.runtime import build_engine
    from gaottt.store.manifest import UniverseManifest, write_manifest
    from gaottt.diagnostics.startup import run_startup_checks

    uid = "drill" + hex(int(time.time() * 1000))[-8:]
    udir = root / "universes" / uid
    udir.mkdir(parents=True, exist_ok=True)
    backup_dir = root / "drill-backup" / uid
    backup_dir.mkdir(parents=True, exist_ok=True)

    # ---- step 1: standalone manifest (managed=False) --------------------
    _log(f"step 1: create standalone universe {uid}")
    write_manifest(
        udir,
        UniverseManifest(
            universe_id=uid,
            embedder_id=EMBEDDER_ID,
            embedder_version=EMBEDDER_VERSION,
            embedding_dim=EMBEDDER_DIM,
            created_at=time.time(),
            managed=False,
        ),
    )

    def _cfg() -> GaOTTTConfig:
        return GaOTTTConfig(
            data_dir=str(udir),
            db_path=str(udir / "gaottt.db"),
            faiss_index_path=str(udir / "gaottt.faiss"),
            embedding_dim=EMBEDDER_DIM,
            embedder_endpoint=embedder_url,
            flush_interval_seconds=999.0,
            faiss_save_interval_seconds=999.0,
            virtual_faiss_save_interval_seconds=0.0,
            dream_enabled=False,
            orbital_tick_enabled=False,
            wave_initial_k=3,
            wave_max_depth=1,
            # Keep the drill fast + deterministic; disable extras that add
            # background work or non-determinism.
            supernova_enabled=False,
            genesis_kick_enabled=False,
        )

    # ---- step 2: populate + capture deterministic top-1 -----------------
    _log("step 2: build engine, index docs, capture top-1")
    engine = build_engine(_cfg())
    await engine.startup()
    try:
        ids = await engine.index_documents(DRILL_DOCS)
        _log(f"  indexed {len(ids)} docs")
        results = await engine.query(text=DRILL_QUERY, top_k=3)
        if not results:
            _err("top-1 empty before backup — drill cannot prove determinism")
            return 1
        pre_top1 = results[0].id
        _log(f"  pre-backup top-1 for {DRILL_QUERY!r}: {pre_top1}")
    finally:
        await engine.shutdown()

    # ---- step 3: backup (2-point set) -----------------------------------
    _log("step 3: backup gaottt.db + manifest.json (2-point set)")
    shutil.copy2(udir / "gaottt.db", backup_dir / "gaottt.db")
    shutil.copy2(udir / "manifest.json", backup_dir / "manifest.json")
    assert (backup_dir / "gaottt.db").exists()
    assert (backup_dir / "manifest.json").exists()

    if with_litestream:
        litestream_bin = shutil.which("litestream")
        if litestream_bin is None:
            # Best-effort path: binary absence is an environment limit, not a
            # drill failure. The default raw-copy backup above is the
            # authoritative proof. Log at ERROR so operators notice in CI/logs.
            _err(
                "litestream binary not found; --with-litestream path skipped "
                "(default raw-copy path still proves the DR claim)"
            )
        else:
            _log(f"  litestream binary found at {litestream_bin}; taking snapshot")
            replica = root / "drill-litestream-replica" / uid
            replica.mkdir(parents=True, exist_ok=True)
            # Best-effort: a subprocess failure (non-zero exit OR a timeout/
            # launch exception) is logged at ERROR but does NOT fail the drill
            # (the raw file-copy backup above is the authoritative path).
            # Strict litestream WAL-restore e2e is a manual pre-production check.
            try:
                proc = subprocess.run(
                    [litestream_bin, "replicate", "-no-expand",
                     f"-db {udir / 'gaottt.db'}", f"-replica {replica}"],
                    check=False, timeout=30, capture_output=True, text=True,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort, keep drilling
                _err(
                    f"litestream subprocess did not complete ({exc}); default "
                    f"raw-copy path still proves the DR claim. See "
                    f"Operations-Backup-Multiverse.md §商用導入前チェックリスト "
                    f"for strict litestream verification."
                )
            else:
                if proc.returncode != 0:
                    _err(
                        f"litestream subprocess failed (rc={proc.returncode}); "
                        f"default raw-copy path still proves the DR claim. See "
                        f"Operations-Backup-Multiverse.md §商用導入前チェックリスト "
                        f"for strict litestream verification. stderr={proc.stderr.strip()}"
                    )
    else:
        _log("  --with-litestream not set; skipping litestream snapshot path")

    # ---- step 4: destroy -------------------------------------------------
    _log("step 4: destroy universe dir (simulate disaster)")
    shutil.rmtree(udir)
    assert not udir.exists()

    # ---- step 5: restore (both files) -----------------------------------
    _log("step 5: restore gaottt.db + manifest.json from backup")
    udir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_dir / "gaottt.db", udir / "gaottt.db")
    shutil.copy2(backup_dir / "manifest.json", udir / "manifest.json")

    # ---- step 6: FAISS rebuild (--apply then --check) -------------------
    _log("step 6: rebuild FAISS from DB (--apply, --check)")
    rc = _run_rebuild(["--apply"], udir, embedder_url)
    if rc != 0:
        _err(f"rebuild --apply failed (rc={rc})")
        return 1
    rc = _run_rebuild(["--check"], udir, embedder_url)
    if rc != 0:
        _err(f"rebuild --check failed (rc={rc})")
        return 1
    _log("  rebuild OK")

    # ---- step 7: startup diagnostics + determinism fence ----------------
    _log("step 7: startup diagnostics + top-1 determinism check")
    engine = build_engine(_cfg())
    await engine.startup()
    try:
        report = await run_startup_checks(engine, _cfg())
        _log(f"  diagnostics: {report.summary()}")
        if report.has_errors:
            _err("startup diagnostics has ERRORS: "
                 + "; ".join(r.detail for r in report.by_level(__import_diagnostics().DiagnosticLevel.ERROR)))
            return 1
        results = await engine.query(text=DRILL_QUERY, top_k=3)
        if not results:
            _err("top-1 empty after restore — determinism lost")
            return 1
        post_top1 = results[0].id
        if post_top1 != pre_top1:
            _err(f"top-1 changed after restore: pre={pre_top1} post={post_top1}")
            return 1
        _log(f"  post-restore top-1 for {DRILL_QUERY!r}: {post_top1} (matches pre)")
    finally:
        await engine.shutdown()

    # ---- step 8: cleanup -------------------------------------------------
    _log("step 8: cleanup")
    shutil.rmtree(udir, ignore_errors=True)
    shutil.rmtree(backup_dir, ignore_errors=True)
    _log("DR DRILL PASSED")
    return 0


def _run_rebuild(flags: list[str], udir: Path, embedder_url: str) -> int:
    """Invoke scripts/rebuild_faiss_from_db.py with the universe's config env.

    GAOTTT_DATA_DIR points at the universe dir; GAOTTT_EMBEDDER_ENDPOINT
    makes build_engine wire a RemoteEmbedder against the stub service
    (deterministic, same identity as the manifest); GAOTTT_EMBEDDING_DIM
    matches the manifest so the FAISS dimension guard passes.
    """
    env = os.environ.copy()
    env["GAOTTT_DATA_DIR"] = str(udir)
    env["GAOTTT_EMBEDDER_ENDPOINT"] = embedder_url
    env["GAOTTT_EMBEDDING_DIM"] = str(EMBEDDER_DIM)
    # Keep the rebuild quiet on stdout; surface failures via stderr.
    proc = subprocess.run(
        [str(PYTHON), str(REBUILD_SCRIPT), *flags],
        capture_output=True, text=True, env=env, timeout=120,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
    return proc.returncode


def __import_diagnostics():
    from gaottt.diagnostics import startup

    return startup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/tmp/gaottt-dr-drill"),
        help="drill root (universe lives under <root>/universes/, backup under <root>/drill-backup/)",
    )
    parser.add_argument(
        "--with-litestream",
        action="store_true",
        help="best-effort litestream binary exercise; failures are logged at "
             "ERROR but do not fail the drill; strict WAL-restore e2e is a "
             "manual pre-production check",
    )
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    return run_drill(args.root, with_litestream=args.with_litestream)


if __name__ == "__main__":
    sys.exit(main())
