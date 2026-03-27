"""Run test queries against GER-RAG and display results.

Usage:
    python scripts/test_queries.py [--url URL] [--rounds N] [--mode basic|full|stress]

Modes:
    basic  - 5 queries (default, quick check)
    full   - 30+ diverse queries across many topics
    stress - full queries x many rounds, builds up mass/temperature/co-occurrence fast
"""

from __future__ import annotations

import argparse
import random
import time

import httpx

DEFAULT_URL = "http://localhost:8000"

# Basic set (original)
BASIC_QUERIES = [
    "人工知能と機械学習について",
    "映画やアニメの感想",
    "プログラミングや技術的な話題",
    "日常の面白い出来事",
    "紅茶や食べ物の話",
]

# Diverse query set covering many topics likely in the tweet dataset
FULL_QUERIES = [
    # --- Tech & AI ---
    "ChatGPTやLLMの活用方法",
    "機械学習のモデル学習とGPU",
    "プログラミング言語の比較、PythonやJavaScript",
    "ソフトウェア開発の設計思想",
    "AIによる創作と著作権の問題",
    "VRやメタバースの体験",
    "個人開発やサービス運営の話",

    # --- Culture & Entertainment ---
    "映画の感想、特にSFやアクション",
    "アニメや漫画のおすすめ作品",
    "ゲームの攻略や感想",
    "音楽の趣味やライブの思い出",
    "読書や本の紹介、小説やノンフィクション",
    "マッドマックスのような名作映画",

    # --- Food & Drink ---
    "紅茶やコーヒーの淹れ方",
    "美味しいお店やレストランの話",
    "料理のレシピや自炊のコツ",
    "お酒やビールを楽しむ話",
    "スイーツやケーキの話",

    # --- Life & Philosophy ---
    "仕事のモチベーションや働き方",
    "人間関係やコミュニケーションの悩み",
    "創造性とネガティブ思考の対比",
    "日常の小さな幸せ",
    "深夜のひとりごとや内省",
    "学生時代の思い出",

    # --- Society & News ---
    "SNSとの付き合い方",
    "歴史や文化に関する雑学",
    "旅行や観光地の話",
    "政治や社会問題への意見",

    # --- Misc / Humor ---
    "面白いツイートやネタ",
    "猫や動物の可愛い話",
    "季節や天気の話題",
    "祝日や休みの過ごし方",

    # --- Overlapping topics (builds co-occurrence) ---
    "AIを使ったプログラミングと創作",
    "映画を見ながら紅茶を飲む休日",
    "技術的な挑戦と自己成長",
    "好きな食べ物と幸せな気持ち",
    "深夜に見るアニメと感想",
]

# Focused bursts - same topic queried repeatedly to build mass
BURST_QUERIES = [
    ("AI集中", [
        "人工知能の未来",
        "機械学習の実用例",
        "AIと人間の共存",
        "ディープラーニングの仕組み",
        "ChatGPTの使い方と限界",
    ]),
    ("映画集中", [
        "最近見た映画の感想",
        "映画館での体験",
        "SF映画のおすすめ",
        "感動する映画の名シーン",
        "アニメ映画と実写映画の違い",
    ]),
    ("食べ物集中", [
        "美味しい紅茶の銘柄",
        "ランチのおすすめ",
        "自炊で作る簡単レシピ",
        "カフェでの時間の過ごし方",
        "お気に入りのスイーツ",
    ]),
]


def query(client: httpx.Client, url: str, text: str, top_k: int = 10) -> dict:
    resp = client.post(f"{url}/query", json={"text": text, "top_k": top_k}, timeout=60.0)
    resp.raise_for_status()
    return resp.json()


def show_node(client: httpx.Client, url: str, node_id: str) -> None:
    resp = client.get(f"{url}/node/{node_id}", timeout=10.0)
    if resp.status_code == 200:
        d = resp.json()
        print(f"    mass={d['mass']:.3f} temp={d['temperature']:.6f} history={len(d['sim_history'])}")


def show_graph_summary(client: httpx.Client, url: str) -> None:
    resp = client.get(f"{url}/graph?min_weight=0.1", timeout=10.0)
    if resp.status_code == 200:
        data = resp.json()
        total = data["count"]
        if total == 0:
            print("  Graph: no edges yet")
            return
        weights = [e["weight"] for e in data["edges"]]
        print(f"  Graph: {total} edges (min_w>=0.1), "
              f"max_weight={max(weights):.1f}, avg={sum(weights)/len(weights):.1f}")
        # Show top edges
        top = sorted(data["edges"], key=lambda e: e["weight"], reverse=True)[:5]
        for e in top:
            print(f"    {e['src'][:8]}.. <-> {e['dst'][:8]}.. w={e['weight']:.1f}")


def run_queries(client: httpx.Client, url: str, queries: list[str],
                top_k: int, verbose: bool = True) -> int:
    """Run a batch of queries. Returns total results."""
    total_results = 0
    for q in queries:
        result = query(client, url, q, top_k)
        total_results += result["count"]
        if verbose:
            top1 = result["results"][0] if result["results"] else None
            preview = top1["content"][:60].replace("\n", " ") if top1 else "(no results)"
            score = f"score={top1['final_score']:.4f}" if top1 else ""
            print(f"  {q[:30]:30s} -> {result['count']} hits  {score}  {preview}")
    return total_results


def mode_basic(client: httpx.Client, url: str, args):
    print("Mode: basic (5 queries)")
    for round_num in range(1, args.rounds + 1):
        print(f"\n{'='*60}")
        print(f"Round {round_num}/{args.rounds}")
        print(f"{'='*60}")
        run_queries(client, url, BASIC_QUERIES, args.top_k)
        show_graph_summary(client, url)


def mode_full(client: httpx.Client, url: str, args):
    print(f"Mode: full ({len(FULL_QUERIES)} queries)")
    for round_num in range(1, args.rounds + 1):
        print(f"\n{'='*60}")
        print(f"Round {round_num}/{args.rounds} - Diverse queries")
        print(f"{'='*60}")

        # Shuffle to vary co-occurrence patterns each round
        shuffled = list(FULL_QUERIES)
        random.shuffle(shuffled)
        run_queries(client, url, shuffled, args.top_k)

        print()
        show_graph_summary(client, url)

    # Final: show top nodes by mass
    print(f"\n{'='*60}")
    print("Top nodes by mass (most frequently retrieved):")
    print(f"{'='*60}")
    for q in ["人工知能", "映画", "紅茶", "プログラミング", "日常"]:
        result = query(client, url, q, 3)
        if result["results"]:
            top = result["results"][0]
            print(f"\n  '{q}' top hit:")
            print(f"    {top['content'][:70].replace(chr(10), ' ')}...")
            show_node(client, url, top["id"])


def mode_stress(client: httpx.Client, url: str, args):
    total_queries = 0
    start = time.time()

    print(f"Mode: stress ({args.rounds} rounds x {len(FULL_QUERIES)} diverse + bursts)")

    for round_num in range(1, args.rounds + 1):
        print(f"\nRound {round_num}/{args.rounds}", end="")

        # Full diverse pass (quiet)
        shuffled = list(FULL_QUERIES)
        random.shuffle(shuffled)
        n = run_queries(client, url, shuffled, args.top_k, verbose=False)
        total_queries += len(shuffled)

        # Burst passes - focused queries to build mass in specific clusters
        for burst_name, burst_qs in BURST_QUERIES:
            for _ in range(3):  # repeat each burst 3 times
                run_queries(client, url, burst_qs, args.top_k, verbose=False)
                total_queries += len(burst_qs)

        elapsed = time.time() - start
        print(f"  ({total_queries} queries, {elapsed:.1f}s)")

        # Show summary every 3 rounds
        if round_num % 3 == 0 or round_num == args.rounds:
            show_graph_summary(client, url)

    elapsed = time.time() - start
    print(f"\n{'='*60}")
    print(f"Stress test complete: {total_queries} queries in {elapsed:.1f}s "
          f"({total_queries/elapsed:.1f} q/s)")
    print(f"{'='*60}")

    # Show most impacted nodes
    print("\nMost impacted nodes:")
    for q in ["ChatGPTやLLM", "映画の感想", "紅茶やコーヒー", "プログラミング", "面白いツイート"]:
        result = query(client, url, q, 1)
        if result["results"]:
            r = result["results"][0]
            print(f"  '{q}': score={r['final_score']:.4f}")
            show_node(client, url, r["id"])

    show_graph_summary(client, url)


def main():
    parser = argparse.ArgumentParser(description="GER-RAG test query runner")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--mode", choices=["basic", "full", "stress"], default="basic",
                        help="basic=5 queries, full=30+ diverse, stress=heavy load for visualization")
    args = parser.parse_args()

    with httpx.Client() as client:
        # Check server is up
        try:
            client.get(f"{args.url}/docs", timeout=5.0)
        except httpx.ConnectError:
            print(f"ERROR: Cannot connect to {args.url}. Is the server running?")
            raise SystemExit(1)

        if args.mode == "basic":
            mode_basic(client, args.url, args)
        elif args.mode == "full":
            mode_full(client, args.url, args)
        elif args.mode == "stress":
            mode_stress(client, args.url, args)


if __name__ == "__main__":
    main()
