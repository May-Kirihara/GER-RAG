"""WP-4: embedder lazy spawn integration tests (real subprocess).

Exercises the full lazy-spawn -> readiness -> /info -> idle -> terminate
lifecycle with a REAL subprocess that serves a ``StubServiceEmbedder``-backed
embedding service — NOT the heavy RURI model (~1.2 GB). The production
``_spawn_embedder_detached`` is monkey-patched to launch the stub service,
so the test validates the real ``Popen`` + readiness poll + watchdog +
terminate path end-to-end without paying the model-load cost.

Each test gets its own ephemeral multiverse root (``tmp_path``) and a
dynamically allocated embedder port (never 9999 — the dev embedder).
Spawned embedder subprocesses are SIGTERM/SIGKILL-reaped in an autouse
teardown so no orphans leak across the suite.

Contract source: ``docs/plans/embedder-auto-spawn-supervisor.md`` v3 §5-§6,
acceptance criteria a-h. Reference patterns:

- ``tests/integration/test_engine_remote_embedder.py``: StubServiceEmbedder
  + uvicorn lifecycle (background-thread variant; here we use subprocess).
- ``tests/integration/_supervisor_helpers.py``: ASGITransport client,
  multiverse_root / port_range fixtures, StubServiceEmbedder definition.
- ``tests/unit/test_supervisor_embedder_spawn.py``: unit-level lifecycle
  tests for the same paths (mocks only, no real process).
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gaottt.config import GaOTTTConfig
from gaottt.multiverse.supervisor import PROBE_DOWN, PROBE_OK

from tests.integration._supervisor_helpers import (
    ADMIN_KEY,
    SUPERVISOR,
    StubServiceEmbedder,
    asgi_client,
    create_universe,
    free_port,
    make_supervisor,
    reserve_port_range,
    start_uvicorn,
    stop_uvicorn,
)

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

REPO_ROOT = str(Path(__file__).resolve().parents[2])

# Patch target for the production spawn function. Tests replace this with
# _spawn_stub_embedder (or _spawn_dying_embedder) to avoid loading the real
# RURI model while still exercising the real Popen + readiness lifecycle.
SPAWN_PATCH_TARGET = "gaottt.multiverse.supervisor._spawn_embedder_detached"

# Embedder /info payload returned by the stub service. Used as a mock
# return value when seeding a universe WITHOUT triggering a real spawn
# (so the spawn can be observed later during /route).
VALID_STUB_INFO = {
    "model_name": "stub-service",
    "dimension": 768,
    "version": "stub-v0",
    "batch_size": 32,
}


# ---------------------------------------------------------------------------
# stub embedder subprocess launcher (test replacement for _spawn_embedder_detached)
# ---------------------------------------------------------------------------

def _build_stub_runner_code(host: str, port: int) -> str:
    """Inline Python source that starts a StubServiceEmbedder uvicorn server.

    The subprocess inherits ``sys.executable`` (the .venv Python) but NOT
    the repo root on its path (``python -c`` does not add CWD to
    ``sys.path``), so REPO_ROOT is inserted explicitly. The resulting
    process is a real child of the test process, so ``os.waitpid`` in the
    supervisor's lifecycle code can reap it normally.
    """
    return textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {REPO_ROOT!r})
        import uvicorn
        from gaottt.embedding.service import create_app
        from tests.integration._supervisor_helpers import StubServiceEmbedder
        app = create_app(StubServiceEmbedder(dimension=768))
        uvicorn.run(app, host={host!r}, port={port!r}, log_level="warning")
    """)


def _spawn_stub_embedder(host: str, port: int, log_path: Path, model: str, **_kwargs) -> int:
    """Launch a detached StubServiceEmbedder subprocess. Returns PID.

    Mirrors the real ``_spawn_embedder_detached`` signature and detach
    semantics (``start_new_session``, ``DEVNULL`` stdin, log-file stdout,
    parent closes the log fd after ``Popen``) so the supervisor's PID-
    tracking + waitpid lifecycle works identically.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", buffering=1)  # noqa: SIM115 — child owns the fd
    log_file.write(
        f"\n--- stub embedder spawn at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
    )
    code = _build_stub_runner_code(host, port)
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "start_new_session": True,
    }
    proc = subprocess.Popen([sys.executable, "-c", code], **kwargs)  # noqa: S603
    pid = proc.pid
    _SPAWNED_PIDS.add(pid)
    log_file.close()
    return pid


def _spawn_dying_embedder(host: str, port: int, log_path: Path, model: str, **_kwargs) -> int:
    """Launch a subprocess that exits immediately (spawn-failure simulation).

    Used to drive the readiness-failure -> ``EmbedderValidationError`` path
    in ``_handle_spawn_readiness_failure`` without depending on timing or
    port conflicts.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", buffering=1)  # noqa: SIM115
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "start_new_session": True,
    }
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(1)"],  # noqa: S603
        **kwargs,
    )
    pid = proc.pid
    _SPAWNED_PIDS.add(pid)
    log_file.close()
    return pid


# ---------------------------------------------------------------------------
# PID tracking + cleanup
# ---------------------------------------------------------------------------

_SPAWNED_PIDS: set[int] = set()


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` exists (signal 0 succeeds). Does not reap."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _force_kill_pid(pid: int) -> None:
    """SIGTERM (2s grace) then SIGKILL a subprocess, reaping it."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            return
        if sig == signal.SIGTERM:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    done, _ = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    return
                if done != 0:
                    return
                time.sleep(0.1)
    # final reap attempt
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


@pytest.fixture(autouse=True)
def _reap_spawned_embedders():
    """SIGTERM/SIGKILL any embedder subprocess this test spawned.

    The supervisor's ``_terminate_embedder`` handles owned embedders when
    called, but ASGITransport skips the lifespan shutdown, and tests that
    only observe (never terminate) would otherwise leak detached children
    that linger until their idle timeout.
    """
    yield
    for pid in list(_SPAWNED_PIDS):
        _force_kill_pid(pid)
    _SPAWNED_PIDS.clear()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

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
    return reserve_port_range()


def _make_config(
    root: Path,
    embedder_port: int,
    port_range: tuple[int, int],
    *,
    spawn_embedder: bool = True,
    readiness_timeout: float = 20.0,
    idle_timeout: float = 300.0,
    poll_seconds: float = 30.0,
) -> GaOTTTConfig:
    """Build a supervisor config pointing embedder_endpoint at ``embedder_port``.

    ``embedder_port`` is always a dynamically allocated port (never 9999),
    so the test never collides with the dev embedder.
    """
    return GaOTTTConfig(
        multiverse_root=str(root),
        supervisor_admin_key=ADMIN_KEY,
        embedder_endpoint=f"http://127.0.0.1:{embedder_port}",
        universe_port_range_start=port_range[0],
        universe_port_range_end=port_range[1],
        supervisor_readiness_timeout=30.0,
        supervisor_spawn_concurrency=3,
        supervisor_spawn_embedder=spawn_embedder,
        embedder_spawn_readiness_timeout_seconds=readiness_timeout,
        embedder_spawn_idle_timeout_seconds=idle_timeout,
        embedder_idle_watchdog_poll_seconds=poll_seconds,
    )


def _mock_valid_info():
    """Patch _validate_embedder to return VALID_STUB_INFO (no real embedder needed)."""
    return patch(
        f"{SUPERVISOR}._validate_embedder",
        return_value=VALID_STUB_INFO,
    )


# ===========================================================================
# a. basic lazy spawn via /route
# ===========================================================================

@pytest.mark.timeout(60)
async def test_basic_lazy_spawn_via_route(multiverse_root, port_range):
    """a: embedder down -> /route triggers lazy spawn -> /healthz + /info
    pass -> embedder is ``owned_idle`` with a live PID."""
    embedder_port = free_port()
    config = _make_config(multiverse_root, embedder_port, port_range)
    app, reg = await make_supervisor(config)
    sup = app.state.supervisor

    try:
        async with asgi_client(app) as client:
            # Seed a universe WITHOUT spawning the embedder (mocked /info).
            with _mock_valid_info():
                body = await create_universe(client, owner="basic-a")
            # Clear the cache so /route re-validates and enters the spawn path.
            sup._embedder_info_cache = None

            # /route: embedder is down -> lazy spawn -> backend probe mocked OK.
            with patch(SPAWN_PATCH_TARGET, _spawn_stub_embedder), \
                 patch(f"{SUPERVISOR}._probe_backend_with_token",
                       AsyncMock(return_value=PROBE_OK)):
                r = await client.post("/route", json={"api_key": body["api_key"]})
            assert r.status_code == 200, r.text

            # The supervisor spawned and now owns the embedder.
            assert sup._embedder_state == "owned_idle"
            assert sup._embedder_pid is not None

            # /healthz and /info answer on the spawned embedder's port.
            async with httpx.AsyncClient(timeout=5.0) as http:
                healthz = await http.get(
                    f"http://127.0.0.1:{embedder_port}/healthz",
                )
                assert healthz.status_code == 200
                assert healthz.json() == {"status": "ok"}

                info = await http.get(
                    f"http://127.0.0.1:{embedder_port}/info",
                )
                assert info.status_code == 200
                assert info.json()["model_name"] == "stub-service"
                assert info.json()["dimension"] == 768
    finally:
        await reg.close()


# ===========================================================================
# b. /route ordering: embedder spawns BEFORE backend
# ===========================================================================

@pytest.mark.timeout(60)
async def test_route_spawns_embedder_before_backend(multiverse_root, port_range):
    """b: on /route with both down, embedder lazy-spawns FIRST, then the
    backend spawns. The handler calls ``ensure_embedder_up`` before
    ``ensure_backend``; this test verifies the real ordering by recording
    spawn-call timestamps."""
    embedder_port = free_port()
    config = _make_config(multiverse_root, embedder_port, port_range)
    app, reg = await make_supervisor(config)
    sup = app.state.supervisor

    events: list[str] = []

    def _tracking_embedder_spawn(host, port, log_path, model, **_kwargs):
        events.append("embedder_spawn")
        return _spawn_stub_embedder(host, port, log_path, model)

    def _tracking_backend_spawn(_universe, _token):
        events.append("backend_spawn")
        return 999999  # fake PID — no real mcp_server process

    try:
        async with asgi_client(app) as client:
            with _mock_valid_info():
                body = await create_universe(client, owner="order-b")
            sup._embedder_info_cache = None

            # Backend probe: initial DOWN (triggers spawn) then OK (readiness).
            probe = AsyncMock(
                side_effect=[PROBE_DOWN, PROBE_OK, PROBE_OK, PROBE_OK, PROBE_OK],
            )
            with patch(SPAWN_PATCH_TARGET, _tracking_embedder_spawn), \
                 patch(f"{SUPERVISOR}._probe_backend_with_token", probe), \
                 patch.object(sup, "_spawn", side_effect=_tracking_backend_spawn):
                r = await client.post("/route", json={"api_key": body["api_key"]})
            assert r.status_code == 200, r.text
    finally:
        await reg.close()

    assert "embedder_spawn" in events, f"embedder never spawned: {events}"
    assert "backend_spawn" in events, f"backend never spawned: {events}"
    assert events.index("embedder_spawn") < events.index("backend_spawn"), (
        f"embedder must spawn before backend; order was {events}"
    )
    assert sup._embedder_state == "owned_idle"


# ===========================================================================
# c. concurrent /route spawns embedder exactly once
# ===========================================================================

@pytest.mark.timeout(60)
async def test_concurrent_route_spawns_embedder_once(multiverse_root, port_range):
    """c: multiple concurrent /route requests with embedder down spawn the
    embedder exactly once (``_embedder_spawn_lock`` + ``fcntl.flock``
    serialize the spawn critical section within one process)."""
    embedder_port = free_port()
    config = _make_config(multiverse_root, embedder_port, port_range)
    app, reg = await make_supervisor(config)
    sup = app.state.supervisor

    spawn_count = [0]

    def _counting_embedder_spawn(host, port, log_path, model, **_kwargs):
        spawn_count[0] += 1
        return _spawn_stub_embedder(host, port, log_path, model)

    try:
        async with asgi_client(app) as client:
            with _mock_valid_info():
                body = await create_universe(client, owner="concurrent-c")
            sup._embedder_info_cache = None

            # 5 concurrent /routes. Backend probe mocked OK so no real
            # backend spawn — only the embedder spawn is observed.
            with patch(SPAWN_PATCH_TARGET, _counting_embedder_spawn), \
                 patch(f"{SUPERVISOR}._probe_backend_with_token",
                       AsyncMock(return_value=PROBE_OK)):
                results = await asyncio.gather(*[
                    client.post("/route", json={"api_key": body["api_key"]})
                    for _ in range(5)
                ])
            assert all(r.status_code == 200 for r in results), [
                r.text for r in results
            ]
    finally:
        await reg.close()

    assert spawn_count[0] == 1, (
        f"embedder spawned {spawn_count[0]} times under concurrent /route; "
        "expected exactly 1 (spawn lock failed to serialize)"
    )
    assert sup._embedder_state == "owned_idle"


# ===========================================================================
# d. pre-spawned embedder -> supervisor goes unowned (race-lost equivalent)
# ===========================================================================

@pytest.mark.timeout(60)
async def test_prespawned_embedder_supervisor_stays_unowned(
    multiverse_root, port_range,
):
    """d: an embedder already serving on the configured port (another
    supervisor / systemd) is detected via /info on the first
    ``ensure_embedder_up`` call. The supervisor caches its info, stays
    ``unowned``, and NEVER calls ``_spawn_embedder_detached``."""
    from gaottt.embedding.service import create_app

    # Pre-spawn a stub embedder (background-thread uvicorn, same process).
    pre_app = create_app(StubServiceEmbedder(dimension=768))
    pre_server, pre_thread, pre_port = start_uvicorn(pre_app)

    spawn_called = []

    def _assert_no_spawn(host, port, log_path, model, **_kwargs):
        spawn_called.append(True)
        raise AssertionError(
            "_spawn_embedder_detached must not be called when an external "
            "embedder is already reachable",
        )

    try:
        config = _make_config(multiverse_root, pre_port, port_range)
        app, reg = await make_supervisor(config)
        sup = app.state.supervisor

        try:
            async with asgi_client(app) as client:
                with patch(SPAWN_PATCH_TARGET, _assert_no_spawn):
                    body = await create_universe(client, owner="prespawn-d")
                assert body["universe_id"]
        finally:
            await reg.close()

        assert not spawn_called, "spawn was attempted despite external embedder"
        assert sup._embedder_state == "unowned"
        assert sup._embedder_pid is None
    finally:
        stop_uvicorn(pre_server, pre_thread)


# ===========================================================================
# e. watchdog task is re-created on re-spawn (old one cancelled)
# ===========================================================================

@pytest.mark.timeout(60)
async def test_watchdog_recreated_on_respawn(multiverse_root, port_range):
    """e: terminate embedder -> re-/route spawns a new embedder -> a NEW
    watchdog task is created and the old one is done (cancelled/completed)."""
    embedder_port = free_port()
    config = _make_config(multiverse_root, embedder_port, port_range)
    app, reg = await make_supervisor(config)
    sup = app.state.supervisor

    try:
        async with asgi_client(app) as client:
            with patch(SPAWN_PATCH_TARGET, _spawn_stub_embedder):
                body = await create_universe(client, owner="watchdog-e")

            first_watchdog = sup._embedder_watchdog_task
            assert first_watchdog is not None
            assert sup._embedder_state == "owned_idle"

            # Terminate the owned embedder. _reset_embedder_state cancels
            # the watchdog and clears the task field.
            await sup._terminate_embedder()
            assert sup._embedder_state == "unowned"
            assert sup._embedder_watchdog_task is None
            # The watchdog task was cancelled inside the sync
            # _reset_embedder_state call — the CancelledError is pending
            # and won't be processed until the event loop runs. Await the
            # task so it reaches .done() before the assertion.
            try:
                await first_watchdog
            except asyncio.CancelledError:
                pass
            assert first_watchdog.done()

            # Force re-validation and re-spawn via /route.
            sup._embedder_info_cache = None
            with patch(SPAWN_PATCH_TARGET, _spawn_stub_embedder), \
                 patch(f"{SUPERVISOR}._probe_backend_with_token",
                       AsyncMock(return_value=PROBE_OK)):
                r = await client.post("/route", json={"api_key": body["api_key"]})
            assert r.status_code == 200, r.text

            second_watchdog = sup._embedder_watchdog_task
            assert second_watchdog is not None
            assert second_watchdog is not first_watchdog
            assert sup._embedder_state == "owned_idle"
    finally:
        await reg.close()


# ===========================================================================
# f. supervisor lifespan shutdown terminates the embedder (no zombie)
# ===========================================================================

@pytest.mark.timeout(60)
async def test_lifespan_shutdown_terminates_embedder_no_zombie(
    multiverse_root, port_range,
):
    """f: exiting the supervisor lifespan terminates the owned embedder
    subprocess (SIGTERM -> SIGKILL -> waitpid). After shutdown the PID is
    gone and has been reaped (no zombie)."""
    from gaottt.multiverse.registry import MultiverseRegistry
    from gaottt.multiverse.supervisor import create_supervisor_app

    embedder_port = free_port()
    config = _make_config(multiverse_root, embedder_port, port_range)

    registry = MultiverseRegistry(multiverse_root)
    app = create_supervisor_app(config, registry)
    sup = app.state.supervisor

    spawned_pid: list[int] = []

    async with app.router.lifespan_context(app):
        # Lifespan startup ran (registry initialized). Create a universe
        # inside the lifespan to trigger the real lazy spawn.
        with patch(SPAWN_PATCH_TARGET, _spawn_stub_embedder):
            async with asgi_client(app) as client:
                await create_universe(client, owner="shutdown-f")

        pid = sup._embedder_pid
        assert pid is not None
        assert _pid_alive(pid)
        spawned_pid.append(pid)

    # Lifespan exited — the owned embedder was terminated + reaped.
    assert sup._embedder_state == "unowned"
    pid = spawned_pid[0]

    # _terminate_embedder already waited for exit inside the lifespan
    # shutdown, so the process should be gone immediately.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _pid_alive(pid):
        await asyncio.sleep(0.2)
    assert not _pid_alive(pid), (
        f"embedder pid={pid} survived lifespan shutdown (zombie/leak)"
    )


# ===========================================================================
# g. untracked live backend -> watchdog defers embedder termination
# ===========================================================================

@pytest.mark.timeout(60)
async def test_watchdog_defers_kill_with_untracked_live_backend(
    multiverse_root, port_range,
):
    """g: when an untracked active universe has a live backend (another
    supervisor's backend), the idle watchdog does NOT terminate the
    embedder even though the idle timeout has elapsed. The real embedder
    subprocess stays alive."""
    from gaottt.multiverse.supervisor import _Supervisor

    embedder_port = free_port()
    config = _make_config(multiverse_root, embedder_port, port_range)
    app, reg = await make_supervisor(config)
    sup = app.state.supervisor

    try:
        async with asgi_client(app) as client:
            with patch(SPAWN_PATCH_TARGET, _spawn_stub_embedder):
                await create_universe(client, owner="untracked-g")

            pid = sup._embedder_pid
            assert pid is not None
            assert _pid_alive(pid)

            # Cancel the background watchdog so the test controls exactly
            # when the loop body runs (avoids a race where the real
            # watchdog terminates the embedder before mocks are installed).
            if sup._embedder_watchdog_task is not None:
                sup._embedder_watchdog_task.cancel()
                try:
                    await sup._embedder_watchdog_task
                except asyncio.CancelledError:
                    pass
                sup._embedder_watchdog_task = None

            # Force idle-elapsed + simulate an untracked live backend.
            sup._last_backend_active_at = 0.0

            # Break the watchdog loop after 2 sleep calls so the test
            # observes the "deferred" decision without hanging.
            sleep_calls = [0]

            async def _fake_sleep(_):
                sleep_calls[0] += 1
                if sleep_calls[0] >= 2:
                    raise asyncio.CancelledError()

            with patch.object(
                _Supervisor, "_has_tracked_live_backends", return_value=False,
            ), patch.object(
                _Supervisor, "_has_untracked_live_backends",
                AsyncMock(return_value=True),
            ), patch(
                f"{SUPERVISOR}.asyncio.sleep", side_effect=_fake_sleep,
            ):
                await sup._embedder_idle_watchdog()

            # Embedder NOT terminated — the watchdog deferred.
            assert sup._embedder_state == "owned_idle"
            assert _pid_alive(pid), (
                "embedder was killed despite an untracked live backend"
            )
    finally:
        await reg.close()


# ===========================================================================
# h. /route returns 503 on EmbedderValidationError
# ===========================================================================

@pytest.mark.timeout(30)
async def test_route_returns_503_when_spawn_disabled_and_embedder_down(
    multiverse_root, port_range,
):
    """h: with ``supervisor_spawn_embedder=False`` and the embedder
    unreachable, /route's ``ensure_embedder_up`` raises
    ``EmbedderValidationError`` which the B2-4 handler maps to 503
    (not 400 — that is ``create_universe``'s mapping)."""
    embedder_port = free_port()  # nothing listening
    config = _make_config(
        multiverse_root, embedder_port, port_range,
        spawn_embedder=False,
    )
    app, reg = await make_supervisor(config)
    sup = app.state.supervisor

    try:
        async with asgi_client(app) as client:
            # Seed a universe with mocked /info (embedder is down, so a
            # real create would fail at validation).
            with _mock_valid_info():
                body = await create_universe(client, owner="route503-h")
            # Clear the cache so /route re-validates against the real
            # (unreachable) embedder endpoint.
            sup._embedder_info_cache = None

            r = await client.post("/route", json={"api_key": body["api_key"]})
            assert r.status_code == 503
            assert "Embedder validation failed" in r.json()["detail"]
    finally:
        await reg.close()


@pytest.mark.timeout(30)
async def test_route_returns_503_on_spawn_readiness_failure(
    multiverse_root, port_range,
):
    """h (spawn-failure variant): when the spawned embedder child exits
    immediately (never becomes ready), ``_handle_spawn_readiness_failure``
    raises ``EmbedderValidationError`` -> /route 503. Exercises the
    real Popen + readiness-poll + race-classification path."""
    embedder_port = free_port()
    config = _make_config(
        multiverse_root, embedder_port, port_range,
        readiness_timeout=3.0,
    )
    app, reg = await make_supervisor(config)
    sup = app.state.supervisor

    try:
        async with asgi_client(app) as client:
            with _mock_valid_info():
                body = await create_universe(client, owner="spawnfail-h2")
            sup._embedder_info_cache = None

            with patch(SPAWN_PATCH_TARGET, _spawn_dying_embedder):
                r = await client.post("/route", json={"api_key": body["api_key"]})

            assert r.status_code == 503
            assert "Embedder validation failed" in r.json()["detail"]
            # The failed spawn did not establish ownership.
            assert sup._embedder_state != "owned_idle"
    finally:
        await reg.close()
