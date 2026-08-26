"""WP-6a (Phase U / R5) — startup フェーズ計装の integration test.

``engine.startup_timings`` が全フェーズキーを秒 (monotonic perf_counter 差分)
で記録すること、informational カウント (node_count / index_size) が入ること、
startup 前の constructed engine では空 dict であることを検証する。
計装は純観測なので、lifecycle 挙動 (検索可否) に影響しないことも確認する。
"""
from __future__ import annotations

import hashlib

import numpy as np

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.index.bm25_index import BM25Index
from gaottt.index.faiss_index import FaissIndex
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore


class StubEmbedder:
    """Deterministic embedder: keyword-overlap controls similarity.

    test_engine_archive_ttl.py と同一方式 (token → md5 seed の unit basis
    vector の L2-normalized sum)。ランダム埋め込みによる flaky 回避。
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


# WP-6a の安定キー (engine.startup のコメントと同じ集合 — 変更時は両方更新)。
PHASE_KEYS = [
    "manifest",
    "lease",
    "store_init",
    "ttl_scan",
    "cache_load",
    "faiss_load",
    "virtual_faiss_load",
    "bm25_build",
    "background_loops",
    "diagnostics",
]


def _make_engine(tmp_path) -> GaOTTTEngine:
    """production 相当の full wiring (virtual FAISS + hybrid BM25 + gate BM25)。

    ambient gate index は sudachi extra 非依存にするため trigram 固定。
    background loop 系 (write-behind 保存 / dream) は interval=0 で無効化 —
    task 生成パス (background_loops フェーズ) は条件判定ごと計測される。
    """
    config = GaOTTTConfig(
        embedding_dim=32,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "test.db"),
        faiss_index_path=str(tmp_path / "test.faiss"),
        virtual_faiss_index_path=str(tmp_path / "test.virtual.faiss"),
        virtual_faiss_enabled=True,
        hybrid_bm25_enabled=True,
        dream_enabled=False,
        faiss_save_interval_seconds=0.0,
        virtual_faiss_save_interval_seconds=0.0,
        flush_interval_seconds=999.0,
    )
    return GaOTTTEngine(
        config=config,
        embedder=StubEmbedder(dimension=32),
        faiss_index=FaissIndex(dimension=32),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=config.db_path),
        virtual_faiss_index=FaissIndex(dimension=32),
        bm25_index=BM25Index(
            k1=config.bm25_k1, b=config.bm25_b, tokenizer=config.bm25_tokenizer,
        ),
        ambient_gate_index=BM25Index(tokenizer="trigram"),
    )


async def test_startup_timings_records_all_phases(tmp_path):
    eng = _make_engine(tmp_path)
    await eng.startup()
    ids = await eng.index_documents([
        {"content": "alpha note about startup timing instrumentation"},
        {"content": "beta note about cold start cost breakdown"},
        {"content": "gamma note about bm25 build expense"},
    ])
    await eng.shutdown()

    eng2 = _make_engine(tmp_path)
    # constructed engine: attribute 常在の契約 — startup 前は空 dict
    assert eng2.startup_timings == {}
    await eng2.startup()
    try:
        timings = eng2.startup_timings
        for key in PHASE_KEYS:
            assert key in timings, f"missing phase key: {key}"
            assert isinstance(timings[key], float), f"{key} not float"
            assert timings[key] >= 0.0, f"{key} negative"
        assert "startup_total" in timings
        assert isinstance(timings["startup_total"], float)
        assert timings["startup_total"] > 0.0
        # informational な規模値 (int)
        assert timings["node_count"] == len(ids) == 3
        assert timings["index_size"] == 3
        assert isinstance(timings["node_count"], int)
        assert isinstance(timings["index_size"], int)
        # 純観測の fence: 計装入り startup を通っても engine は検索可能
        results = await eng2.query(text="startup timing", top_k=3)
        assert results, "engine should remain queryable after instrumented startup"
    finally:
        await eng2.shutdown()
