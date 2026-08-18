"""asyncpg pool lifecycle for the control plane (MV4 WP-1, design J6).

The pool is created in the FastAPI lifespan (WP-2) and shared across request
handlers. Connection failure at creation time raises :class:`ConnectionError`
immediately — the control plane must not start against an unreachable DB.
"""

from __future__ import annotations

from typing import Any

import asyncpg

__all__ = ["create_pool", "close_pool"]


async def create_pool(
    dsn: str, min_size: int = 2, max_size: int = 10
) -> asyncpg.Pool:
    """Create an asyncpg connection pool and fail-fast on an unreachable DB.

    ``asyncpg.create_pool`` eagerly opens ``min_size`` connections; we also
    issue a probe ``SELECT 1`` so that authentication / routing failures (which
    can surface lazily) are caught here and surfaced as :class:`ConnectionError`
    before the control plane starts serving traffic.
    """
    try:
        pool = await asyncpg.create_pool(
            dsn=dsn, min_size=min_size, max_size=max_size
        )
    except Exception as exc:  # noqa: BLE001 - fail-fast wrap, see docstring
        raise ConnectionError(f"failed to create asyncpg pool: {exc}") from exc

    if pool is None:  # defensive: create_pool may return None on misconfig
        raise ConnectionError("asyncpg.create_pool returned None")

    try:
        async with pool.acquire() as conn:
            value = await conn.fetchval("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - fail-fast wrap
        await pool.close()
        raise ConnectionError(f"control plane DB unreachable: {exc}") from exc

    if value != 1:
        await pool.close()
        raise ConnectionError("control plane DB probe returned unexpected value")

    return pool


async def close_pool(pool: Any) -> None:
    """Close every connection in the pool. Safe to call once at shutdown."""
    await pool.close()
