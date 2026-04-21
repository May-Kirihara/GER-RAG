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
| POST | `/explore` | `explore` |
| POST | `/forget` | `forget` |
| POST | `/restore` | `restore` |
| POST | `/revalidate` | `revalidate` |
| POST | `/auto_remember` | `auto_remember` |

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
| GET | `/node/{id}` | ノード状態の直接参照 |
| GET | `/graph` | 共起グラフ確認 |
| POST | `/reset` | 全動的状態リセット（**REST 専用**、MCP からは呼べない） |

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

重力波伝播付き検索。`source_filter` で特定 source のみに絞り込み可。`force_refresh=true` で prefetch キャッシュを bypass。

```json
{
  "query": "tidal dynamics",
  "top_k": 5,
  "source_filter": ["agent", "compaction"],
  "wave_depth": null,
  "wave_k": null,
  "force_refresh": false
}
```

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
      "displacement_norm": 0.087
    }
  ],
  "count": 5
}
```

### POST /explore

発散的探索。`diversity` ∈ [0.0, 1.0] で gamma と wave depth/k をブースト。

```json
{"query": "connections between themes", "diversity": 0.7, "top_k": 10}
```

**レスポンス 200**: `items`（recall と同じ shape）+ `diversity`。

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

ディレクトリ or ファイルを一括読み込み。

```json
{"path": "/path/to/dir", "source": "notes", "recursive": true, "pattern": "*.md,*.txt", "chunk_size": 2000}
```

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
{"task_id": "same-as-path", "outcome": "patched engine.py", "emotion": 0.7}
```

outcome を記憶し、`completed` edge を outcome → task に張り、task を archive。`task_id` が path と body で一致しない場合 400。

### POST /tasks/{id}/abandon

```json
{"task_id": "same-as-path", "reason": "priority shifted"}
```

reason を記憶し、`abandoned` edge を reason → task に張り、task を archive。

### POST /tasks/{id}/depend

```json
{"task_id": "same-as-path", "depends_on_id": "other-task", "blocking": false}
```

`blocking=true` で `blocked_by` edge（強い依存）、false で `depends_on`。

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

### GET /graph

共起グラフ全体のエッジ。

### POST /reset

全動的状態を初期値に戻す（**破壊的操作**）。MCP からは呼べない REST 専用。

```json
{"reset": true, "nodes_reset": 1500, "edges_removed": 42}
```

**リセット対象**: mass, temperature, sim_history, last_access, displacement, velocity, expires_at, is_archived, merge*, emotion_weight, certainty, last_verified_at, 共起グラフ全エッジ, directed_edges 全件

**保持**: ドキュメント本文, metadata, embedding, FAISS インデックス

---

## MCP との関係（Phase S 以降）

REST と MCP は同じ `gaottt/services/` レイヤを叩く **2 つのトランスポート** です。

- **MCP** 各ツール = `await service(...)` → `formatter(...)` で LLM 向け文字列を返す
- **REST** 各エンドポイント = `await service(...)` → Pydantic JSON をそのまま返す

同じ操作なので、バグ修正は一箇所で両方に効きます。差異は:
- MCP は LLM 向け整形済み文字列、REST は構造化 JSON
- `/reset` は REST 専用（LLM に破壊的操作を露出しない設計判断）
- `/index` `/query` は REST 専用の Phase A 互換

→ サービス層の設計: [`docs/maintainers/rest-mcp-unification-plan.md`](https://github.com/May-Kirihara/GaOTTT/blob/main/docs/maintainers/rest-mcp-unification-plan.md)
→ MCP ツール一覧: [MCP Reference Index](MCP-Reference-Index.md)
→ サーバー起動方法: [Operations — Server Setup](Operations-Server-Setup.md)
