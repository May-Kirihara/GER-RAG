"""MV4 WP-4 — supervisor <-> control plane integration tests.

Nine scenarios that drive the real supervisor FastAPI app together with the
real control plane FastAPI app against a disposable docker-compose Postgres.
All scenarios are ``@pytest.mark.requires_postgres`` and skip cleanly when
docker is unavailable.

Topology per test:

* Control plane — real ``control.api.create_app`` with a manually-created
  asyncpg pool + applied migrations (the ASGI test client does not drive the
  lifespan, mirroring ``control/tests/conftest.py``). Runs on a real
  ephemeral uvicorn port so scenario 3 can stop / restart it to exercise
  degraded mode.
* Supervisor — real ``gaottt.multiverse.supervisor.create_supervisor_app``
  with an MV3 ``MultiverseRegistry`` over an isolated tmp-path root. The
  control_client points at the real control-plane URL (no transport=
  injection), so its httpx calls traverse real loopback HTTP just like
  production. Backend spawn is short-circuited by patching
  ``_probe_backend_with_token`` to PROBE_OK (same pattern as
  ``test_supervisor.py``) so the tests stay fast and deterministic.
* Embedder service — the same session-scoped StubServiceEmbedder on a real
  uvicorn port that ``test_supervisor.py`` uses, so the supervisor's
  embedder-validation step passes.

Scenario map (per the WP-4 plan):

  1. supervisor <-> control: create universe via supervisor -> reconcile ->
     control sees the universe via GET /hosts/{hid}/universes.
  2. usage telemetry: /route x N -> flush_usage -> control usage_events has
     event_type='route_resolution' count=N (NOT operation count, J1=A).
  3. degraded mode: stop control plane -> /route still succeeds locally,
     usage spool accumulates -> restart control plane -> spool replay ->
     usage_events reflects accumulated counts.
  4. permanent auth failure: revoke host token -> 401 -> _auth_failed=True
     -> subsequent /route still succeeds locally, POST attempts stop,
     spool keeps accumulating, admin status exposes auth_failed=True.
  5. default 不変 (MV3 regression): control_plane_url="" -> control_client
     None -> supervisor behaves EXACTLY as MV3. Existing test_supervisor.py
     stays green (regression guard); this scenario also asserts the
     /admin/status shape and that POST /admin/universes without tenant_id
     still works.
  6. idempotent replay: spool a batch, POST succeeds, monkeypatch
     Path.unlink to simulate crash-before-delete, replay -> control plane
     sees batch_id once (idempotent).
  7. tenant mapping: control_default_tenant_id unset -> create universe ->
     arecord_event uses "default" -> control usage_events.tenant_id=
     'default' (FK resolves via the bootstrap tenant).
  8. audit transactional: trigger a usage POST, confirm audit_log row
     exists in same txn (assertion via direct DB query).
  9. supervisor status expose: after scenario 4's permanent auth failure,
     GET /admin/status returns {"control": {"auth_failed": True,
     "since": <float>, "spool_pending": <int>}}.

Acceptance criteria exercised: 1 (default 不変), 5 (MV3 regression), 13
(route_resolution naming), 11 (degraded mode), 12 (idempotency), 18
(tenant mapping), 16 (audit transactional — integration level).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import tempfile
import textwrap
import time
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

ADMIN_KEY = "integ-admin-key"
CONTROL_ADMIN_KEY = "test-admin-key"
HOST_LABEL = "integ-host"

# The schema dir bundled with the control package (control/control/schema).
# Resolved from the installed-package path so the test works under
# editable installs.
import control as _control_pkg  # noqa: E402

SCHEMA_DIR = Path(_control_pkg.__file__).resolve().parent / "schema"

# ---------------------------------------------------------------------------
# disposable Postgres (docker-compose) — ported from control/tests/conftest.py
# so this test file is self-contained (it lives in tests/integration/, not
# control/tests/, and cross-tree conftest discovery would be awkward).
# ---------------------------------------------------------------------------

HOST_PORT = os.environ.get("CONTROL_TEST_POSTGRES_HOST_PORT", "55432")
DSN = (
    f"postgresql://gaottt:dev-only@127.0.0.1:{HOST_PORT}/gaottt_control"
)
COMPOSE_PROJECT = f"gaottt-control-integration-{HOST_PORT}"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _disposable_compose_file() -> Path:
    """Standalone disposable compose file bound to HOST_PORT (see conftest docstring)."""
    compose = (
        Path(tempfile.gettempdir())
        / f"gaottt-control-integration-{HOST_PORT}.yml"
    )
    compose.write_text(
        textwrap.dedent(
            f"""\
            # Auto-generated disposable Postgres for GaOTTT control integration tests.
            services:
              postgres:
                image: postgres:16-alpine
                environment:
                  POSTGRES_DB: gaottt_control
                  POSTGRES_USER: gaottt
                  POSTGRES_PASSWORD: dev-only
                ports:
                  - "{HOST_PORT}:5432"
                healthcheck:
                  test: ["CMD-SHELL", "pg_isready -U gaottt -d gaottt_control"]
                  interval: 1s
                  timeout: 3s
                  retries: 30
            """
        )
    )
    return compose


def _compose(*args: str) -> subprocess.CompletedProcess:
    compose_file = _disposable_compose_file()
    return subprocess.run(
        [
            "docker", "compose",
            "-p", COMPOSE_PROJECT,
            "-f", str(compose_file),
            *args,
        ],
        capture_output=True, text=True,
    )


async def _wait_for_ready(timeout: float = 40.0) -> bool:
    import asyncpg

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            conn = await asyncpg.connect(DSN)
            await conn.close()
            return True
        except Exception:  # noqa: BLE001 - readiness poll
            await asyncio.sleep(0.5)
    return False


async def _reset_schema() -> None:
    import asyncpg

    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("DROP SCHEMA IF EXISTS public CASCADE;")
        await conn.execute("CREATE SCHEMA public;")
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def _compose_service():
    """Start the disposable Postgres once per session."""
    if not _docker_available():
        pytest.skip("docker not available")

    up = _compose("up", "-d")
    if up.returncode != 0:
        _compose("down")
        pytest.skip(f"docker compose up failed: {up.stderr.strip()}")

    if not asyncio.run(_wait_for_ready()):
        _compose("down")
        pytest.skip("disposable Postgres did not become ready in time")

    try:
        yield DSN
    finally:
        _compose("down")


@pytest.fixture
def disposable_postgres(_compose_service: str) -> str:
    """Per-test: drop & recreate the public schema for a clean slate."""
    asyncio.run(_reset_schema())
    return _compose_service


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_postgres: requires disposable Postgres via docker-compose "
        "(skipped if docker is unavailable)",
    )


# ---------------------------------------------------------------------------
# control plane: real uvicorn server on a free port
# ---------------------------------------------------------------------------
#
# The control plane is ALWAYS run on a real ephemeral uvicorn port so its
# lifespan (pool creation + migrations + auth-checker wiring) executes on
# uvicorn's own event loop. This avoids asyncpg's "attached to a different
# loop" pitfall: a pool created on the test loop cannot be shared with the
# uvicorn worker thread. For DB-level assertions we use a one-off
# ``asyncpg.connect()`` (no pool sharing) which is safe because it lives
# only for the duration of the query.

async def _build_control_app(dsn: str):
    """Build create_app against the disposable DSN.

    Returns just the app — pool/migrations/auth-checker wiring happens in
    the lifespan, which uvicorn runs when ``server.run()`` starts. The DSN
    is read from the ControlConfig the app closes over, so no manual pool
    setup is needed here.
    """
    from control.api import create_app
    from control.config import ControlConfig

    config = ControlConfig(
        database_url=dsn,
        admin_key=CONTROL_ADMIN_KEY,
        listen_host="127.0.0.1",
        listen_port=7881,
        schema_dir=SCHEMA_DIR,
    )
    return create_app(config)


async def _db_query(dsn: str, sql: str, *args):
    """Run a one-off SQL query against the disposable DB and return rows.

    Uses a fresh ``asyncpg.connect()`` (not a pool) so there is no
    cross-loop contamination with the uvicorn thread's pool."""
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(sql, *args)
        return rows
    finally:
        await conn.close()


async def _db_fetchval(dsn: str, sql: str, *args):
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval(sql, *args)
    finally:
        await conn.close()


async def _db_fetchrow(dsn: str, sql: str, *args):
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchrow(sql, *args)
    finally:
        await conn.close()


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _start_uvicorn(app, host: str = "127.0.0.1"):
    """Start a uvicorn server on a free port (background thread).

    Returns (server, thread, port). Stop with _stop_uvicorn.
    """
    import uvicorn

    port = _free_port()
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    import threading
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if server.started:
            return server, thread, port
        time.sleep(0.05)
    server.should_exit = True
    thread.join(timeout=5)
    raise RuntimeError("uvicorn did not start within 10s")


def _stop_uvicorn(server, thread) -> None:
    server.should_exit = True
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# supervisor-side helpers
# ---------------------------------------------------------------------------

def _stub_embedder():
    """Lazy import + construct the StubServiceEmbedder from MV3 helpers."""
    from tests.integration._supervisor_helpers import StubServiceEmbedder
    return StubServiceEmbedder(dimension=768)


def _probe_ok():
    """Patch seam: probe always returns OK so /route never spawns a backend."""
    from unittest.mock import AsyncMock
    from gaottt.multiverse.supervisor import PROBE_OK
    return patch(
        "gaottt.multiverse.supervisor._probe_backend_with_token",
        AsyncMock(return_value=PROBE_OK),
    )


def _make_supervisor_config(
    root: Path,
    embedder_url: str,
    *,
    port_range: tuple[int, int],
    control_plane_url: str = "",
    control_host_id: str = "",
    control_host_token: str = "",
    control_default_tenant_id: str = "",
    usage_spool_dir: str | None = None,
):
    """Build a GaOTTTConfig; control_* fields default to empty (MV3 mode)."""
    from gaottt.config import GaOTTTConfig

    start, end = port_range
    return GaOTTTConfig(
        multiverse_root=str(root),
        supervisor_admin_key=ADMIN_KEY,
        embedder_endpoint=embedder_url,
        universe_port_range_start=start,
        universe_port_range_end=end,
        supervisor_readiness_timeout=30.0,
        supervisor_spawn_concurrency=3,
        control_plane_url=control_plane_url,
        control_host_id=control_host_id,
        control_host_token=control_host_token,
        control_default_tenant_id=control_default_tenant_id,
        usage_spool_dir=usage_spool_dir or str(root / "spool"),
        # Disable background loops so tests are deterministic.
        control_sync_interval_seconds=9999.0,
        usage_push_interval_seconds=9999.0,
    )


async def _build_supervisor(config, registry, control_client):
    """Construct the supervisor app (does NOT call lifespan — ASGI client skips it)."""
    from gaottt.multiverse.supervisor import create_supervisor_app
    app = create_supervisor_app(config, registry, control_client)
    return app


@asynccontextmanager
async def asgi_client(app):
    from httpx import ASGITransport, AsyncClient
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@asynccontextmanager
async def http_client(base_url: str):
    """A real-loopback httpx client against a running uvicorn server.

    Used for control-plane admin setup (register host, revoke, etc.) that
    must hit the actual HTTP server (whose pool lives on uvicorn's loop)."""
    import httpx
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as c:
        yield c


def _admin_headers(key: str = ADMIN_KEY) -> dict[str, str]:
    return {"X-Admin-Key": key}


def _control_admin_headers(key: str = CONTROL_ADMIN_KEY) -> dict[str, str]:
    return {"X-Admin-Key": key}


async def _register_host(control_client, *, label: str = HOST_LABEL) -> dict:
    """Register a host on the control plane; returns {host_id, label, token}."""
    r = await control_client.post(
        "/admin/hosts", json={"label": label},
        headers=_control_admin_headers(),
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _create_universe(sup_client, owner: str = "owner",
                           tenant_id: str | None = None) -> dict:
    body = {"owner_label": owner}
    if tenant_id is not None:
        body["tenant_id"] = tenant_id
    r = await sup_client.post(
        "/admin/universes", json=body, headers=_admin_headers(),
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def embedder_url():
    """Start the StubServiceEmbedder service once for the whole session."""
    from gaottt.embedding.service import create_app
    from tests.integration._supervisor_helpers import (
        start_uvicorn, stop_uvicorn,
    )

    app = create_app(_stub_embedder())
    server, thread, port = start_uvicorn(app)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        stop_uvicorn(server, thread)


@pytest.fixture
def multiverse_root(tmp_path: Path) -> Path:
    root = tmp_path / "multiverse"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    (root / "universes").mkdir(parents=True, exist_ok=True)
    (root / "trash").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def port_range() -> tuple[int, int]:
    from tests.integration._supervisor_helpers import reserve_port_range
    return reserve_port_range()


@pytest.fixture(autouse=True)
def _kill_spawned_backends(port_range: tuple[int, int]):
    """Defensive: SIGTERM any mcp_server this test spawned on its port window."""
    from tests.integration._supervisor_helpers import kill_backends_in_range
    yield
    kill_backends_in_range(port_range[0], port_range[1])


# ===========================================================================
# Scenario 1: supervisor <-> control 連携
# ===========================================================================

@pytest.mark.requires_postgres
async def test_scenario1_supervisor_control_reconcile(
    disposable_postgres, embedder_url, multiverse_root, port_range,
):
    """Create universe via supervisor -> reconcile_with_control -> control
    plane knows about it (GET /hosts/{hid}/universes sees the row).

    Requires the universe's tenant_id (control's default "default") to FK-
    resolve — the bootstrap tenant in 001_initial.sql covers that."""
    from gaottt.multiverse.registry import MultiverseRegistry
    from gaottt.multiverse.control_client import ControlClient

    control_app = await _build_control_app(disposable_postgres)
    server, thread, control_port = _start_uvicorn(control_app)
    try:
        control_url = f"http://127.0.0.1:{control_port}"
        async with http_client(control_url) as control_http:
            host = await _register_host(control_http, label=HOST_LABEL)
            host_id = host["host_id"]
            host_token = host["token"]

        config = _make_supervisor_config(
            multiverse_root, embedder_url, port_range=port_range,
            control_plane_url=control_url,
            control_host_id=host_id,
            control_host_token=host_token,
        )
        registry = MultiverseRegistry(multiverse_root)
        await registry.initialize()
        try:
            cc = ControlClient(config, registry)
            sup_app = await _build_supervisor(config, registry, cc)
            with _probe_ok():
                async with asgi_client(sup_app) as sup:
                    body = await _create_universe(sup, owner="alice")
                    uid = body["universe_id"]

                    # Manually trigger reconcile so the universe is POSTed
                    # to /hosts/{hid}/sync. The control plane's sync handler
                    # inserts unknown universes (tenant_id "default" resolves
                    # via the bootstrap tenant).
                    await cc.reconcile_with_control()

            # Control plane now knows about the universe.
            async with http_client(control_url) as control_http:
                listing = await control_http.get(
                    f"/hosts/{host_id}/universes",
                    headers={"Authorization": f"Bearer {host_token}"},
                )
                assert listing.status_code == 200, listing.text
                rows = listing.json()
                uids = [r["universe_id"] for r in rows]
                assert uid in uids, (
                    f"universe {uid} missing from control listing: {rows}"
                )

            # Direct DB cross-check.
            row = await _db_fetchrow(
                disposable_postgres,
                "SELECT tenant_id, status FROM universes "
                "WHERE universe_id = $1",
                uid,
            )
            assert row is not None
            assert row["tenant_id"] == "default"
            assert row["status"] == "active"

            await cc.stop()
        finally:
            await registry.close()
    finally:
        _stop_uvicorn(server, thread)


# ===========================================================================
# Scenario 2: usage telemetry (J1=A — route_resolution, not operation count)
# ===========================================================================

@pytest.mark.requires_postgres
async def test_scenario2_usage_telemetry_route_resolution(
    disposable_postgres, embedder_url, multiverse_root, port_range,
):
    """/route x N -> flush_usage -> usage_events has event_type='route_resolution'
    with the expected count. Asserts the EXACT event_type name (J1=A — this
    is route-resolution activity telemetry, NOT an operation count)."""
    from gaottt.multiverse.registry import MultiverseRegistry
    from gaottt.multiverse.control_client import ControlClient

    control_app = await _build_control_app(disposable_postgres)
    server, thread, control_port = _start_uvicorn(control_app)
    try:
        control_url = f"http://127.0.0.1:{control_port}"
        async with http_client(control_url) as control_http:
            host = await _register_host(control_http)
            host_id = host["host_id"]
            host_token = host["token"]

        config = _make_supervisor_config(
            multiverse_root, embedder_url, port_range=port_range,
            control_plane_url=control_url,
            control_host_id=host_id,
            control_host_token=host_token,
        )
        registry = MultiverseRegistry(multiverse_root)
        await registry.initialize()
        try:
            cc = ControlClient(config, registry)
            sup_app = await _build_supervisor(config, registry, cc)
            with _probe_ok():
                async with asgi_client(sup_app) as sup:
                    body = await _create_universe(sup, owner="bob")
                    uid = body["universe_id"]
                    api_key = body["api_key"]

                    # First sync the universe into control so the usage
                    # POST can resolve its universe_id (FK to universes).
                    await cc.reconcile_with_control()

                    # Fire N routes.
                    N = 5
                    for _ in range(N):
                        r = await sup.post(
                            "/route", json={"api_key": api_key},
                        )
                        assert r.status_code == 200, r.text

            # Manually push the accumulated counter.
            await cc.flush_usage()
            await cc.stop()

            # Inspect control DB directly.
            rows = await _db_query(
                disposable_postgres,
                "SELECT event_type, count FROM usage_events "
                "WHERE universe_id = $1",
                uid,
            )
            assert rows, f"no usage_events for universe {uid}"
            route_rows = [r for r in rows if r["event_type"] == "route_resolution"]
            assert route_rows, (
                f"no route_resolution event; got {[dict(r) for r in rows]}"
            )
            assert len(route_rows) == 1, (
                f"expected 1 route_resolution row, got {len(route_rows)}"
            )
            assert route_rows[0]["count"] == N, (
                f"expected count={N}, got {route_rows[0]['count']}"
            )
        finally:
            await registry.close()
    finally:
        _stop_uvicorn(server, thread)


# ===========================================================================
# Scenario 3: degraded mode (control down -> spool -> restart -> replay)
# ===========================================================================

@pytest.mark.requires_postgres
async def test_scenario3_degraded_mode_spool_replay(
    disposable_postgres, embedder_url, multiverse_root, port_range,
):
    """Control plane is stopped mid-flight -> /route still succeeds locally
    (supervisor never blocks on control) -> usage spool accumulates on disk
    -> control plane restarts -> next flush replays the spool -> control DB
    reflects the accumulated counts."""
    from gaottt.multiverse.registry import MultiverseRegistry
    from gaottt.multiverse.control_client import ControlClient

    control_app = await _build_control_app(disposable_postgres)
    # First start: register host, register universe in control.
    server, thread, control_port = _start_uvicorn(control_app)
    try:
        control_url = f"http://127.0.0.1:{control_port}"
        async with http_client(control_url) as control_http:
            host = await _register_host(control_http)
            host_id = host["host_id"]
            host_token = host["token"]

        config = _make_supervisor_config(
            multiverse_root, embedder_url, port_range=port_range,
            control_plane_url=control_url,
            control_host_id=host_id,
            control_host_token=host_token,
        )
        registry = MultiverseRegistry(multiverse_root)
        await registry.initialize()
        try:
            cc = ControlClient(config, registry)
            sup_app = await _build_supervisor(config, registry, cc)
            with _probe_ok():
                async with asgi_client(sup_app) as sup:
                    body = await _create_universe(sup, owner="carol")
                    uid = body["universe_id"]
                    api_key = body["api_key"]
                    # Sync the universe into control before going dark.
                    await cc.reconcile_with_control()

            # --- STOP the control plane ---
            _stop_uvicorn(server, thread)

            # /route still succeeds locally; telemetry accumulates in memory.
            with _probe_ok():
                async with asgi_client(sup_app) as sup:
                    N_DOWN = 3
                    for _ in range(N_DOWN):
                        r = await sup.post(
                            "/route", json={"api_key": api_key},
                        )
                        assert r.status_code == 200, r.text

            # Flush: counter snapshots to a spool file, POST fails
            # (ConnectError), spool file is retained.
            await cc.flush_usage()
            spool_dir = Path(config.usage_spool_dir)
            spool_files = list(spool_dir.glob("*.jsonl"))
            assert spool_files, (
                "spool file must be retained when control plane is down"
            )

            # --- RESTART the control plane ---
            server2, thread2, control_port2 = _start_uvicorn(control_app)
            try:
                # The control URL stayed the same conceptually but the port
                # changed. Update the client's base URL (production would
                # point at a stable URL).
                cc._base_url = (
                    f"http://127.0.0.1:{control_port2}".rstrip("/")
                )
                # Force a fresh httpx client so the old connection pool does
                # not reuse the closed socket.
                if cc._client is not None:
                    await cc._client.aclose()
                cc._client = None

                # Replay the spool — control plane now receives the batch.
                await cc.replay_stale_spool()

                # Spool cleared on success.
                spool_files_after = list(spool_dir.glob("*.jsonl"))
                assert not spool_files_after, (
                    f"spool not drained after replay: {spool_files_after}"
                )

                # Control DB reflects the accumulated count.
                rows = await _db_query(
                    disposable_postgres,
                    "SELECT event_type, count FROM usage_events "
                    "WHERE universe_id = $1 "
                    "AND event_type = 'route_resolution'",
                    uid,
                )
                assert rows, "no route_resolution events after degraded replay"
                total = sum(r["count"] for r in rows)
                assert total == N_DOWN, (
                    f"expected {N_DOWN} route_resolution events, got {total}"
                )

                await cc.stop()
            finally:
                _stop_uvicorn(server2, thread2)
        finally:
            await registry.close()
    finally:
        # server/thread may already be stopped — best-effort.
        try:
            _stop_uvicorn(server, thread)
        except Exception:  # noqa: BLE001 - cleanup race
            pass


# ===========================================================================
# Scenario 4: permanent auth failure (401 -> _auth_failed -> spool keeps
# accumulating, POST attempts stop, /route still succeeds locally)
# ===========================================================================

@pytest.mark.requires_postgres
async def test_scenario4_permanent_auth_failure(
    disposable_postgres, embedder_url, multiverse_root, port_range,
):
    """Revoke the host token -> next usage POST gets 401 -> _auth_failed=True
    -> subsequent /route still succeeds locally (local authority), POST
    attempts stop (no wasted requests), but the spool keeps accumulating
    for post-rotation replay."""
    from gaottt.multiverse.registry import MultiverseRegistry
    from gaottt.multiverse.control_client import ControlClient

    control_app = await _build_control_app(disposable_postgres)
    server, thread, control_port = _start_uvicorn(control_app)
    try:
        control_url = f"http://127.0.0.1:{control_port}"
        async with http_client(control_url) as control_http:
            host = await _register_host(control_http)
            host_id = host["host_id"]
            host_token = host["token"]

        config = _make_supervisor_config(
            multiverse_root, embedder_url, port_range=port_range,
            control_plane_url=control_url,
            control_host_id=host_id,
            control_host_token=host_token,
        )
        registry = MultiverseRegistry(multiverse_root)
        await registry.initialize()
        try:
            cc = ControlClient(config, registry)
            sup_app = await _build_supervisor(config, registry, cc)
            with _probe_ok():
                async with asgi_client(sup_app) as sup:
                    body = await _create_universe(sup, owner="dave")
                    uid = body["universe_id"]
                    api_key = body["api_key"]
                    await cc.reconcile_with_control()

            # --- REVOKE the host token via control admin ---
            async with http_client(control_url) as control_http:
                rev = await control_http.delete(
                    f"/admin/hosts/{host_id}",
                    headers=_control_admin_headers(),
                )
                assert rev.status_code == 200, rev.text

            # Drive some routes — these still succeed locally because the
            # supervisor's local registry is authoritative.
            with _probe_ok():
                async with asgi_client(sup_app) as sup:
                    N = 4
                    for _ in range(N):
                        r = await sup.post(
                            "/route", json={"api_key": api_key},
                        )
                        assert r.status_code == 200, r.text

            # flush_usage attempts a POST -> 401 -> _auth_failed=True.
            await cc.flush_usage()
            assert cc._auth_failed is True, (
                "401 must set _auth_failed (permanent auth failure)"
            )
            assert cc._auth_failed_since is not None

            # Spool file retained (POST returned 401, not 200).
            spool_dir = Path(config.usage_spool_dir)
            pending_after_401 = list(spool_dir.glob("*.jsonl"))
            assert pending_after_401, (
                "spool must accumulate during permanent auth failure "
                "(awaiting credential rotation + restart)"
            )

            # Fire more routes — these still succeed locally.
            with _probe_ok():
                async with asgi_client(sup_app) as sup:
                    for _ in range(2):
                        r = await sup.post(
                            "/route", json={"api_key": api_key},
                        )
                        assert r.status_code == 200, r.text

            # Flush again — must NOT issue another POST (would be wasted).
            # The spool count grows; we use a spy on the client.post to
            # assert no HTTP request is made.
            import types
            original_post = cc._get_client().__class__.post
            post_calls: list = []

            async def _spy_post(self, url, **kwargs):
                post_calls.append(str(url))
                return await original_post(self, url, **kwargs)

            client_obj = cc._get_client()
            client_obj.post = types.MethodType(_spy_post, client_obj)

            await cc.flush_usage()
            assert post_calls == [], (
                "control_client must not POST after permanent auth failure "
                f"(calls observed: {post_calls})"
            )

            # Spool keeps growing.
            pending_final = list(spool_dir.glob("*.jsonl"))
            assert len(pending_final) > len(pending_after_401), (
                "spool must keep accumulating during permanent auth failure"
            )

            # No rows landed in usage_events (control rejected them).
            n = await _db_fetchval(
                disposable_postgres,
                "SELECT count(*) FROM usage_events WHERE universe_id = $1",
                uid,
            )
            assert n == 0, (
                f"usage_events should be empty (control rejected), got {n}"
            )

            await cc.stop()
        finally:
            await registry.close()
    finally:
        _stop_uvicorn(server, thread)


# ===========================================================================
# Scenario 5: default 不変 (MV3 regression) — control_plane_url="" -> None
# ===========================================================================

@pytest.mark.requires_postgres
async def test_scenario5_default_invariant_no_control(
    disposable_postgres, embedder_url, multiverse_root, port_range,
):
    """control_plane_url="" -> control_client=None -> supervisor behaves
    EXACTLY as MV3. POST /admin/universes without tenant_id still works;
    GET /admin/status returns {"control": None}; no control_client methods
    are invoked. (The full MV3 regression guard is test_supervisor.py
    running unchanged — exercised separately.)"""
    from gaottt.multiverse.registry import MultiverseRegistry
    from gaottt.multiverse.supervisor import create_supervisor_app

    # No control_* knobs set -> the supervisor should run in pure MV3 mode.
    config = _make_supervisor_config(
        multiverse_root, embedder_url, port_range=port_range,
    )
    assert config.control_plane_url == ""
    assert config.control_host_id == ""
    assert config.control_host_token == ""

    registry = MultiverseRegistry(multiverse_root)
    await registry.initialize()
    try:
        # No control_client passed — production _main() also passes None
        # when the 3-point gate is incomplete.
        sup_app = create_supervisor_app(config, registry, None)
        sup = sup_app.state.supervisor
        assert sup._control is None, (
            "supervisor must hold no control_client when none was passed"
        )

        with _probe_ok():
            async with asgi_client(sup_app) as sup_client:
                # MV3 universe-creation still works without tenant_id.
                body = await _create_universe(sup_client, owner="erin")
                uid = body["universe_id"]
                api_key = body["api_key"]

                # /route works (probe mocked to OK).
                r = await sup_client.post(
                    "/route", json={"api_key": api_key},
                )
                assert r.status_code == 200, r.text

                # Admin status reports control: None.
                status = await sup_client.get(
                    "/admin/status", headers=_admin_headers(),
                )
                assert status.status_code == 200
                assert status.json() == {"control": None}, status.json()

        # Delete needs PROBE_DOWN (the supervisor never spawned a real
        # backend, so PROBE_OK would make it think one is alive and refuse
        # the delete with 409 _BackendAliveConflict). Same pattern as
        # test_supervisor.py::test_delete_moves_to_trash.
        from unittest.mock import AsyncMock
        from gaottt.multiverse.supervisor import PROBE_DOWN
        with patch(
            "gaottt.multiverse.supervisor._probe_backend_with_token",
            AsyncMock(return_value=PROBE_DOWN),
        ):
            async with asgi_client(sup_app) as sup_client:
                r = await sup_client.delete(
                    f"/admin/universes/{uid}", headers=_admin_headers(),
                )
                assert r.status_code == 200, r.text
    finally:
        await registry.close()


# ===========================================================================
# Scenario 6: idempotent replay (crash-after-POST-before-delete)
# ===========================================================================

@pytest.mark.requires_postgres
async def test_scenario6_idempotent_replay_crash_before_delete(
    disposable_postgres, embedder_url, multiverse_root, port_range,
):
    """Spool a batch -> POST succeeds -> simulate crash-before-delete
    (monkeypatch Path.unlink to no-op) -> next replay POSTs the same
    batch_id again -> control plane deduplicates (usage_batches.batch_id
    is PK) -> control DB sees the batch exactly once."""
    from gaottt.multiverse.registry import MultiverseRegistry
    from gaottt.multiverse.control_client import ControlClient

    control_app = await _build_control_app(disposable_postgres)
    server, thread, control_port = _start_uvicorn(control_app)
    try:
        control_url = f"http://127.0.0.1:{control_port}"
        async with http_client(control_url) as control_http:
            host = await _register_host(control_http)
            host_id = host["host_id"]
            host_token = host["token"]

        config = _make_supervisor_config(
            multiverse_root, embedder_url, port_range=port_range,
            control_plane_url=control_url,
            control_host_id=host_id,
            control_host_token=host_token,
        )
        registry = MultiverseRegistry(multiverse_root)
        await registry.initialize()
        try:
            cc = ControlClient(config, registry)
            sup_app = await _build_supervisor(config, registry, cc)
            with _probe_ok():
                async with asgi_client(sup_app) as sup:
                    body = await _create_universe(sup, owner="frank")
                    uid = body["universe_id"]
                    api_key = body["api_key"]
                    await cc.reconcile_with_control()
                    # One route to populate the counter.
                    r = await sup.post(
                        "/route", json={"api_key": api_key},
                    )
                    assert r.status_code == 200

            # Crash-after-POST: simulate by stubbing out Path.unlink so the
            # spool file survives a successful POST. The POST goes through,
            # but the spool remains for "replay".
            original_unlink = Path.unlink

            def noop_unlink(self, *args, **kwargs):
                pass

            Path.unlink = noop_unlink  # type: ignore[method-assign]
            try:
                await cc.flush_usage()
            finally:
                Path.unlink = original_unlink  # type: ignore[method-assign]

            spool_dir = Path(config.usage_spool_dir)
            retained = list(spool_dir.glob("*.jsonl"))
            assert len(retained) == 1, (
                f"expected 1 retained spool, got {len(retained)}"
            )

            # Capture the batch_id before replay.
            import json
            payload = json.loads(retained[0].read_text())
            batch_id = payload["batch_id"]

            # Replay — same batch_id POSTed again. Control plane must
            # deduplicate via usage_batches.batch_id PK.
            await cc.replay_stale_spool()

            # The spool file is now deleted (real unlink ran).
            retained_after = list(spool_dir.glob("*.jsonl"))
            assert not retained_after, (
                f"spool should be cleared after replay: {retained_after}"
            )

            # Control DB saw the batch exactly ONCE.
            n_batches = await _db_fetchval(
                disposable_postgres,
                "SELECT count(*) FROM usage_batches WHERE batch_id = $1::uuid",
                batch_id,
            )
            assert n_batches == 1, (
                f"batch should appear once, got {n_batches}"
            )
            n_events = await _db_fetchval(
                disposable_postgres,
                "SELECT count(*) FROM usage_events "
                "WHERE universe_id = $1 AND batch_id = $2::uuid",
                uid, batch_id,
            )
            assert n_events >= 1, (
                f"expected >=1 usage_events, got {n_events}"
            )

            await cc.stop()
        finally:
            await registry.close()
    finally:
        _stop_uvicorn(server, thread)


# ===========================================================================
# Scenario 7: tenant mapping (control_default_tenant_id unset -> "default")
# ===========================================================================

@pytest.mark.requires_postgres
async def test_scenario7_tenant_mapping_default(
    disposable_postgres, embedder_url, multiverse_root, port_range,
):
    """control_default_tenant_id unset -> arecord_event uses "default" ->
    control usage_events.tenant_id='default' (FK resolves via the bootstrap
    tenant in 001_initial.sql)."""
    from gaottt.multiverse.registry import MultiverseRegistry
    from gaottt.multiverse.control_client import ControlClient

    control_app = await _build_control_app(disposable_postgres)
    server, thread, control_port = _start_uvicorn(control_app)
    try:
        control_url = f"http://127.0.0.1:{control_port}"
        async with http_client(control_url) as control_http:
            host = await _register_host(control_http)
            host_id = host["host_id"]
            host_token = host["token"]

        # Explicitly leave control_default_tenant_id empty -> J11 fallback
        # to "default".
        config = _make_supervisor_config(
            multiverse_root, embedder_url, port_range=port_range,
            control_plane_url=control_url,
            control_host_id=host_id,
            control_host_token=host_token,
            control_default_tenant_id="",
        )
        assert config.control_default_tenant_id == ""

        registry = MultiverseRegistry(multiverse_root)
        await registry.initialize()
        try:
            cc = ControlClient(config, registry)
            sup_app = await _build_supervisor(config, registry, cc)
            with _probe_ok():
                async with asgi_client(sup_app) as sup:
                    body = await _create_universe(sup, owner="grace")
                    uid = body["universe_id"]
                    api_key = body["api_key"]
                    await cc.reconcile_with_control()
                    r = await sup.post(
                        "/route", json={"api_key": api_key},
                    )
                    assert r.status_code == 200

            await cc.flush_usage()
            await cc.stop()

            row = await _db_fetchrow(
                disposable_postgres,
                "SELECT tenant_id, event_type FROM usage_events "
                "WHERE universe_id = $1 AND event_type = 'route_resolution'",
                uid,
            )
            assert row is not None, "no route_resolution row"
            assert row["tenant_id"] == "default", (
                f"expected 'default', got {row['tenant_id']}"
            )
        finally:
            await registry.close()
    finally:
        _stop_uvicorn(server, thread)


# ===========================================================================
# Scenario 8: audit transactional (usage POST writes audit_log in same txn)
# ===========================================================================

@pytest.mark.requires_postgres
async def test_scenario8_audit_transactional(
    disposable_postgres, embedder_url, multiverse_root, port_range,
):
    """A successful usage POST writes an audit_log row in the SAME transaction
    as the usage_batches / usage_events INSERT (J12). Integration assertion via
    direct DB query — the unit-level rollback test is in WP-2 test_api.py."""
    from gaottt.multiverse.registry import MultiverseRegistry
    from gaottt.multiverse.control_client import ControlClient

    control_app = await _build_control_app(disposable_postgres)
    server, thread, control_port = _start_uvicorn(control_app)
    try:
        control_url = f"http://127.0.0.1:{control_port}"
        async with http_client(control_url) as control_http:
            host = await _register_host(control_http)
            host_id = host["host_id"]
            host_token = host["token"]

        config = _make_supervisor_config(
            multiverse_root, embedder_url, port_range=port_range,
            control_plane_url=control_url,
            control_host_id=host_id,
            control_host_token=host_token,
        )
        registry = MultiverseRegistry(multiverse_root)
        await registry.initialize()
        try:
            cc = ControlClient(config, registry)
            sup_app = await _build_supervisor(config, registry, cc)
            with _probe_ok():
                async with asgi_client(sup_app) as sup:
                    body = await _create_universe(sup, owner="henry")
                    api_key = body["api_key"]
                    await cc.reconcile_with_control()
                    r = await sup.post(
                        "/route", json={"api_key": api_key},
                    )
                    assert r.status_code == 200

            await cc.flush_usage()
            await cc.stop()

            # The audit_log should have at least one usage_received row
            # attributable to this host, written in the same txn as the
            # usage_batches/usage_events INSERTs.
            audit = await _db_query(
                disposable_postgres,
                "SELECT actor, action, target FROM audit_log "
                "WHERE actor = $1 AND action = 'usage_received'",
                host_id,
            )
            assert audit, (
                f"no usage_received audit_log row for host {host_id}"
            )
        finally:
            await registry.close()
    finally:
        _stop_uvicorn(server, thread)


# ===========================================================================
# Scenario 9: supervisor status exposes permanent auth failure state
# ===========================================================================

@pytest.mark.requires_postgres
async def test_scenario9_admin_status_exposes_auth_failure(
    disposable_postgres, embedder_url, multiverse_root, port_range,
):
    """After a permanent auth failure (401), GET /admin/status returns
    {"control": {"auth_failed": True, "since": <float>,
    "spool_pending": <int>}} so an operator can detect the revoked token
    early (review #2 remaining gap)."""
    from gaottt.multiverse.registry import MultiverseRegistry
    from gaottt.multiverse.control_client import ControlClient

    control_app = await _build_control_app(disposable_postgres)
    server, thread, control_port = _start_uvicorn(control_app)
    try:
        control_url = f"http://127.0.0.1:{control_port}"
        async with http_client(control_url) as control_http:
            host = await _register_host(control_http)
            host_id = host["host_id"]
            host_token = host["token"]

        config = _make_supervisor_config(
            multiverse_root, embedder_url, port_range=port_range,
            control_plane_url=control_url,
            control_host_id=host_id,
            control_host_token=host_token,
        )
        registry = MultiverseRegistry(multiverse_root)
        await registry.initialize()
        try:
            cc = ControlClient(config, registry)
            sup_app = await _build_supervisor(config, registry, cc)
            with _probe_ok():
                async with asgi_client(sup_app) as sup:
                    body = await _create_universe(sup, owner="ivan")
                    api_key = body["api_key"]
                    await cc.reconcile_with_control()

            # Sanity: status before failure is clean.
            async with asgi_client(sup_app) as sup:
                status = await sup.get(
                    "/admin/status", headers=_admin_headers(),
                )
                assert status.status_code == 200
                payload = status.json()
                assert payload["control"]["auth_failed"] is False

            # Revoke the host token.
            async with http_client(control_url) as control_http:
                rev = await control_http.delete(
                    f"/admin/hosts/{host_id}",
                    headers=_control_admin_headers(),
                )
                assert rev.status_code == 200

            # Fire a route + flush to trigger the 401.
            with _probe_ok():
                async with asgi_client(sup_app) as sup:
                    r = await sup.post(
                        "/route", json={"api_key": api_key},
                    )
                    assert r.status_code == 200
            await cc.flush_usage()
            assert cc._auth_failed is True

            # Status now exposes the permanent auth failure.
            async with asgi_client(sup_app) as sup:
                status = await sup.get(
                    "/admin/status", headers=_admin_headers(),
                )
                assert status.status_code == 200
                payload = status.json()
                ctrl = payload["control"]
                assert ctrl["auth_failed"] is True, ctrl
                assert ctrl["since"] is not None, ctrl
                assert isinstance(ctrl["since"], float), ctrl
                assert ctrl["spool_pending"] >= 1, ctrl

            await cc.stop()
        finally:
            await registry.close()
    finally:
        _stop_uvicorn(server, thread)


# ===========================================================================
# Scenario 10: rotate-token recovery (Codex B1) — same-host_id credential
# rotation after revocation, accumulated spool drains, host_id unchanged.
# ===========================================================================

@pytest.mark.requires_postgres
async def test_scenario10_rotate_token_recovery(
    disposable_postgres, embedder_url, multiverse_root, port_range,
):
    """B1: revoke host token → spool accumulates (401, _auth_failed) →
    ``POST /admin/hosts/{hid}/rotate-token`` on the SAME host_id → fresh
    client with the new token replays the spool → usage_events reflects
    the accumulated counts. Asserts host_id is UNCHANGED across the
    rotation (the fix for Codex B1: registering a new host would orphan
    ``universes.host_id`` rows)."""
    from gaottt.multiverse.registry import MultiverseRegistry
    from gaottt.multiverse.control_client import ControlClient

    control_app = await _build_control_app(disposable_postgres)
    server, thread, control_port = _start_uvicorn(control_app)
    try:
        control_url = f"http://127.0.0.1:{control_port}"
        async with http_client(control_url) as control_http:
            host = await _register_host(control_http)
            host_id = host["host_id"]
            original_token = host["token"]

        config = _make_supervisor_config(
            multiverse_root, embedder_url, port_range=port_range,
            control_plane_url=control_url,
            control_host_id=host_id,
            control_host_token=original_token,
        )
        registry = MultiverseRegistry(multiverse_root)
        await registry.initialize()
        try:
            cc = ControlClient(config, registry)
            sup_app = await _build_supervisor(config, registry, cc)
            with _probe_ok():
                async with asgi_client(sup_app) as sup:
                    body = await _create_universe(sup, owner="kate")
                    uid = body["universe_id"]
                    api_key = body["api_key"]
                    await cc.reconcile_with_control()

            # --- REVOKE the host token ---
            async with http_client(control_url) as control_http:
                rev = await control_http.delete(
                    f"/admin/hosts/{host_id}",
                    headers=_control_admin_headers(),
                )
                assert rev.status_code == 200, rev.text

            # Drive routes (still succeed locally) + flush → 401 →
            # _auth_failed, spool accumulates for post-rotation replay.
            with _probe_ok():
                async with asgi_client(sup_app) as sup:
                    N = 3
                    for _ in range(N):
                        r = await sup.post(
                            "/route", json={"api_key": api_key},
                        )
                        assert r.status_code == 200, r.text
            await cc.flush_usage()
            assert cc._auth_failed is True, (
                "revoke must trigger 401 → _auth_failed"
            )
            spool_dir = Path(config.usage_spool_dir)
            pending = list(spool_dir.glob("*.jsonl"))
            assert pending, "spool must accumulate while token revoked"

            # No rows landed yet (control rejected the batch via 401).
            n_before = await _db_fetchval(
                disposable_postgres,
                "SELECT count(*) FROM usage_events WHERE universe_id = $1",
                uid,
            )
            assert n_before == 0, (
                f"usage_events should be empty pre-rotation, got {n_before}"
            )

            # --- ROTATE the token on the SAME host_id (B1 fix) ---
            async with http_client(control_url) as control_http:
                rot = await control_http.post(
                    f"/admin/hosts/{host_id}/rotate-token",
                    headers=_control_admin_headers(),
                )
                assert rot.status_code == 200, rot.text
                rot_body = rot.json()
                assert rot_body["host_id"] == host_id, (
                    "rotate-token must return the SAME host_id (B1: a new "
                    "host_id would orphan universes.host_id rows)"
                )
                new_token = rot_body["token"]
                assert new_token, "rotate-token must return a plaintext token"
                assert new_token != original_token, (
                    "rotated token must differ from the original"
                )

            # The old token is now invalid (its hash no longer matches).
            async with http_client(control_url) as control_http:
                old_check = await control_http.get(
                    f"/hosts/{host_id}/universes",
                    headers={"Authorization": f"Bearer {original_token}"},
                )
                assert old_check.status_code == 401, (
                    f"old token must be invalid after rotation, "
                    f"got {old_check.status_code}"
                )

            # audit_log recorded the rotation atomically (J12).
            audit = await _db_fetchval(
                disposable_postgres,
                "SELECT count(*) FROM audit_log "
                "WHERE action = 'host_token_rotated' AND target = $1",
                host_id,
            )
            assert audit == 1, (
                f"expected 1 rotation audit row, got {audit}"
            )

            # Unknown host → 404 (and no audit row written).
            async with http_client(control_url) as control_http:
                missing = await control_http.post(
                    "/admin/hosts/does-not-exist/rotate-token",
                    headers=_control_admin_headers(),
                )
                assert missing.status_code == 404, (
                    f"rotate-token on unknown host must 404, "
                    f"got {missing.status_code}"
                )
            no_audit = await _db_fetchval(
                disposable_postgres,
                "SELECT count(*) FROM audit_log "
                "WHERE action = 'host_token_rotated' "
                "AND target = 'does-not-exist'",
            )
            assert no_audit == 0, (
                "404 rotation must not write an audit row (txn rolled back)"
            )

            # --- RESTART supervisor with the new token ---
            # A real restart constructs a fresh ControlClient (new token,
            # fresh _auth_failed=False) pointing at the SAME on-disk spool.
            new_config = _make_supervisor_config(
                multiverse_root, embedder_url, port_range=port_range,
                control_plane_url=control_url,
                control_host_id=host_id,
                control_host_token=new_token,
                usage_spool_dir=str(spool_dir),
            )
            cc2 = ControlClient(new_config, registry)
            assert cc2._auth_failed is False, (
                "fresh client must start with auth_failed=False"
            )

            # Replay the accumulated spool — drains now that the new
            # token authenticates against the (unchanged) host_id.
            await cc2.replay_stale_spool()

            pending_after = list(spool_dir.glob("*.jsonl"))
            assert not pending_after, (
                f"spool must drain after rotation + replay: {pending_after}"
            )

            # usage_events now reflects the accumulated route resolutions.
            rows = await _db_query(
                disposable_postgres,
                "SELECT event_type, count FROM usage_events "
                "WHERE universe_id = $1 AND event_type = 'route_resolution'",
                uid,
            )
            assert rows, "no route_resolution events after recovery"
            total = sum(r["count"] for r in rows)
            assert total == N, (
                f"expected {N} route_resolution events after drain, got {total}"
            )

            await cc2.stop()
            await cc.stop()
        finally:
            await registry.close()
    finally:
        _stop_uvicorn(server, thread)
