# REST API Reference

GaOTTT の HTTP API（FastAPI）の完全リファレンス。Phase S （2026-04-22）以降、**MCP と同じ共有サービス層** (`gaottt/services/`) を叩くため、MCP ツール 25 本と同等の操作が REST からも行えます。

ベース URL: `http://localhost:8000`（[Operations — Server Setup](Operations-Server-Setup.md)）
認証: なし（Phase 1）
Swagger UI: http://localhost:8000/docs
ReDoc: http://localhost:8000/redoc

## エンドポイント一覧

### Memory

| メソッド | パス | 対応 MCP ツール |
|---|---|---|
| POST | `/remember` | `remember` |
| POST | `/recall` | `recall` |
| POST | `/ambient_recall` | `ambient_recall` |
| POST | `/explore` | `explore` |
| POST | `/forget` | `forget` |
| POST | `/restore` | `restore` |
| POST | `/revalidate` | `revalidate` |
| POST | `/auto_remember` | `auto_remember` |
| POST | `/save_candidates` | `save_candidates` |

### Relations

| メソッド | パス | 対応 MCP ツール |
|---|---|---|
| POST | `/relations` | `relate` |
| DELETE | `/relations` | `unrelate` |
| GET | `/relations/{node_id}` | `get_relations` |

### Maintenance

| メソッド | パス | 対応 MCP ツール |
|---|---|---|
| POST | `/merge` | `merge` |
| POST | `/compact` | `compact` |
| POST | `/prefetch` | `prefetch` |
| GET | `/prefetch/status` | `prefetch_status` |

### Reflection（`reflect` ツールの全 aspect に対応）

| メソッド | パス | 対応 MCP aspect |
|---|---|---|
| POST | `/reflect/summary` | `summary` |
| POST | `/reflect/hot_topics` | `hot_topics` |
| POST | `/reflect/connections` | `connections` |
| POST | `/reflect/dormant` | `dormant` |
| POST | `/reflect/duplicates` | `duplicates` |
| POST | `/reflect/relations` | `relations` |
| POST | `/reflect/tasks_todo` | `tasks_todo` |
| POST | `/reflect/tasks_doing` | `tasks_doing` |
| POST | `/reflect/tasks_completed` | `tasks_completed` |
| POST | `/reflect/tasks_abandoned` | `tasks_abandoned` |
| POST | `/reflect/commitments` | `commitments` |
| POST | `/reflect/intentions` | `intentions` |
| POST | `/reflect/values` | `values` |
| POST | `/reflect/relationships` | `relationships` |
| POST | `/reflect/persona` | `persona` |

> **`/reflect/connections` の `bucket` query param** (オプショナル):
> `bucket=persona` / `bucket=agent_user` / `bucket=ingest` で単一バケットにフィルタ。
> フィルタは weight top-N 選択の **前** に適用され、高 weight の ingest 共起が
> persona / agent_user の関係を押し潰すのを防ぐ。無効な値は HTTP 422。

### Ingest

| メソッド | パス | 対応 MCP ツール |
|---|---|---|
| POST | `/ingest` | `ingest` |

### Phase D — Tasks

| メソッド | パス | 対応 MCP ツール |
|---|---|---|
| POST | `/tasks` | `commit` |
| POST | `/tasks/{id}/start` | `start` |
| POST | `/tasks/{id}/complete` | `complete` |
| POST | `/tasks/{id}/abandon` | `abandon` |
| POST | `/tasks/{id}/depend` | `depend` |

### Phase D — Persona

| メソッド | パス | 対応 MCP ツール |
|---|---|---|
| POST | `/persona/values` | `declare_value` |
| POST | `/persona/intentions` | `declare_intention` |
| POST | `/persona/commitments` | `declare_commitment` |
| GET | `/persona` | `inherit_persona` |

### Legacy / Inspection（REST 専用 or Phase A 互換）

| メソッド | パス | メモ |
|---|---|---|
| POST | `/index` | Phase A 互換。内部的には `remember` を呼ぶ |
| POST | `/query` | Phase A 互換。`/recall` のサブセット（prefetch キャッシュは bypass） |
| GET | `/node/{id}` | ノード状態の直接参照 (物理状態のみ) |
| GET | `/node/{id}/detail` | content + provenance + 物理状態 (read-only) |
| GET | `/graph` | 共起グラフ確認 |
| POST | `/reset` | 全動的状態リセット（**REST 専用**、MCP からは呼べない） |
| POST | `/admin/reset_masses` | Phase M Stage 1 maintainer-only — mass のみ既定 1.0 にリセット（**REST 専用**、MCP からは呼べない） |
| POST | `/admin/warm_displacement` | Phase M follow-up — `displacement = velocity` を 1 step 種付け（M004 直後の coverage gap を即時解消、**REST 専用**） |

---

## Memory

### POST /remember

新しい記憶を保存。

```json
{
  "content": "uv beats pip for gaottt",
  "source": "user",
  "tags": ["preference"],
  "context": "セットアップ時のユーザー指定",
  "ttl_seconds": null,
  "emotion": 0.0,
  "certainty": 1.0
}
```

**レスポンス 200**:
```json
{
  "id": "550e8400-...",
  "duplicate": false,
  "expires_at": null
}
```

`source="hypothesis"` なら TTL が自動で付与される（`default_hypothesis_ttl_seconds`）。`duplicate=true` のときは `id=null`。

### POST /recall

重力波伝播付き検索。`source_filter` で特定 source のみに絞り込み可。`force_refresh=true` で prefetch キャッシュを bypass。`tag_filter` / `persona_context` で embedding 距離によらず seed pool に強制注入（Phase J Stage 2）。

```json
{
  "query": "tidal dynamics",
  "top_k": 5,
  "source_filter": ["agent", "compaction"],
  "wave_depth": null,
  "wave_k": null,
  "force_refresh": false,
  "persona_context": null,
  "tag_filter": null,
  "auto_route": true,
  "mode": "detail",
  "passive": false
}
```

`tag_filter` は `source_filter` を bypass する（呼び出し側の明示的指定が優先）。`output_mode` は MCP 専用（REST は常に構造化 JSON を返すため不要）。`auto_route` (Phase O Stage 3) は default `true`。`mode` (Phase O Stage 4) は `"detail"` (既定、全文) / `"list"` (`config.list_mode_excerpt_chars` 字に切り詰め、改行を空白に置換 — `top_k=20` の scan + 興味ある id に対する `mode="detail"` の deep dive という 2-step に向く)。

**`passive` (Ambient Recall)**: `true` で **read-only recall** — 検索結果は同一だが末尾の simulation update を丸ごとスキップ（mass 更新・query attraction displacement・co-occurrence edge・`last_access` 更新がすべて起きない）。自動/バックグラウンド recall がノイズで重力場を汚さないための「摂動なしの観察」モード。`passive=true` の応答では `training_delta` の `displacement_changes` / `mass_changes` がすべて 0 になる。既定 `false` は legacy の「recall は訓練ステップ」挙動。詳細は [Ambient Recall](Guides-Ambient-Recall.md)。

**埋め込みは cross-lingual ではない**: 埋め込みモデル RURI v3 は日本語特化で、`/recall` は実質「`query` と同じ言語で書かれた記憶」しか引けない（英語クエリ → 英語の記憶、日本語クエリ → 日本語の記憶。両者を橋渡ししない）。`score` は言語ミスマッチでも高いまま出るため失敗が黙殺される。探したい記憶の言語で `query` を書き、言語が異なる場合は `tag_filter` / `source_filter` でターゲットを明示注入する。詳細は [Operations — Troubleshooting](Operations-Troubleshooting.md)。

**レスポンス 200**:
```json
{
  "items": [
    {
      "id": "...",
      "content": "...",
      "metadata": {...},
      "raw_score": 0.85,
      "final_score": 0.92,
      "source": "agent",
      "tags": ["concept"],
      "displacement_norm": 0.087,
      "score_breakdown": {
        "raw_cosine": 0.142,
        "virtual_cosine": 0.185,
        "decay_factor": 1.0,
        "wave_score": 0.060,
        "mass_boost": 0.245,
        "emotion_term": 0.0,
        "certainty_term": 0.0,
        "saturation": 0.910,
        "persona_proximity": 0.0,
        "bm25_contributed": false,
        "forced_inclusion": false,
        "qualified": true,
        "direct_score": 0.185,
        "field_score": 0.305,
        "lensing_gap": 0.043,
        "cohort": "3663aa4912bb",
        "provenance": "virtual",
        "in_learn_set": true
      }
    }
  ],
  "count": 5
}
```

**Phase O Stage 1 — Score breakdown**: 各 item に `score_breakdown` が attach され、`final_score` の additive な内訳が露出する。`final_score ≈ (virtual_cosine · decay_factor + wave_score + mass_boost + emotion_term + certainty_term) × saturation`。`raw_cosine` / `persona_proximity` / `bm25_contributed` / `forced_inclusion` は informational (sum に入らない)。`config.expose_score_breakdown=false` で `score_breakdown=null` 返却 (legacy 互換)。

**Phase T — breakdown 拡張 field (informational、追加のみ)**: `qualified` (bool | null — `null` は qualification flag 両方 OFF の legacy、`false` は fallback pick)、`direct_score` (pre-saturation の semantic-direct 項 = `virtual_cos_norm × decay_factor`)、`field_score` (pre-saturation の field 項 = `wave + mass + emo + cert`)、`lensing_gap` (query path では `virtual_cos_norm − raw_cos`)。**Phase T Stage 3 (direct qualification) は Phase U WP-1 で default ON 昇格** (rollback `GAOTTT_DIRECT_QUALIFICATION_ENABLED=false`) — direct 候補の presentation が qualified-first (forced → qualified → fallback) になり、fallback pick は `qualified=false` で明示される。**raw-top rescue (Phase U WP-4b)**: qualified item のうち pool 内 raw cosine rank ≤ `direct_rescue_raw_rank` (既定 3) の item は qualified group の先頭に lift。**Phase U WP-5 — selection trace field (追加のみ)**: `cohort` (structural cluster key = `cohort_id` OR `original_id`)、`provenance` (`"raw"` / `"virtual"` / `"forced"` — candidate generation の由来)、`in_learn_set` (bool | null — Stage 4 TTT learn-set membership。passive / ttt-OFF recall では `null`)。詳細: [MCP Reference — Memory](MCP-Reference-Memory.md) recall 節、[Plans — Phase U](Plans-Phase-U-Review-Hardening.md)。

**Phase O Stage 2 — Training delta**: response root に `training_delta` field が attach される (recall + explore で同じ shape)。caller が起こした state 変化 (backward pass) を JSON で受け取れる:

```json
{
  "training_delta": {
    "displacement_changes": {"abc12345...": 0.0124, "def67890...": -0.0050},
    "mass_changes": {"abc12345...": 0.0034, "def67890...": 0.0012},
    "wave_reached_count": 12,
    "wave_max_depth": 2,
    "persona_hop_reached": 3,
    "supernova_triggered": false,
    "cache_hit": false,
    "topk_only": true
  }
}
```

`topk_only=true` (default) で delta dicts は top-K 結果の node のみ。`training_delta_topk_only=false` で全 reached node を含める (debug 用)。`cache_hit=true` のとき simulation 走らず、dicts は空 (caller は「ガード hit で update 抑止された」と「触れた node が無かった」を区別できる)。`training_delta_enabled=false` で `training_delta=null` 返却。

**Phase O Stage 3 — Routing hint**: query 形式が構造化された aspect 問い合わせ (例 `"現在 active な commitment"`, `"持っている value"`, `"今やってる task"`) に match したら、対応する `reflect` aspect を並走実行して `routing_hint` に summary を attach する:

```json
{
  "routing_hint": {
    "aspect": "commitments",
    "pattern_matched": true,
    "auto_routed": true,
    "reflect_summary": "Active commitments (3 total, showing top 10):\n  id=abc12345 deadline=2026-05-31 (+17.0d) | ...\n  ..."
  }
}
```

`pattern_matched=false` (自由文 query) → 並走無し、`reflect_summary=null`。`auto_routed=false` (per-call `auto_route=false` or `auto_route_enabled=false`) でも `pattern_matched` だけは true で返り、caller は「router が off だった」と「pattern に一致しなかった」を区別できる。`auto_route` を request body に省略すると default `true`。

### POST /ambient_recall

構造化された passive (read-only) recall。1 回の passive recall から複数スロットの注入ブロックを組み立てる — [Ambient Recall](Guides-Ambient-Recall.md)（Claude Code フックが毎ターン呼ぶ）の本体。

```json
{
  "query": "ambient recall の仕組み",
  "direct_k": 2,
  "min_score": null,
  "exclude_tags": ["smoke-test"],
  "expose_breakdown": false,
  "recently_surfaced": {"abc12345": 2, "def67890": 1}
}
```

`direct_k` は直接ヒット件数（既定 2）。relevance gate は **語単位（Sudachi）BM25 の「強一致」gate**（`config.ambient_bm25_min_score`、既定 32.0）— dense cosine も char-3gram BM25 も大規模コーパスで分離できず、語単位 BM25 だけが機能した。`min_score` は **フォールバック** virtual_score gate のしきい値で、`ambient_gate_use_bm25=False` または `bm25-sudachi` extra 未導入時のみ効く（`null` → `config.ambient_min_score` 0.70）。`exclude_tags`（**Refinement Stage 2**、`null` または `[]` で no-op）は substring マッチで direct / lensing / persona 全スロットの候補から除外する — 本番フックは `GAOTTT_AMBIENT_EXCLUDE_TAGS=smoke-test,test` を default forward し、smoke test 用 memory を corpus に残しつつ ambient 注入だけ silent にする（[Plans — Ambient Recall Refinement](Plans-Ambient-Recall-Refinement.md) Stage 2）。`expose_breakdown`（**Refinement Stage 3**、default `false`）は Phase O Stage 1 の `ScoreBreakdown` を slot 粒度の `breakdown` field として JSON response に attach する — direct / lensing は recall 経由の full breakdown、persona は mass + raw cosine の minimal breakdown。`true` で response の各 slot に `breakdown` object が現れる（off では `null`）。`recently_surfaced`（**Lateral Association Stage 1**、`null` または `{}` で no-op）は `{node_id: count}` map — 各スロットの ranking score に `config.ambient_novelty_decay ** count`（既定 0.7）を乗じて recently-seen な memo を 1-2 turn でローテーションする（[Plans — Ambient Recall Lateral Association](Plans-Ambient-Recall-Lateral-Association.md) Stage 1）。本番フックは過去 N turn の `<!-- ambient-ids ... -->` manifest から自動的に組み立てて forward する。

**レスポンス 200** — `AmbientRecallResponse`:
```json
{
  "direct": [
    {
      "id": "...", "content": "...(excerpt)", "source": "agent",
      "tags": ["..."], "certainty": 0.8, "age_days": 12.3,
      "virtual_score": 0.83, "final_score": 0.41,
      "lensing_gap": null, "because": "派生元の決定の抜粋…"
    }
  ],
  "lensing": [
    {
      "id": "...", "content": "...", "source": "tweet",
      "certainty": 1.0, "age_days": 340.0,
      "virtual_score": 0.71, "lensing_gap": 0.42,
      "lensing_resonance": 0.62, "because": null
    }
  ],
  "tensions": [
    {"memory_id": "...", "memory_excerpt": "...",
     "contradicts_id": "...", "contradicts_excerpt": "..."}
  ],
  "persona": {"id": "...", "kind": "value", "content": "最も literal な解を選ぶ"},
  "count": 3
}
```

`direct` ① は `final_score` 上位、`lensing` ② は `virtual_cosine − raw_cosine` の gap 最大の **top-K リスト**（[**Lateral Association Stage 3**](Plans-Ambient-Recall-Lateral-Association.md)、cap = `config.ambient_lensing_max_k`、既定 2。場が学習した類推、各 entry の `lensing_gap` に raw gap 値、ranking は novelty 適用後の decayed gap で取り直す）、各 lensing entry には [**Stage 5**](Plans-Ambient-Recall-Lateral-Association.md) で **`lensing_resonance`** (`[0, 1)`、`raw / (raw + scale)` の saturating non-linearity、`raw = Σ_{d∈direct} cache.get_neighbors(lensing)[d]` で「場が今日の direct hits と過去に何度 co-recall したか」を測る trust 軸) も populate される。`tensions` ⑤ は `contradicts` ペア、`persona` ⑥ は active な declared value/intention。各 `direct`/`lensing` の `because` ④ は `derived_from`/`supersedes` 親の抜粋。`count == 0` は relevance gate で抑制された状態（注入なし）。MCP (`ambient_recall`) は同じ内容を `<gaottt-ambient-recall>` 文字列ブロックに整形して返す。常に passive — 重力場を摂動しない。

**Phase T Stage 5 — OR gate + gate_diagnostics (default ON)**: relevance gate は BM25「強一致」を主軸にしつつ、**BM25 reject では即空返しにならない** — passive recall を実行し `max(virtual_score) ≥ ambient_min_score` OR `max(raw_cos) ≥ config.ambient_semantic_raw_min`（既定 0.60）で accept、両軸とも下回れば空返し (off-topic 抑制は維持)。`ambient_gate_or_semantic=false` で legacy BM25 veto に rollback。レスポンスには **`gate_diagnostics`** が空・非空に関わらず常に attach される:

```json
{
  "gate_diagnostics": {
    "candidates_generated": 12,
    "after_tag_exclusion": 10,
    "after_dump_filter": 10,
    "semantic_qualified": 4,
    "direct_selected": 2,
    "lensing_selected": 1,
    "bm25_top_score": 38.2,
    "bm25_gate": true,
    "semantic_max_virtual": 0.812,
    "semantic_max_raw": 0.774,
    "virt_top1": null,
    "bm25_top": null,
    "composite_signal": null,
    "empty_reason": null
  },
  "expose_breakdown": false
}
```

`empty_reason` は離散一意: `bm25_veto` (legacy flag OFF で BM25 reject 即空) / `bm25_and_semantic_below_threshold` / `no_candidates` / `all_tag_excluded` / `all_dump_filtered`、および **`ambient_gate_mode="composite"` (Phase U WP-3) のときのみ** `composite_reject` (3-arm すべて未達) / `composite_pool_too_small` (pool < 2) / `composite_reference_unavailable` (参照 artifact 欠損・破損・fingerprint 不一致・count drift 超過 — **fail-closed**、BM25 のみ accept 経路)。`bm25_gate`/`bm25_top_score` は gate index 利用不可時 `null`、`semantic_max_raw` は breakdown が populate されていない場合 `null` (raw cosine 軸はその場合判定から外れる)。**composite 軸 (Phase U WP-3 §10、`ambient_gate_mode="composite"` で判定した場合のみ populate、それ以外は `null`)**: `virt_top1` (pool top-1 virtual cosine) / `bm25_top` (word-BM25 top score) / `composite_signal` (`bm25_strong` / `virt_hi` / `bm25_virt_mid` = accept した arm、拒否理由は `empty_reason` と同値)。composite mode は実装済みだが default は `"or"` (v2/v3 両較正で昇格 gate 不達 — R3 は既知制約として close)。triage 表は [Operations — Troubleshooting](Operations-Troubleshooting.md)。

> **★ Breaking change (2026-05-25, Stage 3)**: `lensing` field は `AmbientMemory | null` → `list[AmbientMemory]` に変更されました。旧 client は `data["lensing"]` を `None`/object として読んでいた箇所を `data["lensing"]` が常に list (空の場合 `[]`) であることに合わせて update してください。Stage 3 以前のロジックを保持したい場合は `config.ambient_lensing_max_k=1` で「1 picks 上限」になり、`data["lensing"][0] if data["lensing"] else None` が旧 shape 等価。

### POST /explore

発散的探索。`diversity` ∈ [0.0, 1.0] で gamma と wave depth/k をブースト。

```json
{"query": "connections between themes", "diversity": 0.7, "top_k": 10, "auto_route": true, "mode": "serendipity"}
```

**レスポンス 200**: `items`（recall と同じ shape。Phase T breakdown 拡張 field + Phase U selection trace field (`cohort`/`provenance`/`in_learn_set`) も parity）+ `diversity` + `wave_depth` / `wave_reached` (**Phase U WP-5** — wave 伝播の実効 depth と到達 node 数。`training_delta.wave_max_depth` / `wave_reached_count` と同値を response root でも露出。dormant mode では `null`) + `training_delta` + `routing_hint` (Phase O Stage 2 / 3 — recall と parity)。

**Phase T Stage 6 — diversified presentation (**Phase U WP-5 で default ON 昇格**・rollback `GAOTTT_EXPLORE_DIVERSIFIED_PRESENTATION_ENABLED=false`)**: flag ON かつ `diversity > 0` で engine 層の MMR selection (pool 拡大 `top_k × explore_diversity_pool_multiplier` / `explore_min_semantic` floor / cohort penalty) が有効になる。`diversity=0.0` は bit-for-bit legacy。詳細: [MCP Reference — Memory](MCP-Reference-Memory.md) explore 節。

**Dormant mode (Phase O Stage 5):** `mode: "dormant"` で wave を bypass し counter-importance sampling。**自己発信 source class** (`agent` / `value` / `intention` / `commitment` / `note` / `reference`) のうち `last_access` が `dormant_age_threshold_seconds` (既定 30 日) より古く、かつ `mass ≤ dormant_mass_threshold` (既定 2.0) を満たすノードからランダムに `top_k` 件返す。`query` は ignore、`training_delta` / `routing_hint` は常に `null` (simulation 走らず、aspect intent も検出しない)。

```json
{"query": "_ignored", "top_k": 5, "mode": "dormant"}
```

レスポンス 200: `items[]` (空も可)、`count`、`diversity` (request value をそのまま返却、informational)、`training_delta=null`、`routing_hint=null`。

### POST /forget

ソフト archive（既定）または hard delete（`hard=true`）。

```json
{"node_ids": ["id1", "id2"], "hard": false}
```

**レスポンス 200**:
```json
{"affected": 2, "requested": 2, "hard": false}
```

### POST /restore

ソフト archive されたノードを復活。hard delete されたものは対象外。

```json
{"node_ids": ["id1"]}
```

### POST /revalidate

certainty timestamp を更新し、オプションで certainty/emotion を更新。

```json
{"node_id": "id", "certainty": 0.95, "emotion": 0.3}
```

**レスポンス 200**: `{found, id, certainty, emotion_weight}`
**レスポンス 404**: ノードが存在しない or archived

### POST /auto_remember

会話ログから保存候補を抽出（保存はしない）。

```json
{"transcript": "...", "max_candidates": 5, "include_reasons": true}
```

**レスポンス 200**: `{candidates: [{content, score, suggested_source, suggested_tags, reasons}], count}`

### POST /save_candidates

Stop / turn-end hook 用 — `auto_remember` を block formatter でラップ ([Plans](Plans-Save-Candidates-Hook.md))。

```json
{"transcript": "...", "max_candidates": 3, "include_reasons": true, "include_persona": true}
```

**レスポンス 200**: `{candidates: [...], persona: {id, kind, content} | null, count}` — `count == 0` のとき formatter は sentinel `(保存候補なし)` を返し、hook は無音化する。

---

## Relations

### POST /relations

有向タイプ付きエッジを作成。予約 edge_type: `supersedes`, `derived_from`, `contradicts`（詳細は [MCP — Memory Tools](MCP-Reference-Memory.md)）。

```json
{"src_id": "new", "dst_id": "old", "edge_type": "supersedes", "weight": 1.0, "metadata": {"reason": "user feedback"}}
```

**エラー 400**: src==dst の self-relation は禁止。

### DELETE /relations?src_id=...&dst_id=...[&edge_type=...]

エッジを削除。`edge_type` を省略するとペア間の全エッジを削除。

### GET /relations/{node_id}?direction=out|in|both[&edge_type=...]

指定ノードに接続する有向エッジの一覧。

---

## Maintenance

### POST /merge

2 つ以上の記憶を重力衝突で 1 ノードに統合。

```json
{"node_ids": ["id1", "id2"], "keep": null}
```

`keep=null` の場合は最重量が生存（タイ時は最新アクセス）。

### POST /compact

TTL 失効 + FAISS 再構築 + オプションで duplicate auto-merge。

```json
{"expire_ttl": true, "rebuild_faiss": true, "auto_merge": false, "merge_threshold": 0.95, "merge_top_n": 500}
```

### POST /prefetch

クエリ周りの記憶をバックグラウンドで pre-warm。続く `/recall` がキャッシュヒットで即返る。

### GET /prefetch/status

prefetch キャッシュ/pool の統計。

---

## Reflection

全 15 aspect に別エンドポイント。各 aspect は専用 Pydantic レスポンスを返す（Swagger で完全な型が見える）。`limit` など aspect 固有のパラメータは **クエリパラメータ** で渡す。

**例**: `POST /reflect/hot_topics?limit=20`

**POST /reflect/summary**（パラメータなし）レスポンス:
```json
{
  "total_memories": 1250,
  "active_memories": 340,
  "displaced_nodes": 89,
  "total_edges": 567,
  "sources": {"agent": 800, "user": 200, "hypothesis": 50}
}
```

**POST /reflect/duplicates?limit=5&threshold=0.95** レスポンス:
```json
{
  "clusters": [
    {
      "ids": ["id1", "id2"],
      "avg_pairwise_similarity": 0.97,
      "members": [
        {"id": "id1", "mass": 2.1, "content_preview": "..."},
        {"id": "id2", "mass": 1.8, "content_preview": "..."}
      ]
    }
  ],
  "threshold": 0.95
}
```

各 aspect の詳細なフィールドは Swagger UI (`/docs`) で確認可。

---

## Ingest

### POST /ingest

ディレクトリ or ファイルを一括読み込み。MCP `ingest` ツールと同じ service を経由する（[Operations — Ingestion](Operations-Ingestion.md)）。

```json
{
  "path": "/path/to/dir",
  "source": "notes",
  "recursive": true,
  "pattern": "*.md,*.txt",
  "chunk_size": 2000,
  "include_tool_results": false
}
```

対応形式: `.md` / `.txt` / `.csv` / `.jsonl`（Claude Code チャット履歴）。`include_tool_results` は `.jsonl` のみ意味を持ち、`true` で `tool_result` の生 stdout を exchange 本文に追記する（既定 `false`）。チャット履歴を流すときは `pattern="*.jsonl"`, `source="claude-code"` が定番。

**レスポンス 200**: `{path, ingested, skipped, found}`

---

## Phase D — Tasks

### POST /tasks

新規タスク。`parent_id` は commitment/intention の ID（あれば `fulfills` edge が張られる）。

```json
{"content": "fix the FAISS leak", "parent_id": null, "deadline_seconds": 604800, "certainty": 1.0}
```

**レスポンス**: `{id, duplicate, expires_at, parent_id, edge_error}`

### POST /tasks/{id}/start

タスクの TTL を refresh、emotion を +0.4 に。404 when unknown/archived。

### POST /tasks/{id}/complete

```json
{"outcome": "patched engine.py", "emotion": 0.7}
```

`task_id` は path から渡す（body には含めない）。outcome を記憶し、`completed` edge を outcome → task に張り、task を archive。

### POST /tasks/{id}/abandon

```json
{"reason": "priority shifted"}
```

`task_id` は path から渡す。reason を記憶し、`abandoned` edge を reason → task に張り、task を archive。

### POST /tasks/{id}/depend

```json
{"depends_on_id": "other-task", "blocking": false}
```

`task_id` は path から渡す。`blocking=true` で `blocked_by` edge（強い依存）、false で `depends_on`。

---

## Phase D — Persona

### POST /persona/values

永続的な value を宣言。

```json
{"content": "curiosity is load-bearing", "certainty": 1.0}
```

### POST /persona/intentions

永続的な intention を宣言（オプションで value から derive）。

```json
{"content": "teach by building", "parent_value_id": "optional", "certainty": 1.0}
```

### POST /persona/commitments

期限付きの commitment（必ず intention から derive）。

```json
{"content": "ship S5 by next week", "parent_intention_id": "required", "deadline_seconds": 604800, "certainty": 1.0}
```

### GET /persona

統合された persona snapshot（`reflect/persona` と同じ）。

```json
{
  "values": [{"id": "...", "content": "..."}],
  "intentions": [...],
  "commitments": [{"id": "...", "content": "...", "deadline": "2026-04-30T12:00:00"}],
  "styles": [...],
  "relationships": [{"id": "...", "who": "Mei", "content": "..."}]
}
```

---

## Legacy — Phase A 互換

### POST /index

Phase A の古いエンドポイント。`/remember` を複数件バッチ相当で呼び出す。新規クライアントは `/remember` 推奨。

```json
{"documents": [{"content": "...", "metadata": {...}}]}
```

### POST /query

Phase A の古いエンドポイント。`/recall` のサブセット（source_filter 不可、prefetch キャッシュは bypass）。

```json
{"text": "...", "top_k": 10, "wave_depth": null, "wave_k": null}
```

レスポンスは旧 schema（`results[]` に `id/content/metadata/raw_score/final_score` のみ、`source/tags/displacement_norm` なし）。

### GET /node/{id}

単一ノードの動的状態。

### GET /node/{id}/detail

単一ノードの content + provenance (source, tags, certainty, emotion) + 物理状態 (mass, temperature, displacement)。Observation Apparatus Round 2 Stage A — read-only (重力場不変)。`GetNodeResponse` を返す。archived / 不存在は 404。

### GET /graph

共起グラフ全体のエッジ。

### POST /reset

全動的状態を初期値に戻す（**破壊的操作**）。MCP からは呼べない REST 専用。

```json
{"reset": true, "nodes_reset": 1500, "edges_removed": 42}
```

**リセット対象**: mass, temperature, sim_history, last_access, displacement, velocity, expires_at, is_archived, merge*, emotion_weight, certainty, last_verified_at, 共起グラフ全エッジ, directed_edges 全件

**保持**: ドキュメント本文, metadata, embedding, FAISS インデックス

### POST /admin/reset_masses

Phase M Stage 1 maintainer-only — **mass のみ** を `value`（既定 1.0）にリセット。displacement / velocity / edges / cohort_id / source など他の動的状態は触らない。MCP 非露出（LLM 用途なし）。

**リクエスト**: `{"value": 1.0}`（省略時 1.0）

**レスポンス**: `{"nodes_reset": 23012, "value": 1.0}`

**用途**: Mass Conservation 規則 (`mass_conservation_enabled=True`) を本番 DB にロールアウトする時の一回限り操作。旧規則下で蓄積した chunk 内輪取引 inflation を一度ゼロにしてから新規則で観察する。**走らせる前に他 MCP / REST プロセスを停止し DB backup を取ること**（write-behind 上書き罠）。詳細: [Plans — Phase M](Plans-Phase-M-Mass-Conservation.md) §11.2 / CLI: `scripts/reset_masses.py --apply`。

### POST /admin/warm_displacement

Phase M follow-up — `displacement = velocity` を 1 orbital timestep ぶん一括 seed。M004 (cosmic-bang) は velocity を全 active node に書くが displacement は NULL のまま残し、dream loop が ~20h かけて埋める設計だった。サーバー再起動を挟むと埋まりきらず「velocity arrow はあるのに位置が動かない」状態が続くため、それを一発で解消するためのエンドポイント。MCP 非露出。

**リクエスト**: `{"overwrite": false}`（既定）。`overwrite=true` で displacement が既に非ゼロのノードも上書き。

**レスポンス**:
```json
{
  "seeded": 14865,
  "skipped_no_velocity": 0,
  "skipped_already_displaced": 9186,
  "active_total": 24051
}
```

**用途**: M004 直後 (または M005 の代替として live server に対して実行する場合)。既定 `overwrite=false` は冪等で、追加の dream tick 蓄積を壊さない。`overwrite=true` は M002/M004 直後の cold cosmos を完全 reseed したい時のみ使う（destructive）。同等の効果を migration ledger に記録したい場合は `scripts/migrate.py --apply --step M005`。

### GET /admin/readiness — FastMCP HTTP app 専用 (parity 例外、本 app には無し)

**Phase U WP-6b** の staged readiness endpoint (`STARTING` → `SEMANTIC_READY` → `HYBRID_READY` / `FAILED` + `timings` + `bm25_size` + `node_count`) は **MCP HTTP backend (`gaottt.server.mcp_server`、streamable-http / SSE) の FastMCP app にのみ存在**し、本 REST app (`server/app.py`) には実装されない。supervisor の `/route` がこの endpoint を poll する (Bearer `GAOTTT_BACKEND_TOKEN` 認証)。**MCP/REST parity 鉄則の文書化された例外** — `/reset` と同じ扱い (管理面・backend lifecycle の確認は LLM の engine 能力ではなく運用面。詳細は [Architecture — Overview](Architecture-Overview.md) 設計判断表)。triage は [Operations — Troubleshooting](Operations-Troubleshooting.md)「backend の readiness が STARTING / FAILED に張り付く」。

---

## MCP との関係（Phase S 以降）

REST と MCP は同じ `gaottt/services/` レイヤを叩く **2 つのトランスポート** です。

- **MCP** 各ツール = `await service(...)` → `formatter(...)` で LLM 向け文字列を返す
- **REST** 各エンドポイント = `await service(...)` → Pydantic JSON をそのまま返す

同じ操作なので、バグ修正は一箇所で両方に効きます。差異は:
- MCP は LLM 向け整形済み文字列、REST は構造化 JSON
- `/reset` は REST 専用（LLM に破壊的操作を露出しない設計判断）
- `/admin/readiness` は FastMCP HTTP app 専用（Phase U WP-6b — 上記、parity 例外）
- `/index` `/query` は REST 専用の Phase A 互換

→ サービス層の設計: [`docs/maintainers/rest-mcp-unification-plan.md`](https://github.com/May-Kirihara/GaOTTT/blob/main/docs/maintainers/rest-mcp-unification-plan.md)
→ MCP ツール一覧: [MCP Reference Index](MCP-Reference-Index.md)
→ サーバー起動方法: [Operations — Server Setup](Operations-Server-Setup.md)
