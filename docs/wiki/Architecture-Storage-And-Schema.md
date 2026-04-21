# Architecture — Storage & Schema

ストレージ層の詳細とテーブル定義。一次ソースは [`gaottt/store/sqlite_store.py`](../../gaottt/store/sqlite_store.py)。

## 全体構成

```
[in-memory cache (CacheLayer)]
   ↑ load_from_store on startup
   ↓ write-behind every 5s
[SQLite (WAL)]
   + [FAISS index file]
```

- **CacheLayer**: 高速 read、dirty フラグで書き込み追跡
- **SqliteStore**: 永続化、自動マイグレーション、削除/アーカイブ API
- **FaissIndex**: ベクトル近傍探索、`get_vectors` で逆引き対応

## SQLite スキーマ

### documents

| 列 | 型 | 用途 |
|---|---|---|
| id | TEXT PRIMARY KEY | UUID |
| content | TEXT NOT NULL | 元テキスト |
| content_hash | TEXT NOT NULL | SHA-256（重複検出） |
| metadata | TEXT (JSON) | source, tags, context, etc. |

### nodes

| 列 | 型 | 追加 Phase | 用途 |
|---|---|---|---|
| id | TEXT PRIMARY KEY | 初期 | documents.id と同一 |
| mass | REAL DEFAULT 1.0 | 初期 | 重力質量 |
| temperature | REAL DEFAULT 0.0 | 初期 | sim_history の分散 |
| last_access | REAL | 初期 | 最終アクセス時刻 |
| sim_history | BLOB (msgpack) | 初期 | リング buffer |
| displacement | BLOB | 初期 | 重力変位ベクトル |
| velocity | BLOB | 初期 | 速度ベクトル（軌道力学） |
| return_count | REAL DEFAULT 0.0 | 初期 | 馴化用 |
| expires_at | REAL | F4 | TTL（NULL なら永続） |
| is_archived | INTEGER DEFAULT 0 | F5 | ソフトアーカイブ |
| merged_into | TEXT | F2.1 | 合体先のサバイバー ID |
| merge_count | INTEGER DEFAULT 0 | F2.1 | 合体に参加した回数 |
| merged_at | REAL | F2.1 | 合体時刻 |
| emotion_weight | REAL DEFAULT 0.0 | F7 | 情動 [-1.0, 1.0] |
| certainty | REAL DEFAULT 1.0 | F7 | 確信度 |
| last_verified_at | REAL | F7 | 確信度の最終リセット時刻 |

### edges (共起、無向)

| 列 | 型 | 用途 |
|---|---|---|
| src | TEXT | 端点 1 |
| dst | TEXT | 端点 2 |
| weight | REAL DEFAULT 0.0 | 共起回数の累積 |
| last_update | REAL | 最終更新時刻 |
| PRIMARY KEY | (src, dst) | |

### directed_edges (有向、typed) — F3

| 列 | 型 | 用途 |
|---|---|---|
| src | TEXT NOT NULL | 始点 |
| dst | TEXT NOT NULL | 終点 |
| edge_type | TEXT NOT NULL | supersedes/derived_from/contradicts/completed/abandoned/depends_on/blocked_by/working_on/fulfills |
| weight | REAL DEFAULT 1.0 | リレーションの強さ |
| created_at | REAL | 作成時刻（時系列クエリ用） |
| metadata | TEXT (JSON) | 任意 |
| PRIMARY KEY | (src, dst, edge_type) | 同一ペアに複数 type 可 |

## インデックス

```sql
CREATE INDEX idx_documents_content_hash ON documents(content_hash);
CREATE INDEX idx_edges_src/dst;
CREATE INDEX idx_directed_src/dst/type/created;
CREATE INDEX idx_nodes_archived/expires_at/merged_into;
```

## 自動マイグレーション

`SqliteStore.initialize()` が起動時に:
1. テーブル定義を `CREATE TABLE IF NOT EXISTS`
2. 旧 DB に欠けている列を `ALTER TABLE ... ADD COLUMN`（DEFAULT 値付き）
3. 必要なインデックスを `CREATE INDEX IF NOT EXISTS`

→ 新規列追加は **必ず DEFAULT を設定** する規約。後方互換を担保。

## FAISS index

- `IndexFlatIP`（exact search、内積、L2 正規化済み embedding 前提）
- `_id_map`（list[str]）で row → node_id を保持
- `.faiss` バイナリ + `.faiss.ids` テキストで永続化
- 起動時にロード、shutdown 時に保存

→ orphan ベクトル問題（`compact()` での rebuild）は [Concurrency](Architecture-Concurrency.md) と [Operations — Compact & Backup](Operations-Compact-And-Backup.md)

## In-memory cache

`CacheLayer` (`store/cache.py`):
- `node_cache: dict[str, NodeState]` — 非 archived のみ
- `displacement_cache`, `velocity_cache: dict[str, np.ndarray]`
- `graph_cache: dict[str, dict[str, float]]` — 共起の隣接リスト
- `dirty_*: set[str]` — 書き込み待ち
- 5 秒ごと（既定）に write-behind タスクが flush

→ 一次ソース: [`gaottt/store/sqlite_store.py`](../../gaottt/store/sqlite_store.py), [`gaottt/store/cache.py`](../../gaottt/store/cache.py)
