"""Bootstrap-aware numbered SQL migration runner (MV4 WP-1, design J4).

Design decisions baked in:
  * ``schema_migrations`` is bootstrapped by :func:`ensure_bootstrap` BEFORE any
    numbered file is scanned — chicken-and-egg avoidance (Codex B7).
  * One migration file == one DB transaction. On failure the whole file rolls
    back and no row is inserted into ``schema_migrations`` (no partial apply,
    Codex missing #1).
  * Idempotent: already-applied versions are skipped.
  * :func:`parse_version` / :func:`list_migrations` are pure functions and can
    be unit-tested without docker.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "ensure_bootstrap",
    "run_migrations",
    "parse_version",
    "list_migrations",
]


def parse_version(filename: str) -> str:
    """Extract the leading ``NNN`` version token from a migration filename.

    ``001_initial.sql`` -> ``"001"``; ``010_add_index.sql`` -> ``"010"``.
    Accepts either a bare filename or a path (only the final component is used).
    """
    name = Path(filename).name
    return name.split("_", 1)[0]


def list_migrations(schema_dir: Path) -> list[tuple[str, Path]]:
    """List ``(version, path)`` for every ``NNN_*.sql`` file in numeric order.

    Numeric-prefixed files are sorted by their integer value so ``002`` sorts
    before ``010`` regardless of filesystem listing order or zero-padding
    width. Non-numeric prefixes (none today) sort after numeric ones.
    """
    schema_dir = Path(schema_dir)
    files = list(schema_dir.glob("*.sql"))

    def _sort_key(p: Path) -> tuple[int, Any]:
        version = parse_version(p.name)
        if version.isdigit():
            return (0, int(version))
        return (1, version)

    files.sort(key=_sort_key)
    return [(parse_version(p.name), p) for p in files]


async def ensure_bootstrap(pool: Any) -> None:
    """Create the ``schema_migrations`` ledger if it does not yet exist.

    Idempotent (``CREATE TABLE IF NOT EXISTS``). Must be called before
    :func:`run_migrations` so the ledger exists when scanning for applied
    versions. ``run_migrations`` also calls this internally for safety.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")"
        )


async def _applied_versions(pool: Any) -> set[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT version FROM schema_migrations")
    return {r["version"] for r in rows}


async def run_migrations(pool: Any, schema_dir: Path) -> list[str]:
    """Apply every pending ``NNN_*.sql`` migration in numeric order.

    One file == one transaction: the file's SQL and the
    ``schema_migrations`` INSERT run inside the same ``BEGIN``/``COMMIT`` so a
    failure rolls both back (atomicity guarantee, Codex B7 / missing #1).

    Returns the list of newly-applied version strings (empty if no-op). Raises
    on the first failing file — already-applied earlier files stay committed,
    the failed file leaves no trace, and later files are not attempted.
    """
    await ensure_bootstrap(pool)
    already = await _applied_versions(pool)
    applied: list[str] = []

    for version, path in list_migrations(Path(schema_dir)):
        if version in already:
            continue
        sql = path.read_text(encoding="utf-8")
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)",
                    version,
                )
        applied.append(version)

    return applied


# --- CLI entry ---------------------------------------------------------------
# `python -m control.migrate` reads ControlConfig.from_env(), opens a pool,
# bootstraps + applies migrations, reports, and exits. WP-2's lifespan will
# call these functions directly instead of shelling out.
if __name__ == "__main__":  # pragma: no cover - exercised manually / README
    from .config import ControlConfig
    from .db import close_pool, create_pool

    cfg = ControlConfig.from_env()
    if not cfg.database_url:
        print("CONTROL_DATABASE_URL is not set", file=sys.stderr)
        sys.exit(2)

    async def _main() -> None:
        pool = await create_pool(
            cfg.database_url,
            min_size=cfg.db_pool_min_size,
            max_size=cfg.db_pool_max_size,
        )
        try:
            await ensure_bootstrap(pool)
            applied = await run_migrations(pool, cfg.schema_dir)
        finally:
            await close_pool(pool)
        if applied:
            print(f"applied {len(applied)} migration(s): {applied}")
        else:
            print("no new migrations to apply")

    asyncio.run(_main())
