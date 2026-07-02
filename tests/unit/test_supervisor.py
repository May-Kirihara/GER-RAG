"""MV3 Multiverse — universe supervisor unit tests (test-first / RED stage).

These tests assume the WP-3 contract exposed by
``gaottt/multiverse/supervisor.py``::

    PROBE_OK, PROBE_UNAUTHORIZED, PROBE_DOWN   # probe result sentinels

    class EmbedderValidationError(RuntimeError)

    def _validate_embedder(config) -> dict          # GET <endpoint>/info
    async def _probe_backend_with_token(host, port, token, timeout=3.0) -> str

    class _Supervisor:
        async def ensure_backend(self, universe: dict) -> tuple[str, str]
        def _load_token(self, uid) -> str | None     # patch seam (token stale)
        def _spawn(self, universe, token)            # patch seam (no real process)

    def create_supervisor_app(config, registry) -> FastAPI
        # raises RuntimeError if config.supervisor_admin_key is empty

Endpoints:
    POST   /admin/universes           (admin key)  -> 201 {universe_id, api_key, port}
    DELETE /admin/universes/{uid}     (admin key)  -> {status: deleted}
    GET    /admin/universes           (admin key)  -> [universe, ...]
    POST   /route                     (api key)    -> {url, token}

The tests never spawn a real backend process: ``subprocess.Popen`` and
``_probe_backend_with_token`` are patched. ASGITransport does not run the
app lifespan, so the registry is pre-initialised in a fixture (mirrors the
``test_rest_memory.py`` pattern).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from gaottt.config import GaOTTTConfig

ADMIN_KEY = "test-admin-key"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Key": ADMIN_KEY}


def _embedder_ok(model_name: str = "test-emb", dimension: int = 768,
                 version: str = "test-v1"):
    """A patch context that makes ``httpx.Client.get`` return a valid /info."""
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "model_name": model_name,
        "dimension": dimension,
        "version": version,
        "batch_size": 32,
    }
    return patch("httpx.Client.get", return_value=mock_resp)


def _embedder_fail(status: int = 503):
    mock_resp = Mock()
    mock_resp.status_code = status
    mock_resp.json.return_value = {}
    return patch("httpx.Client.get", return_value=mock_resp)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_multiverse_root(tmp_path: Path) -> Path:
    root = tmp_path / "multiverse"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


@pytest.fixture
def stub_config(tmp_multiverse_root: Path) -> GaOTTTConfig:
    return GaOTTTConfig(
        multiverse_root=str(tmp_multiverse_root),
        supervisor_admin_key=ADMIN_KEY,
        embedder_endpoint="http://127.0.0.1:9999",
        supervisor_readiness_timeout=5.0,
    )


@pytest.fixture
async def stub_registry(tmp_multiverse_root: Path):
    from gaottt.multiverse.registry import MultiverseRegistry

    reg = MultiverseRegistry(tmp_multiverse_root)
    await reg.initialize()
    try:
        yield reg
    finally:
        await reg.close()


@pytest.fixture
async def app(stub_config: GaOTTTConfig, stub_registry):
    from gaottt.multiverse.supervisor import create_supervisor_app

    return create_supervisor_app(stub_config, stub_registry)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _make_universe(client, owner: str = "owner-a") -> dict:
    """Create a universe via the admin API (embedder mocked ok); return its body."""
    with _embedder_ok():
        r = await client.post(
            "/admin/universes",
            json={"owner_label": owner},
            headers=_admin_headers(),
        )
    assert r.status_code == 201, r.text
    return r.json()


# ===========================================================================
# 1. admin key empty -> fail-fast
# ===========================================================================

async def test_admin_key_empty_raises_runtime_error(stub_registry, tmp_multiverse_root):
    from gaottt.multiverse.supervisor import create_supervisor_app

    config = GaOTTTConfig(
        multiverse_root=str(tmp_multiverse_root),
        supervisor_admin_key="",
    )
    with pytest.raises(RuntimeError):
        create_supervisor_app(config, stub_registry)


# ===========================================================================
# 2. admin auth matrix (all /admin/*)
# ===========================================================================

async def test_admin_auth_correct_key_passes(client):
    r = await client.get("/admin/universes", headers=_admin_headers())
    assert r.status_code == 200
    assert r.json() == []


async def test_admin_auth_wrong_key_returns_401(client):
    r = await client.get("/admin/universes", headers={"X-Admin-Key": "wrong"})
    assert r.status_code == 401


async def test_admin_auth_missing_header_returns_401(client):
    r = await client.get("/admin/universes")
    assert r.status_code == 401


async def test_admin_auth_bearer_form_accepted(client):
    r = await client.get(
        "/admin/universes",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
    )
    assert r.status_code == 200


async def test_admin_auth_wrong_key_blocks_post_too(client):
    r = await client.post(
        "/admin/universes",
        json={"owner_label": "o"},
        headers={"X-Admin-Key": "nope"},
    )
    assert r.status_code == 401


# ===========================================================================
# 3. create universe — success path
# ===========================================================================

async def test_create_universe_returns_id_key_port(client, stub_config, stub_registry,
                                                   tmp_multiverse_root):
    from gaottt.store.manifest import load_manifest

    with _embedder_ok(model_name="test-emb", dimension=768, version="test-v1"):
        r = await client.post(
            "/admin/universes",
            json={"owner_label": "alice"},
            headers=_admin_headers(),
        )
    assert r.status_code == 201
    body = r.json()
    assert body["universe_id"]
    assert body["api_key"]
    assert isinstance(body["port"], int)

    # manifest written with managed=True and the embedder identity from /info
    udir = tmp_multiverse_root / "universes" / body["universe_id"]
    manifest = load_manifest(udir)
    assert manifest is not None
    assert manifest.managed is True
    assert manifest.embedder_id == "test-emb"
    assert manifest.embedding_dim == 768
    assert manifest.embedder_version == "test-v1"

    # registry row added
    row = await stub_registry.get_universe(body["universe_id"])
    assert row is not None
    assert row["status"] == "active"
    assert row["owner_label"] == "alice"
    # the returned api_key verifies back to this universe
    assert await stub_registry.verify_api_key(body["api_key"]) == body["universe_id"]


# ===========================================================================
# 4. create universe — embedder validation failure
# ===========================================================================

async def test_create_universe_embedder_validation_fail_returns_400(client):
    with _embedder_fail(status=503):
        r = await client.post(
            "/admin/universes",
            json={"owner_label": "o"},
            headers=_admin_headers(),
        )
    assert r.status_code == 400


# ===========================================================================
# 5. route — correct key
# ===========================================================================

async def test_route_correct_key_returns_url_and_token(app, client):
    from gaottt.multiverse.supervisor import PROBE_OK

    body = await _make_universe(client)
    with patch("gaottt.multiverse.supervisor._probe_backend_with_token",
               AsyncMock(return_value=PROBE_OK)):
        r = await client.post("/route", json={"api_key": body["api_key"]})
    assert r.status_code == 200
    data = r.json()
    assert data["url"].startswith("http://127.0.0.1:")
    assert data["token"]


# ===========================================================================
# 6. route — wrong key
# ===========================================================================

async def test_route_wrong_key_returns_401(client):
    body = await _make_universe(client)
    r = await client.post("/route", json={"api_key": "totally-wrong"})
    assert r.status_code == 401
    _ = body  # universe exists but we use a bad key


# ===========================================================================
# 7. route — universe not active (deleted) -> 404
# ===========================================================================

async def test_route_inactive_universe_returns_404(client, stub_registry,
                                                    tmp_multiverse_root):
    # delete_universe revokes the key (-> 401); to reach the status!=active
    # branch with a *still-valid* key we orphan the universe instead: remove its
    # dir then reconcile, which flips status to 'orphan' without touching keys.
    body = await _make_universe(client)
    uid = body["universe_id"]
    udir = tmp_multiverse_root / "universes" / uid
    shutil.rmtree(udir)
    await stub_registry.reconcile()
    assert (await stub_registry.get_universe(uid))["status"] == "orphan"

    r = await client.post("/route", json={"api_key": body["api_key"]})
    assert r.status_code == 404


# ===========================================================================
# 8. delete — trash move + registry status + key revoke
# ===========================================================================

async def test_delete_moves_to_trash_and_revokes_key(client, stub_registry,
                                                     tmp_multiverse_root):
    from gaottt.multiverse.supervisor import PROBE_DOWN

    body = await _make_universe(client)
    uid = body["universe_id"]
    udir = tmp_multiverse_root / "universes" / uid
    assert udir.exists()

    # backend down (mocked) so the stop-wait returns immediately
    with patch("gaottt.multiverse.supervisor._probe_backend_with_token",
               AsyncMock(return_value=PROBE_DOWN)):
        r = await client.delete(
            f"/admin/universes/{uid}", headers=_admin_headers()
        )
    assert r.status_code == 200
    assert r.json() == {"status": "deleted"}

    # dir moved to trash, not physically removed
    assert not udir.exists()
    assert (tmp_multiverse_root / "trash" / uid).exists()

    # registry status flipped + key revoked
    row = await stub_registry.get_universe(uid)
    assert row["status"] == "deleted"
    assert await stub_registry.verify_api_key(body["api_key"]) is None


async def test_delete_missing_universe_returns_404(client):
    r = await client.delete(
        "/admin/universes/does-not-exist", headers=_admin_headers()
    )
    assert r.status_code == 404


# ===========================================================================
# 9. list
# ===========================================================================

async def test_list_universes_includes_all(client):
    a = await _make_universe(client, owner="alice")
    b = await _make_universe(client, owner="bob")
    r = await client.get("/admin/universes", headers=_admin_headers())
    assert r.status_code == 200
    ids = {row["universe_id"] for row in r.json()}
    assert {a["universe_id"], b["universe_id"]} <= ids


# ===========================================================================
# 10. _ensure_backend spawn lock — concurrent ensure spawns once
# ===========================================================================

async def test_concurrent_ensure_spawns_backend_once(app, client):
    from gaottt.multiverse.supervisor import PROBE_DOWN, PROBE_OK

    body = await _make_universe(client)
    api_key = body["api_key"]

    # probe sequence (lock-serialised): route1 init=DOWN, route1 readiness=OK,
    # route2 init=OK. Extra OKs are harmless padding.
    probe = AsyncMock(side_effect=[PROBE_DOWN, PROBE_OK, PROBE_OK, PROBE_OK, PROBE_OK])
    popen = MagicMock()

    with patch("gaottt.multiverse.supervisor._probe_backend_with_token", probe), \
         patch("gaottt.multiverse.supervisor.subprocess.Popen", popen):
        results = await asyncio.gather(
            client.post("/route", json={"api_key": api_key}),
            client.post("/route", json={"api_key": api_key}),
        )
    assert all(r.status_code == 200 for r in results)
    assert popen.call_count == 1


# ===========================================================================
# 11. token read-back — existing backend.token is reused (no spawn)
# ===========================================================================

async def test_existing_token_reused_no_spawn(app, client, tmp_multiverse_root):
    from gaottt.multiverse.supervisor import PROBE_OK

    body = await _make_universe(client)
    uid = body["universe_id"]
    # pre-write a backend token (simulating an already-running backend)
    token_file = tmp_multiverse_root / "universes" / uid / "backend.token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("pre-existing-token\n", encoding="utf-8")

    probe = AsyncMock(return_value=PROBE_OK)
    popen = MagicMock()
    with patch("gaottt.multiverse.supervisor._probe_backend_with_token", probe), \
         patch("gaottt.multiverse.supervisor.subprocess.Popen", popen):
        r = await client.post("/route", json={"api_key": body["api_key"]})
    assert r.status_code == 200
    # the existing token was used for the probe and returned
    probe.assert_called()
    _, call_kwargs = probe.call_args
    # host/port positional; token passed as 3rd positional
    assert probe.call_args.args[2] == "pre-existing-token"
    assert r.json()["token"] == "pre-existing-token"
    popen.assert_not_called()


# ===========================================================================
# 12. token stale path
# ===========================================================================

async def test_token_stale_reread_then_success(app, client, tmp_multiverse_root):
    """probe 401 -> reread backend.token (rotated) -> re-probe OK, no spawn."""
    from gaottt.multiverse.supervisor import PROBE_OK, PROBE_UNAUTHORIZED

    body = await _make_universe(client)
    uid = body["universe_id"]
    sup = app.state.supervisor

    # initial token T1; the reread yields a rotated T2 (another supervisor wrote it)
    t1 = "stale-token-1"
    t2 = "rotated-token-2"
    token_file = tmp_multiverse_root / "universes" / uid / "backend.token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(t1 + "\n", encoding="utf-8")

    load = patch.object(sup, "_load_token", side_effect=[t1, t2])
    probe = patch(
        "gaottt.multiverse.supervisor._probe_backend_with_token",
        AsyncMock(side_effect=[PROBE_UNAUTHORIZED, PROBE_OK]),
    )
    popen = MagicMock()
    with load, probe, patch("gaottt.multiverse.supervisor.subprocess.Popen", popen):
        r = await client.post("/route", json={"api_key": body["api_key"]})
    assert r.status_code == 200
    assert r.json()["token"] == t2
    popen.assert_not_called()


async def test_token_stale_persistent_unauthorized_respawns(app, client):
    """probe 401 -> reread (same) -> still 401 -> respawn with fresh token."""
    from gaottt.multiverse.supervisor import PROBE_OK, PROBE_UNAUTHORIZED

    body = await _make_universe(client)
    sup = app.state.supervisor

    t1 = "stale-token-1"
    load = patch.object(sup, "_load_token", side_effect=[t1, t1])
    # probe(T1)=UNAUTH, reread probe(T1)=UNAUTH, then readiness probe(fresh)=OK
    probe = patch(
        "gaottt.multiverse.supervisor._probe_backend_with_token",
        AsyncMock(side_effect=[PROBE_UNAUTHORIZED, PROBE_UNAUTHORIZED, PROBE_OK]),
    )
    popen = MagicMock()
    with load, probe, patch("gaottt.multiverse.supervisor.subprocess.Popen", popen):
        r = await client.post("/route", json={"api_key": body["api_key"]})
    assert r.status_code == 200
    assert popen.call_count == 1
    # respawn rotates the token
    assert r.json()["token"] != t1


# ===========================================================================
# 13. file modes — backend.token 0600, manifest.json 0600, universe dir 0700
# ===========================================================================

async def test_file_modes_token_and_universe_dir(app, client, tmp_multiverse_root):
    from gaottt.multiverse.supervisor import PROBE_OK
    from gaottt.store.manifest import MANIFEST_FILENAME

    body = await _make_universe(client)
    uid = body["universe_id"]
    udir = tmp_multiverse_root / "universes" / uid
    # universe dir is 0700 at creation time
    assert (os.stat(udir).st_mode & 0o777) == 0o700

    # manifest.json is hardened to 0600 at creation time (carries the embedder
    # identity; leaking it would let an attacker fingerprint the backend).
    manifest_file = udir / MANIFEST_FILENAME
    assert manifest_file.exists()
    assert (os.stat(manifest_file).st_mode & 0o777) == 0o600

    # route triggers ensure_backend which writes backend.token
    with patch("gaottt.multiverse.supervisor._probe_backend_with_token",
               AsyncMock(return_value=PROBE_OK)):
        await client.post("/route", json={"api_key": body["api_key"]})

    token_file = udir / "backend.token"
    assert token_file.exists()
    assert (os.stat(token_file).st_mode & 0o777) == 0o600


# ===========================================================================
# 14. lifespan hardens the multiverse root to 0700
# ===========================================================================
#
# ASGITransport skips the app lifespan, so the root chmod at lifespan startup
# is invisible to the HTTP-level tests above. Drive the lifespan context
# directly to prove production startup tightens the root before the registry
# populates it. (owner.lock is created by the backend process via lease.py,
# never by the supervisor, so its mode is out of the supervisor's test scope.)

async def test_lifespan_hardens_multiverse_root_to_0700(tmp_path: Path):
    from gaottt.multiverse.registry import MultiverseRegistry
    from gaottt.multiverse.supervisor import create_supervisor_app

    root = tmp_path / "multiverse"
    root.mkdir(parents=True, exist_ok=True)
    # Start permissive so the lifespan is provably what tightens it (a root
    # already at 0700 would make the assertion vacuous).
    os.chmod(root, 0o755)
    assert (os.stat(root).st_mode & 0o777) == 0o755

    config = GaOTTTConfig(
        multiverse_root=str(root),
        supervisor_admin_key=ADMIN_KEY,
        embedder_endpoint="http://127.0.0.1:9999",
    )
    registry = MultiverseRegistry(root)
    app = create_supervisor_app(config, registry)

    async with app.router.lifespan_context(app):
        assert (os.stat(root).st_mode & 0o777) == 0o700


# ===========================================================================
# 15. B4 — readiness probe: only a successful MCP initialize() is PROBE_OK
# ===========================================================================

async def test_probe_initialize_failure_returns_down_even_with_http_200():
    """B4: PROBE_OK must require a successful MCP ``initialize()``.

    A server that answers a plain HTTP GET with 200 but whose MCP handshake
    fails is NOT ready. The pre-fix code fell through to ``return PROBE_OK``
    on any non-401 response — a false positive that spawn-readiness polling
    relied on, mistaking "something answered" for "MCP is ready".
    """
    from gaottt.multiverse.supervisor import _probe_backend_with_token, PROBE_DOWN

    # MCP handshake raises (server up but not MCP-ready / protocol mismatch).
    mock_resp = MagicMock(status_code=200)
    mock_http_client = AsyncMock()
    mock_http_client.get = AsyncMock(return_value=mock_resp)
    # `async with httpx.AsyncClient(...) as http_client:` binds http_client
    # to __aenter__'s return value — point it back at the configured mock.
    mock_http_client.__aenter__.return_value = mock_http_client

    with patch(
        "gaottt.multiverse.supervisor.streamablehttp_client",
        side_effect=RuntimeError("MCP initialize failed"),
    ), patch(
        "gaottt.multiverse.supervisor.httpx.AsyncClient",
        return_value=mock_http_client,
    ):
        result = await _probe_backend_with_token(
            "127.0.0.1", 9999, "tok", timeout=1.0,
        )

    assert result == PROBE_DOWN


async def test_probe_initialize_success_returns_ok():
    """B4 companion: a successful MCP ``initialize()`` still yields PROBE_OK
    (the fix narrows OK to this path only — make sure the happy path holds)."""
    from gaottt.multiverse.supervisor import _probe_backend_with_token, PROBE_OK

    mock_session = AsyncMock()  # initialize() is an async no-op (succeeds)

    class _StreamableCM:
        async def __aenter__(self):
            # streamablehttp_client unpacks as (read, write, _)
            return (object(), object(), object())

        async def __aexit__(self, *exc):
            return False

    class _SessionCM:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *exc):
            return False

    with patch(
        "gaottt.multiverse.supervisor.streamablehttp_client",
        return_value=_StreamableCM(),
    ), patch(
        "gaottt.multiverse.supervisor.ClientSession",
        return_value=_SessionCM(),
    ):
        result = await _probe_backend_with_token(
            "127.0.0.1", 9999, "tok", timeout=1.0,
        )

    assert result == PROBE_OK


# ===========================================================================
# 16. B1 — concurrent universe creation is serialized (distinct ports)
# ===========================================================================

async def test_concurrent_universe_creates_serialized_to_distinct_ports(app, client):
    """B1: two concurrent ``POST /admin/universes`` must not race on port
    allocation. ``_create_lock`` serializes creation so each lands a distinct
    port. Determinism: a tracking wrapper around ``allocate_port`` forces the
    race window open (yields mid-call); without the lock both calls overlap
    and both reserve the same port.
    """
    registry = app.state.registry
    original_allocate = registry.allocate_port
    in_flight = 0
    max_in_flight = 0

    async def tracking_allocate(start, end):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            # Open the race window wide: a 50ms hold guarantees the gathered
            # sibling coroutine also reaches allocate_port unless a lock
            # serializes them.
            await asyncio.sleep(0.05)
            return await original_allocate(start, end)
        finally:
            in_flight -= 1

    registry.allocate_port = tracking_allocate  # type: ignore[method-assign]
    try:
        with _embedder_ok():
            results = await asyncio.gather(
                client.post(
                    "/admin/universes", json={"owner_label": "a"},
                    headers=_admin_headers(),
                ),
                client.post(
                    "/admin/universes", json={"owner_label": "b"},
                    headers=_admin_headers(),
                ),
            )
    finally:
        registry.allocate_port = original_allocate  # type: ignore[method-assign]

    assert all(r.status_code == 201 for r in results), [r.text for r in results]
    ports = {r.json()["port"] for r in results}
    assert len(ports) == 2, f"expected 2 distinct ports, got {sorted(ports)}"
    # The lock serialized allocation — at no point were two in flight at once.
    assert max_in_flight == 1, (
        f"allocate_port ran concurrently (max_in_flight={max_in_flight}); "
        "create_lock failed to serialize"
    )


# ===========================================================================
# 17. B3 — delete serializes with the per-universe spawn lock
# ===========================================================================

async def test_delete_acquires_spawn_lock(app, client):
    """B3: DELETE must hold the per-universe spawn lock while it stops the
    backend and moves the dir, so a concurrent ``/route``->``ensure_backend``
    cannot spawn onto a universe mid-delete. While the lock is externally
    held, delete blocks; once released, it completes."""
    from gaottt.multiverse.supervisor import PROBE_DOWN

    body = await _make_universe(client)
    uid = body["universe_id"]
    sup = app.state.supervisor
    # The same lock instance ensure_backend acquires.
    lock = sup._spawn_lock(uid)

    await lock.acquire()
    try:
        with patch("gaottt.multiverse.supervisor._probe_backend_with_token",
                   AsyncMock(return_value=PROBE_DOWN)):
            delete_task = asyncio.create_task(
                client.delete(f"/admin/universes/{uid}", headers=_admin_headers())
            )
            # delete reached the lock and is parked — not yet done.
            await asyncio.sleep(0.15)
            assert not delete_task.done(), (
                "delete completed without acquiring the spawn lock"
            )
    finally:
        lock.release()

    r = await delete_task
    assert r.status_code == 200
    assert r.json() == {"status": "deleted"}


# ===========================================================================
# 18. B2 — known PID: SIGTERM -> port down -> trash move, PID popped
# ===========================================================================

async def test_stop_backend_known_pid_sigterms_and_moves_to_trash(
    app, client, stub_registry, tmp_multiverse_root,
):
    """B2: when the supervisor tracked the backend's PID, delete SIGTERMs it,
    polls the process until it exits, then moves the dir to trash. The
    recorded PID is cleaned up. (No real process is launched — ``os.kill`` is
    mocked and the fake PID is not a real child, so ``os.waitpid`` reports it
    as already-exited and the kill-poll resolves at once.)"""
    body = await _make_universe(client)
    uid = body["universe_id"]
    sup = app.state.supervisor
    udir = tmp_multiverse_root / "universes" / uid

    # Simulate a tracked backend. The fake PID is not a child of this process,
    # so os.waitpid(WNOHANG) raises ChildProcessError -> treated as exited ->
    # the kill-poll returns immediately without ever needing the signal to
    # take effect.
    fake_pid = 999901
    sup._backend_pids[uid] = fake_pid

    kill_mock = MagicMock()
    with patch("gaottt.multiverse.supervisor.os.kill", kill_mock):
        r = await client.delete(
            f"/admin/universes/{uid}", headers=_admin_headers()
        )

    assert r.status_code == 200, r.text
    # SIGTERM was issued against the tracked PID.
    sent_signals = [call.args[1] for call in kill_mock.call_args_list]
    assert signal.SIGTERM in sent_signals, (
        f"SIGTERM not sent; signals were {sent_signals}"
    )
    # PID entry cleaned up after the stop sequence.
    assert uid not in sup._backend_pids
    # dir moved to trash, registry status flipped.
    assert not udir.exists()
    assert (tmp_multiverse_root / "trash" / uid).exists()
    row = await stub_registry.get_universe(uid)
    assert row["status"] == "deleted"


# ===========================================================================
# 19. B2 — unknown PID + port alive -> 409 Conflict, dir untouched
# ===========================================================================

async def test_stop_backend_unknown_pid_alive_returns_409(
    app, client, tmp_multiverse_root,
):
    """B2: after a supervisor restart the backend PID is unknown. If the
    backend is still serving, delete must refuse with 409 (cannot safely kill
    what it cannot track) and leave the universe dir in place."""
    from gaottt.multiverse.supervisor import PROBE_OK

    body = await _make_universe(client)
    uid = body["universe_id"]
    sup = app.state.supervisor
    udir = tmp_multiverse_root / "universes" / uid

    # No PID recorded (restart scenario) and the backend is still alive.
    assert uid not in sup._backend_pids

    with patch("gaottt.multiverse.supervisor._probe_backend_with_token",
               AsyncMock(return_value=PROBE_OK)):
        r = await client.delete(
            f"/admin/universes/{uid}", headers=_admin_headers()
        )

    assert r.status_code == 409
    detail = r.json()["detail"].lower()
    assert "alive" in detail or "pid" in detail, detail
    # The dir was NOT moved out from under the live backend.
    assert udir.exists()


# ===========================================================================
# 20. B3 residual — _ensure_locked re-checks status inside the spawn lock
# ===========================================================================
#
# /route verifies status==active OUTSIDE the spawn lock, then calls
# ensure_backend which acquires it. A concurrent DELETE can flip status to
# 'deleted' (and move the dir) in that window. _ensure_locked must re-check
# status once it holds the lock and refuse to spawn onto a half-deleted
# universe. The failure maps to 404, not 500 — the universe is genuinely gone
# from the router's perspective. (A distinct exception is used rather than
# bare RuntimeError so the readiness-timeout RuntimeError at the tail of
# _ensure_locked stays a 500-class condition, not a 404.)

@pytest.mark.parametrize("status_value", ["deleted", "orphan"])
async def test_ensure_locked_rechecks_status_raises_when_not_active(status_value):
    """_ensure_locked re-fetches status inside the lock and raises when the
    universe is no longer active. Parametrized over the two non-active
    statuses a concurrent delete/orphan can leave behind."""
    from gaottt.multiverse.supervisor import _Supervisor

    config = GaOTTTConfig(
        multiverse_root="/tmp/gaottt-test-ensure-locked-inactive",
        supervisor_admin_key=ADMIN_KEY,
    )
    mock_registry = MagicMock()
    mock_registry.get_universe = AsyncMock(
        return_value={"universe_id": "uid123", "status": status_value}
    )
    sup = _Supervisor(config, mock_registry)

    with pytest.raises(RuntimeError, match="no longer active"):
        await sup._ensure_locked(
            "uid123", 9999, "http://127.0.0.1:9999/mcp",
            {"universe_id": "uid123", "status": status_value},
        )


async def test_ensure_locked_rechecks_status_raises_when_universe_gone():
    """Same re-check, but the universe row is entirely gone (None) — e.g. a
    hard purge ran between the outside-lock check and here."""
    from gaottt.multiverse.supervisor import _Supervisor

    config = GaOTTTConfig(
        multiverse_root="/tmp/gaottt-test-ensure-locked-gone",
        supervisor_admin_key=ADMIN_KEY,
    )
    mock_registry = MagicMock()
    mock_registry.get_universe = AsyncMock(return_value=None)
    sup = _Supervisor(config, mock_registry)

    with pytest.raises(RuntimeError, match="no longer active"):
        await sup._ensure_locked(
            "uid123", 9999, "http://127.0.0.1:9999/mcp",
            {"universe_id": "uid123", "status": "active"},
        )


async def test_ensure_locked_status_active_proceeds_to_probe(monkeypatch):
    """Happy path: when status is still active inside the lock, the re-check
    passes and _ensure_locked proceeds to its normal token/probe flow (the
    re-check must not regress the existing behaviour)."""
    from gaottt.multiverse.supervisor import _Supervisor, PROBE_OK

    config = GaOTTTConfig(
        multiverse_root="/tmp/gaottt-test-ensure-locked-active",
        supervisor_admin_key=ADMIN_KEY,
    )
    mock_registry = MagicMock()
    mock_registry.get_universe = AsyncMock(
        return_value={"universe_id": "uid", "status": "active"}
    )
    sup = _Supervisor(config, mock_registry)

    monkeypatch.setattr(sup, "_load_token", lambda _uid: "reuse-token")
    monkeypatch.setattr(
        "gaottt.multiverse.supervisor._probe_backend_with_token",
        AsyncMock(return_value=PROBE_OK),
    )

    url, token = await sup._ensure_locked(
        "uid", 9999, "http://127.0.0.1:9999/mcp", {"universe_id": "uid"},
    )
    assert token == "reuse-token"
    assert url == "http://127.0.0.1:9999/mcp"


async def test_route_maps_inflight_delete_to_404(app, client, stub_registry):
    """B3 residual end-to-end: /route's outside-lock status check sees
    'active', then a concurrent delete flips status before _ensure_locked's
    inside-lock re-check. The route must return 404 (not 500, and not a spawn
    onto a moved dir).

    The race is simulated by swapping registry.get_universe to return the
    active row on the first (outside-lock) call and a 'deleted' row on the
    second (inside-lock) call — exactly the interleaving a concurrent DELETE
    produces."""
    body = await _make_universe(client)
    uid = body["universe_id"]

    active_row = await stub_registry.get_universe(uid)
    assert active_row["status"] == "active"
    deleted_row = dict(active_row, status="deleted")

    calls = {"n": 0}
    original_get = stub_registry.get_universe

    async def racing_get(universe_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return active_row        # route's outside-lock check: passes
        return deleted_row           # _ensure_locked's inside-lock check: deleted

    stub_registry.get_universe = racing_get  # type: ignore[method-assign]
    try:
        r = await client.post("/route", json={"api_key": body["api_key"]})
    finally:
        stub_registry.get_universe = original_get  # type: ignore[method-assign]

    assert r.status_code == 404
    assert r.json()["detail"] == "universe not available"
    # The inside-lock re-check actually ran (>= 2 get_universe calls).
    assert calls["n"] >= 2


# ===========================================================================
# 21. B2 residual — tracked backend survives SIGKILL -> _BackendAliveConflict
# ===========================================================================

async def test_kill_tracked_backend_sigkill_survivor_raises_conflict(app, client):
    """B2 residual: if the tracked backend is still alive after SIGKILL,
    ``_kill_tracked_backend`` must raise ``_BackendAliveConflict`` instead of
    logging, clearing the PID, and proceeding. The PID is retained so a retry
    targets the same process."""
    from gaottt.multiverse.supervisor import _BackendAliveConflict

    body = await _make_universe(client)
    uid = body["universe_id"]
    sup = app.state.supervisor

    fake_pid = 999902
    sup._backend_pids[uid] = fake_pid

    kill_mock = MagicMock()
    # _poll_process_dead always False: the process survives both SIGTERM and
    # SIGKILL polling (a wedged / unkillable backend).
    with patch("gaottt.multiverse.supervisor.os.kill", kill_mock), \
         patch.object(sup, "_poll_process_dead", AsyncMock(return_value=False)):
        with pytest.raises(_BackendAliveConflict):
            await sup._kill_tracked_backend(uid, fake_pid)

    # PID retained so a retry can SIGTERM/SIGKILL the same process.
    assert sup._backend_pids[uid] == fake_pid
    sent = [c.args[1] for c in kill_mock.call_args_list]
    assert signal.SIGTERM in sent
    assert signal.SIGKILL in sent


async def test_delete_sigkill_survivor_returns_409_and_leaves_dir(
    app, client, tmp_multiverse_root,
):
    """B2 residual end-to-end: a tracked backend that survives SIGKILL makes
    DELETE return 409 Conflict and leaves the universe dir in place. The
    recorded PID is retained for a manual kill + retry."""
    body = await _make_universe(client)
    uid = body["universe_id"]
    sup = app.state.supervisor
    udir = tmp_multiverse_root / "universes" / uid

    fake_pid = 999903
    sup._backend_pids[uid] = fake_pid

    kill_mock = MagicMock()
    with patch("gaottt.multiverse.supervisor.os.kill", kill_mock), \
         patch.object(sup, "_poll_process_dead", AsyncMock(return_value=False)):
        r = await client.delete(
            f"/admin/universes/{uid}", headers=_admin_headers()
        )

    assert r.status_code == 409
    detail = r.json()["detail"].lower()
    assert "sigkill" in detail or "survived" in detail, detail
    # Dir NOT moved; PID retained for retry.
    assert udir.exists()
    assert sup._backend_pids[uid] == fake_pid


# ===========================================================================
# 22. B3 residual — delete takes the cross-process fcntl.flock
# ===========================================================================

async def test_delete_acquires_cross_process_flock(app, client):
    """B3 residual: delete must take the same cross-process ``fcntl.flock`` on
    ``<universe_dir>/.spawn.lock`` that ``ensure_backend`` takes, so a
    concurrent supervisor process cannot interleave a spawn with the trash
    move. Both LOCK_EX (acquire) and LOCK_UN (release) must be observed."""
    import fcntl as _fcntl
    from gaottt.multiverse.supervisor import PROBE_DOWN

    body = await _make_universe(client)
    uid = body["universe_id"]

    flock_mock = MagicMock()
    with patch("gaottt.multiverse.supervisor._probe_backend_with_token",
               AsyncMock(return_value=PROBE_DOWN)), \
         patch("gaottt.multiverse.supervisor.fcntl.flock", flock_mock):
        r = await client.delete(
            f"/admin/universes/{uid}", headers=_admin_headers()
        )

    assert r.status_code == 200, r.text
    flags = [c.args[1] for c in flock_mock.call_args_list]
    assert _fcntl.LOCK_EX in flags, f"LOCK_EX not acquired; flags={flags}"
    assert _fcntl.LOCK_UN in flags, f"LOCK_UN not released; flags={flags}"


# ===========================================================================
# 23. PermissionError on signal -> _BackendAliveConflict (PID retained)
# ===========================================================================

async def test_kill_tracked_backend_sigterm_permission_denied_raises_conflict(app, client):
    """SIGTERM raises PermissionError (e.g. the backend was re-parented to
    init after a supervisor restart and is now owned by another uid). The
    stop must refuse via ``_BackendAliveConflict`` rather than clearing the
    PID and letting the delete move the dir under a live backend. The PID is
    retained so a manual kill + retry targets the same process."""
    from gaottt.multiverse.supervisor import _BackendAliveConflict

    body = await _make_universe(client)
    uid = body["universe_id"]
    sup = app.state.supervisor

    fake_pid = 999904
    sup._backend_pids[uid] = fake_pid

    kill_mock = MagicMock(side_effect=PermissionError("not permitted"))
    with patch("gaottt.multiverse.supervisor.os.kill", kill_mock):
        with pytest.raises(_BackendAliveConflict):
            await sup._kill_tracked_backend(uid, fake_pid)

    # PID retained — a retry must target the same process, not fall through
    # to the port-probe path.
    assert sup._backend_pids[uid] == fake_pid
    # Only SIGTERM was attempted (escalation to SIGKILL never reached).
    sent = [c.args[1] for c in kill_mock.call_args_list]
    assert sent == [signal.SIGTERM]


async def test_kill_tracked_backend_sigkill_permission_denied_raises_conflict(app, client):
    """SIGTERM lands but the backend survives the wait; escalation to SIGKILL
    raises PermissionError. Same contract as the SIGTERM case: refuse via
    ``_BackendAliveConflict`` and retain the PID."""
    from gaottt.multiverse.supervisor import _BackendAliveConflict

    body = await _make_universe(client)
    uid = body["universe_id"]
    sup = app.state.supervisor

    fake_pid = 999905
    sup._backend_pids[uid] = fake_pid

    def kill_side_effect(_pid, sig):
        if sig == signal.SIGTERM:
            return
        raise PermissionError("not permitted")

    kill_mock = MagicMock(side_effect=kill_side_effect)
    # SIGTERM delivered but the process did not exit within the wait window.
    with patch("gaottt.multiverse.supervisor.os.kill", kill_mock), \
         patch.object(sup, "_poll_process_dead", AsyncMock(return_value=False)):
        with pytest.raises(_BackendAliveConflict):
            await sup._kill_tracked_backend(uid, fake_pid)

    assert sup._backend_pids[uid] == fake_pid
    sent = [c.args[1] for c in kill_mock.call_args_list]
    assert signal.SIGTERM in sent
    assert signal.SIGKILL in sent
