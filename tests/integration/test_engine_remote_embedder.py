"""MV1 — embedding service + RemoteEmbedder integration (test-first / RED stage).

Launches ``create_app(StubServiceEmbedder())`` as a real uvicorn server on an
ephemeral port (background thread), then drives ``GaOTTTEngine`` wired with a
``RemoteEmbedder`` against it: remember -> recall round-trip over real HTTP.

Until WP-5 (``gaottt.embedding.service``) and WP-6 (``gaottt.embedding.remote``)
land, the function-internal imports raise ``ModuleNotFoundError`` — the
expected RED state. Collection stays intact because no unimplemented module is
imported at module top level (WP-1 learning).

StubServiceEmbedder (defined below) is the MV1-specific stub: the canonical
``StubEmbedder`` in ``tests/integration/test_engine_archive_ttl.py`` lacks
``embedder_id`` / ``embedder_version`` / ``encode_queries``, which the
service's ``/info`` and the remote manifest check need. It is defined here as
an independent class so the canonical stub (used by many other tests) is
untouched.
"""
from __future__ import annotations

import hashlib
import socket
import threading
import time
from socket import AF_INET, SOL_SOCKET, SO_REUSEADDR, SOCK_STREAM

import numpy as np
import pytest
import uvicorn

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.index.faiss_index import FaissIndex
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore


# ---------------------------------------------------------------------------
# StubServiceEmbedder — MV1-specific stub (embedder_id / version / encode_queries)
# ---------------------------------------------------------------------------

class StubServiceEmbedder:
    """MV1 stub: keyword-overlap deterministic embeddings that also expose
    ``embedder_id`` / ``embedder_version`` / ``encode_queries`` so it can drive
    the service's ``/info`` response and the RemoteEmbedder manifest check.

    Inherits the determinism contract of the canonical StubEmbedder (md5-seeded
    per-token unit vectors, L2-normalized sum) but is a standalone class to
    avoid perturbing other tests.
    """

    def __init__(
        self,
        dimension: int = 32,
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

def _free_port(host: str = "127.0.0.1") -> int:
    """Reserve and release an ephemeral port for uvicorn to bind.

    Tiny race window exists between release and uvicorn bind, but this is the
    standard pattern and keeps parallel test runs from colliding on a fixed
    port (Codex NB3).
    """
    s = socket.socket(AF_INET, SOCK_STREAM)
    try:
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _start_uvicorn(app, host: str = "127.0.0.1"):
    port = _free_port(host)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait (up to 10s) for the server to accept requests.
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if server.started:
            return server, thread, port
        time.sleep(0.05)
    server.should_exit = True
    thread.join(timeout=5)
    raise RuntimeError("uvicorn did not start within 10s")


def _stop_uvicorn(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# engine builder — direct construction (bypasses build_engine, which is WP-6)
# ---------------------------------------------------------------------------

def _build_engine(data_dir, endpoint: str) -> GaOTTTEngine:
    """Construct an engine whose embedder is a RemoteEmbedder at ``endpoint``.

    ``RemoteEmbedder`` is imported inside the function so collection survives
    its absence (RED until WP-6). Direct construction is used instead of
    ``build_engine`` so the test does not depend on the WP-6 factory branch;
    it also bypasses the factory's embedder-identity check, matching how the
    canonical archive_ttl fixture builds engines.
    """
    from gaottt.embedding.remote import RemoteEmbedder  # RED until WP-6

    cfg = GaOTTTConfig(
        embedding_dim=32,
        data_dir=str(data_dir),
        db_path=str(data_dir / "gaottt.db"),
        faiss_index_path=str(data_dir / "gaottt.faiss"),
        flush_interval_seconds=999.0,  # disable background flush in tests
        wave_initial_k=3,
        wave_max_depth=1,
    )
    embedder = RemoteEmbedder(endpoint=endpoint)
    return GaOTTTEngine(
        config=cfg,
        embedder=embedder,
        faiss_index=FaissIndex(dimension=32),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=cfg.db_path),
    )


# ---------------------------------------------------------------------------
# shared service fixture (module scope — one uvicorn start for tests 1-3)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def service_port():
    from gaottt.embedding.service import create_app  # RED until WP-5

    app = create_app(StubServiceEmbedder(dimension=32))
    server, thread, port = _start_uvicorn(app)
    try:
        yield port
    finally:
        _stop_uvicorn(server, thread)


# ---------------------------------------------------------------------------
# 1. RemoteEmbedder reads /info from the running service
# ---------------------------------------------------------------------------

async def test_remote_embedder_reads_info_from_running_service(service_port):
    from gaottt.embedding.remote import RemoteEmbedder  # RED until WP-6

    embedder = RemoteEmbedder(endpoint=f"http://127.0.0.1:{service_port}")
    assert embedder.dimension == 32
    assert embedder.embedder_id == "stub-service"
    assert embedder.embedder_version == "stub-v0"


# ---------------------------------------------------------------------------
# 2. remember -> recall round-trip over real HTTP
# ---------------------------------------------------------------------------

async def test_remember_recall_roundtrip_via_remote_embedder(service_port, tmp_path):
    endpoint = f"http://127.0.0.1:{service_port}"
    eng = _build_engine(tmp_path, endpoint)
    await eng.startup()
    try:
        ids = await eng.index_documents(
            [{"content": "alpha red fox jumps over the fence"}]
        )
        assert len(ids) == 1

        results = await eng.query(text="fox", top_k=5)
        returned = {r.id for r in results}
        assert ids[0] in returned
    finally:
        await eng.shutdown()


# ---------------------------------------------------------------------------
# 3. Separate data_dirs are mutually invisible (foundation for multiverse)
# ---------------------------------------------------------------------------

async def test_separate_data_dirs_do_not_leak(service_port, tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    endpoint = f"http://127.0.0.1:{service_port}"

    eng_a = _build_engine(dir_a, endpoint)
    eng_b = _build_engine(dir_b, endpoint)
    await eng_a.startup()
    await eng_b.startup()
    try:
        ids_a = await eng_a.index_documents([{"content": "alpha red fox unique"}])
        ids_b = await eng_b.index_documents([{"content": "beta blue whale distinct"}])

        # Querying A for B's distinctive token must not surface B's node.
        res_a = await eng_a.query(text="whale", top_k=5)
        res_b = await eng_b.query(text="fox", top_k=5)
        assert ids_b[0] not in {r.id for r in res_a}
        assert ids_a[0] not in {r.id for r in res_b}
    finally:
        await eng_a.shutdown()
        await eng_b.shutdown()


# ---------------------------------------------------------------------------
# 4. Server cleanup: stop terminates the thread and frees the port
#    (Codex missing test 4 — standalone so it does not disturb the shared
#    fixture above)
# ---------------------------------------------------------------------------

def test_uvicorn_stop_terminates_thread_and_frees_port():
    from gaottt.embedding.service import create_app  # RED until WP-5

    app = create_app(StubServiceEmbedder(dimension=8))
    server, thread, port = _start_uvicorn(app)
    assert server.started

    _stop_uvicorn(server, thread)

    # Thread has terminated — no zombie uvicorn worker.
    assert not thread.is_alive()

    # The listening socket is released: a fresh socket can rebind the port.
    # SO_REUSEADDR handles the normal TIME_WAIT post-close window.
    probe = socket.socket(AF_INET, SOCK_STREAM)
    probe.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()
