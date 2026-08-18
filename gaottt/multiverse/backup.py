"""MV5 WP-1 / WP-2 — Litestream config generation (pure, importable).

The pure function :func:`generate_litestream_config` lives here (not in
``scripts/``) so that:

* the supervisor's backup hook (WP-2) can ``from gaottt.multiverse.backup
  import generate_litestream_config`` — a clean, package-qualified import
  rather than importing from a non-package ``scripts/`` directory;
* unit tests import the same symbol without path hacks;
* ``scripts/gen_litestream_config.py`` is a thin CLI wrapper over this module.

The function reads on-disk state only — no network, no external binary,
no side effects. It returns a YAML string and emits diagnostics (WARN /
ERROR / INFO) through the module logger; the CLI routes those to stderr
and the YAML to stdout (Codex review B1 — stdout must stay parseable).

Schema (litestream v0.3 ``dbs:`` block)::

    dbs:
      - path: /abs/path/to/universes/<uid>/gaottt.db
        replicas:
          - type: file
            path: /var/lib/litestream/gaottt-multiverse/<uid>

``manifest.json`` is NOT a litestream target — litestream replicates SQLite
WAL only. The manifest must be backed up separately (filesystem snapshot or
the ``exec`` example in ``deploy/litestream.yml``); the runbook documents
this as part of the mandatory 2-point backup set (SQLite + manifest).
"""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

from gaottt.multiverse.registry import TRASH_SUBDIR, UNIVERSES_SUBDIR
from gaottt.store.manifest import MANIFEST_FILENAME

logger = logging.getLogger(__name__)

# Default replica path prefix. The generated ``dbs:`` block points each
# universe's SQLite at ``<DEFAULT_REPLICA_PREFIX>/<uid>``. Operators override
# by editing the generated file or post-processing; the generator's job is to
# enumerate live universes, not to pick storage topology.
DEFAULT_REPLICA_PREFIX = "/var/lib/litestream/gaottt-multiverse"

# Canonical SQLite filename inside a universe dir. Kept as a constant so the
# scan rule "only this exact filename counts" is explicit and grep-able.
UNIVERSE_DB_FILENAME = "gaottt.db"


def generate_litestream_config(root: Path, replica_prefix: str = DEFAULT_REPLICA_PREFIX) -> str:
    """Scan ``<root>/universes/*/`` and return a litestream ``dbs:`` YAML string.

    Pure: reads on-disk state only, returns a string, emits diagnostics via
    the module logger (the CLI routes those to stderr).

    Scan rules (Codex review B1 / trash-race fence):

    * ``<root>/trash/`` is NEVER scanned (deleted universes must not appear).
    * A universe dir without ``manifest.json`` → ``ERROR`` log + **skipped
      entirely**. Rationale: restore without a manifest cannot pass
      ``verify_embedder_identity`` (the embedder-identity gate), so emitting
      a replica entry for a manifest-less universe would advertise a backup
      that cannot be restored. Safer to skip + log.
    * A universe dir without ``gaottt.db`` → ``WARN`` log + skipped (nothing
      to replicate).
    * ``*.db`` files outside the canonical ``universes/<uid>/gaottt.db``
      location are ignored (e.g. ``registry.db`` at the root, stray files).
    * Empty ``universes/`` → ``WARN`` log, returns ``dbs: []``.

    Args:
        root: the multiverse root (the value of ``config.multiverse_root``).
        replica_prefix: base path for the generated ``file`` replica entries;
            the full replica path is ``<replica_prefix>/<uid>``.

    Returns:
        A YAML string with a top-level ``dbs:`` list. Each entry has
        ``path`` (the absolute SQLite path) and ``replicas`` (one ``file``
        replica). An empty / fully-skipped root yields ``dbs: []``.
    """
    root = Path(root)
    universes_dir = root / UNIVERSES_SUBDIR

    entries: list[dict] = []

    if not universes_dir.is_dir():
        # No universes/ at all (fresh root, or a non-multiverse path). This is
        # not an error — emit an empty config so litestream has a valid file.
        logger.info("litestream-gen: %s/ does not exist; emitting empty dbs", universes_dir)
        return _emit_yaml(entries)

    # Collect candidate universe dirs. Defensive: never descend into a stray
    # ``trash`` subdir inside universes/ (canonical trash is a sibling).
    # NOTE: a permission / I/O error listing ``universes/`` is a HARD error
    # and is allowed to propagate — it is not a per-universe skip. Letting it
    # raise means the CLI returns non-zero WITHOUT touching ``--output``
    # (the atomic write is never reached), so a broken scan cannot overwrite
    # a previously-good config file (Codex review B2).
    candidates = sorted(
        p for p in universes_dir.iterdir()
        if p.is_dir() and p.name != TRASH_SUBDIR
    )

    if not candidates:
        logger.warning("litestream-gen: %s/ contains no universe dirs", universes_dir)

    for udir in candidates:
        uid = udir.name
        manifest_path = udir / MANIFEST_FILENAME
        db_path = udir / UNIVERSE_DB_FILENAME

        if not manifest_path.exists():
            # Skip entirely — a restore of this universe cannot pass the
            # embedder-identity gate, so advertising it as backed up is
            # misleading. The runbook flags this case for manual handling.
            logger.error(
                "litestream-gen: %s has no %s; skipping (restore would fail "
                "verify_embedder_identity)", udir, MANIFEST_FILENAME,
            )
            continue

        if not db_path.exists():
            logger.warning(
                "litestream-gen: %s has no %s; skipping (nothing to replicate)",
                udir, UNIVERSE_DB_FILENAME,
            )
            continue

        entries.append({
            "path": str(db_path),
            "replicas": [{
                "type": "file",
                "path": f"{replica_prefix}/{uid}",
            }],
        })

    return _emit_yaml(entries)


def _emit_yaml(entries: list[dict]) -> str:
    """Render the litestream ``dbs:`` block as a YAML string (hand-emitted).

    Hand-emitted (no ``pyyaml`` import) because pyyaml is not a declared
    dependency of the project — only a transitive one via transformers.
    The schema is small and fixed, so a manual emitter is both robust and
    dependency-free. The output is validated by ``yaml.safe_load`` in the
    unit tests.
    """
    if not entries:
        return "dbs: []\n"

    lines: list[str] = ["dbs:"]
    for e in entries:
        lines.append(f"  - path: {_yaml_quote(e['path'])}")
        lines.append("    replicas:")
        for r in e["replicas"]:
            lines.append(f"      - type: {r['type']}")
            lines.append(f"        path: {_yaml_quote(r['path'])}")
    return "\n".join(lines) + "\n"


def _yaml_quote(s: str) -> str:
    """Quote a scalar for YAML with minimal, correct escaping.

    Double quotes with backslash + double-quote escaping covers every
    filesystem path safely. We always quote (rather than conditionally) so
    the emitter has one code path and ``yaml.safe_load`` round-trips exactly.
    """
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` (tmp + fsync + os.replace).

    Used by both the CLI ``--output`` path and the supervisor backup hook.
    On replace failure the tmp file is removed so no ``*.tmp`` leftover
    survives — the atomic-write invariant is only complete when the failure
    path also cleans up (mirrors ``store.manifest.write_manifest``).

    A pre-existing ``path`` is untouched if the write raises: the tmp file
    is the only thing mutated, and it is removed before the exception
    propagates, so ``os.replace`` (the only step that touches ``path``) is
    never reached on error.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # Durability of the rename itself: fsync the parent dir so a crash
        # after os.replace does not lose the directory entry. Best-effort —
        # some filesystems don't support directory fsync.
        _fsync_dir(path.parent)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync of a directory (POSIX); silent no-op on Windows.

    Called after ``os.replace`` so the rename's directory entry is durable
    across a crash. Skipped silently when the OS or filesystem does not
    support directory fsync (e.g. Windows, some network filesystems)."""
    if os.name == "nt":
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass  # best-effort; some filesystems don't support dir fsync
