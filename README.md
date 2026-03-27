# GER-RAG

**Gravity-Based Event-Driven RAG** - 重力による創発的自己組織化検索システム

## 概要

GER-RAGは、知識ノードが質量（mass）・温度（temperature）・重力変位（displacement）といった物理的メタデータを持ち、**共起した文書同士が重力で引き寄せ合う**ことで創発的な検索結果を生み出すRAGシステムである。

- **頻繁に検索される知識**は質量が増加し、周囲の文書を引き寄せるハブ（恒星）になる
- **一緒に検索される知識**は重力で互いに接近し、次の検索で予想外のつながりが発見される
- **長期間アクセスされない知識**は変位が減衰し、元のembedding位置に静かに戻る
- **原始embedding空間は不変**のまま、仮想座標空間での重力変位により創発的検索を実現

## クイックスタート

### セットアップ

```bash
uv venv .venv --python 3.12
uv pip install -e ".[dev]"
uv pip install plotly umap-learn  # 可視化（任意）
```

### サーバー起動

```bash
.venv/bin/uvicorn ger_rag.server.app:app --host 0.0.0.0 --port 8000
```

### データ投入 → クエリ → 可視化

```bash
# 1. ドキュメント投入
.venv/bin/python scripts/load_csv.py

# 2. 大量クエリで重力を蓄積
.venv/bin/python scripts/test_queries.py --mode stress --rounds 10

# 3. サーバー停止後、宇宙空間を可視化
.venv/bin/python scripts/visualize_3d.py --compare --sample 3000 --open
```

### API経由の操作

```bash
# ドキュメント登録
curl -X POST http://localhost:8000/index \
  -H "Content-Type: application/json" \
  -d '{"documents": [{"content": "Pythonは汎用プログラミング言語です。"}]}'

# 検索（重力変位を反映した結果が返る）
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"text": "プログラミングについて", "top_k": 5}'
```

## Cosmic 3D可視化

ドキュメントを宇宙空間の恒星として表現。使い込むほど星の配置が変わる。

```bash
# 仮想座標ビュー（重力変位後の宇宙空間）
.venv/bin/python scripts/visualize_3d.py --sample 3000 --open

# 原始座標 vs 仮想座標の並列比較
.venv/bin/python scripts/visualize_3d.py --compare --sample 3000 --open
```

| 視覚要素 | 恒星アナロジー |
|---------|--------------|
| サイズ | Mass — 赤色巨星（大きく安定）vs 矮星（小さい） |
| 色温度 | Temperature — M赤 → K橙 → G黄 → F白 → A/B青白 |
| 明るさ | Decay × Mass — 最近アクセスされた高質量星が最も明るい |
| フィラメント | 共起エッジ — 宇宙の大規模構造 |

## API

| メソッド | パス | 説明 |
|---------|------|------|
| POST | /index | ドキュメント登録（SHA-256重複自動スキップ） |
| POST | /query | 重力変位付き検索（二段階: FAISS候補→仮想座標再計算） |
| GET | /node/{id} | ノード状態確認（displacement_norm含む） |
| GET | /graph | 共起グラフ確認 |
| POST | /reset | 動的状態リセット（displacement含む） |

Swagger UI: http://localhost:8000/docs

## 重力モデル

```
# 二重座標系
原始embedding (不変) ──→ FAISS広め候補取得 (top-K × 3)
                              ↓
仮想座標 = normalize(original_emb + displacement)
                              ↓
gravity_sim = dot(query, virtual_pos)
final_score = gravity_sim * decay + mass_boost
                              ↓
重力更新: F = G * m_i * m_j / d²  →  displacement蓄積
```

| 要素 | 数式 | 効果 |
|------|------|------|
| gravity_sim | dot(query, virtual_pos) | 仮想座標での類似度（重力で変動） |
| decay | exp(-δ * (now - last_access)) | 最近アクセスされたものを優先 |
| mass_boost | α * log(1 + mass) | 頻出ドキュメントを優先 |
| gravitational force | G * m_i * m_j / d² | 共起ノード間の引力 |
| displacement decay | 時間とともに0に漸近 | 未アクセスノードは原始位置に回帰 |

## 技術スタック

| コンポーネント | 技術 |
|-------------|------|
| Embedding | [RURI-v3-310m](https://huggingface.co/cl-nagoya/ruri-v3-310m) (768次元, 日本語特化) |
| ベクトル検索 | FAISS IndexFlatIP |
| 重力計算 | NumPy (gravity.py) |
| ストレージ | SQLite (WAL) + インメモリキャッシュ |
| API | FastAPI |
| 可視化 | Plotly + PCA/UMAP (Cosmic View) |
| パッケージ管理 | uv |

## ドキュメント

### 保守・運用

- [アーキテクチャ概要](docs/architecture.md) - 二重座標系、重力モデル、データフロー、モジュール構成
- [APIリファレンス](docs/api-reference.md) - 全エンドポイントの詳細仕様
- [運用・保守ガイド](docs/operations.md) - セットアップ、テスト、可視化、チューニング
- [引継書](docs/handover.md) - 設計判断、コードの読み方、スクリプト詳細、ロードマップ

### 評価・研究

- [Phase 2 評価レポート](docs/research/evaluation-report.md) - Static RAG比較、セッション適応性、ベンチマーク
- [Gravitational Displacement 設計書](docs/research/gravitational-displacement-design.md) - 重力座標変位の設計

### 設計ドキュメント

- [機能仕様](specs/001-ger-rag-core/spec.md) - ユーザーストーリー、機能要件、成功基準
- [実装計画](specs/001-ger-rag-core/plan.md) - 技術選定、プロジェクト構成
- [技術調査](specs/001-ger-rag-core/research.md) - RURI-v3, FAISS, SQLite WAL等の調査結果
- [データモデル](specs/001-ger-rag-core/data-model.md) - エンティティ定義、ハイパーパラメータ一覧
- [APIコントラクト](specs/001-ger-rag-core/contracts/api.md) - API設計仕様
- [設計原案](plan.md) - GER-RAGの着想・数理的背景

## ライセンス

Private
