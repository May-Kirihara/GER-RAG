"""Authentication dependencies for the control plane API (MV4 WP-2, design J3).

Two trust boundaries:

* **Admin** — a single shared key protecting every ``/admin/*`` endpoint.
  Validated with :func:`secrets.compare_digest` (constant-time). The empty-key
  fail-fast lives in :func:`control.api.create_app`, not here; by the time a
  checker is constructed the key is guaranteed non-empty.

* **Host token** — per-supervisor tokens issued once via
  ``POST /admin/hosts``. Only the SHA-256 hash is stored, so verification is a
  parameterized DB lookup against ``hosts.token_hash`` (NOT ``compare_digest``
  on a stored plaintext — there is none). A revoked host (``revoked_at`` set)
  never authenticates. A token whose ``host_id`` differs from the path ``hid``
  is rejected with 403.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from fastapi import HTTPException, Request

__all__ = ["make_admin_checker", "make_host_checker", "hash_token"]


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a host token (what gets stored in ``hosts.token_hash``)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def make_admin_checker(config: Any):
    """Build an async admin-auth dependency for ``config.admin_key``.

    The returned coroutine reads ``X-Admin-Key`` or
    ``Authorization: Bearer <key>`` and validates with
    :func:`secrets.compare_digest`. Raises 401 on missing/mismatch. It does NOT
    police an empty key — that is ``create_app``'s fail-fast responsibility.
    """
    admin_key = config.admin_key

    async def _check(request: Request) -> None:
        provided = request.headers.get("X-Admin-Key")
        if provided is None:
            auth = request.headers.get("Authorization")
            if auth is not None and auth.lower().startswith("bearer "):
                provided = auth[7:].strip()
        if provided is None:
            raise HTTPException(status_code=401, detail="missing admin key")
        if not secrets.compare_digest(provided, admin_key):
            raise HTTPException(status_code=401, detail="invalid admin key")

    return _check


def make_host_checker(config: Any, pool: Any):
    """Build an async host-token dependency bound to ``pool``.

    Reads ``Authorization: Bearer <token>``, hashes it, and looks up the active
    (non-revoked) host via a parameterized query. Returns the authenticated
    ``host_id``. Raises 401 on missing/unknown/revoked token and 403 when the
    token's ``host_id`` differs from the path ``hid``.
    """
    del config  # admin config not needed for host auth; accepted for symmetry

    async def _check(hid: str, request: Request) -> str:
        auth = request.headers.get("Authorization")
        if auth is None or not auth.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="missing host token")
        token = auth[7:].strip()
        if not token:
            raise HTTPException(status_code=401, detail="missing host token")

        token_hash = hash_token(token)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT host_id FROM hosts WHERE token_hash = $1 "
                "AND revoked_at IS NULL",
                token_hash,
            )
        if row is None:
            raise HTTPException(
                status_code=401, detail="invalid or revoked host token"
            )
        host_id = row["host_id"]
        if host_id != hid:
            raise HTTPException(
                status_code=403,
                detail="host token does not match path host_id",
            )
        request.state.host_id = host_id
        return host_id

    return _check
