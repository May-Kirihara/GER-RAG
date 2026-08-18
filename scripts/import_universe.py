#!/usr/bin/env python3
"""Multiverse importer — adopt a standalone GaOTTT ``data_dir`` as one universe.

Transforms an existing single-user ``data_dir`` (the standalone
``gaottt.db`` + FAISS layout) into a managed universe under
``<multiverse_root>/universes/<uid>/`` with a fresh API key, registry row,
and ``managed=True`` manifest. The supervisor picks it up on its next
``reconcile``; the importer does not need the supervisor to be running.

Usage::

    .venv/bin/python scripts/import_universe.py \\
        --source ~/.local/share/gaottt \\
        --owner-label "main" \\
        [--universe-id <12-hex-lower>] \\
        [--multiverse-root <path>] \\
        [--move] [--force] [--yes] [--dry-run] \\
        [--embedder-id <id>] [--embedder-version <v>]

Exit codes:
    0  success
    2  argument error (bad uid format, non-TTY without --yes,
       empty multiverse_root)
    3  source backend still running (use --force to override;
       adds a mandatory post-copy integrity_check)
    4  embedder service unreachable or identity mismatch
    5  WAL too large (>256MB hard reject, or >64MB without --yes)
    6  insufficient disk capacity on target filesystem
    7  copy / move failed with a generic exception
    8  post-copy PRAGMA integrity_check failed
    9  retry budget exhausted (persistent port race)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from gaottt.config import GaOTTTConfig  # noqa: E402
from gaottt.multiverse.importer import (  # noqa: E402
    IntegrityCheckFailed,
    UniverseAlreadyExistsError,
    build_import_plan,
    execute_import,
)
from gaottt.multiverse.registry import MultiverseRegistry  # noqa: E402
from gaottt.store.manifest import load_manifest  # noqa: E402

# ---------------------------------------------------------------------------
# Tunables — thresholds without new config knobs (plan §1.3 / §2.5)
# ---------------------------------------------------------------------------

# Hard reject: an unflushed WAL this large is almost certainly a live writer
# (the busy_timeout / WAL normalization cycle keeps a healthy WAL well below
# this). Suggests the source backend was not stopped.
WAL_HARD_REJECT_BYTES = 256 * 1024 * 1024
# Soft warning: a healthy post-shutdown WAL is usually <10MB. >64MB merits a
# pause; --yes (or interactive confirm) proceeds.
WAL_WARN_BYTES = 64 * 1024 * 1024

# Disk capacity buffer: target filesystem free must cover copy bytes +10%
# so a near-full disk does not fail mid-copy leaving a half-written target.
DISK_CAPACITY_BUFFER = 1.10

# Lease heartbeat staleness for the source owner.lock check. Mirrors the
# config default so the importer does not need to second-guess it.
DEFAULT_LEASE_STALE_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Process / port probes (parity with scripts/reset_masses.py)
# ---------------------------------------------------------------------------


def _running_gaottt_pids() -> list[tuple[int, str]]:
    """Return PIDs of GaOTTT server processes still running on this host."""
    patterns = [
        ("gaottt.server.mcp_server", "MCP server"),
        ("gaottt.server.app", "REST server"),
    ]
    found: list[tuple[int, str]] = []
    for pattern, label in patterns:
        try:
            out = subprocess.check_output(
                ["pgrep", "-f", pattern], stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        for line in out.decode().splitlines():
            line = line.strip()
            if line.isdigit():
                found.append((int(line), label))
    return found


def _platform_default_data_dir() -> Path:
    """The platform-default ``data_dir`` used when no env / config override
    is present (Linux/macOS: ``~/.local/share/gaottt``). Used by
    :func:`_gaottt_pids_for_source` to decide whether a standalone-default
    process (no ``GAOTTT_DATA_DIR`` env) is plausibly writing to ``source``.
    """
    xdg = os.environ.get("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "gaottt"


def _gaottt_pids_for_source(source: Path) -> list[tuple[int, str]]:
    """Return PIDs of GaOTTT servers that may be writing to ``source``.

    A host may run many unrelated GaOTTT processes (this dev machine runs
    several); only one whose data_dir is the import source can race the copy.

    Resolution policy (Codex final review #2 — close the standalone-default
    gap):

    * If ``/proc/<pid>/environ`` carries ``GAOTTT_DATA_DIR`` resolving to
      ``source``, the process is a definite writer.
    * If ``/proc/<pid>/environ`` is unreadable (permission denied, ESRCH,
      not Linux), the process is treated conservatively as a writer — we
      cannot prove it is unrelated.
    * If the environ is readable but ``GAOTTT_DATA_DIR`` is absent, the
      process is a *standalone-default* run. Such a process writes to the
      platform-default ``data_dir`` (``~/.local/share/gaottt``). We flag
      it as a candidate writer **only when ``source`` itself resolves to
      the platform default** — that is the one case where a
      standalone-default process actually races the copy. For other
      sources (e.g. a tmp_path test fixture) the standalone-default
      process is unrelated and not flagged.
    """
    src_resolved = source.resolve()
    platform_default = _platform_default_data_dir().resolve()
    src_is_platform_default = src_resolved == platform_default
    candidates = _running_gaottt_pids()
    if not candidates:
        return []
    writers: list[tuple[int, str]] = []
    for pid, label in candidates:
        env_path = Path(f"/proc/{pid}/environ")
        if not env_path.exists():
            # Not Linux or process gone — conservatively include it so a
            # platform without /proc still gets the safety net.
            writers.append((pid, label))
            continue
        try:
            raw = env_path.read_bytes()
        except (PermissionError, OSError, ProcessLookupError):
            # Cannot prove it is unrelated — include conservatively.
            writers.append((pid, label))
            continue
        # /proc/environ is NUL-separated KEY=VALUE records.
        gaottt_data_dir: str | None = None
        for entry in raw.split(b"\x00"):
            if entry.startswith(b"GAOTTT_DATA_DIR="):
                val = entry.split(b"=", 1)[1].decode("utf-8", "replace")
                gaottt_data_dir = val
                break
        if gaottt_data_dir is not None:
            try:
                if Path(gaottt_data_dir).resolve() == src_resolved:
                    writers.append((pid, label))
            except OSError:
                pass
        elif src_is_platform_default:
            # Standalone-default run (no GAOTTT_DATA_DIR env) and source is
            # the platform-default location — the process is plausibly
            # writing to source. Flag it; --force overrides.
            writers.append((pid, label))
    return writers


def _backend_port_reachable(
    host: str = "127.0.0.1", port: int = 7878, timeout: float = 1.0,
) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((host, port))
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Source-state probes
# ---------------------------------------------------------------------------


def _source_owner_lock_state(
    source: Path, stale_seconds: float,
) -> tuple[bool, dict | None]:
    """Return ``(active, payload)`` for ``source/owner.lock``.

    * Missing lock → ``(False, None)``.
    * Stale lock (``now - heartbeat_at > stale_seconds``) → ``(False, payload)``.
    * Active lock → ``(True, payload)``.
    * Corrupt lock → conservative ``(True, None)`` (cannot prove it is takeable).
    """
    lock_path = source / "owner.lock"
    if not lock_path.exists():
        return False, None
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        # Cannot prove the lock is takeable — be conservative and treat it
        # as active. --force still overrides.
        return True, None
    if not isinstance(data, dict):
        return True, None
    try:
        heartbeat_at = float(data.get("heartbeat_at", 0.0))
    except (TypeError, ValueError):
        return True, None
    active = (time.time() - heartbeat_at) <= stale_seconds
    return active, data


def _wal_size(source: Path) -> int:
    wal = source / "gaottt.db-wal"
    return wal.stat().st_size if wal.exists() else 0


# ---------------------------------------------------------------------------
# Embedder service probe — parity with supervisor._validate_embedder but
# standalone (no supervisor import → importer never couples to it).
# ---------------------------------------------------------------------------


def _probe_embedder_service(
    endpoint: str, *, timeout: float = 5.0,
) -> dict:
    """GET ``<endpoint>/info`` and return the parsed dict.

    Raises ``RuntimeError`` on any failure (unreachable, non-200, invalid
    JSON, missing fields). Caller maps to exit 4.
    """
    import httpx

    url = endpoint.rstrip("/") + "/info"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"embedder service unreachable: {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(
            f"embedder /info returned status {response.status_code}"
        )
    try:
        info = response.json()
    except ValueError as exc:
        raise RuntimeError("embedder /info returned invalid JSON") from exc
    if not info.get("model_name") or not info.get("dimension"):
        raise RuntimeError("embedder /info missing model_name or dimension")
    return info


# ---------------------------------------------------------------------------
# Plan rendering (dry-run / pre-execute summary)
# ---------------------------------------------------------------------------


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MiB"
    return f"{n / (1024 * 1024 * 1024):.2f} GiB"


def _print_plan(plan, *, stream=sys.stdout) -> None:
    print(f"universe_id  : {plan.universe_id}", file=stream)
    print(f"source       : {plan.source}", file=stream)
    print(f"target       : {plan.target}", file=stream)
    print(f"owner_label  : {plan.owner_label}", file=stream)
    print(f"mode         : {plan.mode}", file=stream)
    print(f"embedder_id  : {plan.embedder_id}", file=stream)
    print(f"embedder_ver : {plan.embedder_version}", file=stream)
    print(f"embedding_dim: {plan.embedding_dim}", file=stream)
    print(f"copy_files   : {len(plan.file_plan.copy_files)} "
          f"({_format_size(plan.file_plan.total_bytes)})", file=stream)
    for name in plan.file_plan.copy_files:
        print(f"  - {name}", file=stream)
    if plan.file_plan.skipped:
        print(f"skipped      : {len(plan.file_plan.skipped)}", file=stream)
        for name, reason in plan.file_plan.skipped:
            print(f"  - {name}  ({reason})", file=stream)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import a standalone GaOTTT data_dir as a multiverse universe.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--source", required=True, type=Path,
                        help="standalone data_dir to import")
    parser.add_argument("--owner-label", required=True,
                        help="owner label recorded in the registry")
    parser.add_argument("--universe-id", default=None,
                        help="12-char lowercase hex; default = uuid4().hex[:12]")
    parser.add_argument("--multiverse-root", default=None, type=Path,
                        help="multiverse root (default: config.multiverse_root)")
    parser.add_argument("--move", action="store_true",
                        help="move instead of copy (source loses the 7 files)")
    parser.add_argument("--force", action="store_true",
                        help="bypass source-backend-running checks (UNSAFE; "
                             "forces a post-copy integrity_check)")
    parser.add_argument("--yes", action="store_true",
                        help="skip TTY confirmation prompts; "
                             "REQUIRED in non-TTY (CI/pipe) environments")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and exit without executing")
    parser.add_argument("--embedder-id", default=None,
                        help="override embedder_id for the target manifest")
    parser.add_argument("--embedder-version", default=None,
                        help="override embedder_version for the target manifest")
    return parser


def _emit(*args, file=sys.stderr, **kw) -> None:
    print(*args, file=file, **kw)


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    # ---- step 1: argument / TTY / multiverse_root validation --------------

    if args.universe_id is not None:
        from gaottt.multiverse.importer import validate_universe_id
        if not validate_universe_id(args.universe_id):
            _emit(f"ERROR: --universe-id must be 12 lowercase hex chars, "
                  f"got {args.universe_id!r}")
            return 2

    # Non-TTY environments (CI, pipes) require explicit --yes so an automated
    # run cannot sail through an interactive confirmation prompt silently.
    if not args.dry_run and not args.yes and not sys.stdin.isatty():
        _emit("ERROR: non-TTY environment requires --yes "
              "(or --dry-run) to proceed.")
        return 2

    config = GaOTTTConfig.from_config_file()
    if args.multiverse_root is not None:
        mv_root = Path(args.multiverse_root)
        config.multiverse_root = str(mv_root)
    else:
        mv_root = Path(config.multiverse_root) if config.multiverse_root else None

    if not mv_root:
        _emit("ERROR: multiverse_root is not set. Pass --multiverse-root or "
              "set GAOTTT_MULTIVERSE_ROOT / config.multiverse_root.")
        return 2

    # ---- step 2: source sanity -------------------------------------------

    source = Path(args.source)
    if not source.exists() or not source.is_dir():
        _emit(f"ERROR: source directory does not exist or is not a directory: "
              f"{source}")
        return 2
    if not (source / "gaottt.db").exists():
        _emit(f"ERROR: source has no gaottt.db: {source}")
        return 2

    # ---- step 3: running-process probe (scoped to source writers) -------
    #
    # Codex final review #2: the per-PID environ probe misses standalone-
    # default runs that have no GAOTTT_DATA_DIR env var. The detached proxy
    # backend (port 7878) is the host-level signal that the source's live
    # backend is up. We use both signals, but scope the port probe to the
    # standalone-default case (source == platform-default data_dir): on a
    # multiverse host a stray backend on 7878 serving a *different* universe
    # is not evidence of a source writer, and flagging it would make the
    # importer unusable on shared dev machines.
    source_writers = _gaottt_pids_for_source(source)
    src_is_default = (
        source.resolve() == _platform_default_data_dir().resolve()
    )
    backend_live = (
        src_is_default
        and _backend_port_reachable(host="127.0.0.1", port=7878)
    )
    if (source_writers or backend_live) and not args.force:
        reasons = []
        if source_writers:
            desc = ", ".join(f"pid={p} ({lb})" for p, lb in source_writers)
            reasons.append(f"server processes: {desc}")
        if backend_live:
            reasons.append("detached proxy backend reachable on 127.0.0.1:7878")
        _emit(
            "ERROR: GaOTTT source writers appear to be live "
            f"({'; '.join(reasons)}).\n"
            "  Stop them first (pkill -f gaottt.server.mcp_server; "
            "pkill -f gaottt.server.app; kill the detached proxy backend "
            "on 127.0.0.1:7878) or pass --force."
        )
        return 3
    if args.force and (source_writers or backend_live):
        notes = []
        if source_writers:
            desc = ", ".join(f"pid={p} ({lb})" for p, lb in source_writers)
            notes.append(f"server processes: {desc}")
        if backend_live:
            notes.append("detached proxy backend reachable on 127.0.0.1:7878")
        _emit(
            f"WARNING: --force in effect with source writers running "
            f"({'; '.join(notes)})\n"
            "  This is unsafe: a concurrent writer can produce a corrupt "
            "copy.\n"
            "  A post-copy PRAGMA integrity_check WILL be run; if it fails "
            "the import is aborted (exit 8)."
        )

    # ---- step 4: source owner.lock probe --------------------------------

    lock_active, lock_payload = _source_owner_lock_state(
        source, DEFAULT_LEASE_STALE_SECONDS,
    )
    if lock_active and not args.force:
        owner = (lock_payload or {}).get("owner_id", "<unknown>")
        _emit(
            f"ERROR: source owner.lock is held by an active owner "
            f"({owner}). Stop that process first or pass --force."
        )
        return 3
    if lock_active and args.force:
        owner = (lock_payload or {}).get("owner_id", "<unknown>")
        _emit(
            f"WARNING: --force overriding active source owner.lock "
            f"(owner={owner}). Post-copy integrity_check will run."
        )
    elif not lock_active and lock_payload is not None:
        owner = lock_payload.get("owner_id", "<unknown>")
        _emit(
            f"WARNING: source owner.lock is stale (owner={owner}); "
            f"treating source as unowned."
        )

    # ---- step 5: WAL size check -----------------------------------------

    wal_size = _wal_size(source)
    if wal_size > WAL_HARD_REJECT_BYTES:
        _emit(
            f"ERROR: source WAL is {wal_size} bytes (> "
            f"{WAL_HARD_REJECT_BYTES}). The source backend likely did not "
            f"shut down cleanly. Stop it, let SQLite checkpoint, then retry."
        )
        return 5
    if wal_size > WAL_WARN_BYTES:
        if not args.yes:
            _emit(
                f"ERROR: source WAL is {wal_size} bytes (> "
                f"{WAL_WARN_BYTES}). Pass --yes to proceed anyway."
            )
            return 5
        _emit(
            f"WARNING: source WAL is {wal_size} bytes (> "
            f"{WAL_WARN_BYTES}); --yes given, proceeding."
        )

    # ---- step 6: embedder service probe (only when endpoint set) --------

    # ``resolved_embedder_override`` captures the embedder_id that
    # build_import_plan should record in the target manifest. Priority:
    #   1. --embedder-id CLI flag (user explicit)
    #   2. /info model_name when source manifest is absent (parity with
    #      supervisor.create_universe at supervisor.py:689 — the live
    #      service is the authoritative identity)
    #   3. None → build_import_plan falls back to source manifest → config
    resolved_embedder_override: str | None = args.embedder_id

    if config.embedder_endpoint:
        try:
            info = _probe_embedder_service(config.embedder_endpoint)
        except RuntimeError as exc:
            _emit(f"ERROR: embedder service check failed: {exc}")
            return 4

        # Dimension gate: the live service's dimension MUST match
        # config.embedding_dim, otherwise the imported universe's manifest
        # would record a dimension the backend cannot serve. (Codex final
        # review #4.)
        try:
            service_dim = int(info.get("dimension", 0))
        except (TypeError, ValueError):
            service_dim = 0
        if service_dim != config.embedding_dim:
            _emit(
                f"ERROR: embedder dimension mismatch: service /info reports "
                f"{info.get('dimension')!r}, config.embedding_dim="
                f"{config.embedding_dim}. Re-embed the source with the "
                f"matching model or fix config.embedding_dim."
            )
            return 4

        # Identity gate (Codex final review #4). Two cases:
        # * Source manifest present + no CLI override: source manifest's
        #   embedder_id must equal /info model_name. A mismatch means the
        #   source was embedded with a different model → silent recall
        #   degradation. Reject (exit 4).
        # * Source manifest absent + no CLI override: the live service's
        #   model_name is the authoritative identity (parity with
        #   supervisor.create_universe). Pass it as the override so the
        #   target manifest records the live identity rather than the
        #   config fallback (which may not match the service actually
        #   serving the backend).
        source_manifest = None
        try:
            source_manifest = load_manifest(source)
        except (json.JSONDecodeError, ValueError, OSError):
            pass
        live_model = info.get("model_name")
        if args.embedder_id is None:
            if source_manifest is not None:
                if source_manifest.embedder_id != live_model:
                    _emit(
                        f"ERROR: embedder identity mismatch: source manifest="
                        f"{source_manifest.embedder_id!r}, service=/info "
                        f"model_name={live_model!r}. "
                        f"Re-embed the source or pass --embedder-id to "
                        f"override."
                    )
                    return 4
            elif live_model:
                # No source manifest — the live service is authoritative.
                resolved_embedder_override = live_model
                if config.model_name != live_model:
                    _emit(
                        f"NOTE: no source manifest; using live service "
                        f"model_name={live_model!r} as the target manifest "
                        f"embedder_id (config.model_name={config.model_name!r} "
                        f"differs). Pass --embedder-id to override."
                    )

    # ---- step 7: build plan ----------------------------------------------

    try:
        plan = build_import_plan(
            source, args.owner_label, config,
            universe_id=args.universe_id,
            move=args.move,
            embedder_id_override=resolved_embedder_override,
            embedder_version_override=args.embedder_version,
        )
    except (ValueError, FileExistsError) as exc:
        _emit(f"ERROR: {exc}")
        return 2

    # ---- step 8: disk capacity check ------------------------------------

    # Plan §2.3 step 7: target filesystem free >= copy total + 10% buffer.
    # ``shutil.disk_usage`` reports the real filesystem free (sparse files
    # report their nominal size in st_size but consume little actual space,
    # so this correctly rejects a 500GB sparse source on a <550GB-free disk).
    # Walk up to the first existing ancestor so a not-yet-created mv_root
    # does not crash the check (dry-run must still work, and a fresh root is
    # the common case).
    probe_path = mv_root
    while not probe_path.exists():
        if probe_path.parent == probe_path:
            break  # reached /
        probe_path = probe_path.parent
    try:
        target_fs_free = shutil.disk_usage(str(probe_path)).free
    except OSError as exc:
        _emit(f"ERROR: cannot stat target filesystem {probe_path}: {exc}")
        return 6
    required_bytes = int(plan.file_plan.total_bytes * DISK_CAPACITY_BUFFER)
    if target_fs_free < required_bytes:
        _emit(
            f"ERROR: insufficient disk capacity on {probe_path}: "
            f"need {_format_size(required_bytes)} (copy "
            f"{_format_size(plan.file_plan.total_bytes)} + 10% buffer), "
            f"have {_format_size(target_fs_free)} free."
        )
        return 6

    # ---- step 9: plan display + dry-run exit ----------------------------

    _print_plan(plan)
    if args.dry_run:
        print("\n[dry-run] no changes made.", file=sys.stdout)
        return 0

    # ---- step 10: interactive confirm -----------------------------------

    if not args.yes:
        # Already gated on TTY in step 1; reaching here means TTY + no --yes.
        print(f"\nAbout to {plan.mode} {len(plan.file_plan.copy_files)} files "
              f"({_format_size(plan.file_plan.total_bytes)}) into "
              f"{plan.target}.", file=sys.stdout)
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.", file=sys.stdout)
            return 0

    # ---- step 11: transactional execute ---------------------------------

    # Initialize the multiverse root + registry. ``registry.initialize`` is
    # idempotent (mkdir + schema IF NOT EXISTS), safe on an existing root.
    try:
        mv_root.mkdir(parents=True, exist_ok=True)
        os.chmod(mv_root, 0o700)
    except OSError as exc:
        _emit(f"ERROR: cannot initialize multiverse_root {mv_root}: {exc}")
        return 7

    registry = MultiverseRegistry(mv_root)

    async def _run() -> str:
        await registry.initialize()
        try:
            return await execute_import(
                plan, registry,
                port_range=(
                    config.universe_port_range_start,
                    config.universe_port_range_end,
                ),
            )
        finally:
            await registry.close()

    try:
        api_key = asyncio.run(_run())
    except UniverseAlreadyExistsError as exc:
        # UID collision with an existing registry row (Codex final review
        # #5). Argument-class error — same exit code as validate_universe_id
        # rejection.
        _emit(f"ERROR: {exc}")
        return 2
    except IntegrityCheckFailed as exc:
        # Post-copy PRAGMA integrity_check failed (Codex final review #3).
        # Distinct from generic RuntimeError (exit 7) per the documented
        # exit-code contract.
        _emit(f"ERROR: post-copy integrity check failed: {exc}")
        return 8
    except RuntimeError as exc:
        # Distinguish the retry-exhausted message (exit 9) from other
        # RuntimeErrors (treat as exit 7).
        msg = str(exc)
        if "could not allocate port" in msg and "attempts" in msg:
            _emit(f"ERROR: {exc}")
            return 9
        _emit(f"ERROR: import failed: {exc}")
        return 7
    except Exception as exc:
        _emit(f"ERROR: import failed: {exc}")
        return 7

    # ---- step 12: success report ----------------------------------------

    print("", file=sys.stdout)
    print(f"universe_id : {plan.universe_id}", file=sys.stdout)
    print(f"port        : {plan.port}", file=sys.stdout)
    print(f"target_dir  : {plan.target}", file=sys.stdout)
    print(f"api_key     : {api_key}  (shown once — store it now)",
          file=sys.stdout)
    print("", file=sys.stdout)
    print("Next steps:", file=sys.stdout)
    print(f"  - Ensure the supervisor is running on multiverse_root={mv_root}",
          file=sys.stdout)
    print("    (or start it: python -m gaottt.multiverse.supervisor)",
          file=sys.stdout)
    print("  - Point a shim at the supervisor and /route with the api_key",
          file=sys.stdout)
    print("  - Confirm with a recall() through the spawned backend",
          file=sys.stdout)
    if plan.mode == "move":
        backup_residue = [n for n, _ in plan.file_plan.skipped]
        if backup_residue:
            print("", file=sys.stdout)
            print(
                "WARNING: --move left non-copy files in source (backups, "
                "manifests, tokens). Move or remove them manually:",
                file=sys.stderr,
            )
            for name in backup_residue:
                print(f"  - {source / name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
