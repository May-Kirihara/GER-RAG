#!/usr/bin/env python
"""Calibrate the ambient composite gate 3-arm (Phase U §10 R3 follow-up)
on a production COPY.

Runs the REAL ``ambient_recall`` pipeline (passive by contract — physics
untouched, and this is a copy anyway) over a labeled probe set, builds the
reference virt-top-1 distribution (recalibration provenance — the 3-arm
decision does not read it, but the runtime fail-closed contract does),
grid-searches the three 3-arm thresholds on a fixed-seed 50/50 stratified
split, and reports held-out FP/FN rates with bootstrap CIs. With
``--emit-artifact`` it writes the reference artifact (schema v1) that
``ambient_gate_mode="composite"`` validates at runtime (fingerprint:
embedder identity + corpus digest + active count).

3-arm decision under calibration (Plans-Phase-U-Review-Hardening.md §10)::

    accept = bm25_strong (>= ambient_bm25_min_score, NOT gridded —
                          production-calibrated, rides the engine config)
          OR (virt_top1 >= ambient_composite_virt_hi)
          OR (bm25_top >= ambient_composite_bm25_mid
              AND virt_top1 >= ambient_composite_virt_mid)

Pre-registered promotion gate (§10, v3 fresh probe set): default promotion
to composite requires held-out **FP=0 AND FN<=10%**. This script only
REPORTS that verdict — the promotion decision is the PM's, never the
script's.

Read-only w.r.t. ``--data-dir`` EXCEPT that engine startup/shutdown may
touch sidecars (manifest is only written when absent; write-behind loops
are disabled). Always run against a COPY (``sqlite3 .backup`` + FAISS /
manifest file copy), never the live DB. The copy is calibration-only —
do not use it as a restore source (a few seconds of sidecar race are
accepted; see Plans-Phase-U §5).

Usage::

    .venv/bin/python scripts/calibrate_ambient_gate.py \\
        --data-dir /tmp/gaottt-calib-copy \\
        --probes scripts/ambient_probes_default.json \\
        [--emit-artifact /tmp/gaottt-calib-copy/ambient_composite_reference.json] \\
        [--seed 42]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Allow direct invocation (bootstrap_report.py pattern).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from gaottt.config import GaOTTTConfig  # noqa: E402
from gaottt.core.engine import GaOTTTEngine  # noqa: E402
from gaottt.services import memory as memory_service  # noqa: E402
from gaottt.services.ambient_composite import (  # noqa: E402
    CompositeGateThresholds,
    build_artifact_payload,
    compute_corpus_digest,
    composite_gate_decision,
    write_reference_artifact,
)
from gaottt.services.runtime import build_engine  # noqa: E402
from gaottt.store.manifest import load_manifest  # noqa: E402


def _grid(lo: float, hi: float, step: float) -> list[float]:
    """Inclusive float grid — round で 2 進小数の drift を断つ (決定的)。"""
    n_steps = int(round((hi - lo) / step))
    return [round(lo + i * step, 6) for i in range(n_steps + 1)]


# 3-arm grid (plan §10)。範囲は v2 探索的解析の分離帯 (~0.850/~22/~0.845)
# を中央に据え、両方向に広げた。arm1 (bm25_strong) は grid 外 —
# production 較正済みの ambient_bm25_min_score に固定。
VIRT_HI_GRID = _grid(0.83, 0.87, 0.005)     # 9 points
BM25_MID_GRID = _grid(16.0, 28.0, 2.0)      # 7 points
VIRT_MID_GRID = _grid(0.83, 0.855, 0.005)   # 6 points
# arm3 は arm2 より緩い中途 arm なので virt_mid > virt_hi の組は意味を
# なさない (arm3 が arm2 に支配される) — grid から除外する。
BOOTSTRAP_RESAMPLES = 200
# 事前登録 gate (PM 判断用の報告値 — script は昇格しない)
PROMOTION_MAX_FN_RATE = 0.10
# WP-6c: BM25 build は background task — startup 返却直後の "building" 窓内
# に probe を走らせると bm25 軸が全 probe で欠落し較正が無効になる (v3 run
# VOID の根因)。production 実測 147s (startup-timings) + 余裕。
BM25_WAIT_TIMEOUT_SECONDS = 300.0


async def wait_for_bm25_ready(
    engine: GaOTTTEngine,
    timeout: float = BM25_WAIT_TIMEOUT_SECONDS,
    poll_interval: float = 1.0,
) -> str:
    """``engine.bm25_build_state`` が終状態へ着くまで bounded wait する。

    戻り値は到達した結果: ``"ready"`` / ``"failed"`` / ``"idle"``
    (index 未配線 — build は一度も始まらない) / ``"timeout"``。
    呼び出し側が継続可否を判断する (本 script は ERROR で exit 1、
    diag_target_trace.py は WARNING して続行)。
    """
    deadline = time.monotonic() + timeout
    while True:
        state = engine.bm25_build_state
        if state != "building":
            return state
        if time.monotonic() >= deadline:
            return "timeout"
        await asyncio.sleep(poll_interval)


@dataclass
class ProbeRecord:
    label: str                 # "positive" | "negative"
    query: str
    expect_related: bool
    bm25_top: float | None = None
    virt_top1: float | None = None
    pool_size: int = 0
    runtime_signal: str | None = None  # composite_signal observed during probing
    top1_snippet: str = ""             # sanity stage (positives)
    top1_match_count: int = 0


@dataclass
class Split:
    calibration: list[ProbeRecord] = field(default_factory=list)
    held_out: list[ProbeRecord] = field(default_factory=list)


def _load_probes(path: Path) -> tuple[list[dict], list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    positives = data.get("positives") or []
    negatives = data.get("negatives") or []
    if not positives or not negatives:
        raise SystemExit(
            f"probe file {path} must contain non-empty 'positives' and "
            f"'negatives'"
        )
    for p in positives:
        if not isinstance(p, dict) or not p.get("query"):
            raise SystemExit(f"positive probe without query: {p!r}")
    for n in negatives:
        if not isinstance(n, dict) or not n.get("query"):
            raise SystemExit(f"negative probe without query: {n!r}")
    return positives, negatives


def _char_bigrams(text: str) -> set[str]:
    normalized = "".join(ch.lower() for ch in text if ch.isalnum())
    return {normalized[i:i + 2] for i in range(len(normalized) - 1)}


def _match_count(query: str, content: str) -> int:
    """Sanity-stage signal: how many query char-bigrams appear in the top-1
    content. 日本語 query は分かち書きできないため bigram overlap で近似
    (目視補助であり判定ではない)。"""
    q = _char_bigrams(query)
    if not q:
        return 0
    return sum(1 for g in q if g in content.lower())


async def _probe(engine: GaOTTTEngine, rec: ProbeRecord) -> None:
    """One probe through the REAL ambient path (mode already forced to
    composite on the engine config). The reference artifact does not exist
    yet, so the semantic arms come back fail-closed — the AXES
    (virt_top1 / bm25_top / pool) ride the diagnostics regardless of the
    verdict, which is all calibration needs."""
    resp = await memory_service.ambient_recall(
        engine, rec.query, direct_k=3, expose_breakdown=True,
    )
    diag = resp.gate_diagnostics
    if diag is not None:
        rec.bm25_top = diag.bm25_top_score
        rec.virt_top1 = diag.virt_top1
        rec.pool_size = diag.after_dump_filter or 0
        rec.runtime_signal = diag.composite_signal


async def _sanity_top1(engine: GaOTTTEngine, rec: ProbeRecord) -> None:
    """Read-only top-1 retrieval for the PM's eyeball check (passive
    recall; ambient's composite gate has not been calibrated yet, so we
    ask the plain recall path for the snippet)."""
    rr = await memory_service.recall(
        engine, rec.query, top_k=3, passive=True, auto_route=False,
    )
    if rr.items:
        rec.top1_snippet = rr.items[0].content[:60].replace("\n", " ")
        rec.top1_match_count = _match_count(rec.query, rr.items[0].content)


def _decide(
    rec: ProbeRecord, thresholds: CompositeGateThresholds,
    bm25_threshold: float,
) -> bool:
    """Runtime-identical accept decision for evaluation (pure function).
    ``reference_available=True`` — grid 評価は較正 artifact が存在する
    runtime (昇格後) を模す。"""
    verdict = composite_gate_decision(
        bm25_top=rec.bm25_top,
        bm25_threshold=bm25_threshold,
        virt_top1=rec.virt_top1,
        reference_available=True,
        thresholds=thresholds,
        pool_size=rec.pool_size,
    )
    return verdict.accepted


def _split(records: list[ProbeRecord], seed: int) -> Split:
    """Fixed-seed 50/50 stratified split (label 別に shuffle → 半分ずつ)。"""
    rng = random.Random(seed)
    split = Split()
    for label in ("positive", "negative"):
        group = [r for r in records if r.label == label]
        rng.shuffle(group)
        half = (len(group) + 1) // 2
        split.calibration.extend(group[:half])
        split.held_out.extend(group[half:])
    return split


def _rates(
    records: list[ProbeRecord], thresholds: CompositeGateThresholds,
    bm25_threshold: float,
) -> tuple[float, float]:
    """(FP rate over negatives, FN rate over positives) on ``records``."""
    neg = [r for r in records if r.label == "negative"]
    pos = [r for r in records if r.label == "positive"]
    fp = sum(
        1 for r in neg if _decide(r, thresholds, bm25_threshold)
    ) / max(len(neg), 1)
    fn = sum(
        1 for r in pos if not _decide(r, thresholds, bm25_threshold)
    ) / max(len(pos), 1)
    return fp, fn


def _bootstrap_ci(
    records: list[ProbeRecord], thresholds: CompositeGateThresholds,
    bm25_threshold: float, seed: int,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """200-resample percentile bootstrap for held-out FP/FN CIs."""
    rng = random.Random(seed ^ 0x5EED)
    fp_samples: list[float] = []
    fn_samples: list[float] = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        sample = [rng.choice(records) for _ in range(len(records))]
        fp, fn = _rates(sample, thresholds, bm25_threshold)
        fp_samples.append(fp)
        fn_samples.append(fn)
    fp_samples.sort()
    fn_samples.sort()
    lo = int(0.025 * BOOTSTRAP_RESAMPLES)
    hi = max(int(0.975 * BOOTSTRAP_RESAMPLES), BOOTSTRAP_RESAMPLES - 1)
    return (fp_samples[lo], fp_samples[hi]), (fn_samples[lo], fn_samples[hi])


async def _run(args: argparse.Namespace) -> int:
    probes_path = Path(args.probes)
    positives_raw, negatives_raw = _load_probes(probes_path)
    print("== ambient composite gate calibration (3-arm, Phase U §10) ==")
    print(f"data-dir : {args.data_dir}")
    print(f"probes   : {probes_path} "
          f"({len(positives_raw)} positives / {len(negatives_raw)} negatives)")
    print(f"seed     : {args.seed}")
    if probes_path.resolve() == (Path(__file__).parent / "ambient_probes_default.json").resolve():
        print("WARNING: using the SEED probe set — PM should run the real "
              "calibration on the production copy with curated probes.")

    config = GaOTTTConfig(data_dir=str(args.data_dir))
    # diag_recall.py と同じ read-only 配慮 — write-behind loop を全て止める。
    config.faiss_save_interval_seconds = 0.0
    config.virtual_faiss_save_interval_seconds = 0.0
    config.dream_enabled = False
    engine = build_engine(config)
    await engine.startup()
    try:
        # WP-6c: build 完了前に probe を走らせない — bm25 軸なしの較正は
        # 無効 (arm1 bm25_strong / arm3 bm25_virt_mid が評価できない)。
        if engine.bm25_build_state == "building":
            print(
                "waiting for BM25 background build "
                f"(timeout {BM25_WAIT_TIMEOUT_SECONDS:.0f}s)..."
            )
        bm25_state = await wait_for_bm25_ready(engine)
        if bm25_state != "ready":
            print(
                f"\nERROR: BM25 build did not reach 'ready' (wait result: "
                f"{bm25_state!r}) — the ambient word-BM25 axis is "
                "unavailable. Calibration without the bm25 axis is invalid. "
                "Check the engine logs (build failure / timeout), fix or "
                "raise BM25_WAIT_TIMEOUT_SECONDS, then re-run.",
            )
            return 1
        if engine.ambient_gate_index is None:
            print(
                "\nERROR: ambient gate index is not wired (bm25-sudachi "
                "extra missing or ambient_gate_use_bm25=False) — the bm25 "
                "axis is unavailable and calibration is invalid. Install "
                "the extra / enable the gate and re-run.",
            )
            return 1
        # script-level override: composite mode so the ambient path computes
        # (and surfaces) the composite axes. No artifact exists yet, so the
        # semantic arms are fail-closed during probing — by design.
        engine.config.ambient_gate_mode = "composite"
        bm25_threshold = engine.config.ambient_bm25_min_score

        records: list[ProbeRecord] = []
        for p in positives_raw:
            records.append(ProbeRecord(
                label="positive", query=p["query"],
                expect_related=bool(p.get("expect_related", True)),
            ))
        for n in negatives_raw:
            records.append(ProbeRecord(
                label="negative", query=n["query"], expect_related=False,
            ))

        print("\n-- stage 1: probe collection (ambient_recall, 3-arm axes) --")
        for rec in records:
            await _probe(engine, rec)
            missing = rec.virt_top1 is None
            print(
                f"  [{rec.label[:3]}] bm25={_fmt(rec.bm25_top)} "
                f"virt1={_fmt(rec.virt_top1)} "
                f"pool={rec.pool_size}{' (EMPTY POOL)' if missing else ''}"
                f"  {rec.query}"
            )

        print("\n-- stage 2: sanity eyeball (positives, passive recall top-1) --")
        print("  (match = query char-bigram overlap count — 目視補助のみ)")
        for rec in records:
            if rec.label == "positive" and rec.expect_related:
                await _sanity_top1(engine, rec)
                print(f"  match={rec.top1_match_count:3d}  top1: {rec.top1_snippet}")

        # 参照分布は 3-arm 判定には使わないが、artifact の再較正 provenance
        # (plan §10) として構築・保存する。
        reference = [r.virt_top1 for r in records if r.virt_top1 is not None]
        if len(reference) < 4:
            print(
                f"\nABORT: reference distribution has only {len(reference)} "
                "valid points — probes/corpus too small to calibrate."
            )
            return 1
        print(f"\nreference distribution (provenance): n={len(reference)} "
              f"min={min(reference):.4f} max={max(reference):.4f}")

        split = _split(records, args.seed)
        print(f"split (seeded, stratified): calibration={len(split.calibration)} "
              f"held-out={len(split.held_out)}")

        print("\n-- stage 3: grid search on calibration split (3-arm) --")
        print("  goal: FP_cal=0 → min FN_cal → laxer thresholds (deterministic)")
        results: list[tuple[float, float, CompositeGateThresholds]] = []
        for vhi in VIRT_HI_GRID:
            for bmid in BM25_MID_GRID:
                for vmid in VIRT_MID_GRID:
                    if vmid > vhi:
                        continue  # arm3 が arm2 に支配される非意味領域
                    th = CompositeGateThresholds(
                        virt_hi=vhi, bm25_mid=bmid, virt_mid=vmid,
                    )
                    fp, fn = _rates(split.calibration, th, bm25_threshold)
                    results.append((fp, fn, th))
        # 決定規約は v2 と同一: FP_cal 昇順 → FN_cal 昇順 → 閾値が緩い
        # (全軸小さい) 方向。laxer = 3 閾値とも低い。
        results.sort(key=lambda t: (t[0], t[1], t[2].virt_hi,
                                    t[2].bm25_mid, t[2].virt_mid))
        zero_fp = [r for r in results if r[0] == 0.0]
        shown = zero_fp[:10] if zero_fp else results[:10]
        print("  virt_hi  bm25_mid  virt_mid  FP_cal  FN_cal")
        for fp, fn, th in shown:
            print(f"  {th.virt_hi:7.3f} {th.bm25_mid:9.1f} "
                  f"{th.virt_mid:9.3f}  {fp:6.1%}  {fn:6.1%}")
        if not zero_fp:
            print("  (no FP=0 combination on calibration — see best rows above)")

        best_fp, best_fn, best_th = results[0]
        print(f"\nrecommended thresholds: virt_hi={best_th.virt_hi} "
              f"bm25_mid={best_th.bm25_mid} virt_mid={best_th.virt_mid}")

        print("\n-- stage 4: held-out evaluation + bootstrap CI --")
        fp, fn = _rates(split.held_out, best_th, bm25_threshold)
        (fp_lo, fp_hi), (fn_lo, fn_hi) = _bootstrap_ci(
            split.held_out, best_th, bm25_threshold, args.seed,
        )
        n_neg = sum(1 for r in split.held_out if r.label == "negative")
        n_pos = sum(1 for r in split.held_out if r.label == "positive")
        print(f"  held-out n: {n_pos} positives / {n_neg} negatives")
        print(f"  FP rate: {fp:.1%}  (95% CI {fp_lo:.1%}–{fp_hi:.1%})")
        print(f"  FN rate: {fn:.1%}  (95% CI {fn_lo:.1%}–{fn_hi:.1%})")

        # 事前登録 promotion gate の報告 (判断は PM — script は昇格しない)
        gate_pass = (fp == 0.0) and (fn <= PROMOTION_MAX_FN_RATE)
        if gate_pass:
            gate_msg = "PASS — PM may consider promoting composite"
        else:
            gate_msg = "FAIL — keep default 'or' and escalate per plan §10"
        print(
            "\n  pre-registered promotion gate (FP=0 AND FN<=10%): "
            + gate_msg
        )

        if args.emit_artifact:
            manifest = load_manifest(Path(engine.config.data_dir))
            if manifest is None:
                print("\nABORT --emit-artifact: no manifest.json in data-dir — "
                      "cannot fingerprint embedder identity.")
                return 1
            contents = await engine.store.get_all_contents()
            digest, _ = compute_corpus_digest(
                contents, engine.cache.node_cache.keys(),
            )
            payload = build_artifact_payload(
                embedder_id=manifest.embedder_id,
                embedder_version=manifest.embedder_version,
                corpus_digest=digest,
                # runtime drift-guard convention: len(cache.node_cache)
                active_count=len(engine.cache.node_cache),
                virt_top1_distribution=reference,
                thresholds={
                    "ambient_bm25_min_score": bm25_threshold,
                    "ambient_composite_virt_hi": best_th.virt_hi,
                    "ambient_composite_bm25_mid": best_th.bm25_mid,
                    "ambient_composite_virt_mid": best_th.virt_mid,
                },
                provenance={
                    "script": "scripts/calibrate_ambient_gate.py",
                    "seed": args.seed,
                    "data_dir": str(args.data_dir),
                    "probe_file": str(probes_path),
                    "n_probes": len(records),
                    "n_positive": sum(1 for r in records if r.label == "positive"),
                    "n_negative": sum(1 for r in records if r.label == "negative"),
                    "split": {
                        "calibration": len(split.calibration),
                        "held_out": len(split.held_out),
                    },
                    "recommended": {
                        "ambient_composite_virt_hi": best_th.virt_hi,
                        "ambient_composite_bm25_mid": best_th.bm25_mid,
                        "ambient_composite_virt_mid": best_th.virt_mid,
                    },
                    "heldout": {
                        "fp_rate": fp, "fn_rate": fn,
                        "fp_ci95": [fp_lo, fp_hi],
                        "fn_ci95": [fn_lo, fn_hi],
                        "promotion_gate_pass": gate_pass,
                    },
                    "gate_rule": (
                        "accept = bm25_strong OR (virt_top1 >= virt_hi) "
                        "OR (bm25_top >= bm25_mid AND virt_top1 >= "
                        "virt_mid)"
                    ),
                },
            )
            out = Path(args.emit_artifact)
            write_reference_artifact(out, payload)
            print(f"\nartifact written: {out} "
                  f"(thresholds echo = recommended; runtime authority is config)")
            print("NOTE: runtime knobs (ambient_composite_virt_hi / "
                  "ambient_composite_bm25_mid / ambient_composite_virt_mid) "
                  "must be set to the recommended values separately — PM "
                  "decision.")
        else:
            print("\n(no --emit-artifact — nothing written)")
        return 0
    finally:
        await engine.shutdown()


def _fmt(v: float | None, width: int = 6) -> str:
    return f"{v:.{width - 2}f}" if isinstance(v, (int, float)) else "  n/a "


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Calibrate the ambient composite gate (3-arm) on a "
                    "production COPY.",
    )
    parser.add_argument(
        "--data-dir", required=True,
        help="data dir of the production COPY (never the live DB)",
    )
    parser.add_argument(
        "--probes", default=str(Path(__file__).parent / "ambient_probes_default.json"),
        help="probe JSON: {'positives': [{'query', 'expect_related'}], "
             "'negatives': [{'query'}]}",
    )
    parser.add_argument(
        "--emit-artifact", default=None,
        help="write the reference artifact to this path (default: no write)",
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
