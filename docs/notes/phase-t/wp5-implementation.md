# WP-5 実装ノート — Stage 6 explore presentation 多様化 (engine 層)

- 日付: 2026-08-25
- 対応 plan: docs/wiki/Plans-Phase-T-Semantic-Requalification.md §3 Stage 6
- 状態: 実装 + test green。default OFF (plan A3 gate — 較正・dogfooding後に確定)

## 実装構成

| 箇所 | 内容 |
|---|---|
| `gaottt/core/diversity.py` (新規) | MMR 純粋関数: `normalize_relevance` (min-max, all-equal→1.0) / `apply_relevance_floor` / `mmr_select` (greedy, λ=1−0.5d, tie は入力順=final_score 降順) / `cluster_key_from_cache` (cohort_id OR original_id, services と同規則・core から services は import 不可のため mirror) |
| `gaottt/core/engine.py` | `query(..., diversity=None)` (最後尾 keyword)。`diversity_active = diversity>0 ∧ flag` のみ新経路: Step 4 で `_select_diverse_natural` (pool = legacy 順 top `top_k×multiplier` → floor → MMR。forced は preselected として redundancy/cluster 参照のみ)。cache key に diversity が無いため diversity_active は prefetch cache も bypass |
| `gaottt/services/memory.py` | `explore` が `engine.query(..., diversity=diversity)` を渡すのみ。`mode="dormant"` 無変更 |
| `gaottt/config.py` | `explore_diversified_presentation_enabled=False` / `explore_cohort_penalty=0.05` / `explore_diversity_pool_multiplier=4` / `explore_min_semantic=0.45` |

## 契約判断の記録

- **habituation recovery は all-reached 維持** (WP 指定の選択肢のうち現行挙動)。presentation
  由来と明示されたのは return_count / cooccurrence / training delta (topk_only) のみ。
- **rel の min-max は floor 通過後の pool を対象**。「pool 内」の解釈として、
  提示不可能 (sub-floor) 候補を min/max の錨に使わない方が「実際に競合する候補」の
  意味論に合う。統合 test の固定期待値もこの解釈で校正。
- training delta の `topk_only=False` (debug 用 config) は all-reached のまま。
  diversity 契約が変えるのは default (`True`) の presented-only 経路のみ。
- flag OFF / diversity<=0 は pool 拡大・floor・MMR・cohort penalty を一切通さない
  (bit-for-bit legacy)。`query()` が flag OFF 時 `diversity=None` を下流に渡すため、
  `_query_internal` 直接 caller (prefetch / dream loop) は常に legacy。

## StubEmbedder 校正 (tests/integration/test_engine_explore_diversity.py)

dim=512・md5 決定論 embedder・`wave_boost_weight=0` (fresh node の final 順序 =
raw cosine 順序) で固定。X1..X3 = original_id "book-x" クラスタ、Y1/Y2 = "book-y"。

QUERY="alpha beta gamma delta epsilon" の測定値:

- plain top-5: `[Y2, Y1, X2, X1, X3]` (book-x 3 連続)
- d=0.8: `[Y2, X2, Y1, Z1, X1]` — book-x run 3→1, Jaccard@5 = 0.667
- d=1.0: `[Y2, X2, Y1, Z1, W1]` — book-x 3→1 (X2 のみ)

5 query × d∈{0,0.5,0.8,1.0} の median Jaccard@5 (plain 比): **1.0 / 1.0 / 1.0 / 0.6**
(非増加 ✓)。order level の変化は d=0.8 で 4/5 query。

本番 (RURI 狭 cosine 帯) での Jaccard は acceptance (dogfooding probe ③) で別途測定。
Stub の min-max 圧縮が効く構造 (final score 帯が狭いほど redundancy が相対的に支配)
は本番と同方向だが、数値の外挿はしない。

## 検証記録

- red: `ModuleNotFoundError: gaottt.core.diversity` / `TypeError: GaOTTTConfig ...
  unexpected keyword argument 'explore_diversified_presentation_enabled'`
- green: unit 12 / integration 8 / REST parity +1 = 全 pass
- full suite: 1313 passed, 11 skipped, 既知 flaky `test_probe_initialize_success_returns_ok`
  のみ失敗 (単独再実行で green)
- ruff: touched files all passed
- smoke: `scripts/rest_smoke.py` / `scripts/mcp_smoke.py` 両方全シナリオ green
