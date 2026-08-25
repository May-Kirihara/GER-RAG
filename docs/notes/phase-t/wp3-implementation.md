# WP-3 実装記録 — Phase T Stage 3 (direct relevance qualification) + Stage 4 (TTT update qualification)

- 実装日: 2026-08-25
- Plan: [docs/wiki/Plans-Phase-T-Semantic-Requalification.md](../../wiki/Plans-Phase-T-Semantic-Requalification.md) §3 Stage 3 / Stage 4
- 状態: 実装完了・test green。default は **両 flag OFF (env opt-in)** — 下記「default 値の判断」参照。

## 実装箇所 (engine.py)

| 機能 | 箇所 |
|---|---|
| BM25 pool 再構成 (qualification 常時計算) | `_query_internal` Step 3 前、旧 `bm25_hit_ids` block (`qualification_active or expose` で 1 回 search、pool 50 / legacy は `max(reached,50)` のまま) |
| `virtual_cos_norm` + qualification + confidence | scoring loop 内 (`pure_raw_cosines` 直後)。`qualification_map` / `learn_confidences` |
| breakdown 新 field | `ScoreBreakdown(qualified=, direct_score=, field_score=, lensing_gap=vcos_norm−raw_cos)` |
| qualified-first 選択 (Stage 3) | Step 4: natural items を final_score 降順 → stable secondary sort (qualified=0 / fallback=1)。injected branch では `others` 側のみ適用、forced 規則 (Phase J RRF/raw) 不変 |
| learn set 構築 (Stage 4) | Step 6: `not passive and ttt_qualification_enabled` のとき `learn_ids = [nid in all_reached if qualified(nid)]` |
| mass growth gate ×confidence | `_update_simulation`: `learn_set is None` → legacy 式 (bit-for-bit)、in learn set → `η·force·confidence·(1−m/m_max)`、else skip |
| query kick gate | `_update_simulation` → `update_orbital_state(query_scores={nid: reached[nid] for nid in learn_ids})`。absent key → `q_score=None` → kick なし (A6 確証) |
| cooccurrence gate | Step 6: `presented ∩ learn set` (`learn_ids is None` なら従来どおり全 presented) |
| 純粋関数 | `scorer.py`: `compute_lexical_strength` / `is_direct_qualified` / `qualification_confidence` (+ `_axis_margin`, threshold≥1 安全) |
| formatter 追加 segment | `formatters._format_breakdown`: `q=+/q=-` + `d= f= gap=` (既存 segment byte-identical) |
| explain 追加行 | `explain.py` branch 3.5: `qualified is False` → "gravity pick (below relevance floor)" + "TTT update gated" hint |

## test (新規 3 file, 34 case)

- `tests/unit/test_direct_qualification.py` — 真偽表 / lexical relative-ratio / confidence max-margin / threshold=1 edge
- `tests/integration/test_engine_direct_qualification.py` — qualified-first 順序 + fallback tail / top1 昇格 / flag OFF legacy 同一 / fill 保証 / expose 非依存 / lexical 二重条件 (config で absolute arm・relative arm を独立無効化) / forced 順序維持 + forced-only 非学習 / prefetch parity / breakdown identity (vcos 独立再計算)
- `tests/integration/test_engine_ttt_qualification.py` — passive purity / unqualified presented の mass・kick 不変 (kick isolation config: G=0, anchor=0, kick のみ) + ungated 対照 / maintenance all-reached (evaporation・last_access・sim_history) / qualified 学習 / confidence 単調性 / 誤候補自己強化 regression (twin engine ×10 recall) / orbital 非縮退 / Δmass==0 / flag 組み合わせ (S3off×S4on, S3on×S4off)

StubEmbedder (64-d, md5 token seed) 実測値を fixture 設計に使用: QUERY="quantum gravity wave general relativity" に対し A=+0.790 (qualified via raw @0.75) / C=+0.621 / B=+0.041 / LEX=+0.018 (LEX_QUERY で bm25 39.1 rel 1.0)。

## default 値の判断 (重要)

WP 指示の暫定 `True` で full suite を回した結果 **2 件の既存 test が破壊**:

1. `test_engine_dream_loop.py::test_dream_loop_builds_cooccurrence_over_time` — dream loop (synthetic recall) は無関係 node 群の co-recall で cooccurrence edge を構築する設計。Stage 4 gate が learn set (qualified のみ) に限定するため edge が 1 本も生えない。plan の learn set 定義 (¬synthetic) どおりに synthetic を学習除外すればより壊れる。
2. `test_engine_query_kick.py::test_stage3_gate_dampens_drift_for_new_nodes` — mass growth ×confidence が mass 軌道を変え、tanh(m/θ) gate の stage2/stage3 比較 (margin 小) が反転。

※ 当初 2 は「flags OFF でも失敗するため pre-existing」と誤判定したが、**test は `GaOTTTConfig(...)` 直接構築で env override が効かない**ため検証が無効だった (env override loop は `from_config_file()` のみ)。default False 化で両方 green。

→ plan A3 の escape hatch (「較正が不十分なら default OFF + env opt-in」) を適用し **両 flag default False**。dream loop の synthetic 学習の扱い (qualified gate を入れる / synthetic だけ除外 / 無条件) は較正 (tier3/4/7 + dogfooding) 時に判断する。

## 既知の相互作用・留保

1. **`lensing_gap` の二重意味**: engine query path は `vcos_norm − raw_cos` を populate するが、`services/memory._enrich_breakdown` (recall/ambient direct path) が `model_copy(update={"lensing_gap": 0.0})` で上書きする。よって **MCP/REST の recall 出力の `gap=` は現状ほぼ +0.00** (engine level / REST なしの直接 query では正値)。ambient lensing slot は従来どおり独自 gap で上書き。WP-4 (Stage 5, memory.py 触れる) で伝播させるかどうか PM 判断。
2. **BM25 pool の corpus-size 依存**: idf が N 依存のため、同一 doc でも corpus 構成で absolute score が変わる (5-doc で C=15.9 → 3-doc で 7.2)。test は raw threshold か doc 構成で安定化済み。本番較正時は absolute 8.0 の再確認を推奨。
3. **`bm25_contributed` flag**: qualification ON 時は pool が 50 固定になるため、reached > 50 の大規模 DB では flag の付く範囲が従来 (max(reached,50)) と変わる。informational のみ。
4. **synthetic recall (dream loop) の学習**: 本実装では passive 以外の全 recall に uniform に qualification gate を適用 (synthetic も qualified node のみ学習)。default OFF のため挙動変化なし。ON 時の dream loop は qualified のみ consolidate する — 較正時に観察。

## 検証

- 新規 test: red (ImportError / TypeError / 18 failed) → green (34 passed)
- full suite: `1272 passed, 1 failed (既知 flaky test_probe_initialize_success_returns_ok — 再試行で green), 11 skipped`
- ruff: touched files all clean (repo 既存 4 件は不変)
