"""Unit tests for ``scripts/diag_target_trace.py`` (Phase U WP-4 / R4).

Covers the pure helpers (rank search / seed-pool sizing mirror / pool-drop
diagnosis), the CLI wiring, and one fast integration happy-path on a tiny
StubEmbedder corpus: the script's ``--json`` output parses and reports the
target found in the raw pool with a qualification verdict.

The heavy paths (real RURI, production copy) are exercised manually by the
PM per Plans-Phase-U-Review-Hardening §4 WP-4 — not here.
"""
from __future__ import annotations

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


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "diag_target_trace", SCRIPT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    # dataclass の文字列 annotation 解決は sys.modules 登録を要求する
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


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
