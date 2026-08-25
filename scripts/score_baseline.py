"""Phase T Stage 1 — retrieval-quality baseline (score_baseline).

golden corpus (``tests/perf/golden_corpus/``) 上に隔離 engine を build し、
Phase T Stage 2-6 (semantic half-life / direct qualification / TTT gate /
ambient OR gate / explore diversity) の before/after 比較と threshold 較正の
根拠となる観測 baseline を採る。本番 DB には一切触れない (``--data-dir`` は
必ず隔離 dir)。測定はすべて passive (read-only) — mass / displacement /
cooccurrence / return_count を変化させない。

採る指標 (docs/wiki/Plans-Phase-T-Semantic-Requalification.md §3 Stage 1):

  1. score 項別寄与率 — top-100 passive recall の各 result の
     ``ScoreBreakdown`` から semantic (virtual_cosine*decay_factor) / wave /
     mass / emotion / certainty の additive sum に占める割合 (mean/p50/p90)
     + decay_factor / saturation の分布
  2. qualification sweep — raw_cos (breakdown 値) / 正規化 virtual cosine
     (自前計算: ``dot(q, v)/(|q||v|)``) / lexical strength (BM25 top score
     に対する相対比) の threshold 別 qualified 率
  3. ambient gate diagnostic — ``ambient_recall`` の count / slot 構成と
     gate 内部値 (word-BM25 top score vs ``ambient_bm25_min_score``、
     passive pool の semantic max virtual/raw)。gate OFF 版も併記し
     「BM25 veto による空返し」と「slot 構成による空返し」を切り分ける
  4. recall vs explore Jaccard@5 — diversity 0.0 / 0.5 / 0.8 / 1.0

Usage::

    .venv/bin/python scripts/score_baseline.py
    .venv/bin/python scripts/score_baseline.py \\
        --out docs/notes/phase-t/score-baseline-before.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from gaottt.core.engine import GaOTTTEngine  # noqa: E402
from gaottt.core.gravity import compute_virtual_position  # noqa: E402
from gaottt.index.bm25_index import BM25Index  # noqa: E402
from gaottt.services import memory as memory_service  # noqa: E402
from tests.perf._helpers import make_engine  # noqa: E402

GOLDEN_DIR = _PROJECT_ROOT / "tests" / "perf" / "golden_corpus"
SYNTHETIC_PATH = GOLDEN_DIR / "synthetic_chunks.jsonl"
AMBIENT_CORPUS_PATH = GOLDEN_DIR / "ambient_corpus.jsonl"
AMBIENT_QUERIES_PATH = GOLDEN_DIR / "ambient_queries.json"
QUERIES_PATH = GOLDEN_DIR / "queries.json"

# Stage 3 較定用 sweep 範囲 (plan §3 の指定値)。
RAW_SWEEP = [round(0.30 + 0.05 * i, 2) for i in range(9)]     # 0.30..0.70
VCOS_SWEEP = [round(0.40 + 0.05 * i, 2) for i in range(9)]    # 0.40..0.80
REL_SWEEP = [round(0.20 + 0.10 * i, 2) for i in range(5)]     # 0.20..0.60
# 実装契約値 — config (direct_raw_cosine_min / direct_virtual_cosine_min /
# direct_bm25_relative_min) と同一。較正根拠は docs/notes/phase-t/
# score-baseline-before.json: raw_cos p50=0.764 / p90=0.820 の RURI 狭帯に
# 対し 0.75 で qualified 率 70.6% (config.py「閾値根拠」comment と同一出所)。
# artifact key (provisional_thresholds / or_qualified_provisional) との互換の
# ため変数名は PROVISIONAL のまま。
PROVISIONAL = {"raw": 0.75, "vcos": 0.75, "rel": 0.40}
# lexical 軸の absolute guard — scorer.is_direct_qualified と同じ二重条件
# (absolute + relative)。BM25 (char 3-gram) の on-topic top score は 14-58
# なので 8.0 は off-topic guard: relative-only (low-score pool の top 項目) は
# qualified に数えない (= config.direct_bm25_absolute_min)。
_BM25_ABS_MIN = 8.0

# additive score 項 (final = (これらの和) * saturation)。
ADDITIVE_TERMS = ("semantic", "wave", "mass", "emotion", "certainty")

# compute_virtual_position の thermal noise 用 seed。fresh passive corpus では
# temperature=0 で noise は乗らないが、Stage 2-6 後の再測定で displacement /
# temperature が乗った状態でも決定論を保つため固定 seed を使う。
_NOISE_SEED = 20260825

# metadata に記録する config snapshot の対象 field。
_CONFIG_FIELDS = (
    "alpha", "delta", "gamma", "saturation_rate",
    "wave_boost_weight", "wave_initial_k", "wave_max_depth",
    "emotion_alpha", "certainty_alpha", "certainty_half_life_seconds",
    "hybrid_bm25_enabled", "bm25_k1", "bm25_b", "bm25_tokenizer",
    "virtual_faiss_enabled", "expose_score_breakdown",
    "ambient_gate_use_bm25", "ambient_gate_tokenizer",
    "ambient_bm25_min_score", "ambient_min_score",
    "ambient_novelty_decay", "ambient_conversational_source_factor",
    "ambient_dump_symbol_ratio",
    "ambient_lensing_enabled", "ambient_lensing_min_score",
    "ambient_lensing_min_gap", "ambient_lensing_max_k",
    "ambient_lensing_resonance_min", "ambient_lensing_resonance_scale",
    "ambient_dormant_slot_enabled", "ambient_dormant_slot_count",
    "ambient_dormant_relevance_floor",
    "dormant_mass_threshold", "dormant_mass_percentile",
    "dormant_age_threshold_seconds",
    "ambient_persona_enabled", "ambient_persona_min_relevance",
    "ambient_persona_pool_size", "ambient_persona_mass_weight",
    "direct_hit_anti_hub_lambda",
    "semantic_halflife_enabled", "semantic_half_life_seconds",
    "semantic_floor",
    "genesis_kick_enabled", "supernova_enabled", "dream_enabled",
    "mass_conservation_enabled", "mass_bh_enabled", "persona_boost_enabled",
    "multi_source_enabled", "multi_source_ambient_enabled",
)

DIVERSITIES = (0.0, 0.5, 0.8, 1.0)

# 指標 1+2 の測定 call に渡す wave_k。perf helper の wave_initial_k=3 は
# latency test 用の tight 設定で、そのままだと top_k=100 を要求しても
# seed pool が ~4 件に絞られ qualification sweep が較正にならない
# (distractor が pool に入らないため全 threshold で 1.000 になる)。
# wave_k は到達性の public knob (CLAUDE.md「sparse class は wave_k で明示」)
# なので、golden corpus 全件 (42) を超える 50 を指定して top-100 を実名化
# する。ambient 診断の mirror pool と Jaccard 測定は service 既定のまま
# (production 挙動の faithful 測定)。
_MEASURE_WAVE_K = 50


def _r(x) -> float:
    """JSON artifact 用に float 化 + 6 桁丸め (np.float32 除去も兼ねる)。"""
    return round(float(x), 6)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = int(round((pct / 100.0) * (len(s) - 1)))
    return s[k]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _share_stats(values: list[float]) -> dict:
    """寄与率の集計形 (mean / p50 / p90)。"""
    return {
        "mean": _r(_mean(values)),
        "p50": _r(_percentile(values, 50)),
        "p90": _r(_percentile(values, 90)),
    }


def _dist_stats(values: list[float]) -> dict:
    """較正用の分布形 (min / p10 / p50 / p90 / max + n)。"""
    return {
        "n": len(values),
        "min": _r(_percentile(values, 0)),
        "p10": _r(_percentile(values, 10)),
        "p50": _r(_percentile(values, 50)),
        "p90": _r(_percentile(values, 90)),
        "max": _r(_percentile(values, 100)),
    }


def _sweep_rates(values: list[float], thresholds: list[float]) -> dict[str, float]:
    n = len(values)
    return {
        f"{t:.2f}": _r(sum(1 for v in values if v >= t) / n) if n else 0.0
        for t in thresholds
    }


def _rate_at(values: list[float], threshold: float) -> float:
    """指定 threshold での qualified 率 (sweep 範囲外の契約値でも集計できる
    よう _sweep_rates とは独立の単点版)。"""
    if not values:
        return 0.0
    return _r(sum(1 for v in values if v >= threshold) / len(values))


def _or_qualified_rate(
    raws: list[float], vcoss: list[float], rels: list[float],
    bm25_abs: list[float],
) -> float:
    """scorer.is_direct_qualified と同じ OR 集計。lexical 軸は absolute
    (>= _BM25_ABS_MIN) + relative (>= PROVISIONAL["rel"]) の二重条件 —
    relative-only は qualified に数えない。"""
    n = len(raws)
    if not n:
        return 0.0
    hits = sum(
        1 for raw, vc, rel, ab in zip(raws, vcoss, rels, bm25_abs)
        if (raw >= PROVISIONAL["raw"] or vc >= PROVISIONAL["vcos"]
            or (rel >= PROVISIONAL["rel"] and ab >= _BM25_ABS_MIN))
    )
    return _r(hits / n)


def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_PROJECT_ROOT, stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return "nogit"


def _load_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _load_queries(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


async def _ingest(engine: GaOTTTEngine) -> tuple[dict, dict[str, str]]:
    """golden corpus 2 file を ingest し、file 別 count と fixture→engine id map を返す。

    metadata key は tier7 (``golden_fixture_id``) / tier3 (``ambient_fixture_id``)
    の作法を踏襲。supernova は make_config 既定で無効なので batch 境界に
    物理的な差はなく、file 単位の 2 回呼び出しは map 構築の都合のみ。
    """
    summary: dict = {}
    fixture_to_engine: dict[str, str] = {}
    for filename, meta_key in (
        (SYNTHETIC_PATH, "golden_fixture_id"),
        (AMBIENT_CORPUS_PATH, "ambient_fixture_id"),
    ):
        records = _load_jsonl(filename)
        docs = [
            {
                "content": r["content"],
                "metadata": {
                    "source": r.get("source", "synthetic"),
                    "tags": r.get("tags", []),
                    meta_key: r["id"],
                },
            }
            for r in records
        ]
        engine_ids = await engine.index_documents(docs)
        fixture_to_engine.update(zip((r["id"] for r in records), engine_ids))
        summary[filename.name] = len(records)
    return summary, fixture_to_engine


def _term_values(bd) -> dict[str, float]:
    """breakdown から additive 項の値を取り出す (semantic は掛け算済み)。"""
    return {
        "semantic": float(bd.virtual_cosine) * float(bd.decay_factor),
        "wave": float(bd.wave_score),
        "mass": float(bd.mass_boost),
        "emotion": float(bd.emotion_term),
        "certainty": float(bd.certainty_term),
    }


def _collect_contributions(
    query: str, results, fixture_of: dict[str, str], age_seconds: float,
) -> tuple[dict, dict[str, list]]:
    """指標 1 — top-100 の score 項別寄与率 + decay / saturation 分布。

    戻り値: (JSON 用 per-query dict, pooled 集計用の生値リスト)。
    """
    shares: dict[str, list[float]] = {t: [] for t in ADDITIVE_TERMS}
    decay_vals: list[float] = []
    saturation_vals: list[float] = []
    skipped_zero_sum = 0
    top5: list[dict] = []
    for rank, r in enumerate(results[:5], 1):
        tags = (r.metadata or {}).get("tags", [])
        top5.append({
            "rank": rank,
            "fixture_id": fixture_of.get(r.id, "?"),
            "tags": tags,
            "final_score": _r(r.final_score),
        })
    for r in results:
        bd = r.score_breakdown
        if bd is None:
            continue
        terms = _term_values(bd)
        additive_sum = sum(terms.values())
        if additive_sum > 1e-12:
            for t in ADDITIVE_TERMS:
                shares[t].append(terms[t] / additive_sum)
        else:
            skipped_zero_sum += 1
        decay_vals.append(float(bd.decay_factor))
        saturation_vals.append(float(bd.saturation))
    per_query = {
        "query": query,
        "n_results": len(results),
        "age_seconds_at_query": _r(age_seconds),
        "shares": {t: _share_stats(v) for t, v in shares.items()},
        "decay_factor": {
            "min": _r(_percentile(decay_vals, 0)),
            "median": _r(_percentile(decay_vals, 50)),
            "max": _r(_percentile(decay_vals, 100)),
        },
        "saturation": _share_stats(saturation_vals),
        "skipped_zero_additive_sum": skipped_zero_sum,
        "top5": top5,
    }
    pooled = {
        **{f"share_{t}": v for t, v in shares.items()},
        "decay": decay_vals,
        "saturation": saturation_vals,
    }
    return per_query, pooled


def _normalized_virtual_cosines(
    engine: GaOTTTEngine, query: str, ids: list[str],
) -> tuple[list[float], int]:
    """正規化 virtual cosine を自前計算する (plan §3 共通規約の定義)。

    breakdown の ``virtual_cosine`` は非正規化 dot 契約なので使わない。
    ``faiss_index.get_vectors`` (raw) + ``cache.get_displacement`` +
    ``compute_virtual_position`` (node state の temperature 込み・seeded
    noise) から ``dot(q, v)/(|q||v|)`` を求める。vector が取得できない
    node は 0.0 詰め (missing count を呼び出し側に返す)。
    """
    q_vec = np.asarray(
        engine.embedder.encode_query(query), dtype=np.float32,
    ).reshape(-1)
    q_norm = float(np.linalg.norm(q_vec))
    raw_vecs = engine.faiss_index.get_vectors(ids) if ids else {}
    out: list[float] = []
    missing = 0
    for nid in ids:
        vec = raw_vecs.get(nid)
        if vec is None:
            missing += 1
            out.append(0.0)
            continue
        state = engine.cache.get_node(nid)
        temperature = float(state.temperature) if state is not None else 0.0
        displacement = engine.cache.get_displacement(nid)
        rng = np.random.default_rng(_NOISE_SEED)
        v = compute_virtual_position(vec, displacement, temperature, rng=rng)
        v_norm = float(np.linalg.norm(v))
        if q_norm <= 0.0 or v_norm <= 0.0:
            out.append(0.0)
            continue
        out.append(float(np.dot(q_vec, v)) / (q_norm * v_norm))
    return out, missing


def _lexical_scores(engine: GaOTTTEngine, query: str) -> tuple[dict[str, float], float]:
    """BM25 top-50 pool の score map と top score (lexical strength の分母)。"""
    scores: dict[str, float] = {}
    if engine.bm25_index is not None and engine.bm25_index.size > 0:
        scores = {nid: float(s) for nid, s in engine.bm25_index.search(query, 50)}
    top = max(scores.values()) if scores else 0.0
    return scores, top


def _collect_qualification(
    engine: GaOTTTEngine, query: str, results,
) -> tuple[dict, dict[str, list]]:
    """指標 2 — raw_cos / 正規化 virtual cosine / lexical strength の測定と sweep。

    戻り値: (JSON 用 per-query dict, pooled 集計用の生値リスト)。
    """
    ids = [r.id for r in results]
    raw_vals = [
        float(r.score_breakdown.raw_cosine) if r.score_breakdown is not None else 0.0
        for r in results
    ]
    vcos_vals, missing_vec = _normalized_virtual_cosines(engine, query, ids)
    bm25_scores, bm25_top = _lexical_scores(engine, query)
    rel_vals = [
        bm25_scores.get(nid, 0.0) / bm25_top if bm25_top > 0 else 0.0
        for nid in ids
    ]
    bm25_abs_vals = [bm25_scores.get(nid, 0.0) for nid in ids]
    gap_vals = [vc - raw for vc, raw in zip(vcos_vals, raw_vals)]
    per_query = {
        "query": query,
        "n_results": len(results),
        "rates": {
            "raw": _sweep_rates(raw_vals, RAW_SWEEP),
            "vcos_norm": _sweep_rates(vcos_vals, VCOS_SWEEP),
            "rel": _sweep_rates(rel_vals, REL_SWEEP),
        },
        "or_qualified_provisional": _or_qualified_rate(
            raw_vals, vcos_vals, rel_vals, bm25_abs_vals,
        ),
        "missing_raw_vector": missing_vec,
        "bm25_pool_top_score": _r(bm25_top),
    }
    pooled = {
        "raw": raw_vals, "vcos": vcos_vals, "rel": rel_vals, "gap": gap_vals,
        "bm25_abs": bm25_abs_vals,
    }
    return per_query, pooled


def _slot_counts(resp) -> dict:
    return {
        "direct": len(resp.direct),
        "lensing": len(resp.lensing),
        "dormant": len(resp.dormant),
        "persona": 1 if resp.persona is not None else 0,
    }


async def _ambient_diag(engine: GaOTTTEngine, record: dict) -> dict:
    """指標 3 — ambient_recall の gate 内部値 + count (gate ON/OFF 両方)。

    gate OFF 版は「BM25 veto による空返し」と「slot 構成 / semantic fallback
    による空返し」を切り分けるための追加測定 (engine.config の一時変更は
    自プロセス内の隔離 engine のみに影響)。empty_reason_inferred は script
    側の推論であり、Stage 5 実装後は response 由来の離散値に置き換わる前提。
    """
    cfg = engine.config
    query = record["query"]
    exclude_tags = record.get("exclude_tags")

    gate_available = (
        engine.ambient_gate_index is not None and engine.ambient_gate_index.size > 0
    )
    bm25_top: float | None = None
    if gate_available:
        hits = engine.ambient_gate_index.search(query, 1)
        bm25_top = float(hits[0][1]) if hits else 0.0
    gate_pass = None if bm25_top is None else bm25_top >= cfg.ambient_bm25_min_score

    # ambient_recall 内部と同じ pool 形状 (direct_k=2 → pool_k=10) を mirror。
    pool = await engine.query(
        text=query, top_k=10, passive=True,
        multi_source=cfg.multi_source_ambient_enabled,
    )
    semantic_max_virtual = max((float(r.raw_score) for r in pool), default=0.0)
    semantic_max_raw = max(
        (float(r.score_breakdown.raw_cosine) for r in pool
         if r.score_breakdown is not None),
        default=0.0,
    )

    resp = await memory_service.ambient_recall(
        engine, query, expose_breakdown=True, exclude_tags=exclude_tags,
    )

    saved_flag = cfg.ambient_gate_use_bm25
    cfg.ambient_gate_use_bm25 = False
    try:
        resp_off = await memory_service.ambient_recall(
            engine, query, expose_breakdown=True, exclude_tags=exclude_tags,
        )
    finally:
        cfg.ambient_gate_use_bm25 = saved_flag

    if resp.count > 0:
        reason = "surfaced"
    elif gate_pass is False:
        reason = "bm25_veto"
    elif not pool:
        reason = "no_candidates"
    elif semantic_max_virtual < cfg.ambient_min_score:
        reason = "semantic_below_min_score"
    else:
        reason = "filtered_empty"

    return {
        "query": query,
        "source_file": record["source_file"],
        "axis": record.get("axis"),
        "gate_bm25_top_score": None if bm25_top is None else _r(bm25_top),
        "gate_threshold": _r(cfg.ambient_bm25_min_score),
        "gate_pass": gate_pass,
        "semantic_max_virtual": _r(semantic_max_virtual),
        "semantic_max_raw": _r(semantic_max_raw),
        "ambient_min_score": _r(cfg.ambient_min_score),
        "count": resp.count,
        "slots": _slot_counts(resp),
        "count_gate_off": resp_off.count,
        "slots_gate_off": _slot_counts(resp_off),
        "empty_reason_inferred": reason,
        "exclude_tags": exclude_tags,
    }


def _jaccard(a: list[str], b: list[str]) -> float | None:
    sa, sb = set(a), set(b)
    union = sa | sb
    if not union:
        return None
    return len(sa & sb) / len(union)


async def _measure(
    engine: GaOTTTEngine, fixture_to_engine: dict[str, str],
    ingest_done_at: float,
) -> dict:
    queries = _load_queries(QUERIES_PATH)
    ambient_queries = _load_queries(AMBIENT_QUERIES_PATH)
    fixture_of = {v: k for k, v in fixture_to_engine.items()}

    # --- 指標 1 + 2: queries.json 各 query の passive top-100 ---
    contributions: list[dict] = []
    qualifications: list[dict] = []
    pooled: dict[str, list] = {
        **{f"share_{t}": [] for t in ADDITIVE_TERMS},
        "decay": [], "saturation": [],
        "raw": [], "vcos": [], "rel": [], "gap": [], "bm25_abs": [],
    }
    for q in queries:
        age = time.time() - ingest_done_at
        results = await engine.query(
            text=q["query"], top_k=100, passive=True, wave_k=_MEASURE_WAVE_K,
        )
        contrib, contrib_pool = _collect_contributions(
            q["query"], results, fixture_of, age,
        )
        qual, qual_pool = _collect_qualification(engine, q["query"], results)
        contributions.append(contrib)
        qualifications.append(qual)
        for key, values in (*contrib_pool.items(), *qual_pool.items()):
            pooled[key].extend(values)

    # --- 指標 3: ambient gate diagnostic (両 query file) ---
    ambient_targets = [
        {**q, "source_file": "queries.json"} for q in queries
    ] + [
        {**q, "source_file": "ambient_queries.json"} for q in ambient_queries
    ]
    ambient_records = [await _ambient_diag(engine, t) for t in ambient_targets]

    # --- 指標 4: recall vs explore Jaccard@5 ---
    jaccard_rows: list[dict] = []
    for q in queries:
        base = await memory_service.recall(
            engine, q["query"], top_k=5, passive=True, auto_route=False,
        )
        base_ids = [it.id for it in base.items][:5]
        row: dict = {"query": q["query"], "jaccard": {}}
        for d in DIVERSITIES:
            ex = await memory_service.explore(
                engine, q["query"], diversity=d, top_k=5,
                auto_route=False, mode="serendipity", passive=True,
            )
            ex_ids = [it.id for it in ex.items][:5]
            row["jaccard"][f"{d:.1f}"] = _jaccard(base_ids, ex_ids)
        jaccard_rows.append(row)
    jaccard_median = {
        f"{d:.1f}": _r(_percentile(
            [row["jaccard"][f"{d:.1f}"] for row in jaccard_rows
             if row["jaccard"][f"{d:.1f}"] is not None], 50,
        ))
        for d in DIVERSITIES
    }

    score_section = {
        "terms": list(ADDITIVE_TERMS),
        "note": (
            "share = term / sum(additive terms) (= term / final_score when "
            "saturation=1)。additive_sum<=0 の node は除外 (skipped 参照)。"
        ),
        "overall": {
            **{t: _share_stats(pooled[f"share_{t}"]) for t in ADDITIVE_TERMS},
            "decay_factor": {
                "min": _r(_percentile(pooled["decay"], 0)),
                "median": _r(_percentile(pooled["decay"], 50)),
                "max": _r(_percentile(pooled["decay"], 100)),
            },
            "saturation": _share_stats(pooled["saturation"]),
            "n_results_pooled": len(pooled["decay"]),
        },
        "per_query": contributions,
    }
    qual_section = {
        "axes": {
            "raw_cos": "ScoreBreakdown.raw_cosine (deterministic)",
            "virtual_cos_norm": (
                "self-computed dot(q, v)/(|q||v|) via get_vectors + "
                "get_displacement + compute_virtual_position (temperature "
                "included, seeded noise)"
            ),
            "lexical_rel": "bm25 score / bm25 top score (top-50 pool)",
        },
        "sweep_ranges": {
            "raw": RAW_SWEEP, "vcos_norm": VCOS_SWEEP, "rel": REL_SWEEP,
        },
        "provisional_thresholds": PROVISIONAL,
        "bm25_absolute_min": _BM25_ABS_MIN,
        "pooled_distributions": {
            "raw_cos": _dist_stats(pooled["raw"]),
            "vcos_norm": _dist_stats(pooled["vcos"]),
            "lexical_rel": _dist_stats(pooled["rel"]),
            "lensing_gap": _dist_stats(pooled["gap"]),
        },
        "pooled_rates": {
            "raw": _sweep_rates(pooled["raw"], RAW_SWEEP),
            "vcos_norm": _sweep_rates(pooled["vcos"], VCOS_SWEEP),
            "rel": _sweep_rates(pooled["rel"], REL_SWEEP),
            # 実装契約 threshold での per-axis qualified 率。sweep 範囲が
            # 契約値を含まない axis (raw sweep は 0.30-0.70) でも summary
            # が契約値を正確に出せるよう独立計上する。
            "at_provisional": {
                "raw": _rate_at(pooled["raw"], PROVISIONAL["raw"]),
                "vcos_norm": _rate_at(pooled["vcos"], PROVISIONAL["vcos"]),
                "rel": _rate_at(pooled["rel"], PROVISIONAL["rel"]),
            },
            "or_provisional": _or_qualified_rate(
                pooled["raw"], pooled["vcos"], pooled["rel"],
                pooled["bm25_abs"],
            ),
        },
        "per_query": qualifications,
    }
    ambient_section = {
        "note": (
            "count は production gate 設定 (word-BM25 veto) での ambient_recall。"
            "count_gate_off は ambient_gate_use_bm25=False (virtual_score "
            "fallback gate) での測定。empty_reason_inferred は script 側推論。"
        ),
        "queries": ambient_records,
    }
    jaccard_section = {
        "diversities": list(DIVERSITIES),
        "per_query": jaccard_rows,
        "median": jaccard_median,
    }
    return {
        "n_queries": len(queries),
        "n_ambient_queries": len(ambient_queries),
        "score_contributions": score_section,
        "qualification": qual_section,
        "ambient_gate": ambient_section,
        "explore_jaccard": jaccard_section,
    }


def _summary_lines(payload: dict) -> list[str]:
    lines: list[str] = []
    eng = payload["engine"]
    corpus = " ".join(f"{k}={v}" for k, v in sorted(eng["corpus"].items()))
    lines.append("=== Phase T Stage 1 baseline summary ===")
    lines.append(
        f"corpus: {corpus} / faiss={eng['faiss_size']} "
        f"bm25={eng['bm25_size']} gate={eng['ambient_gate_index_size']}"
    )
    lines.append(
        f"embedder: {eng['embedder']['class']} ({eng['embedder']['model']}) "
        f"dim={eng['embedder']['dimension']}"
    )
    ov = payload["score_contributions"]["overall"]
    lines.append(
        f"[score contributions] pooled n={ov['n_results_pooled']} "
        f"({payload['n_queries']} queries x top-100 passive)"
    )
    for t in ADDITIVE_TERMS:
        s = ov[t]
        lines.append(
            f"  {t:<9} share: mean={s['mean']:.3f} p50={s['p50']:.3f} "
            f"p90={s['p90']:.3f}"
        )
    d = ov["decay_factor"]
    lines.append(
        f"  decay_factor: min={d['min']:.4f} median={d['median']:.4f} "
        f"max={d['max']:.4f}  (delta is per-second — see caveats)"
    )
    qual = payload["qualification"]
    dists = qual["pooled_distributions"]
    lines.append("[qualification — pooled distributions]")
    for axis in ("raw_cos", "vcos_norm", "lexical_rel", "lensing_gap"):
        s = dists[axis]
        lines.append(
            f"  {axis:<11} min={s['min']:.3f} p10={s['p10']:.3f} "
            f"p50={s['p50']:.3f} p90={s['p90']:.3f} max={s['max']:.3f} "
            f"(n={s['n']})"
        )
    rates = qual["pooled_rates"]
    lines.append(
        f"  qualified @ contract OR (raw>={PROVISIONAL['raw']} or "
        f"vcos>={PROVISIONAL['vcos']} or (rel>={PROVISIONAL['rel']} and "
        f"bm25>={_BM25_ABS_MIN:g})): {rates['or_provisional']:.3f}"
    )
    at = rates["at_provisional"]
    lines.append(
        f"  at contract thresholds: "
        f"raw@{PROVISIONAL['raw']:.2f}={at['raw']:.3f} "
        f"vcos@{PROVISIONAL['vcos']:.2f}={at['vcos_norm']:.3f} "
        f"rel@{PROVISIONAL['rel']:.2f}={at['rel']:.3f}"
    )
    amb = payload["ambient_gate"]["queries"]
    surfaced = sum(1 for a in amb if a["count"] > 0)
    surfaced_off = sum(1 for a in amb if a["count_gate_off"] > 0)
    veto = sum(1 for a in amb if a["gate_pass"] is False)
    lines.append(
        f"[ambient gate] threshold={amb[0]['gate_threshold']} — surfaced "
        f"{surfaced}/{len(amb)} (gate on), {surfaced_off}/{len(amb)} (gate "
        f"off); bm25 veto {veto}/{len(amb)}"
    )
    jm = payload["explore_jaccard"]["median"]
    parts = " ".join(f"d{k}={v:.3f}" for k, v in jm.items())
    lines.append(f"[explore Jaccard@5 vs passive recall] median: {parts}")
    return lines


def _backdate_all(engine: GaOTTTEngine, age_seconds: float) -> int:
    """隔離 DB の全 active node の last_access を一斉 backdate する。

    Phase T Stage 2 の after 測定用: fresh corpus では decay≈0.98 で
    half-life 契約の効果が観測できないため、query 前に経年 corpus を
    simulate する。測定はすべて passive なので last_access は測定中に
    書き換わらない。隔離 engine のみに作用し production には無関係。
    """
    now = time.time()
    n = 0
    for state in engine.cache.get_all_nodes():
        if state.is_archived:
            continue
        state.last_access = now - age_seconds
        engine.cache.set_node(state, dirty=True)
        n += 1
    return n


async def _run(args: argparse.Namespace) -> dict:
    t0 = time.time()
    data_dir = Path(args.data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    # 前回 run の残骸を消して cold 測定を保証 (perf_baseline.py と同じ作法)。
    for f in data_dir.iterdir():
        if f.is_file():
            f.unlink()

    engine = make_engine(data_dir)
    if engine.config.ambient_gate_use_bm25:
        # perf helper は ambient gate index を組まないため、runtime factory
        # (services/runtime.py) と同じ作法で word-BM25 gate index を接続する。
        # startup 前に set すれば index_documents が自動で populate する。
        try:
            engine.ambient_gate_index = BM25Index(
                tokenizer=engine.config.ambient_gate_tokenizer,
            )
        except ImportError as exc:
            print(f"WARNING: ambient gate index unavailable ({exc})")
    await engine.startup()
    try:
        corpus_counts, fixture_to_engine = await _ingest(engine)
        ingest_done_at = time.time()
        backdated_nodes = 0
        if args.synthetic_age_seconds is not None:
            backdated_nodes = _backdate_all(engine, args.synthetic_age_seconds)
        measurements = await _measure(engine, fixture_to_engine, ingest_done_at)
    finally:
        await engine.shutdown()

    cfg = engine.config
    embedder_model = getattr(engine.embedder, "_model", None)
    payload = {
        "script": "scripts/score_baseline.py",
        "purpose": "Phase T Stage 1 — retrieval-quality baseline (before Stage 2-6)",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "git_sha": _git_sha(),
        "engine": {
            "embedder": {
                "class": type(engine.embedder).__name__,
                "model": getattr(engine.embedder, "_model_name", "n/a"),
                "dimension": engine.embedder.dimension,
                "device": str(getattr(embedder_model, "device", "n/a")),
            },
            "corpus": corpus_counts,
            "faiss_size": engine.faiss_index.size,
            "bm25_size": engine.bm25_index.size if engine.bm25_index else 0,
            "ambient_gate_index_size": (
                engine.ambient_gate_index.size
                if engine.ambient_gate_index is not None else 0
            ),
            "config": {name: getattr(cfg, name, None) for name in _CONFIG_FIELDS},
        },
        "measurement": {
            "recall_call": (
                "engine.query(text=q, top_k=100, passive=True, "
                f"wave_k={_MEASURE_WAVE_K})"
            ),
            "wave_k_rationale": (
                "perf helper 既定の wave_initial_k=3 では top-100 を要求しても "
                "seed pool が ~4 件に絞られ threshold 較正にならないため、"
                "corpus 全件を seed 化する wave_k を明示。ambient / Jaccard 測定"
                "は service 既定 wave params のまま (production faithful)。"
            ),
            "ambient_mirror_pool": (
                "engine.query(text=q, top_k=10, passive=True) — ambient_recall "
                "内部 (direct_k=2 → pool_k=10) と同じ形状"
            ),
            "synthetic_age_seconds": (
                None if args.synthetic_age_seconds is None
                else _r(args.synthetic_age_seconds)
            ),
            "synthetic_backdated_nodes": backdated_nodes,
            "synthetic_age_note": (
                "--synthetic-age-seconds 指定時、測定前に全 active node の "
                "last_access を一斉 backdate した (passive 測定のみなので "
                "測定中の書き換えなし)。per-query の age_seconds_at_query は "
                "ingest からの実経過秒のままなので、decay 分布の解釈には "
                "synthetic_age_seconds を優先すること。"
            ),
            "passive_only": True,
        },
        "caveats": [
            "decay_factor は ingest からの経過秒で決まる (現行 delta は秒 rate "
            "契約)。run の wall-clock が変わると decay 分布は変わるため、"
            "before/after 比較では per-query の age_seconds_at_query を参照。",
            "passive 測定のみなので displacement / temperature はほぼ 0 — "
            "vcos_norm ≈ raw_cos が期待値。Stage 2-6 後の再測定も同一手順で。",
            "ambient gate の word-BM25 threshold は本番 corpus scale で較正済み。"
            "42-doc の golden corpus では veto になりやすく、gate_off 列で slot "
            "構成を別途観察する。",
        ],
        "run_seconds": _r(time.time() - t0),
        **measurements,
    }

    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"baseline JSON written to {out_path}")
    for line in _summary_lines(payload):
        print(line)
    return payload


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--data-dir",
        default=str(_PROJECT_ROOT / ".score-baseline-tmp"),
        help="隔離 data dir (本番 DB 不可触。run ごとに file を wipe する)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="JSON 出力先。未指定なら summary のみ stdout へ。",
    )
    parser.add_argument(
        "--synthetic-age-seconds",
        type=float,
        default=None,
        help=(
            "測定前に隔離 DB の全 active node の last_access を一斉 "
            "backdate する (経年 corpus の simulate。fresh corpus では "
            "Stage 2 の half-life 契約効果が decay≈0.98 で見えないため)。"
            "例: 7日=604800、30日=2592000。production には影響しない。"
        ),
    )
    args = parser.parse_args(argv)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
