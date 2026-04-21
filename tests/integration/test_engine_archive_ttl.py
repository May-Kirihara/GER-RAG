"""End-to-end engine tests for archive / forget / TTL using a deterministic stub embedder."""
from __future__ import annotations

import hashlib
import time

import numpy as np
import pytest

from ger_rag.config import GERConfig
from ger_rag.core.engine import GEREngine
from ger_rag.index.faiss_index import FaissIndex
from ger_rag.store.cache import CacheLayer
from ger_rag.store.sqlite_store import SqliteStore


class StubEmbedder:
    """Deterministic embedder: keyword-overlap controls similarity.

    Each unique whitespace-separated token gets a stable unit basis vector
    (seeded by md5 of the token, so it is consistent across processes).
    A text's embedding is the L2-normalized sum of its token vectors.
    """

    def __init__(self, dimension: int = 32):
        self._dimension = dimension
        self._token_cache: dict[str, np.ndarray] = {}

    @property
    def dimension(self) -> int:
        return self._dimension

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

    def encode_query(self, text: str) -> np.ndarray:
        return self._embed(text).reshape(1, -1)


@pytest.fixture
async def engine(tmp_path):
    cfg = GERConfig(
        embedding_dim=32,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "ger.db"),
        faiss_index_path=str(tmp_path / "ger.faiss"),
        flush_interval_seconds=999.0,  # disable background flush in tests
        wave_initial_k=3,
        wave_max_depth=1,
    )
    eng = GEREngine(
        config=cfg,
        embedder=StubEmbedder(dimension=32),
        faiss_index=FaissIndex(dimension=32),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=cfg.db_path),
    )
    await eng.startup()
    try:
        yield eng
    finally:
        await eng.shutdown()


async def test_archive_excludes_from_query(engine):
    ids = await engine.index_documents([
        {"content": "alpha note about uv tooling", "metadata": {"source": "user"}},
        {"content": "beta note about uv migration", "metadata": {"source": "user"}},
    ])
    assert len(ids) == 2

    affected = await engine.archive([ids[0]])
    assert affected == 1

    results = await engine.query(text="uv", top_k=5)
    returned_ids = {r.id for r in results}
    assert ids[0] not in returned_ids
    assert ids[1] in returned_ids


async def test_restore_brings_node_back(engine):
    ids = await engine.index_documents([
        {"content": "gamma note about uv", "metadata": {"source": "user"}},
    ])
    await engine.archive(ids)
    assert (await engine.query(text="uv", top_k=5)) == []

    restored = await engine.restore(ids)
    assert restored == 1
    results = await engine.query(text="uv", top_k=5)
    assert ids[0] in {r.id for r in results}


async def test_hard_delete_removes_node_permanently(engine):
    ids = await engine.index_documents([
        {"content": "delta hard-delete me", "metadata": None},
    ])
    deleted = await engine.forget(ids, hard=True)
    assert deleted == 1
    # Document is gone from store
    assert (await engine.store.get_document(ids[0])) is None
    # Restore is a no-op (the node row no longer exists)
    assert (await engine.restore(ids)) == 0


async def test_expired_hypothesis_is_filtered_at_query(engine):
    past = time.time() - 1.0
    ids = await engine.index_documents([
        {
            "content": "stale hypothesis to be filtered",
            "metadata": {"source": "hypothesis"},
            "expires_at": past,
        },
        {
            "content": "live hypothesis kept around",
            "metadata": {"source": "hypothesis"},
            "expires_at": time.time() + 3600.0,
        },
    ])
    results = await engine.query(text="hypothesis", top_k=5)
    returned = {r.id for r in results}
    assert ids[0] not in returned
    assert ids[1] in returned


async def test_startup_auto_archives_expired_nodes(tmp_path):
    cfg = GERConfig(
        embedding_dim=32,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "ger.db"),
        faiss_index_path=str(tmp_path / "ger.faiss"),
        flush_interval_seconds=999.0,
    )
    eng = GEREngine(
        config=cfg,
        embedder=StubEmbedder(dimension=32),
        faiss_index=FaissIndex(dimension=32),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=cfg.db_path),
    )
    await eng.startup()
    ids = await eng.index_documents([
        {
            "content": "to be auto-expired across restart",
            "metadata": {"source": "hypothesis"},
            "expires_at": time.time() - 10.0,
        },
    ])
    await eng.shutdown()

    eng2 = GEREngine(
        config=cfg,
        embedder=StubEmbedder(dimension=32),
        faiss_index=FaissIndex(dimension=32),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=cfg.db_path),
    )
    await eng2.startup()
    try:
        states = await eng2.store.get_node_states(ids)
        assert states[ids[0]].is_archived is True
        assert eng2.cache.get_node(ids[0]) is None
    finally:
        await eng2.shutdown()
