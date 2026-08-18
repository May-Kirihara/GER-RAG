"""GaOTTT MCP stdio→HTTP proxy with auto-spawn backend.

Architecture (cold-war dead-man-switch pattern):

  agent (Claude Code / opencode / ...)
    ↓ stdio
  this proxy  ← lightweight, one per agent
    ↓ HTTP (streamable-http MCP)
  gaottt backend  ← heavy, ONE process shared by all proxies

The proxy:
  1. On startup, probes ``http://<host>:<port>/mcp`` for a live backend.
  2. If absent, spawns ``mcp_server --transport streamable-http`` as a
     detached subprocess (survives the proxy's death — other proxies can
     keep using it) and polls until ready.
  3. Opens an MCP ClientSession to the backend and runs a stdio Server
     that forwards every request to the upstream session. No tool
     definitions are duplicated — tools / prompts / resources are
     discovered dynamically from the upstream so the proxy works for
     any tool list without code changes.
  4. Sends an MCP ``ping`` to the backend every ``ping_interval`` seconds
     (default 60) so the backend's idle watchdog (default 300s) doesn't
     consider the system idle as long as at least one proxy is alive.

When the agent disconnects, this proxy exits naturally. The backend
stays alive until ALL proxies stop pinging for ``idle_timeout`` seconds
(cold-war fail-safe: no key turn → stand down).

Race on simultaneous proxy startup is handled by the spawn step: the
``streamable-http`` backend binds to the port atomically; the second
proxy's spawn fails with ``Address already in use`` and falls through
to the "connect to existing" path.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import anyio
from mcp import ClientSession, types
from mcp.client.streamable_http import streamablehttp_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError

logger = logging.getLogger(__name__)

DEFAULT_PORT = 7878
DEFAULT_HOST = "127.0.0.1"
DEFAULT_IDLE_TIMEOUT = 300.0   # backend self-shutdown after 5 minutes of silence
DEFAULT_PING_INTERVAL = 60.0   # proxy heartbeat cadence
# A cold /route can spend up to 90s starting the embedder and another 90s
# loading a universe backend, so the shim must cover both stages.
DEFAULT_SUPERVISOR_READINESS_TIMEOUT = 200.0


# -----------------------------------------------------------------------
# Backend lifecycle
# -----------------------------------------------------------------------

def _port_in_use(host: str, port: int) -> bool:
    """Cheap TCP probe — does someone hold ``host:port``?"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect((host, port))
            return True
    except OSError:
        return False


async def _probe_backend(url: str, timeout: float = 5.0) -> bool:
    """Open an MCP session against ``url`` and try ``initialize``.

    Returns True iff the backend answers a valid initialize response.
    Used both for the initial existence check and for spawn-readiness
    polling.
    """
    try:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=timeout)
                return True
    except Exception:  # noqa: BLE001 — any failure means "not ready"
        return False


def _gaottt_env_names() -> list[str]:
    """Return the names (not values) of GAOTTT_* env vars in this process."""
    return sorted(k for k in os.environ if k.startswith("GAOTTT_"))


def _spawn_backend_detached(
    host: str,
    port: int,
    idle_timeout: float,
    log_path: Path,
) -> int:
    """Launch a detached ``mcp_server --transport streamable-http`` subprocess.

    Returns the PID. The subprocess is fully detached: its stdin is
    DEVNULL, stdout / stderr go to ``log_path``, and it gets a new
    session so it survives the proxy's death.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", buffering=1)
    log_file.write(
        f"\n--- backend spawn at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
    )
    cmd = [
        sys.executable,
        "-m",
        "gaottt.server.mcp_server",
        "--transport",
        "streamable-http",
        "--host",
        host,
        "--port",
        str(port),
        "--idle-timeout",
        str(idle_timeout),
    ]
    # Linux/macOS: start_new_session detaches. Windows: DETACHED_PROCESS.
    # Pass an explicit env snapshot so GAOTTT_* overrides (e.g.
    # GAOTTT_LEASE_FORCE_TAKEOVER from --force-takeover) are observably
    # forwarded to the detached backend; the default inheritance does the
    # same in production, but an explicit copy is verifiable + snapshot-safe.
    popen_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "env": os.environ.copy(),
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        )
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **popen_kwargs)  # noqa: S603 — controlled args
    return proc.pid


async def _ensure_backend(
    host: str,
    port: int,
    idle_timeout: float,
    spawn_log_path: Path,
    readiness_timeout: float = 90.0,
) -> str:
    """Return a usable backend URL, spawning if necessary.

    Resolution order:
      1. If ``host:port`` already has a live MCP backend → return URL.
      2. If port is in use but doesn't answer MCP → assume a stranger
         is on the port, error out (we won't overwrite).
      3. Else spawn detached + poll until ready (max ``readiness_timeout``
         seconds, giving RURI load + virtual FAISS rebuild headroom).
    """
    url = f"http://{host}:{port}/mcp"
    if await _probe_backend(url, timeout=3.0):
        logger.info("GaOTTT backend already up at %s — connecting", url)
        local_env = _gaottt_env_names()
        if local_env:
            logger.warning(
                "Local GAOTTT_* env vars are NOT applied to the existing "
                "backend (env was inherited at spawn time): %s",
                ", ".join(local_env),
            )
        return url

    if _port_in_use(host, port):
        raise RuntimeError(
            f"Port {host}:{port} is taken but not by a GaOTTT MCP backend. "
            "Check what's listening (`lsof -i :{port}`) or pick a different "
            "port via --port."
        )

    logger.info(
        "GaOTTT backend not running; spawning detached subprocess "
        "(idle_timeout=%ds, log=%s)",
        int(idle_timeout), spawn_log_path,
    )
    env_names = _gaottt_env_names()
    if env_names:
        logger.info(
            "Spawning backend with GAOTTT_* env inherited: %s",
            ", ".join(env_names),
        )
    pid = _spawn_backend_detached(host, port, idle_timeout, spawn_log_path)
    logger.info("Spawned backend pid=%d, waiting for readiness ...", pid)

    deadline = time.monotonic() + readiness_timeout
    poll_interval = 1.0
    while time.monotonic() < deadline:
        if await _probe_backend(url, timeout=3.0):
            elapsed = readiness_timeout - (deadline - time.monotonic())
            logger.info("Backend ready after %.1fs at %s", elapsed, url)
            return url
        await asyncio.sleep(poll_interval)
    raise RuntimeError(
        f"Backend pid={pid} did not become ready within {readiness_timeout}s. "
        f"Check {spawn_log_path} for startup errors."
    )


# -----------------------------------------------------------------------
# Supervisor routing (multiverse MV3 / WP-4)
# -----------------------------------------------------------------------

class _SupervisorUnreachable(RuntimeError):
    """The supervisor could not be reached at the transport layer."""


def _route_to_supervisor(
    supervisor_url: str, api_key: str, timeout: float = 10.0,
) -> tuple[str, str]:
    """Resolve a backend ``(url, token)`` via the supervisor's ``POST /route``.

    Returns the upstream MCP URL and the per-universe backend token that must
    be presented as ``Authorization: Bearer`` on every connection. 401 maps to
    an "Invalid API key" error (the only auth-failure worth distinguishing for
    the caller); any other status or transport failure is a generic route
    failure so the proxy can surface it without leaking supervisor internals.
    """
    try:
        response = httpx.post(
            f"{supervisor_url.rstrip('/')}/route",
            json={"api_key": api_key},
            timeout=timeout,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise _SupervisorUnreachable(
            f"Supervisor route failed (unreachable): {exc}"
        ) from exc
    except httpx.HTTPError as exc:
        # A read/protocol failure means something accepted the connection;
        # spawning a second supervisor would only create a port race.
        raise RuntimeError(f"Supervisor route failed: {exc}") from exc
    if response.status_code == 401:
        raise RuntimeError("Invalid API key")
    if response.status_code != 200:
        raise RuntimeError(f"Supervisor route failed: {response.status_code}")
    data = response.json()
    return data["url"], data["token"]


def _supervisor_log_path(multiverse_root: str) -> Path:
    if multiverse_root:
        return Path(multiverse_root) / "logs" / "supervisor.log"
    return _spawn_log_path().with_name("supervisor.log")


def _spawn_supervisor_detached(log_path: Path) -> int:
    """Start a detached local multiverse supervisor with inherited config."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", buffering=1)
    log_file.write(
        f"\n--- supervisor spawn at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
    )
    cmd = [sys.executable, "-m", "gaottt.multiverse.supervisor"]
    popen_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "env": os.environ.copy(),
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        )
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **popen_kwargs)  # noqa: S603 — controlled args
    return proc.pid


async def _route_with_supervisor_autospawn(
    supervisor_url: str,
    api_key: str,
    *,
    enabled: bool,
    readiness_timeout: float = DEFAULT_SUPERVISOR_READINESS_TIMEOUT,
) -> tuple[str, str]:
    """Route normally; optionally spawn a missing *local* supervisor."""
    try:
        return _route_to_supervisor(supervisor_url, api_key)
    except _SupervisorUnreachable:
        if not enabled:
            raise

    parsed = urlsplit(supervisor_url)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1", "localhost", "::1",
    }:
        raise RuntimeError(
            "Supervisor auto-spawn is allowed only for a local http URL"
        )

    from gaottt.config import GaOTTTConfig

    config = GaOTTTConfig.from_config_file()
    if not config.multiverse_root:
        raise RuntimeError(
            "Supervisor auto-spawn requires GAOTTT_MULTIVERSE_ROOT"
        )
    if not config.supervisor_admin_key:
        raise RuntimeError(
            "Supervisor auto-spawn requires GAOTTT_SUPERVISOR_ADMIN_KEY"
        )

    log_path = _supervisor_log_path(config.multiverse_root)
    pid = _spawn_supervisor_detached(log_path)
    logger.info(
        "Spawned local supervisor pid=%d; waiting for /route readiness", pid,
    )

    deadline = time.monotonic() + readiness_timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(
                f"{supervisor_url.rstrip('/')}/openapi.json", timeout=1.0,
            )
            if response.status_code < 500:
                # /route may cold-start both embedder and backend. Give that
                # single request the full readiness budget; do not issue
                # overlapping route requests that could rotate tokens.
                remaining = max(1.0, deadline - time.monotonic())
                return _route_to_supervisor(
                    supervisor_url, api_key, timeout=remaining,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_error = exc
        await asyncio.sleep(0.5)
    raise RuntimeError(
        f"Supervisor did not become ready within {readiness_timeout}s; "
        f"check {log_path}: {last_error}"
    )


# -----------------------------------------------------------------------
# stdio ↔ HTTP forwarder
# -----------------------------------------------------------------------

def _is_session_dead(exc: BaseException) -> bool:
    """Does ``exc`` mean the upstream session is gone (vs. an app error)?

    Reconnect ONLY on transport/session-terminated signals — never on a
    legitimate tool error. Tool errors come back as a successful
    ``CallToolResult(isError=True)``, not as an exception, so they never
    reach here. The match is deliberately narrow: stream-closed anyio
    errors, a dropped connection, or an ``McpError`` whose message says the
    session was terminated (the observed ``MCP error 32600: Session
    terminated``). A bare 32600 ("Invalid Request") is NOT matched on the
    code alone so a genuine malformed request can't trigger a reconnect loop.
    """
    if isinstance(exc, (anyio.ClosedResourceError, anyio.BrokenResourceError,
                        anyio.EndOfStream, ConnectionError)):
        # EndOfStream: a *gracefully* closed backend (SIGTERM / idle-watchdog
        # shutdown sends FIN) drains the session's read stream to EOF — the
        # most likely real-world death mode, so it must count as "dead".
        return True
    if isinstance(exc, McpError):
        return "terminat" in str(exc).lower()
    return False


class _Upstream:
    """Owns the (rebuildable) upstream ClientSession + a serialization lock.

    The single streamable-http ``ClientSession`` cannot survive concurrent
    in-flight requests: two simultaneous POSTs break it (``MCP error 32600:
    Session terminated``), after which every later call fails until the
    client reconnects. ``lock`` serializes all upstream access to one
    in-flight request at a time; ``call`` / ``ping`` transparently rebuild
    the session and retry once on a terminated/closed error (which also
    self-heals backend death / idle-watchdog shutdown / cold-start).

    The handlers close over this stable holder, not the session object, so
    the session underneath can be torn down and rebuilt while the stdio
    ``Server`` keeps serving.
    """

    def __init__(
        self,
        url: str,
        *,
        host: str,
        port: int,
        idle_timeout: float,
        spawn_log_path: Path,
        serialize: bool,
        auto_reconnect: bool,
        instructions_override: str | None,
        token: str | None = None,
        supervisor_url: str | None = None,
        api_key: str | None = None,
        spawn_supervisor: bool = False,
    ) -> None:
        self.url = url
        self._host = host
        self._port = port
        self._idle_timeout = idle_timeout
        self._spawn_log_path = spawn_log_path
        self._serialize = serialize
        self._auto_reconnect = auto_reconnect
        self._instructions_override = instructions_override
        # Supervisor mode: when set, connect() attaches the backend token as a
        # Bearer header and _reconnect_locked() re-routes via /route instead of
        # auto-spawning. None/empty on the legacy 7878 path.
        self._token = token or None
        self._supervisor_url = supervisor_url or None
        self._api_key = api_key or None
        self._spawn_supervisor = spawn_supervisor
        self.lock = asyncio.Lock()
        self._stack: contextlib.AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self.instructions: str | None = None

    def _auth_headers(self) -> dict[str, str]:
        """Bearer headers for the upstream backend token. ``{}`` (legacy) when
        no token is configured."""
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    async def connect(self) -> None:
        """Open the streamable-http transport + ClientSession and initialize."""
        stack = contextlib.AsyncExitStack()
        try:
            read, write, _ = await stack.enter_async_context(
                # `or None` keeps the no-token path byte-identical to the
                # legacy streamablehttp_client(self.url) call: an empty dict
                # would also work, but None matches the SDK default exactly.
                streamablehttp_client(self.url, headers=self._auth_headers() or None)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            init_result = await session.initialize()
        except BaseException:
            # initialize() / transport open failed: close whatever was already
            # entered so the streamable-http transport + fds don't leak. Without
            # this the stack stays a local (never assigned to self._stack) and
            # is never closed — and reconnect calls connect() repeatedly, so a
            # flaky backend would leak an fd per failed attempt.
            await stack.aclose()
            raise
        self._stack = stack
        self._session = session
        self.instructions = (
            init_result.instructions
            if self._instructions_override is None
            else self._instructions_override
        )

    async def aclose(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
            self._stack = None
            self._session = None

    async def _reconnect_locked(self) -> None:
        """Rebuild the upstream session. Caller MUST hold ``self.lock`` so
        no other coroutine touches the session mid-rebuild.

        Supervisor mode re-resolves URL+token via ``POST /route`` (the
        supervisor owns spawn/respawn); legacy mode re-probes via
        ``_ensure_backend`` which re-spawns the backend if it died."""
        await self.aclose()
        if self._supervisor_url:
            self.url, self._token = await _route_with_supervisor_autospawn(
                self._supervisor_url,
                self._api_key,
                enabled=self._spawn_supervisor,
            )
        else:
            self.url = await _ensure_backend(
                host=self._host,
                port=self._port,
                idle_timeout=self._idle_timeout,
                spawn_log_path=self._spawn_log_path,
            )
        await self.connect()

    async def call(self, method: str, *args):
        """Forward ``method`` to the upstream session, serialized + healing."""
        if not self._serialize:
            # Legacy no-lock pass-through (rollback). No reconnect either —
            # reconnect needs the lock for an exclusive rebuild.
            return await getattr(self._session, method)(*args)
        async with self.lock:
            try:
                return await getattr(self._session, method)(*args)
            except BaseException as exc:  # noqa: BLE001
                if not (self._auto_reconnect and _is_session_dead(exc)):
                    raise
                logger.warning(
                    "upstream %s failed (%s); rebuilding session + retry once",
                    method, exc,
                )
                await self._reconnect_locked()
                return await getattr(self._session, method)(*args)

    async def ping(self) -> None:
        if not self._serialize:
            await self._session.send_ping()
            return
        async with self.lock:
            try:
                await self._session.send_ping()
            except BaseException as exc:  # noqa: BLE001
                if not (self._auto_reconnect and _is_session_dead(exc)):
                    raise
                logger.warning(
                    "upstream ping failed (%s); rebuilding session", exc,
                )
                await self._reconnect_locked()
                await self._session.send_ping()


async def _ping_loop(upstream: _Upstream, interval: float) -> None:
    """Cold-war heartbeat — refresh the backend's idle timer.

    Any incoming request from this proxy also resets the timer, but
    when the agent is silent for a while only these pings prevent
    backend shutdown. ``upstream.ping`` shares the same serialization lock
    as the tool forwarders, so a ping never races an in-flight tool call.
    """
    while True:
        try:
            await asyncio.sleep(interval)
            await upstream.ping()
            logger.debug("backend ping ok")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Keep looping — upstream.ping() already tried to reconnect; a
            # transient failure shouldn't permanently stop the heartbeat.
            logger.warning("backend ping failed: %s", exc)


def _build_proxy_server(upstream: _Upstream) -> Server:
    """Create a low-level Server that forwards every request type to the
    ``_Upstream`` holder.

    Instead of using FastMCP's decorator-based API (which wraps return
    values into specific MCP types and would force us to unwrap+rewrap
    the upstream's already-built results), we register raw request
    handlers on ``proxy.request_handlers`` that return ``ServerResult``
    directly. Pass-through is perfect: every field of the upstream's
    response makes it back to the agent unmodified — schemas, hidden
    fields, future MCP types we haven't seen yet.

    All handlers route through ``upstream.call(...)`` so they are
    serialized on one in-flight request and heal a broken session.

    ``PingRequest`` is left to the default handler that the lowlevel
    Server pre-registers; it just returns an empty result. The proxy's
    periodic ``upstream.ping`` keeps the backend warm independently.
    """
    proxy = Server("gaottt", instructions=upstream.instructions or "")

    async def list_tools(_req: types.ListToolsRequest) -> types.ServerResult:
        return types.ServerResult(await upstream.call("list_tools"))

    async def call_tool(req: types.CallToolRequest) -> types.ServerResult:
        result = await upstream.call(
            "call_tool", req.params.name, req.params.arguments or {},
        )
        return types.ServerResult(result)

    async def list_resources(_req: types.ListResourcesRequest) -> types.ServerResult:
        return types.ServerResult(await upstream.call("list_resources"))

    async def list_resource_templates(
        _req: types.ListResourceTemplatesRequest,
    ) -> types.ServerResult:
        return types.ServerResult(await upstream.call("list_resource_templates"))

    async def read_resource(req: types.ReadResourceRequest) -> types.ServerResult:
        return types.ServerResult(await upstream.call("read_resource", req.params.uri))

    async def list_prompts(_req: types.ListPromptsRequest) -> types.ServerResult:
        return types.ServerResult(await upstream.call("list_prompts"))

    async def get_prompt(req: types.GetPromptRequest) -> types.ServerResult:
        return types.ServerResult(
            await upstream.call("get_prompt", req.params.name, req.params.arguments or {})
        )

    proxy.request_handlers[types.ListToolsRequest] = list_tools
    proxy.request_handlers[types.CallToolRequest] = call_tool
    proxy.request_handlers[types.ListResourcesRequest] = list_resources
    proxy.request_handlers[types.ListResourceTemplatesRequest] = list_resource_templates
    proxy.request_handlers[types.ReadResourceRequest] = read_resource
    proxy.request_handlers[types.ListPromptsRequest] = list_prompts
    proxy.request_handlers[types.GetPromptRequest] = get_prompt

    return proxy


async def _proxy_session(
    url: str,
    ping_interval: float,
    instructions: str | None,
    *,
    serialize: bool = True,
    auto_reconnect: bool = True,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    spawn_log_path: Path | None = None,
    token: str | None = None,
    supervisor_url: str | None = None,
    api_key: str | None = None,
    spawn_supervisor: bool = False,
) -> None:
    """Connect to backend, run the stdio proxy, until the agent disconnects."""
    upstream = _Upstream(
        url,
        host=host,
        port=port,
        idle_timeout=idle_timeout,
        spawn_log_path=spawn_log_path or _spawn_log_path(),
        serialize=serialize,
        auto_reconnect=auto_reconnect,
        instructions_override=instructions,
        token=token,
        supervisor_url=supervisor_url,
        api_key=api_key,
        spawn_supervisor=spawn_supervisor,
    )
    await upstream.connect()
    logger.info("Proxy connected to backend; serving stdio")

    ping_task = asyncio.create_task(_ping_loop(upstream, ping_interval))
    try:
        proxy_server = _build_proxy_server(upstream)
        async with stdio_server() as (stdio_read, stdio_write):
            await proxy_server.run(
                stdio_read,
                stdio_write,
                proxy_server.create_initialization_options(),
            )
    finally:
        ping_task.cancel()
        try:
            await ping_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await upstream.aclose()


# -----------------------------------------------------------------------
# Public entrypoint
# -----------------------------------------------------------------------

def _spawn_log_path() -> Path:
    """Where to redirect spawned backend stdout/stderr.

    Uses XDG_STATE_HOME if set, else ``~/.local/state/gaottt/``. Keeps
    the spawn log out of the data directory so a wipe of data_dir
    doesn't lose startup diagnostics.
    """
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"
    return base / "gaottt" / "backend.log"


async def run_proxy(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    ping_interval: float = DEFAULT_PING_INTERVAL,
    supervisor_url: str | None = None,
    api_key: str | None = None,
    spawn_supervisor: bool = False,
) -> None:
    """High-level entrypoint: ensure backend, run stdio proxy.

    The proxy is its own spawned process, so it reads its own config to
    learn the concurrency-hardening flags (the backend's env does not reach
    it). Defaults keep serialization + auto-reconnect ON.

    When ``supervisor_url`` is set the proxy resolves the upstream URL + token
    via ``POST /route``. ``spawn_supervisor=True`` additionally starts a
    missing local supervisor; it is opt-in so remote URLs and existing
    deployments retain their previous behaviour.
    """
    # Local import: keep mcp_proxy importable without pulling the full config
    # module at module-load time (it is heavy and not needed for the helpers).
    from gaottt.config import GaOTTTConfig

    config = GaOTTTConfig.from_config_file()
    spawn_log_path = _spawn_log_path()

    if supervisor_url:
        if not api_key:
            raise RuntimeError(
                "supervisor_url is set but no api_key was provided "
                "(set the GAOTTT_API_KEY environment variable)"
            )
        url, token = await _route_with_supervisor_autospawn(
            supervisor_url,
            api_key,
            enabled=spawn_supervisor,
        )
        await _proxy_session(
            url,
            ping_interval=ping_interval,
            instructions=None,
            serialize=config.proxy_serialize_requests_enabled,
            auto_reconnect=config.proxy_auto_reconnect_enabled,
            host=host,
            port=port,
            idle_timeout=idle_timeout,
            spawn_log_path=spawn_log_path,
            token=token,
            supervisor_url=supervisor_url,
            api_key=api_key,
            spawn_supervisor=spawn_supervisor,
        )
        return

    url = await _ensure_backend(
        host=host,
        port=port,
        idle_timeout=idle_timeout,
        spawn_log_path=spawn_log_path,
    )
    await _proxy_session(
        url,
        ping_interval=ping_interval,
        instructions=None,
        serialize=config.proxy_serialize_requests_enabled,
        auto_reconnect=config.proxy_auto_reconnect_enabled,
        host=host,
        port=port,
        idle_timeout=idle_timeout,
        spawn_log_path=spawn_log_path,
    )
