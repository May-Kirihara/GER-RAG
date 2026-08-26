# MCP Reference — Memory Tools

GaOTTT の中核となる 6 つの記憶ツール。詳細仕様は [`SKILL.md`](../../SKILL.md) のツールセクションを正とする。

## remember

知識を長期記憶に保存する。

```
remember(
  content: str,
  source: str = "agent",                  # agent/user/system/compaction/hypothesis/task/commitment/value/intention/style/relationship:<name>
  tags: list[str] | None = None,
  context: str | None = None,
  ttl_seconds: float | None = None,       # 既定: source が hypothesis なら 7日、task なら 30日、commitment なら 14日
  emotion: float = 0.0,                   # [-1.0, 1.0]、|magnitude| が boost
  certainty: float = 1.0,                 # [0.0, 1.0]、半減期 30日で減衰
)
→ "Remembered. ID: <uuid>" or "Already exists in memory (duplicate content)."
```

## recall

重力波伝播による検索。`prefetch` キャッシュを透過消費する。

```
recall(
  query: str,
  top_k: int = 5,
  source_filter: list[str] | None = None, # 制限フィルタ: 指定 source のみが seed pool に入る
  wave_depth: int | None = None,
  wave_k: int | None = None,
  force_refresh: bool = False,            # True で prefetch キャッシュを無視
  persona_context: list[str] | None = None, # Phase J Stage 2: 明示的 ID リスト。seed pool に強制注入 + persona boost
  tag_filter: list[str] | None = None,   # Phase J Stage 2: タグ substring (OR 一致) の node を seed pool に強制注入。source_filter を bypass
  output_mode: str = "full",             # MCP 専用トークン節約。"full"=全文, "compact"=300字切詰, "ids"=ID+スコアのみ
  auto_route: bool = True,               # Phase O Stage 3: 構造化質問なら reflect 並走 + summary 添付
  mode: str = "detail",                  # Phase O Stage 4: "list" で content を 80 字に切り詰め (REST にも効く)
  passive: bool = False,                 # Ambient Recall: True で read-only (重力場を摂動しない)
)
→ 各結果に id=<uuid> が含まれる
```

**Passive recall (Ambient Recall):** `passive=True` で **read-only recall** になる。検索・wave 伝播・scoring はそのまま走り結果も同一だが、末尾の simulation update を丸ごとスキップする — **mass 更新なし・query attraction displacement なし・co-occurrence edge なし・`last_access` 更新なし**。`recall` を「重力勾配を供給する TTT ステップ」から「摂動なしの観察」に切り替える。自動/バックグラウンド recall（Claude Code の ambient-recall フックがこれを呼ぶ — [Ambient Recall](Guides-Ambient-Recall.md)）のためにあり、ノイズクエリが無制御の TTT シグナルになるのを防ぐ。`passive=True` の recall は prefetch キャッシュを **読む** が **書かない**（passive 結果が後続の active recall に cache hit させて simulation update を握り潰すのを防ぐ）。`training_delta` は全 0（場が動いていないことの正直な報告）。既定 `False` は legacy の「recall は訓練ステップ」挙動を保つ。MCP / REST 両対応。

**`tag_filter` vs `source_filter` の使い分け:**
- `source_filter` — 制限フィルタ。seed pool の範囲を絞る（source が一致しないノードは入場できない）
- `tag_filter` — 拡張注入。embedding 距離に関わらず tag が一致したノードを強制的に seed pool へ追加。`source_filter` も bypass する（呼び出し側の明示的指定が優先）

**埋め込みは cross-lingual ではない:** RURI v3 は日本語特化モデルで、`recall` は実質「クエリと**同じ言語**で書かれた記憶」しか引けない — 英語クエリは英語の記憶を、日本語クエリは日本語の記憶を surface させ、両者を橋渡ししない。RURI は EN→EN / JA→JA のモノリンガル検索はこなすが、EN↔JA を共有意味空間で揃えない。`cos` は当たり外れに関わらず狭い高スコア帯（実測 0.74〜0.89）に入るため、言語ミスマッチは **黙って失敗する**（エラーも低スコアも出ない）。探したい記憶の言語に合わせて `query` を書くこと。クエリとターゲットの言語が異なる場合は `tag_filter` / `source_filter` でターゲットを明示注入する。詳細・実測データ・multilingual モデルへの移行可否は [Operations — Troubleshooting](Operations-Troubleshooting.md) 参照。

**`output_mode` の選択:**
- `"full"` — 全文返却（詳細確認時）
- `"compact"` — 300 字切り詰め（通常利用、トークン節約に推奨）
- `"ids"` — ID + スコア行のみ（大量 ID を把握したいが内容不要な場合）

**Score breakdown (Phase O Stage 1):** 各結果に additive な内訳が 1 行で付く。`final_score = (vcos·decay + wave + mass + emo + cert) × sat` を literal に再現する 8 数 + 補助情報 (`persona_prox`, `cos`, flags `[bm25]` / `[forced]`)。LLM caller が「mass で勝ってる」「semantic 弱い」を一発判定できる:

```
[1] id=abc12345... (score=0.4231, virtual_score=0.1850, source=agent, displacement=0.0234)
  breakdown: cos=0.142 vcos=0.185·decay=1.000 +wave=0.060 +mass=0.245 +emo=0.000 +cert=0.000 ×sat=0.910 persona_prox=0.000
```

| field | 意味 | 出方 |
|---|---|---|
| `cos` | `raw_cosine` — pure cosine(query, original_emb) | informational (sum に入らない) |
| `vcos` | `virtual_cosine` — query · virtual_pos (displacement 反映) | sum に入る |
| `decay` | recency decay multiplier — **Phase T Stage 2 以降は half-life + floor 契約** (`semantic_half_life_seconds` 既定 604800=7日 / `semantic_floor` 既定 0.35。`semantic_halflife_enabled=False` で legacy 秒 rate `delta` に戻る、その際 startup に deprecation warning) | vcos に掛かる |
| `wave` | gravity wave propagation の追加項 | additive |
| `mass` | `α · log(1+mass)` | additive |
| `emo` | emotion weighting | additive |
| `cert` | certainty boost | additive |
| `sat` | habituation saturation multiplier | 全体に掛かる |
| `persona_prox` | persona-graph 近接度 | informational (wave に baked-in) |
| `[bm25]` flag | BM25 lexical hit (informational) | wave に baked-in |
| `[forced]` flag | `tag_filter` / `persona_context` で強制注入された | informational |
| `qualified` | **Phase T Stage 3**: relevance qualification の verdict。`true` / `false` (fallback pick) / `null` (qualification flag 両方 OFF の legacy) | informational (順序に効く、下記) |
| `direct_score` | **Phase T Stage 3**: pre-saturation の semantic-direct 項 = `virtual_cos_norm × decay_factor` | informational |
| `field_score` | **Phase T Stage 3**: pre-saturation の field 項 = `wave + mass + emo + cert` | informational |
| `lensing_gap` | query path では `virtual_cos_norm − raw_cos` として populate (**Phase T Stage 3**。旧: ambient lensing slot 専用の gap signal) | informational |
| `c=` | **Phase U WP-5 selection trace**: item の structural cluster key (`cohort_id` OR `original_id`、8 字 prefix) | informational |
| `src=` | **Phase U WP-5**: candidate generation の由来 — `"raw"` (raw FAISS seed pool) / `"virtual"` (virtual FAISS seed / virtual neighbor expansion) / `"forced"` (tag / persona 注入) | informational |
| `learn=±` | **Phase U WP-5**: Stage 4 TTT learn-set membership。passive / synthetic / ttt-OFF recall では **出ない** (learn set = all reached なので True は情報を持たない — WP-5 契約では `True` ではなく segment 省略) | informational |

REST (`POST /recall`) では `items[].score_breakdown` に上記 field がそのまま JSON で返る。`expose_score_breakdown=false` で全体 off (legacy 互換用)。

**Direct relevance qualification (Phase T Stage 3、**Phase U WP-1 で default ON 昇格**・rollback `GAOTTT_DIRECT_QUALIFICATION_ENABLED=false`):** ON のとき direct 候補の presentation 選択が **qualified-first** になる — forced items (従来の RRF/raw 順序を維持) → qualified natural items (`final_score` 順) → 不足分のみ **fallback** (unqualified、`final_score` 順)。qualification は score 値のみで判断 (source 分岐ゼロ)、3 軸の OR: `raw_cos ≥ direct_raw_cosine_min` (決定論的軸) / `virtual_cos_norm ≥ direct_virtual_cosine_min` (正規化軸、新規計算) / BM25 lexical strength (score ≥ `direct_bm25_absolute_min` **AND** top score 比 ≥ `direct_bm25_relative_min`)。fallback pick は breakdown `qualified=false` + reason 行 `gravity pick (below relevance floor)` で明示され、TTT 更新も gate される (Stage 4 併用時)。MCP formatter は breakdown 行末尾に `q=+/q=- d=.. f=.. gap=..` を追記するだけで既存 segment は不変。**raw-top rescue (Phase U WP-4b、`direct_rescue_raw_rank=3`)**: qualified な natural item のうち pool 内 raw cosine rank が本値以下の item は qualified group の先頭 tier に lift される (生成直後の genesis kick で displacement が逸脱し raw rank 1 でも top-K から消える病理の修正。`0` で無効)。閾値の較正根拠は golden corpus baseline (raw p50=0.764 の狭帯に対し 0.75 で 70.6% qualified) — [docs/notes/phase-t/](../notes/phase-t/) および [Plans — Phase T](Plans-Phase-T-Semantic-Requalification.md)。

**Training delta trailer (Phase O Stage 2):** recall 出力の末尾に `## 訓練差分` セクションが付く。caller (LLM) が起こした state 変化 (backward pass) を可視化:

```
## 訓練差分
wave_reached=12 depth=2 persona_hop=3 (top-k only)
Δmass top: abc12345.. +0.0034, def67890.. +0.0012, fed09876.. +0.0008
Δ|disp| top: abc12345.. +0.0124, def67890.. -0.0050, fed09876.. +0.0021
```

| field | 意味 |
|---|---|
| `displacement_changes` | dict<node_id, Δ\|displacement\|> — post − pre (signed)。Phase I Stage 2 query attraction の literal な観測 |
| `mass_changes` | dict<node_id, Δmass> — Phase M self-force filter 適用後 |
| `wave_reached_count` | wave が触れた node 数 (informational) |
| `wave_max_depth` | 設定 / 要求された wave depth |
| `persona_hop_reached` | persona graph (Phase J) 経由で触れた node 数 (`persona_proximity > 0`) |
| `supernova_triggered` | recall path では常に `False` (parity field、ingest path で意味を持つ) |
| `cache_hit` | `True` のとき simulation 走らず (prefetch cache served)、delta dicts は空 |
| `topk_only` | default `True`、top-K 結果の node のみ delta dict に含める (context 経済)。`False` で reached 全体 |

REST (`POST /recall` / `POST /explore`) では `training_delta` フィールドにそのまま JSON で返る。`training_delta_enabled=false` で全体 off。

**Auto-routed reflect (Phase O Stage 3):** query 形式 (surface form) が構造化された aspect 問い合わせに一致したら (例: 「現在 active な commitment」「持っている value」「今やってる task」「my intentions」) `recall` / `explore` は **対応する `reflect` aspect を並走実行** し、結果を末尾に append:

```
## 関連 reflect サマリ (auto-routed)
_aspect_: `commitments` (query 形式から自動判定 — 関連した state snapshot を併走実行)

Active commitments (3 total, showing top 10):
  id=abc12345 deadline=2026-05-31 (+17.0d) | niceboat self-knowledge を完了する
  ...
```

| field (`routing_hint`) | 意味 |
|---|---|
| `aspect` | 一致した aspect 名 (例 `"commitments"`, `"values"`) — 一致なしは `null` |
| `pattern_matched` | classifier がパターン一致したか (bool) |
| `auto_routed` | 実際に `reflect` が並走実行されたか (bool) — `auto_route=False` か config off だと `false` |
| `reflect_summary` | 並走実行された場合の整形済み summary 文字列、なしなら `null` |

判定は **query 形式 (surface form) ベース、source 分岐ゼロ** — Phase M の単一規則を侵さない (caller の質問形式を見るだけで、physics rule は一切触らない)。一致しない自由文 query は legacy free-form recall のまま動作。`auto_route=False` で単発無効化、`config.auto_route_enabled=False` で全体無効化。

REST (`POST /recall` / `POST /explore`) では `routing_hint` フィールドにそのまま JSON で返る。

**List mode (Phase O Stage 4):** `mode="list"` で各結果の `content` を `config.list_mode_excerpt_chars` (既定 80) 字に切り詰め、改行を空白に置換。`top_k=20, mode="list"` で 1 リクエスト ≈ 20 行のスキャン用インデックスを取得 → 興味ある id に対して `recall(query=..., top_k=1, mode="detail")` で深掘り、という 2-step pattern を支える。**MCP / REST 両方で同じ truncate が wire 上に乗る** (MCP 専用の `output_mode` とは独立、`output_mode` は文字列表示の控除、`mode="list"` は service 層の payload 控除)。

## ambient_recall

構造化された **passive (read-only) recall**。1 回の passive recall から `<gaottt-ambient-recall>` ブロックを組み立てる — [Ambient Recall](Guides-Ambient-Recall.md)（Claude Code `UserPromptSubmit` フックが毎ターンこれを呼ぶ）の本体。明示的に `recall` を呼ばなくても長期記憶が文脈に注入される。

```
ambient_recall(
  query: str,
  direct_k: int = 2,           # ① 直接ヒット件数
  min_score: float | None = None,  # relevance gate しきい値。None → config.ambient_min_score (既定 0.70)
  exclude_tags: list[str] | None = None,  # substring マッチで全スロット候補から除外
  expose_breakdown: bool = False, # 各 slot 行末に [raw=.. virt=.. bm25 mass=..] を追記
  recently_surfaced: dict[str, int] | None = None,  # {node_id: count} session-novelty decay
)
```

`exclude_tags`（**Refinement Stage 2**、`None` または `[]` で no-op）は direct / lensing / persona の全スロット候補に対し substring マッチで除外を適用する。本番フックは `GAOTTT_AMBIENT_EXCLUDE_TAGS=smoke-test,test` を default forward し、test 用 memory を corpus に残しつつ ambient 注入だけ silent に保つ（[Plans — Ambient Recall Refinement](Plans-Ambient-Recall-Refinement.md) Stage 2、persona slot 再ランクは Stage 1）。`expose_breakdown`（**Refinement Stage 3**、default off）は Phase O Stage 1 の `ScoreBreakdown` を slot 粒度で露出する — direct / lensing には recall 経由の full breakdown、persona には mass + raw cosine の minimal breakdown が attach され、formatter が ` [raw=.. virt=.. bm25 mass=..]` を行末に追記する。debug session / measurement (Stage 5) で opt-in、本番フックは `GAOTTT_AMBIENT_EXPOSE_BREAKDOWN=1` で同等。`recently_surfaced`（**Lateral Association Stage 1**、`None` または `{}` で no-op）は「過去 N turn で何回 surface したか」の `{node_id: count}` map で、各スロットの ranking score に `config.ambient_novelty_decay ** count`（既定 0.7）を乗じる — 直近で出た memo を 1-2 turn ローテーションすることで「〇〇といえば〜だったよな」の **新鮮さ** 軸を機構化する（[Plans — Ambient Recall Lateral Association](Plans-Ambient-Recall-Lateral-Association.md) Stage 1）。本番フックは `<gaottt-ambient-recall>` ブロック末尾の `<!-- ambient-ids ... -->` manifest を過去 `GAOTTT_AMBIENT_NOVELTY_TURNS` turn (既定 5) 分 parse して自動的に組み立てる。プログラム呼び出しから手動で渡す場合は、自前で surface 履歴を持っている caller のみ意味あり（典型的な agent 自己 invoke では空 / None で OK）。

組み立てるスロット:

| スロット | 中身 |
|---|---|
| ▼ 直接ヒット | `final_score` 上位 `direct_k` 件 |
| ▼ 重力レンズ | `virtual_cosine − raw_cosine` の gap 上位 **K 件** (Lateral Association Stage 3、`config.ambient_lensing_max_k` 既定 2) — embedding 的には query から遠いのに、Phase I/J の displacement が場の重力で query 近傍まで引き寄せた記憶。**場が学習した類推**で、素の embedding 検索には出せない枠。各 pick は独立に `min_score`/`min_gap` を clear する必要あり (quota 緩和なし)。複数の lateral 連想 ("X といえば Y で、Y といえば Z") が同 turn で同時発火する核。各 pick の行末には `gap +0.42 · resonance 0.62` のように [**Stage 5**](Plans-Ambient-Recall-Lateral-Association.md) で導入した `resonance` (cooccurrence-derived trust 軸、`[0, 1)`、過去 active recall で direct hits と一緒に引かれた頻度) も付く |
| ▼ ⚠ 矛盾 | surface 記憶の `contradicts` エッジのペア |
| ▼ いま誰として | active な declared value/intention 1 行（grounding） |

各エントリは provenance メタ（`source · certainty · age`）付き。**relevance gate — BM25「強一致」+ semantic OR gate (Phase T Stage 5、default ON)**: 主軸は **語単位（Sudachi）BM25 の「強一致」gate** — corpus 専用の word-level BM25 index でプロンプトをスコアし、top BM25 が `config.ambient_bm25_min_score`（既定 32.0）以上なら即 accept (従来どおり)。**BM25 reject は即空返しにならない** — passive recall を実行し、`max(virtual_score) ≥ ambient_min_score` **OR** `max(raw_cos) ≥ config.ambient_semantic_raw_min`（既定 0.60、Phase T 新設）なら注入、両軸とも下回れば空返し。off-topic 抑制は維持 (`ambient_gate_or_semantic=False` / `GAOTTT_AMBIENT_GATE_OR_SEMANTIC=false` で legacy BM25 veto に rollback)。dense cosine の `virtual_score` も char-3gram BM25 も大規模コーパスで on/off-topic を分離できず（4 ラウンド校正、2026-05-21）、語単位 BM25 だけが「強一致」を弁別できた（`ambient_gate_use_bm25=False` or `bm25-sudachi` extra 未導入時は `virtual_score` gate にフォールバック、`min_score` 引数はこの時のみ効く）。**Composite mode (Phase U WP-3) は実装済みだが default は `"or"` のまま** — `ambient_gate_mode="composite"` にすると BM25 強一致 OR (参照分布 percentile + margin + raw floor の 3 軸 AND) で判定し、参照 artifact が使えない場合は fail-closed (BM25 のみ accept 経路)。事前登録昇格 gate (held-out FP=0 ∧ FN≤10%) を較正が満たさなかったため昇格せず、R3 (off-topic 通過) は user 判断待ち。詳細は [Operations — Troubleshooting](Operations-Troubleshooting.md)「ambient composite gate」節・[docs/notes/phase-u/ambient-composite-calibration.md](../notes/phase-u/ambient-composite-calibration.md)。

**Gate diagnostics (Phase T Stage 5 + Phase U WP-3 拡張):** 空返しの理由が段階別に判別できる。`AmbientRecallResponse.gate_diagnostics` が pipeline 順の staged counts（`candidates_generated` / `after_tag_exclusion` / `after_dump_filter` / `semantic_qualified` / `direct_selected` / `lensing_selected`）と gate 入力（`bm25_top_score` / `bm25_gate` / `semantic_max_virtual` / `semantic_max_raw`）、離散一意の `empty_reason`（`bm25_veto` / `bm25_and_semantic_below_threshold` / `no_candidates` / `all_tag_excluded` / `all_dump_filtered` + **composite mode のみ** `composite_reject` / `composite_pool_too_small` / `composite_reference_unavailable`、非空なら `null`）を **空・非空に関わらず常に** 返す。composite mode (`ambient_gate_mode="composite"`) で判定した場合は **composite 軸** も populate される: `virt_percentile` (参照分布に対する [0,100]) / `margin` (pool top-1 virtual − pool virtual median) / `raw_top1` (ambient_recall 内部の独自 raw FAISS 検索、`expose_score_breakdown` 非依存) / `composite_signal` (accept `bm25_strong`|`semantic_composite` / 拒否理由)。mode="or" ではこれらは `null` のまま。triage 表は [Operations — Troubleshooting](Operations-Troubleshooting.md)。MCP は空のとき `(関連する記憶なし)` センチネルを返す（フックはこれを見て注入しない; `expose_breakdown=True` のときのみ `gate: <reason|passed> (...)` 診断行を sentinel の後に追記 — composite 評価時は `pct=… margin=… raw=… sig=…` segment も追記、default 出力は byte-identical）。REST は `AmbientRecallResponse` JSON（`count == 0` + `gate_diagnostics`）。常に passive — 重力場を一切摂動しない。チューニングは [Operations — Tuning](Operations-Tuning.md) の `ambient_*`・Phase U 節、設計は [Plans — Ambient Recall Enrichment](Plans-Ambient-Recall-Enrichment.md)・[Plans — Phase T](Plans-Phase-T-Semantic-Requalification.md)・[Plans — Phase U](Plans-Phase-U-Review-Hardening.md)。

## explore

温度を上げた創発的探索。離れた記憶も引き寄せる。

```
explore(
  query=...,
  diversity=0.0-1.0,
  top_k=10,
  persona_context: list[str] | None = None,  # recall と同じ注入引数
  tag_filter: list[str] | None = None,
  auto_route: bool = True,                    # Phase O Stage 3: recall と parity
  mode: str = "serendipity",                  # Phase O Stage 5: "dormant" で counter-importance sampling
)
```

- `diversity=0.0` 通常検索に近い
- `diversity=0.5` 適度な探索（既定）
- `diversity=1.0` 最大多様性

**Diversified presentation (Phase T Stage 6、**Phase U WP-5 で default ON 昇格**・rollback `GAOTTT_EXPLORE_DIVERSIFIED_PRESENTATION_ENABLED=false`):** flag ON かつ `diversity > 0` のとき、engine 層で natural slot の最終選択を **MMR (maximal marginal relevance)** 化する — scoring 済み results list の上位 `top_k × explore_diversity_pool_multiplier` (既定 4) 候補から、`explore_min_semantic` (既定 0.45) の raw-cosine floor を課した上で、`λ·rel − (1−λ)·redundancy − diversity × explore_cohort_penalty` (λ = 1 − 0.5·diversity、redundancy は raw embedding cos、cohort penalty は同一 `cohort_id`/`original_id` cluster の連続採用抑制) を greedy 選択。forced (`tag_filter`/`persona_context`) item は従来規則の順序を維持し MMR 対象外。`diversity=0.0` / `None` は **flag ON でも完全 legacy 経路** (pool 拡大・floor・MMR をすべて bypass、bit-for-bit)。MMR は presentation の切り取り幅のみを変え、wave 到達性・simulation 系更新 (last_access / evaporation / temperature / orbital) は従来の all-reached 契約のまま。低関連候補への TTT 更新保護は `ttt_qualification_enabled` (Phase U WP-1 昇格済み) が担う。

**Selection trace (Phase U WP-5):** explore response (`ExploreResponse`) に `wave_depth` / `wave_reached` が追加され、MCP 出力の header 直後に **`wave: depth=N reached=M`** 行が付く (wave 伝播の実効 depth と到達 node 数 — dormant mode では出ない)。各 item の breakdown には recall と同じ selection-trace segment (`c=` cluster key / `src=` raw|virtual|forced / `learn=±` learn-set membership) が **strictly trailing** で追記される (既存 segment の書式・順序は不変)。

**Dormant mode (Phase O Stage 5):** `mode="dormant"` で wave / FAISS を完全に bypass し、**自己発信 source class** (`agent` / `value` / `intention` / `commitment` / `note` / `reference`) のうち以下 3 条件を満たす node からランダムに `top_k` 件を返す:

| 条件 | しきい値 (config) |
|---|---|
| `last_access` が cutoff より古い | `dormant_age_threshold_seconds` (既定 **7 日**、2026-06-01 に 30d から昇格) |
| `mass ≤ θ` (mature gate 未満) | `dormant_mass_percentile` (既定 p10、設定時は `dormant_mass_threshold=2.0` 絶対値より優先) |
| `metadata.source ∈` allowlist | `dormant_source_classes` (上記 6 種) |

`query` は **ignore** (任意の placeholder で OK)。`training_delta` / `routing_hint` は `None` (wave 走らず、aspect 意図も無し)。出力は `## 関連 reflect サマリ` / `## 訓練差分` 無しの dormant 専用 formatter で「💭 Dormant memories surfaced (N):」 prefix から始まる。**設計判断**: `source` 列挙は Phase M 「source 分岐ゼロの単一規則」を侵さない — physics rule (mass update / Hooke / kick) は branching せず、ここでは「自己発信 class」という structural identifier に対する filter (query intent) として使う。

## reflect

メモリ状態の分析。**11 種類の aspect**:

| aspect | 内容 |
|---|---|
| `summary` | 全体統計 |
| `hot_topics` | 高質量ノード |
| `connections` | 強い共起エッジ |
| `dormant` | 長期間未アクセス |
| `duplicates` | 近接重複クラスタ |
| `relations` | 有向リレーション |
| `tasks_todo` / `tasks_doing` / `tasks_completed` / `tasks_abandoned` | Phase D タスク系 |
| `commitments` / `values` / `intentions` / `relationships` / `persona` | Phase D 人格系 |

→ Phase D の使い方は [Tasks & Persona](MCP-Reference-Tasks-and-Persona.md)

## ingest

ファイル / ディレクトリの一括取り込み。

```
ingest(
  path: str,
  source: str = "file",
  recursive: bool = False,
  pattern: str = "*.md,*.txt",
  chunk_size: int = 2000,
  include_tool_results: bool = False,
)
```

対応形式:

- `.md` — `##` 見出し or サイズで分割
- `.txt` — 段落で分割
- `.csv` — 行単位、`content`/`text`/`body`/`message` 列を自動検出（`id` 列があれば `original_id` に使う）
- `.jsonl` — Claude Code 形式のチャット履歴（user prompt + 続く assistant 群を **1 exchange** にまとめて 1 document、CLI 注入や `permission-mode` / synthetic 行は skip、`tool_use` は `[tool:<name>]` で要約）

`include_tool_results` は `.jsonl` 専用。`true` にすると `tool_result` の生 stdout/stderr も exchange 本文に追記する（DB 容量が増えるので既定 `false`）。チャット履歴を流し込むときは `pattern="*.jsonl"`, `source="claude-code"` を渡す。詳細は [Operations — Ingestion](Operations-Ingestion.md)。

> **既存 backend は再起動が必要**: `.jsonl` ディスパッチと `include_tool_results` は loader 改修後の機能。古いプロセスがメモリにロードしていると `.jsonl` をプレーンテキスト扱いする。

## get_node

ID 指定で 1 件取得 — **read-only、重力場を一切迂回**。content / provenance (source, tags, certainty, emotion) / 物理状態 (mass, temperature, displacement) を返す。seed pool を通らない唯一の取得経路。

```
get_node(node_id: str)
```

- `reflect(hot_topics)` 等で id が見えたが `recall` が到達できない (lexical / gravity capture で seed pool が占拠されている) 場合に使う
- mass / co-occurrence / displacement / prefetch に一切触れない (passive by construction)
- archived / 不存在 id は `"Node not found."` を返す

**recall との使い分け**: id が分かっているなら `get_node`、意味で探すなら `recall`。

## auto_remember

会話 transcript から保存候補をヒューリスティック抽出（**保存はしない**）。

```
auto_remember(transcript=..., max_candidates=5, include_reasons=True)
```

抽出される傾向:
- 決定・結論・採用/却下
- 失敗・成功・エラー・解決
- ユーザーの好み・禁止・制約
- 教訓・次回への申し送り
- 数値（メトリクス候補）

返り値の各候補には推奨 `source` と `tags` が付くので、内容を確認してから `remember` で正式保存する。

## save_candidates

`auto_remember` を Stop / turn-end hook 用にラップした **書き込み側の symmetric 機能** ([Plans](Plans-Save-Candidates-Hook.md))。`<gaottt-save-candidates>` block を返し、Claude Code Stop hook が **次 prompt 先頭に注入** する (option A = ambient_recall と同 pattern)。候補ゼロ時は sentinel `(保存候補なし)` (hook は block タグの有無を見て無音化)。

```
save_candidates(transcript, max_candidates=3, include_reasons=True, include_persona=True)
```

- `transcript`: 直近 N 会話 (Claude Code hook は `transcript_path` から自動抽出、env `GAOTTT_SAVE_CANDIDATES_TURNS` で N 制御)
- `max_candidates`: 上位 N 件 (default 3、token-budget 配慮)
- `include_persona`: ambient_recall persona slot との重複を避けたい場合 `False`

**設計原理**: 観察層 (lens で候補を浮かべる) は自動化、physics 層 (`remember` 呼び出し = mass の入口) は agent の能動的判断のまま。Articulation as Carrier (mass の入口は articulation の volitional moment) と Phase M 単一規則の前提を崩さない (詳細 [[observation-vs-physics-boundary]])。

## ソースの使い分け

| source | TTL | 用途 |
|---|---|---|
| `agent` | 永続 | あなた自身の判断・発見・学び |
| `user` | 永続 | ユーザーの発言・好み・指示 |
| `compaction` | 永続 | コンテキスト圧縮時の退避 |
| `system` | 永続 | システム情報 |
| `hypothesis` | 7 日 | 仮説（自動消滅） |
| `task` | 30 日 | タスク（要 complete/abandon/revalidate） |
| `commitment` | 14 日 | 期限付き約束 |
| `value` / `intention` / `style` / `relationship:<name>` | 永続 | Phase D 人格層 |

→ 関連: [Tasks & Persona](MCP-Reference-Tasks-and-Persona.md), [Maintenance](MCP-Reference-Maintenance.md)
