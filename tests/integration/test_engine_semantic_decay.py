"""Phase T Stage 2 — engine 経由の semantic half-life + floor 契約 (integration)。

StubEmbedder は tests/integration/test_engine_archive_ttl.py と同じ
deterministic 実装。経年 node (last_access backdate) の active / passive
recall で:

- legacy flag off では semantic 項が数値上 0 に消滅 (exp(-delta*age) の
  秒 rate 契約バグの再現)
- 新契約 (default) では factor が floor 支配で生存 (7d ≈ 0.675,
  30d ≈ 0.384 @ floor=0.35 / half_life=7d)
- ``ScoreBreakdown.decay_factor`` が factor を運び ``expected_sum`` が成立
- genesis kick 有効 engine (default) の新規 node 即時 recall は従来どおり

determinism のため dream loop / background save は無効化している
(backdate した node は dream 条件を満たしてしまい、background synthetic
recall に last_access を書き戻されると flaky になるため)。
"""
from __future__ import annotations

import hashlib
import time

import numpy as np
import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.index.faiss_index import FaissIndex
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore


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


def _make_config(tmp_path, **overrides) -> GaOTTTConfig:
    defaults = dict(
        embedding_dim=32,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "gaottt.db"),
        faiss_index_path=str(tmp_path / "gaottt.faiss"),
        flush_interval_seconds=999.0,   # disable background flush in tests
        faiss_save_interval_seconds=0.0,
        dream_enabled=False,            # backdated nodes are dream-eligible
        wave_initial_k=3,
        wave_max_depth=1,
    )
    defaults.update(overrides)
    return GaOTTTConfig(**defaults)


async def _make_engine(tmp_path, **config_overrides) -> GaOTTTEngine:
    cfg = _make_config(tmp_path, **config_overrides)
    eng = GaOTTTEngine(
        config=cfg,
        embedder=StubEmbedder(dimension=32),
        faiss_index=FaissIndex(dimension=32),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=cfg.db_path),
    )
    await eng.startup()
    return eng


DOCS = [
    "apple pie recipe with cinnamon spice and sugar",
    "quantum gravity wave simulation update notes",
    "sqlite migration wal checkpoint procedure",
]
QUERY = "apple pie recipe cinnamon"


def _backdate(engine: GaOTTTEngine, node_ids: list[str], age_seconds: float) -> None:
    now = time.time()
    for nid in node_ids:
        state = engine.cache.get_node(nid)
        assert state is not None, f"node {nid} not in cache"
        state.last_access = now - age_seconds
        engine.cache.set_node(state, dirty=True)


def _find(results, node_id):
    for r in results:
        if r.id == node_id:
            return r
    return None


def _expected_factor(cfg: GaOTTTConfig, age_seconds: float) -> float:
    return cfg.semantic_floor + (1.0 - cfg.semantic_floor) * 0.5 ** (
        age_seconds / cfg.semantic_half_life_seconds
    )


@pytest.fixture
async def engine(tmp_path):
    eng = await _make_engine(tmp_path)
    try:
        yield eng
    finally:
        await eng.shutdown()


@pytest.mark.parametrize(
    ("age_seconds", "label"),
    [(7 * 86400.0, "7d"), (30 * 86400.0, "30d")],
)
async def test_backdated_semantic_factor_survives(tmp_path, age_seconds, label):
    eng = await _make_engine(tmp_path)
    try:
        ids = await eng.index_documents([
            {"content": c, "metadata": {"source": "user"}} for c in DOCS
        ])
        _backdate(eng, ids, age_seconds)

        results = await eng.query(text=QUERY, top_k=5, passive=True)
        target = _find(results, ids[0])
        assert target is not None, f"{label}: target node not recalled"
        bd = target.score_breakdown
        assert bd is not None

        expected = _expected_factor(eng.config, age_seconds)
        assert bd.decay_factor == pytest.approx(expected, abs=5e-3), (
            f"{label}: decay_factor {bd.decay_factor} != {expected}"
        )
        # semantic 項 (= virtual_cosine * factor) は floor 支配で生存
        semantic_term = bd.virtual_cosine * bd.decay_factor
        assert semantic_term >= eng.config.semantic_floor * bd.virtual_cosine * 0.9
        assert bd.decay_factor >= eng.config.semantic_floor
    finally:
        await eng.shutdown()


async def test_legacy_flag_off_zeroes_semantic_term_after_7d(tmp_path):
    eng = await _make_engine(tmp_path, semantic_halflife_enabled=False)
    try:
        assert eng.config.semantic_halflife_enabled is False
        ids = await eng.index_documents([
            {"content": c, "metadata": {"source": "user"}} for c in DOCS
        ])
        _backdate(eng, ids, 7 * 86400.0)

        results = await eng.query(text=QUERY, top_k=5, passive=True)
        target = _find(results, ids[0])
        assert target is not None
        bd = target.score_breakdown
        # exp(-0.01 * 604800) = exp(-6048) は float64 で exactly 0.0 に
        # underflow する — legacy 契約では semantic 項が時間経過で消滅する。
        assert bd.decay_factor == 0.0
        assert bd.virtual_cosine * bd.decay_factor == 0.0
        # field/mass 系の項だけで構成されても node は surface する
        assert target.final_score > 0.0
        assert target.final_score == pytest.approx(bd.expected_sum, rel=1e-9)
    finally:
        await eng.shutdown()


async def test_legacy_flag_off_is_deterministic_across_calls(tmp_path):
    # flag off の passive recall は時間に依存する項が decay=0.0 (exact) のみ
    # なので、同一 engine での再 query は bit-for-bit 同一 score を返す。
    eng = await _make_engine(tmp_path, semantic_halflife_enabled=False)
    try:
        ids = await eng.index_documents([
            {"content": c, "metadata": {"source": "user"}} for c in DOCS
        ])
        _backdate(eng, ids, 30 * 86400.0)
        first = await eng.query(text=QUERY, top_k=5, passive=True)
        second = await eng.query(text=QUERY, top_k=5, passive=True)
        assert [r.id for r in first] == [r.id for r in second]
        assert [r.final_score for r in first] == [r.final_score for r in second]
    finally:
        await eng.shutdown()


async def test_legacy_flag_off_keeps_unclamped_future_timestamp(tmp_path):
    # legacy path は future timestamp を clamp しない (bit-for-bit 仕様)。
    eng = await _make_engine(tmp_path, semantic_halflife_enabled=False)
    try:
        ids = await eng.index_documents([
            {"content": c, "metadata": {"source": "user"}} for c in DOCS
        ])
        _backdate(eng, ids, -3600.0)  # last_access を 1h 未来へ
        results = await eng.query(text=QUERY, top_k=5, passive=True)
        target = _find(results, ids[0])
        assert target is not None
        assert target.score_breakdown.decay_factor > 1.0
    finally:
        await eng.shutdown()


async def test_new_contract_clamps_future_timestamp(tmp_path):
    eng = await _make_engine(tmp_path)
    try:
        ids = await eng.index_documents([
            {"content": c, "metadata": {"source": "user"}} for c in DOCS
        ])
        _backdate(eng, ids, -3600.0)  # last_access を 1h 未来へ
        results = await eng.query(text=QUERY, top_k=5, passive=True)
        target = _find(results, ids[0])
        assert target is not None
        assert target.score_breakdown.decay_factor == 1.0
    finally:
        await eng.shutdown()


async def test_active_recall_scores_before_refresh_then_updates_last_access(tmp_path):
    eng = await _make_engine(tmp_path)
    try:
        ids = await eng.index_documents([
            {"content": c, "metadata": {"source": "user"}} for c in DOCS
        ])
        backdated_to = time.time() - 7 * 86400.0
        _backdate(eng, ids, 7 * 86400.0)

        results = await eng.query(text=QUERY, top_k=5)  # active recall
        target = _find(results, ids[0])
        assert target is not None
        bd = target.score_breakdown
        # scoring は _update_simulation より前に走るので、backdate された age
        # の factor が breakdown に乗る
        assert bd.decay_factor == pytest.approx(
            _expected_factor(eng.config, 7 * 86400.0), abs=5e-3,
        )
        # active recall 後は last_access が refresh されている
        state = eng.cache.get_node(ids[0])
        assert state.last_access > backdated_to + 6 * 86400.0
    finally:
        await eng.shutdown()


async def test_breakdown_expected_sum_identity(tmp_path):
    eng = await _make_engine(tmp_path)
    try:
        ids = await eng.index_documents([
            {"content": c, "metadata": {"source": "user"}} for c in DOCS
        ])
        _backdate(eng, ids, 7 * 86400.0)
        results = await eng.query(text=QUERY, top_k=5, passive=True)
        assert results
        for r in results:
            assert r.score_breakdown is not None
            assert r.score_breakdown.decay_factor >= eng.config.semantic_floor
            assert r.final_score == pytest.approx(
                r.score_breakdown.expected_sum, rel=1e-9, abs=1e-12,
            )
    finally:
        await eng.shutdown()


async def test_genesis_immediate_recall_with_default_engine(tmp_path):
    # genesis kick 有効 (default) engine: 新規 node の即時 recall は
    # age≈0 → factor≈1.0 で従来どおり機能する。
    eng = await _make_engine(tmp_path)  # genesis_kick_enabled default True
    try:
        assert eng.config.genesis_kick_enabled is True
        ids = await eng.index_documents([
            {"content": DOCS[0], "metadata": {"source": "user"}},
        ])
        results = await eng.query(text=QUERY, top_k=5)
        target = _find(results, ids[0])
        assert target is not None
        assert target.score_breakdown.decay_factor == pytest.approx(1.0, abs=1e-4)
    finally:
        await eng.shutdown()
