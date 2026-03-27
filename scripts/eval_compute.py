"""Step 3: LLM判定スコアからIRメトリクスを算出

eval_export.py の出力 + LLMの判定スコアを読み込み、
nDCG@10, MRR, Precision@10 を算出して比較レポートを出力する。

Usage:
    python scripts/eval_compute.py [--dir eval_output]

Expects:
    eval_output/
    ├── results.json       # eval_export.py が生成
    ├── judge_scores.json  # LLMの判定結果（ユーザーが保存）
    └── session_scores.json  # (optional) セッション適応性の判定結果
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys


# -----------------------------------------------------------------------
# IR Metrics
# -----------------------------------------------------------------------

def dcg_at_k(relevances: list[int], k: int) -> float:
    dcg = 0.0
    for i, rel in enumerate(relevances[:k]):
        dcg += rel / math.log2(i + 2)
    return dcg


def ndcg_at_k(relevances: list[int], k: int) -> float:
    dcg = dcg_at_k(relevances, k)
    ideal = dcg_at_k(sorted(relevances, reverse=True), k)
    if ideal == 0:
        return 0.0
    return dcg / ideal


def mrr(relevances: list[int], threshold: int = 2) -> float:
    for i, rel in enumerate(relevances):
        if rel >= threshold:
            return 1.0 / (i + 1)
    return 0.0


def precision_at_k(relevances: list[int], k: int, threshold: int = 2) -> float:
    relevant = sum(1 for r in relevances[:k] if r >= threshold)
    return relevant / k


def avg_relevance(relevances: list[int]) -> float:
    return sum(relevances) / len(relevances) if relevances else 0.0


# -----------------------------------------------------------------------
# Reporting
# -----------------------------------------------------------------------

def print_comparison_report(results_data: dict, scores: dict, top_k: int) -> dict:
    print(f"\n{'='*80}")
    print("  RETRIEVAL QUALITY: GER-RAG vs Static RAG")
    print(f"{'='*80}")

    header = f"  {'Query':<12s} {'':18s} {'nDCG@10':>8s} {'MRR':>8s} {'P@10':>8s} {'Avg Rel':>8s}"
    print(f"\n{header}")
    print(f"  {'-'*74}")

    all_metrics = []

    for q in results_data["queries"]:
        qid = q["query_id"]
        static_key = f"{qid}_static"
        ger_key = f"{qid}_ger"

        if static_key not in scores or ger_key not in scores:
            print(f"  {qid:<12s} SKIP (scores not found)")
            continue

        s_rel = scores[static_key]
        g_rel = scores[ger_key]

        s_ndcg = ndcg_at_k(s_rel, top_k)
        g_ndcg = ndcg_at_k(g_rel, top_k)
        s_mrr = mrr(s_rel)
        g_mrr = mrr(g_rel)
        s_prec = precision_at_k(s_rel, top_k)
        g_prec = precision_at_k(g_rel, top_k)
        s_avg = avg_relevance(s_rel)
        g_avg = avg_relevance(g_rel)

        print(f"  {qid:<12s} {'Static RAG':18s} {s_ndcg:>8.4f} {s_mrr:>8.4f} {s_prec:>8.4f} {s_avg:>8.2f}")
        print(f"  {'':12s} {'GER-RAG':18s} {g_ndcg:>8.4f} {g_mrr:>8.4f} {g_prec:>8.4f} {g_avg:>8.2f}")

        delta_ndcg = g_ndcg - s_ndcg
        sign = "+" if delta_ndcg >= 0 else ""
        print(f"  {'':12s} {'Delta':18s} {sign}{delta_ndcg:>7.4f}")
        print()

        all_metrics.append({
            "query_id": qid,
            "query_text": q["query_text"],
            "static": {"ndcg": s_ndcg, "mrr": s_mrr, "precision": s_prec, "avg_rel": s_avg, "relevances": s_rel},
            "ger": {"ndcg": g_ndcg, "mrr": g_mrr, "precision": g_prec, "avg_rel": g_avg, "relevances": g_rel},
            "delta_ndcg": delta_ndcg,
        })

    if not all_metrics:
        print("  No valid scores found.")
        return {}

    # Aggregates
    avg_s_ndcg = statistics.mean(m["static"]["ndcg"] for m in all_metrics)
    avg_g_ndcg = statistics.mean(m["ger"]["ndcg"] for m in all_metrics)
    avg_s_mrr = statistics.mean(m["static"]["mrr"] for m in all_metrics)
    avg_g_mrr = statistics.mean(m["ger"]["mrr"] for m in all_metrics)
    avg_s_prec = statistics.mean(m["static"]["precision"] for m in all_metrics)
    avg_g_prec = statistics.mean(m["ger"]["precision"] for m in all_metrics)

    print(f"  {'='*74}")
    print(f"  {'AVERAGE':<12s} {'Static RAG':18s} {avg_s_ndcg:>8.4f} {avg_s_mrr:>8.4f} {avg_s_prec:>8.4f}")
    print(f"  {'':12s} {'GER-RAG':18s} {avg_g_ndcg:>8.4f} {avg_g_mrr:>8.4f} {avg_g_prec:>8.4f}")

    delta = avg_g_ndcg - avg_s_ndcg
    pct = (delta / avg_s_ndcg * 100) if avg_s_ndcg > 0 else 0
    print(f"\n  nDCG@10 difference: {delta:+.4f} ({pct:+.1f}%)")

    wins = sum(1 for m in all_metrics if m["delta_ndcg"] > 0.001)
    ties = sum(1 for m in all_metrics if abs(m["delta_ndcg"]) <= 0.001)
    losses = len(all_metrics) - wins - ties
    print(f"  Win/Tie/Loss: {wins}W / {ties}T / {losses}L (out of {len(all_metrics)} queries)")

    return {
        "comparison": all_metrics,
        "summary": {
            "static_ndcg_mean": avg_s_ndcg, "ger_ndcg_mean": avg_g_ndcg,
            "static_mrr_mean": avg_s_mrr, "ger_mrr_mean": avg_g_mrr,
            "static_precision_mean": avg_s_prec, "ger_precision_mean": avg_g_prec,
            "ndcg_delta": delta, "ndcg_delta_pct": pct,
            "wins": wins, "ties": ties, "losses": losses,
        },
    }


def print_session_report(session_data: dict, scores: dict, top_k: int) -> dict:
    print(f"\n{'='*80}")
    print("  SESSION ADAPTIVITY: Before/After cross-topic co-occurrence training")
    print(f"{'='*80}")

    all_sessions = []

    scenarios = session_data.get("scenarios", session_data.get("queries", []))

    for sc in scenarios:
        sid = sc.get("scenario_id", sc.get("query_id", "?"))
        name = sc.get("scenario_name", sc.get("query_text", ""))
        before_key = f"{sid}_before"
        after_key = f"{sid}_after"

        if before_key not in scores or after_key not in scores:
            print(f"\n  {sid}: SKIP (scores not found for {before_key} or {after_key})")
            continue

        b_rel = scores[before_key]
        a_rel = scores[after_key]

        b_ndcg = ndcg_at_k(b_rel, top_k)
        a_ndcg = ndcg_at_k(a_rel, top_k)
        b_mrr_val = mrr(b_rel)
        a_mrr_val = mrr(a_rel)
        b_prec = precision_at_k(b_rel, top_k)
        a_prec = precision_at_k(a_rel, top_k)
        b_avg = avg_relevance(b_rel)
        a_avg = avg_relevance(a_rel)

        delta_ndcg = a_ndcg - b_ndcg
        delta_avg = a_avg - b_avg

        rounds = sc.get("training_rounds", "?")
        total_q = sc.get("total_training_queries", "?")
        edges = sc.get("edges_formed", "?")
        new_count = len(sc.get("new_docs", []))
        dropped_count = len(sc.get("dropped_docs", []))

        print(f"\n  {sid}: {name}")
        print(f"    Training: {rounds} rounds, {total_q} queries, {edges} edges formed")
        print(f"    New docs in top-{top_k}: {new_count}, Dropped: {dropped_count}")
        print(f"    {'':18s} {'nDCG@10':>8s} {'MRR':>8s} {'P@10':>8s} {'Avg Rel':>8s}")
        print(f"    {'Before (static)':18s} {b_ndcg:>8.4f} {b_mrr_val:>8.4f} {b_prec:>8.4f} {b_avg:>8.2f}")
        print(f"    {'After (GER-RAG)':18s} {a_ndcg:>8.4f} {a_mrr_val:>8.4f} {a_prec:>8.4f} {a_avg:>8.2f}")
        sign = "+" if delta_ndcg >= 0 else ""
        print(f"    {'Delta':18s} {sign}{delta_ndcg:>7.4f} {'':8s} {'':8s} {delta_avg:>+8.2f}")
        print(f"    Before scores: {b_rel}")
        print(f"    After scores:  {a_rel}")

        all_sessions.append({
            "scenario_id": sid,
            "scenario_name": name,
            "training_rounds": rounds,
            "total_training_queries": total_q,
            "edges_formed": edges,
            "new_docs": new_count,
            "dropped_docs": dropped_count,
            "before": {"ndcg": b_ndcg, "mrr": b_mrr_val, "precision": b_prec, "avg_rel": b_avg, "relevances": b_rel},
            "after": {"ndcg": a_ndcg, "mrr": a_mrr_val, "precision": a_prec, "avg_rel": a_avg, "relevances": a_rel},
            "delta_ndcg": delta_ndcg,
            "delta_avg_rel": delta_avg,
        })

    if all_sessions:
        avg_delta = statistics.mean(s["delta_ndcg"] for s in all_sessions)
        improving = sum(1 for s in all_sessions if s["delta_ndcg"] > 0.001)
        print(f"\n  {'='*70}")
        print(f"  Average nDCG delta (Before→After): {avg_delta:+.4f}")
        print(f"  Scenarios improving: {improving}/{len(all_sessions)}")

    return {"sessions": all_sessions}


def main():
    parser = argparse.ArgumentParser(description="GER-RAG 評価メトリクス算出")
    parser.add_argument("--dir", default="eval_output", help="eval_export.py の出力ディレクトリ")
    parser.add_argument("--output", default=None, help="結果をJSONに保存")
    args = parser.parse_args()

    results_path = os.path.join(args.dir, "results.json")
    scores_path = os.path.join(args.dir, "judge_scores.json")
    session_results_path = os.path.join(args.dir, "session_results.json")
    session_scores_path = os.path.join(args.dir, "session_scores.json")

    if not os.path.exists(results_path):
        print(f"ERROR: {results_path} not found. Run eval_export.py first.")
        sys.exit(1)

    if not os.path.exists(scores_path):
        print(f"ERROR: {scores_path} not found.")
        print(f"  1. {os.path.join(args.dir, 'judge_prompt.md')} をLLMに渡して判定してもらう")
        print(f"  2. 判定結果のJSONを {scores_path} に保存する")
        print(f"")
        print(f"  例:")
        print(f'  {{')
        print(f'    "Q01_static": [3, 2, 1, 0, 1, 2, 0, 1, 0, 0],')
        print(f'    "Q01_ger": [3, 2, 2, 1, 1, 0, 1, 0, 0, 0],')
        print(f'    ...')
        print(f'  }}')
        sys.exit(1)

    with open(results_path, encoding="utf-8") as f:
        results_data = json.load(f)
    with open(scores_path, encoding="utf-8") as f:
        scores = json.load(f)

    top_k = results_data.get("top_k", 10)

    report = {}
    report.update(print_comparison_report(results_data, scores, top_k))

    # Session adaptivity (optional)
    if os.path.exists(session_results_path) and os.path.exists(session_scores_path):
        with open(session_results_path, encoding="utf-8") as f:
            session_data = json.load(f)
        with open(session_scores_path, encoding="utf-8") as f:
            session_scores = json.load(f)
        report.update(print_session_report(session_data, session_scores, top_k))
    elif os.path.exists(session_results_path):
        print(f"\n  Session data found but {session_scores_path} missing.")
        print(f"  Judge session_judge_prompt.md and save scores to {session_scores_path}")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"\n  Report saved to {args.output}")


if __name__ == "__main__":
    main()
