# Phase U WP-4 — target trace findings (de1b528f, 2026-08-26)

- script: `scripts/diag_target_trace.py` / 対象: production copy (`/tmp/opencode/gaottt-prod-calib.*`)
- query: `recall explore ambient_recall が全件空返し FAISS 埋め込みパイプライン 沈黙エラー` (review probe 6)
- target: `de1b528f-f95a-46e8-a28d-7a4fbd580806` (2026-08-25 障害記録 node, mass 1.393, return_count 0)

## 計測結果

| 段階 | 結果 |
|---|---|
| raw FAISS | **rank 1** (cos 0.8967) |
| hybrid BM25 | **rank 1** (266.96) |
| ambient word-BM25 | **rank 1** (96.21) |
| virtual FAISS | **top-100 外** (vcos_norm 0.1033) |
| fused seed pool / wave reach | IN (seed rank 2, force 1.0) |
| qualification | **True** (raw 0.8967 ≥ 0.75, lexical 1.0) confidence 1.0 |
| **final passive top-20** | **不在** — scoring loop で脱落 |

## 根因

1. `final_score = gravity_sim(virtual cos)·decay + mass + wave + …` — **virtual cosine が支配項**。
2. 本 node は生成直後の genesis/priming kick (近傍 high-mass 方向, CLAUDE.md 既知) で displacement が raw content 方向から逸脱し、**自分の内容とほぼ同文の query に対する virtual cosine が池の band (〜0.78-0.86) を下回る**。
3. Stage 3 の qualified-first は「final_score 降順の安定 partition」であり、**qualification score 自体は順位信号にならない**。qualified 35 件超の pool で target は final_score 最下位帯 → top-K (20) 外。

→ 観測 (raw rank 1) が場の drift に消される構造。post-recovery handoff の北極星 (「semantic relevance を観測の土台に置き、重力場はその上で順位を育てる」) に反する。

## scoped fix 設計 — raw-top rescue (Stage 3 拡張)

- **rule**: qualified な natural item のうち **pool 内 raw cosine rank ≤ `direct_rescue_raw_rank` (新 knob, default 3)** の item を qualified group の先頭に lift (rescue group 内は raw cosine 降順)。sort key = `(0 if rescued else 1, 0 if qualified else 1, final_score desc)`。
- **根拠**: raw cosine (と lexical) は観測の土台。virtual displacement の発散 (genesis kick / mass-BH) が near-exact match を消すことは「場が順位を育てる」の範囲を超えた veto。rescue は観測を field の drift から守る最小の防腐剤。
- **適用範囲**: natural items のみ (forced/injected 経路は Phase J の規則のまま)。diversity (MMR) は rescue 適用後の pool から選択。
- **rollback**: `direct_rescue_raw_rank=0` で無効 (現行挙動に完全復帰)。`GAOTTT_DIRECT_RESCUE_RAW_RANK` で env rollback 可能なため supervisor allowlist への追加も実施。
- **予測**: 本 node は raw rank 1 → rescue group 先頭 → top-1 (review probe 6 の期待「top-5 入り」を上回る)。近接の「BM25 gate veto 病理」memory (bm25 96.2) も raw 高位なら同時 rescue され、これも妥当 (直接関連)。

## 検証計画

1. test-first: 高 raw match + displacement 発散 + qualified 混在 pool の fixture で「rescue なし=top-K 外 / あり=先頭」を検証。knob=0 で現行挙動の fence。
2. production copy で trace 再実行 → top-5 入りを確認 (R4 acceptance)。
3. full suite + perf tier3/4/7 (ranking 変更なので golden 回帰確認)。
