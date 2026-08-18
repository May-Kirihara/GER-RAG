"""FastAPI control plane (MV4 WP-2).

``create_app(config)`` wires the auth boundaries (admin key, host token), the
lifespan (pool -> bootstrap -> migrate), and every endpoint from the WP-2 spec.
Mutating endpoints write their ``audit_log`` row inside the *same* transaction
as the domain mutation (J12 / Codex B5): an audit failure rolls the mutation
back. All user-supplied text flows through asyncpg parameterized queries
(``$1`` ...) so nothing is ever interpolated into SQL (Codex missing #10).
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .auth import hash_token, make_admin_checker, make_host_checker
from .config import ControlConfig
from .db import close_pool, create_pool
from .migrate import ensure_bootstrap, run_migrations
from .models import (
    CreateTenantBody,
    CreateUserBody,
    RegisterHostBody,
    RegisterUniverseBody,
    SyncBody,
    UsageBatchBody,
)

__all__ = ["create_app", "require_admin", "require_host"]

# Loopback hosts that the trust boundary permits. Anything else exits the
# process at app construction (J8) — the host-token auth model assumes the
# control plane is reachable only from the same host.
_LOCALHOST_HOSTS = {"127.0.0.1", "localhost", "::1"}

# --- vocabulary -------------------------------------------------------------
# audit_log.action is PAST tense (a completed auditable event);
# usage_events.event_type is BASE form (a classification). (Codex review #2)
AUDIT_TENANT_CREATED = "tenant_created"
AUDIT_USER_CREATED = "user_created"
AUDIT_HOST_REGISTERED = "host_registered"
AUDIT_HOST_REVOKED = "host_revoked"
AUDIT_HOST_TOKEN_ROTATED = "host_token_rotated"
AUDIT_UNIVERSE_REGISTERED = "universe_registered"
AUDIT_UNIVERSE_DELETED = "universe_deleted"
AUDIT_USAGE_RECEIVED = "usage_received"
AUDIT_SYNC_UNIVERSE_INSERTED = "sync_universe_inserted"
AUDIT_SYNC_CONFLICT = "sync_conflict"


# --- audit helper (monkeypatchable for the rollback test) -------------------


async def _audit(
    conn: asyncpg.Connection,
    *,
    tenant_id: str | None = None,
    actor: str = "admin",
    action: str,
    target: str | None = None,
    detail: dict | None = None,
) -> None:
    """Insert an audit_log row.

    ``detail`` is JSON-encoded and cast to JSONB server-side (no asyncpg codec
    registration needed). Must be called *inside* the caller's transaction so a
    failure rolls the domain mutation back with it (J12).
    """
    payload = json.dumps(detail) if detail is not None else None
    await conn.execute(
        "INSERT INTO audit_log (tenant_id, actor, action, target, detail) "
        "VALUES ($1, $2, $3, $4, $5::jsonb)",
        tenant_id,
        actor,
        action,
        target,
        payload,
    )


# --- dependencies -----------------------------------------------------------


async def require_admin(request: Request) -> None:
    """Admin auth dependency. Delegates to the checker stored on app.state."""
    await request.app.state.admin_checker(request)


async def require_host(hid: str, request: Request) -> str:
    """Host-token auth dependency. Returns the authenticated ``host_id``.

    The checker raises 403 if the token's host_id differs from the path ``hid``,
    so on success ``host_id == hid``.
    """
    return await request.app.state.host_checker(hid, request)


# --- app factory ------------------------------------------------------------


def _validate_listen_host(listen_host: str) -> None:
    if listen_host not in _LOCALHOST_HOSTS:
        raise SystemExit(
            f"control plane must bind to localhost (got {listen_host!r}); "
            "the auth model trusts the loopback boundary only (J8)"
        )


def create_app(config: ControlConfig) -> FastAPI:
    """Build the control plane FastAPI app.

    Fails fast at construction: empty ``admin_key`` -> ``RuntimeError``;
    non-loopback ``listen_host`` -> ``SystemExit`` (J8). The lifespan opens the
    asyncpg pool, bootstraps the migration ledger, applies pending migrations,
    and tears the pool down on shutdown. A migration/pool failure propagates
    so uvicorn never starts serving against a broken DB.
    """
    if not config.admin_key:
        raise RuntimeError("admin_key must be non-empty")
    _validate_listen_host(config.listen_host)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = await create_pool(
            config.database_url,
            min_size=config.db_pool_min_size,
            max_size=config.db_pool_max_size,
        )
        try:
            await ensure_bootstrap(pool)
            await run_migrations(pool, config.schema_dir)
        except Exception:
            await close_pool(pool)
            raise
        app.state.pool = pool
        app.state.admin_checker = make_admin_checker(config)
        app.state.host_checker = make_host_checker(config, pool)
        try:
            yield
        finally:
            await close_pool(pool)

    app = FastAPI(title="GaOTTT control plane", version="0.1.0", lifespan=lifespan)
    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:
    # -- health (no auth) ---------------------------------------------------
    @app.get("/health")
    async def health(request: Request):
        try:
            async with request.app.state.pool.acquire() as conn:
                value = await conn.fetchval("SELECT 1")
        except Exception:  # noqa: BLE001 - any DB failure = unavailable
            return JSONResponse(
                status_code=503, content={"status": "unavailable"}
            )
        if value != 1:
            return JSONResponse(
                status_code=503, content={"status": "unavailable"}
            )
        return {"status": "ok"}

    # -- tenants ------------------------------------------------------------
    @app.post("/admin/tenants", dependencies=[Depends(require_admin)])
    async def create_tenant(body: CreateTenantBody, request: Request):
        tenant_id = body.tenant_id or uuid.uuid4().hex
        pool = request.app.state.pool
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO tenants (tenant_id, name) VALUES ($1, $2)",
                    tenant_id,
                    body.name,
                )
                await _audit(
                    conn,
                    tenant_id=tenant_id,
                    action=AUDIT_TENANT_CREATED,
                    target=tenant_id,
                    detail={"name": body.name},
                )
        return {"tenant_id": tenant_id, "name": body.name}

    @app.get("/admin/tenants", dependencies=[Depends(require_admin)])
    async def list_tenants(request: Request):
        pool = request.app.state.pool
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT tenant_id, name, created_at FROM tenants ORDER BY created_at"
            )
        return [
            {"tenant_id": r["tenant_id"], "name": r["name"]}
            for r in rows
        ]

    # -- users --------------------------------------------------------------
    @app.post(
        "/admin/tenants/{tid}/users", dependencies=[Depends(require_admin)]
    )
    async def create_user(tid: str, body: CreateUserBody, request: Request):
        user_id = body.user_id or uuid.uuid4().hex
        pool = request.app.state.pool
        async with pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM tenants WHERE tenant_id = $1", tid
            )
            if not exists:
                raise HTTPException(
                    status_code=404, detail="unknown tenant"
                )
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO users (user_id, tenant_id, email) "
                    "VALUES ($1, $2, $3)",
                    user_id,
                    tid,
                    body.email,
                )
                await _audit(
                    conn,
                    tenant_id=tid,
                    action=AUDIT_USER_CREATED,
                    target=user_id,
                    detail={"email": body.email},
                )
        return {"user_id": user_id, "tenant_id": tid, "email": body.email}

    # -- hosts --------------------------------------------------------------
    @app.post("/admin/hosts", dependencies=[Depends(require_admin)])
    async def register_host(body: RegisterHostBody, request: Request):
        import secrets

        host_id = body.host_id or uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        token_hash = hash_token(token)
        pool = request.app.state.pool
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO hosts (host_id, label, token_hash) "
                    "VALUES ($1, $2, $3)",
                    host_id,
                    body.label,
                    token_hash,
                )
                await _audit(
                    conn,
                    actor="admin",
                    action=AUDIT_HOST_REGISTERED,
                    target=host_id,
                    detail={"label": body.label},
                )
        # Plaintext token is returned EXACTLY ONCE (J3); only the hash is stored.
        return {"host_id": host_id, "label": body.label, "token": token}

    @app.delete("/admin/hosts/{hid}", dependencies=[Depends(require_admin)])
    async def revoke_host(hid: str, request: Request):
        pool = request.app.state.pool
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.execute(
                    "UPDATE hosts SET revoked_at = now() "
                    "WHERE host_id = $1 AND revoked_at IS NULL",
                    hid,
                )
                # asyncpg returns 'UPDATE N'; only audit when a row changed.
                changed = _rows_from_status(result)
                if changed:
                    await _audit(
                        conn,
                        actor="admin",
                        action=AUDIT_HOST_REVOKED,
                        target=hid,
                    )
        return {"host_id": hid, "revoked": True}

    @app.post(
        "/admin/hosts/{hid}/rotate-token",
        dependencies=[Depends(require_admin)],
    )
    async def rotate_host_token(hid: str, request: Request):
        import secrets

        token = secrets.token_urlsafe(32)
        token_hash = hash_token(token)
        pool = request.app.state.pool
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Atomic existence check via affected-row count: a missing
                # host updates 0 rows, so we roll back (no audit, no token
                # change) and surface 404 — same in-txn HTTPException pattern
                # as register_universe. host_id is UNCHANGED on success so
                # existing universes.host_id rows stay valid — creating a new
                # host via POST /admin/hosts would orphan them (Codex B1).
                result = await conn.execute(
                    "UPDATE hosts SET token_hash = $1, revoked_at = NULL "
                    "WHERE host_id = $2",
                    token_hash,
                    hid,
                )
                if _rows_from_status(result) == 0:
                    raise HTTPException(
                        status_code=404, detail="unknown host"
                    )
                await _audit(
                    conn,
                    actor="admin",
                    action=AUDIT_HOST_TOKEN_ROTATED,
                    target=hid,
                )
        # Plaintext token returned EXACTLY ONCE (same pattern as
        # POST /admin/hosts, J3); only the hash is stored.
        return {"host_id": hid, "token": token}

    # -- universes ----------------------------------------------------------
    @app.post(
        "/admin/universes", dependencies=[Depends(require_admin)]
    )
    async def register_universe(body: RegisterUniverseBody, request: Request):
        pool = request.app.state.pool
        async with pool.acquire() as conn:
            try:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "INSERT INTO universes "
                        "(universe_id, tenant_id, owner_user_id, host_id, "
                        "embedder_id, embedder_version, status) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7) "
                        "ON CONFLICT (universe_id) DO NOTHING "
                        "RETURNING universe_id",
                        body.universe_id,
                        body.tenant_id,
                        body.owner_user_id,
                        body.host_id,
                        body.embedder_id,
                        body.embedder_version,
                        body.status or "active",
                    )
                    if row is None:
                        raise HTTPException(
                            status_code=409,
                            detail="universe_id already exists",
                        )
                    await _audit(
                        conn,
                        tenant_id=body.tenant_id,
                        action=AUDIT_UNIVERSE_REGISTERED,
                        target=body.universe_id,
                        detail={
                            "host_id": body.host_id,
                            "embedder_id": body.embedder_id,
                            "embedder_version": body.embedder_version,
                        },
                    )
            except asyncpg.ForeignKeyViolationError:
                raise HTTPException(
                    status_code=400,
                    detail="unknown tenant, host, or owner_user_id (FK)",
                )
        return {
            "universe_id": body.universe_id,
            "tenant_id": body.tenant_id,
            "owner_user_id": body.owner_user_id,
            "host_id": body.host_id,
            "embedder_id": body.embedder_id,
            "embedder_version": body.embedder_version,
            "status": body.status or "active",
        }

    @app.delete(
        "/admin/universes/{uid}", dependencies=[Depends(require_admin)]
    )
    async def delete_universe(uid: str, request: Request):
        # Logical delete ONLY (status='deleted'). Operational deprovisioning is
        # the local manifest's authority, not control's (J5).
        pool = request.app.state.pool
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE universes SET status = 'deleted' "
                    "WHERE universe_id = $1",
                    uid,
                )
                await _audit(
                    conn,
                    action=AUDIT_UNIVERSE_DELETED,
                    target=uid,
                )
        return {"universe_id": uid, "status": "deleted"}

    @app.get("/admin/universes", dependencies=[Depends(require_admin)])
    async def list_universes(request: Request):
        pool = request.app.state.pool
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT universe_id, tenant_id, owner_user_id, host_id, "
                "embedder_id, embedder_version, status, created_at "
                "FROM universes ORDER BY created_at"
            )
        return [dict(r) for r in rows]

    # -- host-facing: universes list ---------------------------------------
    @app.get("/hosts/{hid}/universes")
    async def host_universes(
        hid: str,
        request: Request,
        _host_id: str = Depends(require_host),
    ):
        # require_host guarantees host_id == hid (403 otherwise).
        pool = request.app.state.pool
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT universe_id, tenant_id, owner_user_id, host_id, "
                "embedder_id, embedder_version, status, created_at "
                "FROM universes WHERE host_id = $1 AND status = 'active' "
                "ORDER BY created_at",
                hid,
            )
        return [dict(r) for r in rows]

    # -- host-facing: usage batch ------------------------------------------
    @app.post("/hosts/{hid}/usage")
    async def post_usage(
        hid: str,
        body: UsageBatchBody,
        request: Request,
        _host_id: str = Depends(require_host),
    ):
        pool = request.app.state.pool
        events_ingested = 0
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Idempotent replay: if the batch_id already exists, commit
                # empty and return without re-inserting events (Codex B4).
                already = await conn.fetchval(
                    "SELECT 1 FROM usage_batches WHERE batch_id = $1",
                    body.batch_id,
                )
                if already:
                    events_ingested = 0
                else:
                    # Validate each event's universe belongs to this host; the
                    # event's tenant_id is denormalized from the universe row
                    # (the host cannot spoof it).
                    resolved: list[tuple[Any, str]] = []
                    for ev in body.events:
                        universe = await conn.fetchrow(
                            "SELECT tenant_id FROM universes "
                            "WHERE universe_id = $1 AND host_id = $2",
                            ev.universe_id,
                            hid,
                        )
                        if universe is None:
                            raise HTTPException(
                                status_code=400,
                                detail=f"unknown universe {ev.universe_id} on host {hid}",
                            )
                        resolved.append((ev, universe["tenant_id"]))
                    # usage_batches FIRST, then usage_events (FK load order,
                    # review #2 remaining gap), then audit (J12).
                    await conn.execute(
                        "INSERT INTO usage_batches "
                        "(batch_id, host_id, window_start, window_end, event_count) "
                        "VALUES ($1, $2, $3, $4, $5)",
                        body.batch_id,
                        hid,
                        body.window_start,
                        body.window_end,
                        len(body.events),
                    )
                    for ev, tenant_id in resolved:
                        await conn.execute(
                            "INSERT INTO usage_events "
                            "(batch_id, universe_id, tenant_id, host_id, "
                            "event_type, count, window_start, window_end) "
                            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                            body.batch_id,
                            ev.universe_id,
                            tenant_id,
                            hid,
                            ev.event_type,
                            ev.count,
                            body.window_start,
                            body.window_end,
                        )
                    await _audit(
                        conn,
                        actor=hid,
                        action=AUDIT_USAGE_RECEIVED,
                        target=str(body.batch_id),
                        detail={
                            "event_count": len(body.events),
                            "window_start": body.window_start.isoformat(),
                            "window_end": body.window_end.isoformat(),
                        },
                    )
                    events_ingested = len(body.events)
        return {"batch_id": str(body.batch_id), "events_ingested": events_ingested}

    # -- host-facing: sync --------------------------------------------------
    @app.post("/hosts/{hid}/sync")
    async def post_sync(
        hid: str,
        body: SyncBody,
        request: Request,
        _host_id: str = Depends(require_host),
    ):
        pool = request.app.state.pool
        inserted = 0
        conflicts = 0
        async with pool.acquire() as conn:
            async with conn.transaction():
                for entry in body.local_universes:
                    existing = await conn.fetchrow(
                        "SELECT tenant_id, host_id, embedder_id, "
                        "embedder_version, status "
                        "FROM universes WHERE universe_id = $1",
                        entry.universe_id,
                    )
                    if existing is None:
                        # Unknown to control: INSERT. Tenant must resolve
                        # (J11: control never auto-creates tenants).
                        tenant_ok = await conn.fetchval(
                            "SELECT 1 FROM tenants WHERE tenant_id = $1",
                            entry.tenant_id,
                        )
                        if not tenant_ok:
                            await _audit(
                                conn,
                                actor=hid,
                                action=AUDIT_SYNC_CONFLICT,
                                target=entry.universe_id,
                                detail={
                                    "reason": "unknown_tenant",
                                    "tenant_id": entry.tenant_id,
                                },
                            )
                            conflicts += 1
                            continue
                        await conn.execute(
                            "INSERT INTO universes "
                            "(universe_id, tenant_id, host_id, embedder_id, "
                            "embedder_version, status) "
                            "VALUES ($1, $2, $3, $4, $5, $6)",
                            entry.universe_id,
                            entry.tenant_id,
                            hid,
                            entry.embedder_id,
                            entry.embedder_version,
                            entry.status or "active",
                        )
                        await _audit(
                            conn,
                            tenant_id=entry.tenant_id,
                            actor=hid,
                            action=AUDIT_SYNC_UNIVERSE_INSERTED,
                            target=entry.universe_id,
                            detail={
                                "embedder_id": entry.embedder_id,
                                "embedder_version": entry.embedder_version,
                            },
                        )
                        inserted += 1
                    else:
                        # Exists: record a conflict if host/embedder differ.
                        # Do NOT mutate (J5).
                        if (
                            existing["host_id"] != hid
                            or existing["embedder_id"] != entry.embedder_id
                            or existing["embedder_version"]
                            != entry.embedder_version
                        ):
                            await _audit(
                                conn,
                                tenant_id=existing["tenant_id"],
                                actor=hid,
                                action=AUDIT_SYNC_CONFLICT,
                                target=entry.universe_id,
                                detail={
                                    "reason": "attribute_mismatch",
                                    "control": {
                                        "host_id": existing["host_id"],
                                        "embedder_id": existing["embedder_id"],
                                        "embedder_version": existing[
                                            "embedder_version"
                                        ],
                                    },
                                    "local": {
                                        "host_id": hid,
                                        "embedder_id": entry.embedder_id,
                                        "embedder_version": entry.embedder_version,
                                    },
                                },
                            )
                            conflicts += 1
                for uid in body.deleted_universes:
                    row = await conn.fetchrow(
                        "SELECT status FROM universes WHERE universe_id = $1",
                        uid,
                    )
                    if row is not None and row["status"] == "active":
                        await _audit(
                            conn,
                            actor=hid,
                            action=AUDIT_SYNC_CONFLICT,
                            target=uid,
                            detail={"reason": "local_deleted_control_active"},
                        )
                        conflicts += 1
        return {"inserted": inserted, "conflicts": conflicts}


def _rows_from_status(status: str) -> int:
    """Parse the trailing integer from an asyncpg command status ('UPDATE N')."""
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0
