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
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel

from gaottt.config import GaOTTTConfig
from gaottt.multiverse.cloner import (
    CloneConflict,
    InsufficientCloneStorage,
    clone_universe_files,
)
from gaottt.multiverse.control_client import (
    ROUTE_RESOLUTION,
    UNIVERSE_CREATE,
    UNIVERSE_DELETE,
    ControlClient,
)
from gaottt.multiverse.registry import TRASH_SUBDIR, UNIVERSES_SUBDIR, MultiverseRegistry
from gaottt.multiverse.tuning_env import (
    TuningEnvValidationError,
    filter_tuning_env,
    validate_tuning_env,
)
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

# Phase U WP-6b — backend readiness (GET /admin/readiness)。transport ready
# (initialize handshake) と engine ready (SEMANTIC_READY) を区別する。
READINESS_LEGACY = "legacy"
# endpoint 無し (旧 backend version) — readiness 待ちせず従来挙動へ即 fallback。

# _await_backend_readiness が poll を打ち切る state (STARTING 以外)。
_READINESS_DONE_STATES = frozenset({
    "SEMANTIC_READY", "HYBRID_READY", "FAILED",
})


async def _fetch_backend_readiness(
    host: str, port: int, token: str, timeout: float = 3.0,
) -> dict | str | None:
    """GET ``http://<host>:<port>/admin/readiness`` (Bearer token 付き)。

    Returns:
        dict — 200 + JSON body (``{"state": ..., ...}``)。
        READINESS_LEGACY — 404。endpoint が存在しない旧 backend なので
            readiness 待ちをせず即時 legacy 挙動へ fallback する。
        None — transient (接続 error / 非 200 / 不正 JSON / shape 不備)。
            deadline 内の再試行対象であり、単体では失敗扱いにしない。

    既存の initialize-handshake (``_probe_backend_with_token``) とは独立した
    seam — ここが「何も返さない」でも initialize probe の成否には影響しない。
    """
    url = f"http://{host}:{port}/admin/readiness"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError:
        return None
    if resp.status_code == 404:
        return READINESS_LEGACY
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("state"), str):
        return None
    return payload


async def _await_backend_readiness(
    host: str, port: int, token: str, timeout: float,
) -> dict | str:
    """backend の readiness state が確定するまで bounded poll する。

    ``timeout`` 秒の deadline 内に SEMANTIC_READY / HYBRID_READY / FAILED
    が観測できればその payload を返す。STARTING が続く・transient error が
    続いた場合は deadline 超過として ``{"state": "STARTING"}`` を返す
    (**error にしない** — 呼び出し側は URL を返しつつ観測可能な状態として
    付与する)。404 (旧 backend) は即座に :data:`READINESS_LEGACY` を返す。
    """
    deadline = time.monotonic() + timeout
    while True:
        payload = await _fetch_backend_readiness(host, port, token)
        if payload is READINESS_LEGACY:
            return READINESS_LEGACY
        if isinstance(payload, dict) and payload.get("state") in _READINESS_DONE_STATES:
            return payload
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"state": "STARTING"}
        await asyncio.sleep(min(1.0, remaining))

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


class CloneUniverseBody(BaseModel):
    owner_label: str | None = None
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
# embedder lazy-spawn helpers (standalone — patchable in unit tests)
# ---------------------------------------------------------------------------

def _strip_gaottt_env() -> dict[str, str]:
    """Return a copy of ``os.environ`` with all ``GAOTTT_*`` keys removed.

    The supervisor's own ``GAOTTT_*`` env (data dir, backend token, owner
    lease state) must not leak into a managed subprocess. OS essentials
    (``PATH``, ``HOME``, ...) are preserved so the subprocess can run.
    Same pattern as :meth:`_Supervisor._build_spawn_env`.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GAOTTT_")}


def _spawn_embedder_detached(
    host: str, port: int, log_path: Path, model: str,
    env: dict[str, str] | None = None,
) -> int:
    """Launch a detached ``gaottt.embedding.service`` subprocess. Returns PID.

    The child runs ``python -m gaottt.embedding.service --host H --port P
    --model M`` fully detached: ``stdin`` is ``DEVNULL``, ``stdout`` /
    ``stderr`` go to ``log_path``, and it gets a new session
    (``start_new_session`` on POSIX, ``creationflags`` on Windows) so it
    survives the supervisor's death. The parent closes the log file handle
    after ``Popen`` so it does not leak in the supervisor process — the
    child already has its own dup. Mirrors
    :func:`gaottt.server.mcp_proxy._spawn_backend_detached`.

    ``env`` (B-F2): when provided, passed to ``Popen`` as ``env=`` so the
    caller controls what ``GAOTTT_*`` state reaches the child (the
    supervisor strips its own via :meth:`_Supervisor._build_embedder_spawn_env`).
    ``None`` (default) inherits the parent process environment unchanged —
    kept out of ``kwargs`` so callers/tests that do not care about ``env``
    observe an untouched ``Popen`` call shape.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", buffering=1)  # noqa: SIM115 — child owns the fd
    log_file.write(
        f"\n--- embedder spawn at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
    )
    cmd = [
        sys.executable, "-m", "gaottt.embedding.service",
        "--host", host, "--port", str(port), "--model", model,
    ]
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
    }
    if env is not None:
        kwargs["env"] = env
    if sys.platform == "win32":
        kwargs["creationflags"] = (  # type: ignore[attr-defined]
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603 — controlled argv
    log_file.close()
    return proc.pid


def _is_my_child_alive(pid: int) -> bool:
    """True if ``pid`` is a child of this process and has not yet exited.

    Uses ``os.waitpid(pid, WNOHANG)`` which both tests for exit and reaps
    the resulting zombie in one call. ``ChildProcessError`` means the
    child was already reaped (or was never ours); ``OSError`` covers the
    race where the PID was recycled by the kernel. Either way the PID is
    not a live child of ours, so return ``False`` — the caller falls
    through to the "race lost" path. Distinct from
    :meth:`_Supervisor._process_alive` (an instance method for backend
    PID tracking) only in its error handling: this standalone helper is
    also used by readiness polling where a recycled PID is common.
    """
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except (ChildProcessError, OSError):
        return False


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
        # WP-2b: embedder lazy-spawn lifecycle state. The supervisor tracks
        # whether it owns the embedder process (and its PID), caches the
        # last-known /info result, and serializes spawn attempts with a
        # dedicated lock (distinct from the per-universe backend spawn
        # locks — the embedder is shared across all universes).
        # State machine: "unowned" (external/systemd embedder or none yet)
        # → "owned_idle" (this supervisor spawned it, it is up) →
        # "owned_terminating" (this supervisor is shutting it down). See
        # docs/plans/embedder-auto-spawn-supervisor.md §3.1 for the full
        # transition table.
        self._embedder_state = "unowned"
        self._embedder_pid: int | None = None
        self._embedder_spawn_lock = asyncio.Lock()
        self._embedder_info_cache: dict | None = None
        # WP-3b starts the idle-watchdog task after a successful owned
        # spawn. Initialized to None here so the field always exists.
        self._embedder_watchdog_task: asyncio.Task | None = None
        self._last_backend_active_at = time.monotonic()

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
        # Phase U WP-2: runtime-tuning knob のみ閉じた完全名 allowlist 経由で
        # 通す (R1 の env rollback 経路)。GAOTTT_CONFIG や identity 系は
        # allowlist 外なのでここを通れない。identity overlay の前に merge する
        # ため、仮に衝突しても identity が常に勝つ。
        env.update(filter_tuning_env(os.environ))
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
        # Phase U WP-2: fail-fast — 不正な runtime-tuning env では backend を
        # 起動しない。backend 側 ``_coerce_env`` は bool("banana")→False の
        # ように黙って coerce / drop してしまうため、spawn 時点 (唯一の
        # _build_spawn_env 呼び出し箇所) で検証して拒否する。例外は
        # /route handler が 500 + error 明細で観測可能にする。
        tuning_errors = validate_tuning_env(os.environ)
        if tuning_errors:
            logger.error(
                "refusing to spawn backend for universe %s: invalid "
                "runtime-tuning env: %s", uid, "; ".join(tuning_errors),
            )
            raise TuningEnvValidationError(tuning_errors)
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

    # -- embedder lifecycle (WP-2b) ----------------------------------------

    def _build_embedder_spawn_env(self) -> dict[str, str]:
        """Build the env dict for the embedder subprocess.

        Strips all ``GAOTTT_*`` from the supervisor's own environment
        (same pattern as :meth:`_build_spawn_env`) so the supervisor's
        data-dir / backend-token / owner-lease state does not leak into
        the embedder. The embedder service reads its config from CLI
        args, not env vars, so nothing ``GAOTTT_*`` is added back.
        """
        return _strip_gaottt_env()

    async def _probe_embedder_health(self) -> bool:
        """GET ``<embedder_endpoint>/healthz`` via ``httpx.AsyncClient``.

        Returns ``True`` on HTTP 200, ``False`` on any non-200 status,
        :class:`httpx.HTTPError` (connection refused / timeout), or when
        ``embedder_endpoint`` is unset. This is a **different seam** from
        :func:`_validate_embedder` (which uses sync ``httpx.Client.get``
        for ``/info``) — the two never interfere, so the existing test
        mock seam (``patch("httpx.Client.get", ...)``) is unaffected.
        """
        endpoint = self._config.embedder_endpoint
        if not endpoint:
            return False
        url = endpoint.rstrip("/") + "/healthz"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def _reset_embedder_state(self) -> None:
        """Reset embedder ownership to ``unowned`` and clear the PID + cache.

        Extended in WP-3b to also cancel a running idle-watchdog task so a
        state reset (stale-cache invalidation in :meth:`ensure_embedder_up`,
        clean termination in :meth:`_terminate_embedder`, or lifespan
        shutdown) does not leave an orphaned watchdog loop.

        Self-cancellation guard: when this method is called from *within*
        the watchdog task itself (via ``_terminate_embedder`` →
        ``_reset_embedder_state``), the watchdog's own task is NOT cancelled
        — cancelling the current task would raise ``CancelledError`` at the
        next await point for no benefit, since the watchdog returns on its
        own right after ``_terminate_embedder`` completes. The guard uses
        :func:`asyncio.current_task`; in a sync call context (no running
        loop) there is no self-cancellation risk, so the cancel proceeds.
        """
        self._embedder_state = "unowned"
        self._embedder_pid = None
        self._embedder_info_cache = None
        if self._embedder_watchdog_task is not None:
            current = None
            try:
                current = asyncio.current_task()
            except RuntimeError:
                pass  # no running event loop — sync call context
            if current is not self._embedder_watchdog_task:
                self._embedder_watchdog_task.cancel()
            self._embedder_watchdog_task = None

    async def ensure_embedder_up(self, *, validate_info: bool = True) -> dict:
        """Ensure the embedding service is reachable, spawning it if needed.

        Resolution order (B2-1 + B2-3):

        0. **Fail-fast on ``owned_terminating``** (B-F1): if this supervisor
           previously gave up terminating the embedder (PermissionError /
           SIGKILL survivor, see :meth:`_terminate_embedder`), refuse to
           trust any cache or attempt any spawn. Manual recovery required.
        1. **Endpoint-empty fail-fast** (B-F4 completion): inside the
           spawn lock, raise immediately if ``embedder_endpoint`` is empty,
           regardless of cache state. The config default ``""`` keeps the
           feature inert (in-process RuriEmbedder); this hoist closes the
           stale-cache gap where a non-empty ``_embedder_info_cache``
           skipped ``_validate_embedder`` and fell through to spawn.
        2. **Cache + ``/info`` validation** (B2-1 fence): if the cache is
           empty, try :func:`_validate_embedder` (sync ``/info``). Success
           → cache + return immediately. This lets the existing test mock
           seam (``httpx.Client.get`` for ``/info``) drive
           ``create_universe`` without touching ``/healthz``.
        3. **Owned cache freshness** (B2-3): if this supervisor spawned the
           embedder (``owned_idle``), verify the child PID is still alive
           **and** ``/healthz`` answers before trusting the cache.
        4. **Lazy spawn** (§3.3-§3.5): if no embedder is reachable and
           ``supervisor_spawn_embedder`` is ``True``, acquire the spawn
           lock + flock, re-check ``/healthz``, spawn, and poll readiness.

        Raises :class:`EmbedderValidationError` when the embedder is
        unreachable and either spawn is disabled or spawn fails.
        """
        # B-F1: owned_terminating ⇒ fail-fast (manual recovery). Evaluated
        # before the lock so a wedged supervisor does not even attempt the
        # cache / /healthz / spawn paths — silent recovery would hide the
        # ownership hazard that B2-6 retained the state to surface.
        if self._embedder_state == "owned_terminating":
            raise EmbedderValidationError(
                "embedder is in owned_terminating state — manual recovery "
                "required (check supervisor logs for PermissionError / "
                "SIGKILL survival)"
            )
        async with self._embedder_spawn_lock:
            # B-F4 completion: endpoint unset ⇒ raise before any cache,
            # /healthz, or spawn path. The original B-F4 fence lived inside
            # the ``except EmbedderValidationError`` block below, reachable
            # only when ``_embedder_info_cache is None``; a stale cache
            # skipped ``_validate_embedder`` entirely and fell through to
            # spawn. Hoisting the check here makes "no endpoint ⇒ no spawn"
            # literal regardless of cache state (config default stays inert).
            if not self._config.embedder_endpoint:
                raise EmbedderValidationError(
                    "embedder_endpoint is not configured — lazy spawn requires "
                    "an explicit endpoint (config.embedder_endpoint or "
                    "GAOTTT_EMBEDDER_ENDPOINT env). Default empty string keeps "
                    "the feature inert (in-process RuriEmbedder)."
                )
            # B2-1: try /info first. Existing test mock seam (httpx.Client.get)
            # makes this the fast path — /healthz is never reached when /info
            # answers, so create_universe's existing _embedder_ok() helper
            # works unchanged.
            if self._embedder_info_cache is None:
                try:
                    info = await asyncio.to_thread(_validate_embedder, self._config)
                except EmbedderValidationError:
                    # B-F4: endpoint unset ⇒ no spawn target. ``_validate_embedder``
                    # already raised on the empty endpoint; re-raise here so the
                    # default ``embedder_endpoint=""`` (feature off) does not
                    # fall through to a spawn derived from an empty URL.
                    if not self._config.embedder_endpoint:
                        raise
                    if not self._config.supervisor_spawn_embedder:
                        raise  # opt-out mode: fail fast, no spawn attempt
                    # /info unreachable → fall through to /healthz + spawn path
                else:
                    if self._embedder_state == "unowned":
                        self._embedder_info_cache = info
                    # owned_idle falls through to the freshness check below
                    if self._embedder_state != "owned_idle":
                        return info

            # B2-3: owned cache freshness — verify child alive + /healthz
            if self._embedder_state == "owned_idle" and self._embedder_info_cache:
                if (self._embedder_pid is not None
                        and _is_my_child_alive(self._embedder_pid)
                        and await self._probe_embedder_health()):
                    return self._embedder_info_cache
                # child died or /healthz NG → invalidate, spawn path
                self._reset_embedder_state()

            # B2-3 continued: unowned cache freshness — external embedder
            # may have died between calls. /healthz probe catches this.
            if self._embedder_state == "unowned" and self._embedder_info_cache:
                if await self._probe_embedder_health():
                    return self._embedder_info_cache
                self._embedder_info_cache = None  # external embedder went down

            # (3) lazy spawn path
            if await self._probe_embedder_health():
                # Someone else (systemd / another supervisor) has an embedder up
                self._embedder_state = "unowned"
                self._embedder_pid = None
            elif self._config.supervisor_spawn_embedder:
                await self._spawn_embedder_owned()
                if self._embedder_state != "owned_idle":
                    # Race lost: another supervisor won the flock and spawned.
                    # Verify their embedder is reachable before trusting it.
                    if await self._probe_embedder_health():
                        self._embedder_state = "unowned"
                    else:
                        raise EmbedderValidationError("embedder spawn race lost")
            else:
                raise EmbedderValidationError(
                    "embedder service unreachable (supervisor_spawn_embedder=False)"
                )

            # (4) post-spawn /info validation (cache may still be None if
            # we arrived via the /healthz-only path without a prior /info).
            if validate_info and self._embedder_info_cache is None:
                info = await asyncio.to_thread(_validate_embedder, self._config)
                self._embedder_info_cache = info
            return self._embedder_info_cache  # type: ignore[return-value]

    async def _spawn_embedder_owned(self) -> None:
        """Spawn the embedder service as a tracked child of this supervisor.

        Two-layer locking mirrors :meth:`ensure_backend`: the
        ``_embedder_spawn_lock`` (already held by the caller) serializes
        in-process, and ``fcntl.flock`` on ``<root>/.embedder.spawn.lock``
        serializes across supervisor processes/restarts. The flock is
        taken via :func:`asyncio.to_thread` (B2-2) so the event loop is
        not stalled.
        """
        endpoint = self._config.embedder_endpoint
        parsed = urlparse(endpoint)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 7879
        model = self._config.model_name
        log_path = self._root / "logs" / "embedder.log"

        lock_path = self._root / ".embedder.spawn.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            # B2-2: flock via to_thread so the event loop is not blocked.
            await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_EX)
            try:
                # Re-check inside the flock: another supervisor may have
                # spawned while we waited.
                if await self._probe_embedder_health():
                    self._embedder_state = "unowned"
                    return
                pid = _spawn_embedder_detached(
                    host, port, log_path, model,
                    env=self._build_embedder_spawn_env(),  # B-F2
                )
                ready = await self._poll_embedder_readiness(pid)
                if ready:
                    # Race-lost check: child may have been replaced or died
                    # between the last /healthz OK and now.
                    if (not _is_my_child_alive(pid)
                            or not await self._probe_embedder_health()):
                        self._embedder_state = "unowned"
                        self._embedder_pid = None
                        return
                    self._embedder_pid = pid
                    self._embedder_state = "owned_idle"
                    self._last_backend_active_at = time.monotonic()
                    # Start the idle-watchdog. Cancel any prior task first
                    # so a re-spawn (after the prior embedder died and the
                    # cache was invalidated in ensure_embedder_up) does not
                    # leave two watchdog loops running.
                    if self._embedder_watchdog_task is not None:
                        self._embedder_watchdog_task.cancel()
                        try:
                            await self._embedder_watchdog_task
                        except asyncio.CancelledError:
                            pass
                    self._embedder_watchdog_task = asyncio.create_task(
                        self._embedder_idle_watchdog()
                    )
                else:
                    await self._handle_spawn_readiness_failure(pid)
            finally:
                await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    async def _poll_embedder_readiness(self, pid: int) -> bool:
        """Poll ``/healthz`` until the spawned embedder is ready or timeout.

        Returns ``True`` if ``/healthz`` answers 200 within
        ``embedder_spawn_readiness_timeout_seconds``. Returns ``False`` on
        timeout or child-process death. The caller
        (:meth:`_spawn_embedder_owned`) uses the ``False`` return to
        invoke :meth:`_handle_spawn_readiness_failure` for race-lost
        classification (B2-5).
        """
        deadline = (
            time.monotonic()
            + self._config.embedder_spawn_readiness_timeout_seconds
        )
        while time.monotonic() < deadline:
            if not _is_my_child_alive(pid):
                return False  # child died before becoming ready
            if await self._probe_embedder_health():
                return True
            await asyncio.sleep(1.0)
        return False  # timeout

    async def _handle_spawn_readiness_failure(self, pid: int) -> None:
        """Classify a readiness failure into one of four race-lost patterns.

        (B2-5): ``_poll_embedder_readiness`` returning ``False`` means
        either the child died or the readiness timeout elapsed. This
        method probes the **external** embedder (``/healthz``) to
        distinguish:

        1. **Child dead + external healthy** → race lost; another
           supervisor's embedder won. Set ``unowned``.
        2. **Child dead + external unhealthy** → spawn genuinely failed;
           raise :class:`EmbedderValidationError`.
        3. **Child alive + external healthy** → race lost; our child is
           likely stuck on ``Address already in use``. Kill + reap it,
           set ``unowned``.
        4. **Child alive + external unhealthy** → spawn genuinely failed;
           kill + reap the child, raise :class:`EmbedderValidationError`.
        """
        external_ok = await self._probe_embedder_health()
        child_alive = _is_my_child_alive(pid)

        if external_ok:
            # Race lost — another supervisor's embedder is serving.
            self._embedder_state = "unowned"
            self._embedder_pid = None
            if child_alive:
                # Our child is redundant (probably stuck on bind failure);
                # clean it up so it does not linger as a zombie.
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
            return

        # External embedder is also unhealthy → genuine spawn failure.
        if child_alive:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
        raise EmbedderValidationError(
            f"spawned embedder pid={pid} did not become ready within "
            f"{self._config.embedder_spawn_readiness_timeout_seconds}s"
        )

    # -- embedder lifecycle (WP-3b) ----------------------------------------

    async def _wait_for_pid_exit(self, pid: int, timeout: float) -> bool:
        """Poll ``os.waitpid(pid, WNOHANG)`` until the process exits or
        ``timeout`` elapses.

        Returns ``True`` as soon as ``waitpid`` reports the process has
        exited (non-zero first element) or raises ``ChildProcessError``
        (already reaped / never ours). Returns ``False`` on timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return True
            if done != 0:
                return True
            await asyncio.sleep(0.5)
        return False

    async def _terminate_embedder(self) -> None:
        """Terminate the owned embedder process (SIGTERM → SIGKILL).

        B2-6: when ``SIGTERM`` or ``SIGKILL`` raises ``PermissionError``, or
        the process survives ``SIGKILL``, the state stays
        ``owned_terminating`` — it is **not** reset to ``unowned``. This
        mirrors :meth:`_kill_tracked_backend`'s ``_BackendAliveConflict``
        pattern: silently clearing state would hide an ownership hazard
        (the embedder is still alive but this supervisor gave up on it).
        Unlike ``_kill_tracked_backend``, no conflict exception is raised —
        embedder termination is best-effort, and the retained
        ``owned_terminating`` state makes the next :meth:`ensure_embedder_up`
        fail fast (``EmbedderValidationError``) to force manual recovery.
        """
        if self._embedder_state != "owned_idle":
            return
        self._embedder_state = "owned_terminating"
        pid = self._embedder_pid
        if pid is None:
            self._reset_embedder_state()
            return

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._reset_embedder_state()
            return
        except PermissionError:
            logger.error(
                "embedder pid=%d PermissionError on SIGTERM (owned by "
                "another user?) — state stays owned_terminating, manual "
                "recovery required", pid,
            )
            return

        if await self._wait_for_pid_exit(pid, STOP_SIGTERM_WAIT):
            self._reset_embedder_state()
            return

        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            self._reset_embedder_state()
            return
        except PermissionError:
            logger.error(
                "embedder pid=%d PermissionError on SIGKILL — state stays "
                "owned_terminating, manual recovery required", pid,
            )
            return

        if not await self._wait_for_pid_exit(pid, STOP_SIGKILL_WAIT):
            logger.error(
                "embedder pid=%d survived SIGKILL — state stays "
                "owned_terminating, manual recovery required "
                "(kill -9 %d)", pid, pid,
            )
            return
        self._reset_embedder_state()

    async def _embedder_idle_watchdog(self) -> None:
        """Background task that reaps the embedder when all backends are
        idle past ``embedder_spawn_idle_timeout_seconds``.

        Loop (only while state is ``owned_idle``):

        1. Reap dead tracked backend PIDs (housekeeping — zombie cleanup).
        2. If the idle timeout has NOT elapsed, sleep and continue.
        3. If tracked backends are still live, sleep and continue.
        4. If untracked active universes still respond, sleep and continue.
        5. All idle + timeout elapsed → call :meth:`_terminate_embedder`
           and exit the loop.

        ``asyncio.CancelledError`` is caught and swallowed — the watchdog
        is cancelled on lifespan shutdown, state reset, and re-spawn.
        """
        try:
            while True:
                if self._embedder_state != "owned_idle":
                    return
                self._reap_dead_backend_pids()
                idle_elapsed = (
                    time.monotonic() - self._last_backend_active_at
                    >= self._config.embedder_spawn_idle_timeout_seconds
                )
                if not idle_elapsed:
                    await asyncio.sleep(
                        self._config.embedder_idle_watchdog_poll_seconds,
                    )
                    continue
                if self._has_tracked_live_backends():
                    await asyncio.sleep(
                        self._config.embedder_idle_watchdog_poll_seconds,
                    )
                    continue
                if await self._has_untracked_live_backends():
                    await asyncio.sleep(
                        self._config.embedder_idle_watchdog_poll_seconds,
                    )
                    continue
                await self._terminate_embedder()
                return
        except asyncio.CancelledError:
            return

    def _reap_dead_backend_pids(self) -> None:
        """Remove ``_backend_pids`` entries whose process has exited.

        Uses ``os.waitpid(pid, WNOHANG)`` to both test for exit and reap
        the resulting zombie in one call. ``ChildProcessError`` (already
        reaped / never ours) also qualifies as dead.
        """
        dead: list[str] = []
        for uid, pid in self._backend_pids.items():
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                dead.append(uid)
                continue
            if done != 0:
                dead.append(uid)
        for uid in dead:
            self._backend_pids.pop(uid, None)

    def _has_tracked_live_backends(self) -> bool:
        """True if any PID in ``_backend_pids`` is still alive.

        Uses ``os.waitpid(pid, WNOHANG)``: ``done == 0`` means the process
        is still running. ``ChildProcessError`` means it is gone (already
        reaped or never ours). An empty ``_backend_pids`` dict returns
        ``False`` — nothing tracked, nothing live.
        """
        for pid in self._backend_pids.values():
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                continue
            if done == 0:
                return True
        return False

    async def _has_untracked_live_backends(self) -> bool:
        """True if any active universe NOT in ``_backend_pids`` has a live
        backend serving on its port.

        Non-active universes (``deleted`` / ``orphan``) are skipped — they
        are never probed. A universe whose PID this supervisor tracks is
        also skipped (its liveness is covered by
        :meth:`_has_tracked_live_backends`). The remaining untracked active
        universes are probed via :func:`_probe_backend_with_token`:
        ``PROBE_OK`` counts as live (the MCP handshake succeeded),
        ``PROBE_DOWN`` / ``PROBE_UNAUTHORIZED`` do not (a stranger process
        holding the port without speaking MCP, or a stale-token live
        backend, is not counted — Codex v2 stranger-discrimination).
        """
        universes = await self._registry.list_universes()
        for u in universes:
            if u.get("status") != "active":
                continue
            uid = u["universe_id"]
            if uid in self._backend_pids:
                continue  # tracked — covered by _has_tracked_live_backends
            port = u["port"]
            token = self._load_token(uid) or ""
            probe = await _probe_backend_with_token(HOST, port, token)
            if probe == PROBE_OK:
                return True
        return False


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
            # WP-3b: stop the embedder watchdog and terminate the owned
            # embedder BEFORE closing the registry — the watchdog's
            # _has_untracked_live_backends calls registry.list_universes,
            # so it must be stopped first to avoid racing with close().
            # Embedder termination is also stopped before the control
            # client flushes its final usage (which may reference the
            # embedder indirectly via active backends).
            if sup._embedder_watchdog_task is not None:
                sup._embedder_watchdog_task.cancel()
                try:
                    await sup._embedder_watchdog_task
                except asyncio.CancelledError:
                    pass
                sup._embedder_watchdog_task = None
            if sup._embedder_state == "owned_idle":
                await sup._terminate_embedder()
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
                info = await sup.ensure_embedder_up()
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

    # -- POST /admin/universes/{uid}/clone -------------------------------

    @admin.post(
        "/admin/universes/{source_universe_id}/clone",
        status_code=status.HTTP_201_CREATED,
    )
    async def clone_universe(
        source_universe_id: str, body: CloneUniverseBody,
    ):
        source = await registry.get_universe(source_universe_id)
        if source is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="source universe not found",
            )

        source_dir = root / UNIVERSES_SUBDIR / source_universe_id
        async with sup._create_lock:
            async with sup._spawn_lock(source_universe_id):
                if not source_dir.exists():
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="source universe directory is missing",
                    )

                lock_path = source_dir / ".spawn.lock"
                lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    await asyncio.to_thread(
                        fcntl.flock, lock_fd, fcntl.LOCK_EX,
                    )
                    try:
                        # Re-read inside the source lock. A concurrent delete
                        # may have changed status after the initial 404 check.
                        source = await registry.get_universe(source_universe_id)
                        if source is None or source.get("status") != "active":
                            raise HTTPException(
                                status_code=status.HTTP_409_CONFLICT,
                                detail="source universe is not active",
                            )
                        try:
                            await sup._stop_backend(source)
                        except _BackendAliveConflict as exc:
                            raise HTTPException(
                                status_code=status.HTTP_409_CONFLICT,
                                detail=str(exc),
                            )
                        # The process is confirmed dead and the source spawn
                        # lock is still held. Remove lease bookkeeping left by
                        # a signal-driven shutdown so the source can restart
                        # immediately after cloning instead of waiting for the
                        # normal stale-heartbeat takeover window.
                        for lease_name in ("owner.lock", "owner.lock.guard"):
                            try:
                                (source_dir / lease_name).unlink()
                            except FileNotFoundError:
                                pass

                        target_id = uuid4().hex[:12]
                        target_dir = root / UNIVERSES_SUBDIR / target_id
                        port = await registry.allocate_port(
                            config.universe_port_range_start,
                            config.universe_port_range_end,
                        )
                        try:
                            snapshot = await asyncio.to_thread(
                                clone_universe_files,
                                source_dir,
                                target_dir,
                                target_id,
                            )
                        except CloneConflict as exc:
                            raise HTTPException(
                                status_code=status.HTTP_409_CONFLICT,
                                detail=str(exc),
                            )
                        except InsufficientCloneStorage as exc:
                            raise HTTPException(
                                status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                                detail=str(exc),
                            )

                        owner_label = (
                            body.owner_label
                            or f"{source['owner_label']}-clone"
                        )
                        try:
                            plaintext_key = await registry.create_universe(
                                target_id,
                                owner_label,
                                port,
                                snapshot.manifest.embedder_id,
                                snapshot.manifest.embedder_version,
                            )
                        except Exception:
                            shutil.rmtree(target_dir, ignore_errors=True)
                            raise

                        if sup._control is not None:
                            resolved_tenant = (
                                body.tenant_id
                                or config.control_default_tenant_id
                                or "default"
                            )
                            await sup._control.arecord_event(
                                target_id, UNIVERSE_CREATE, resolved_tenant,
                            )
                    finally:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)

        await sup._run_backup_hook()
        return {
            "source_universe_id": source_universe_id,
            "universe_id": target_id,
            "api_key": plaintext_key,
            "port": port,
        }

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
            await sup.ensure_embedder_up()
        except EmbedderValidationError as exc:
            logger.warning("embedder validation failed on /route: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Embedder validation failed",
            )
        try:
            url, token = await sup.ensure_backend(universe)
        except _UniverseInactive:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="universe not available",
            )
        except TuningEnvValidationError as exc:
            # Phase U WP-2: spawn 時の tuning env 検証失敗を観測可能にする
            # (str(exc) は全 validation error の "; " 連結)。
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Invalid runtime tuning env: {exc}",
            )
        # Phase U WP-6b: transport ready (initialize handshake) と engine
        # ready (SEMANTIC_READY) を区別する。STARTING 中は deadline
        # (route_readiness_timeout_seconds) まで poll し、超過しても error
        # にせず readiness:"starting" 付きで応答する (観測可能な状態 —
        # proxy/client は接続して backend 側の bounded wait に委ねられる)。
        # endpoint 無し (旧 backend, 404) は即時 legacy 挙動へ fallback。
        readiness = await _await_backend_readiness(
            HOST, universe["port"], token,
            config.route_readiness_timeout_seconds,
        )
        if readiness is READINESS_LEGACY:
            pass  # 旧 backend — 従来どおり即応答
        elif readiness.get("state") == "FAILED":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Backend startup failed: "
                    f"{readiness.get('error', 'unknown error')}"
                ),
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
        response = {"url": url, "token": token}
        if (
            readiness is not READINESS_LEGACY
            and readiness.get("state") == "STARTING"
        ):
            response["readiness"] = "starting"
        return response

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
