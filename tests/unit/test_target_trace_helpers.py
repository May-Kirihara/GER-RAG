"""Unit tests for ``scripts/diag_target_trace.py`` (Phase U WP-4 / R4).

Covers the pure helpers (rank search / seed-pool sizing mirror / pool-drop
diagnosis / BM25-ready bounded wait), the CLI wiring, and fast integration
paths on a tiny StubEmbedder corpus: the ``--json`` happy-path, the
BM25-not-ready WARNING-continue path, and calibrate_ambient_gate.py's
abort-when-bm25-axis-unavailable wiring (WP-6c empty-window fix).

The heavy paths (real RURI, production copy) are exercised manually by the
PM per Plans-Phase-U-Review-Hardening §4 WP-4 — not here.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.index.bm25_index import BM25Index
from gaottt.index.faiss_index import FaissIndex
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "diag_target_trace.py"
)
CALIBRATE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "calibrate_ambient_gate.py"
)


def _load_module_at(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # dataclass の文字列 annotation 解決は sys.modules 登録を要求する
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_module():
    return _load_module_at(SCRIPT_PATH, "diag_target_trace")


@pytest.fixture(scope="module")
def mod():
    return _load_module()


@pytest.fixture(scope="module")
def calib_mod():
    return _load_module_at(CALIBRATE_PATH, "calibrate_ambient_gate")


# ---------------------------------------------------------------------------
# find_rank
# ---------------------------------------------------------------------------


def test_find_rank_present_first(mod):
    hits = [("a", 0.9), ("b", 0.8), ("c", 0.7)]
    hit = mod.find_rank(hits, "a")
    assert hit is not None
    assert hit.rank == 1
    assert hit.score == pytest.approx(0.9)


def test_find_rank_present_middle(mod):
    hits = [("a", 0.9), ("b", 0.8), ("c", 0.7)]
    hit = mod.find_rank(hits, "c")
    assert hit is not None
    assert hit.rank == 3
    assert hit.score == pytest.approx(0.7)


def test_find_rank_absent_returns_none(mod):
    hits = [("a", 0.9), ("b", 0.8)]
    assert mod.find_rank(hits, "zzz") is None


def test_find_rank_empty_hits(mod):
    assert mod.find_rank([], "a") is None


# ---------------------------------------------------------------------------
# mirror_seed_pool_size — engine の WP-5 provenance mirror と同一の分岐
# ---------------------------------------------------------------------------


def test_mirror_seed_pool_size_no_boost(mod):
    cfg = GaOTTTConfig(
        wave_initial_k=3, wave_seed_mass_alpha=0.0,
        wave_seed_pool_size=50, persona_boost_alpha=0.5,
    )
    # mass α=0 / persona proximity なし → initial_k のまま
    assert mod.mirror_seed_pool_size(cfg) == 3


def test_mirror_seed_pool_size_mass_boost(mod):
    cfg = GaOTTTConfig(
        wave_initial_k=3, wave_seed_mass_alpha=0.05,
        wave_seed_pool_size=50,
    )
    assert mod.mirror_seed_pool_size(cfg) == 50


def test_mirror_seed_pool_size_persona_boost(mod):
    cfg = GaOTTTConfig(
        wave_initial_k=3, wave_seed_mass_alpha=0.0,
        wave_seed_pool_size=50, persona_boost_alpha=0.5,
    )
    assert mod.mirror_seed_pool_size(
        cfg, persona_proximities_present=True,
    ) == 50


def test_mirror_seed_pool_size_wave_k_override_wins(mod):
    cfg = GaOTTTConfig(
        wave_initial_k=3, wave_seed_mass_alpha=0.05,
        wave_seed_pool_size=50,
    )
    # wave_k=1000 (source_filter 運用) の明示 override は pool size を上回る
    assert mod.mirror_seed_pool_size(cfg, wave_k=1000) == 1000


# ---------------------------------------------------------------------------
# diagnose_pool_drop — どの候補源で落ちたか
# ---------------------------------------------------------------------------

_TARGET = "target-1"


def _diagnosis(mod, *, raw=None, virtual=None, bm25=None, fused=(), reached=None):
    return mod.diagnose_pool_drop(
        _TARGET,
        raw_hits=raw,
        virtual_hits=virtual,
        bm25_hits=bm25,
        fused_pool=fused,
        wave_reached=reached or {},
        pool_size=50,
    )


def test_diagnose_all_pools_hit(mod):
    d = _diagnosis(
        mod,
        raw=[("x", 0.9), (_TARGET, 0.8)],
        virtual=[(_TARGET, 0.85)],
        bm25=[("x", 12.0), (_TARGET, 9.0)],
        fused=[("x", 0.03), (_TARGET, 0.02)],
        reached={_TARGET: 1.0},
    )
    assert d.in_raw_pool and d.raw_rank == 2
    assert d.in_virtual_pool and d.virtual_rank == 1
    assert d.in_bm25_pool and d.bm25_rank == 2
    assert d.in_fused_pool and d.fused_rank == 2
    assert d.in_wave_reached and d.wave_force == 1.0
    assert d.missed_sources == []
    assert d.unavailable_sources == []


def test_diagnose_target_missing_everywhere(mod):
    d = _diagnosis(
        mod,
        raw=[("x", 0.9)],
        virtual=[("x", 0.9)],
        bm25=[("x", 5.0)],
        fused=[("x", 0.03)],
        reached={"x": 1.0},
    )
    assert d.missed_sources == [
        "raw_pool", "virtual_pool", "bm25_pool", "fused_seed_pool",
        "wave_reach",
    ]


def test_diagnose_unavailable_leg_is_not_a_miss(mod):
    # leg 自体が無効 (None) は「missed」ではなく「unavailable」
    d = _diagnosis(mod, raw=[(_TARGET, 0.9)], virtual=None, bm25=None)
    assert d.unavailable_sources == ["virtual_pool", "bm25_pool"]
    assert "virtual_pool" not in d.missed_sources
    assert "bm25_pool" not in d.missed_sources
    assert d.raw_rank == 1


def test_diagnose_empty_searched_leg_is_a_miss(mod):
    # 検索は走ったが 0 件 (空 list) は有効な「missed」
    d = _diagnosis(mod, raw=[(_TARGET, 0.9)], virtual=[], bm25=[])
    assert "virtual_pool" in d.missed_sources
    assert "bm25_pool" in d.missed_sources
    assert d.unavailable_sources == []


def test_diagnosis_to_dict_shape(mod):
    d = _diagnosis(
        mod,
        raw=[(_TARGET, 0.9)],
        virtual=None,
        bm25=[("x", 5.0)],
        fused=[(_TARGET, 0.02)],
        reached={_TARGET: 0.5},
    )
    payload = d.to_dict()
    assert payload["pool_size"] == 50
    assert payload["raw_pool"] == {"available": True, "in_pool": True, "rank": 1}
    assert payload["virtual_pool"] == {
        "available": False, "in_pool": False, "rank": None,
    }
    assert payload["bm25_pool"]["in_pool"] is False
    assert payload["fused_seed_pool"] == {"in_pool": True, "rank": 1}
    assert payload["wave_reach"] == {"reached": True, "force": 0.5}
    assert payload["missed_sources"] == ["bm25_pool"]
    assert payload["unavailable_sources"] == ["virtual_pool"]


# ---------------------------------------------------------------------------
# wait_for_bm25_ready — WP-6c background build の bounded wait
# (両 script に同一実装が置いてあるので drift を検出できるよう両方叩く)
# ---------------------------------------------------------------------------


class _StaticStateEngine:
    """attribute-driven stub — 常に同じ bm25_build_state。"""

    def __init__(self, state: str):
        self.bm25_build_state = state


class _FlipStateEngine:
    """bm25_build_state を building_reads 回 "building" を返した後
    final に遷移する stub (poll ごとの状態遷移を模す)。"""

    def __init__(self, final: str, building_reads: int):
        self._final = final
        self._building_reads = building_reads

    @property
    def bm25_build_state(self) -> str:
        if self._building_reads > 0:
            self._building_reads -= 1
            return "building"
        return self._final


async def test_wait_immediate_terminal_states(mod, calib_mod):
    for m in (mod, calib_mod):
        for state in ("ready", "failed", "idle"):
            stub = _StaticStateEngine(state)
            assert await m.wait_for_bm25_ready(stub, timeout=1.0) == state


async def test_wait_building_then_ready(mod, calib_mod):
    for m in (mod, calib_mod):
        stub = _FlipStateEngine("ready", building_reads=2)
        assert await m.wait_for_bm25_ready(
            stub, timeout=5.0, poll_interval=0.01,
        ) == "ready"


async def test_wait_building_then_failed(mod, calib_mod):
    for m in (mod, calib_mod):
        stub = _FlipStateEngine("failed", building_reads=1)
        assert await m.wait_for_bm25_ready(
            stub, timeout=5.0, poll_interval=0.01,
        ) == "failed"


async def test_wait_timeout_while_building(mod, calib_mod):
    for m in (mod, calib_mod):
        stub = _StaticStateEngine("building")
        assert await m.wait_for_bm25_ready(
            stub, timeout=0.03, poll_interval=0.01,
        ) == "timeout"


# ---------------------------------------------------------------------------
# CLI arg wiring (argparse dry)
# ---------------------------------------------------------------------------


def test_parser_full_args(mod):
    args = mod.build_parser().parse_args([
        "--data-dir", "/tmp/copy",
        "--query", "recall ambient 空返し",
        "--target-id", "de1b528f-f95a-46e8-a28d-7a4fbd580806",
        "--top-n", "5",
        "--json",
    ])
    assert args.data_dir == "/tmp/copy"
    assert args.query == "recall ambient 空返し"
    assert args.target_id == "de1b528f-f95a-46e8-a28d-7a4fbd580806"
    assert args.top_n == 5
    assert args.json is True


def test_parser_defaults(mod):
    args = mod.build_parser().parse_args([
        "--data-dir", "d", "--query", "q", "--target-id", "t",
    ])
    assert args.top_n == 20
    assert args.json is False


@pytest.mark.parametrize("argv", [
    ["--query", "q", "--target-id", "t"],           # --data-dir なし
    ["--data-dir", "d", "--target-id", "t"],        # --query なし
    ["--data-dir", "d", "--query", "q"],            # --target-id なし
])
def test_parser_required_args(mod, argv, capsys):
    with pytest.raises(SystemExit) as excinfo:
        mod.build_parser().parse_args(argv)
    assert excinfo.value.code == 2
    capsys.readouterr()  # argparse の usage 出力を消費


# ---------------------------------------------------------------------------
# integration happy-path — tiny StubEmbedder corpus で --json を完走
# ---------------------------------------------------------------------------


class StubEmbedder:
    """Deterministic token-overlap embedder (test_engine_archive_ttl.py 流)."""

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


QUERY = "zephyr falcon engine failure diagnostic"


def _make_stub_engine(tmp_path: Path) -> GaOTTTEngine:
    cfg = GaOTTTConfig(
        embedding_dim=32,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "gaottt.db"),
        faiss_index_path=str(tmp_path / "gaottt.faiss"),
        virtual_faiss_index_path=str(tmp_path / "gaottt.virtual.faiss"),
        flush_interval_seconds=999.0,
        faiss_save_interval_seconds=0.0,
        virtual_faiss_save_interval_seconds=0.0,
        wave_initial_k=3,
        wave_max_depth=1,
        wave_seed_mass_alpha=0.0,
        dream_enabled=False,
        virtual_faiss_enabled=True,
        hybrid_bm25_enabled=True,
    )
    return GaOTTTEngine(
        config=cfg,
        embedder=StubEmbedder(dimension=32),
        faiss_index=FaissIndex(dimension=32),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=cfg.db_path),
        virtual_faiss_index=FaissIndex(dimension=32),
        bm25_index=BM25Index(
            k1=cfg.bm25_k1, b=cfg.bm25_b, tokenizer=cfg.bm25_tokenizer,
        ),
        # ambient_gate_index=None — sudachi extra なし環境と同じ
    )


async def test_run_json_happy_path(tmp_path, monkeypatch, capsys, mod):
    """target が raw pool に入り verdict が計算され JSON が parse できる。"""
    # 1) corpus を作って一旦完全 shutdown (script が disk から再 startup する
    #    — production copy と同じ lifecycle)
    eng = _make_stub_engine(tmp_path)
    await eng.startup()
    try:
        ids = await eng.index_documents([
            # target: query と完全同一 token 構成 → stub cosine 1.0
            {"content": QUERY, "metadata": {"source": "agent", "tags": ["t"]}},
            {"content": "garden tomatoes harvest season watering"},
            {"content": "medieval castles stone walls siege"},
            {"content": "deep sea anglerfish bioluminescence"},
            {"content": "compiler register allocation linear scan"},
        ])
    finally:
        await eng.shutdown()
    target_id = ids[0]

    # 2) build_engine を stub engine に差し替え (script の config は無視)
    monkeypatch.setattr(mod, "build_engine", lambda _config: eng)

    args = mod.build_parser().parse_args([
        "--data-dir", str(tmp_path),
        "--query", QUERY,
        "--target-id", target_id,
        "--top-n", "5",
        "--json",
    ])
    rc = await mod._run(args)
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)

    # node state
    assert payload["node"]["found"] is True
    assert payload["node"]["source"] == "agent"
    assert payload["node"]["mass"] is not None

    # per-index rank: raw top-1 / hybrid BM25 にも入っている (同一文なので)
    assert payload["index_ranks"]["raw_faiss"]["in_pool"] is True
    assert payload["index_ranks"]["raw_faiss"]["rank"] == 1
    assert payload["index_ranks"]["hybrid_bm25"]["in_pool"] is True
    # ambient gate index は未配線 → unavailable (graceful degradation)
    assert payload["index_ranks"]["ambient_gate_bm25"]["available"] is False

    # qualification verdict: raw axis が 0.75 を確実に超える (cosine 1.0)
    assert payload["qualification"]["axes"]["raw_cos"] == pytest.approx(1.0, abs=1e-5)
    assert payload["qualification"]["axis_pass"]["raw"] is True
    assert payload["qualification"]["qualified"] is True
    assert payload["qualification"]["confidence"] == pytest.approx(1.0)

    # final passive query
    assert payload["final_query"]["target_in_results"] is True
    assert payload["final_query"]["target_rank"] == 1
    assert payload["final_query"]["passive"] is True
    bd = payload["final_query"]["breakdown"]
    assert bd is not None
    assert bd["raw_cosine"] == pytest.approx(1.0, abs=1e-5)
    assert bd["qualified"] is True

    # pool diagnosis — 全段階 IN
    pool = payload["pool_diagnosis"]
    assert pool["raw_pool"]["in_pool"] is True
    assert pool["fused_seed_pool"]["in_pool"] is True
    assert pool["wave_reach"]["reached"] is True
    assert pool["missed_sources"] == []


async def test_run_target_not_found_exits_1(tmp_path, monkeypatch, capsys, mod):
    eng = _make_stub_engine(tmp_path)
    monkeypatch.setattr(mod, "build_engine", lambda _config: eng)
    args = mod.build_parser().parse_args([
        "--data-dir", str(tmp_path),
        "--query", QUERY,
        "--target-id", "00000000-0000-0000-0000-000000000000",
        "--json",
    ])
    rc = await mod._run(args)
    assert rc == 1
    assert "not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# WP-6c — BM25 ready 待ちの script wiring (build 空窓 fix)
# ---------------------------------------------------------------------------


async def _seed_corpus_and_recycle(tmp_path: Path) -> str:
    """corpus を作って一旦完全 shutdown (script が disk から再 startup する
    — production copy と同じ lifecycle)。target id を返す。"""
    eng = _make_stub_engine(tmp_path)
    await eng.startup()
    try:
        ids = await eng.index_documents([
            {"content": QUERY, "metadata": {"source": "agent"}},
            {"content": "garden tomatoes harvest season watering"},
        ])
    finally:
        await eng.shutdown()
    return ids[0]


async def test_run_bm25_not_ready_warns_and_continues(
    tmp_path, monkeypatch, capsys, mod,
):
    """build が ready に届かない場合: WARNING (stderr) + notes 入り + 続行。"""
    target_id = await _seed_corpus_and_recycle(tmp_path)
    eng = _make_stub_engine(tmp_path)
    monkeypatch.setattr(mod, "build_engine", lambda _config: eng)

    async def _never_ready(_engine, timeout=None, poll_interval=None):
        return "timeout"

    monkeypatch.setattr(mod, "wait_for_bm25_ready", _never_ready)
    args = mod.build_parser().parse_args([
        "--data-dir", str(tmp_path),
        "--query", QUERY,
        "--target-id", target_id,
        "--top-n", "5",
        "--json",
    ])
    rc = await mod._run(args)
    assert rc == 0  # trace としては成功 (raw/virtual は有効)

    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "raw/virtual ranks are still valid" in captured.err
    # stdout は JSON のみ — warning は stderr + payload notes の両経路
    payload = json.loads(captured.out)
    assert any("did not reach 'ready'" in n for n in payload["notes"])


def _calib_args(tmp_path: Path) -> argparse.Namespace:
    probes = tmp_path / "probes.json"
    probes.write_text(json.dumps({
        "positives": [{"query": "zephyr falcon engine failure"}],
        "negatives": [{"query": "random off topic penguins"}],
    }), encoding="utf-8")
    return argparse.Namespace(
        data_dir=str(tmp_path), probes=str(probes),
        emit_artifact=None, seed=42,
    )


async def _calib_rc_with_wait_result(
    tmp_path: Path, monkeypatch, capsys, calib_mod, wait_result: str,
) -> tuple[int, str]:
    eng = _make_stub_engine(tmp_path)
    await eng.startup()
    await eng.shutdown()
    monkeypatch.setattr(calib_mod, "build_engine", lambda _config: eng)

    async def _stub_wait(_engine, timeout=None, poll_interval=None):
        return wait_result

    monkeypatch.setattr(calib_mod, "wait_for_bm25_ready", _stub_wait)
    rc = await calib_mod._run(_calib_args(tmp_path))
    return rc, capsys.readouterr().out


async def test_calibrate_aborts_when_bm25_not_ready(
    tmp_path, monkeypatch, capsys, calib_mod,
):
    """bm25 軸なしの較正は無効 — exit 1 (v3 VOID 再発防止の契約)。"""
    rc, out = await _calib_rc_with_wait_result(
        tmp_path, monkeypatch, capsys, calib_mod, "timeout",
    )
    assert rc == 1
    assert "ERROR" in out
    assert "axis is unavailable" in out
    assert "invalid" in out


async def test_calibrate_aborts_when_gate_index_unwired(
    tmp_path, monkeypatch, capsys, calib_mod,
):
    """build が ready でも ambient gate index 未配線なら同じ理由で exit 1
    (bm25-sudachi extra 欠落時に n/a probe を量産しない)。"""
    rc, out = await _calib_rc_with_wait_result(
        tmp_path, monkeypatch, capsys, calib_mod, "ready",
    )
    assert rc == 1
    assert "ERROR" in out
    assert "ambient gate index is not wired" in out
