"""MV3 WP-5 — shared helpers for ``test_supervisor.py`` (no fixtures).

Pure classes and functions only; the test module owns the pytest fixtures so
there is no name-shadowing between imported fixture names and test parameters
(ruff F811) and fixture discovery is native.

What lives here:

* :class:`StubServiceEmbedder` — a deterministic keyword-overlap embedder that
  also exposes ``embedder_id`` / ``embedder_version`` / ``encode_queries`` so it
  can answer the embedding service ``/info`` and the spawned backend's
  ``RemoteEmbedder`` manifest check. Constructed at **dimension=768** so the
  manifest written at universe-creation (whose ``embedding_dim`` comes from
  ``/info``) matches the spawned backend's default ``config.embedding_dim=768``
  — the supervisor's explicit spawn env strips every ``GAOTTT_*`` knob and only
  re-injects four, so the backend runs on stock config and its manifest gate
  must see 768==768.
* uvicorn background-thread lifecycle for the embedding service.
* Per-test ephemeral port-window reservation + backend-process cleanup
  (``kill_backends_in_range`` SIGTERMs spawned backends the supervisor's
  ``_stop_backend`` cannot — it only polls, never kills).
* Thin wrappers (``make_config`` / ``make_supervisor`` / ``asgi_client`` /
  ``create_universe`` / ``mcp_call``) that the tests compose per scenario.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import socket
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import uvicorn
from httpx import ASGITransport, AsyncClient

from gaottt.config import GaOTTTConfig

ADMIN_KEY = "integ-admin-key"
SUPERVISOR = "gaottt.multiverse.supervisor"

# A short backend idle timeout handed to spawned backends via monkeypatch of
# ``supervisor.BACKEND_IDLE_TIMEOUT`` (read at ``_spawn`` call time). Chosen to
# comfortably exceed the cache/FAISS write-behind interval (5s) and the idle
# watcher's ``check_every = max(5.0, timeout/10)`` floor so the respawn test is
# deterministic without a 300s wall-clock wait.
SHORT_IDLE_TIMEOUT = 8.0


# ---------------------------------------------------------------------------
# StubServiceEmbedder — satisfies both /info and the manifest identity check
# ---------------------------------------------------------------------------

class StubServiceEmbedder:
    """Deterministic keyword-overlap embedder exposing the full service surface.

    md5-seeded per-token unit vectors summed and L2-normalized: a query sharing
    tokens with an indexed document scores high, which is what the mutual-
    isolation and respawn-retention assertions rely on. Identical algorithm to
    the canonical StubEmbedder but standalone (the canonical stub lacks
    ``embedder_id`` / ``encode_queries``).
    """

    def __init__(
        self,
        dimension: int = 768,
        embedder_id: str = "stub-service",
        embedder_version: str = "stub-v0",
    ) -> None:
        self._dimension = dimension
        self._embedder_id = embedder_id
        self._embedder_version = embedder_version
        self._token_cache: dict[str, np.ndarray] = {}

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def embedder_id(self) -> str:
        return self._embedder_id

    @property
    def embedder_version(self) -> str:
        return self._embedder_version

    def _token_vec(self, token: str) -> np.ndarray:
        cached = self._token_cache.get(token)
        if cached is not None:
            return cached
        seed = int.from_bytes(hashlib.md5(token.encode()).digest()[:8], "big")
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(self._dimension).astype(np.float32)
        v /= np.linalg.norm(v) + 1e-9
        self._token_cache[token] = v
        return v

    def _embed(self, text: str) -> np.ndarray:
        tokens = [t.lower() for t in text.split() if t.strip()]
        if not tokens:
            return np.zeros(self._dimension, dtype=np.float32)
        v = sum(self._token_vec(t) for t in tokens)
        norm = np.linalg.norm(v)
        return (v / norm).astype(np.float32) if norm > 0 else v.astype(np.float32)

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._embed(t) for t in texts])

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._embed(t) for t in texts])

    def encode_query(self, text: str) -> np.ndarray:
        return self._embed(text).reshape(1, -1)


# ---------------------------------------------------------------------------
# uvicorn background-thread lifecycle (ephemeral port)
# ---------------------------------------------------------------------------

def free_port(host: str = "127.0.0.1") -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def start_uvicorn(app, host: str = "127.0.0.1"):
    port = free_port(host)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
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


def stop_uvicorn(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=5)


def reserve_port_range(size: int = 24) -> tuple[int, int]:
    """Reserve and release an ephemeral ``[base, base+size-1]`` window.

    A tiny release/bind race exists (standard pattern, see plan assumption A5)
    but each test gets its own window so parallel-collection collisions are
    unlikely; the registry additionally bind-checks each candidate at allocation
    time, so a handed-out-but-raced port is skipped rather than crashing spawn.
    """
    base = free_port()
    return base, base + size - 1


# ---------------------------------------------------------------------------
# config / app / client construction
# ---------------------------------------------------------------------------

def make_config(
    root: Path,
    embedder_url: str,
    *,
    admin_key: str = ADMIN_KEY,
    port_range: tuple[int, int] | None = None,
    readiness_timeout: float = 30.0,
    spawn_concurrency: int = 3,
) -> GaOTTTConfig:
    start, end = port_range or reserve_port_range()
    return GaOTTTConfig(
        multiverse_root=str(root),
        supervisor_admin_key=admin_key,
        embedder_endpoint=embedder_url,
        universe_port_range_start=start,
        universe_port_range_end=end,
        supervisor_readiness_timeout=readiness_timeout,
        supervisor_spawn_concurrency=spawn_concurrency,
    )


async def make_supervisor(config: GaOTTTConfig):
    """Build a supervisor app + initialized registry over the given config.

    Mirrors the unit-test pattern: ASGITransport skips the app lifespan, so the
    registry is pre-initialized here. The caller owns ``registry.close()``.
    """
    from gaottt.multiverse.registry import MultiverseRegistry
    from gaottt.multiverse.supervisor import create_supervisor_app

    registry = MultiverseRegistry(Path(config.multiverse_root))
    await registry.initialize()
    app = create_supervisor_app(config, registry)
    return app, registry


@asynccontextmanager
async def asgi_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def admin_headers(key: str = ADMIN_KEY) -> dict[str, str]:
    return {"X-Admin-Key": key}


async def create_universe(client: AsyncClient, owner: str = "owner",
                          admin_key: str = ADMIN_KEY) -> dict:
    """POST /admin/universes against the *real* embedder /info and return body."""
    r = await client.post(
        "/admin/universes",
        json={"owner_label": owner},
        headers=admin_headers(admin_key),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def route_universe(client: AsyncClient, api_key: str) -> dict:
    r = await client.post("/route", json={"api_key": api_key})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# MCP client (token-authed) — used by the heavy mutual-isolation / respawn tests
# ---------------------------------------------------------------------------

async def mcp_call(url: str, token: str, tool: str, args: dict | None = None):
    """Open a Bearer-tokened MCP ClientSession and call ``tool`` once."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool, args or {})


# ---------------------------------------------------------------------------
# spawned-backend process management
# ---------------------------------------------------------------------------

def _ps_lines() -> list[str]:
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return out.stdout.splitlines()


def count_listeners_on_port(port: int) -> int:
    marker = f"--port {port}"
    return sum(
        1 for line in _ps_lines()
        if "gaottt.server.mcp_server" in line and marker in line
    )


def kill_backends_in_range(start: int, end: int) -> None:
    """SIGTERM (then SIGKILL after a grace) every spawned backend on [start, end].

    Matches on the exact ``--port <p>`` argv token the supervisor emits, so only
    backends this test spawned are touched. Best-effort: a miss only leaves a
    backend to idle out on its own.
    """
    ports = set(range(start, end + 1))
    victims: list[int] = []
    for line in _ps_lines():
        if "gaottt.server.mcp_server" not in line:
            continue
        for p in ports:
            if f"--port {p}" in line:
                pid_str = line.strip().split()[0]
                try:
                    victims.append(int(pid_str))
                except ValueError:
                    continue
                break
    for pid in victims:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            except PermissionError:
                break
            # Give SIGTERM a moment before escalating; SIGKILL is terminal.
            if sig == signal.SIGTERM:
                time.sleep(0.4)


async def wait_port_down(port: int, timeout: float = 25.0) -> bool:
    """Poll until nothing answers on ``port`` (or timeout). Returns True if down."""
    import httpx

    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/mcp"
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient(timeout=1.0) as c:
                await c.get(url)
            # something still answered
        except httpx.HTTPError:
            return True
        await asyncio.sleep(0.5)
    return False
