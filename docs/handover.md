# 引継書

## プロジェクト概要

**GER-RAG** (Gravity-Based Event-Driven RAG) は、知識ノードが質量・温度・重力変位といった物理的メタデータを持ち、**共起した文書同士が重力で引き寄せ合う**ことで創発的な検索結果を生み出すシステムである。

目的は検索精度の最適化ではなく、**セレンディピティと創発** — 使い込むほど予想外のつながりが発見される検索体験。

## 現在の状態 (Phase 2 実装済み)

### 実装済み機能

| 機能 | エンドポイント / スクリプト | 状態 |
|------|--------------------------|------|
| ドキュメント登録 | POST /index | 完了 (SHA-256重複チェック付き) |
| 重力変位付き検索 | POST /query | 完了 (二段階: FAISS候補→仮想座標再計算) |
| ノード状態確認 | GET /node/{id} | 完了 (displacement_norm含む) |
| 共起グラフ確認 | GET /graph | 完了 |
| 状態リセット | POST /reset | 完了 (displacement含む) |
| CSV投入 | scripts/load_csv.py | 完了 |
| テストクエリ | scripts/test_queries.py | 完了 (basic/full/stress 3モード) |
| Cosmic 3D可視化 | scripts/visualize_3d.py | 完了 (恒星表現、原始/仮想座標比較) |
| ベンチマーク | scripts/benchmark.py | 完了 (SC-001〜SC-007) |
| 評価エクスポート | scripts/eval_export.py | 完了 (LLM-as-judge用) |
| 評価メトリクス算出 | scripts/eval_compute.py | 完了 (nDCG, MRR, Precision) |

### 技術スタック

| コンポーネント | 選定技術 | 選定理由 |
|-------------|---------|---------|
| Embedding | RURI-v3-310m (768次元) | 日本語特化、8192トークン対応 |
| ベクトル検索 | FAISS IndexFlatIP | 100K件で~1ms、exact search |
| 重力計算 | NumPyベクトル演算 | 純粋関数、gravity.pyに集約 |
| ストレージ | SQLite WAL + aiosqlite | 非同期、displacement BLOB永続化 |
| キャッシュ | インメモリdict + write-behind | displacement_cacheを含む |
| API | FastAPI + uvicorn | 非同期、自動ドキュメント生成 |
| 可視化 | Plotly + PCA/UMAP | 恒星色温度表現、原始/仮想座標比較 |
| パッケージ管理 | uv | 高速、Python環境構築 |

### 設計判断の記録

| 判断事項 | 決定内容 | 経緯 |
|---------|---------|------|
| 二重座標系 | 原始embedding(不変) + 仮想座標(重力変動) | Phase 1評価で単一空間での限界が判明 |
| 重力モデル | 万有引力 F = G*m_i*m_j/d² | 物理的直感に合致、パラメータが明快 |
| 変位上限 | max_displacement_norm=0.3 | 暴走防止、同一大トピック内の移動に制限 |
| 候補拡張 | FAISS top-K×3 → 仮想座標で再計算 | FAISSリビルド不要、レイテンシ維持 |
| graph_boost廃止 | 重力変位に統合 | スコア加算では順位変動が不足 |
| 並行性 | Last-write-wins（ロックなし） | シングルインスタンス前提 |
| 重複チェック | content SHA-256ハッシュ | embedding生成前にスキップ |
| モデルキャッシュ | ローカルキャッシュ自動検出 | HuggingFace API通信を完全抑制 |

## コードの読み方

### エントリポイント

1. **サーバー起動**: `server/app.py` の `lifespan()` → 全コンポーネント初期化（displacement含む）
2. **クエリ処理**: `engine.query()` → FAISS広め取得 → `gravity.compute_virtual_position()` → 仮想座標sim → top-K選出
3. **重力更新**: `engine._update_state_after_query()` → `gravity.update_displacements_for_cooccurrence()` → `cache.set_displacement()`
4. **重複チェック**: `engine.index_documents()` → `store.find_existing_hashes()`

### 主要クラスの役割

| クラス | ファイル | 責務 |
|-------|---------|------|
| GEREngine | core/engine.py | 二段階検索 + 重力更新オーケストレーション |
| gravity.py | core/gravity.py | 重力計算（force, displacement更新, decay, clamp, virtual_pos） |
| RuriEmbedder | embedding/ruri.py | テキスト→ベクトル変換（ローカルキャッシュ自動検出） |
| FaissIndex | index/faiss_index.py | ベクトル近傍探索 + get_vectors()原始embedding逆引き |
| CacheLayer | store/cache.py | インメモリ状態管理 + displacement_cache + write-behind |
| SqliteStore | store/sqlite_store.py | 永続化（displacement BLOB含む、自動マイグレーション対応） |
| CooccurrenceGraph | graph/cooccurrence.py | 共起エッジの形成・減衰・剪定 |

### gravity.py は純粋関数

`core/gravity.py` の全関数は副作用なしの純粋関数。ユニットテストが書きやすい。

```python
compute_virtual_position()    # original + displacement → L2正規化
compute_gravitational_force() # 万有引力ベクトル計算
update_displacements_for_cooccurrence()  # 共起ペア全体の変位更新
apply_displacement_decay()    # 時間ベース減衰
clamp_displacement()          # ノルム上限クランプ
```

## スクリプト詳細

### scripts/load_csv.py
- `input/documents.csv` を読み込み、`POST /index` に分割投入
- DM/group_DMはプライバシー保護のため自動除外
- 長文（2000文字超）は `---` セパレータまたは段落区切りで自動チャンク分割
- 結果: 9,060行 → 約12,010チャンク

### scripts/test_queries.py
- **basic**: 5クエリ（動作確認）
- **full**: 37クエリ（多様なトピック + 架橋クエリで共起促進）
- **stress**: 82クエリ/ラウンド（多様 + 集中バーストでmass/displacement急速蓄積）

### scripts/visualize_3d.py (Cosmic View)
- FAISSから原始embedding + SQLiteからdisplacement を読み取り
- PCA/UMAPで3Dに次元削減
- 恒星色温度表現: temperature→スペクトル型 (M赤 → K橙 → G黄 → F白 → A/B青白)
- mass→恒星サイズ、decay×mass→明るさ
- `--compare` モードで原始座標と仮想座標を並列表示

### scripts/benchmark.py
- SC-001〜SC-007の成功基準を自動検証
- レイテンシ、mass蓄積、temporal decay、共起エッジ、並行処理を測定

### scripts/eval_export.py + eval_compute.py
- 静的RAG vs GER-RAGの比較データ書き出し
- セッション適応性: Before/After方式（リセット→観測→トレーニング→再観測）
- LLM-as-judge用プロンプト生成 → 外部LLMで判定 → nDCG/MRR/Precision算出

## Phase 2 評価結果サマリ

### 静的RAGとの比較

| メトリクス | Static RAG | GER-RAG | 差分 |
|-----------|-----------|---------|------|
| nDCG@10 | 0.9457 | 0.9708 | +2.7% |
| MRR | 0.8833 | 1.0000 | +13.2% |

### セッション適応性（重力変位後）

- 500クエリのトレーニングで10,000+エッジ、350+ノードが変位
- S2 (映画×食×旅) で nDCG +15.0% の改善
- 全シナリオ平均 nDCG +3.8%

詳細: [docs/research/evaluation-report.md](research/evaluation-report.md)

## 将来の拡張 (Phase 3)

### 本番強化

- **PostgreSQL移行**: `store/base.py` のStoreBaseに対してPostgres実装を追加
- **MCP Server統合**: `/query` をMCPツールとして公開
- **マルチユーザー状態分離**: NodeState, CacheLayerにユーザーIDディメンション追加
- **認証**: FastAPIミドルウェアでAPIキー or OAuth2
- **IndexIVFFlat移行**: 100K件超でFAISSインデックスをIVFに切り替え

### 拡張時の注意点

- `store/base.py` のStoreBaseインターフェースを崩さない
- embeddingのL2正規化は必須（仮想座標もL2正規化している）
- RURI-v3のプレフィックス（「検索クエリ: 」「検索文書: 」）は省略不可
- displacement BLOB は768次元 float32（3KB/ノード）
- 既存DBは起動時にALTER TABLEで自動マイグレーション

## ファイル構成

```
GER-RAG/
├── ger_rag/                  # メインパッケージ
│   ├── config.py             # ハイパーパラメータ (★チューニング対象、重力含む)
│   ├── core/
│   │   ├── engine.py         # 二段階検索エンジン + 重力更新
│   │   ├── gravity.py        # 重力計算モジュール（純粋関数）
│   │   ├── scorer.py         # スコアリング純粋関数
│   │   └── types.py          # Pydanticモデル
│   ├── embedding/            # Embeddingモデル (キャッシュ自動検出)
│   ├── index/                # ベクトルインデックス (get_vectors逆引き対応)
│   ├── store/                # 永続化 (displacement BLOB、自動マイグレーション)
│   ├── graph/                # 共起グラフ
│   └── server/               # FastAPI
├── scripts/
│   ├── load_csv.py           # CSV投入
│   ├── test_queries.py       # テストクエリ (basic/full/stress)
│   ├── visualize_3d.py       # Cosmic 3D可視化 (恒星表現、--compare)
│   ├── benchmark.py          # ベンチマーク (SC-001〜SC-007)
│   ├── eval_export.py        # 評価データ書き出し
│   └── eval_compute.py       # 評価メトリクス算出
├── input/
│   └── documents.csv         # テストCSV (9,060行 → 約12,010チャンク)
├── eval_output/              # 評価結果
├── docs/                     # 保守ドキュメント
│   ├── research/             # 評価レポート、設計書
│   └── ...
├── specs/                    # 設計ドキュメント (speckit)
├── pyproject.toml            # プロジェクト定義
└── .gitignore
```

## 連絡事項

- 設計経緯: `specs/001-ger-rag-core/` 以下の spec.md, plan.md, research.md
- 重力変位の設計根拠: `docs/research/gravitational-displacement-design.md`
- Phase 2評価の詳細: `docs/research/evaluation-report.md`
- スコアリング数式: `plan.md` Section 6
- ハイパーパラメータ推奨範囲: `plan.md` Section 11 + `config.py`
