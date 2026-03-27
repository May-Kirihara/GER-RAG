"""Step 1: GER-RAG評価データ書き出し

サーバーにクエリを投げ、静的RAG順位とGER-RAG順位の両方を記録し、
LLM判定用のプロンプトファイルを生成する。

Usage:
    python scripts/eval_export.py [--url URL] [--out-dir eval_output]
    python scripts/eval_export.py --session --rounds 20 [--out-dir eval_output]

Output:
    eval_output/
    ├── results.json              # 生データ（クエリ結果の全記録）
    ├── judge_prompt.md           # LLMに渡す判定プロンプト
    ├── session_results.json      # セッション適応性データ
    └── session_judge_prompt.md   # セッション判定プロンプト
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

DEFAULT_URL = "http://localhost:8000"

EVAL_QUERIES = [
    {"id": "Q01", "text": "人工知能や機械学習の技術的な話題", "intent": "AI・機械学習に関する技術的な議論やニュース"},
    {"id": "Q02", "text": "映画やアニメを見た感想・レビュー", "intent": "映画・アニメの視聴感想や批評"},
    {"id": "Q03", "text": "プログラミングやソフトウェア開発の話", "intent": "コーディング、言語、開発手法に関する話題"},
    {"id": "Q04", "text": "紅茶やコーヒー、食べ物の話題", "intent": "飲食に関する感想・レシピ・お店の話"},
    {"id": "Q05", "text": "日常生活で起きた出来事", "intent": "日常のエピソード・雑談・つぶやき"},
    {"id": "Q06", "text": "仕事やキャリアについての考え", "intent": "働き方・モチベーション・キャリアの悩み"},
    {"id": "Q07", "text": "ゲームの感想やおすすめ", "intent": "ゲームプレイの感想・レビュー・攻略"},
    {"id": "Q08", "text": "読書や本の紹介", "intent": "本の感想・おすすめ・読書体験"},
    {"id": "Q09", "text": "SNSやインターネット文化について", "intent": "SNSの使い方・ネット文化・デジタルライフ"},
    {"id": "Q10", "text": "旅行や観光の体験", "intent": "旅行記・観光地の紹介・移動の話"},
]

# セッション適応性シナリオ
#
# 設計方針:
#   - training_top_k を大きく（50）して、広い範囲のドキュメントに共起を形成
#   - 多様なクエリで異なるクラスタの文書を同時にtop-Kに引き込む
#   - observe_top_k は通常の10で、「元のtop-10に入っていなかった文書が浮上するか」を見る
#   - 架橋クエリ（2つのトピックをまたぐ）を含めて共起ネットワークを形成
SESSION_SCENARIOS = [
    {
        "id": "S1",
        "name": "AI×プログラミング×仕事 クラスタ架橋",
        "description": "AI・プログラミング・仕事を横断するクエリで共起を蓄積し、「仕事」検索にAI/プログラミング関連が浮上するか",
        "training_queries": [
            # 単独トピック（各クラスタのノードにmass蓄積）
            "人工知能と機械学習の最新動向",
            "プログラミング言語の比較",
            "仕事のモチベーションとキャリア",
            # 架橋クエリ（異なるクラスタ間に共起を形成）
            "AIエンジニアとして働くこと",
            "プログラミングスキルとキャリアアップ",
            "機械学習エンジニアの日常業務",
            "技術者の仕事の面白さ",
            "AIを活用した業務効率化",
            "開発者のワークライフバランス",
            "テック業界の転職とスキル",
        ],
        "observe_query": {"text": "仕事やキャリアについての考え", "intent": "働き方・モチベーション・キャリアの悩み"},
    },
    {
        "id": "S2",
        "name": "映画×食べ物×旅行 クラスタ架橋",
        "description": "映画・食べ物・旅行を横断するクエリで共起を蓄積し、「旅行」検索に映画/食文化関連が浮上するか",
        "training_queries": [
            # 単独トピック
            "映画やアニメのおすすめ",
            "美味しいお店やレストラン",
            "旅行や観光地の思い出",
            # 架橋クエリ
            "旅先で食べた美味しいもの",
            "映画のロケ地を巡る旅",
            "海外の食文化と映画",
            "観光地のカフェやレストラン",
            "映画に出てくる料理と旅",
            "食べ歩きの旅行記",
            "聖地巡礼と現地グルメ",
        ],
        "observe_query": {"text": "旅行や観光の体験", "intent": "旅行記・観光地の紹介・移動の話"},
    },
    {
        "id": "S3",
        "name": "SNS×ゲーム×日常 クラスタ架橋",
        "description": "SNS・ゲーム・日常を横断するクエリで共起を蓄積し、「SNS」検索にゲーム/日常関連が浮上するか",
        "training_queries": [
            # 単独トピック
            "SNSやTwitterの使い方",
            "ゲームの感想や攻略",
            "日常の面白い出来事",
            # 架橋クエリ
            "SNSでゲームの感想を共有",
            "Twitterで見つけた面白いネタ",
            "ゲーム実況とSNSの反応",
            "日常をSNSに投稿すること",
            "ネットで話題のゲーム",
            "SNSでバズった日常ツイート",
            "オンラインゲームの人間関係",
        ],
        "observe_query": {"text": "SNSやインターネット文化について", "intent": "SNSの使い方・ネット文化・デジタルライフ"},
    },
]

# トレーニング時は広い範囲の文書に共起を形成するため大きなtop_kを使う
TRAINING_TOP_K = 50


def query_server(client: httpx.Client, url: str, text: str, top_k: int = 10) -> list[dict]:
    resp = client.post(f"{url}/query", json={"text": text, "top_k": top_k}, timeout=60.0)
    resp.raise_for_status()
    return resp.json()["results"]


def collect_comparison_data(url: str, top_k: int) -> list[dict]:
    """各クエリに対しGER-RAG順と静的順の結果を収集する。"""
    records = []

    with httpx.Client() as client:
        for q in EVAL_QUERIES:
            print(f"  {q['id']}: {q['text']}")
            results = query_server(client, url, q["text"], top_k)

            ger_ranking = [
                {
                    "rank": i + 1,
                    "doc_id": r["id"],
                    "content": r["content"][:300],
                    "raw_score": r["raw_score"],
                    "final_score": r["final_score"],
                }
                for i, r in enumerate(results)
            ]

            static_sorted = sorted(results, key=lambda r: r["raw_score"], reverse=True)
            static_ranking = [
                {
                    "rank": i + 1,
                    "doc_id": r["id"],
                    "content": r["content"][:300],
                    "raw_score": r["raw_score"],
                    "final_score": r["final_score"],
                }
                for i, r in enumerate(static_sorted)
            ]

            records.append({
                "query_id": q["id"],
                "query_text": q["text"],
                "query_intent": q["intent"],
                "ger_ranking": ger_ranking,
                "static_ranking": static_ranking,
            })

    return records


def collect_session_data(url: str, top_k: int, rounds: int) -> list[dict]:
    """交互クエリでの共起蓄積 + 観測クエリでの変化を記録する。

    各シナリオ:
    1. Reset
    2. observe_query で「Before」スナップショット取得
    3. training_queries を rounds回繰り返し（共起・mass蓄積）
    4. observe_query で「After」スナップショット取得
    5. Before/After の結果ドキュメント・順位の変化を記録
    """
    records = []

    with httpx.Client() as client:
        for scenario in SESSION_SCENARIOS:
            sid = scenario["id"]
            print(f"\n  {sid}: {scenario['name']}")
            print(f"    {scenario['description']}")

            # Reset for clean measurement
            print(f"    Resetting state...")
            client.post(f"{url}/reset", timeout=30.0)

            observe = scenario["observe_query"]
            training = scenario["training_queries"]

            # Before: observe query on fresh state
            print(f"    Before snapshot...")
            before_results = query_server(client, url, observe["text"], top_k)
            before_ranking = [
                {
                    "rank": i + 1,
                    "doc_id": r["id"],
                    "content": r["content"][:300],
                    "raw_score": r["raw_score"],
                    "final_score": r["final_score"],
                }
                for i, r in enumerate(before_results)
            ]

            # Training: interleaved queries with large top_k to build wide co-occurrence
            total_training = rounds * len(training)
            print(f"    Training: {rounds} rounds x {len(training)} queries = {total_training} queries "
                  f"(top_k={TRAINING_TOP_K})...", end="")
            for r in range(rounds):
                for tq in training:
                    query_server(client, url, tq, TRAINING_TOP_K)
                if (r + 1) % 5 == 0:
                    print(f" R{r+1}", end="", flush=True)
            print(" done")

            # After: observe same query after training
            print(f"    After snapshot...")
            after_results = query_server(client, url, observe["text"], top_k)
            after_ranking = [
                {
                    "rank": i + 1,
                    "doc_id": r["id"],
                    "content": r["content"][:300],
                    "raw_score": r["raw_score"],
                    "final_score": r["final_score"],
                }
                for i, r in enumerate(after_results)
            ]

            # Detect changes
            before_ids = [d["doc_id"] for d in before_ranking]
            after_ids = [d["doc_id"] for d in after_ranking]
            new_docs = [d for d in after_ranking if d["doc_id"] not in before_ids]
            dropped_docs = [d for d in before_ranking if d["doc_id"] not in after_ids]
            rank_shifts = []
            for d in after_ranking:
                if d["doc_id"] in before_ids:
                    old_rank = before_ids.index(d["doc_id"]) + 1
                    rank_shifts.append({"doc_id": d["doc_id"], "before": old_rank, "after": d["rank"], "shift": old_rank - d["rank"]})

            print(f"    Changes: {len(new_docs)} new, {len(dropped_docs)} dropped, "
                  f"{sum(1 for s in rank_shifts if s['shift'] != 0)} rank shifts")

            # Check graph
            resp = client.get(f"{url}/graph?min_weight=0.1", timeout=10.0)
            edge_count = resp.json()["count"] if resp.status_code == 200 else 0
            print(f"    Graph: {edge_count} edges formed")

            records.append({
                "scenario_id": sid,
                "scenario_name": scenario["name"],
                "scenario_description": scenario["description"],
                "observe_query_text": observe["text"],
                "observe_query_intent": observe["intent"],
                "training_queries": training,
                "training_rounds": rounds,
                "total_training_queries": total_training,
                "edges_formed": edge_count,
                "before": before_ranking,
                "after": after_ranking,
                "new_docs": new_docs,
                "dropped_docs": dropped_docs,
                "rank_shifts": rank_shifts,
            })

    return records


def generate_judge_prompt(records: list[dict], out_path: str) -> None:
    """LLMに渡す判定プロンプトをMarkdownとして書き出す。"""
    lines = [
        "# GER-RAG 関連度判定",
        "",
        "以下の各クエリに対して、検索結果の各ドキュメントの関連度を0-3のスケールで判定してください。",
        "",
        "## 判定基準",
        "",
        "| スコア | 意味 |",
        "|--------|------|",
        "| 0 | 無関係（クエリと接点なし） |",
        "| 1 | やや関連（話題がかすっている程度） |",
        "| 2 | 関連あり（クエリのトピックに明確に関連） |",
        "| 3 | 高い関連性（クエリの意図に直接合致） |",
        "",
        "## 回答フォーマット",
        "",
        "各クエリについて、以下のJSON形式で回答してください。",
        "ドキュメントの順番は提示された順序のまま、スコアだけを配列で回答します。",
        "",
        "```json",
        "{",
        '  "Q01_static": [3, 2, 1, ...],',
        '  "Q01_ger": [2, 3, 1, ...],',
        '  "Q02_static": [...]',
        "}",
        "```",
        "",
        "---",
        "",
    ]

    for rec in records:
        qid = rec["query_id"]
        lines.append(f"## {qid}: {rec['query_text']}")
        lines.append(f"**検索意図**: {rec['query_intent']}")
        lines.append("")

        lines.append(f"### {qid} 静的RAG順位 (raw cosine similarity)")
        lines.append("")
        for doc in rec["static_ranking"]:
            content = doc["content"].replace("\n", " ").strip()
            lines.append(f"**[{doc['rank']}]** (raw={doc['raw_score']:.4f})")
            lines.append(f"> {content}")
            lines.append("")

        lines.append(f"### {qid} GER-RAG順位 (dynamic scoring)")
        lines.append("")
        for doc in rec["ger_ranking"]:
            content = doc["content"].replace("\n", " ").strip()
            lines.append(f"**[{doc['rank']}]** (final={doc['final_score']:.4f}, raw={doc['raw_score']:.4f})")
            lines.append(f"> {content}")
            lines.append("")

        lines.append("---")
        lines.append("")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def generate_session_prompt(records: list[dict], out_path: str) -> None:
    """セッション適応性のBefore/After判定プロンプトを書き出す。"""
    lines = [
        "# GER-RAG セッション適応性 関連度判定",
        "",
        "各シナリオで、異なるトピックの交互クエリを大量に実行した前後で",
        "観測クエリの検索結果がどう変わったかを判定してください。",
        "",
        "## 判定基準",
        "",
        "| スコア | 意味 |",
        "|--------|------|",
        "| 0 | 無関係（クエリと接点なし） |",
        "| 1 | やや関連（話題がかすっている程度） |",
        "| 2 | 関連あり（クエリのトピックに明確に関連） |",
        "| 3 | 高い関連性（クエリの意図に直接合致） |",
        "",
        "## 回答フォーマット",
        "",
        "```json",
        "{",
        '  "S1_before": [3, 2, 1, ...],',
        '  "S1_after": [3, 3, 2, ...],',
        '  "S2_before": [...]',
        "}",
        "```",
        "",
        "---",
        "",
    ]

    for rec in records:
        sid = rec["scenario_id"]
        lines.append(f"## {sid}: {rec['scenario_name']}")
        lines.append(f"**シナリオ**: {rec['scenario_description']}")
        lines.append(f"**トレーニング**: {rec['training_rounds']} rounds x {len(rec['training_queries'])} queries "
                     f"= {rec['total_training_queries']} total queries")
        lines.append(f"**形成されたエッジ数**: {rec['edges_formed']}")
        lines.append("")

        lines.append(f"**観測クエリ**: {rec['observe_query_text']}")
        lines.append(f"**検索意図**: {rec['observe_query_intent']}")
        lines.append("")

        # Before
        lines.append(f"### {sid} Before（トレーニング前 = 静的RAG相当）")
        lines.append("")
        for doc in rec["before"]:
            content = doc["content"].replace("\n", " ").strip()
            lines.append(f"**[{doc['rank']}]** (final={doc['final_score']:.4f}, raw={doc['raw_score']:.4f})")
            lines.append(f"> {content}")
            lines.append("")

        # After
        lines.append(f"### {sid} After（トレーニング後 = GER-RAG動的状態蓄積済み）")
        lines.append("")
        for doc in rec["after"]:
            content = doc["content"].replace("\n", " ").strip()
            lines.append(f"**[{doc['rank']}]** (final={doc['final_score']:.4f}, raw={doc['raw_score']:.4f})")
            lines.append(f"> {content}")
            lines.append("")

        # Changes summary
        if rec["new_docs"]:
            lines.append(f"**新規浮上ドキュメント** ({len(rec['new_docs'])}件):")
            for d in rec["new_docs"]:
                content = d["content"][:100].replace("\n", " ").strip()
                lines.append(f"  - [{d['rank']}位] {content}...")
            lines.append("")

        if rec["dropped_docs"]:
            lines.append(f"**脱落ドキュメント** ({len(rec['dropped_docs'])}件):")
            for d in rec["dropped_docs"]:
                content = d["content"][:100].replace("\n", " ").strip()
                lines.append(f"  - [旧{d['rank']}位] {content}...")
            lines.append("")

        shifts = [s for s in rec["rank_shifts"] if s["shift"] != 0]
        if shifts:
            lines.append(f"**順位変動** ({len(shifts)}件):")
            for s in sorted(shifts, key=lambda x: -abs(x["shift"])):
                direction = "↑" if s["shift"] > 0 else "↓"
                lines.append(f"  - {s['before']}位 → {s['after']}位 ({direction}{abs(s['shift'])})")
            lines.append("")

        lines.append("---")
        lines.append("")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description="GER-RAG 評価データ書き出し")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--session", action="store_true",
                        help="Run session adaptivity test (WARNING: resets state)")
    parser.add_argument("--rounds", type=int, default=20,
                        help="Training rounds per scenario (default: 20)")
    parser.add_argument("--skip-comparison", action="store_true",
                        help="Skip static vs GER-RAG comparison")
    parser.add_argument("--out-dir", default="eval_output")
    args = parser.parse_args()

    try:
        httpx.get(f"{args.url}/docs", timeout=5.0)
    except httpx.ConnectError:
        print(f"ERROR: Cannot connect to {args.url}")
        sys.exit(1)

    os.makedirs(args.out_dir, exist_ok=True)

    # Part 1: Comparison data
    if not args.skip_comparison:
        print("Collecting comparison data...")
        comparison = collect_comparison_data(args.url, args.top_k)

        results_path = os.path.join(args.out_dir, "results.json")
        with open(results_path, "w", encoding="utf-8") as f:
            json.dump({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "top_k": args.top_k, "queries": comparison}, f, indent=2, ensure_ascii=False)
        print(f"  -> {results_path}")

        prompt_path = os.path.join(args.out_dir, "judge_prompt.md")
        generate_judge_prompt(comparison, prompt_path)
        print(f"  -> {prompt_path}")

    # Part 2: Session adaptivity
    if args.session:
        print(f"\nCollecting session data ({args.rounds} rounds per scenario, state will be reset)...")
        session = collect_session_data(args.url, args.top_k, args.rounds)

        session_path = os.path.join(args.out_dir, "session_results.json")
        with open(session_path, "w", encoding="utf-8") as f:
            json.dump({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "top_k": args.top_k, "rounds": args.rounds,
                        "scenarios": session}, f, indent=2, ensure_ascii=False)
        print(f"  -> {session_path}")

        session_prompt_path = os.path.join(args.out_dir, "session_judge_prompt.md")
        generate_session_prompt(session, session_prompt_path)
        print(f"  -> {session_prompt_path}")

    print(f"\n完了。次のステップ:")
    if not args.skip_comparison:
        print(f"  1. eval_output/judge_prompt.md をLLMに読ませて判定")
        print(f"  2. 判定結果を eval_output/judge_scores.json に保存")
    if args.session:
        print(f"  3. eval_output/session_judge_prompt.md をLLMに読ませて判定")
        print(f"  4. 判定結果を eval_output/session_scores.json に保存")
    print(f"  最後: python scripts/eval_compute.py --dir {args.out_dir}")


if __name__ == "__main__":
    main()
