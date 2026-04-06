# GER-RAG

**Gravity-Based Event-Driven RAG** - 重力で知識が引き合う、AIの長期外部記憶

> これは「ドキュメントに質量と速度と万有引力を与えたらどうなるか」を探求する研究プロジェクトです。たぶんエターナルベータ。だって宇宙はまだ膨張してるし。

[English README](README.md)

## 概要

GER-RAGは、**AIエージェントの長期外部記憶**として設計された検索システムである。使い込むほど知識同士が引き合い、**創発的なつながりやひらめき**を生み出す。

技術ドキュメント、自炊した書籍、過去のトラブルシューティング記録、設計判断のログなど — 散在する知識を投入すると、検索のたびに関連ドキュメントが重力で引き寄せ合い、通常のRAGでは出会えない**異分野間のつながり**が浮かび上がる。MCPサーバーとして動作し、Claude CodeやClaude Desktop等のAIエージェントからシームレスに利用できる。

### 仕組み

知識ノードは質量・温度・重力変位といった物理的メタデータを持ち、共起した文書同士が**万有引力で引き寄せ合う**。エージェントが思考し、検索し、記憶を蓄積するたびに、知識空間が自己組織化されていく。

- **頻繁に検索される知識**は質量が増加し、周囲の文書を引き寄せるハブ（恒星）になる
- **一緒に検索される知識**は重力で互いに接近し、次の検索で予想外のつながりが発見される
- **長期間アクセスされない知識**は変位が減衰し、元のembedding位置に静かに戻る
- **エージェント自身の思考やトラブルシューティング経験**も記憶に蓄積でき、セッションを越えて活用される
- **原始embedding空間は不変**のまま、仮想座標空間での重力変位により創発的検索を実現

### 活用例

| ユースケース | 使い方 |
|------------|--------|
| 技術ドキュメントの横断検索 | 社内Wiki・設計書を`ingest`で取り込み、`recall`で検索 |
| 自炊した書籍の知識ベース | `load_files.py`で書籍mdを一括投入、読書メモと書籍内容が重力で結合 |
| トラブルシューティング記録 | エラーと解決策を`remember`で蓄積、次に似た問題で`recall`が即座に浮上 |
| 設計判断のログ | 判断理由を`remember`で記録、後から「なぜこの設計にしたか」を`recall` |
| コンテキスト圧縮の退避 | 長い会話の要約を`remember(source="compaction")`で退避、次回セッションで復元 |
| 発想の転換・ブレスト | `explore(diversity=0.8)`で異分野の記憶を横断し、意外な着想を得る |

## 要件

| 項目 | 推奨 | 最低 |
|------|------|------|
| Python | 3.12 | 3.11 |
| OS | Linux / macOS / Windows | |
| GPU | CUDA対応GPU (embedding高速化) | なし (CPU動作可) |
| メモリ | 8GB+ | 4GB |
| ディスク | 4GB+ (モデル ~2GB + データ) | |
| パッケージ管理 | uv | pip も可 |

### GPU / CPU 動作

| | GPU (CUDA) | CPU |
|--|-----------|-----|
| クエリ速度 | ~20ms | ~200-500ms |
| バッチ投入 (12K文書) | ~6分 | ~30-60分 |
| 起動 | 通常 | 通常 |

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

### データ投入

#### ファイル・ディレクトリの一括取り込み（load_files.py）

```bash
# 指定ディレクトリ以下のmdを再帰的に取り込み
.venv/bin/python scripts/load_files.py ~/path/to/your/documents/ --recursive

# 中身を確認してから投入（dry-run）
.venv/bin/python scripts/load_files.py ~/path/to/your/documents/ -r --dry-run

# 特定のファイルだけ
.venv/bin/python scripts/load_files.py ~/path/to/your/documents/meeting_notes.md

# txtとmd混在
.venv/bin/python scripts/load_files.py ~/path/to/your/documents/ --pattern "*.md,*.txt" -r

# sourceラベル付き（後でrecallのsource_filterで絞れる）
.venv/bin/python scripts/load_files.py ~/path/to/your/documents/ --source book -r

# チャンクサイズを大きめに（長い章を保持）
.venv/bin/python scripts/load_files.py ~/documents/ --chunk-size 3000 -r
```

#### CSV取り込み（load_csv.py）

```bash
.venv/bin/python scripts/load_csv.py                       # 全件投入
.venv/bin/python scripts/load_csv.py --limit 100            # テスト用に100件だけ
```

#### MCP ingestツール経由

MCPクライアント（Claude Code等）から直接取り込み:

```
ingest(path="docs/architecture.md")                         # 単一ファイル
ingest(path="notes/", pattern="*.md", recursive=true)       # ディレクトリ
ingest(path="data/articles.csv")                            # CSV
```

#### REST API経由

FastAPIサーバー起動中に任意のJSONで投入:

```bash
curl -X POST http://localhost:8000/index \
  -H "Content-Type: application/json" \
  -d '{"documents": [
    {"content": "Pythonは汎用プログラミング言語です。"},
    {"content": "機械学習はAIの一分野です。", "metadata": {"source": "manual", "tags": ["AI"]}}
  ]}'
```

### クエリ → 可視化

```bash
# 大量クエリで重力を蓄積
.venv/bin/python scripts/test_queries.py --mode stress --rounds 10

# サーバー停止後、宇宙空間を可視化
.venv/bin/python scripts/visualize_3d.py --compare --sample 3000 --open
```

### 対応フォーマット

| 形式 | 分割方式 | 備考 |
|------|---------|------|
| `.md` | `##` 見出し単位 → 長い場合はさらにチャンク分割 | `#` はタイトルとしてメタデータに |
| `.txt` | 段落（空行）単位 → チャンク分割 | |
| `.csv` | 行単位、`content`/`text`/`body`列を自動検出 | 他の列はメタデータに |
| REST API | 任意のJSONで直接投入 | metadata付きで自由な構造 |
| MCP `remember` | エージェント発言・コンパクティング等 | source/tags/contextをメタデータに |

## MCPサーバー (AIエージェント長期記憶)

GER-RAGをMCPプロトコルで公開し、AIエージェントの**外部長期記憶**として使う。

```bash
# Claude Code / Claude Desktop (stdio)
.venv/bin/python -m ger_rag.server.mcp_server

# リモートクライアント (SSE)
.venv/bin/python -m ger_rag.server.mcp_server --transport sse --port 8001
```

### 登録方法

#### Claude Code

`.mcp.json.example` を `.mcp.json` にコピーしてパスを編集:

```json
{
  "mcpServers": {
    "ger-rag-memory": {
      "command": "/path/to/GER-RAG/.venv/bin/python",
      "args": ["-m", "ger_rag.server.mcp_server"],
      "cwd": "/path/to/GER-RAG"
    }
  }
}
```

#### OpenCode

`opencode.json` に以下を追加:

```json
{
  "mcp": {
    "ger-rag-memory": {
      "type": "local",
      "command": [
        "/path/to/GER-RAG/.venv/bin/python",
        "-m",
        "ger_rag.server.mcp_server"
      ]
    }
  }
}
```

### ツール一覧

| ツール | 用途 |
|--------|------|
| `remember` | 思考・発見・ユーザー指示・トラブルシューティング・コンテキスト圧縮を記録 |
| `recall` | 重力変位付き検索（使い込むほど関連記憶が浮上しやすくなる） |
| `explore` | 温度を上げた創発的探索（予想外のつながりを発見） |
| `reflect` | 記憶の自己分析（「最近何をよく考えてる？」） |
| `ingest` | ファイル/ディレクトリの一括取り込み（md, txt, csv） |

### SKILL.md (エージェント向けスキル定義)

[SKILL.md](SKILL.md) はAIエージェントがGER-RAG長期記憶の**使い方・使いどころ**を理解するためのスキル定義ファイル。OpenClaw等のエージェントフレームワークから参照可能。

記載内容:
- 各ツールの呼び出し例
- 利用パターン（コンテキスト圧縮退避、文脈復元、判断記録、トラブルシューティング記録、ユーザー好み記録、創発探索）
- sourceの使い分け（agent / user / compaction / system）

## Embedding空間の可視化

各埋め込みドキュメントを宇宙空間の恒星のように表現。使い込むほど星の配置が変わる。

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
| シアン矢印 | 速度ベクトル — 次のステップの移動方向と距離 |
| 金色リング | 重力半径 — massから物理計算されたノードの重力圏 |
| 紫◆ | 共起ブラックホール — 共起クラスタの重心引力源 |
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
クエリ → 重力波伝播（再帰的近傍展開、mass依存top-k、重力半径カットオフ）
            ↓
        N ノード到達（シミュレーション層）
            ↓
        仮想座標 = normalize(original_emb + displacement)
        final = (gravity_sim * decay + mass_boost + wave_boost) * saturation
            ↓
        top-k=5 をLLMに返却（プレゼンテーション層）
            ↓
        軌道力学（全到達ノード）:
          Stage 1: a = Σ[G*m_j/r²]*dir + (-k*disp) + a_bh*escape   ← 引力 + アンカー + BH
          Stage 2: v += a*dt, v *= (1-friction), clamp               ← 速度（慣性）
          Stage 3: displacement += v*dt, clamp                        ← 位置更新
```

| 要素 | 数式 | 効果 |
|------|------|------|
| gravity_sim | dot(query, virtual_pos) | 仮想座標での類似度（重力で変動） |
| decay | exp(-δ * (now - last_access)) | 最近アクセスされたものを優先 |
| mass_boost | α * log(1 + mass) | 頻出ドキュメントを優先 |
| **返却飽和** | 1 / (1 + return_count * rate) | 馴化 — 繰り返し返されたノードのスコア低下 |
| wave_boost | β * wave_force | 重力波伝播によるブースト |
| 近傍引力 | G * m_j / (r² + ε) | 共起ノード間の引力 |
| **BH引力** | G * bh_mass / r² * escape | 共起クラスタ重心の超大質量ブラックホール |
| **温度脱出** | 1 / (1 + temp * scale) | 高温ノードがBH束縛から脱出 |
| アンカー復元 | -k * displacement | Hooke則 — 原始位置への復元力（脱出防止） |
| 重力半径 | 1 - G*mass/(2*a_min) | mass依存の到達範囲（実際の物理から導出） |
| 摩擦 | v *= (1 - f) | 速度減衰 — 軌道寿命を制御 |

## 技術スタック

| コンポーネント | 技術 |
|-------------|------|
| Embedding | [RURI-v3-310m](https://huggingface.co/cl-nagoya/ruri-v3-310m) (768次元, 日本語特化) |
| ベクトル検索 | FAISS IndexFlatIP |
| 重力・軌道力学 | NumPy (gravity.py) |
| ストレージ | SQLite (WAL) + インメモリキャッシュ |
| API | FastAPI (REST) + MCP Server (エージェント長期記憶) |
| 可視化 | Plotly + PCA/UMAP (Cosmic View) |
| パッケージ管理 | uv |

## データディレクトリ

GER-RAGのデータ（DB、FAISSインデックス）はプラットフォームごとの固定ディレクトリに保存される。どのディレクトリからサーバーやMCPを起動しても同じデータを参照する。

| OS | データディレクトリ | 設定ファイル |
|----|-----------------|------------|
| Linux | `~/.local/share/ger-rag/` | `~/.config/ger-rag/config.json` |
| macOS | `~/.local/share/ger-rag/` | `~/.config/ger-rag/config.json` |
| Windows | `%LOCALAPPDATA%\ger-rag\` | `%APPDATA%\ger-rag\config.json` |

### カスタマイズ

```bash
# 環境変数（一時的）
export GER_RAG_DATA_DIR=/path/to/data

# 設定ファイル（永続的）— ~/.config/ger-rag/config.json
{"data_dir": "/path/to/data"}

# 設定ファイルの場所も変更可能
export GER_RAG_CONFIG=/path/to/config.json
```

## ドキュメント

### 保守・運用

- [アーキテクチャ概要](docs/architecture.md) - 二重座標系、重力モデル、データフロー、モジュール構成
- [APIリファレンス](docs/api-reference.md) - 全エンドポイントの詳細仕様
- [運用・保守ガイド](docs/operations.md) - セットアップ、テスト、可視化、チューニング
- [引継書](docs/handover.md) - 設計判断、コードの読み方、スクリプト詳細、ロードマップ

### 評価・研究

- [評価レポート](docs/research/evaluation-report.md) - Static RAG比較、セッション適応性、創発性指標、ベンチマーク
- [Gravitational Displacement 設計書](docs/research/gravitational-displacement-design.md) - 重力座標変位の設計
- [Gravity Wave Propagation 設計書](docs/research/gravity-wave-propagation-design.md) - 再帰的重力波伝播
- [Orbital Mechanics 設計書](docs/research/orbital-mechanics-design.md) - 速度ベクトル、軌道力学、アンカー引力
- [Co-occurrence Black Hole 設計書](docs/research/cooccurrence-blackhole-design.md) - 共起クラスタBH
- [Habituation & Thermal Escape 設計書](docs/research/habituation-escape-design.md) - 返却飽和、温度脱出
- [MCP Server 設計書](docs/research/mcp-server-design.md) - AIエージェント外部長期記憶

### 設計ドキュメント

- [機能仕様](specs/001-ger-rag-core/spec.md) - ユーザーストーリー、機能要件、成功基準
- [実装計画](specs/001-ger-rag-core/plan.md) - 技術選定、プロジェクト構成
- [技術調査](specs/001-ger-rag-core/research.md) - RURI-v3, FAISS, SQLite WAL等の調査結果
- [データモデル](specs/001-ger-rag-core/data-model.md) - エンティティ定義、ハイパーパラメータ一覧
- [APIコントラクト](specs/001-ger-rag-core/contracts/api.md) - API設計仕様
- [設計原案](plan.md) - GER-RAGの着想・数理的背景