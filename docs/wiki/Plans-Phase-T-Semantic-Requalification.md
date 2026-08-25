# Plans — Phase T: Semantic Requalification (retrieval-quality sub-plan)

- 状態: **実装完了 — Stage 1-6 (2026-08-25)**。Stage 2 (semantic half-life + floor) と Stage 5 (ambient OR gate) は **default ON**、Stage 3 (direct qualification) / Stage 4 (TTT qualification) / Stage 6 (explore MMR) は **default OFF + env opt-in** (昇格判断の根拠は §3 rollout gate と [docs/notes/phase-t/](../notes/phase-t/) の較正記録。multiverse 運用では supervisor が GAOTTT_* env を strip するため実質 code default 昇格時に有効化 — [Operations — Tuning](Operations-Tuning.md) §Semantic Requalification の注意)。**Phase 2 (Stage 7-8: backend lifecycle / BM25 永続化) は未実装** — 別 plan で実装するまで起点 handoff は closed にならない (§2 スコープ宣言どおり)。docs 更新 (WP-6) 完了、handoff 更新は PM 担当
- 改訂履歴: v3 (2026-08-25。Codex review round1 Reject → v2、round2 残 blocking 2 件 + QA approve-with-changes を反映。round2 の残余 blocking は reviewer が文面指定済みの機械的修正のため plan 再レビューは省略し、Codex final diff review で検証する)
- 起点ハンドオフ: [docs/handoff/2026-08-25-post-recovery-retrieval-quality.md](../handoff/2026-08-25-post-recovery-retrieval-quality.md)
- 前提障害: semantic search silent failure (復旧済み、[2026-08-25-semantic-search-silent-failure.md](../handoff/2026-08-25-semantic-search-silent-failure.md))
- **スコープ宣言**: 本 plan は handoff のうち **retrieval quality 系 (実装順 1-6) のみ** を扱う sub-plan である。handoff の P0 cold-start / backend lifecycle (実装順 7-8) は **Phase 2 plan で別途実装するまで本 handoff は closed にならない**。運用回避 (再接続待ち) は acceptance の満たし方ではない。
- 命名注記: 「Phase R」は旧 GER-RAG 改名プロジェクト (R0-R11) が使用済みのため T を採用。physics に近い (decay 契約・TTT gate) 変更を含むため phase letter を消費する。Roadmap 編集時に整合すること。

## 1. 目的

SQLite/FAISS 不整合復旧後も残る検索品質課題を、wiki 哲学 (意味的観測を土台に、重力場が順位・連想・人格を育てる) を守ったまま解消する:

1. semantic 項が時間経過で数値上消滅し mass/wave が順位を支配する (decay の単位・契約問題)
2. 低 relevance・高 mass ノードが direct top-K を占有し、active recall で自己強化される (bad gradient)
3. ambient recall の BM25 gate が veto として働き、関連ノードが存在しても無言で空返しになる
4. explore の diversity が最終 presentation に反映されず通常 recall と同一結果になる

mass/wave 支配の因果は Stage 1 baseline で確定させるまでは **強く支持された仮説** として扱う (Codex review 指摘)。

## 2. スコープ

### 今回 (Stage 1-6)

| Stage | 内容 | 主な touched files |
|---|---|---|
| 1 | 観測 baseline script (score 項寄与率・qualification 率・ambient empty reason・recall/explore Jaccard) | `scripts/score_baseline.py` (新規) |
| 2 | semantic decay を秒 rate から **half-life + floor** 契約へ | `gaottt/core/scorer.py`, `gaottt/config.py`, `gaottt/core/engine.py` |
| 3 | direct 候補の **relevance qualification** (正規化軸 + lexical strength) + breakdown の direct/field/lensing 分離 | `gaottt/core/engine.py`, `gaottt/core/types.py`, `gaottt/services/formatters.py`, `gaottt/core/explain.py` |
| 4 | TTT 更新の **query-conditioned 項のみ** を semantic-qualified 候補に限定 | `gaottt/core/engine.py` |
| 5 | ambient gate を BM25 veto から **OR gate** へ + 段階別空返し診断 | `gaottt/services/memory.py`, `gaottt/core/types.py`, `gaottt/services/formatters.py` |
| 6 | explore の **engine 層 presentation 多様化** (MMR + cohort penalty) | `gaottt/core/engine.py`, `gaottt/services/memory.py`, `gaottt/config.py` |

### Phase 2 (別 plan / 別セッション — handoff closure に必須)

- Stage 7: backend lifecycle / staged readiness (supervisor `/route` 202 starting + proxy poll budget + engine warm 保持) — handoff P0 cold start
- Stage 8: BM25 snapshot 永続化 / background build

### 非ゴール (今回)

- mass/wave 係数そのものの再調整 (α, eta, wave_boost_weight) — Stage 2-4 効果を baseline で測ってから
- ambient gate への persona/lensing resonance 入力 — 診断フィールドで観測してから判断
- seed pool / wave 到達性の変更 (qualification は final selection のみを改善する。reached に入らない文書は救えない)
- 2,053 orphan nodes の由来調査、legacy `ger-rag` 警告抑止 (別途)

## 3. 設計

### 共通規約

- 各 Stage は **独立 feature flag**。`False` で legacy 経路に戻ること。flag 組み合わせ (特に S3 off × S4 on / S3 on × S4 off) の interaction test を必須とする。
- 新 config は `GAOTTT_<UPPER>` env で上書き可能。
- breakdown / formatter の出力変更は **追加行・追加フィールドのみ**。
- source 分岐ゼロ (Phase M 単一規則) 維持。qualification は score 値のみで判断。
- **semantic 軸の定義を混ぜない** (Codex blocking #3):
  - `raw_cos` = `dot(q, node_raw)/(‖q‖‖node_raw‖)` — 決定論的。[-1, 1]。
  - `virtual_cos_norm` = `dot(q, virtual_pos)/(‖q‖‖virtual_pos‖)` — **新規に正規化して計算**。displacement+temperature を含む (= 場の学習を反映、小さな noise は許容)。[-1, 1]。
  - 既存 `gravity_sim` (raw_score / MCP "virtual_score") は **非正規化 dot のままラベル変更しない**。final score の構成も変えない。
- **rollout gate**: Stage 2 は欠陥修正なので default ON。**較正結果 (2026-08-25 WP-C、docs/notes/phase-t/calibration-round.md) に基づく確定**:
  - `semantic_halflife_enabled=True` (Stage 2, 確定)
  - `ambient_gate_or_semantic=True` (Stage 5 に昇格 — 再現済み欠陥の修正、tier3/tier6 green、off-topic 抑制維持)
  - `direct_qualification_enabled=False` / `ttt_qualification_enabled=False` / `explore_diversified_presentation_enabled=False` (Stage 3/4/6 は **env opt-in のまま**。理由: ① Stage 3 ON 時に `lensing_gap` populate が explain 分岐 2 "lensing pick" を誤発火させ dominance 警告をマスクする観測層 bug が残存 (min-gap 閾値の follow-up が必要)、② Stage 4 ON は query_kick test の数値契約と stub 閾値感度、③ Stage 6 は golden corpus で MMR 効果 1/11 query — 本番 dogfooding で効果を確認してから昇格)
  - dogfooding probe (handoff 実測と同じ系): ①「GaOTTTの設計思想と長期記憶の運用知見」recall (top-5 の class 構成)、②「GaOTTTのsemantic search障害を復旧し…」ambient_recall (direct slot 復帰 + empty_reason)、③ explore diversity=0.8 vs recall の Jaccard。**probe ② は Stage 5 昇格で即有効。①③ は env opt-in (`GAOTTT_DIRECT_QUALIFICATION_ENABLED=1` 等) が必要 — multiverse backend は supervisor が GAOTTT_* env を strip するため、実質的には code default 昇格時に有効化される点に注意**。

### Stage 1 — baseline 観測 (scripts/score_baseline.py)

- golden corpus (`tests/perf/golden_corpus/`) から隔離 DB を build し (本番 DB 不可触)、queries.json の各 query を `passive=True` で実行して:
  - top-100 の score 項別寄与率 (semantic / wave / mass / emotion / certainty / saturation) の平均・分布
  - Stage 3 qualification 率 (raw_cos / virtual_cos_norm / lexical strength の threshold sweep 表)
  - ambient_recall の gate diagnostic (BM25 top score / semantic max / empty reason)
  - recall vs explore(diversity 0.0/0.5/0.8/1.0) の Jaccard@5
- 出力: JSON (`--out`) + 人間可読 summary。read-only (passive のみ)。
- 用途: Stage 2-6 の before/after 比較、threshold 決定根拠、default ON/OFF 判定。

### Stage 2 — semantic half-life + floor (P0)

現行: `decay = exp(-delta * age_seconds)`, `delta=0.01` (config.py:276)。10 分で ≈0.0025。**単位の契約バグ** として修正する。

```python
# scorer.py — 新関数 (compute_decay は残す)
def compute_semantic_factor(last_access, now, half_life_seconds, floor):
    age = max(0.0, now - last_access)
    return floor + (1.0 - floor) * (0.5 ** (age / half_life_seconds))

# engine.py — final 計算 (掛かる項は現行 decay と同一スロット)
factor = compute_semantic_factor(...) if cfg.semantic_halflife_enabled else compute_decay(...)
final = (gravity_sim * factor + mass_boost + wave_boost + emo + cert) * saturation
```

新 config:

| knob | provisional default | 意味 |
|---|---|---|
| `semantic_halflife_enabled: bool` | `True` | `False` → legacy `delta` 経路 |
| `semantic_half_life_seconds: float` | `604800.0` (7日) | **較正出力** (7d+floor0.35 では 6 週間で factor≈0.355 = floor 支配。baseline で再検討) |
| `semantic_floor: float` | `0.35` | **較正出力**。経年 semantic を完全消滅させない下限 |

- validation: `half_life_seconds > 0`、`0 ≤ floor ≤ 1` (pydantic / config 検証で明示 reject)。
- factor は age=0 で 1.0 (legacy と一致) — 新規ノードの既存テストは影響なし。future timestamp は `max(0, age)` で clamp (legacy `compute_decay` は >1 になり得るが、legacy path は bit-for-bit 維持し clamp しない — flag off = 完全旧挙動)。
- floor が保存するのは legacy と同じ `gravity_sim` 項 (virtual 位置の dot)。raw 意味論そのものではない — 既知の性質として文書化し、baseline で displacement 大位 node の挙動を観察する。
- `ScoreBreakdown.decay_factor` には factor を入れる (`expected_sum` はそのまま成立)。
- legacy mode 有効時は engine.startup に「`delta` は秒 rate の deprecated 契約」warning log。

### Stage 3 — direct relevance qualification (P0)

engine.query の presentation 選択 (Step 4)。**qualification は常に計算** (`expose_score_breakdown` とは独立 — Codex blocking #2):

```python
# lexical strength: 相対比契約 (corpus size 非依存)
bm25_pool = bm25.search(text, N_direct_bm25_pool)       # flag: 直接qualification用。N=top_K бюджета別 default 50
bm25_top = max score
lexical_strength(nid) = bm25_score(nid) / bm25_top      # [0,1] 相対比

qualified(nid) = (
    raw_cos[nid]            >= cfg.direct_raw_cosine_min      # 決定論的軸
    or virtual_cos_norm[nid] >= cfg.direct_virtual_cosine_min  # 正規化軸 (新規計算)
    or lexical_strength(nid) >= cfg.direct_bm25_relative_min   # 例: top score の 40% 以上
)
```

- BM25 pool 計算は `direct_qualification_enabled or expose_score_breakdown` で走る (observability flag には依存しない)。
- **選択契約** (Codex fallback contract): forced items (既存 RRF/raw 順序を維持) → qualified natural items (final_score 順) → 不足分のみ **fallback** (unqualified, final_score 順、breakdown に `qualified=False` + reason line で明示)。
- 閾値は baseline の分布から決定。**baseline 後の新暫定: raw 0.75 / virt_norm 0.75 / rel 0.40** (旧暫定 0.45/0.55 は WP-1 baseline で 100% qualified と判明し廃棄。raw p50=0.764 / p90=0.820 の狭帯に対し 0.75 で 70.6% qualified)。

新 config: `direct_qualification_enabled: bool` (default は較正後決定), `direct_raw_cosine_min`, `direct_virtual_cosine_min`, `direct_bm25_relative_min`, `direct_bm25_pool_size: int = 50`。

breakdown (informational, 追加のみ):

- `qualified: bool | None` — None = flag OFF (legacy)。breakdown 無効でも qualification 自体は計算済み。
- `direct_score: float` — **pre-saturation** の `virtual_cos_norm * decay_factor` (定義明記)
- `field_score: float` — **pre-saturation** の `wave + mass + emotion + certainty`
- `lensing_gap: float` — 既存 field (types.py:123)。`virtual_cos_norm - raw_cos` で populate (これまで query 経路では未使用)
- `fallback` は `qualified=False` で表現 (新 field 不要)

`explain.py`: fallback pick に追加 reason 行 (既存行は不変)。

### Stage 4 — TTT update qualification (P0)

`_update_simulation` の更新を **カテゴリ分解** し、query-conditioned 項のみ gate する (Codex blocking #4):

| 更新カテゴリ | 対象 | 根拠 |
|---|---|---|
| last_access 更新 | all reached (非 passive) | 観測 bookkeeping。触れた事実の記録 |
| lazy mass evaporation | all reached (非 passive) | 時間ベース保守。query と無関係 |
| sim_history / temperature | all reached (非 passive) | wave force の観測記録 |
| orbital N-body (Hooke + 近傍重力) | all reached (非 passive) | 場の自律ダイナミクス。N-body 参加者集合は維持 |
| **query kick (query_anchor displacement)** | **learn set only** | query-conditioned learning |
| **mass growth (η)** | **learn set only × confidence** | query-conditioned learning |
| **cooccurrence edge** | **presented ∩ learn set** | presentation 契約 + relevance |
| return_count (saturation) | all presented (非 passive) | presentation 的事実 (現行維持) |

- `learn set` = (qualified(nid) ∨ **synthetic recall**) ∧ ¬(forced-only) ∧ ¬passive。Stage 3 と同一 qualification。**synthetic (dream loop) recall は qualification gate を免除** — dream は自己主導の maintenance rehearsal であり user query 由来の bad gradient 経路ではない (WP-3 実装時に dream loop が learn set 制限で cooccurrence 構築を失う事象で判明、flag ON 時の回帰として test_engine_dream_loop が検知)。
- **confidence**: 複数軸を通過した場合は **通過軸すべての normalized margin の最大値** を採る (決定論的)。`margin_axis = clamp((score_axis - threshold_axis) / (1 - threshold_axis), 0, 1)`。単純 clamp(score,0,1) は使わない (Codex 指摘)。
- query kick の per-node gate: `update_orbital_state(query_scores=...)` に learn set のみを渡して kick を限定 (gravity 側の `query_scores.get(nid, 0)` 挙動を実装時に確認 — absent = kick 0 であること)。
- training delta trailer: unqualified top-K は「提示されたが学習対象外」であることを delta の値 (0) がそのまま表現。仕様として明記。

新 config: `ttt_qualification_enabled: bool` (default は較正後決定 — Stage 3 と連動。S3 off × S4 on の場合は qualification 計算のみ standalone で走らせる)。

### Stage 5 — ambient OR gate + 段階別診断 (P1)

```
BM25 strong (word-index, 従来閾値) → early accept (従来どおり)
else → passive recall 実行 →
    max(virtual_score) >= ambient_min_score
    OR max(raw_cos) >= ambient_semantic_raw_min    # 新 knob (diagnostics で計測の上較正)
    → accept / それ以外は空返し (off-topic 抑制は維持)
```

- 新 config: `ambient_gate_or_semantic: bool = True` (rollback で veto 復帰)、`ambient_semantic_raw_min: float` (provisional 0.60、baseline で較正)。
- **latency 注記**: BM25 reject だった prompt が passive recall のコストを払うようになる。pool は `direct_k*5` と小さいが、tier6 で hook latency を測定して回帰ないことを確認する。

`AmbientRecallResponse` 追加 (default 付き後方互換):

```python
gate_diagnostics: AmbientGateDiagnostics | None
# 段階別カウント (Codex 指摘の staged counts):
#   candidates_generated / after_tag_exclusion / after_dump_filter
#   / semantic_qualified / direct_selected / lensing_selected
# 判定入力: bm25_top_score / bm25_gate(True|False|None)
#          / semantic_max_virtual / semantic_max_raw
# empty_reason (離散・一意): "bm25_and_semantic_below_threshold"
#   / "no_candidates" / "all_tag_excluded" / "all_dump_filtered" / None
```

- formatter: `(関連する記憶なし)` sentinel は **一字不变**。`expose_breakdown=True` のときのみ診断行を追記 (hook default 出力は不変)。

### Stage 6 — explore presentation diversity (engine 層) (P1)

**service 層後処理では不十分** (Codex blocking #1)。engine.query に selection を移す:

```python
# engine.query 新引数
diversity: float | None = None   # None または 0.0 = 従来経路 (完全 bypass)

# diversity > 0 の場合のみ:
pool_k = top_k * cfg.explore_diversity_pool_multiplier        # 広い候補を score まで
# Step 4 で:
#   forced items は従来規則 (RRF/raw 順序) のまま MMR 選択対象外
#     (ただし redundancy 計算上は selected 集合に含める — slot を占めるため)
#   natural items は canonical MMR:
#     rel_i  = pool 内 final_score の min-max 正規化 [0,1]
#              (max == min の場合は全候補 rel = 1.0 — 全同点なら relevance で差を付けない)
#     red_i  = max cos(selected_j, i)   (raw embedding、FAISS get_vectors)
#     score_i = λ*rel_i - (1-λ)*red_i - diversity * cohort_penalty(i 既出 cluster)
#     λ = 1 - 0.5 * diversity
#   greedy に top_k 個選択
```

**更新契約 (Stage 4 のカテゴリ分解と整合 — Codex round2 blocking #1、final review で精密化)**:

- MMR 選択が変えるのは **presentation 由来の更新のみ**: `return_count` と `cooccurrence` は **MMR 後の presented ids のみ**。
- **habituation recovery は all-reached の freshness maintenance として維持** (legacy と同一。recovery は recall event 起因の減衰であり、MMR は reached 集合を変えないため MMR の対象外。final review で plan 文面を実装契約に合わせて精密化 — 「presentation 由来」は return_count/cooccurrence のみを指す)。
- **simulation 系 (last_access / evaporation / sim_history+temperature / orbital N-body) は Stage 4 と同一の all-reached 契約のまま**。MMR は maintenance を変更しない (MMR が広げるのは *scored 済み results list の切り取り幅* のみ — wave reached 集合自体は不変。seed/wave 到達性は非ゴールどおり変更しない)。
- query kick / mass growth は Stage 4 の learn set 規則のまま。

**`diversity=0.0` の legacy 同一性 (Codex round2 blocking #2)**:

- `diversity` が `None` または `0.0` のときは **pool 拡大・relevance floor・MMR・cohort penalty を完全に bypass** し現行 `engine.query` と同一経路を通る (relevance floor は diversity > 0 のときのみ適用)。
- relevance floor: pool 候補は diversity > 0 のとき `raw_cos >= cfg.explore_min_semantic` を満たすものに限る (lateral にも最低 relevance)。
- `mode="dormant"` は対象外。
- handoff 調査候補の `gamma_override` 作用確認・wave depth bonus (handoff §P1) は **不採用**: depth 情報が QueryResultItem に露出しておらず新規露出を要するのに対し、MMR + cohort penalty で acceptance (Jaccard/多様性) に十分届くと判断。depth bonus が必要になったら follow-up で追加する。
- acceptance は **集計トレンド** (golden queries の median Jaccard@5 低下 + 高 diversity クエリの 8 割が変化) とし、毎クエリ単調性は要求しない (Codex 指摘)。

新 config: `explore_diversified_presentation_enabled: bool` (較正後決定), `explore_mmr_lambda_base` は上記 λ 式に統合削除, `explore_cohort_penalty: float = 0.05`, `explore_diversity_pool_multiplier: int = 4`, `explore_min_semantic: float = 0.45`。

## 4. テスト戦略

- **unit**: scorer 契約 (half-life 境界/複数半減期/floor/age=0=1.0/未来 clamp/巨大 age の underflow 安全/invalid 設定の reject)、legacy path の未来 timestamp は >1 のまま (bit-for-bit)、config env override、ambient gate 純粋部、MMR 正規化の純粋部、lexical relative-ratio 計算。
- **integration (StubEmbedder)**:
  - Stage 2: 経年 node の semantic 項 ≥ floor×vcos / legacy flag で旧挙動 / genesis kick 併用 (新規 node 即 recall → age=0 factor=1.0、perf helper が genesis を無効化するため明示的に通常 engine で)
  - Stage 3: qualified-first 順序 (全 qualified が全 fallback に先行) / 不足 fill / `expose_score_breakdown=False` でも qualification が作用 / weak BM25 tail hit は qualify しない / strong lexical hit は qualify / `virtual_cos_norm` の bounds と zero-norm guard / forced (<, =, > top_k) との優先規則 / prefetch cache 経由でも同一順序 / breakdown identity (`expected_sum` + 新 field の整合)
  - Stage 4: passive purity / unqualified node の mass・query kick・cooccurrence 不変 / last_access・evaporation・temperature は all reached で従前どおり / relevant active recall で学習 / **誤候補自己強化 regression fixture** / qualified 1 node + unqualified 多数 (orbital `len(active_ids)>=2` 縮退なし) / training delta の意味
  - Stage 5: near-exact query で direct slot 復帰 / empty_reason の離散値 / sentinel 一字不変 / expose_breakdown 時のみ診断行 / off-topic false-positive corpus
  - Stage 6: diversity=0.0 厳密一致 (cohort penalty 込み) / pool 拡大でも presented ids のみ return_count・cooccurrence 更新 / forced は MMR 非適用 / relevance floor
  - **flag 組み合わせ**: (S3 off, S4 on), (S3 on, S4 off), (S5 off × S6 on) 等の interaction
  - REST parity: `gate_diagnostics` / breakdown 新 field の roundtrip、**REST explore `diversity` 引数の roundtrip**
  - MCP formatter: `test_mcp_tools.py` 形式の substring assert (新行・sentinel 不変)
- **smoke (parity 鉄則)**: WP-4/WP-5 完了後 `scripts/mcp_smoke.py` + `scripts/rest_smoke.py` 両方 green。
- **perf (手動, real RURI)**: tier3 (retrieval / ambient quality)、tier4 (dynamics / stability / anti-hub)、**tier6 (latency: Stage 3 BM25 pool + Stage 5 ambient 追加コスト + Stage 6 pool 拡大・pairwise cos)**、tier7 (golden regression)。real-RURI の狭 cosine 帯での qualification 率・score 分布は Stub では較正不能 — baseline script + tier3/7 で確認。**diversity 0.0/0.5/0.8/1.0 sweep は実装後にも再測定** (acceptance 判定)。
- 既存テストを削除・skip・weaken しない。

## 5. Work packages (直列 — high-risk)

| WP | Scope | 主ファイル |
|---|---|---|
| WP-1 | Stage 1 baseline script + baseline 記録 (before) | `scripts/score_baseline.py` |
| WP-2 | Stage 2 (test first → impl → baseline after) | scorer / config / engine / types |
| WP-3 | Stage 3+4 (engine 同一領域、一括。qualification 計算を共有) | engine / types / explain / formatters |
| WP-4 | Stage 5 ambient | services/memory / types / formatters / REST parity test |
| WP-5 | Stage 6 explore (engine 層 selection + service 接続) | engine / services/memory / config |
| WP-6 | docs + handoff (gate 後) | 下記 docs checklist |

WP-6 docs checklist (CLAUDE.md のドキュメント更新フローに従い網羅):

1. `SKILL.md` + `.claude/skills/gaottt/SKILL.md` (cp 同期) — breakdown 新 field / ambient 診断 / explore diversity
2. `docs/wiki/MCP-Reference-*` 該当ページ + Index — `gate_diagnostics`、breakdown `qualified`/`direct_score`/`field_score`/`lensing_gap`
3. `docs/wiki/REST-API-Reference.md` — 同一 field の REST side
4. `docs/wiki/Plans-Roadmap.md` — Phase T 行
5. `docs/wiki/Architecture-Overview.md` 設計判断表 — 意味的観測と重力場の分離
6. `docs/wiki/Operations-Tuning.md` — 新 knob 約 10 個 (half-life 系 3 / qualification 系 5 / ambient 2 / explore 4)
7. `docs/wiki/Operations-Troubleshooting.md` — legacy delta warning / empty_reason triage
8. `docs/wiki/_Sidebar.md` + `Home.md` — 本 plan ページ追加
9. `CLAUDE.md` — workflow に影響する変更があれば
10. 本 plan のステータス更新 + handoff (`docs/handoff/`) 作成

各 WP 後 PM verification。WP-2〜5 完了後: full pytest + ruff + perf tier (3/4/6/7) + smoke 両方 + **本番 dogfooding (opt-in env、probe ①②③)** + Codex final diff review + QA final review → **較正結果に基づき default ON/OFF を確定** → WP-6。

## 6. 受け入れ条件 (handoff + review 追加分)

- 数日〜数か月前の記憶でも semantic 寄与が数値上ゼロにならない (Stage 2)
- golden corpus で無関係クラスによる top-5 占有が解消 (Stage 2+3): **unrelated-class top-5 share が baseline 比 -50% 以上かつ GaOTTT 系 query で target class ≥ 2/5** (tier3/tier7 + dogfooding probe ①)
- 低 semantic・高 mass ノードが qualified 候補より上位に来ない。fallback は明示マーク (Stage 3)
- breakdown に qualification / direct score / field score / lensing gap (既存 field の populate) (Stage 3)
- passive purity 維持 / forced-only・低 relevance の mass・query kick・cooccurrence 増加なし / 誤候補自己強化 fixture (Stage 4)
- 障害記録類似 query で ambient が surface (dogfooding probe ②) / 空返し理由の段階判別 / off-topic 抑制維持 (ambient_quality tier の false-positive 数が baseline 比不増) / hook latency 回帰なし (tier6) (Stage 5)
- explore diversity=0.8 が通常 recall と完全一致しない (集計: median Jaccard@5 低下 + 8 割変化) / golden query median relevance が baseline 比 -10% 以内 (relevance 維持) / diversity=0 厳密一致 (Stage 6)
- 各 flag OFF で legacy。flag 組み合わせ interaction test。

## 7. リスクと対策

| リスク | 対策 |
|---|---|
| Stage 2 で saturation との再平衡が逆効果 (よく出る node は飽和のまま) | baseline の saturation 分布測定 + tier7。回帰時は floor 値再較正 |
| floor が displacement 歪みを恒久保存する | 既知の性質として文書化。baseline で high-displacement node の挙動観察。悪化時は floor を raw_cos 側へ移す改訂 (Phase 2 検討) |
| RURI 狭 cosine 帯で threshold が機能しない / 空を生む | baseline sweep 表で分布確認。fill 機構で結果数保証 |
| Stage 4 で Hebbian 学習量減少 → lensing/wave 弱体化 | tier3 の lensing 指標 + tier4 dynamics。回帰時 flag OFF |
| Stage 5 の off-topic false-positive 増加 / latency 回帰 | ambient_quality tier で false-positive 測定 + tier6 latency |
| temperature noise で threshold 境界が非決定的 | 決定論的軸 (raw_cos) を第一軸に、正規化 virtual は補助。margin をもって閾値設定 |
| flag 組み合わせの未検証経路 | interaction test 必須化 |
| genesis kick との相互作用 (perf helper は genesis 無効) | genesis 有効 engine での明示 integration test |
| 本番 42k corpus との挙動差 | default 確定前に secondopinion-MCP 経由の本番 dogfooding acceptance (opt-in env、operator 承認、probe ①②③) |

## 8. Assumption ledger

| # | assumption | basis | falsification | blast radius |
|---|---|---|---|---|
| A1 | retrieval 系 (1-6) を先に、lifecycle (7-8) を Phase 2 に分離してよい (本 plan は sub-plan と明示済み) | handoff 実装順 + ファイル所有分離 + Codex review のスコープ明示条件 | user が cold start 優先を要求 | Stage 7 前倒し |
| A2 | Stage 2 は default ON にしてよい (欠陥修正) | handoff「単位・契約の問題」 | tier7 で回帰 | flag OFF |
| A3 | Stage 3-6 の default は較正後に決める (現時点で default ON を確定しない) | Codex「未較正閾値の default-on は不可」 | baseline/tier 結果 | default OFF + env opt-in |
| A4 | ~~provisional 値 (7d / 0.35 / raw 0.45 / virt 0.55 / rel 0.40 / ambient raw 0.60 / explore floor 0.45)~~ → **baseline 後の新暫定**: raw 0.75 / vcos_norm 0.75 / rel 0.40 (baseline: raw p50=0.764, 0.75 で qualified 70.6%, 0.80 で 16.2%。旧 0.45/0.55 は狭帯で 100% qualified = 無意味と判明) | handoff 候補 → **WP-1 baseline で反証・更新済み** (docs/notes/phase-t/score-baseline-before.json) | dogfooding probe ① で unrelated が依然通過する場合 | config 再設定のみ |
| A5 | 空返しは gate 除外が原因 | 障害記録 node の存在 | **✅ WP-1 で確証**: golden corpus で 19/19 bm25_veto (gate score 5.8-8.8 vs 32.0)、gate OFF で 19/19 surfaced、semantic_max 0.899 ≥ 0.70 | — |
| A6 | `update_orbital_state` の query kick は `query_scores` 辞書の absent key で 0 になる (per-node gate 可能) | gravity.py の実装読み (実装時に確認) | 実装時に違反発覚 | kick gate を gravity 側に追加 |
| A7 | engine の既存未コミット変更 (復旧 session 由来) は保持したまま重ねてよい | git status が handoff 群と対応 | user が先の commit を希望 | 手順変更のみ |

## 9. オープンクエスチョン (PM 判断で進行、記録する)

- floor を `gravity_sim` 項 (現行 slot) に適用するか raw_cos に移すか — v1 は現行 slot 維持、baseline 観察後に再検討 (§7)。
- Stage 7/8 の詳細設計は Phase 2 plan で本 plan からリンクする。

## 10. 実装順

1. WP-1 baseline script → baseline (before) 記録
2. WP-2 Stage 2 → baseline after で semantic 復活確認
3. WP-3 Stage 3+4 → qualification 率 / 誤候補 fixture
4. WP-4 Stage 5 → ambient 診断
5. WP-5 Stage 6 → Jaccard (engine 層)
6. full test + perf tier 3/4/6/7 + smoke 両方 + 本番 dogfooding (opt-in env) + Codex final + QA final → default 値確定
7. WP-6 docs / handoff / GaOTTT writeback
