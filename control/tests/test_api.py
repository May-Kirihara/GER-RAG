"""End-to-end tests for the control plane API (MV4 WP-2).

Docker-free tests (``create_app`` fail-fast) run unconditionally. The
``@pytest.mark.requires_postgres`` tests exercise the full FastAPI app against
the disposable Postgres via the ``app_client`` fixture (httpx ASGITransport).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from control.config import ControlConfig

ADMIN_KEY = "test-admin-key"


# --- helpers ----------------------------------------------------------------


def _ts() -> datetime:
    return datetime(2026, 7, 3, 10, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


async def _register_host(client, admin_headers, label="host-1") -> dict:
    r = await client.post(
        "/admin/hosts", json={"label": label}, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _register_universe(
    client,
    admin_headers,
    *,
    universe_id,
    host_id,
    tenant_id="default",
    embedder_id="cl-nagoya/ruri-v3-310m",
    embedder_version="abc123",
) -> dict:
    body = {
        "universe_id": universe_id,
        "tenant_id": tenant_id,
        "host_id": host_id,
        "embedder_id": embedder_id,
        "embedder_version": embedder_version,
    }
    r = await client.post(
        "/admin/universes", json=body, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    return r.json()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- docker-free: create_app fail-fast --------------------------------------


def test_create_app_empty_admin_key_raises() -> None:
    from control.api import create_app

    config = ControlConfig(database_url="postgresql://x", admin_key="")
    with pytest.raises(RuntimeError, match="admin_key must be non-empty"):
        create_app(config)


def test_create_app_non_localhost_raises() -> None:
    from control.api import create_app

    config = ControlConfig(
        database_url="postgresql://x",
        admin_key=ADMIN_KEY,
        listen_host="0.0.0.0",
    )
    with pytest.raises(SystemExit):
        create_app(config)


def test_create_app_ipv6_loopback_ok() -> None:
    # ::1 is an accepted loopback address; construction must succeed.
    from control.api import create_app

    config = ControlConfig(
        database_url="postgresql://x",
        admin_key=ADMIN_KEY,
        listen_host="::1",
    )
    app = create_app(config)
    assert app.title == "GaOTTT control plane"


# --- admin auth (DB-backed) -------------------------------------------------


@pytest.mark.requires_postgres
async def test_admin_auth_x_admin_key_header(app_client, admin_headers) -> None:
    client, _ = app_client
    r = await client.post(
        "/admin/tenants", json={"name": "Acme"}, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    assert "tenant_id" in r.json()


@pytest.mark.requires_postgres
async def test_admin_auth_bearer_header(app_client) -> None:
    client, _ = app_client
    r = await client.post(
        "/admin/tenants",
        json={"name": "Acme"},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
    )
    assert r.status_code == 200, r.text


@pytest.mark.requires_postgres
async def test_admin_auth_missing_401(app_client) -> None:
    client, _ = app_client
    r = await client.post("/admin/tenants", json={"name": "Acme"})
    assert r.status_code == 401


@pytest.mark.requires_postgres
async def test_admin_auth_wrong_401(app_client) -> None:
    client, _ = app_client
    r = await client.post(
        "/admin/tenants",
        json={"name": "Acme"},
        headers={"X-Admin-Key": "totally-wrong"},
    )
    assert r.status_code == 401


# --- host token auth (DB-backed) -------------------------------------------


@pytest.mark.requires_postgres
async def test_host_auth_correct_token_passes(app_client, admin_headers) -> None:
    client, _ = app_client
    host = await _register_host(client, admin_headers)
    r = await client.get(
        f"/hosts/{host['host_id']}/universes", headers=_bearer(host["token"])
    )
    assert r.status_code == 200, r.text
    assert r.json() == []


@pytest.mark.requires_postgres
async def test_host_auth_wrong_token_401(app_client, admin_headers) -> None:
    client, _ = app_client
    host = await _register_host(client, admin_headers)
    r = await client.get(
        f"/hosts/{host['host_id']}/universes",
        headers=_bearer("not-the-real-token"),
    )
    assert r.status_code == 401


@pytest.mark.requires_postgres
async def test_host_auth_missing_token_401(app_client, admin_headers) -> None:
    client, _ = app_client
    host = await _register_host(client, admin_headers)
    r = await client.get(f"/hosts/{host['host_id']}/universes")
    assert r.status_code == 401


@pytest.mark.requires_postgres
async def test_host_auth_revoked_token_401(app_client, admin_headers) -> None:
    client, _ = app_client
    host = await _register_host(client, admin_headers)
    # Revoke the host.
    rev = await client.delete(
        f"/admin/hosts/{host['host_id']}", headers=admin_headers
    )
    assert rev.status_code == 200, rev.text
    # Token is now permanently invalid.
    r = await client.get(
        f"/hosts/{host['host_id']}/universes", headers=_bearer(host["token"])
    )
    assert r.status_code == 401


@pytest.mark.requires_postgres
async def test_host_auth_path_hid_mismatch_403(app_client, admin_headers) -> None:
    client, _ = app_client
    host = await _register_host(client, admin_headers)
    r = await client.get(
        "/hosts/some-other-host/universes", headers=_bearer(host["token"])
    )
    assert r.status_code == 403


@pytest.mark.requires_postgres
async def test_host_revoke_is_idempotent(app_client, admin_headers) -> None:
    client, _ = app_client
    host = await _register_host(client, admin_headers)
    first = await client.delete(
        f"/admin/hosts/{host['host_id']}", headers=admin_headers
    )
    second = await client.delete(
        f"/admin/hosts/{host['host_id']}", headers=admin_headers
    )
    assert first.status_code == 200
    assert second.status_code == 200  # already-revoked is no-op success


# --- tenant + user CRUD -----------------------------------------------------


@pytest.mark.requires_postgres
async def test_tenant_crud(app_client, admin_headers) -> None:
    client, _ = app_client
    r = await client.post(
        "/admin/tenants", json={"name": "Acme"}, headers=admin_headers
    )
    assert r.status_code == 200
    tenant_id = r.json()["tenant_id"]

    listing = await client.get("/admin/tenants", headers=admin_headers)
    assert listing.status_code == 200
    ids = [t["tenant_id"] for t in listing.json()]
    assert tenant_id in ids
    # The bootstrap 'default' tenant is always present.
    assert "default" in ids


@pytest.mark.requires_postgres
async def test_user_create(app_client, admin_headers) -> None:
    client, _ = app_client
    tenant = (
        await client.post(
            "/admin/tenants", json={"name": "Acme"}, headers=admin_headers
        )
    ).json()
    r = await client.post(
        f"/admin/tenants/{tenant['tenant_id']}/users",
        json={"email": "ops@acme.test"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == tenant["tenant_id"]
    assert body["email"] == "ops@acme.test"
    assert body["user_id"]


@pytest.mark.requires_postgres
async def test_user_create_unknown_tenant_404(app_client, admin_headers) -> None:
    client, _ = app_client
    r = await client.post(
        "/admin/tenants/no-such-tenant/users",
        json={"email": "ops@acme.test"},
        headers=admin_headers,
    )
    assert r.status_code == 404


# --- universe CRUD ----------------------------------------------------------


@pytest.mark.requires_postgres
async def test_universe_register(app_client, admin_headers) -> None:
    client, _ = app_client
    host = await _register_host(client, admin_headers)
    await _register_universe(
        client,
        admin_headers,
        universe_id="uni-1",
        host_id=host["host_id"],
    )
    # Admin sees it.
    listing = await client.get("/admin/universes", headers=admin_headers)
    assert listing.status_code == 200
    ids = [u["universe_id"] for u in listing.json()]
    assert "uni-1" in ids
    # Host sees it via the host-facing list.
    r = await client.get(
        f"/hosts/{host['host_id']}/universes", headers=_bearer(host["token"])
    )
    assert r.status_code == 200
    assert [u["universe_id"] for u in r.json()] == ["uni-1"]


@pytest.mark.requires_postgres
async def test_universe_duplicate_409(app_client, admin_headers) -> None:
    client, _ = app_client
    host = await _register_host(client, admin_headers)
    await _register_universe(
        client, admin_headers, universe_id="dup", host_id=host["host_id"]
    )
    body = {
        "universe_id": "dup",
        "tenant_id": "default",
        "host_id": host["host_id"],
        "embedder_id": "x",
        "embedder_version": "y",
    }
    r = await client.post(
        "/admin/universes", json=body, headers=admin_headers
    )
    assert r.status_code == 409


@pytest.mark.requires_postgres
async def test_universe_logical_delete(app_client, admin_headers) -> None:
    client, _ = app_client
    host = await _register_host(client, admin_headers)
    await _register_universe(
        client, admin_headers, universe_id="delme", host_id=host["host_id"]
    )
    r = await client.delete(
        "/admin/universes/delme", headers=admin_headers
    )
    assert r.status_code == 200, r.text
    # The row remains with status='deleted' (logical delete only, J5).
    listing = await client.get("/admin/universes", headers=admin_headers)
    rows = {u["universe_id"]: u for u in listing.json()}
    assert "delme" in rows
    assert rows["delme"]["status"] == "deleted"
    # Host-facing list excludes deleted universes.
    host_list = await client.get(
        f"/hosts/{host['host_id']}/universes", headers=_bearer(host["token"])
    )
    assert "delme" not in [u["universe_id"] for u in host_list.json()]


@pytest.mark.requires_postgres
async def test_universe_register_fk_violation_400(
    app_client, admin_headers
) -> None:
    client, _ = app_client
    # host_id references a host that does not exist -> FK violation -> 400.
    body = {
        "universe_id": "orphan",
        "tenant_id": "default",
        "host_id": "ghost-host",
        "embedder_id": "x",
        "embedder_version": "y",
    }
    r = await client.post("/admin/universes", json=body, headers=admin_headers)
    assert r.status_code == 400


# --- usage batch ------------------------------------------------------------


def _usage_body(universe_id, tenant_id="default", batch_id=None, count=3):
    return {
        "batch_id": str(batch_id or uuid4()),
        "window_start": _iso(_ts()),
        "window_end": _iso(_ts()),
        "events": [
            {
                "universe_id": universe_id,
                "tenant_id": tenant_id,
                "event_type": "route_resolution",
                "count": count,
            }
        ],
    }


@pytest.mark.requires_postgres
async def test_usage_batch_idempotent(app_client, admin_headers) -> None:
    client, app = app_client
    host = await _register_host(client, admin_headers)
    await _register_universe(
        client, admin_headers, universe_id="u-usage", host_id=host["host_id"]
    )
    payload = _usage_body("u-usage")

    first = await client.post(
        f"/hosts/{host['host_id']}/usage",
        json=payload,
        headers=_bearer(host["token"]),
    )
    assert first.status_code == 200, first.text
    assert first.json()["events_ingested"] == 1

    # Same batch_id again -> idempotent replay, no new events.
    second = await client.post(
        f"/hosts/{host['host_id']}/usage",
        json=payload,
        headers=_bearer(host["token"]),
    )
    assert second.status_code == 200, second.text
    assert second.json()["events_ingested"] == 0

    async with app.state.pool.acquire() as conn:
        n_events = await conn.fetchval("SELECT count(*) FROM usage_events")
        n_batches = await conn.fetchval("SELECT count(*) FROM usage_batches")
    assert n_batches == 1
    assert n_events == 1  # unchanged after replay


@pytest.mark.requires_postgres
async def test_usage_batch_unknown_universe_400(
    app_client, admin_headers
) -> None:
    client, _ = app_client
    host = await _register_host(client, admin_headers)
    payload = _usage_body("universe-not-on-this-host")
    r = await client.post(
        f"/hosts/{host['host_id']}/usage",
        json=payload,
        headers=_bearer(host["token"]),
    )
    assert r.status_code == 400


@pytest.mark.requires_postgres
async def test_usage_batch_insert_order(app_client, admin_headers) -> None:
    client, app = app_client
    host = await _register_host(client, admin_headers)
    await _register_universe(
        client, admin_headers, universe_id="u-order", host_id=host["host_id"]
    )
    batch_id = str(uuid4())
    payload = _usage_body("u-order", batch_id=batch_id, count=2)
    r = await client.post(
        f"/hosts/{host['host_id']}/usage",
        json=payload,
        headers=_bearer(host["token"]),
    )
    assert r.status_code == 200, r.text

    async with app.state.pool.acquire() as conn:
        # usage_batches row exists (inserted before events; FK validates order).
        batch = await conn.fetchval(
            "SELECT batch_id FROM usage_batches WHERE batch_id = $1", batch_id
        )
        assert batch is not None
        # events reference the batch consistently.
        event_count = await conn.fetchval(
            "SELECT count(*) FROM usage_events WHERE batch_id = $1", batch_id
        )
        assert event_count == 1
        # tenant_id was denormalized from the universe row.
        tenant = await conn.fetchval(
            "SELECT tenant_id FROM usage_events WHERE batch_id = $1", batch_id
        )
        assert tenant == "default"


# --- sync -------------------------------------------------------------------


@pytest.mark.requires_postgres
async def test_sync_insert_new_universe(app_client, admin_headers) -> None:
    client, _ = app_client
    host = await _register_host(client, admin_headers)
    body = {
        "local_universes": [
            {
                "universe_id": "synced-new",
                "tenant_id": "default",
                "host_id": host["host_id"],
                "embedder_id": "cl-nagoya/ruri-v3-310m",
                "embedder_version": "v1",
            }
        ],
        "deleted_universes": [],
    }
    r = await client.post(
        f"/hosts/{host['host_id']}/sync",
        json=body,
        headers=_bearer(host["token"]),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"inserted": 1, "conflicts": 0}
    # The universe is now present in control.
    listing = await client.get("/admin/universes", headers=admin_headers)
    assert "synced-new" in [u["universe_id"] for u in listing.json()]


@pytest.mark.requires_postgres
async def test_sync_conflict_logged(app_client, admin_headers) -> None:
    client, app = app_client
    host = await _register_host(client, admin_headers)
    # Control has the universe with embedder_id "A".
    await _register_universe(
        client,
        admin_headers,
        universe_id="conflict-uni",
        host_id=host["host_id"],
        embedder_id="A",
    )
    # Local reports embedder_id "B" -> conflict, no mutation.
    body = {
        "local_universes": [
            {
                "universe_id": "conflict-uni",
                "tenant_id": "default",
                "host_id": host["host_id"],
                "embedder_id": "B",
                "embedder_version": "v1",
            }
        ],
        "deleted_universes": [],
    }
    r = await client.post(
        f"/hosts/{host['host_id']}/sync",
        json=body,
        headers=_bearer(host["token"]),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"inserted": 0, "conflicts": 1}

    async with app.state.pool.acquire() as conn:
        # audit_log captured the conflict.
        n = await conn.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'sync_conflict' "
            "AND target = $1",
            "conflict-uni",
        )
        assert n == 1
        # The universe was NOT mutated.
        emb = await conn.fetchval(
            "SELECT embedder_id FROM universes WHERE universe_id = $1",
            "conflict-uni",
        )
        assert emb == "A"


@pytest.mark.requires_postgres
async def test_sync_deleted_universe_conflict(
    app_client, admin_headers
) -> None:
    client, app = app_client
    host = await _register_host(client, admin_headers)
    await _register_universe(
        client,
        admin_headers,
        universe_id="will-delete",
        host_id=host["host_id"],
    )
    body = {
        "local_universes": [],
        "deleted_universes": ["will-delete"],
    }
    r = await client.post(
        f"/hosts/{host['host_id']}/sync",
        json=body,
        headers=_bearer(host["token"]),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"inserted": 0, "conflicts": 1}

    async with app.state.pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'sync_conflict' "
            "AND target = $1",
            "will-delete",
        )
        assert n == 1
        # NOT auto-deleted (J5).
        status = await conn.fetchval(
            "SELECT status FROM universes WHERE universe_id = $1",
            "will-delete",
        )
        assert status == "active"


@pytest.mark.requires_postgres
async def test_tenant_mapping_sync_unknown_tenant_warning(
    app_client, admin_headers
) -> None:
    # J11: control never auto-creates tenants; an unknown tenant in a sync
    # insert is recorded as a conflict (WARNING), not a hard error.
    client, app = app_client
    host = await _register_host(client, admin_headers)
    body = {
        "local_universes": [
            {
                "universe_id": "unknown-tenant-uni",
                "tenant_id": "no-such-tenant",
                "host_id": host["host_id"],
                "embedder_id": "x",
                "embedder_version": "y",
            }
        ],
        "deleted_universes": [],
    }
    r = await client.post(
        f"/hosts/{host['host_id']}/sync",
        json=body,
        headers=_bearer(host["token"]),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"inserted": 0, "conflicts": 1}

    async with app.state.pool.acquire() as conn:
        inserted = await conn.fetchval(
            "SELECT count(*) FROM universes WHERE universe_id = $1",
            "unknown-tenant-uni",
        )
        assert inserted == 0  # not inserted


# --- health -----------------------------------------------------------------


@pytest.mark.requires_postgres
async def test_health_ok(app_client) -> None:
    client, _ = app_client
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.requires_postgres
async def test_health_db_down_503(app_client) -> None:
    client, app = app_client

    class _BrokenAcquire:
        async def __aenter__(self):
            raise RuntimeError("simulated DB down")

        async def __aexit__(self, *exc):
            return False

    class _BrokenPool:
        def acquire(self):
            return _BrokenAcquire()

    real_pool = app.state.pool
    app.state.pool = _BrokenPool()
    try:
        r = await client.get("/health")
        assert r.status_code == 503
        assert r.json() == {"status": "unavailable"}
    finally:
        app.state.pool = real_pool


# --- audit transactional rollback (Codex B5) --------------------------------


@pytest.mark.requires_postgres
async def test_audit_log_transactional_rollback(
    app_client, admin_headers, monkeypatch
) -> None:
    client, app = app_client
    host = await _register_host(client, admin_headers)
    await _register_universe(
        client, admin_headers, universe_id="rollback-uni", host_id=host["host_id"]
    )
    payload = _usage_body("rollback-uni")

    # Force the audit INSERT to fail. Because audit runs in the SAME txn as the
    # usage batch/event inserts, the whole transaction rolls back (J12).
    import control.api as api_mod

    async def _failing_audit(conn, **kwargs):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(api_mod, "_audit", _failing_audit)

    # Starlette always re-raises unhandled exceptions after emitting the 500 so
    # test clients can observe them; the production client still receives a
    # 500. The Codex B5 acceptance is the *transactional rollback*, so we catch
    # the propagated error and then assert nothing was committed.
    with pytest.raises(RuntimeError, match="injected audit failure"):
        await client.post(
            f"/hosts/{host['host_id']}/usage",
            json=payload,
            headers=_bearer(host["token"]),
        )

    async with app.state.pool.acquire() as conn:
        n_batches = await conn.fetchval(
            "SELECT count(*) FROM usage_batches WHERE batch_id = $1",
            payload["batch_id"],
        )
        n_events = await conn.fetchval(
            "SELECT count(*) FROM usage_events WHERE batch_id = $1",
            payload["batch_id"],
        )
    assert n_batches == 0  # rolled back with the audit failure
    assert n_events == 0


# --- SQL injection (Codex missing #10) --------------------------------------


_SQLI_PAYLOADS = [
    "' OR '1'='1",
    "'; DROP TABLE tenants; --",
    "pg_sleep(5)--",
    "' UNION SELECT 1,2,3--",
    "x' OR 1=1; --",
]


@pytest.mark.requires_postgres
async def test_sql_injection_email(app_client, admin_headers) -> None:
    client, app = app_client
    tenant = (
        await client.post(
            "/admin/tenants", json={"name": "T"}, headers=admin_headers
        )
    ).json()
    for payload in _SQLI_PAYLOADS:
        r = await client.post(
            f"/admin/tenants/{tenant['tenant_id']}/users",
            json={"email": payload},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text
    async with app.state.pool.acquire() as conn:
        # tenants table still exists; the payloads are stored literally.
        exists = await conn.fetchval("SELECT to_regclass('public.tenants')")
        assert exists == "tenants"
        emails = [
            r["email"]
            for r in await conn.fetch("SELECT email FROM users")
        ]
    for payload in _SQLI_PAYLOADS:
        assert payload in emails


@pytest.mark.requires_postgres
async def test_sql_injection_label(app_client, admin_headers) -> None:
    client, app = app_client
    for payload in _SQLI_PAYLOADS:
        r = await client.post(
            "/admin/hosts", json={"label": payload}, headers=admin_headers
        )
        assert r.status_code == 200, r.text
    async with app.state.pool.acquire() as conn:
        labels = [r["label"] for r in await conn.fetch("SELECT label FROM hosts")]
        assert await conn.fetchval("SELECT to_regclass('public.hosts')") == "hosts"
    for payload in _SQLI_PAYLOADS:
        assert payload in labels


@pytest.mark.requires_postgres
async def test_sql_injection_universe_id(app_client, admin_headers) -> None:
    client, app = app_client
    host = await _register_host(client, admin_headers)
    for payload in _SQLI_PAYLOADS:
        await _register_universe(
            client,
            admin_headers,
            universe_id=payload,
            host_id=host["host_id"],
        )
    async with app.state.pool.acquire() as conn:
        uids = [
            r["universe_id"]
            for r in await conn.fetch("SELECT universe_id FROM universes")
        ]
        assert (
            await conn.fetchval("SELECT to_regclass('public.universes')")
            == "universes"
        )
    for payload in _SQLI_PAYLOADS:
        assert payload in uids


@pytest.mark.requires_postgres
async def test_sql_injection_tenant_id(app_client, admin_headers) -> None:
    client, app = app_client
    for payload in _SQLI_PAYLOADS:
        r = await client.post(
            "/admin/tenants",
            json={"name": payload, "tenant_id": payload},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text
    async with app.state.pool.acquire() as conn:
        tids = [
            r["tenant_id"]
            for r in await conn.fetch("SELECT tenant_id FROM tenants")
        ]
        assert (
            await conn.fetchval("SELECT to_regclass('public.tenants')") == "tenants"
        )
    for payload in _SQLI_PAYLOADS:
        assert payload in tids


@pytest.mark.requires_postgres
async def test_sql_injection_host_id_in_universe_register(
    app_client, admin_headers
) -> None:
    # host_id flows through a parameterized query; an injection string is a
    # value, not SQL. It must fail the FK cleanly (400) without executing.
    client, app = app_client
    for payload in _SQLI_PAYLOADS:
        body = {
            "universe_id": f"uni-{payload}",
            "tenant_id": "default",
            "host_id": payload,
            "embedder_id": "x",
            "embedder_version": "y",
        }
        r = await client.post(
            "/admin/universes", json=body, headers=admin_headers
        )
        assert r.status_code == 400, r.text
    async with app.state.pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT to_regclass('public.hosts')") == "hosts"
        )
        n = await conn.fetchval("SELECT count(*) FROM hosts")
        assert n == 0  # nothing injected; no host created


# --- tenant mapping: default tenant (J11) -----------------------------------


@pytest.mark.requires_postgres
async def test_tenant_mapping_default(app_client, admin_headers) -> None:
    client, _ = app_client
    host = await _register_host(client, admin_headers)
    # 'default' tenant is bootstrapped in 001_initial.sql -> FK resolves.
    r = await _register_universe(
        client,
        admin_headers,
        universe_id="default-tenant-uni",
        host_id=host["host_id"],
        tenant_id="default",
    )
    assert r["tenant_id"] == "default"
