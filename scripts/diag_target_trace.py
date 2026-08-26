#!/usr/bin/env python
"""目標 node ID の rank + qualification trace (Phase U WP-4 / R4)。

1 個の query と 1 個の target node ID を与えると、candidate generation から
最終 top-K までの各段階でその node がどこにいるか / どこで落ちるかを
一括表示する read-only 診断 script。

表示内容:

  a. raw FAISS rank        — 埋め込み query に対する生 index の rank + score
  b. virtual FAISS rank    — displacement 反映済み virtual index の rank + score
  c. hybrid BM25 rank      — Phase L hybrid retrieval (char 3-gram) の rank + score
  d. ambient word-BM25 rank — ambient gate 用 word (Sudachi) BM25 の rank + score
  e. qualification verdict — engine と同じ純関数 (gaottt.core.scorer の
                             is_direct_qualified / qualification_confidence) で
                             3 軸 (raw cos / normalized vcos / lexical rel+abs) を判定
  f. final rank            — passive な engine.query 1 回 (use_cache=False =
                             prefetch cache bypass = force_refresh 相当)。
                             target の最終 rank と ScoreBreakdown
  g. pool diagnosis        — target が最終結果に現れない場合、
                             raw pool / virtual pool / bm25 pool / fused seed pool /
                             wave reach のどの段階で落ちたかを報告
                             (engine の WP-5 provenance mirror と同じ pool sizing で
                             gravity._union_pool / propagate_gravity_wave を直接再実行)

read-only 契約:
  * active query は一切投げない (engine.query は passive=True 、
    mass / displacement / return_count / 共起 edge の更新を全スキップ)。
  * write-behind loop は全停止 (scripts/calibrate_ambient_gate.py と同一 pattern)。
  * 既知の例外は script 慣例どおり: manifest が無ければ生成される /
    startup の TTL 期限 scan は archive を書き得る / shutdown の最終 flush。
  そのため **本番 DB は必ず copy に対して使うこと** (sqlite3 .backup + FAISS /
  virtual FAISS / manifest の file copy)。copy は診断専用とし restore source
  にしない (sidecar との数秒 race は許容 — Plans-Phase-U §5)。

Usage::

    .venv/bin/python scripts/diag_target_trace.py \\
        --data-dir /tmp/gaottt-prod-copy \\
        --query "recall explore ambient_recall が全件空返し FAISS 埋め込みパイプライン 沈黙エラー" \\
        --target-id de1b528f-f95a-46e8-a28d-7a4fbd580806 \\
        [--top-n 20] [--json]

exit code: 診断成功なら 0 (target が結果外でも診断としては成功)。
target ID が store に存在しない等の致命的失敗のみ 1。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

# Allow direct invocation (bootstrap_report.py pattern).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np  # noqa: E402

from gaottt.config import GaOTTTConfig  # noqa: E402
from gaottt.core.engine import GaOTTTEngine  # noqa: E402
from gaottt.core.gravity import (  # noqa: E402
    _multi_source_pool,
    _union_pool,
    compute_virtual_position,
    propagate_gravity_wave,
)
from gaottt.core.persona_gravity import (  # noqa: E402
    collect_active_persona_ids,
    compute_persona_proximities,
)
from gaottt.core.scorer import (  # noqa: E402
    compute_lexical_strength,
    is_direct_qualified,
    qualification_confidence,
)
from gaottt.core.segmentation import segment_query  # noqa: E402
from gaottt.services.runtime import build_engine  # noqa: E402


# ---------------------------------------------------------------------------
# 純 helper (unit test 対象)
# ---------------------------------------------------------------------------


@dataclass
class RankHit:
    """target の (1-based rank, その index での score)。"""

    rank: int
    score: float


def find_rank(
    hits: Sequence[tuple[str, float]], target_id: str,
) -> RankHit | None:
    """score 降順 ``hits`` 列から target を探し 1-based rank を返す。

    ``hits`` は FaissIndex.search / BM25Index.search の返り値
    (``(id, score)`` の list、降順)。不在なら None。
    """
    for i, (nid, score) in enumerate(hits):
        if nid == target_id:
            return RankHit(rank=i + 1, score=float(score))
    return None


def mirror_seed_pool_size(
    config: GaOTTTConfig,
    *,
    wave_k: int | None = None,
    persona_proximities_present: bool = False,
) -> int:
    """engine.query の WP-5 provenance mirror と同一の seed pool sizing。

    propagate_gravity_wave の seed step が引く pool size の再現
    (gaottt/core/engine.py の prov_pool_n 計算と同じ分岐)。本 script は
    source_filter / injected_ids無しの plain query だけを想定しているため
    その 2 分岐は省略。
    """
    initial_k = wave_k if wave_k is not None else config.wave_initial_k
    has_boost = (
        config.wave_seed_mass_alpha > 0.0
        or (persona_proximities_present and config.persona_boost_alpha > 0.0)
    )
    if has_boost:
        return max(initial_k, config.wave_seed_pool_size)
    return initial_k


@dataclass
class PoolDiagnosis:
    """(g) pool diagnosis — 候補生成各段階の target membership。

    ``*_rank`` は各 leg の top-``pool_size`` 内での 1-based rank。
    ``None`` は「その leg に target がいない」または「leg 自体が unavailable」
    (``*_available=False``) のどちらか — missed/unavailable で区別する。
    """

    pool_size: int
    raw_rank: int | None
    virtual_rank: int | None
    bm25_rank: int | None
    fused_rank: int | None
    raw_available: bool
    virtual_available: bool
    bm25_available: bool
    in_wave_reached: bool
    wave_force: float | None

    @property
    def in_raw_pool(self) -> bool:
        return self.raw_rank is not None

    @property
    def in_virtual_pool(self) -> bool:
        return self.virtual_rank is not None

    @property
    def in_bm25_pool(self) -> bool:
        return self.bm25_rank is not None

    @property
    def in_fused_pool(self) -> bool:
        return self.fused_rank is not None

    @property
    def missed_sources(self) -> list[str]:
        """「index は有効だが top-N に target がいなかった」段階のリスト。"""
        missed: list[str] = []
        if self.raw_available and self.raw_rank is None:
            missed.append("raw_pool")
        if self.virtual_available and self.virtual_rank is None:
            missed.append("virtual_pool")
        if self.bm25_available and self.bm25_rank is None:
            missed.append("bm25_pool")
        if self.fused_rank is None:
            missed.append("fused_seed_pool")
        if not self.in_wave_reached:
            missed.append("wave_reach")
        return missed

    @property
    def unavailable_sources(self) -> list[str]:
        """「leg 自体が無効 (index 未構築 / tokenizer extra 未導入)」のリスト。"""
        unavailable: list[str] = []
        if not self.raw_available:
            unavailable.append("raw_pool")
        if not self.virtual_available:
            unavailable.append("virtual_pool")
        if not self.bm25_available:
            unavailable.append("bm25_pool")
        return unavailable

    def to_dict(self) -> dict:
        return {
            "pool_size": self.pool_size,
            "raw_pool": _rank_json(self.raw_rank, self.raw_available),
            "virtual_pool": _rank_json(self.virtual_rank, self.virtual_available),
            "bm25_pool": _rank_json(self.bm25_rank, self.bm25_available),
            "fused_seed_pool": (
                {"in_pool": True, "rank": self.fused_rank}
                if self.fused_rank is not None
                else {"in_pool": False, "rank": None}
            ),
            "wave_reach": {
                "reached": self.in_wave_reached,
                "force": self.wave_force,
            },
            "missed_sources": self.missed_sources,
            "unavailable_sources": self.unavailable_sources,
        }


def _rank_json(rank: int | None, available: bool) -> dict:
    return {
        "available": available,
        "in_pool": rank is not None,
        "rank": rank,
    }


def diagnose_pool_drop(
    target_id: str,
    raw_hits: Sequence[tuple[str, float]] | None,
    virtual_hits: Sequence[tuple[str, float]] | None,
    bm25_hits: Sequence[tuple[str, float]] | None,
    fused_pool: Sequence[tuple[str, float]],
    wave_reached: dict[str, float],
    pool_size: int,
) -> PoolDiagnosis:
    """候補生成の各 leg への membership を一括判定。

    各 ``*_hits`` は ``None`` = leg unavailable (index 無効) / list = 有効
    (空 list も「検索したが 0 件」の有効結果)。
    """
    raw_hit = find_rank(raw_hits, target_id) if raw_hits is not None else None
    virtual_hit = (
        find_rank(virtual_hits, target_id) if virtual_hits is not None else None
    )
    bm25_hit = (
        find_rank(bm25_hits, target_id) if bm25_hits is not None else None
    )
    fused_hit = find_rank(fused_pool, target_id)
    return PoolDiagnosis(
        pool_size=pool_size,
        raw_rank=raw_hit.rank if raw_hit is not None else None,
        virtual_rank=virtual_hit.rank if virtual_hit is not None else None,
        bm25_rank=bm25_hit.rank if bm25_hit is not None else None,
        fused_rank=fused_hit.rank if fused_hit is not None else None,
        raw_available=raw_hits is not None,
        virtual_available=virtual_hits is not None,
        bm25_available=bm25_hits is not None,
        in_wave_reached=target_id in wave_reached,
        wave_force=wave_reached.get(target_id),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Per-index rank + qualification trace for one target node ID "
            "(read-only; run against a COPY of the production DB)."
        ),
    )
    parser.add_argument(
        "--data-dir", required=True,
        help="data dir of the production COPY (never the live DB)",
    )
    parser.add_argument(
        "--query", required=True,
        help="query text to trace",
    )
    parser.add_argument(
        "--target-id", required=True, dest="target_id",
        help="target node UUID to trace",
    )
    parser.add_argument(
        "--top-n", type=int, default=20, dest="top_n",
        help="final top-K window for the passive query (default: 20); "
             "per-index rank windows are top_n*5",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="machine-readable JSON output (stdout contains ONLY the JSON)",
    )
    return parser


# ---------------------------------------------------------------------------
# trace 本体
# ---------------------------------------------------------------------------


async def _run(args: argparse.Namespace) -> int:
    notes: list[str] = []
    config = GaOTTTConfig(data_dir=str(args.data_dir))
    # diag_recall.py / calibrate_ambient_gate.py と同じ read-only 配慮 —
    # write-behind loop を全て止める (manifest 生成と shutdown flush は
    # 確立された script 例外)。
    config.faiss_save_interval_seconds = 0.0
    config.virtual_faiss_save_interval_seconds = 0.0
    config.dream_enabled = False

    engine = build_engine(config)
    await engine.startup()
    try:
        return await _trace(engine, args, notes)
    finally:
        await engine.shutdown()


async def _trace(
    engine: GaOTTTEngine, args: argparse.Namespace, notes: list[str],
) -> int:
    target_id: str = args.target_id
    query: str = args.query
    top_n: int = args.top_n
    cfg = engine.config

    # -- node 存在チェック + ranking-relevant state -------------------------
    doc = await engine.store.get_document(target_id)
    if doc is None:
        print(
            f"ERROR: target node {target_id} not found in {cfg.db_path} — "
            "check the ID or the data-dir.",
            file=sys.stderr,
        )
        return 1

    state = engine.cache.get_node(target_id)
    if state is None:
        states = await engine.store.get_node_states([target_id])
        state = states.get(target_id)
    displacement = engine.cache.get_displacement(target_id)
    metadata = doc.get("metadata") or {}
    content_preview = (doc.get("content") or "")[:200]

    mass = state.mass if state else None
    return_count = state.return_count if state else None
    certainty = state.certainty if state else None
    temperature = state.temperature if state else None
    is_archived = state.is_archived if state else None
    expires_at = state.expires_at if state else None
    saturation = (
        1.0 / (1.0 + state.return_count * cfg.saturation_rate)
        if state else None
    )

    node_section = {
        "found": True,
        "source": metadata.get("source"),
        "tags": metadata.get("tags") or [],
        "mass": mass,
        "return_count": return_count,
        "certainty": certainty,
        "temperature": temperature,
        "is_archived": is_archived,
        "expires_at": expires_at,
        "saturation_factor": saturation,
        "cohort_id": engine.cache.get_cohort(target_id),
        "original_id": engine.cache.get_original(target_id),
        "content_preview": content_preview,
    }
    if state is None:
        notes.append("node state unavailable in cache/store")
    if is_archived:
        notes.append("node is ARCHIVED — scoring loop excludes it")
    if expires_at is not None and expires_at <= time.time():
        notes.append("node TTL expired — scoring loop excludes it")

    # -- query embedding -----------------------------------------------------
    query_vec = engine.embedder.encode_query(query)
    qv = query_vec[0] if query_vec.ndim == 2 else query_vec
    q_norm = float(np.linalg.norm(qv)) + 1e-12

    # (a)-(d) per-index rank (window = top_n * 5)
    window = max(top_n * 5, 1)
    raw_hits = (
        engine.faiss_index.search(query_vec, window)
        if engine.faiss_index.size > 0 else None
    )
    virtual_index_alive = (
        engine.virtual_faiss_index is not None
        and engine.virtual_faiss_index.size > 0
    )
    virtual_hits = (
        engine.virtual_faiss_index.search(query_vec, window)
        if virtual_index_alive else None
    )
    hybrid_index_alive = (
        engine.bm25_index is not None and engine.bm25_index.size > 0
    )
    hybrid_hits = (
        engine.bm25_index.search(query, window)
        if hybrid_index_alive and query else None
    )
    ambient_index_alive = (
        engine.ambient_gate_index is not None
        and engine.ambient_gate_index.size > 0
    )
    ambient_hits = (
        engine.ambient_gate_index.search(query, window)
        if ambient_index_alive and query else None
    )
    if engine.ambient_gate_index is None:
        notes.append(
            "ambient gate index unavailable (bm25-sudachi extra 未導入 or "
            "ambient_gate_use_bm25=False) — ambient word-BM25 rank is n/a"
        )

    index_ranks = {
        "window": window,
        "raw_faiss": _rank_section(raw_hits, target_id, engine.faiss_index.size),
        "virtual_faiss": _rank_section(
            virtual_hits, target_id,
            engine.virtual_faiss_index.size if virtual_index_alive else 0,
        ),
        "hybrid_bm25": _rank_section(
            hybrid_hits, target_id,
            engine.bm25_index.size if hybrid_index_alive else 0,
        ),
        "ambient_gate_bm25": _rank_section(
            ambient_hits, target_id,
            engine.ambient_gate_index.size if ambient_index_alive else 0,
        ),
    }

    # -- (e) qualification verdict (engine と同じ純関数で再計算) -------------
    qualification_active = (
        cfg.direct_qualification_enabled or cfg.ttt_qualification_enabled
    )
    # lexical 軸の pool — engine の query path と同じ sizing:
    # qualification が有効なら direct_bm25_pool_size、無効 (legacy) なら
    # 幅広 pool (engine は max(len(reached), 50) — reached 数は query 前に
    # 不明なので window で近似し note)。
    if qualification_active:
        lexical_pool_n = cfg.direct_bm25_pool_size
    else:
        lexical_pool_n = max(window, 50)
        notes.append(
            "both qualification flags OFF — lexical pool sized by window "
            f"({lexical_pool_n}), engine uses max(len(reached), 50)"
        )
    bm25_pool_scores: dict[str, float] = {}
    bm25_pool_top = 0.0
    if hybrid_index_alive and query:
        bm25_pool_scores = dict(engine.bm25_index.search(query, lexical_pool_n))
        if bm25_pool_scores:
            bm25_pool_top = max(bm25_pool_scores.values())

    emb_map = engine.faiss_index.get_vectors([target_id])
    original_emb = emb_map.get(target_id)
    raw_cos: float | None = None
    vcos_norm: float | None = None
    temperature_noise = False
    if original_emb is not None:
        emb_norm = float(np.linalg.norm(original_emb)) + 1e-12
        raw_cos = float(np.dot(qv, original_emb)) / (q_norm * emb_norm)
        virtual_pos = compute_virtual_position(
            original_emb, displacement,
            state.temperature if state else 0.0,
        )
        vp_norm = float(np.linalg.norm(virtual_pos))
        vcos_norm = (
            float(np.dot(qv, virtual_pos)) / (q_norm * vp_norm)
            if vp_norm > 0.0 else 0.0
        )
        if (state.temperature if state else 0.0) > 0.001:
            temperature_noise = True
            notes.append(
                "temperature > 0.001 — vcos_norm is one thermal-noise draw "
                "(production behavior; repeated runs differ slightly)"
            )
    else:
        notes.append(
            "target vector MISSING from raw FAISS (orphan node?) — raw/vcos "
            "axes computed as 0.0"
        )

    bm25_sc = bm25_pool_scores.get(target_id, 0.0)
    lexical = compute_lexical_strength(bm25_sc, bm25_pool_top)
    # FAISS vector 欠落時は軸値 0.0 で判定 (engine も同様に cosine を得られ
    # ない node は scored にならないため、ここでの verdict は参考値)。
    qualified = is_direct_qualified(
        raw_cos if raw_cos is not None else 0.0,
        vcos_norm if vcos_norm is not None else 0.0,
        bm25_sc, lexical,
        cfg.direct_raw_cosine_min,
        cfg.direct_virtual_cosine_min,
        cfg.direct_bm25_absolute_min,
        cfg.direct_bm25_relative_min,
    )
    confidence = qualification_confidence(
        raw_cos if raw_cos is not None else 0.0,
        vcos_norm if vcos_norm is not None else 0.0,
        bm25_sc, lexical,
        cfg.direct_raw_cosine_min,
        cfg.direct_virtual_cosine_min,
        cfg.direct_bm25_absolute_min,
        cfg.direct_bm25_relative_min,
    )

    qualification_section = {
        "flags": {
            "direct_qualification_enabled": cfg.direct_qualification_enabled,
            "ttt_qualification_enabled": cfg.ttt_qualification_enabled,
        },
        "lexical_pool_size": lexical_pool_n,
        "axes": {
            "raw_cos": raw_cos,
            "virtual_cos_norm": vcos_norm,
            "bm25_score": bm25_sc,
            "bm25_pool_top": bm25_pool_top,
            "lexical_strength": lexical,
        },
        "thresholds": {
            "raw_min": cfg.direct_raw_cosine_min,
            "virtual_min": cfg.direct_virtual_cosine_min,
            "bm25_absolute_min": cfg.direct_bm25_absolute_min,
            "bm25_relative_min": cfg.direct_bm25_relative_min,
        },
        "axis_pass": {
            "raw": raw_cos is not None and raw_cos >= cfg.direct_raw_cosine_min,
            "virtual": (
                vcos_norm is not None
                and vcos_norm >= cfg.direct_virtual_cosine_min
            ),
            "lexical": (
                bm25_sc >= cfg.direct_bm25_absolute_min
                and lexical >= cfg.direct_bm25_relative_min
            ),
        },
        "qualified": qualified,
        "confidence": confidence,
        "temperature_noise": temperature_noise,
    }

    # -- (f) passive query 1 回 (use_cache=False = force_refresh 相当) --------
    if not cfg.expose_score_breakdown:
        cfg.expose_score_breakdown = True
        notes.append(
            "config.expose_score_breakdown was False — script forced True "
            "(diagnostic override on this copy)"
        )
    wave_stats: dict = {}
    results = await engine.query(
        text=query, top_k=top_n, passive=True, use_cache=False,
        out_wave_stats=wave_stats,
    )
    target_result = next((r for r in results if r.id == target_id), None)
    target_rank = (
        results.index(target_result) + 1 if target_result is not None else None
    )
    final_section = {
        "top_k": top_n,
        "passive": True,
        "use_cache": False,
        "wave": {
            "depth": wave_stats.get("depth"),
            "reached": wave_stats.get("reached"),
        },
        "n_results": len(results),
        "target_in_results": target_result is not None,
        "target_rank": target_rank,
        "final_score": (
            float(target_result.final_score) if target_result else None
        ),
        "breakdown": (
            target_result.score_breakdown.model_dump()
            if target_result is not None and target_result.score_breakdown
            else None
        ),
    }

    # -- (g) pool diagnosis — engine の provenance mirror と同じ再現 ----------
    # (engine._query_internal と同じ引数作り: persona proximity / multi-source
    #  segment vectors を自前計算し、read-only な純関数を直接呼ぶ)
    persona_proximities: dict[str, float] | None = None
    if cfg.persona_boost_enabled and cfg.persona_boost_alpha > 0.0:
        persona_ids = collect_active_persona_ids(engine.cache, cfg, time.time())
        if persona_ids:
            persona_proximities = compute_persona_proximities(
                persona_ids, engine.cache, cfg,
            )

    segment_vectors: np.ndarray | None = None
    n_segments = 1
    if cfg.multi_source_enabled:
        segments = segment_query(query, cfg)
        n_segments = len(segments)
        if len(segments) > 1:
            encode_many = getattr(engine.embedder, "encode_queries", None)
            if encode_many is not None:
                segment_vectors = encode_many(segments)
            else:
                segment_vectors = np.vstack(
                    [engine.embedder.encode_query(s) for s in segments]
                )

    pool_n = mirror_seed_pool_size(
        cfg, persona_proximities_present=persona_proximities is not None,
    )
    bm25_effective = engine.bm25_index if cfg.hybrid_bm25_enabled else None
    if segment_vectors is not None:
        fused_pool = _multi_source_pool(
            segment_vectors, engine.faiss_index, engine.virtual_faiss_index,
            pool_n, query_text=query, bm25_index=bm25_effective,
            rrf_k=cfg.rrf_k,
        )
    else:
        fused_pool = _union_pool(
            qv, engine.faiss_index, engine.virtual_faiss_index, pool_n,
            query_text=query, bm25_index=bm25_effective,
            bm25_score_mode=cfg.bm25_score_mode,
            bm25_score_alpha=cfg.bm25_score_alpha,
            rrf_k=cfg.rrf_k,
        )

    # 各 leg (fused pool 構築時に _union_pool が内部的に引くのと同じ検索) —
    # multi-source 時は centroid query 近似 (informational)。
    raw_leg = (
        engine.faiss_index.search(query_vec, pool_n)
        if engine.faiss_index.size > 0 else None
    )
    virtual_leg = (
        engine.virtual_faiss_index.search(query_vec, pool_n)
        if virtual_index_alive else None
    )
    bm25_leg = (
        bm25_effective.search(query, pool_n)
        if bm25_effective is not None and bm25_effective.size > 0 and query
        else None
    )
    if segment_vectors is not None:
        notes.append(
            f"multi-source query ({n_segments} segments) — per-leg membership "
            "is measured against the centroid query (approximation)"
        )

    reached = propagate_gravity_wave(
        query_vec, engine.faiss_index, engine.cache, cfg,
        source_filter=None,
        virtual_faiss_index=engine.virtual_faiss_index,
        persona_proximities=persona_proximities,
        injected_ids=None,
        query_text=query,
        bm25_index=engine.bm25_index,
        segment_vectors=segment_vectors,
    )
    diagnosis = diagnose_pool_drop(
        target_id, raw_leg, virtual_leg, bm25_leg, fused_pool, reached, pool_n,
    )

    if (
        target_result is None
        and diagnosis.in_wave_reached
        and original_emb is not None
    ):
        # wave には到達したのに最終結果にいない → scoring loop で落ちた。
        # 考え得る原因の値を hint として出す (再計算ではないので近似)。
        hints = {
            "wave_force": reached.get(target_id),
            "saturation_factor": saturation,
            "mass": mass,
            "note": (
                "in wave reach but not in results — dropped in the scoring "
                "loop (archived / expired / missing vector / final_score<=0) "
                "or cut by the top-K window"
            ),
        }
        diagnosis_notes = hints
    else:
        diagnosis_notes = None

    pool_section = diagnosis.to_dict()
    pool_section["dropout_hints"] = diagnosis_notes

    # -- 出力 -----------------------------------------------------------------
    payload = {
        "data_dir": args.data_dir,
        "query": query,
        "target_id": target_id,
        "top_n": top_n,
        "node": node_section,
        "index_ranks": index_ranks,
        "qualification": qualification_section,
        "final_query": final_section,
        "pool_diagnosis": pool_section,
        "notes": notes,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    _print_human(payload)
    return 0


def _rank_section(
    hits: Sequence[tuple[str, float]] | None,
    target_id: str,
    index_size: int,
) -> dict:
    """window rank 共通 shape。``hits=None`` = leg unavailable
    (空 list は「検索したが 0 件」の有効結果)。"""
    hit = find_rank(hits, target_id) if hits is not None else None
    return {
        "available": hits is not None,
        "index_size": index_size,
        "in_pool": hit is not None,
        "rank": hit.rank if hit else None,
        "score": hit.score if hit else None,
    }


def _fmt(v: float | None, width: int = 7, prec: int = 4) -> str:
    return f"{v:{width}.{prec}f}" if isinstance(v, (int, float)) else "n/a"


def _mark(flag: bool | None) -> str:
    if flag is None:
        return "-"
    return "○" if flag else "×"


def _print_human(p: dict) -> None:
    node = p["node"]
    ranks = p["index_ranks"]
    qual = p["qualification"]
    final = p["final_query"]
    pool = p["pool_diagnosis"]

    print("== target-ID trace ==")
    print(f"data-dir : {p['data_dir']}")
    print(f"query    : {p['query']}")
    print(f"target   : {p['target_id']}")
    print(f"top-n    : {p['top_n']}")

    print("\n-- node state (ranking-relevant) --")
    print(
        f"source={node['source']} tags={node['tags']} "
        f"mass={_fmt(node['mass'], 7, 3)} "
        f"return_count={_fmt(node['return_count'], 7, 3)} "
        f"certainty={_fmt(node['certainty'], 7, 3)}"
    )
    print(
        f"temperature={_fmt(node['temperature'], 7, 4)} "
        f"saturation={_fmt(node['saturation_factor'], 7, 4)} "
        f"archived={node['is_archived']}"
    )
    print(
        f"cohort_id={node['cohort_id'] or '-'} "
        f"original_id={node['original_id'] or '-'}"
    )
    print(f"content[:200]: {node['content_preview']}")

    print(f"\n-- per-index rank (window={ranks['window']}) --")
    for key, label in (
        ("raw_faiss", "raw FAISS        "),
        ("virtual_faiss", "virtual FAISS    "),
        ("hybrid_bm25", "hybrid BM25      "),
        ("ambient_gate_bm25", "ambient word-BM25"),
    ):
        sec = ranks[key]
        if not sec["available"]:
            print(f"  {label} : (unavailable)")
        elif sec["in_pool"]:
            print(
                f"  {label} : rank={sec['rank']:>6}  "
                f"score={_fmt(sec['score'])}  (index size={sec['index_size']})"
            )
        else:
            print(
                f"  {label} : rank=   out  "
                f"(not in top-{ranks['window']}, index size={sec['index_size']})"
            )

    print("\n-- qualification verdict (Phase T Stage 3/4) --")
    print(
        f"flags: direct={qual['flags']['direct_qualification_enabled']} "
        f"ttt={qual['flags']['ttt_qualification_enabled']}"
    )
    thr = qual["thresholds"]
    ax = qual["axes"]
    print(
        f"  raw_cos   ={_fmt(ax['raw_cos'])} (min {thr['raw_min']}) "
        f"{_mark(qual['axis_pass']['raw'])}"
    )
    print(
        f"  vcos_norm ={_fmt(ax['virtual_cos_norm'])} (min {thr['virtual_min']}) "
        f"{_mark(qual['axis_pass']['virtual'])}"
    )
    print(
        f"  bm25      ={_fmt(ax['bm25_score'], 7, 2)} (abs min {thr['bm25_absolute_min']}) "
        f"/ lexical={_fmt(ax['lexical_strength'])} (rel min {thr['bm25_relative_min']}) "
        f"{_mark(qual['axis_pass']['lexical'])}"
    )
    print(
        f"  → qualified={qual['qualified']} "
        f"confidence={_fmt(qual['confidence'])}"
    )
    if qual["temperature_noise"]:
        print("  (temperature>0 — vcos_norm is one noise draw)")

    print(
        f"\n-- final passive query (top_k={final['top_k']}, "
        f"use_cache=False) --"
    )
    print(
        f"wave: depth={final['wave']['depth']} reached={final['wave']['reached']} "
        f"/ results={final['n_results']}"
    )
    if final["target_in_results"]:
        print(
            f"target in results: YES rank={final['target_rank']} "
            f"final_score={_fmt(final['final_score'])}"
        )
        bd = final["breakdown"]
        if bd:
            print(
                f"  breakdown: raw_cosine={_fmt(bd.get('raw_cosine'))} "
                f"virtual_cosine={_fmt(bd.get('virtual_cosine'))} "
                f"decay={_fmt(bd.get('decay_factor'))} "
                f"saturation={_fmt(bd.get('saturation'))}"
            )
            print(
                f"  qualified={bd.get('qualified')} "
                f"direct_score={bd.get('direct_score')} "
                f"field_score={bd.get('field_score')} "
                f"lensing_gap={_fmt(bd.get('lensing_gap'))}"
            )
            print(
                f"  cohort={bd.get('cohort')} "
                f"provenance={bd.get('provenance')} "
                f"in_learn_set={bd.get('in_learn_set')} "
                f"bm25_contributed={bd.get('bm25_contributed')}"
            )
    else:
        print("target in results: NO")

    print(f"\n-- candidate pool diagnosis (seed pool size={pool['pool_size']}) --")
    for key, label in (
        ("raw_pool", "raw top-N pool    "),
        ("virtual_pool", "virtual top-N pool"),
        ("bm25_pool", "bm25 top-N pool   "),
        ("fused_seed_pool", "fused seed pool   "),
    ):
        sec = pool[key]
        if key != "fused_seed_pool" and not sec["available"]:
            print(f"  {label}: (unavailable)")
        elif sec["in_pool"]:
            print(f"  {label}: IN  rank={sec['rank']}")
        else:
            print(f"  {label}: OUT")
    wr = pool["wave_reach"]
    if wr["reached"]:
        print(f"  wave reach       : IN  force={_fmt(wr['force'])}")
    else:
        print("  wave reach       : OUT")
    if pool["missed_sources"]:
        print(f"  missed at: {', '.join(pool['missed_sources'])}")
    if pool["unavailable_sources"]:
        print(f"  unavailable legs: {', '.join(pool['unavailable_sources'])}")
    if pool.get("dropout_hints"):
        print(f"  {pool['dropout_hints']['note']}")
        print(
            f"    wave_force={_fmt(pool['dropout_hints']['wave_force'])} "
            f"saturation={_fmt(pool['dropout_hints']['saturation_factor'])} "
            f"mass={_fmt(pool['dropout_hints']['mass'], 7, 3)}"
        )

    if p["notes"]:
        print("\n-- notes --")
        for note in p["notes"]:
            print(f"  * {note}")


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
