# APIリファレンス

ベースURL: `http://localhost:8000`
認証: なし (Phase 1)

サーバー起動後、Swagger UIが `http://localhost:8000/docs` で利用可能。

---

## POST /index

ドキュメントを登録する。同一contentの重複登録は自動スキップされる。

### リクエスト

```json
{
  "documents": [
    {
      "content": "ドキュメントのテキスト内容",
      "metadata": {"source": "example", "category": "tech"}
    }
  ]
}
```

| フィールド | 型 | 必須 | 説明 |
|-----------|------|------|------|
| documents | array | Yes | 1件以上 |
| documents[].content | string | Yes | 空文字不可 |
| documents[].metadata | object | No | 任意のJSON |

### レスポンス 200

```json
{
  "indexed": [{"id": "550e8400-..."}],
  "count": 1,
  "skipped": 0
}
```

| フィールド | 説明 |
|-----------|------|
| indexed | 新規登録されたドキュメントのID一覧 |
| count | 新規登録数 |
| skipped | 重複でスキップされた数 |

### エラー

- `422`: バリデーションエラー（空content、空配列）

---

## POST /query

ドキュメントを動的スコアリングで検索する。クエリ実行後、ヒットしたノードの状態が更新される。

### リクエスト

```json
{
  "text": "検索クエリ",
  "top_k": 10
}
```

| フィールド | 型 | 必須 | デフォルト | 範囲 |
|-----------|------|------|-----------|------|
| text | string | Yes | - | 空文字不可 |
| top_k | int | No | 10 | 1-100 |

### レスポンス 200

```json
{
  "results": [
    {
      "id": "550e8400-...",
      "content": "ドキュメント本文",
      "metadata": {"source": "example"},
      "raw_score": 0.85,
      "final_score": 0.92
    }
  ],
  "count": 5
}
```

| フィールド | 説明 |
|-----------|------|
| raw_score | コサイン類似度（embedding空間） |
| final_score | 動的スコアリング後の最終スコア（> 0のみ） |

結果は `final_score` 降順。負スコアのドキュメントは除外される。

---

## GET /node/{node_id}

ノードの動的状態を確認する（デバッグ・チューニング用）。

### パスパラメータ

| パラメータ | 型 | 説明 |
|-----------|------|------|
| node_id | string | ドキュメントUUID |

### レスポンス 200

```json
{
  "id": "550e8400-...",
  "mass": 3.42,
  "temperature": 0.15,
  "last_access": 1711526400.0,
  "sim_history": [0.85, 0.72, 0.91]
}
```

| フィールド | 説明 |
|-----------|------|
| mass | 重要度（検索されるほど増加、logistic飽和あり） |
| temperature | 文脈変動性（sim_historyの分散 * gamma） |
| last_access | 最終アクセスのUnixタイムスタンプ |
| sim_history | 直近の類似度スコア（リングバッファ、最大20件） |

### エラー

- `404`: ノードが存在しない

---

## GET /graph

共起グラフのエッジを確認する。

### クエリパラメータ

| パラメータ | 型 | デフォルト | 説明 |
|-----------|------|-----------|------|
| min_weight | float | 0.0 | この値未満のエッジを除外 |
| node_id | string | null | 指定ノードに接続するエッジのみ |

### レスポンス 200

```json
{
  "edges": [
    {
      "src": "550e8400-...",
      "dst": "661f9511-...",
      "weight": 7.5,
      "last_update": 1711526400.0
    }
  ],
  "count": 42
}
```

---

## POST /reset

全動的状態を初期値にリセットする。ドキュメントとembeddingは保持される。

### リクエスト

ボディなし。

### レスポンス 200

```json
{
  "reset": true,
  "nodes_reset": 1500,
  "edges_removed": 42
}
```

リセット対象: mass, temperature, sim_history, last_access, 共起グラフ全エッジ。
保持されるもの: ドキュメント本文, metadata, embedding, FAISSインデックス。
