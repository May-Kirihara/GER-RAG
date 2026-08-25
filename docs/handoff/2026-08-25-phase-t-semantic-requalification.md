# 引き継ぎメモ — Phase T Semantic Requalification 実装 (retrieval quality 系)

## ステータス

- 状態: **implemented — gate 通過 (dogfooding と Phase 2 が残)**
- 日付: 2026-08-25
- 起点: [2026-08-25-post-recovery-retrieval-quality.md](2026-08-25-post-recovery-retrieval-quality.md) の実装順 1-6
- 計画書: [docs/wiki/Plans-Phase-T-Semantic-Requalification.md](../wiki/Plans-Phase-T-Semantic-Requalification.md)
- 概要: 復旧後の検索品質課題 (semantic 消滅 / mass 支配 / ambient 無言空返し / explore 非多様化) に対し、意味的観測を土台に重力場が順位を育てる構造へ戻す Stage 1-6 を実装。**handoff は Phase 2 (cold-start / backend lifecycle / BM25 background build) 完了まで closed にならない**。

## 変更内容 (default 状態)

| Stage | 機構 | default | env (rollback / opt-in) |
|---|---|---|---|
| 2 | semantic decay を half-life + floor 契約へ (`floor + (1-floor)·0.5^(age/half_life)`)。7d/0.35 は較正出力 | **ON** | `GAOTTT_SEMANTIC_HALFLIFE_ENABLED=false` (legacy `delta` に bit-for-bit 復帰 + startup warning) |
| 3 | direct relevance qualification (raw_cos≥0.75 OR 正規化 virtual_cos≥0.75 OR lexical rel≥0.40 AND abs≥8.0)。qualified-first 順序 + fallback マーク | OFF | `GAOTTT_DIRECT_QUALIFICATION_ENABLED=true` |
| 4 | TTT 更新の query-conditioned 項 (mass growth ×confidence / query kick / cooccurrence) を qualified learn set に限定。dream loop (synthetic) は免除 | OFF | `GAOTTT_TTT_QUALIFICATION_ENABLED=true` |
| 5 | ambient gate を BM25 veto から OR gate (BM25 strong OR virtual≥0.70 OR raw_cos≥0.60) へ + `gate_diagnostics` / `empty_reason` | **ON** | `GAOTTT_AMBIENT_GATE_OR_SEMANTIC=false` (veto 復帰) |
| 6 | explore presentation を engine 層 canonical MMR (cohort penalty + relevance floor、forced 豁免、diversity=0 は完全 legacy) | OFF | `GAOTTT_EXPLORE_DIVERSIFIED_PRESENTATION_ENABLED=true` |

新観測: `ScoreBreakdown.qualified/direct_score/field_score` + `lensing_gap` populate、reason line `gravity pick (below relevance floor)`、`AmbientRecallResponse.gate_diagnostics` (empty_reason 5 値)。新 script: `scripts/score_baseline.py` (read-only baseline、`--synthetic-age-seconds`)。

## 変更理由

handoff 実測で確認された 4 病理のコード上の根因: ① `exp(-0.01·age_seconds)` は 10 分で ≈0.0025 になり semantic 項が消滅 → mass/wave が順位支配、② 低 relevance・高 mass node が active recall で自己強化 (bad gradient)、③ ambient BM25 gate が veto として即空返し (golden corpus で 19/19 再現、gate score 5.8-8.8 vs 閾値 32.0、semantic_max 0.899 は十分)、④ explore は gamma/depth を広げるだけで最終順序は同一 final_score 順。詳細は計画書 §1。

## Work packages

| WP | Scope | Status | Files | Verification |
|---|---|---|---|---|
| WP-1 | baseline 観測 script | done | `scripts/score_baseline.py`, `docs/notes/phase-t/score-baseline-before.json` | ruff green、2 run で 1e-9 決定論 |
| WP-2 | Stage 2 half-life+floor | done | scorer / config / engine / 新規 test 2 | test-first red→green (24)、full suite green |
| WP-3 | Stage 3+4 qualification | done-with-notes | engine / types / explain / scorer / formatters / config / 新規 test 3 | 34 green、full suite green (default OFF で landing — 既存 2 test との衝突、計画 A3 の escape hatch) |
| WP-4 | Stage 5 ambient OR gate | done | services/memory / types / formatters / mcp docstring / 新規 test 2 + rest_parity 追加 | 19 green、full suite green、smoke 両方 green |
| WP-5 | Stage 6 explore MMR | done | engine / core/diversity.py (新規) / services/memory / config / 新規 test 2 + rest_parity 追加 | 20 green、full suite green、smoke 両方 green |
| WP-C | 較正 round + dream exemption | done-with-notes | engine (dream exemption) / docs/notes/phase-t/ | flags ON で tier3/4/7 14/14 green、tier6 latency 回帰なし、baseline 再取得 |
| WP-D | Stage 5 default 昇格 | done | config / 既存 test 7 件に明示 legacy config (assertion 無変更) / 新 default pin test | full suite 1314+green、tier3 ambient green、smoke 両方 green |
| WP-E | final review 修正 | done-with-notes | explain.py / diversity.py / engine docstring / score_baseline / test 2 件 | 32 green、full suite 1322+green |
| WP-6 | docs 一式 | done | SKILL.md 両 copy / wiki 9 page / CLAUDE.md / mcp docstring | SKILL sync diff 空、knob 16 件 Tuning 表 |

## 触ったファイル

- 実装: `gaottt/core/{engine,scorer,types,explain,diversity}.py`、`gaottt/services/{memory,formatters}.py`、`gaottt/config.py`、`gaottt/server/mcp_server.py` (docstring)
- 新規: `gaottt/core/diversity.py`、`scripts/score_baseline.py`、test 7 file (unit 4 + integration 3) + rest_parity 追加
- docs: SKILL.md 両 copy、`docs/wiki/` 9 page、`docs/notes/phase-t/` 9 artifact、CLAUDE.md
- `.gitignore` (`.score-baseline-tmp/`)
- **※ 復旧 session 起因の未コミット変更が同居**: `gaottt/diagnostics/startup.py`、`gaottt/index/faiss_index.py`、`scripts/rebuild_faiss_from_db.py`、`tests/integration/test_faiss_persist_guard.py`、`engine.py` の index/store mismatch RuntimeError hunk、`scripts/migrate_legacy_delta_to_universe.py`。commit 分割推奨 (QA 指摘)

## テスト

- 新規 105 test (unit 59 / integration 47 相当 — QA 集計)、すべて test-first red→green
- full suite (`--ignore=tests/perf`): **1322 passed / 11 skipped / 1 failed** — 失敗は文書化済み flaky `test_probe_initialize_success_returns_ok` (単独 green、複数回確認)
- perf (real RURI, flags ON): tier3 ×2 + tier4 + tier7 = 14/14 green、tier6 4/4 green (p50 37.5ms / p95 58.0ms、回帰なし)
- smoke: `scripts/mcp_smoke.py` + `scripts/rest_smoke.py` 両方 green
- 未実施: 本番 dogfooding probe (下記 手動確認)

## ドキュメント

SKILL.md (両 copy 同期) / MCP-Reference-Memory + Index / REST-API-Reference / Operations-Tuning (16 knob 表 + multiverse env strip 注意) / Operations-Troubleshooting (empty_reason triage 表) / Plans-Roadmap / Architecture-Overview 設計判断表 / _Sidebar / CLAUDE.md / 計画書 status 更新。`Home.md` は意図的に未更新 (repo convention で phase link は Sidebar 集約 — Phase N/O/P/Q も Home 未追加)。

## 手動確認

**本番 dogfooding probe (2026-08-25 実施済み — backend 20:59 spawn、新コードで稼働確認)**:

- **probe ② (ambient 障害 query)**: ✅ `ambient_recall("GaOTTTのsemantic search障害を復旧し、今後の運用を確認する", direct_k=3, expose_breakdown=true)` → handoff で「関連する記憶なし」だった query が **3 件 surface**。診断行 `gate: passed (bm25_top=27.0/32.0, virt_max=0.854, raw_max=0.861, candidates=15)` — **bm25_top 27.0 < 閾値 32.0 (= BM25 reject) を semantic 軸 (virt 0.854 ≥ 0.70) で accept** した事例。legacy veto なら空返し。manifest 行は block 末尾維持。
- **probe ① (設計思想 query)**: ✅ `recall("GaOTTTの設計思想と長期記憶の運用知見", passive)` top-5 = **shabero 0/5** (handoff では 5/5 shabero)。GaOTTT 探索記録 (統一方程式) / Phase T decay 変更 / TTT 議論 ×2 / semantic compression。全件 cos 0.83-0.85 semantic match — **Stage 2 の semantic 復活のみで改善** (Stage 3 は未昇格)。
- **probe ③ (explore Jaccard)**: 未実施 — Stage 6 が default OFF のため現状では legacy gamma 効果のみの測定になる。昇格判断時に実施。
- **残る運用手動確認**: (a) hook latency の本番体感 (tier6 では回帰なし), (b) Stage 3/4/6 昇格判断 (下記)

**operator 承認待ち項目**:

1. Stage 3/4/6 の default 昇格判断: probe ①② + 1-2 週の観察後に。昇格前の残課題: ① Stage 4 ON で `test_engine_query_kick::test_stage3_gate_dampens` が mass 軌道の数値比較で反転 (契約違反ではないが test 再設計要)、② stub corpus が 0.75 閾値を超えられない test がある
2. **multiverse では supervisor が GAOTTT_* env を strip するため Stage 3/4/6 の opt-in env は効かない** (昇格時に有効化)

## 既知の問題

- Stage 3 ON 時、`virtual_cos_norm` の temperature noise で threshold 境界がわずかに非決定的になり得る (決定論的 raw_cos を第一軸で緩和済み、plan §7)
- ambient raw 軸は `expose_score_breakdown=True` (default) でしか動かない (breakdown 供給依存 — OFF 時は virtual 軸のみ)
- `scripts/score_baseline.py` の before artifact (WP-1) は旧暫定閾値 (0.45/0.55/0.40) で集計された `or_qualified_provisional` を含む (sweep 生値は有効。script 自体は WP-E で契約値に修正済み)
- golden corpus では Stage 6 MMR の効果は 1/11 query のみ (Jaccard 低下の主因は legacy gamma 拡大) — 本番規模での効果確認が必須

## 残TODO

1. **Phase 2 (handoff closure に必須)**: Stage 7 backend lifecycle / staged readiness (cold-start P0)、Stage 8 BM25 snapshot / background build — 計画書 §2 参照
2. 本番 dogfooding 3 probe の実施と記録
3. Stage 3/4/6 の default 昇格判断 (上記条件クリア後)
4. 2,053 orphan nodes の由来調査 (handoff 副次所見、未着手)
5. Roadmap 進捗サマリ表の復旧 (Phase M で停滞中 — 既存状態)

## リスク

- Stage 2 ON により semantic 項が復活 → saturation との再平衡が実本番でどう見えるか未観察 (golden corpus では問題なし、tier7 green)
- floor が displacement 歪み込みの `gravity_sim` 項を恒久保存する既知の性質 (plan §7 — 悪化時は floor を raw_cos 側へ移す改訂)
- Stage 5 ON で BM25 reject prompt が passive recall の cost を払う (tier6 で回帰なし確認済み、本番 42k での hook latency は dogfooding で確認)

## ロールバックメモ

- Stage 2: `GAOTTT_SEMANTIC_HALFLIFE_ENABLED=false` (multiverse 外) / config default flip (multiverse)
- Stage 5: `GAOTTT_AMBIENT_GATE_OR_SEMANTIC=false` — legacy veto に即復帰 (test 7 件が legacy 契約を pin)
- Stage 3/4/6: default OFF のままなので操作不要 (無効化 = 昇格しない)
- dream exemption (synthetic recall の qualification 免除) は flag 非依存の契約 — 元に戻すには engine.py の `and not _is_synthetic` を除去

## 次の担当者・エージェントへのメモ

- Phase 2 (Stage 7/8) はファイル所有が完全分離 (multiverse/supervisor + engine.startup) — Phase T と衝突しない。handoff の「P0: backendをsessionより長寿命に」節が仕様。
- 較正データは `docs/notes/phase-t/` に一式 (baseline 6 本 + calibration-round.md)。threshold 再検討時は `scripts/score_baseline.py` を `--synthetic-age-seconds` 付きで。
- 既存 test との衝突リスト (flags ON 時) は calibration-round.md に catalog 済み — Stage 3/4 昇格時の checklist として使える。
- commit は (1) 復旧 session 分、(2) Phase T 実装分、(3) docs 分の分割を推奨。
