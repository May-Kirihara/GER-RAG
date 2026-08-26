"""WP-6c (Phase U / R5) — BM25 background build の integration test.

startup の BM25 build (hybrid + ambient gate の 2 index) を background task
化した機構の検証。production 実測 (docs/notes/phase-u/startup-timings.md)
で bm25_build 147s = startup 153s の 96% を占めたため、同期 build を
startup 経路から外して SEMANTIC_READY ≈6s を達成する。

検証項目 (WP-6c 受け入れ条件):
  1. fast-path — flag ON で startup() は build を待たず、window 中は
     hybrid seed pool が raw/virtual だけで縮退運転し ambient gate は
     semantic fallback する。
  2. search during build — build 中の query は error にならず結果を返す。
  3. mutation during build — remember / archive / restore が journal に
     記録され、build 完了後の新 index に replay される。
  4. build exception — fill helper 例外で single retry → give up。
     engine は生存し index は空のまま検索可能。
  5. shutdown during build — cancel が clean に完了し task が残らない。
  6. rollback — flag OFF は現行の同期 build に復帰 (startup 直後 ready)。
  7. 2 index (hybrid + gate) は常に同時に swap する。

決定論性: fill helper を threading.Event で gate し build を任意の位置で
一時停止させる (fill は asyncio.to_thread の worker thread で走るため
threading.Event を使う)。
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
import time

import numpy as np
import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.index.bm25_index import BM25Index
from gaottt.index.faiss_index import FaissIndex
from gaottt.services.memory import _bm25_gate_top
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore


class StubEmbedder:
    """Deterministic embedder: token → md5-seed unit basis vector の和。

    test_engine_startup_timings.py と同一方式。トークン完全一致のみが
    cosine に効くので、trigram 部分一致だけで刺さる BM25-only doc を
    作れる (query トークンが corpus トークンの部分文字列なら cosine=0)。
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


# corpus — 各 doc に unique な marker token を含め、BM25 search の
# assert を id 精度で行えるようにする。
DOC_A = "quantum flux capacitor markeralpha blueprint"
DOC_B = "mediterranean naval logistics markerbeta history"
DOC_C = "pasta carbonara recipe with egg markercharlie"
# BM25-only doc: query 側トークン "markerdelta" は corpus トークン
# "markerdeltaunique" の部分文字列 → stub cosine は 0、trigram BM25 は強一致。
DOC_D = "markerdeltaunique lexical payload note"
POPULATE_DOCS = [DOC_A, DOC_B, DOC_C, DOC_D]


def _make_engine(
    tmp_path,
    *,
    background: bool = True,
) -> GaOTTTEngine:
    """production 相当の full wiring (virtual FAISS + hybrid BM25 + gate BM25)。

    ambient gate index は sudachi extra 非依存にするため trigram 固定
    (test_engine_startup_timings.py と同じ規約)。background loop 系は
    interval=0 で無効化。
    """
    config = GaOTTTConfig(
        embedding_dim=32,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "test.db"),
        faiss_index_path=str(tmp_path / "test.faiss"),
        virtual_faiss_index_path=str(tmp_path / "test.virtual.faiss"),
        virtual_faiss_enabled=True,
        hybrid_bm25_enabled=True,
        bm25_background_build_enabled=background,
        # WP-6d: 本 suite は build 窓の機構を検証するので snapshot を OFF
        # に固定する (populate 用 engine が snapshot を書くと、テスト対象
        # engine の boot が fingerprint 一致で load に短路し "building"
        # state の検証ができなくなる。snapshot 機構自体は
        # test_bm25_snapshot.py の専用 suite で検証 — mechanism isolation)。
        bm25_snapshot_enabled=False,
        # gate index の wiring を trigram にするため config も揃える —
        # background build は config 値から新 index を再生成する
        # (runtime.build_engine と同一 param)ので、wiring と config が
        # 疎通すると build 前後で tokenizer が変わってしまう。
        ambient_gate_tokenizer="trigram",
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


async def _populate(
    tmp_path, docs: list[str], *, archive_last: bool = False,
) -> list[str]:
    """テスト対象 engine 起動前に DB に corpus を作る (同期 build で確定)。

    ``archive_last=True`` の場合、最後の doc を populate 内で archive して
    から shutdown する (テスト対象 engine の snapshot には載らない
    archived doc を作る — 窓内 restore の add-fallback 経路の検証用)。
    """
    eng = _make_engine(tmp_path, background=False)
    await eng.startup()
    try:
        ids = await eng.index_documents(
            [{"content": d} for d in docs]
        )
        if archive_last:
            await eng.archive([ids[-1]])
        await eng.cache.flush_to_store(eng.store)
    finally:
        await eng.shutdown()
    return ids


def _gated_fill(engine: GaOTTTEngine, gate: threading.Event, calls: list):
    """fill helper を threading.Event で block する版に差し替える。

    fill は background 経路では asyncio.to_thread の worker thread から
    呼ばれるため、asyncio.Event ではなく threading.Event を使う。
    """
    original = GaOTTTEngine._fill_bm25_indexes

    def fill(hybrid, gate_idx, active_ids, active_texts):
        calls.append(len(active_ids))
        gate.wait(timeout=30.0)
        return original(engine, hybrid, gate_idx, active_ids, active_texts)

    return fill


async def _wait_build_state(engine: GaOTTTEngine, state: str, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while engine.bm25_build_state != state:
        if time.monotonic() > deadline:
            pytest.fail(
                f"bm25_build_state did not reach {state!r} in {timeout}s "
                f"(current={engine.bm25_build_state!r})"
            )
        await asyncio.sleep(0.01)


async def test_fast_path_startup_returns_before_build(tmp_path, monkeypatch):
    """flag ON: startup() は build 完了を待たない。window 中は hybrid が
    raw/virtual 縮退運転・ambient gate が semantic fallback。完了後は
    2 index が corpus サイズまで埋まり検索経路が元に戻る。"""
    await _populate(tmp_path, POPULATE_DOCS)
    eng = _make_engine(tmp_path, background=True)
    gate = threading.Event()
    calls: list[int] = []
    monkeypatch.setattr(
        eng, "_fill_bm25_indexes", _gated_fill(eng, gate, calls),
    )
    await eng.startup()
    try:
        # --- STARTING window: build は始まっているが完了していない ---
        assert eng.bm25_build_state == "building"
        assert eng.startup_timings["bm25_build"] < 1.0, (
            "flag ON の startup 同期区間は ≈0 のはず (WP-6a 計装契約)"
        )
        assert eng.bm25_index is not None and eng.bm25_index.size == 0
        assert eng.ambient_gate_index is not None
        assert eng.ambient_gate_index.size == 0
        # ambient gate: 空 index は None → gate 呼び出し側の semantic fallback 契約
        assert _bm25_gate_top(eng, "pasta carbonara") is None

        # --- 完了させる ---
        gate.set()
        await _wait_build_state(eng, "ready")

        assert eng.bm25_index.size == len(POPULATE_DOCS)
        assert eng.ambient_gate_index.size == len(POPULATE_DOCS)
        # 完了時に実際の build 所要時間で上書きされる (documented exception)
        assert eng.startup_timings["bm25_build"] >= 0.0
        # hybrid: BM25-only doc (DOC_D) が lexical 経路で surface する
        results = await eng.query(text="markerdelta payload", top_k=4)
        assert any("markerdeltaunique" in r.content for r in results), (
            f"BM25-only match did not surface after swap: {[r.content for r in results]}"
        )
        # ambient gate: gate index が答える (None でなく float)
        assert _bm25_gate_top(eng, "pasta carbonara") is not None
    finally:
        gate.set()
        await eng.shutdown()


async def test_search_during_build_returns_results(tmp_path, monkeypatch):
    """build 中に発行した query は error にならず raw/virtual 経路で
    結果を返す。swap 後は BM25 経路の寄与が現れる。"""
    await _populate(tmp_path, POPULATE_DOCS)
    eng = _make_engine(tmp_path, background=True)
    gate = threading.Event()
    monkeypatch.setattr(
        eng, "_fill_bm25_indexes", _gated_fill(eng, gate, []),
    )
    await eng.startup()
    try:
        assert eng.bm25_build_state == "building"
        # build 中: query は結果を返す (BM25 空 → raw/virtual のみ)
        during = await eng.query(text="quantum flux capacitor", top_k=3)
        assert during, "query during build must not error and must return results"

        gate.set()
        await _wait_build_state(eng, "ready")

        # swap 後: BM25-only doc が lexical 経路で届く
        after = await eng.query(text="markerdelta payload", top_k=4)
        assert any("markerdeltaunique" in r.content for r in after)
        # 直接 index search も新オブジェクトで機能している
        hits = eng.bm25_index.search("markerdelta", top_k=2)
        assert hits, "post-swap BM25 index must answer lexical queries"
    finally:
        gate.set()
        await eng.shutdown()


async def test_mutations_during_build_replayed_into_new_index(
    tmp_path, monkeypatch,
):
    """build 窓内の remember / archive / restore は journal に記録され、
    完了後の新 index に反映される:
      - 窓内 remember → 新 index で BM25 search 可能
      - 窓内 archive → 新 index から除外
      - snapshot に無い doc の窓内 restore → content から add される"""
    doc_archived_before = "prearchived markerfoxtrot dormant content"
    ids = await _populate(
        tmp_path, POPULATE_DOCS + [doc_archived_before], archive_last=True,
    )
    archived_before_id = ids[-1]
    doc_a_id = ids[0]

    eng = _make_engine(tmp_path, background=True)
    gate = threading.Event()
    monkeypatch.setattr(
        eng, "_fill_bm25_indexes", _gated_fill(eng, gate, []),
    )
    await eng.startup()
    try:
        assert eng.bm25_build_state == "building"
        # snapshot は既に取得済み (fill が block 中 = snapshot 後)。
        # pre-archived doc は snapshot に入らない
        assert archived_before_id not in eng.cache.node_cache

        # (1) 窓内 remember — journal add
        new_ids = await eng.index_documents(
            [{"content": "jjnewmutation markecho payload"}]
        )
        new_id = new_ids[0]
        # (2) 窓内 archive — journal remove (sync 経路と同じく hybrid のみ)
        await eng.archive([doc_a_id])
        # (3) 窓内 restore — snapshot 外 doc の restore (journal restore + texts)
        await eng.restore([archived_before_id])

        gate.set()
        await _wait_build_state(eng, "ready")

        # corpus 5 (A-D + foxtrot) - archived A + new E = 5
        assert eng.bm25_index.size == 5
        # (1) 新規 doc は BM25 で見つかる
        hits = eng.bm25_index.search("markecho", top_k=3)
        assert new_id in [h for h, _ in hits], (
            f"mutation-during-build add was not replayed: {hits}"
        )
        # (2) archived doc は BM25 から消えている
        hits_a = eng.bm25_index.search("markeralpha", top_k=5)
        assert doc_a_id not in [h for h, _ in hits_a], (
            f"mutation-during-build archive was not replayed: {hits_a}"
        )
        # (3) snapshot 外から restore された doc は content から add されている
        hits_f = eng.bm25_index.search("markerfoxtrot", top_k=3)
        assert archived_before_id in [h for h, _ in hits_f], (
            f"restore of snapshot-absent doc was not replayed: {hits_f}"
        )
        # ambient gate 側も swap 済み (add は gate にも反映)
        assert eng.ambient_gate_index.size == 5
        # journal に 3 mutation 記録 = generation が進んでいる
        assert eng._bm25_mutation_generation >= 3
    finally:
        gate.set()
        await eng.shutdown()


async def test_build_exception_single_retry_then_failed(tmp_path, monkeypatch):
    """fill helper が常に例外 → 1 回だけ自動 retry → give up。
    engine は生存し、index は空のまま検索可能、state は failed。"""
    await _populate(tmp_path, POPULATE_DOCS)
    eng = _make_engine(tmp_path, background=True)
    calls: list[int] = []

    def exploding_fill(hybrid, gate_idx, active_ids, active_texts):
        calls.append(len(active_ids))
        raise RuntimeError("injected fill failure")

    monkeypatch.setattr(eng, "_fill_bm25_indexes", exploding_fill)
    await eng.startup()
    try:
        await _wait_build_state(eng, "failed", timeout=15.0)
        # single automatic retry: 初回 + retry の 2 回だけ呼ばれる
        assert len(calls) == 2, f"expected exactly 2 attempts, got {len(calls)}"
        assert eng.bm25_build_attempts == 2
        # engine は生存・検索可能 (raw/virtual 経路)
        assert eng.bm25_index is not None and eng.bm25_index.size == 0
        results = await eng.query(text="quantum flux capacitor", top_k=3)
        assert results, "engine must stay queryable after BM25 build failure"
        # task は終了済み (残留なし)
        assert eng._bm25_build_task is not None
        assert eng._bm25_build_task.done()
    finally:
        await eng.shutdown()


async def test_shutdown_during_build_cancels_cleanly(tmp_path, monkeypatch):
    """build 実行中の shutdown() は task を cancel して await し、
    pending task を残さない。state は idle に戻る。"""
    await _populate(tmp_path, POPULATE_DOCS)
    eng = _make_engine(tmp_path, background=True)

    # gate timeout を短く: shutdown の cancel が届いた後、worker thread が
    # 自抜けして future 完了 → CancelledError が task に配達される。
    gate = threading.Event()

    def gated_fill(hybrid, gate_idx, active_ids, active_texts):
        gate.wait(timeout=3.0)
        return GaOTTTEngine._fill_bm25_indexes(
            eng, hybrid, gate_idx, active_ids, active_texts,
        )

    monkeypatch.setattr(eng, "_fill_bm25_indexes", gated_fill)
    await eng.startup()
    assert eng.bm25_build_state == "building"
    # shutdown は build 完了を待たず (かつ hang せず) 戻る
    t0 = time.monotonic()
    await asyncio.wait_for(eng.shutdown(), timeout=30.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 20.0, f"shutdown blocked on background build: {elapsed:.1f}s"
    # task は完全に終了している (pending task 警告の出どころがない)
    assert eng._bm25_build_task is not None
    assert eng._bm25_build_task.done()
    assert eng.bm25_build_state == "idle"
    # swap 前に止まったので index は空のまま (中途半端な swap なし)
    assert eng.bm25_index.size == 0


async def test_rollback_sync_build_at_startup(tmp_path):
    """flag OFF: startup() が同期 build を行い、戻り時点で即 ready。"""
    await _populate(tmp_path, POPULATE_DOCS)
    eng = _make_engine(tmp_path, background=False)
    await eng.startup()
    try:
        assert eng.bm25_build_state == "ready"
        assert eng.bm25_build_attempts == 0
        assert eng._bm25_build_task is None, "rollback path must not spawn a task"
        assert eng.bm25_index.size == len(POPULATE_DOCS)
        assert eng.ambient_gate_index.size == len(POPULATE_DOCS)
        hits = eng.bm25_index.search("markerbeta", top_k=2)
        assert hits, "sync rollback path must build a searchable index"
    finally:
        await eng.shutdown()


async def test_both_indexes_swap_together(tmp_path, monkeypatch):
    """hybrid と ambient gate の 2 index は同一 swap で同時に新しい
    object に切り替わる (片方だけ古い状態を作らない)。"""
    await _populate(tmp_path, POPULATE_DOCS)
    eng = _make_engine(tmp_path, background=True)
    gate = threading.Event()
    monkeypatch.setattr(
        eng, "_fill_bm25_indexes", _gated_fill(eng, gate, []),
    )
    await eng.startup()
    try:
        assert eng.bm25_build_state == "building"
        old_hybrid = eng.bm25_index
        old_gate = eng.ambient_gate_index

        gate.set()
        await _wait_build_state(eng, "ready")

        assert eng.bm25_index is not old_hybrid, "hybrid index was not swapped"
        assert eng.ambient_gate_index is not old_gate, "gate index was not swapped"
        assert eng.bm25_index.size == eng.ambient_gate_index.size
        assert eng.bm25_index.size == len(POPULATE_DOCS)
    finally:
        gate.set()
        await eng.shutdown()


async def test_compact_during_build_invalidates_swap(tmp_path, monkeypatch):
    """build 窓内の compact(rebuild_faiss=True) は現行 index を同期的に
    再構築する — background build の新 object は破棄され swap しない
    (compact 済み index を古い snapshot で上書き戻さない)。"""
    await _populate(tmp_path, POPULATE_DOCS)
    eng = _make_engine(tmp_path, background=True)
    gate = threading.Event()
    monkeypatch.setattr(
        eng, "_fill_bm25_indexes", _gated_fill(eng, gate, []),
    )
    await eng.startup()
    try:
        assert eng.bm25_build_state == "building"
        current_hybrid = eng.bm25_index

        report = await eng.compact(rebuild_faiss=True)
        assert report["faiss_rebuilt"] is True
        # compact は現行 index を同期的に再構築済み
        assert current_hybrid.size == len(POPULATE_DOCS)

        gate.set()
        await _wait_build_state(eng, "ready")
        # swap は起きない: compact 済みの現行 object のまま (invalidation)
        assert eng.bm25_index is current_hybrid
        assert eng.bm25_index.size == len(POPULATE_DOCS)
        hits = eng.bm25_index.search("markercharlie", top_k=2)
        assert hits
    finally:
        gate.set()
        await eng.shutdown()
