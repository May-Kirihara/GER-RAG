"""MV3 Multiverse — universe supervisor (WP-3).

A FastAPI control plane that maps **users** (per-universe API keys) to
**universe backends** (per-universe ``mcp_server`` processes on isolated
data directories). It owns four things:

1. **Admin API** (``/admin/universes``) — create / delete / list universes,
   guarded by an admin key (empty key = fail-fast; admin endpoints are never
   exposed unauthenticated).
2. **Routing** (``/route``) — resolve an API key to its universe, then ensure
   that universe's backend is reachable and return ``(url, token)``.
3. **Per-universe backend lifecycle** (``_Supervisor.ensure_backend``) —
   spawn / probe / token-rotate each universe's ``mcp_server`` subprocess with
   an **explicit spawn env** (no GAOTTT_* inheritance from the supervisor's
   own environment) and a **two-layer spawn lock** (asyncio.Lock for in-process
   serialization + ``fcntl.flock`` on ``<universe_dir>/.spawn.lock`` for
   cross-process / supervisor-restart safety).
4. **Embedder validation** — at universe-creation time, GET the embedding
   service's ``/info`` and refuse to create the universe if it is unreachable
   or malformed.

This is a pure ops / coordination layer: it imports no physics (``gaottt/core``)
and mutates no store/ code. The feature is inert unless
``config.multiverse_root`` is set.
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel

from gaottt.config import GaOTTTConfig
from gaottt.multiverse.control_client import (
    ROUTE_RESOLUTION,
    UNIVERSE_CREATE,
    UNIVERSE_DELETE,
    ControlClient,
)
from gaottt.multiverse.registry import TRASH_SUBDIR, UNIVERSES_SUBDIR, MultiverseRegistry
from gaottt.store.manifest import MANIFEST_FILENAME, UniverseManifest, write_manifest

logger = logging.getLogger(__name__)

# Backends are MCP ``streamable-http`` servers bound to loopback only. The idle
# timeout handed to the backend is long enough for interactive sessions but
# short enough that an abandoned universe self-shuts (cold-war dead-man-switch).
HOST = "127.0.0.1"
BACKEND_IDLE_TIMEOUT = 300.0

# Probe result sentinels. A richer-than-bool return is required so the
# token-stale path can tell a 401 (backend alive, our token is wrong) from a
# connection refusal (backend down, needs spawning) — a plain bool cannot.
PROBE_OK = "ok"
PROBE_UNAUTHORIZED = "unauthorized"
PROBE_DOWN = "down"

# B2: how long to wait for a tracked backend to exit after a SIGTERM before
# escalating to SIGKILL, and the grace after SIGKILL. Liveness is tested via
# os.waitpid (process exit), not the port going silent — a backend whose port
# closed but is still in graceful shutdown may still flush write-behind buffers
# to the dir the delete is about to move.
STOP_SIGTERM_WAIT = 5.0
STOP_SIGKILL_WAIT = 2.0


class EmbedderValidationError(RuntimeError):
    """Raised when the embedding service ``/info`` lookup fails or is malformed."""


class _BackendAliveConflict(RuntimeError):
    """Raised by ``_stop_backend`` when a backend is still serving but its PID
    is unknown to this supervisor (e.g. after a restart), so it cannot be
    safely killed. The delete handler maps this to ``409 Conflict``."""


class _UniverseInactive(RuntimeError):
    """Raised by ``_ensure_locked`` when the universe's status is no longer
    ``active`` by the time the spawn lock is held. The route handler verifies
    status==active *outside* the lock, then ``ensure_backend`` acquires it; a
    concurrent delete can flip status to 'deleted' and move the dir in that
    window. This inside-lock re-check closes that race. The route handler maps
    this to ``404 Not Found``. A distinct subclass (rather than bare
    RuntimeError) keeps the readiness-timeout RuntimeError at the tail of
    ``_ensure_locked`` a 500-class condition."""


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------

class CreateUniverseBody(BaseModel):
    owner_label: str
    embedder_id: str | None = None
    # J11: optional tenant_id for control-plane usage attribution. When
    # omitted the supervisor resolves to ``config.control_default_tenant_id``
    # or ``"default"``. The local registry has no tenant column (MV3 schema),
    # so this field is control-plane metadata only — local behavior is
    # unchanged when it is absent (default 不変).
    tenant_id: str | None = None


class RouteBody(BaseModel):
    api_key: str


# ---------------------------------------------------------------------------
# embedder validation (sync httpx — the test mock seam patches httpx.Client.get)
# ---------------------------------------------------------------------------

def _validate_embedder(config: GaOTTTConfig) -> dict:
    """GET ``<embedder_endpoint>/info`` and return the parsed info dict.

    Raises :class:`EmbedderValidationError` when the service is unreachable,
    returns a non-200 status, or omits ``model_name`` / ``dimension``.
    """
    if not config.embedder_endpoint:
        raise EmbedderValidationError("embedder_endpoint is not configured")
    url = config.embedder_endpoint.rstrip("/") + "/info"
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        raise EmbedderValidationError(f"embedder service unreachable: {exc}") from exc
    if response.status_code != 200:
        raise EmbedderValidationError(
            f"embedder /info returned status {response.status_code}"
        )
    try:
        info = response.json()
    except ValueError as exc:  # invalid JSON
        raise EmbedderValidationError("embedder /info returned invalid JSON") from exc
    if not info.get("model_name") or not info.get("dimension"):
        raise EmbedderValidationError(
            "embedder /info missing model_name or dimension"
        )
    return info


# ---------------------------------------------------------------------------
# backend probe
# ---------------------------------------------------------------------------

async def _probe_backend_with_token(
    host: str, port: int, token: str, timeout: float = 3.0,
) -> str:
    """Probe a backend's liveness + auth, returning a tri-state sentinel.

    ``PROBE_OK`` — a full MCP ``initialize`` handshake succeeded (the backend
    is ready *and* the token was accepted); this is the only path that returns
    OK. ``PROBE_UNAUTHORIZED`` — the server is listening but rejected the token
    (401); the caller should reread ``backend.token`` (another
    supervisor/respawn may have rotated it) and retry once before re-spawning.
    ``PROBE_DOWN`` — the backend is not MCP-ready: nothing answered, or
    something answered but the ``initialize`` handshake did not complete.
    """
    url = f"http://{host}:{port}/mcp"
    headers = {"Authorization": f"Bearer {token}"}

    # Full MCP initialize confirms both readiness and auth in one round trip.
    try:
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=timeout)
                return PROBE_OK
    except Exception:  # noqa: BLE001 — any handshake failure falls to classification
        pass

    # The handshake failed; classify just enough to recover the token-stale
    # path. The token middleware runs before any MCP logic, so a 401 reliably
    # means "token rejected". Anything else (connection refused, a 200 from a
    # not-yet-MCP-ready server, a 500, …) is NOT ready — readiness is asserted
    # only by the initialize() handshake above.
    try:
        async with httpx.AsyncClient(timeout=timeout) as http_client:
            resp = await http_client.get(url, headers=headers)
    except httpx.HTTPError:
        return PROBE_DOWN
    if resp.status_code == 401:
        return PROBE_UNAUTHORIZED
    return PROBE_DOWN


# ---------------------------------------------------------------------------
# supervisor
# ---------------------------------------------------------------------------

class _Supervisor:
    """Owns the per-universe backend spawn/probe/rotate machinery.

    All filesystem state lives under ``<multiverse_root>/universes/<uid>/``:
    ``backend.token`` (0600), ``.spawn.lock`` (flock target), and the data dir
    the backend is pointed at via ``GAOTTT_DATA_DIR``.
    """

    def __init__(
        self,
        config: GaOTTTConfig,
        registry: MultiverseRegistry,
        control_client: ControlClient | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._root = Path(config.multiverse_root)
        self._spawn_locks: dict[str, asyncio.Lock] = {}
        self._spawn_semaphore = asyncio.Semaphore(config.supervisor_spawn_concurrency)
        # MV4 WP-4: optional control-plane client. None = MV3 local-only mode
        # (default 不変). All call sites guard on ``self._control is not None``.
        self._control = control_client
        # B2: universe_id -> PID of the backend this supervisor spawned. Lets
        # _stop_backend SIGTERM/SIGKILL a tracked backend instead of moving its
        # data dir out from under a live process. Lost on supervisor restart;
        # the PID-unknown-but-alive case refuses the delete (409) rather than
        # risk corrupting a running backend.
        self._backend_pids: dict[str, int] = {}
        # B1: serialize universe creation so two concurrent POSTs cannot both
        # reserve the same port before either INSERTs it. (The partial UNIQUE
        # index on universes.port is the DB-level backstop.)
        self._create_lock = asyncio.Lock()
        # MV5: serialize the backup hook's scan+atomic-write so concurrent
        # create/delete hooks form an ordered series. Without this, two hooks
        # could each scan on-disk state out of order and the later scan's
        # (older) result could win via os.replace — a stale write. The lock
        # keeps scan+write in ONE critical section (round-2 review B2).
        self._backup_hook_lock = asyncio.Lock()

    # -- paths & tokens ----------------------------------------------------

    def _spawn_lock(self, uid: str) -> asyncio.Lock:
        # Lazy per-universe lock; setdefault is atomic under the GIL so two
        # coroutines cannot create distinct locks for the same uid.
        return self._spawn_locks.setdefault(uid, asyncio.Lock())

    def _universe_dir(self, uid: str) -> Path:
        return self._root / UNIVERSES_SUBDIR / uid

    def _token_path(self, uid: str) -> Path:
        return self._universe_dir(uid) / "backend.token"

    def _load_token(self, uid: str) -> str | None:
        """Read ``backend.token`` if present. Patch seam for token-stale tests."""
        path = self._token_path(uid)
        if path.exists():
            try:
                return path.read_text(encoding="utf-8").strip()
            except OSError:
                return None
        return None

    def _persist_token(self, uid: str, token: str) -> None:
        path = self._token_path(uid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n", encoding="utf-8")
        os.chmod(path, 0o600)

    # -- spawn env ---------------------------------------------------------

    def _build_spawn_env(self, universe_dir: Path, token: str) -> dict[str, str]:
        # Strip ALL GAOTTT_* from the supervisor's own environment, then overlay
        # only the universe-specific knobs. This kills the proxy-backend
        # env-inheritance trap (the supervisor's GAOTTT_DATA_DIR / owner-lease
        # state leaking into a managed backend) while preserving OS essentials
        # (PATH, HOME, ...) the subprocess needs to run.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GAOTTT_")}
        env.update({
            "GAOTTT_DATA_DIR": str(universe_dir),
            "GAOTTT_EMBEDDER_ENDPOINT": self._config.embedder_endpoint,
            "GAOTTT_OWNER_LEASE_ENABLED": "true",
            "GAOTTT_BACKEND_TOKEN": token,
        })
        return env

    # -- MV5 backup hook ---------------------------------------------------

    async def _run_backup_hook(self) -> None:
        """Regenerate the litestream config YAML after a create/delete.

        Best-effort, default-inert, scan+write serialized:

        * Returns immediately when ``litestream_config_path`` is empty (the
          default) — the hook is fully off for MV3/MV4-only deployments.
        * Acquires ``_backup_hook_lock`` and, INSIDE the single critical
          section, scans on-disk state via the pure
          :func:`generate_litestream_config` then atomic-writes the result
          (tmp + fsync + os.replace). Holding the lock across both scan and
          write is what prevents a stale-write: out-of-order completion of
          two concurrent hooks cannot let an older scan's result land last,
          because each hook rescans the latest on-disk state inside its own
          locked section (Codex review round-2 B2).
        * Any exception → ERROR log only. A backup misconfiguration must
          never fail the create/delete HTTP response (D2 best-effort).

        The pure function is imported lazily so a broken ``backup`` module
        never prevents supervisor import, and so tests can monkeypatch it.
        """
        target = self._config.litestream_config_path
        if not target:
            return
        from gaottt.multiverse.backup import (
            atomic_write_text,
            generate_litestream_config,
        )

        async with self._backup_hook_lock:
            try:
                # scan + atomic-write in ONE critical section (stale-write
                # fence). The rescan inside the lock sees the on-disk state
                # as of this hook's turn — including this create/delete's
                # own filesystem effect (dir creation / trash move), which
                # already completed before the hook fired.
                yaml_text = generate_litestream_config(self._root)
                atomic_write_text(Path(target), yaml_text)
            except Exception as exc:  # noqa: BLE001 — best-effort hook
                logger.error("backup hook failed: %s", exc)

    # -- spawn -------------------------------------------------------------

    def _spawn(self, universe: dict, token: str) -> int:
        """Launch a detached ``mcp_server --transport streamable-http`` for the
        universe. Returns the PID."""
        uid = universe["universe_id"]
        port = universe["port"]
        universe_dir = self._universe_dir(uid)
        universe_dir.mkdir(parents=True, exist_ok=True)

        log_path = self._root / "logs" / f"{uid}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "a", buffering=1)  # noqa: SIM115 — detached child owns it
        log_file.write(
            f"\n--- universe {uid} spawn at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
        )

        env = self._build_spawn_env(universe_dir, token)
        cmd = [
            sys.executable, "-m", "gaottt.server.mcp_server",
            "--transport", "streamable-http",
            "--host", HOST, "--port", str(port),
            "--idle-timeout", str(int(BACKEND_IDLE_TIMEOUT)),
        ]
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
            "env": env,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (  # type: ignore[attr-defined]
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603 — controlled argv
        return proc.pid

    async def _poll_ready(
        self, host: str, port: int, token: str, timeout: float,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await _probe_backend_with_token(host, port, token) == PROBE_OK:
                return True
            await asyncio.sleep(1.0)
        return False

    # -- ensure ------------------------------------------------------------

    async def ensure_backend(self, universe: dict) -> tuple[str, str]:
        """Return a ``(url, token)`` for the universe's backend, spawning if
        necessary. Two-layer locking makes spawn a critical section both
        in-process (asyncio.Lock) and cross-process (flock on .spawn.lock)."""
        uid = universe["universe_id"]
        port = universe["port"]
        url = f"http://{HOST}:{port}/mcp"
        universe_dir = self._universe_dir(uid)
        universe_dir.mkdir(parents=True, exist_ok=True)
        lock_path = universe_dir / ".spawn.lock"

        async with self._spawn_lock(uid):
            # flock is a blocking syscall — run it in a thread so the event loop
            # is not stalled. Within one process the asyncio.Lock already
            # serializes, so this only ever contends across processes.
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_EX)
                try:
                    return await self._ensure_locked(uid, port, url, universe)
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    async def _ensure_locked(
        self, uid: str, port: int, url: str, universe: dict,
    ) -> tuple[str, str]:
        # B3 residual: re-check status inside the lock. The route handler
        # verifies status==active before ensure_backend acquires the spawn
        # lock; a concurrent delete can flip status to 'deleted' and move the
        # dir in that window. Re-fetching here (serialised against delete's
        # own hold of this lock) closes the race.
        current = await self._registry.get_universe(uid)
        if current is None or current.get("status") != "active":
            status = current.get("status") if current else "gone"
            raise _UniverseInactive(
                f"universe {uid} is no longer active (status={status}) "
                f"during route"
            )
        token = self._load_token(uid)
        if token is None:
            token = secrets.token_urlsafe(32)
            self._persist_token(uid, token)

        probe = await _probe_backend_with_token(HOST, port, token)
        if probe == PROBE_OK:
            return url, token

        if probe == PROBE_UNAUTHORIZED:
            # Token stale: another supervisor instance or a respawn may have
            # rotated backend.token on disk. Reread + re-probe before respawning.
            reread = self._load_token(uid)
            if reread and reread != token:
                if await _probe_backend_with_token(HOST, port, reread) == PROBE_OK:
                    return url, reread
            # still unauthorized -> fall through to a fresh-token respawn.

        # PROBE_DOWN, or unauthorized-after-reread: (re)spawn with a fresh token.
        fresh = secrets.token_urlsafe(32)
        self._persist_token(uid, fresh)
        async with self._spawn_semaphore:
            pid = self._spawn(universe, fresh)
            # B2: record the PID as soon as the process is launched so a later
            # delete can SIGTERM it even if readiness polling then times out.
            self._backend_pids[uid] = pid
            ready = await self._poll_ready(
                HOST, port, fresh, self._config.supervisor_readiness_timeout,
            )
        if not ready:
            raise RuntimeError(
                f"universe {uid} backend did not become ready within "
                f"{self._config.supervisor_readiness_timeout}s"
            )
        # MV5 (Codex FINAL B1): the fresh backend just created gaottt.db on
        # disk. create_universe's own hook fired before the db existed, so
        # rescan here to close the new-universe config lag. No-op when the
        # litestream knob is unset (default-invariant).
        await self._run_backup_hook()
        return url, fresh

    # -- stop (delete path) ------------------------------------------------

    async def _stop_backend(self, universe: dict) -> None:
        """Stop the universe's backend before a delete moves its data dir.

        Two cases:

        * **PID known** (this supervisor spawned it): SIGTERM, poll the process
          until it exits (``STOP_SIGTERM_WAIT``), escalate to SIGKILL, poll
          once more (``STOP_SIGKILL_WAIT``). On exit the recorded PID is
          cleared; if the process survives SIGKILL, ``_BackendAliveConflict``
          is raised (-> 409) and the PID is retained.
        * **PID unknown** (supervisor restart, or the universe predates PID
          tracking): probe the port. If the backend is still serving we refuse
          with :class:`_BackendAliveConflict` (-> 409) — we cannot safely kill
          what we cannot track, and moving the dir would corrupt the live
          backend. If it is already down, the delete proceeds.

        We wait for the *process* to exit (not merely the port to go silent)
        before proceeding: a backend whose port closed but is still in graceful
        shutdown may still flush write-behind buffers to the dir we are about
        to move. ``os.waitpid`` also reaps the now-detached child so it does
        not linger as a zombie.
        """
        uid = universe["universe_id"]
        port = universe["port"]

        pid = self._backend_pids.get(uid)
        if pid is not None:
            await self._kill_tracked_backend(uid, pid)
            return

        # PID unknown: probe to decide whether it is safe to proceed.
        token = self._load_token(uid) or ""
        probe = await _probe_backend_with_token(HOST, port, token, timeout=1.0)
        if probe != PROBE_DOWN:
            raise _BackendAliveConflict(
                f"Cannot delete universe {uid}: backend is alive on port "
                f"{port} but its PID is unknown to this supervisor (it may "
                f"have been restarted). Stop the backend manually first."
            )
        # Backend is down — safe to proceed with the trash move.

    async def _kill_tracked_backend(self, uid: str, pid: int) -> None:
        """SIGTERM (then SIGKILL) the tracked backend and poll until the
        process exits, then clear the recorded PID.

        Raises :class:`_BackendAliveConflict` *without* clearing the PID when
        the backend cannot be safely stopped — either it survives SIGKILL, or
        we lack permission to signal it (e.g. it was re-parented to init after
        a supervisor restart and is now owned by another uid). Proceeding in
        either case would move the dir out from under a live backend; the
        delete handler maps the conflict to 409, and the retained PID lets a
        retry target the same process rather than falling through to the
        port-probe path (which would also refuse)."""
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._backend_pids.pop(uid, None)
            return
        except PermissionError:
            raise _BackendAliveConflict(
                f"Cannot signal backend pid={pid} for universe {uid} "
                f"(PermissionError) — process may be owned by another user. "
                f"Stop it manually (kill {pid}) and retry."
            )

        if await self._poll_process_dead(pid, STOP_SIGTERM_WAIT):
            self._backend_pids.pop(uid, None)
            return

        # Still alive after SIGTERM — escalate.
        logger.warning(
            "universe %s backend (pid %s) did not exit after SIGTERM within "
            "%.1fs; sending SIGKILL", uid, pid, STOP_SIGTERM_WAIT,
        )
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            raise _BackendAliveConflict(
                f"Cannot SIGKILL backend pid={pid} for universe {uid} "
                f"(PermissionError) — process may be owned by another user. "
                f"Stop it manually (kill -9 {pid}) and retry."
            )

        if not await self._poll_process_dead(pid, STOP_SIGKILL_WAIT):
            raise _BackendAliveConflict(
                f"universe {uid} backend pid {pid} survived SIGKILL — "
                f"refusing to delete. Stop the process manually "
                f"(kill -9 {pid}) and retry."
            )
        self._backend_pids.pop(uid, None)

    def _process_alive(self, pid: int) -> bool:
        """True if the tracked backend process has not yet exited.

        The supervisor forked the backend, so it is the parent and
        ``os.waitpid(WNOHANG)`` both tests for exit and reaps the resulting
        zombie in one call. ``ChildProcessError`` means it was already reaped
        (or never ours) — treat as not alive."""
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
            return done == 0
        except ChildProcessError:
            return False

    async def _poll_process_dead(self, pid: int, timeout: float) -> bool:
        """Poll the backend's liveness every 0.5s until it exits or ``timeout``
        elapses. Returns True if the process exited in time."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._process_alive(pid):
                return True
            await asyncio.sleep(0.5)
        return not self._process_alive(pid)


# ---------------------------------------------------------------------------
# admin auth dependency
# ---------------------------------------------------------------------------

def _make_admin_checker(config: GaOTTTConfig):
    """Build the /admin/* auth dependency closing over ``config``.

    Accepts the key via ``X-Admin-Key`` or ``Authorization: Bearer <key>``.
    Uses :func:`secrets.compare_digest` for constant-time comparison."""
    expected = config.supervisor_admin_key

    async def check_admin(request: Request) -> None:
        key = request.headers.get("x-admin-key")
        if not key:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                key = auth[7:].strip()
        if not key or not secrets.compare_digest(key, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized",
            )

    return check_admin


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------

def create_supervisor_app(
    config: GaOTTTConfig,
    registry: MultiverseRegistry,
    control_client: ControlClient | None = None,
) -> FastAPI:
    """Build the supervisor FastAPI app.

    Raises :class:`RuntimeError` if ``config.supervisor_admin_key`` is empty —
    admin endpoints must never be exposed unauthenticated.

    ``control_client`` is the optional MV4 control-plane client. When None
    (the default) the supervisor runs in pure MV3 local-only mode — the
    feature is fully inert (default 不変). When provided, the lifespan starts
    it after the registry is ready and stops it (with a final usage flush)
    before the registry closes; the route / create / delete handlers also
    record activity telemetry via :meth:`ControlClient.arecord_event`."""
    if not config.supervisor_admin_key:
        raise RuntimeError(
            "supervisor_admin_key must be set (non-empty) — refusing to start "
            "a supervisor with unauthenticated admin endpoints"
        )

    sup = _Supervisor(config, registry, control_client)
    root = Path(config.multiverse_root)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Harden the multiverse root before the registry populates it. The root
        # holds hashed keys + universe data dirs; 0700 keeps it off-limits to
        # other OS users. (Tests pre-create a 0700 root and ASGITransport skips
        # lifespan, so this only runs in production.)
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        # In production the registry passed here is fresh; initialize + reconcile
        # aligns it with on-disk state.
        await registry.initialize()
        await registry.reconcile()
        # MV4: start the control client AFTER the registry is ready so its
        # first reconcile sees the local state. The client's start() is a
        # no-op when disabled (3-point gate not set).
        if control_client is not None:
            await control_client.start()
        try:
            yield
        finally:
            # Stop the control client BEFORE registry.close() so its final
            # flush_usage (in stop()) can still read a valid registry while
            # building the sync payload.
            if control_client is not None:
                await control_client.stop()
            await registry.close()

    app = FastAPI(lifespan=lifespan)
    app.state.supervisor = sup
    app.state.config = config
    app.state.registry = registry
    app.state.control = control_client

    check_admin = _make_admin_checker(config)
    admin = APIRouter(dependencies=[Depends(check_admin)])

    # -- POST /admin/universes --------------------------------------------

    @admin.post("/admin/universes", status_code=status.HTTP_201_CREATED)
    async def create_universe(body: CreateUniverseBody):
        async with sup._create_lock:
            try:
                info = await asyncio.to_thread(_validate_embedder, config)
            except EmbedderValidationError as exc:
                logger.warning("embedder validation failed: %s", exc)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Embedder validation failed",
                )
            # The backend loads the remote embedder whose identity is what /info
            # reports; the manifest must record *that* so the backend's startup
            # manifest_check passes. (body.embedder_id is accepted for
            # forward-compat but the authoritative source is the service.)
            resolved_embedder_id = info.get("model_name") or config.model_name
            embedder_version = info.get("version") or "unpinned"

            universe_id = uuid4().hex[:12]
            port = await registry.allocate_port(
                config.universe_port_range_start, config.universe_port_range_end,
            )
            universe_dir = root / UNIVERSES_SUBDIR / universe_id
            universe_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(universe_dir, 0o700)

            write_manifest(
                universe_dir,
                UniverseManifest(
                    universe_id=universe_id,
                    embedder_id=resolved_embedder_id,
                    embedder_version=embedder_version,
                    embedding_dim=int(info["dimension"]),
                    created_at=time.time(),
                    managed=True,
                ),
            )
            os.chmod(universe_dir / MANIFEST_FILENAME, 0o600)

            plaintext_key = await registry.create_universe(
                universe_id, body.owner_label, port,
                resolved_embedder_id, embedder_version,
            )
            # MV4: attribute the create as control-plane usage telemetry.
            # tenant_id resolves from the body, then config default, then the
            # implicit "default" tenant (J11). The local registry has no
            # tenant column, so this is purely for control-plane accounting —
            # when no control_client is configured the call is a guarded no-op.
            if sup._control is not None:
                resolved_tenant = (
                    body.tenant_id
                    or config.control_default_tenant_id
                    or "default"
                )
                await sup._control.arecord_event(
                    universe_id, UNIVERSE_CREATE, resolved_tenant,
                )
            # MV5: regenerate the litestream backup config so the new
            # universe's SQLite is picked up by the next litestream sync.
            # Best-effort; the hook swallows its own errors so a backup
            # misconfiguration cannot fail the create response (D2).
            await sup._run_backup_hook()
            # api_key is handed out exactly once; only the hash is persisted.
            return {"universe_id": universe_id, "api_key": plaintext_key, "port": port}

    # -- DELETE /admin/universes/{uid} ------------------------------------

    @admin.delete("/admin/universes/{universe_id}")
    async def delete_universe(universe_id: str):
        universe = await registry.get_universe(universe_id)
        if universe is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="universe not found",
            )
        uid = universe_id
        universe_dir = root / UNIVERSES_SUBDIR / uid
        # B3: two-layer lock mirroring ensure_backend — the in-process
        # asyncio.Lock serializes within this supervisor, and a cross-process
        # fcntl.flock on <universe_dir>/.spawn.lock serializes across
        # supervisor processes/restarts. Together they prevent a concurrent
        # /route -> ensure_backend from spawning onto a universe whose dir is
        # being moved to trash. An orphan universe (dir already gone) has no
        # lock target, so its registry row is reconciled without the flock.
        async with sup._spawn_lock(uid):
            if not universe_dir.exists():
                await registry.delete_universe(uid)
                return {"status": "deleted"}

            lock_path = universe_dir / ".spawn.lock"
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_EX)
                try:
                    try:
                        await sup._stop_backend(universe)
                    except _BackendAliveConflict as exc:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=str(exc),
                        )
                    trash_dir = root / TRASH_SUBDIR / uid
                    trash_dir.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(universe_dir), str(trash_dir))
                    await registry.delete_universe(uid)
                    # MV4: attribute the delete as control-plane usage
                    # telemetry. Registry rows carry no tenant_id (MV3
                    # schema), so we use the config default tenant — the
                    # v1 single-tenant assumption (J11).
                    if sup._control is not None:
                        resolved_tenant = (
                            config.control_default_tenant_id or "default"
                        )
                        await sup._control.arecord_event(
                            uid,
                            UNIVERSE_DELETE,
                            resolved_tenant,
                        )
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        # MV5: regenerate the litestream backup config so the deleted
        # universe's SQLite is dropped from replication. Best-effort; the
        # hook swallows its own errors (D2). Fires after the trash move +
        # registry delete have committed, so the rescan sees the post-delete
        # state.
        await sup._run_backup_hook()
        return {"status": "deleted"}

    # -- GET /admin/universes ---------------------------------------------

    @admin.get("/admin/universes")
    async def list_universes():
        return await registry.list_universes()

    # -- GET /admin/status ------------------------------------------------
    # MV4 WP-4: operator-facing supervisor status. Exposes the control
    # client's permanent-auth-failure state so an operator polling this
    # endpoint can detect a revoked host token early (review #2 remaining
    # gap). Parity-exempt — management plane, like /reset and the rest of
    # the admin surface. When no control_client is configured, returns
    # ``{"control": None}`` so the shape stays stable across modes.
    @admin.get("/admin/status")
    async def admin_status():
        if sup._control is None:
            return {"control": None}
        return {"control": sup._control.auth_failure_state()}

    # -- POST /route (universe API key, not admin) ------------------------

    @app.post("/route")
    async def route(body: RouteBody):
        universe_id = await registry.verify_api_key(body.api_key)
        if universe_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized",
            )
        universe = await registry.get_universe(universe_id)
        if universe is None or universe.get("status") != "active":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="universe not available",
            )
        try:
            url, token = await sup.ensure_backend(universe)
        except _UniverseInactive:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="universe not available",
            )
        # MV4: record route-resolution activity telemetry AFTER the response
        # is determined but BEFORE returning (Codex non-blocking #6). Naming
        # is ``route_resolution`` (not "operation count") — proxy reconnect
        # can under-count, J1=A. The local registry has no tenant column
        # (MV3 schema), so v1 uses the config default tenant for route
        # events (J11). When no control_client is configured, no-op.
        if sup._control is not None:
            route_tenant = config.control_default_tenant_id or "default"
            await sup._control.arecord_event(
                universe_id, ROUTE_RESOLUTION, route_tenant,
            )
        return {"url": url, "token": token}

    app.include_router(admin)
    return app


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="GaOTTT multiverse supervisor")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None,
                        help="override supervisor_port from config")
    args = parser.parse_args()

    config = GaOTTTConfig.from_config_file()
    if not config.multiverse_root:
        raise SystemExit(
            "GAOTTT_MULTIVERSE_ROOT must be set to run the supervisor"
        )
    registry = MultiverseRegistry(Path(config.multiverse_root))

    # MV4 WP-4: construct the control-plane client only when the 3-point gate
    # (control_plane_url + control_host_id + control_host_token) is fully
    # satisfied. Any missing knob → control_client stays None and the
    # supervisor runs in pure MV3 local-only mode (default 不変). The
    # ControlClient itself also enforces this gate defensively.
    control_client: ControlClient | None = None
    if (
        config.control_plane_url
        and config.control_host_id
        and config.control_host_token
    ):
        control_client = ControlClient(config, registry)
    else:
        logger.info(
            "control plane not configured (3-point gate incomplete); "
            "supervisor running in local-only mode"
        )

    app = create_supervisor_app(config, registry, control_client)
    port = args.port if args.port is not None else config.supervisor_port
    uvicorn.run(app, host=args.host, port=port, log_level="info")


if __name__ == "__main__":
    _main()
