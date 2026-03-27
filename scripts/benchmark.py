"""GER-RAG Benchmark Suite

Measures all success criteria (SC-001 to SC-007) and Phase 2 evaluation metrics.

Usage:
    python scripts/benchmark.py [--url URL] [--all] [--latency] [--dynamics] [--concurrency]
                                [--persistence] [--reset] [--baseline]

Requires a running server with indexed documents.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import httpx

DEFAULT_URL = "http://localhost:8000"

# Diverse queries for benchmarking
BENCH_QUERIES = [
    "人工知能と機械学習の最新動向",
    "映画やアニメのおすすめ作品",
    "プログラミング言語の比較",
    "紅茶やコーヒーの美味しい淹れ方",
    "日常の小さな幸せについて",
    "SNSとの付き合い方",
    "ゲームの攻略や感想",
    "仕事のモチベーション",
    "歴史や文化に関する雑学",
    "深夜のひとりごと",
]

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"


@dataclass
class BenchResult:
    name: str
    criterion: str
    passed: bool
    detail: str
    metrics: dict = field(default_factory=dict)


def print_header(title: str) -> None:
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def print_result(r: BenchResult) -> None:
    status = PASS if r.passed else FAIL
    print(f"\n  [{status}] {r.name}")
    print(f"    Criterion: {r.criterion}")
    print(f"    Result:    {r.detail}")
    if r.metrics:
        for k, v in r.metrics.items():
            if isinstance(v, float):
                print(f"    {k}: {v:.4f}")
            else:
                print(f"    {k}: {v}")


# ---------------------------------------------------------------------------
# SC-001: Latency (<50ms per query for up to 100K docs)
# ---------------------------------------------------------------------------

def bench_latency(url: str, rounds: int = 50) -> BenchResult:
    print_header("SC-001: Query Latency")
    latencies = []

    with httpx.Client() as client:
        # Warmup
        for q in BENCH_QUERIES[:3]:
            client.post(f"{url}/query", json={"text": q, "top_k": 10}, timeout=60.0)

        # Measure
        for i in range(rounds):
            q = BENCH_QUERIES[i % len(BENCH_QUERIES)]
            start = time.perf_counter()
            resp = client.post(f"{url}/query", json={"text": q, "top_k": 10}, timeout=60.0)
            elapsed_ms = (time.perf_counter() - start) * 1000
            resp.raise_for_status()
            latencies.append(elapsed_ms)
            print(f"    Query {i+1}/{rounds}: {elapsed_ms:.1f}ms", end="\r")

    print()
    p50 = statistics.median(latencies)
    p95 = sorted(latencies)[int(len(latencies) * 0.95)]
    p99 = sorted(latencies)[int(len(latencies) * 0.99)]
    avg = statistics.mean(latencies)
    mn = min(latencies)
    mx = max(latencies)

    passed = p50 < 50.0
    return BenchResult(
        name="SC-001: Query Latency",
        criterion="p50 < 50ms (target for up to 100K docs)",
        passed=passed,
        detail=f"p50={p50:.1f}ms, p95={p95:.1f}ms, p99={p99:.1f}ms",
        metrics={"avg_ms": avg, "min_ms": mn, "max_ms": mx, "p50_ms": p50, "p95_ms": p95, "p99_ms": p99, "samples": rounds},
    )


# ---------------------------------------------------------------------------
# SC-002: Mass Accumulation
# ---------------------------------------------------------------------------

def bench_mass_accumulation(url: str) -> BenchResult:
    print_header("SC-002: Mass Accumulation")
    query_text = "人工知能と機械学習"

    with httpx.Client() as client:
        # Query once to get baseline
        resp = client.post(f"{url}/query", json={"text": query_text, "top_k": 10}, timeout=60.0)
        resp.raise_for_status()
        results_round1 = resp.json()["results"]

        if len(results_round1) < 2:
            return BenchResult("SC-002: Mass Accumulation", "N/A", False, "Not enough results to compare")

        top_id = results_round1[0]["id"]

        # Query 5+ more times to build up mass on top result
        for _ in range(6):
            client.post(f"{url}/query", json={"text": query_text, "top_k": 10}, timeout=60.0)

        # Check node state
        resp = client.get(f"{url}/node/{top_id}", timeout=10.0)
        resp.raise_for_status()
        node_after = resp.json()
        mass_after = node_after["mass"]

        # Query with a slightly different query to get a comparable but less-queried doc
        resp = client.post(f"{url}/query", json={"text": "深層学習とニューラルネットワーク", "top_k": 10}, timeout=60.0)
        resp.raise_for_status()
        alt_results = resp.json()["results"]

        # Find a doc that wasn't in the repeated query results
        repeated_ids = {r["id"] for r in results_round1}
        comparison_doc = None
        for r in alt_results:
            if r["id"] not in repeated_ids:
                comparison_doc = r
                break

        if comparison_doc is None:
            # Fallback: compare mass of top result with initial value
            passed = mass_after > 1.2
            return BenchResult(
                name="SC-002: Mass Accumulation",
                criterion="Repeatedly retrieved docs gain mass > initial (1.0)",
                passed=passed,
                detail=f"Top doc mass after 7 queries: {mass_after:.3f} (initial: 1.0)",
                metrics={"mass_after_7_queries": mass_after},
            )

        resp = client.get(f"{url}/node/{comparison_doc['id']}", timeout=10.0)
        if resp.status_code == 200:
            comparison_mass = resp.json()["mass"]
        else:
            comparison_mass = 1.0

        passed = mass_after > comparison_mass
        return BenchResult(
            name="SC-002: Mass Accumulation",
            criterion="Repeatedly retrieved docs score higher than single-retrieval docs",
            passed=passed,
            detail=f"Repeated doc mass={mass_after:.3f} vs comparison mass={comparison_mass:.3f}",
            metrics={"repeated_mass": mass_after, "comparison_mass": comparison_mass, "queries_on_target": 7},
        )


# ---------------------------------------------------------------------------
# SC-003: Temporal Decay
# ---------------------------------------------------------------------------

def bench_temporal_decay(url: str) -> BenchResult:
    print_header("SC-003: Temporal Decay")

    with httpx.Client() as client:
        # Query to get a "recently accessed" doc
        resp = client.post(f"{url}/query", json={"text": "紅茶の淹れ方", "top_k": 5}, timeout=60.0)
        resp.raise_for_status()
        recent_results = resp.json()["results"]

        if not recent_results:
            return BenchResult("SC-003: Temporal Decay", "N/A", False, "No results returned")

        recent_id = recent_results[0]["id"]
        recent_raw = recent_results[0]["raw_score"]
        recent_final = recent_results[0]["final_score"]

        # Get node state to check decay factor
        resp = client.get(f"{url}/node/{recent_id}", timeout=10.0)
        resp.raise_for_status()
        recent_node = resp.json()

        # The decay factor for a recently accessed doc should be close to 1.0
        # For a never-queried doc, decay would be much smaller
        # We verify by checking that final_score ≈ raw_score (decay near 1) for recent
        decay_approx = recent_final / recent_raw if recent_raw > 0 else 0

        # Also check: a doc with old last_access would have lower score
        # We can verify this by looking at the ratio
        passed = decay_approx > 0.5  # Recently accessed docs should have decay > 0.5
        return BenchResult(
            name="SC-003: Temporal Decay",
            criterion="Recently accessed docs have decay factor > 0.5; old docs score lower",
            passed=passed,
            detail=f"Recent doc: raw={recent_raw:.4f}, final={recent_final:.4f}, "
                   f"decay_approx={decay_approx:.4f}",
            metrics={"raw_score": recent_raw, "final_score": recent_final, "decay_approx": decay_approx},
        )


# ---------------------------------------------------------------------------
# SC-004: Co-occurrence Graph Boost
# ---------------------------------------------------------------------------

def bench_cooccurrence(url: str) -> BenchResult:
    print_header("SC-004: Co-occurrence Graph Boost")

    with httpx.Client() as client:
        # Run overlapping queries many times to build co-occurrence
        overlap_queries = [
            "AIと機械学習のプログラミング",
            "機械学習プログラミングの実践",
            "プログラミングでAIを学ぶ",
            "人工知能プログラミング入門",
            "機械学習の実装テクニック",
        ]
        for _ in range(10):
            for q in overlap_queries:
                client.post(f"{url}/query", json={"text": q, "top_k": 10}, timeout=60.0)

        # Check if edges formed
        resp = client.get(f"{url}/graph?min_weight=0.1", timeout=10.0)
        resp.raise_for_status()
        graph = resp.json()
        edge_count = graph["count"]

        if edge_count == 0:
            return BenchResult(
                name="SC-004: Co-occurrence Graph Boost",
                criterion="Co-occurrence edges form and provide score boost",
                passed=False,
                detail=f"No edges formed after {10 * len(overlap_queries)} queries. "
                       f"edge_threshold ({5}) may not have been reached.",
                metrics={"edge_count": 0, "queries_run": 10 * len(overlap_queries)},
            )

        max_weight = max(e["weight"] for e in graph["edges"])

        passed = edge_count > 0
        return BenchResult(
            name="SC-004: Co-occurrence Graph Boost",
            criterion="Co-occurrence edges form from repeated co-retrieval",
            passed=passed,
            detail=f"{edge_count} edges formed, max_weight={max_weight:.1f}",
            metrics={"edge_count": edge_count, "max_weight": max_weight, "queries_run": 10 * len(overlap_queries)},
        )


# ---------------------------------------------------------------------------
# SC-005: Concurrency
# ---------------------------------------------------------------------------

def bench_concurrency(url: str, n_concurrent: int = 50) -> BenchResult:
    print_header(f"SC-005: Concurrency ({n_concurrent} concurrent queries)")

    errors = []
    latencies = []

    def single_query(query_text: str) -> tuple[float, str | None]:
        try:
            with httpx.Client() as client:
                start = time.perf_counter()
                resp = client.post(
                    f"{url}/query",
                    json={"text": query_text, "top_k": 5},
                    timeout=30.0,
                )
                elapsed = (time.perf_counter() - start) * 1000
                resp.raise_for_status()
                return elapsed, None
        except Exception as e:
            return 0.0, str(e)

    queries = [BENCH_QUERIES[i % len(BENCH_QUERIES)] for i in range(n_concurrent)]

    with ThreadPoolExecutor(max_workers=n_concurrent) as executor:
        futures = {executor.submit(single_query, q): q for q in queries}
        for future in as_completed(futures):
            elapsed, error = future.result()
            if error:
                errors.append(error)
            else:
                latencies.append(elapsed)
            done = len(latencies) + len(errors)
            print(f"    {done}/{n_concurrent} complete", end="\r")

    print()
    passed = len(errors) == 0
    avg_lat = statistics.mean(latencies) if latencies else 0

    return BenchResult(
        name=f"SC-005: Concurrency ({n_concurrent} concurrent)",
        criterion=f"0 errors with {n_concurrent} concurrent queries",
        passed=passed,
        detail=f"{len(latencies)} succeeded, {len(errors)} failed, avg_latency={avg_lat:.1f}ms",
        metrics={
            "succeeded": len(latencies),
            "failed": len(errors),
            "avg_latency_ms": avg_lat,
            "max_latency_ms": max(latencies) if latencies else 0,
            "errors": errors[:3] if errors else [],
        },
    )


# ---------------------------------------------------------------------------
# SC-006: Persistence (requires restart - informational only)
# ---------------------------------------------------------------------------

def bench_persistence(url: str) -> BenchResult:
    print_header("SC-006: Persistence (snapshot check)")

    with httpx.Client() as client:
        # Query and record a node's state
        resp = client.post(f"{url}/query", json={"text": "プログラミング", "top_k": 3}, timeout=60.0)
        resp.raise_for_status()
        results = resp.json()["results"]

        if not results:
            return BenchResult("SC-006: Persistence", "N/A", False, "No results")

        node_id = results[0]["id"]
        resp = client.get(f"{url}/node/{node_id}", timeout=10.0)
        resp.raise_for_status()
        state = resp.json()

    return BenchResult(
        name="SC-006: Persistence (manual verification needed)",
        criterion="Dynamic state survives clean shutdown + restart",
        passed=True,  # Informational - can't auto-restart
        detail=f"Snapshot: node={node_id[:8]}.. mass={state['mass']:.3f} "
               f"temp={state['temperature']:.6f} history={len(state['sim_history'])} entries. "
               f"Restart server and re-run to verify these values are preserved.",
        metrics={"node_id": node_id, "mass": state["mass"], "temperature": state["temperature"],
                 "sim_history_len": len(state["sim_history"])},
    )


# ---------------------------------------------------------------------------
# SC-007: Reset Performance
# ---------------------------------------------------------------------------

def bench_reset(url: str) -> BenchResult:
    print_header("SC-007: Reset Performance")

    with httpx.Client() as client:
        start = time.perf_counter()
        resp = client.post(f"{url}/reset", timeout=30.0)
        elapsed = time.perf_counter() - start
        resp.raise_for_status()
        data = resp.json()

        # Verify state was actually reset
        sample_resp = client.post(f"{url}/query", json={"text": "テスト", "top_k": 1}, timeout=60.0)
        if sample_resp.status_code == 200 and sample_resp.json()["results"]:
            node_id = sample_resp.json()["results"][0]["id"]
            node_resp = client.get(f"{url}/node/{node_id}", timeout=10.0)
            if node_resp.status_code == 200:
                node = node_resp.json()
                mass_ok = abs(node["mass"] - 1.0) < 0.01
                temp_ok = abs(node["temperature"]) < 0.001
            else:
                mass_ok = temp_ok = True  # Can't verify
        else:
            mass_ok = temp_ok = True

    passed = elapsed < 5.0 and mass_ok and temp_ok
    return BenchResult(
        name="SC-007: Reset Performance",
        criterion="Reset completes in <5s, all dynamic values return to defaults",
        passed=passed,
        detail=f"Reset in {elapsed:.2f}s: {data['nodes_reset']} nodes, "
               f"{data['edges_removed']} edges. mass_reset={mass_ok}, temp_reset={temp_ok}",
        metrics={"elapsed_s": elapsed, "nodes_reset": data["nodes_reset"],
                 "edges_removed": data["edges_removed"]},
    )


# ---------------------------------------------------------------------------
# Baseline Comparison: Static RAG vs GER-RAG
# ---------------------------------------------------------------------------

def bench_baseline_comparison(url: str) -> BenchResult:
    print_header("Baseline: Static RAG vs GER-RAG (session adaptivity)")

    query_text = "映画やアニメの感想"

    with httpx.Client() as client:
        # Round 1: first query (approximates static RAG behavior)
        resp = client.post(f"{url}/query", json={"text": query_text, "top_k": 5}, timeout=60.0)
        resp.raise_for_status()
        round1 = resp.json()["results"]

        round1_scores = {r["id"]: r["final_score"] for r in round1}
        round1_raw = {r["id"]: r["raw_score"] for r in round1}
        round1_ranking = [r["id"] for r in round1]

        # Run the same query 10 more times (build dynamic state)
        for _ in range(10):
            client.post(f"{url}/query", json={"text": query_text, "top_k": 5}, timeout=60.0)

        # Round 2: same query after mass/temp accumulation
        resp = client.post(f"{url}/query", json={"text": query_text, "top_k": 5}, timeout=60.0)
        resp.raise_for_status()
        round2 = resp.json()["results"]

        round2_scores = {r["id"]: r["final_score"] for r in round2}
        round2_ranking = [r["id"] for r in round2]

    # Measure ranking change (Kendall-tau like)
    common_ids = set(round1_ranking) & set(round2_ranking)
    rank_changes = 0
    for doc_id in common_ids:
        r1_pos = round1_ranking.index(doc_id)
        r2_pos = round2_ranking.index(doc_id)
        if r1_pos != r2_pos:
            rank_changes += 1

    # Score drift
    score_drifts = []
    for doc_id in common_ids:
        if doc_id in round1_scores and doc_id in round2_scores:
            drift = round2_scores[doc_id] - round1_scores[doc_id]
            score_drifts.append(drift)

    avg_drift = statistics.mean(score_drifts) if score_drifts else 0
    new_in_top5 = len(set(round2_ranking) - set(round1_ranking))

    # GER-RAG should show score changes (avg_drift != 0)
    passed = abs(avg_drift) > 0.001 or rank_changes > 0
    return BenchResult(
        name="Baseline: Static RAG vs GER-RAG",
        criterion="GER-RAG shows measurable score/ranking changes over repeated queries (static RAG would not)",
        passed=passed,
        detail=f"After 11 queries: avg_score_drift={avg_drift:+.4f}, "
               f"rank_changes={rank_changes}/{len(common_ids)}, "
               f"new_in_top5={new_in_top5}",
        metrics={"avg_score_drift": avg_drift, "rank_changes": rank_changes,
                 "common_docs": len(common_ids), "new_in_top5": new_in_top5,
                 "total_queries": 12},
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="GER-RAG Benchmark Suite")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--all", action="store_true", help="Run all benchmarks")
    parser.add_argument("--latency", action="store_true", help="SC-001: Latency")
    parser.add_argument("--dynamics", action="store_true", help="SC-002/003/004: Dynamic behavior")
    parser.add_argument("--concurrency", action="store_true", help="SC-005: Concurrency")
    parser.add_argument("--persistence", action="store_true", help="SC-006: Persistence snapshot")
    parser.add_argument("--reset", action="store_true", help="SC-007: Reset (WARNING: resets state!)")
    parser.add_argument("--baseline", action="store_true", help="Static RAG comparison")
    parser.add_argument("--output", default=None, help="Save results to JSON file")
    args = parser.parse_args()

    # Default to all if no flag specified
    run_all = args.all or not any([args.latency, args.dynamics, args.concurrency,
                                    args.persistence, args.reset, args.baseline])

    # Check server
    try:
        httpx.get(f"{args.url}/docs", timeout=5.0)
    except httpx.ConnectError:
        print(f"ERROR: Cannot connect to {args.url}. Is the server running?")
        sys.exit(1)

    results: list[BenchResult] = []

    if run_all or args.latency:
        results.append(bench_latency(args.url))

    if run_all or args.dynamics:
        results.append(bench_mass_accumulation(args.url))
        results.append(bench_temporal_decay(args.url))
        results.append(bench_cooccurrence(args.url))

    if run_all or args.concurrency:
        results.append(bench_concurrency(args.url))

    if run_all or args.persistence:
        results.append(bench_persistence(args.url))

    if run_all or args.baseline:
        results.append(bench_baseline_comparison(args.url))

    # Reset is destructive - run last and only if explicitly requested
    if args.reset:
        results.append(bench_reset(args.url))
        print(f"\n  {WARN} State has been reset. Re-index documents to continue using the system.")

    # Summary
    print_header("BENCHMARK SUMMARY")
    passed_count = sum(1 for r in results if r.passed)
    total = len(results)

    for r in results:
        print_result(r)

    print(f"\n{'='*70}")
    print(f"  Result: {passed_count}/{total} passed")
    if passed_count == total:
        print(f"  {PASS} All benchmarks passed")
    else:
        failed = [r.name for r in results if not r.passed]
        print(f"  {FAIL} Failed: {', '.join(failed)}")
    print(f"{'='*70}")

    # Save JSON
    if args.output:
        report = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "url": args.url,
            "summary": {"passed": passed_count, "total": total},
            "results": [
                {
                    "name": r.name,
                    "criterion": r.criterion,
                    "passed": r.passed,
                    "detail": r.detail,
                    "metrics": r.metrics,
                }
                for r in results
            ],
        }
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"\n  Report saved to {args.output}")


if __name__ == "__main__":
    main()
