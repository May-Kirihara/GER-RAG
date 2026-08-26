"""WP-6d (Phase U / R5) — BM25 snapshot 永続化の integration test.

build 済み BM25 index (hybrid + ambient gate) を ``data_dir/bm25.snapshot``
に content-digest fingerprint 付きで永続化し、次回 startup で fingerprint
が一致すれば build を skip して load する機構の検証 (WP-6c の background
build 機構の上に構築)。

検証項目 (WP-6d 受け入れ条件):
  1. fresh boot — snapshot 無し → background build → snapshot file が
     checksum 付きで存在。同一 data_dir の 2 回目 boot は fingerprint
     一致 → load (build 未実行)、probe query の top hits が 1 回目と
     完全一致。
  2. content 変更 (外部 writer が doc 追加) → fingerprint 不一致 →
     background rebuild → 新 doc が検索可能。
  3. in-session mutation + graceful shutdown → dirty 再保存 → 次 boot は
     load で新 doc が見える (mutation ごとに再保存しない契約の裏返し)。
  4. 破損 snapshot (truncate) → load 拒否 (checksum guard) → build に
     fallback、engine は健康。
  5. cross-universe — universe_id 不一致の snapshot は拒否。
  6. tokenizer/params 変更 (bm25_k1) → 拒否 → 新 param で rebuild。
  7. rollback flag OFF — file を書かない・load もしない。
  8. persist-block — ``_persist_blocked`` latch 下では snapshot write が
    発生しない (INFO log 1 回)。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

import numpy as np
import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import (
    GaOTTTEngine,
    _bm25_snapshot_read,
    _bm25_snapshot_write,
)
from gaottt.index.bm25_index import BM25Index
from gaottt.index.faiss_index import FaissIndex
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore

SNAPSHOT_NAME = "bm25.snapshot"


class StubEmbedder:
    """Deterministic embedder: token → md5-seed unit basis vector の和。

    test_bm25_background_build.py と同一方式 (トークン完全一致のみが
    cosine に効く)。BM25 の検証には cosine は関係ないが、既存 suite と
    同じ fixture 規約に揃える。
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


DOC_A = "quantum flux capacitor markeralpha blueprint"
DOC_B = "mediterranean naval logistics markerbeta history"
DOC_C = "pasta carbonara recipe with egg markercharlie"
DOC_D = "markerdeltaunique lexical payload note"
POPULATE_DOCS = [DOC_A, DOC_B, DOC_C, DOC_D]


def _make_engine(
    tmp_path,
    *,
    background: bool = True,
    snapshot: bool = True,
    **config_overrides,
) -> GaOTTTEngine:
    """production 相当の full wiring (virtual FAISS + hybrid BM25 + gate BM25)。

    test_bm25_background_build.py と同一規約 (gate は trigram 固定、
    background loop 系は interval=0 で無効化)。``config_overrides`` は
    tokenizer/params 変更 test 用の追加 kwarg。
    """
    kwargs = dict(
        embedding_dim=32,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "test.db"),
        faiss_index_path=str(tmp_path / "test.faiss"),
        virtual_faiss_index_path=str(tmp_path / "test.virtual.faiss"),
        virtual_faiss_enabled=True,
        hybrid_bm25_enabled=True,
        bm25_background_build_enabled=background,
        bm25_snapshot_enabled=snapshot,
        ambient_gate_tokenizer="trigram",
        dream_enabled=False,
        faiss_save_interval_seconds=0.0,
        virtual_faiss_save_interval_seconds=0.0,
        flush_interval_seconds=999.0,
    )
    kwargs.update(config_overrides)
    config = GaOTTTConfig(**kwargs)
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
    tmp_path, docs: list[str], *, snapshot: bool = False,
) -> list[str]:
    """テスト対象 engine 起動前に DB に corpus を作る (同期 build で確定)。

    snapshot は既定で無効 — populate 用 engine が snapshot を書くと テスト
    対象 engine の初回 boot が build ではなく load になってしまう
    (test 2 の「外部 writer」でもこの無効化が前提)。
    """
    eng = _make_engine(tmp_path, background=False, snapshot=snapshot)
    await eng.startup()
    try:
        ids = await eng.index_documents([{"content": d} for d in docs])
        await eng.cache.flush_to_store(eng.store)
    finally:
        await eng.shutdown()
    return ids


async def _wait_build_state(engine: GaOTTTEngine, state: str, timeout: float = 15.0):
    """build state を bounded wait する。``state="ready"`` では index swap
    の直後に state が立つが snapshot publish は同一 build task 内でその
    **後**に await される — publish 完了 (= task done) まで待つことで
    「ready 直後の file 存在 assert」が publish と race しないようにする
    (publish 窓内の shutdown は task を cancel して snapshot を失う)。
    load path / sync build は task=None なので即座に返る。
    """
    deadline = time.monotonic() + timeout
    while engine.bm25_build_state != state:
        if time.monotonic() > deadline:
            pytest.fail(
                f"bm25_build_state did not reach {state!r} in {timeout}s "
                f"(current={engine.bm25_build_state!r})"
            )
        await asyncio.sleep(0.01)
    if state == "ready":
        task = engine._bm25_build_task
        if task is not None:
            while not task.done():
                if time.monotonic() > deadline:
                    pytest.fail(
                        f"bm25 build task did not finish (snapshot publish) "
                        f"within {timeout}s"
                    )
                await asyncio.sleep(0.01)


def _fill_sentinel(engine: GaOTTTEngine, calls: list) -> None:
    """``_fill_bm25_indexes`` 呼び出しを記録する監視 (load 検出の fence)。

    snapshot load は fill を一切呼ばない — 呼ばれたら build が走った。
    """
    original = GaOTTTEngine._fill_bm25_indexes

    def fill(hybrid, gate_idx, active_ids, active_texts):
        calls.append(len(active_ids))
        return original(engine, hybrid, gate_idx, active_ids, active_texts)

    engine._fill_bm25_indexes = fill


async def test_fresh_boot_saves_snapshot_and_second_boot_loads(tmp_path):
    """(1) fresh boot は build → snapshot 保存。2 回目 boot は fingerprint
    一致 → load (build なし / fill 未呼び出し)、probe の top hits が
    1 回目と完全一致 (id も score も)。"""
    await _populate(tmp_path, POPULATE_DOCS)
    snap = tmp_path / SNAPSHOT_NAME

    eng = _make_engine(tmp_path, background=True, snapshot=True)
    await eng.startup()
    try:
        await _wait_build_state(eng, "ready")
        assert eng.bm25_build_attempts == 1, "fresh boot must run the build"
        assert snap.exists(), "build completion must publish a snapshot"
        payload = _bm25_snapshot_read(snap)
        assert payload is not None, "published snapshot must pass checksum"
        assert payload["corpus_fingerprint"]["active_count"] == len(POPULATE_DOCS)
        assert payload["universe_id"] == "default"
        assert payload["tokenizer_identity"]["hybrid"]["k1"] == eng.config.bm25_k1
        hits1 = eng.bm25_index.search("markeralpha capacitor", top_k=4)
        gate1 = eng.ambient_gate_index.search("pasta carbonara", top_k=4)
        assert hits1 and gate1
    finally:
        await eng.shutdown()

    eng2 = _make_engine(tmp_path, background=True, snapshot=True)
    fill_calls: list[int] = []
    _fill_sentinel(eng2, fill_calls)
    await eng2.startup()
    try:
        # load は startup 内で完結 — build は走っていない
        assert eng2.bm25_build_state == "ready"
        assert eng2._bm25_build_task is None
        assert eng2.bm25_build_attempts == 0
        assert fill_calls == [], "second boot must not fill (load, not build)"
        # fingerprint pass + load は軽量 (小 corpus; 緩い上限のみ)
        assert eng2.startup_timings["bm25_build"] < 5.0
        assert eng2.bm25_index.size == len(POPULATE_DOCS)
        assert eng2.ambient_gate_index.size == len(POPULATE_DOCS)
        # top hits は 1 回目と完全一致 (pickle round-trip は float bit-exact)
        hits2 = eng2.bm25_index.search("markeralpha capacitor", top_k=4)
        gate2 = eng2.ambient_gate_index.search("pasta carbonara", top_k=4)
        assert hits2 == hits1
        assert gate2 == gate1
    finally:
        await eng2.shutdown()


async def test_sync_build_with_snapshot_roundtrip(tmp_path):
    """(1 変種) rollback の background flag OFF (同期 build) でも snapshot
    保存 + 次 boot は load で即 ready (fill 未呼び出し)。"""
    await _populate(tmp_path, POPULATE_DOCS)
    snap = tmp_path / SNAPSHOT_NAME

    eng = _make_engine(tmp_path, background=False, snapshot=True)
    await eng.startup()
    assert eng.bm25_build_state == "ready"
    assert snap.exists(), "sync build completion must publish a snapshot"
    await eng.shutdown()

    eng2 = _make_engine(tmp_path, background=False, snapshot=True)
    fill_calls: list[int] = []
    _fill_sentinel(eng2, fill_calls)
    await eng2.startup()
    try:
        assert eng2.bm25_build_state == "ready"
        assert fill_calls == []
        assert eng2.bm25_index.size == len(POPULATE_DOCS)
        assert eng2.bm25_index.search("markerbeta", top_k=2)
    finally:
        await eng2.shutdown()


async def test_external_content_mutation_triggers_rebuild_next_boot(tmp_path):
    """(2) snapshot 保存後に外部 writer (別 engine) が doc を追加 →
    次 boot は fingerprint 不一致 → background rebuild → 新 doc が検索可能。"""
    await _populate(tmp_path, POPULATE_DOCS)
    eng = _make_engine(tmp_path)
    await eng.startup()
    await _wait_build_state(eng, "ready")
    await eng.shutdown()
    assert (tmp_path / SNAPSHOT_NAME).exists()

    # 外部 writer: snapshot 無効 engine (= snapshot 機構を通らない経路) が
    # 同一 data_dir に doc を追加。マルチプロセス運用の store 直接変更に相当。
    await _populate(tmp_path, ["zulu external mutation markerzulu payload"])

    eng2 = _make_engine(tmp_path)
    await eng2.startup()
    try:
        await _wait_build_state(eng2, "ready")
        assert eng2.bm25_build_attempts == 1, "fingerprint mismatch must rebuild"
        hits = eng2.bm25_index.search("markerzulu", top_k=3)
        assert hits, "new doc must be searchable after rebuild"
        # rebuild 完了時の再保存もされる (内容は corpus 5 件)
        payload = _bm25_snapshot_read(tmp_path / SNAPSHOT_NAME)
        assert payload is not None
        assert payload["corpus_fingerprint"]["active_count"] == len(POPULATE_DOCS) + 1
    finally:
        await eng2.shutdown()


async def test_dirty_shutdown_resaves_snapshot_with_mutation(tmp_path):
    """(3) in-session mutation (remember) + graceful shutdown → dirty 再保存
    (fingerprint 取り直し) → 次 boot は load で新 doc が見える。
    mutation ごとに再保存はしない — 保存は build 完了時と shutdown 時のみ。"""
    await _populate(tmp_path, POPULATE_DOCS)
    eng = _make_engine(tmp_path)
    await eng.startup()
    await _wait_build_state(eng, "ready")
    new_ids = await eng.index_documents(
        [{"content": "insession mutation markecho document"}]
    )
    assert new_ids
    await eng.shutdown()

    eng2 = _make_engine(tmp_path)
    fill_calls: list[int] = []
    _fill_sentinel(eng2, fill_calls)
    await eng2.startup()
    try:
        # dirty 再保存済み snapshot と fingerprint 一致 → load (build なし)
        assert eng2.bm25_build_state == "ready"
        assert fill_calls == []
        assert eng2.bm25_build_attempts == 0
        hits = eng2.bm25_index.search("markecho", top_k=3)
        assert new_ids[0] in [h for h, _ in hits], (
            f"in-session mutation doc missing from loaded snapshot: {hits}"
        )
    finally:
        await eng2.shutdown()


async def test_corrupt_snapshot_falls_back_to_build(tmp_path):
    """(4) snapshot bytes の truncate → checksum 検証で load 拒否 →
    build に fallback、engine は健康 (query 可能)。"""
    await _populate(tmp_path, POPULATE_DOCS)
    eng = _make_engine(tmp_path)
    await eng.startup()
    await _wait_build_state(eng, "ready")
    await eng.shutdown()
    snap = tmp_path / SNAPSHOT_NAME
    data = snap.read_bytes()
    snap.write_bytes(data[: len(data) // 2])  # truncate

    eng2 = _make_engine(tmp_path)
    await eng2.startup()
    try:
        await _wait_build_state(eng2, "ready")
        assert eng2.bm25_build_attempts == 1, "corrupt snapshot must be rejected"
        assert eng2.bm25_index.size == len(POPULATE_DOCS)
        results = await eng2.query(text="quantum flux capacitor", top_k=3)
        assert results, "engine must stay healthy after rejecting corruption"
    finally:
        await eng2.shutdown()


async def test_cross_universe_snapshot_rejected(tmp_path):
    """(5) universe_id が異なる snapshot は拒否 → rebuild。"""
    await _populate(tmp_path, POPULATE_DOCS)
    eng = _make_engine(tmp_path)
    await eng.startup()
    await _wait_build_state(eng, "ready")
    await eng.shutdown()
    snap = tmp_path / SNAPSHOT_NAME
    payload = _bm25_snapshot_read(snap)
    assert payload is not None
    payload["universe_id"] = "foreign-universe"
    assert _bm25_snapshot_write(snap, payload)

    eng2 = _make_engine(tmp_path)
    await eng2.startup()
    try:
        await _wait_build_state(eng2, "ready")
        assert eng2.bm25_build_attempts == 1, "cross-universe snapshot must be rejected"
    finally:
        await eng2.shutdown()


async def test_tokenizer_param_change_rejected(tmp_path):
    """(6) bm25_k1 を変えた config で起動 → tokenizer identity 不一致で
    snapshot 拒否 → 新 param で rebuild。"""
    await _populate(tmp_path, POPULATE_DOCS)
    eng = _make_engine(tmp_path)
    await eng.startup()
    await _wait_build_state(eng, "ready")
    await eng.shutdown()

    eng2 = _make_engine(tmp_path, bm25_k1=1.2)
    await eng2.startup()
    try:
        await _wait_build_state(eng2, "ready")
        assert eng2.bm25_build_attempts == 1, "param change must be rejected"
        assert eng2.bm25_index.k1 == 1.2
        assert eng2.bm25_index.search("markercharlie", top_k=2)
    finally:
        await eng2.shutdown()


async def test_snapshot_disabled_no_write_no_load(tmp_path):
    """(7) rollback flag OFF: file を 1 度も書かない。既存 snapshot が
    あっても load も書き換えもしない (WP-6c 挙動に復帰)。"""
    await _populate(tmp_path, POPULATE_DOCS)
    snap = tmp_path / SNAPSHOT_NAME

    eng = _make_engine(tmp_path, snapshot=False)
    await eng.startup()
    await _wait_build_state(eng, "ready")
    await eng.shutdown()
    assert not snap.exists(), "flag OFF must never write a snapshot"

    # 对照: snapshot ON engine が file を作った後、OFF engine は
    # load せず (build が走り) file も更新しない
    eng2 = _make_engine(tmp_path, snapshot=True)
    await eng2.startup()
    await _wait_build_state(eng2, "ready")
    await eng2.shutdown()
    assert snap.exists()
    before = snap.read_bytes()

    eng3 = _make_engine(tmp_path, snapshot=False)
    await eng3.startup()
    try:
        await _wait_build_state(eng3, "ready")
        assert eng3.bm25_build_attempts == 1, "flag OFF must build, not load"
        assert snap.read_bytes() == before, "flag OFF must not touch the file"
    finally:
        await eng3.shutdown()


async def test_persist_block_prevents_snapshot_write(tmp_path, caplog):
    """(8) ``_persist_blocked`` latch 下では snapshot write が発生しない
    (INFO log 1 回)。mutation で dirty にしてから latch し、shutdown の
    再保存経路で検証。"""
    await _populate(tmp_path, POPULATE_DOCS)
    eng = _make_engine(tmp_path)
    await eng.startup()
    await _wait_build_state(eng, "ready")
    snap = tmp_path / SNAPSHOT_NAME
    assert snap.exists()
    before = snap.read_bytes()

    await eng.index_documents([{"content": "persistblock markecho doc"}])
    eng._persist_blocked = True
    with caplog.at_level(logging.INFO, logger="gaottt.core.engine"):
        await eng.shutdown()
    assert snap.read_bytes() == before, "persist-blocked engine must not write"
    assert any(
        "persist blocked" in r.message.lower() for r in caplog.records
    ), "skip reason must be logged (one INFO)"


# ---------------------------------------------------------------------------
# WP-8 (final review blocking #3) — unpickle 前の trusted-file policy。
# checksum は攻撃者が再計算できる (偶然の破損検出のみ) ので、真正性は
# file の所有権・権限で担保する。data_dir が信頼境界。
# ---------------------------------------------------------------------------

def _write_valid_snapshot(path, payload: dict | None = None) -> None:
    """checksum 正当な snapshot を最小 payload で書く (policy 検証は
    中身より前の file 属性で行われるため、最小 dict で十分)。"""
    if payload is None:
        payload = {"format_version": 1, "universe_id": "default"}
    assert _bm25_snapshot_write(path, payload)


def test_snapshot_symlink_rejected(tmp_path):
    """(a) symlink 先が正当な snapshot でも symlink 経由の load は拒否。"""
    import os

    real = tmp_path / "bm25.snapshot.real"
    _write_valid_snapshot(real)
    snap = tmp_path / SNAPSHOT_NAME
    os.symlink(real, snap)

    assert _bm25_snapshot_read(snap) is None, (
        "symlinked snapshot must never be unpickled (trust policy)"
    )


def test_snapshot_foreign_owner_rejected(tmp_path, monkeypatch):
    """(b) 他 uid 所有の snapshot は checksum が正当でも拒否。
    実 chown は root 前提なので portable に os.geteuid を不一致値へ
    monkeypatch して所有者検査だけを反転させる。"""
    import os

    snap = tmp_path / SNAPSHOT_NAME
    _write_valid_snapshot(snap)
    real_euid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real_euid + 4242)

    assert _bm25_snapshot_read(snap) is None, (
        "snapshot owned by another uid must be rejected before unpickling"
    )


def test_snapshot_world_writable_rejected(tmp_path):
    """(c) group/other writable の snapshot は拒否 (0o666)。"""
    import os

    snap = tmp_path / SNAPSHOT_NAME
    _write_valid_snapshot(snap)
    os.chmod(snap, 0o666)

    try:
        assert _bm25_snapshot_read(snap) is None, (
            "world-writable snapshot must be rejected before unpickling"
        )
    finally:
        os.chmod(snap, 0o644)  # tmp cleanup を妨げない


def test_snapshot_group_writable_dir_rejected(tmp_path):
    """(d) 親 directory (data_dir) が group/world-writable なら snapshot
    ごと拒否 — 信頼境界は data_dir 単位。"""
    import os

    sub = tmp_path / "data"
    sub.mkdir()
    snap = sub / SNAPSHOT_NAME
    _write_valid_snapshot(snap)
    os.chmod(sub, 0o777)

    try:
        assert _bm25_snapshot_read(snap) is None, (
            "group/world-writable data_dir must reject the snapshot"
        )
    finally:
        os.chmod(sub, 0o700)


def test_trusted_snapshot_still_loads(tmp_path):
    """(e) policy 全項目を満たす正当な snapshot は引き続き load される
    (既存 roundtrip test と同じ条件の read-seam 版 fence)。"""
    snap = tmp_path / SNAPSHOT_NAME
    payload = {
        "format_version": 1,
        "universe_id": "default",
        "corpus_fingerprint": {"digest": "x" * 64, "active_count": 1},
    }
    _write_valid_snapshot(snap, payload)

    loaded = _bm25_snapshot_read(snap)
    assert loaded is not None
    assert loaded["universe_id"] == "default"


def test_snapshot_write_survives_hostile_umask(tmp_path):
    """(round-2) umask 0o777 下でも snapshot は 0o600 で書けて load できる。

    os.open の mode 引数は umask で削られる (0o600 & ~0o777 = 0o000) ので、
    open 済み fd への fchmod 強制がないと write の read-back 検証自体が
    PermissionError に堕ちる。書き込み → 読み出し roundtrip の fence。
    """
    import os
    import stat

    snap = tmp_path / SNAPSHOT_NAME
    payload = {"format_version": 1, "universe_id": "default"}
    old_umask = os.umask(0o777)
    try:
        assert _bm25_snapshot_write(snap, payload), (
            "write (incl. read-back verification) must survive umask 0o777"
        )
    finally:
        os.umask(old_umask)

    mode = stat.S_IMODE(os.stat(snap).st_mode)
    assert mode == 0o600, f"snapshot must be owner-only 0o600, got {mode:#o}"
    loaded = _bm25_snapshot_read(snap)
    assert loaded is not None, "fchmod-hardened snapshot must pass trust policy"
    assert loaded["universe_id"] == "default"


async def test_engine_falls_back_to_build_on_symlinked_snapshot(tmp_path):
    """(f) symlink snapshot は「存在しない」と同じ扱い — engine は通常の
    background build に fallback し、検索は健康。"""
    import os

    await _populate(tmp_path, POPULATE_DOCS)
    eng = _make_engine(tmp_path)
    await eng.startup()
    await _wait_build_state(eng, "ready")
    await eng.shutdown()
    snap = tmp_path / SNAPSHOT_NAME
    real = tmp_path / "planted.snapshot"
    os.replace(snap, real)
    os.symlink(real, snap)

    eng2 = _make_engine(tmp_path)
    await eng2.startup()
    try:
        await _wait_build_state(eng2, "ready")
        assert eng2.bm25_build_attempts == 1, (
            "untrusted snapshot must fall back to a full build"
        )
        assert eng2.bm25_index.size == len(POPULATE_DOCS)
        results = await eng2.query(text="quantum flux capacitor", top_k=3)
        assert results, "engine must stay healthy after rejecting the snapshot"
    finally:
        await eng2.shutdown()
