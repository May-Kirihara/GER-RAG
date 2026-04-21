# MCP Tool Reference — Index

GaOTTT（旧 GER-RAG）が公開する **25 個の MCP ツール** の早見表。MCP サーバー名は `gaottt`（旧 `ger-rag-memory`）。詳細は各カテゴリページに。

## 早見表

| ツール | 機能 | カテゴリ |
|---|---|---|
| `remember` | 知識を保存 | [Memory](MCP-Reference-Memory.md) |
| `recall` | 重力波伝播による検索 | [Memory](MCP-Reference-Memory.md) |
| `explore` | 高温の創発的探索 | [Memory](MCP-Reference-Memory.md) |
| `reflect` | 状態分析（11 aspects） | [Memory](MCP-Reference-Memory.md) |
| `ingest` | ファイル一括取り込み | [Memory](MCP-Reference-Memory.md) |
| `auto_remember` | transcript から保存候補を抽出 | [Memory](MCP-Reference-Memory.md) |
| `forget` | ソフトアーカイブ / 物理削除 | [Maintenance](MCP-Reference-Maintenance.md) |
| `restore` | アーカイブからの復元 | [Maintenance](MCP-Reference-Maintenance.md) |
| `merge` | 重力衝突合体 | [Maintenance](MCP-Reference-Maintenance.md) |
| `compact` | 定期メンテ (TTL/FAISS rebuild/auto-merge) | [Maintenance](MCP-Reference-Maintenance.md) |
| `revalidate` | 確信度の再検証 | [Maintenance](MCP-Reference-Maintenance.md) |
| `relate` | 有向リレーション作成 | [Maintenance](MCP-Reference-Maintenance.md) |
| `unrelate` | リレーション削除 | [Maintenance](MCP-Reference-Maintenance.md) |
| `get_relations` | リレーション一覧 | [Maintenance](MCP-Reference-Maintenance.md) |
| `prefetch` | バックグラウンド recall 予熱 | [Maintenance](MCP-Reference-Maintenance.md) |
| `prefetch_status` | キャッシュ状態 | [Maintenance](MCP-Reference-Maintenance.md) |
| **`commit`** | タスク作成 | [Tasks & Persona](MCP-Reference-Tasks-and-Persona.md) |
| **`start`** | タスクの active engagement | [Tasks & Persona](MCP-Reference-Tasks-and-Persona.md) |
| **`complete`** | タスク完了 + outcome | [Tasks & Persona](MCP-Reference-Tasks-and-Persona.md) |
| **`abandon`** | タスクを意図的に放棄 | [Tasks & Persona](MCP-Reference-Tasks-and-Persona.md) |
| **`depend`** | タスク依存関係 | [Tasks & Persona](MCP-Reference-Tasks-and-Persona.md) |
| **`declare_value`** | 価値観の宣言 | [Tasks & Persona](MCP-Reference-Tasks-and-Persona.md) |
| **`declare_intention`** | 長期意図の宣言 | [Tasks & Persona](MCP-Reference-Tasks-and-Persona.md) |
| **`declare_commitment`** | コミットメント宣言 | [Tasks & Persona](MCP-Reference-Tasks-and-Persona.md) |
| **`inherit_persona`** | 過去の自分を継承 | [Tasks & Persona](MCP-Reference-Tasks-and-Persona.md) |

## ツール選択フロー

```
何かを保存したい
├── 知識・気づき          → remember
├── タスク                → commit
├── 価値観                → declare_value
├── 長期方向              → declare_intention
├── 期限付き約束          → declare_commitment
└── transcript から抽出   → auto_remember (確認後 remember)

何かを思い出したい
├── 関連記憶              → recall
├── 意外な繋がり          → explore (diversity 高)
├── 過去の自分の人格      → inherit_persona
└── 状態を見る            → reflect (11 aspects)

何かを整理したい
├── 完了                  → complete
├── 放棄                  → abandon
├── アーカイブ            → forget
├── 復元                  → restore
├── 重複統合              → merge
├── 関係付け              → relate
└── 定期メンテ            → compact
```

## reflect aspect 一覧

| aspect | 内容 |
|---|---|
| `summary` | 全体統計 |
| `hot_topics` | 高質量ノード |
| `connections` | 強い共起エッジ |
| `dormant` | 長期間未アクセス |
| `duplicates` | 近接重複クラスタ |
| `relations` | 有向リレーション |
| `tasks_todo` | 進行中タスク（締切順） |
| `tasks_doing` | 直近 1 時間に start されたタスク |
| `tasks_completed` | 完了タスクの年表 |
| `tasks_abandoned` | 放棄タスク（影の年表） |
| `commitments` | アクティブな commitment |
| `values` | 宣言済み values |
| `intentions` | 宣言済み intentions |
| `relationships` | relationship:* ソース、人物別グループ |
| `persona` | 上記の合成（= `inherit_persona` と同じ） |

## ツールの段階別習得

### 初級（最初の 5 ツール）
`remember` → `recall` → `reflect(aspect="summary")` → `forget` → `restore`

### 中級（さらに 5 ツール）
`explore` → `auto_remember` → `relate` → `revalidate` → `inherit_persona`

### 上級（タスク管理）
`commit` → `start` → `complete` → `declare_*` × 3 → `compact`

### 玄人（細部）
`merge`, `prefetch`/`prefetch_status`, `depend`, `unrelate`, `get_relations`, `abandon`

詳細は各カテゴリページへ:
- 🧠 [Memory tools](MCP-Reference-Memory.md)
- 🎯 [Tasks & Persona tools](MCP-Reference-Tasks-and-Persona.md)
- 🛠 [Maintenance tools](MCP-Reference-Maintenance.md)
