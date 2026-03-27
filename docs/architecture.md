# アーキテクチャ概要

## システム構成

GER-RAGは**二重座標系**による創発的検索システムである。原始embedding空間は不変のまま、共起した文書同士が**重力で引き寄せ合う仮想座標空間**で検索を行う。使い込むほど意外なつながりが発見される。

```
クエリ入力
  │
  ▼
[RURI-v3 Embedding] ──→ クエリベクトル (768次元, L2正規化済)
  │                      ※キャッシュ済みならローカルから即座にロード
  ▼
[FAISS IndexFlatIP] ──→ 広い候補取得 (top-K × 3, 原始embedding)
  │
  ▼
[仮想座標での再計算]
  │  virtual_pos = normalize(original_emb + displacement)
  │  gravity_sim = dot(query, virtual_pos)
  │
  ├─ mass_boost   = α * log(1 + mass)
  └─ decay        = exp(-δ * (now - last_access))
  │
  ▼
final_score = gravity_sim * decay + mass_boost
  │ (負スコアは除外、top-Kに絞る)
  ▼
[重力更新] ──→ 共起ペア間に万有引力 → displacement蓄積
  │            mass増加, temperature再計算, 共起グラフ更新
  ▼
[Write-behind] ──→ 非同期でSQLiteにフラッシュ
```

## 二重座標系

```
┌──────────────────────────────────────────────────┐
│  原始embedding空間 (immutable)                    │
│  ・RURI-v3の出力そのまま、FAISSに格納              │
│  ・広い候補取得 (top-K * 3) に使用                 │
│  ・変更されない（意味的保証を維持）                  │
└──────────────────────────────────────────────────┘
                    ↓ 候補取得
┌──────────────────────────────────────────────────┐
│  仮想座標空間 (mutable, gravity-driven)            │
│  ・virtual_pos = normalize(original + displacement)│
│  ・共起した文書同士が重力で引き寄せ合う              │
│  ・massが大きいノードほど強い引力                    │
│  ・最終ランキングの類似度計算に使用                   │
│  ・クエリのたびに更新される                          │
└──────────────────────────────────────────────────┘
```

## 重力モデル

共起したノードペア (i, j) に万有引力が働く：

```
direction_ij = normalize(original_emb[j] - virtual_pos[i])
force_ij     = G * mass[i] * mass[j] / (distance_ij² + ε)
displacement[i] += η_g * force_ij * direction_ij
```

- 変位は `max_displacement_norm` (0.3) でクランプ（暴走防止）
- 変位は時間とともに減衰（未アクセスノードは原始位置に回帰）
- temperatureによる座標揺らぎで探索的な検索を促進

## モジュール構成

```
ger_rag/
├── config.py               # GERConfig: 全ハイパーパラメータ（重力パラメータ含む）
├── core/
│   ├── engine.py           # GEREngine: 二段階検索 + 重力更新
│   ├── gravity.py          # 重力計算（force, displacement更新, decay, clamp）
│   ├── scorer.py           # スコアリング関数群 (純粋関数)
│   └── types.py            # Pydanticモデル (リクエスト/レスポンス/内部型)
├── embedding/
│   └── ruri.py             # RuriEmbedder: RURI-v3ラッパー (ローカルキャッシュ自動検出)
├── index/
│   └── faiss_index.py      # FaissIndex: FAISS IndexFlatIPラッパー + ベクトル逆引き
├── store/
│   ├── base.py             # StoreBase: 抽象ストアインターフェース
│   ├── sqlite_store.py     # SqliteStore: SQLite WAL実装 (displacement永続化含む)
│   └── cache.py            # CacheLayer: インメモリキャッシュ + displacement + write-behind
├── graph/
│   └── cooccurrence.py     # CooccurrenceGraph: 共起グラフ管理
├── ingest/
│   └── loader.py           # ファイル取り込み（md/txt/csv、チャンク分割）
└── server/
    ├── app.py              # FastAPIアプリ + lifespan管理
    └── mcp_server.py       # MCPサーバー（AIエージェント外部長期記憶）
```

## データフロー

### インデックス時

1. `POST /index` でドキュメント受信
2. content SHA-256ハッシュで重複チェック（重複はembedding生成前にスキップ）
3. RURI-v3で embedding生成 (「検索文書: 」プレフィックス付き)
4. L2正規化後、FAISSインデックスに追加
5. NodeState初期化 (mass=1.0, temperature=0.0, displacement=zero)
6. SQLiteにドキュメント + 状態を永続化

### クエリ時

1. `POST /query` でクエリ受信
2. RURI-v3でクエリembedding生成 (「検索クエリ: 」プレフィックス付き)
3. FAISS検索で広い候補取得 (top-K × candidate_multiplier)
4. 各候補の仮想座標を計算 (original_emb + displacement)
5. 仮想座標でのcosine similarity × decay + mass_boostで最終スコア
6. 負スコアを除外、top-Kに絞って返却
7. **返却後**に重力更新: 共起ペアにforce適用 → displacement蓄積
8. mass, temperature, 共起グラフも更新
9. ダーティ状態（displacement含む）はwrite-behindで非同期にSQLiteへフラッシュ

## ストレージ戦略

```
起動時:   SQLite → インメモリキャッシュ（node状態 + displacement）にロード + FAISSインデックスロード
稼働時:   キャッシュから読み取り → 変更はdirtyセットに記録
定期的:   write-behindタスクがdirty状態をバッチでSQLiteにフラッシュ (5秒間隔)
停止時:   write-behind停止 → 全dirty状態フラッシュ → FAISSインデックス保存 → 接続クローズ
異常終了: フラッシュされていないdirty状態は消失 (ドキュメント・embeddingは保全)
```

### SQLiteスキーマ

- `documents`: content_hash (SHA-256) + UNIQUEインデックスで重複防止
- `nodes`: displacement BLOB カラム（768次元 float32、3KB/ノード）
- 既存DBは起動時に `ALTER TABLE nodes ADD COLUMN displacement BLOB` で自動マイグレーション

## 並行性モデル

- **シングルインスタンス**前提
- ノード状態更新は**last-write-wins**（ロックなし）
- SQLite WALモードで読み書き並行可能
- write-behindは単一asyncioタスクで実行

## MCPサーバー (AIエージェント外部長期記憶)

`ger_rag/server/mcp_server.py` はGER-RAGをMCPプロトコルで公開し、AIエージェントの長期記憶として機能させる。

```
MCPクライアント (Claude Code等)
  │
  ├─ remember(content, source, tags)  → 記憶を登録
  ├─ recall(query, top_k)             → 重力変位付き検索
  ├─ explore(query, diversity)        → 創発的探索（高温度）
  ├─ reflect(aspect)                  → 記憶状態の自己分析
  └─ ingest(path, pattern)            → ファイル/ディレクトリ一括取り込み
  │
  ▼
GEREngine (FastAPIと同じエンジンを直接利用)
```

- **双方向記憶**: 検索だけでなく、エージェント自身の思考や会話圧縮も保存可能
- **トランスポート**: stdio（Claude Code/Desktop）または SSE（リモートクライアント）
- **リソース**: `memory://stats`, `memory://hot` で記憶状態を公開
- **プロンプト**: `context-recall`, `save-context`, `explore-connections`

## Embeddingモデルのキャッシュ

`RuriEmbedder`は起動時にHuggingFaceキャッシュ（`~/.cache/huggingface/`）を検査し、モデルがキャッシュ済みであれば`local_files_only=True`でロードする。オフライン環境でも動作。

## Cosmic 3D可視化

`scripts/visualize_3d.py`はFAISSインデックスとSQLiteから直接データを読み取り、原始座標と仮想座標（重力変位後）をインタラクティブなPlotly HTMLとして出力する。

宇宙空間に浮かぶ恒星としてドキュメントを表現：

| 視覚要素 | 対応する動的状態 | 恒星アナロジー |
|---------|---------------|--------------|
| サイズ | Mass (質量) | 赤色巨星（大きい）vs 矮星（小さい） |
| 色温度 | Temperature | M赤 → K橙 → G黄 → F白 → A/B青白 |
| 明るさ | Decay × Mass | 最近アクセスされた高質量ノードが最も明るい |
| フィラメント | 共起エッジ | 宇宙の大規模構造 |
| `--compare`モード | 原始 vs 仮想座標の並列比較 | 重力による星団の再配置を観察 |
