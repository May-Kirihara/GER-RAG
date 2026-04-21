# REST API Reference

GaOTTT の HTTP API（FastAPI）の完全リファレンス。

ベース URL: `http://localhost:8000` （[Operations — Server Setup](Operations-Server-Setup.md)）
認証: なし（Phase 1）
Swagger UI: http://localhost:8000/docs

## エンドポイント一覧

| メソッド | パス | 内容 |
|---|---|---|
| POST | `/index` | ドキュメント登録（SHA-256 重複自動スキップ） |
| POST | `/query` | 重力変位付き検索（二段階: FAISS 候補 → 仮想座標再計算） |
| GET | `/node/{id}` | ノード状態確認 |
| GET | `/graph` | 共起グラフ確認 |
| POST | `/reset` | 動的状態リセット |

---

## POST /index

ドキュメントを登録。同一 content の重複登録は SHA-256 で自動スキップ。

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
|---|---|---|---|
| documents | array | Yes | 1 件以上 |
| documents[].content | string | Yes | 空文字不可 |
| documents[].metadata | object | No | 任意の JSON |

### レスポンス 200

```json
{
  "indexed": [{"id": "550e8400-..."}],
  "count": 1,
  "skipped": 0
}
```

| フィールド | 説明 |
|---|---|
| indexed | 新規登録されたドキュメントの ID 一覧 |
| count | 新規登録数 |
| skipped | 重複でスキップされた数 |

### エラー

- `422` バリデーションエラー（空 content、空配列）

---

## POST /query

ドキュメントを動的スコアリングで検索する。クエリ実行後、ヒットしたノードの状態が更新される（mass, displacement, return_count 等）。

### リクエスト

```json
{
  "text": "検索クエリ",
  "top_k": 10
}
```

| フィールド | 型 | 必須 | デフォルト | 範囲 |
|---|---|---|---|---|
| text | string | Yes | - | 空文字不可 |
| top_k | int | No | 10 | 1-100 |
| wave_depth | int | No | config | 0-5（再帰深度オーバーライド） |
| wave_k | int | No | config | 1-20（初期 seed 数オーバーライド） |

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
|---|---|
| raw_score | コサイン類似度（embedding 空間） |
| final_score | 動的スコアリング後の最終スコア（> 0 のみ） |

結果は `final_score` 降順。負スコアのドキュメントは除外される。

→ スコア式の詳細: [Architecture — Gravity Model](Architecture-Gravity-Model.md)

---

## GET /node/{node_id}

ノードの動的状態を確認する（デバッグ・チューニング用）。

### パスパラメータ

| パラメータ | 型 | 説明 |
|---|---|---|
| node_id | string | ドキュメント UUID |

### レスポンス 200

```json
{
  "id": "550e8400-...",
  "mass": 3.42,
  "temperature": 0.15,
  "last_access": 1711526400.0,
  "sim_history": [0.85, 0.72, 0.91],
  "displacement_norm": 0.087
}
```

| フィールド | 説明 |
|---|---|
| mass | 重要度（検索されるほど増加、logistic 飽和あり） |
| temperature | 文脈変動性（sim_history の分散 × gamma） |
| last_access | 最終アクセスの Unix タイムスタンプ |
| sim_history | 直近の類似度スコア（リングバッファ、最大 20 件） |
| displacement_norm | 重力変位ベクトルの L2 ノルム |

### エラー

- `404` ノードが存在しない

→ 全列の意味: [Architecture — Storage & Schema](Architecture-Storage-And-Schema.md)

---

## GET /graph

共起グラフのエッジを確認する。

### クエリパラメータ

| パラメータ | 型 | デフォルト | 説明 |
|---|---|---|---|
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

全動的状態を初期値にリセットする。ドキュメントと embedding は保持される（**破壊的操作**）。

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

**リセット対象**: mass, temperature, sim_history, last_access, displacement, velocity, expires_at, is_archived, merge*, emotion_weight, certainty, last_verified_at, 共起グラフ全エッジ, directed_edges 全件

**保持されるもの**: ドキュメント本文, metadata, embedding, FAISS インデックス

---

## MCP との関係

REST API は **ベンチマーク・評価・REST クライアント向け** の薄いラッパー。LLM エージェントは MCP プロトコル経由（25 ツール）で使うのが想定。

→ MCP ツール一覧: [MCP Reference Index](MCP-Reference-Index.md)
→ サーバー起動方法: [Operations — Server Setup](Operations-Server-Setup.md)
