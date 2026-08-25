# WP-C — Phase T 較正 round 記録 (flags ON 測定 + dream exemption)

- 実施日: 2026-08-25
- Plan: [docs/wiki/Plans-Phase-T-Semantic-Requalification.md](../../wiki/Plans-Phase-T-Semantic-Requalification.md) §5 WP-C 相当 (Stage 3-6 の 4 flag を一時 default ON にして測定し復元)
- 状態: 測定完了。**config default は 4 flag とも False に復元済み** (`semantic_halflife_enabled=True` は維持)。dream exemption のみコードとして残存 (flag 非依存の契約修正)。
- 測定時 config: `direct_qualification_enabled=True` / `ttt_qualification_enabled=True` / `ambient_gate_or_semantic=True` / `explore_diversified_presentation_enabled=True` (config.py default を直接書き換え — `tests/perf/_helpers.make_config` が `GaOTTTConfig(**defaults)` 直接構築で env が効かないため)。

## 1. dream exemption (Stage 4 learn set の synthetic 除外)

実装: `gaottt/core/engine.py` `_query_internal` の learn set 構築条件に `and not _is_synthetic` を追加。
synthetic (dream loop) recall は `learn_ids=None` (= legacy all-reached 学習・confidence 1.0) を維持し、
cooccurrence も `result_ids` 全体に戻る。passive は従来どおり simulation 全体 skip のまま。

red → green:

| state | `pytest tests/integration/test_engine_dream_loop.py -q` |
|---|---|
| flags ON (direct+ttt)・exemption なし | **1 failed** (`test_dream_loop_builds_cooccurrence_over_time`: edges 0 — learn set 空で cooccurrence 構築不能) |
| flags ON・exemption あり | **3 passed** |
| flags OFF (復元後)・exemption あり | **3 passed** (no-op 確認 — flag OFF は元々 learn set = all reached なので exemption は無操作) |

flags OFF での no-op は full suite でも確認 (§5 参照)。config.py の Stage 3/4 コメント欄も exemption を反映して更新。

## 2. query_kick 失敗の調査 (test は不改変・判断は PM)

flags ON で `tests/integration/test_engine_query_kick.py::test_stage3_gate_dampens_drift_for_new_nodes` が失敗:

```
AssertionError: Stage 3 gate should reduce q-direction drift on low-mass node —
proj_stage2=0.0790, proj_stage3=0.0845  (期待: stage3 < stage2)
```

- **帰属**: `ttt_qualification_enabled` 単独で失敗 (direct 単独では green、数値は同一)。
- **性質**: 契約違反ではなく **mass 軌道の数値比較の反転**。実測 (同一 fixture を flags ON/OFF で再現):

| config | proj_stage2 | proj_stage3 | ratio (stage3/stage2) | target mass (20 recall 後) | 判定 |
|---|---|---|---|---|---|
| flags OFF | 0.0809 | 0.0780 | 0.964 | 2.019 | PASS (margin 3.6%) |
| ttt ON | 0.0790 | 0.0845 | 1.069 | 1.964 | FAIL |

- **メカニズム**: Stage 4 の mass growth ×confidence (この stub corpus では raw_cos≈0.97 → margin≈0.88) で
  両 engine の mass 軌道が ~2.7% 低くなり、20 step の Verlet 積分 (kick gate `tanh(m/θ)` + 加速度の `1/m` +
  近傍重力が mass 経由で結合) を通じて増幅されて、元々 3.6% しかなかった比較 margin を反転させた。
  両 projection とも正で大きさも同程度 (~0.08) — 物理契約 (gate が new node を dampen する) が破れたわけではない。
  test 自身のコメントも "modulo mass-accretion over 20 steps" と mass 軌道依存を認めている。
- **PM 判断候補**: (a) ttt を default ON にする場合は本 test を mass 軌道非依存な契約比較へ再設計 (tests/** は WP-C scope 外),
  (b) ttt を default OFF のまま運用、(c) 失敗を known-issue として明示的に許容。

## 3. flags ON full suite (tests/, perf 除外)

`.venv/bin/python -m pytest tests/ -q --ignore=tests/perf` → **12 failed / 1302 passed / 11 skipped (179s)**。

失敗 12 件の全リストと帰属:

| # | test | 起因 flag | 分類・メカニズム |
|---|---|---|---|
| 1 | `test_engine_query_kick.py::test_stage3_gate_dampens_drift_for_new_nodes` | ttt | §2 参照 (mass 軌道の margin 反転) |
| 2 | `test_reason_line.py::test_dominance_artifact_fires_on_high_mass` | direct or ttt (qualification_active) | **実害あり**: Stage 3 が `lensing_gap = vcos_norm − raw_cos` を populate すると、thermal noise で +1e-6 程度の正 gap でも explain.py branch 2 (`lensing_gap > 0 → "lensing pick"` wins outright) が先行し、branch 4 の dominance warning を覆い隠す。観測層の branch 優先度衝突 |
| 3 | `test_engine_explore_diversity.py::test_relevance_floor_excludes_low_semantic_candidates` | direct | fixture 前提「mass=4000 の無関係 node が plain recall で top-1」が qualified-first reorder で崩壊 (Stage 3 が狙った挙動そのもの) |
| 4 | `test_engine_explore_diversity.py::test_presented_only_updates_pool_members_untouched` | ttt | cooccurrence が presented ∩ learn set に gate され、stub corpus が 0.75 閾値 (RURI 狭帯較正) を超えられず learn set 空 → edge が生えない |
| 5-7 | `test_ambient_or_gate.py` ×3 (`or_gate_restores` / `empty_reason_bm25_veto_legacy` / `expose_off_response_still_carries_diagnostics`) | ambient | legacy 半分の engine が default OFF を暗黙前提 (`bm25_veto` assertion)。default ON では veto が発火しない |
| 8-9 | `test_engine_ambient_recall.py` ×2 (`virtual_score_gate_fallback` / `bm25_lexical_gate`) | ambient | legacy 空返し (veto) を pin。OR 軸 (stub で virtual_score 0.98 ≥ 0.70) が通過してしまう |
| 10 | `test_mcp_tools.py::test_ambient_recall_mcp_returns_block` | ambient | sentinel 空を期待 → OR で block が返る |
| 11 | `test_rest_memory.py::test_ambient_recall_rest_roundtrip` | ambient | count 0 を期待 → OR で 2 件 surface |
| 12 | `test_supervisor.py::test_probe_initialize_success_returns_ok` | (既知 flaky) | 単独再実行で green (2 回確認) |

分類集計: **8/12 が「legacy default-OFF 挙動を pin する test」の予期失敗** (ambient 系 6 + #3)、
**3 件が default ON 時の実際の意味的相互作用** (#1 mass 軌道 / #2 reason line 優先度 / #4 stub corpus と RURI 較正閾値の不整合)、
1 件 flaky。#2 は default ON を採る場合に要対応 (explain.py branch 2 に gap の最小閾値を入れる等 — Phase T follow-up)。

## 4. perf tier (real RURI, flags ON)

| suite | 結果 | 数値要点 |
|---|---|---|
| tier3 retrieval quality | 5 passed | engine.query surface (plain / tag_filter / semantic cluster / source-mix / 全 golden query 非空) |
| tier3 ambient quality | 4 passed | golden corpus ambient / breakdown signals / lensing baseline / session repetition |
| tier4 dynamics | 3 passed | anti-hub top1 diversity / displacement bounds / repeated-recall stability |
| tier7 golden regression | 2 passed | corpus load / golden queries top hit |
| tier6 performance | 4 passed | **p50=37.5ms p95=58.0ms p99=64.9ms / ingest 1245.7 docs/s** |

tier6 flags OFF 比較 (同日 1 run ずつ): OFF p50=38.5 / p95=64.2 / **p99=175.1** / 1216.6 docs/s。
→ **latency 回帰なし** (Stage 3 BM25 pool (50) / Stage 6 MMR / Stage 5 OR gate の追加 cost は
200-doc corpus では noise 圏。p99 の差は OFF 側 1 run の外部 noise を含む — 単発比較である点に注意)。

## 5. flags OFF (復元後) full suite

`.venv/bin/python -m pytest tests/ -q --ignore=tests/perf` → **1 failed (既知 flaky のみ) / 1313 passed / 11 skipped (182s)**。
dream exemption が flags OFF で無操作であることの全 suite での確認 (WP-3 完了時点と同一状態)。

## 6. baseline 再取得 (flags ON)

- `docs/notes/phase-t/score-baseline-flags-on-7d.json` (--synthetic-age-seconds 604800)
- `docs/notes/phase-t/score-baseline-flags-on-fresh.json` (fresh; decay=1.0)
- ※ script の `_CONFIG_FIELDS` snapshot は Phase T flag 追加より前のもので JSON 内 `engine.config` には
  4 flag が `null` で記録されるため、capture 時の実効 flag (4 つとも True) を top-level
  `phase_t_flags_at_capture` field として追記してある。

### score 項寄与率 (7d, top-100 passive, n=462)

| 項 | mean share |
|---|---|
| semantic | **0.832** |
| wave | 0.080 |
| mass | 0.055 |
| certainty | 0.032 |
| emotion | 0.000 |

decay_factor=0.675 一様 (= floor 0.35 + 0.65×0.5^(7d/7d) — half-life 契約の 1 半減期点で妥当)。
Stage 2 の semantic 支配は 4 flag ON 下でも維持。fresh では semantic 0.87。

### qualification 率 (production 閾値 raw 0.75 / vcos 0.75 / rel 0.40+abs 8.0)

- raw_cos / vcos_norm 分布は passive corpus (displacement≈0) のため同一: p50=0.764 / p90=0.820 / max=0.927 (WP-1 before と不変)。
- **cosine 軸 (raw≥0.75 ∨ vcos≥0.75) で 70.56%** (WP-1 と同一 — passive 測定なので他 flag の影響を受けない)。
- lexical 軸単独 (rel≥0.40) は 7.36%。production OR (cosine ∨ lexical 二重条件) は約 70.6% + lexical-only 分 (上限 +7.4pt)。
  ※ script の `or_provisional` 出力は旧暫定 (0.45/0.55) のままで production 閾値の combined OR は JSON に無い — 読み取り側の補足。
- lensing_gap ≈ 0.000 (passive では displacement が無いため。#2 の reason line 問題は active recall でのみ顕在化)。

### ambient gate (19 golden queries)

- 全 19 query が word-BM25 gate reject (gate score 5.8-8.8 vs threshold 32.0; `gate_pass=False`)。
- **OR flag ON で 19/19 surfaced** (semantic_max_virtual 0.86-0.93 ≥ 0.70 が OR 軸で承認; direct slot 2 件/query 程度)。
  legacy (veto) なら 19/19 空 → **「19/19 veto → 19/19 surface」を確認** (WP-1 の A5 確証と整合)。

### explore Jaccard@5 (vs passive recall, median)

| diversity | flags ON | before (WP-1) |
|---|---|---|
| 0.0 | 1.000 | 1.000 |
| 0.5 | 0.600 | 0.600 |
| 0.8 | 0.600 | 0.600 |
| 1.0 | 0.600 | 0.600 |

- d0.8 で 11/11 query が recall と不一致 (jaccard ≤ 0.8 < 1.0) — plan の「8 割変化」基準は数値上 100% 充足。
- **ただし per-query で before と比較すると MMR が結果を変えたのは 1/11** ("Reciprocal Rank Fusion" 0.8→0.5) のみ。
  Jaccard 低下の大部分は explore 既存の gamma 拡大 (legacy 機構) 由来で、**golden corpus top-5 では Stage 6 MMR の
  追加効果は縁際的**。acceptance 基準は形式上満たすが、効果の帰属は legacy gamma が主 — PM はこの点を踏まえて
  Stage 6 default を判断すること (より大きな corpus / top-k で再測定する価値あり)。
- なお「golden query median relevance が baseline 比 -10% 以内」の直接測定は本 round では未実施
  (tier7 golden regression が green であることのみ確認)。

## 7. config 復元の確認

`git diff gaottt/config.py` (HEAD 比較・Phase T 全体が未 commit の状態):

```python
semantic_halflife_enabled: bool = True
direct_qualification_enabled: bool = False  # plan A3: default は較正後確定
ttt_qualification_enabled: bool = False     # plan A3: default は較正後確定
explore_diversified_presentation_enabled: bool = False  # plan A3: 較正後確定
ambient_gate_or_semantic: bool = False   # False = legacy BM25 veto / True = BM25 OR semantic
```

engine.py の WP-C 分 delta は learn set 条件 (+comment) のみ。ruff: 両 file clean。

## 8. PM 判断用まとめ (default ON/OFF の材料)

| flag | 品質 evidence | 回帰 evidence | 所感 |
|---|---|---|---|
| direct (S3) | qualification 70.6% で unrelated 高 mass を fallback に降格 (設計どおり) | #3 (fixture 前提崩壊 — 設計どおりの挙動), #2 (reason line — 共通下地) | 閾値は RURI 実帯で機能。#2 の explain 優先度修正をセットで |
| ttt (S4) | 誤候補自己強化の遮断 (WP-3 fixture green) | #1 (query_kick margin 3.6% 反転 — 数値比較), #4 (stub corpus 閾値非互換) | dream exemption で dream loop は守れた。#1 は test 再設計または default OFF 判断の要 |
| ambient (S5) | **19/19 veto → 19/19 surface** (最大の品質改善) | 6 test が legacy veto pin (予期) | off-topic 抑制は ambient_quality tier green。default ON 有力 (要 #5-11 test 更新 — tests/** は別 WP) |
| explore (S6) | d0.8 で median 0.6 | なし (関連 2 失敗は direct/ttt 帰属) | golden corpus では MMR 追加効果小 (1/11) — 効果実感には本番 dogfooding probe ③ が鍵 |

共通: tier 3/4/6/7 全 green・latency 回帰なし。残る較正 gate は plan §3 rollout gate の **本番 dogfooding (opt-in env, probe ①②③)**。
