# Research — Phase 2 Evaluation

Static RAG と GaOTTT（評価当時は GER-RAG と呼んでいた同じ実装）の比較、セッション適応性、創発性指標、ベンチマーク。

**一次ソース**: [`docs/research/evaluation-report.md`](../research/evaluation-report.md)

> **スコープの断り**: このページの数字は **数百ドキュメント規模の限定シナリオ** での計測結果である。10 万ドキュメント規模、adversarial なクエリ、最新 re-ranker との比較は **未実施**。README の「何を測って、何を主張しているか」節も同じ前提で読めるように揃えてある。

## 静的 RAG との比較サマリ

| メトリクス | Static RAG | GaOTTT | 差分 |
|---|---|---|---|
| nDCG@10 | 0.9457 | 0.9708 | **+2.7%** |
| MRR | 0.8833 | 1.0000 | **+13.2%** |

## セッション適応性（重力変位後）

- 500 クエリのトレーニングで 10,000+ エッジ、350+ ノードが変位
- S2（映画 × 食 × 旅）で nDCG **+15.0%** 改善
- 全シナリオ平均 nDCG **+3.8%**

## 創発性指標

通常の nDCG では測れない「創発的変化」を定量化:
- **Rank Shift Rate** — 同じクエリで順位が変わる頻度
- **Serendipity Index** — top-k に新しい記憶が浮上する頻度

GaOTTT は両指標で Static RAG と質的に異なる挙動を示した（数値の詳細は一次ソース）。

## ベンチマーク (SC-001〜SC-007)

| ID | 内容 | Phase D 完了時点 |
|---|---|---|
| SC-001 | クエリレイテンシ | p50 = **15.1ms** (200 docs) ✅ |
| SC-002 | 質量蓄積 | 反復検索で mass↑ 確認 ✅ |
| SC-003 | 時間減衰 | 直近アクセス boost 確認 ✅ |
| SC-004 | 共起グラフ | 88 エッジ形成 ✅ |
| SC-005 | 並行性 | 50 同時クエリ全成功 ✅ |
| SC-006 | 永続化 | スナップショット OK ✅ |
| Baseline | drift | 反復クエリで順位変動 ✅ |

## 評価スクリプト

| スクリプト | 用途 |
|---|---|
| [`scripts/benchmark.py`](../../scripts/benchmark.py) | SC-001〜SC-007 の成功基準を自動検証（レイテンシ、mass 蓄積、temporal decay、共起エッジ、並行処理） |
| [`scripts/eval_export.py`](../../scripts/eval_export.py) | 静的 RAG vs GaOTTT の比較データ書き出し、LLM-as-judge 用プロンプト生成 |
| [`scripts/eval_compute.py`](../../scripts/eval_compute.py) | 外部 LLM 判定結果から nDCG / MRR / Precision を算出 |
| [`scripts/run_benchmark_isolated.sh`](../../scripts/run_benchmark_isolated.sh) | 隔離 DB で安全に benchmark.py を回す |

セッション適応性評価は **Before/After 方式** を採用: リセット → 観測 → 500 クエリのトレーニング → 再観測。

→ 詳細評価レポート: [`docs/research/evaluation-report.md`](../research/evaluation-report.md)
→ ベンチ実行方法: [Operations — Isolated Benchmark](Operations-Isolated-Benchmark.md)
