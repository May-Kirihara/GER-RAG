"""Pytest configuration for the control plane test suite (MV4 WP-1 + WP-2).

Two tiers of tests:
  * docker-free unit tests (pure functions, Pydantic validation) — always run.
  * ``@pytest.mark.requires_postgres`` tests — need the disposable Postgres
    from ``control/compose.yml``; skipped automatically when docker is not
    available.

The :func:`disposable_postgres` fixture starts the compose service once per
session and yields a clean DSN per test (the public schema is dropped and
recreated before each test so migrations run from scratch).

Host-port override
------------------
``compose.yml`` binds ``55432:5432`` by default. On machines where 55432 is
held by another long-running container (e.g. the dev box's ``infra-postgres-1``),
set ``CONTROL_TEST_POSTGRES_HOST_PORT`` to a free port. A **standalone**
disposable compose file is generated in the system tempdir — replicating
``compose.yml`` verbatim but with the chosen host port — and used as the sole
``-f`` input. (A plain ``override`` file would *union* its ``ports`` list with
the base file's, still binding 55432; a full standalone file avoids that merge
quirk entirely.) A unique compose project name
(``gaottt-control-test-${PORT}``) guarantees the disposable container never
collides with any other stack on the host.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

COMPOSE_FILE = Path(__file__).resolve().parent.parent / "compose.yml"

# Host-side port for the disposable Postgres. Override with
# CONTROL_TEST_POSTGRES_HOST_PORT when the default 55432 is occupied.
HOST_PORT = os.environ.get("CONTROL_TEST_POSTGRES_HOST_PORT", "55432")
DSN = f"postgresql://gaottt:dev-only@127.0.0.1:{HOST_PORT}/gaottt_control"

# Unique compose project so this stack never collides with any other
# `docker compose` project on the host (e.g. the `infra` stack holding 55432).
COMPOSE_PROJECT = f"gaottt-control-test-{HOST_PORT}"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_postgres: requires disposable Postgres via docker-compose "
        "(skipped if docker is unavailable)",
    )


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _disposable_compose_file() -> Path:
    """Generate a standalone disposable compose file bound to HOST_PORT.

    The file replicates ``compose.yml`` verbatim except for the host-side
    port. It is used as the *only* ``-f`` input so the generated port mapping
    fully replaces (rather than unions with) the base ``55432:5432`` mapping —
    a plain override file would append its ``ports`` list and still try to bind
    55432. Written under a port-specific name in the system tempdir so repeated
    runs reuse it harmlessly.
    """
    compose = Path(tempfile.gettempdir()) / f"gaottt-control-test-{HOST_PORT}.yml"
    compose.write_text(
        textwrap.dedent(
            f"""\
            # Auto-generated disposable Postgres for GaOTTT control plane tests.
            # Mirrors control/compose.yml but binds CONTROL_TEST_POSTGRES_HOST_PORT.
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
            "docker",
            "compose",
            "-p",
            COMPOSE_PROJECT,
            "-f",
            str(compose_file),
            *args,
        ],
        capture_output=True,
        text=True,
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
        except Exception:  # noqa: BLE001 - readiness poll, any error = not ready
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
    """Start the disposable Postgres once per session; tear down at the end.

    Skips the whole session's worth of ``requires_postgres`` tests if docker
    is unavailable or the service does not become ready in time.
    """
    if not _docker_available():
        pytest.skip("docker not available", allow_module_level=False)

    up = _compose("up", "-d")
    if up.returncode != 0:
        # `up -d` can leave a partially-created container (e.g. Created state)
        # when it fails on a port binding conflict; tear that down so we don't
        # leak dangling containers across repeated skip-inducing runs.
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
    """Yield a DSN to a disposable Postgres with a freshly-empty schema.

    Depends on the session-scoped compose service, so the container is started
    once; the per-test reset guarantees each migration test starts from a clean
    slate (no leftover tables, no schema_migrations rows).
    """
    asyncio.run(_reset_schema())
    return _compose_service


# --- WP-2: FastAPI app + httpx client against the disposable DB -------------
#
# ASGITransport does not drive the app lifespan, so we replicate the lifespan
# setup (create_pool -> ensure_bootstrap -> run_migrations) here and stash the
# pool + auth checkers on app.state. This keeps the test path free of the
# `asgi-lifespan` extra (not in pyproject.toml's dev deps).

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "control" / "schema"


@pytest.fixture
async def app_client(disposable_postgres: str):
    """Build create_app against the disposable DB and yield (client, app).

    Each test gets a freshly-migrated schema, a real asyncpg pool stored on
    ``app.state.pool``, and auth checkers wired to that pool. Tests can mutate
    ``app.state`` (e.g. swap the pool to a broken one for the health-down 503
    case) — the real pool is closed in teardown regardless.
    """
    import asyncpg
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from control.api import create_app
    from control.auth import make_admin_checker, make_host_checker
    from control.config import ControlConfig
    from control.migrate import ensure_bootstrap, run_migrations

    config = ControlConfig(
        database_url=disposable_postgres,
        admin_key="test-admin-key",
        listen_host="127.0.0.1",
        listen_port=7881,
        schema_dir=SCHEMA_DIR,
    )
    app = create_app(config)

    pool = await asyncpg.create_pool(dsn=disposable_postgres, min_size=1, max_size=4)
    try:
        await ensure_bootstrap(pool)
        await run_migrations(pool, SCHEMA_DIR)
    except Exception:
        await pool.close()
        raise

    app.state.pool = pool
    app.state.admin_checker = make_admin_checker(config)
    app.state.host_checker = make_host_checker(config, pool)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, app

    await pool.close()


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"X-Admin-Key": "test-admin-key"}
