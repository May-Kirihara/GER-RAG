"""asyncpg pool lifecycle tests (MV4 WP-1, design J6).

Requires the disposable Postgres. ``create_pool`` must succeed against the
disposable DB and ``close_pool`` must release resources; a bad DSN must raise
:class:`ConnectionError` immediately (fail-fast).
"""

from __future__ import annotations

import pytest

from control.db import close_pool, create_pool


@pytest.mark.requires_postgres
async def test_pool_lifecycle_select(disposable_postgres: str) -> None:
    pool = await create_pool(disposable_postgres, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            value = await conn.fetchval("SELECT 1")
        assert value == 1
    finally:
        await close_pool(pool)


@pytest.mark.requires_postgres
async def test_pool_reusable_after_release(disposable_postgres: str) -> None:
    pool = await create_pool(disposable_postgres, min_size=1, max_size=2)
    try:
        for _ in range(3):
            async with pool.acquire() as conn:
                assert await conn.fetchval("SELECT 42") == 42
    finally:
        await close_pool(pool)


@pytest.mark.requires_postgres
async def test_bad_dsn_raises_connection_error() -> None:
    # Port 1 is reserved and will refuse TCP connections quickly.
    bad_dsn = "postgresql://nobody@127.0.0.1:1/nonexistent"
    with pytest.raises(ConnectionError):
        await create_pool(bad_dsn, min_size=1, max_size=1)
