"""Tests for the bootstrap-aware migration runner (MV4 WP-1).

Pure-function tests (no docker) cover version parsing and numeric sorting.
The ``@pytest.mark.requires_postgres`` tests exercise the runner against the
disposable Postgres: idempotency, full schema apply, failure atomicity, and
exact-SQL boot (guards against syntax errors the runner might mask — Codex
missing #2).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from control.migrate import (
    ensure_bootstrap,
    list_migrations,
    parse_version,
    run_migrations,
)

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "control" / "schema"

# Tables created by 001_initial.sql (the criterion text lists these 7; the
# "6 domain tables" parenthetical is a known miscount — there are 7 domain
# tables plus the runner-bootstrapped schema_migrations).
DOMAIN_TABLES = [
    "tenants",
    "users",
    "hosts",
    "universes",
    "usage_batches",
    "usage_events",
    "audit_log",
]


# --- pure-function tests (docker-free) --------------------------------------


def test_parse_version() -> None:
    assert parse_version("001_initial.sql") == "001"
    assert parse_version("010_add_index.sql") == "010"
    # Accepts full paths too (only the final component matters).
    assert parse_version("/some/dir/002_x.sql") == "002"


def test_list_migrations_sorted(tmp_path: Path) -> None:
    # Write files in non-sorted insertion order.
    (tmp_path / "010_add_index.sql").write_text("-- 10")
    (tmp_path / "001_initial.sql").write_text("-- 1")
    (tmp_path / "002_small_fix.sql").write_text("-- 2")
    (tmp_path # a stray non-sql file must be ignored
     / "README.md").write_text("ignore me")

    result = list_migrations(tmp_path)
    versions = [v for v, _ in result]
    assert versions == ["001", "002", "010"]
    # All returned paths must actually exist.
    assert all(p.exists() for _, p in result)


# --- DB-backed tests (require disposable Postgres) --------------------------


@pytest.mark.requires_postgres
async def test_ensure_bootstrap_idempotent(disposable_postgres: str) -> None:
    import asyncpg

    pool = await asyncpg.create_pool(dsn=disposable_postgres, min_size=1, max_size=2)
    try:
        await ensure_bootstrap(pool)
        # Second call must be a no-op (CREATE TABLE IF NOT EXISTS).
        await ensure_bootstrap(pool)

        async with pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT to_regclass('public.schema_migrations')"
            )
            assert exists == "schema_migrations"
            rows = await conn.fetch("SELECT version FROM schema_migrations")
            assert rows == []
    finally:
        await pool.close()


@pytest.mark.requires_postgres
async def test_run_migrations_applies_schema(disposable_postgres: str) -> None:
    import asyncpg

    pool = await asyncpg.create_pool(dsn=disposable_postgres, min_size=1, max_size=2)
    try:
        applied = await run_migrations(pool, SCHEMA_DIR)
        assert applied == ["001"]

        async with pool.acquire() as conn:
            for table in DOMAIN_TABLES:
                reg = await conn.fetchval(
                    f"SELECT to_regclass('public.{table}')"
                )
                assert reg == table, f"table {table} was not created"

            # schema_migrations ledger exists and records the applied version.
            assert (
                await conn.fetchval("SELECT to_regclass('public.schema_migrations')")
                == "schema_migrations"
            )
            versions = [
                r["version"]
                for r in await conn.fetch("SELECT version FROM schema_migrations")
            ]
            assert versions == ["001"]

            # default tenant was bootstrapped (Codex review #2 blocking #2).
            default_name = await conn.fetchval(
                "SELECT name FROM tenants WHERE tenant_id = 'default'"
            )
            assert default_name is not None
    finally:
        await pool.close()


@pytest.mark.requires_postgres
async def test_run_migrations_idempotent(disposable_postgres: str) -> None:
    import asyncpg

    pool = await asyncpg.create_pool(dsn=disposable_postgres, min_size=1, max_size=2)
    try:
        first = await run_migrations(pool, SCHEMA_DIR)
        second = await run_migrations(pool, SCHEMA_DIR)
        assert first == ["001"]
        assert second == []  # no-op on second run
    finally:
        await pool.close()


@pytest.mark.requires_postgres
async def test_run_migrations_failure_atomicity(
    disposable_postgres: str, tmp_path: Path
) -> None:
    """A broken 2nd migration must leave no partial apply (Codex missing #1)."""
    import asyncpg

    # Build a tmp schema_dir with the real 001 plus a deliberately-broken 002.
    tmp_schema = tmp_path / "schema"
    tmp_schema.mkdir()
    shutil.copyfile(SCHEMA_DIR / "001_initial.sql", tmp_schema / "001_initial.sql")
    (tmp_schema / "002_broken.sql").write_text(
        "CREATE TABLE will_fail (id INTEGER); THIS IS NOT VALID SQL;\n"
    )

    pool = await asyncpg.create_pool(dsn=disposable_postgres, min_size=1, max_size=2)
    try:
        with pytest.raises(Exception):
            await run_migrations(pool, tmp_schema)

        async with pool.acquire() as conn:
            # 001 was committed in its own transaction before 002 ran.
            assert (
                await conn.fetchval("SELECT to_regclass('public.tenants')")
                == "tenants"
            )
            # The broken file's partial DDL was rolled back.
            assert (
                await conn.fetchval("SELECT to_regclass('public.will_fail')")
                is None
            )
            # 002 was never recorded as applied.
            applied_002 = await conn.fetchval(
                "SELECT version FROM schema_migrations WHERE version = '002'"
            )
            assert applied_002 is None
    finally:
        await pool.close()


@pytest.mark.requires_postgres
async def test_exact_sql_boot(disposable_postgres: str) -> None:
    """Execute 001_initial.sql verbatim to catch syntax errors the runner
    might mask (Codex missing #2)."""
    import asyncpg

    sql = (SCHEMA_DIR / "001_initial.sql").read_text(encoding="utf-8")
    pool = await asyncpg.create_pool(dsn=disposable_postgres, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute(sql)

            for table in DOMAIN_TABLES:
                assert (
                    await conn.fetchval(f"SELECT to_regclass('public.{table}')")
                    == table
                )
            # Indexes the schema declares.
            for idx in [
                "idx_universes_tenant",
                "idx_universes_host",
                "idx_usage_tenant_time",
                "idx_usage_universe_time",
                "idx_audit_tenant_time",
                "idx_audit_actor_time",
            ]:
                assert (
                    await conn.fetchval(f"SELECT to_regclass('public.{idx}')")
                    == idx
                )
            # default tenant present after verbatim boot.
            assert (
                await conn.fetchval(
                    "SELECT name FROM tenants WHERE tenant_id = 'default'"
                )
                is not None
            )
    finally:
        await pool.close()
