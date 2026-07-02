"""MV3 WP-5 — supervisor integration tests.

Drives the real supervisor FastAPI app over a real (uvicorn background-thread)
embedding service and, for the heavy scenarios, real spawned ``mcp_server``
backend subprocesses on isolated per-universe data dirs.

The 12 scenarios split into two tiers:

* **light** (no real backend spawn) — exercise the supervisor HTTP layer, the
  local registry SQLite, and the embedder-validation path against the live
  embedding service ``/info``. Where a ``/route`` would otherwise trigger a
  spawn, the probe / ``Popen`` seams are patched so the test stays fast and
  deterministic. These add integration confidence (real HTTP, real FS, real
  /info) on top of the unit tests in ``tests/unit/test_supervisor.py``.

* **heavy** (``@pytest.mark.slow`` — real process spawn) — the five scenarios
  that mocks cannot prove: mutual isolation across two backends, idle respawn
  with on-disk data retention, concurrent-route spawn dedupe under real flock,
  token-middleware on a real port, and supervisor-restart route continuity via
  ``backend.token`` read-back. Run with ``-m "not slow"`` to skip them.

Per-test isolation: each test gets its own ephemeral multiverse root (under
``tmp_path``) and its own port window; an autouse teardown SIGTERMs any backend
the test spawned that the supervisor's ``_stop_backend`` did not already kill
(e.g. tests that spawn but never delete, or a PID-unknown backend left after a
supervisor restart).
"""
from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gaottt.config import GaOTTTConfig
from gaottt.multiverse.registry import hash_key

from tests.integration._supervisor_helpers import (
    ADMIN_KEY,
    SHORT_IDLE_TIMEOUT,
    SUPERVISOR,
    StubServiceEmbedder,
    admin_headers,
    asgi_client,
    count_listeners_on_port,
    create_universe,
    kill_backends_in_range,
    make_config,
    make_supervisor,
    mcp_call,
    reserve_port_range,
    route_universe,
    start_uvicorn,
    stop_uvicorn,
    wait_port_down,
)

slow = pytest.mark.slow
# NOTE: the ``slow`` marker is functional for selection (``-m "not slow"``). The
# PytestUnknownMarkWarning it emits is benign — silencing it would require
# registering the marker in a conftest.py or pyproject.toml, both outside this
# WP's file scope. pytest hooks (pytest_configure) defined in a *test module*
# are not auto-registered, so that route cannot suppress it either.


# ---------------------------------------------------------------------------
# fixtures — defined natively so test-parameter names do not shadow imports
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def embedder_url():
    """Start the StubServiceEmbedder service once for the whole session.

    Session scope is deliberate: spawned backends (heavy tests) and the
    supervisor's embedder-validation step (light tests) all point at this one
    URL, so the model load is paid once and stays up for the suite.
    """
    from gaottt.embedding.service import create_app

    app = create_app(StubServiceEmbedder(dimension=768))
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
    return reserve_port_range()


@pytest.fixture(autouse=True)
def _kill_spawned_backends(port_range: tuple[int, int]):
    """SIGTERM any ``mcp_server`` backend this test spawned on its port window.

    The supervisor's ``_stop_backend`` now SIGTERM/SIGKILLs a backend whose PID
    it tracked (B2), but backends spawned without a subsequent delete, or a
    backend left alive after a supervisor restart (PID unknown to the new
    instance), are not cleaned up by the delete path. Without this, the heavy
    tests would leak detached backends that only die after their (possibly 300s)
    idle timeout, exhausting ports across the suite. Runs after every test,
    successful or not (yield-fixture teardown).
    """
    yield
    kill_backends_in_range(port_range[0], port_range[1])


# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------

def _tool_text(result) -> str:
    """Join the text content blocks of a ``CallToolResult`` into one string."""
    chunks = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def _probe_ok():
    from gaottt.multiverse.supervisor import PROBE_OK

    return patch(f"{SUPERVISOR}._probe_backend_with_token",
                 AsyncMock(return_value=PROBE_OK))


# ===========================================================================
# LIGHT — no real backend spawn
# ===========================================================================

# ---------------------------------------------------------------------------
# 4. invalid API key -> /route 401
# ---------------------------------------------------------------------------

async def test_invalid_key_returns_401(embedder_url, multiverse_root, port_range):
    config = make_config(multiverse_root, embedder_url, port_range=port_range)
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            body = await create_universe(client, owner="alice")
            # a key that was never issued
            r = await client.post("/route", json={"api_key": "never-issued-key"})
            assert r.status_code == 401
            # and a malformed (empty) key
            r = await client.post("/route", json={"api_key": ""})
            assert r.status_code == 401
            # sanity: the real key routes (probe mocked so no spawn)
            with _probe_ok():
                r = await client.post("/route", json={"api_key": body["api_key"]})
            assert r.status_code == 200
    finally:
        await reg.close()


# ---------------------------------------------------------------------------
# 7. admin auth matrix (all /admin/* surfaces)
# ---------------------------------------------------------------------------

async def test_admin_auth_matrix(embedder_url, multiverse_root, port_range):
    from gaottt.multiverse.supervisor import create_supervisor_app

    # (a) empty admin key -> fail-fast at construction. Build a registry
    # directly for this assertion (no app is ever constructed).
    from gaottt.multiverse.registry import MultiverseRegistry

    bad_config = GaOTTTConfig(
        multiverse_root=str(multiverse_root),
        supervisor_admin_key="",
        embedder_endpoint=embedder_url,
    )
    reg_for_fail = MultiverseRegistry(multiverse_root)
    await reg_for_fail.initialize()
    try:
        with pytest.raises(RuntimeError):
            create_supervisor_app(bad_config, reg_for_fail)
    finally:
        await reg_for_fail.close()

    # (b-d) correct / wrong / missing admin key
    config = make_config(multiverse_root, embedder_url, port_range=port_range)
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            assert (await client.get(
                "/admin/universes", headers=admin_headers())).status_code == 200
            assert (await client.get(
                "/admin/universes",
                headers=admin_headers("wrong"))).status_code == 401
            assert (await client.get("/admin/universes")).status_code == 401
            # Bearer form also accepted
            r = await client.get("/admin/universes",
                                 headers={"Authorization": f"Bearer {ADMIN_KEY}"})
            assert r.status_code == 200
    finally:
        await reg.close()


# ---------------------------------------------------------------------------
# 8. revoked API key -> /route 401
# ---------------------------------------------------------------------------

async def test_revoked_key_returns_401(embedder_url, multiverse_root, port_range):
    config = make_config(multiverse_root, embedder_url, port_range=port_range)
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            body = await create_universe(client, owner="bob")
            api_key = body["api_key"]
            # sanity before revoke
            with _probe_ok():
                r = await client.post("/route", json={"api_key": api_key})
            assert r.status_code == 200

            # revoke via the registry (admin-only op, no HTTP surface in v1)
            await reg.revoke_api_key(hash_key(api_key))

            r = await client.post("/route", json={"api_key": api_key})
            assert r.status_code == 401
    finally:
        await reg.close()


# ---------------------------------------------------------------------------
# 9. delete moves the universe dir to trash + flips registry status
# ---------------------------------------------------------------------------

async def test_delete_moves_to_trash(embedder_url, multiverse_root, port_range):
    from gaottt.multiverse.supervisor import PROBE_DOWN

    config = make_config(multiverse_root, embedder_url, port_range=port_range)
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            body = await create_universe(client, owner="carol")
            uid = body["universe_id"]
            udir = multiverse_root / "universes" / uid
            assert udir.exists()

            # backend mocked DOWN so the delete's stop-wait returns at once
            with patch(f"{SUPERVISOR}._probe_backend_with_token",
                       AsyncMock(return_value=PROBE_DOWN)):
                r = await client.delete(
                    f"/admin/universes/{uid}", headers=admin_headers())
            assert r.status_code == 200
            assert r.json() == {"status": "deleted"}

            # moved to trash (not physically removed), registry marked deleted,
            # and the key no longer verifies.
            assert not udir.exists()
            assert (multiverse_root / "trash" / uid).exists()
            row = await reg.get_universe(uid)
            assert row["status"] == "deleted"
            assert await reg.verify_api_key(body["api_key"]) is None
    finally:
        await reg.close()


# ---------------------------------------------------------------------------
# 10. file modes — root 0700, manifest 0600, universe dir 0700, backend.token 0600
# ---------------------------------------------------------------------------

async def test_file_modes_full(embedder_url, multiverse_root, port_range):
    from gaottt.store.manifest import MANIFEST_FILENAME

    config = make_config(multiverse_root, embedder_url, port_range=port_range)
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            body = await create_universe(client, owner="dave")
            uid = body["universe_id"]
            udir = multiverse_root / "universes" / uid

            assert (os.stat(multiverse_root).st_mode & 0o777) == 0o700
            assert (os.stat(udir).st_mode & 0o777) == 0o700
            manifest_file = udir / MANIFEST_FILENAME
            assert manifest_file.exists()
            assert (os.stat(manifest_file).st_mode & 0o777) == 0o600

            # route triggers ensure_backend -> writes backend.token (probe mocked
            # OK so no spawn occurs; the token is still persisted to disk).
            with _probe_ok():
                await client.post("/route", json={"api_key": body["api_key"]})

            token_file = udir / "backend.token"
            assert token_file.exists()
            assert (os.stat(token_file).st_mode & 0o777) == 0o600
    finally:
        await reg.close()


# ---------------------------------------------------------------------------
# 11. OS port already occupied -> allocate a different port
# ---------------------------------------------------------------------------

async def test_port_occupied_allocates_different(
    embedder_url, multiverse_root, port_range,
):
    config = make_config(multiverse_root, embedder_url, port_range=port_range)
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            start = port_range[0]
            # hold the first port in the range so allocate_port must skip it
            holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            holder.bind(("127.0.0.1", start))
            holder.listen(1)
            try:
                body = await create_universe(client, owner="erin")
            finally:
                holder.close()

            allocated = body["port"]
            assert allocated != start, "occupied port was handed out"
            assert port_range[0] < allocated <= port_range[1]
    finally:
        await reg.close()


# ---------------------------------------------------------------------------
# 12. token stale -> reread backend.token -> success (no respawn)
# ---------------------------------------------------------------------------

async def test_token_stale_recovery(embedder_url, multiverse_root, port_range):
    from gaottt.multiverse.supervisor import PROBE_OK, PROBE_UNAUTHORIZED

    config = make_config(multiverse_root, embedder_url, port_range=port_range)
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            body = await create_universe(client, owner="frank")
            uid = body["universe_id"]
            sup = app.state.supervisor

            t1 = "stale-token-1"
            t2 = "rotated-token-2"
            token_file = multiverse_root / "universes" / uid / "backend.token"
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(t1 + "\n", encoding="utf-8")

            load = patch.object(sup, "_load_token", side_effect=[t1, t2])
            probe = patch(
                f"{SUPERVISOR}._probe_backend_with_token",
                AsyncMock(side_effect=[PROBE_UNAUTHORIZED, PROBE_OK]),
            )
            popen = MagicMock()
            with load, probe, patch(f"{SUPERVISOR}.subprocess.Popen", popen):
                r = await client.post("/route", json={"api_key": body["api_key"]})
            assert r.status_code == 200
            assert r.json()["token"] == t2
            popen.assert_not_called()
    finally:
        await reg.close()


# ===========================================================================
# HEAVY — real backend subprocess spawn
# ===========================================================================

@slow
@pytest.mark.timeout(90)
async def test_mutual_isolation(embedder_url, multiverse_root, port_range):
    """Universe A's memories never surface in universe B's recall.

    Two universes -> two spawned backends on isolated data dirs. ``remember`` a
    distinct marker in each, then assert each backend's recall returns its own
    marker and never the other's. Isolation is structural (separate FAISS +
    SQLite), but this proves it end-to-end through real processes + the token
    auth + the RemoteEmbedder HTTP path.
    """
    config = make_config(multiverse_root, embedder_url, port_range=port_range)
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            a = await create_universe(client, owner="alpha-owner")
            b = await create_universe(client, owner="beta-owner")

            ra = await route_universe(client, a["api_key"])
            rb = await route_universe(client, b["api_key"])
            assert ra["url"] != rb["url"], "two universes share one backend port"

            marker_a = "marker-alpha-zeta-nine only in universe alpha"
            marker_b = "marker-beta-kappa-seven only in universe beta"

            await mcp_call(ra["url"], ra["token"], "remember",
                           {"content": marker_a, "source": "user"})
            await mcp_call(rb["url"], rb["token"], "remember",
                           {"content": marker_b, "source": "user"})

            # sanity: each backend sees its own marker
            res_a_self = _tool_text(await mcp_call(
                ra["url"], ra["token"], "recall",
                {"query": "marker-alpha-zeta", "top_k": 5}))
            res_b_self = _tool_text(await mcp_call(
                rb["url"], rb["token"], "recall",
                {"query": "marker-beta-kappa", "top_k": 5}))
            assert "marker-alpha-zeta-nine" in res_a_self
            assert "marker-beta-kappa-seven" in res_b_self

            # isolation: cross-querying must never surface the other universe
            res_b_cross = _tool_text(await mcp_call(
                rb["url"], rb["token"], "recall",
                {"query": "marker-alpha-zeta", "top_k": 5}))
            res_a_cross = _tool_text(await mcp_call(
                ra["url"], ra["token"], "recall",
                {"query": "marker-beta-kappa", "top_k": 5}))
            assert "marker-alpha-zeta-nine" not in res_b_cross
            assert "marker-beta-kappa-seven" not in res_a_cross
    finally:
        await reg.close()


@slow
@pytest.mark.timeout(120)
async def test_idle_respawn_with_data_retention(
    embedder_url, multiverse_root, port_range, monkeypatch,
):
    """Backend self-shuts on idle -> next /route respawns -> data retained.

    The production timeline is ``idle_timeout (300s) ≫ lease_stale_seconds
    (60s)``: by the time a backend idles out and a client re-routes, the dead
    backend's ``owner.lock`` heartbeat has gone stale, so the respawned
    backend's lease ``acquire`` takes over cleanly. To compress this to a test-
    friendly window we shrink BOTH timers in lock step — ``idle_timeout`` via
    the ``BACKEND_IDLE_TIMEOUT`` module global (read at ``_spawn`` time) and the
    backend's lease heartbeat/staleness via the ``_build_spawn_env`` seam (the
    supervisor's explicit env otherwise strips every ``GAOTTT_*`` knob).

    The engine is built lazily on the first tool call, so the supervisor's MCP
    ``initialize`` probe can report OK before the engine/lease exists — the
    lease takeover is therefore exercised at ``recall`` time, which is exactly
    where a stale-lock regression would surface.
    """
    import gaottt.multiverse.supervisor as sup_mod

    monkeypatch.setattr(sup_mod, "BACKEND_IDLE_TIMEOUT", SHORT_IDLE_TIMEOUT)

    # Inject compressed lease timers into the spawned backend's env so a fast
    # respawn can take over the dead backend's owner.lock (heartbeat must age
    # past lease_stale_seconds before the next acquire).
    orig_build_env = sup_mod._Supervisor._build_spawn_env

    def patched_build_env(self, universe_dir, token):
        env = orig_build_env(self, universe_dir, token)
        env["GAOTTT_LEASE_HEARTBEAT_SECONDS"] = "1"
        env["GAOTTT_LEASE_STALE_SECONDS"] = "3"
        return env

    monkeypatch.setattr(sup_mod._Supervisor, "_build_spawn_env", patched_build_env)

    config = make_config(multiverse_root, embedder_url, port_range=port_range)
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            body = await create_universe(client, owner="respawn-owner")
            api_key = body["api_key"]
            port = body["port"]

            first = await route_universe(client, api_key)
            marker = "retention-sigma-three survives idle respawn"
            await mcp_call(first["url"], first["token"], "remember",
                           {"content": marker, "source": "user"})

            # let at least one write-behind flush (5s) land on disk before the
            # backend idles out, decoupling persistence from the watchdog clock
            await asyncio.sleep(6.0)

            # wait for the idle watchdog to SIGTERM the backend
            down = await wait_port_down(port, timeout=25.0)
            assert down, "backend did not idle out within 25s"

            # let the dead backend's owner.lock heartbeat age past stale (3s)
            # before the respawn attempts a takeover
            await asyncio.sleep(4.0)

            # next route must respawn (probe DOWN) and return a fresh readiness
            second = await route_universe(client, api_key)
            assert second["url"] == first["url"]

            recalled = _tool_text(await mcp_call(
                second["url"], second["token"], "recall",
                {"query": "retention-sigma-three", "top_k": 5}))
            assert "retention-sigma-three" in recalled
    finally:
        await reg.close()


@slow
@pytest.mark.timeout(90)
async def test_concurrent_route_single_backend(
    embedder_url, multiverse_root, port_range,
):
    """5 concurrent /route to one universe -> exactly one backend spawned.

    The supervisor's per-universe asyncio.Lock + flock serialize spawn. All
    five routes must return the same (url, token) and leave a single listener
    on the port.
    """
    config = make_config(multiverse_root, embedder_url, port_range=port_range)
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            body = await create_universe(client, owner="concurrent-owner")
            api_key = body["api_key"]
            port = body["port"]

            results = await asyncio.gather(
                *[client.post("/route", json={"api_key": api_key})
                  for _ in range(5)]
            )
            assert all(r.status_code == 200 for r in results)
            urls = {r.json()["url"] for r in results}
            tokens = {r.json()["token"] for r in results}
            assert len(urls) == 1, "routes diverged on url"
            assert len(tokens) == 1, "token rotated across concurrent routes"

            # give the just-spawned backend a moment to bind, then count
            await asyncio.sleep(1.0)
            assert count_listeners_on_port(port) == 1, "more than one backend"
    finally:
        await reg.close()


@slow
@pytest.mark.timeout(90)
async def test_token_path_no_direct_access(
    embedder_url, multiverse_root, port_range,
):
    """A spawned backend rejects unauthenticated direct hits (401) but admits
    a Bearer-tokened request. Proves the token middleware is installed on the
    real port and that the supervisor's own probe (tokened) reaches it."""
    config = make_config(multiverse_root, embedder_url, port_range=port_range)
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            body = await create_universe(client, owner="token-owner")
            routed = await route_universe(client, body["api_key"])
            port = body["port"]
            token = routed["token"]
            url = f"http://127.0.0.1:{port}/mcp"

            # no Authorization header -> 401 (token middleware short-circuits)
            async with httpx.AsyncClient(timeout=5.0) as http:
                no_auth = await http.get(url)
            assert no_auth.status_code == 401

            # with the Bearer token the server does NOT 401 (it may 400/405 on a
            # bare GET of the MCP endpoint, but the auth layer admits it)
            async with httpx.AsyncClient(timeout=5.0) as http:
                with_auth = await http.get(
                    url, headers={"Authorization": f"Bearer {token}"})
            assert with_auth.status_code != 401

            # the supervisor's own tokened MCP handshake succeeds (probe OK was
            # already proven by route returning 200); exercise remember->recall
            await mcp_call(routed["url"], token, "remember",
                           {"content": "token-path-marker", "source": "user"})
            recalled = _tool_text(await mcp_call(
                routed["url"], token, "recall",
                {"query": "token-path-marker", "top_k": 5}))
            assert "token-path-marker" in recalled
    finally:
        await reg.close()


@slow
@pytest.mark.timeout(90)
async def test_supervisor_restart_route_continuity(
    embedder_url, multiverse_root, port_range,
):
    """A running backend survives a supervisor restart: the new supervisor
    reads ``backend.token`` back from disk, probes the live backend, and routes
    to it WITHOUT respawning (token unchanged, still one listener)."""
    config = make_config(multiverse_root, embedder_url, port_range=port_range)

    # --- first supervisor instance: create universe + spawn backend ---
    app1, reg1 = await make_supervisor(config)
    api_key: str
    port: int
    token1: str
    try:
        async with asgi_client(app1) as client:
            body = await create_universe(client, owner="restart-owner")
            api_key = body["api_key"]
            port = body["port"]
            routed = await route_universe(client, api_key)
            token1 = routed["token"]
            # backend is now running with token1 persisted to backend.token
    finally:
        await reg1.close()

    # --- second supervisor instance over the SAME multiverse root ---
    app2, reg2 = await make_supervisor(config)
    try:
        async with asgi_client(app2) as client:
            routed2 = await route_universe(client, api_key)
            # same backend, same token -> no respawn occurred
            assert routed2["token"] == token1
            assert routed2["url"] == f"http://127.0.0.1:{port}/mcp"
            await asyncio.sleep(0.5)
            assert count_listeners_on_port(port) == 1, "restart spawned a 2nd backend"
    finally:
        await reg2.close()


@slow
@pytest.mark.timeout(90)
async def test_delete_sigterms_tracked_backend(
    embedder_url, multiverse_root, port_range,
):
    """B2: a delete on a universe whose backend was spawned by this supervisor
    (PID known) SIGTERMs the real backend, waits for the port to go silent, and
    only then moves the dir to trash — the live data dir is never moved out
    from under a running backend. The PID-unknown/alive case (409) is covered
    in the unit tests; this proves the kill against a real process."""
    config = make_config(multiverse_root, embedder_url, port_range=port_range)
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            body = await create_universe(client, owner="delete-owner")
            uid = body["universe_id"]
            port = body["port"]
            udir = multiverse_root / "universes" / uid
            sup = app.state.supervisor

            # /route spawns a real backend; the supervisor records its PID.
            await route_universe(client, body["api_key"])
            assert uid in sup._backend_pids, "spawn did not record the backend PID"
            await asyncio.sleep(0.5)
            assert count_listeners_on_port(port) == 1, "backend never came up"

            # delete must SIGTERM the tracked backend and only move the dir
            # once the port is silent.
            r = await client.delete(
                f"/admin/universes/{uid}", headers=admin_headers())
            assert r.status_code == 200, r.text

            # backend stopped serving — port released before the dir moved.
            assert count_listeners_on_port(port) == 0, (
                "backend still listening after delete; dir may have been "
                "moved out from under a live process"
            )
            # PID entry cleaned up, dir in trash, registry marked deleted.
            assert uid not in sup._backend_pids
            assert not udir.exists()
            assert (multiverse_root / "trash" / uid).exists()
            row = await reg.get_universe(uid)
            assert row["status"] == "deleted"
    finally:
        await reg.close()
