# GER-RAG MCP Server 設計書

**日付**: 2026-03-28
**ステータス**: 設計
**前提**: [mcp_concept.md](../../mcp_concept.md) のコンセプトに基づく

## 1. ビジョン

GER-RAGをAIエージェントの**外部長期記憶**として機能させる。

エージェントは検索するだけでなく、自らの思考・発言・判断も記憶に蓄積できる。記憶は重力により自己組織化し、使い込むほど「そのエージェント」に最適化された知識構造が形成される。

### 従来のRAG-MCPとの差別化

| | 従来のRAG-MCP | GER-RAG MCP |
|--|--------------|-------------|
| 検索 | 静的（毎回同じ結果） | 動的（使うほど結果が変わる） |
| 記憶 | 読み取り専用 | **双方向（読み書き）** |
| 構造 | フラット | **重力で自己組織化** |
| コンテキスト | 1セッション | **セッション横断で蓄積** |
| 創発性 | なし | **予想外のつながりが浮上** |

## 2. MCPツール設計

### 2.1 remember — 記憶の登録

エージェント自身の発言、思考、ユーザーの指示、コンパクティング結果を記憶に登録する。

```json
{
  "name": "remember",
  "description": "知識やコンテキストを長期記憶に登録する。エージェント自身の思考・判断・要約も保存可能。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "content": {
        "type": "string",
        "description": "記憶する内容（テキスト）"
      },
      "source": {
        "type": "string",
        "enum": ["agent", "user", "system", "compaction"],
        "description": "記憶の出所。agent=エージェント自身の思考、user=ユーザーの発言、system=システム情報、compaction=コンテキスト圧縮の退避"
      },
      "tags": {
        "type": "array",
        "items": {"type": "string"},
        "description": "分類タグ（任意）"
      },
      "context": {
        "type": "string",
        "description": "この記憶が生まれた文脈の簡潔な説明（任意）"
      }
    },
    "required": ["content"]
  }
}
```

**使用例**:
- エージェントが重要な設計判断をしたとき → `source: "agent"`, `content: "ユーザーはuvを好む。pip禁止。"`
- コンテキスト圧縮時 → `source: "compaction"`, `content: "会話前半の要約: GER-RAGのPhase 1を実装した。..."`
- ユーザーの好みを記録 → `source: "user"`, `content: "宇宙テーマのUIが好き"`

### 2.2 recall — 記憶の検索

重力変位付きの検索。使い込むほど関連記憶が浮上しやすくなる。

```json
{
  "name": "recall",
  "description": "長期記憶を検索する。重力変位により、頻繁に関連する記憶が優先される。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "検索クエリ"
      },
      "top_k": {
        "type": "integer",
        "default": 5,
        "description": "返却する記憶の数"
      },
      "source_filter": {
        "type": "array",
        "items": {"type": "string"},
        "description": "出所でフィルタ（例: [\"agent\", \"compaction\"]）"
      }
    },
    "required": ["query"]
  }
}
```

**レスポンス**: 各記憶のcontent, source, tags, raw_score, final_score, mass, displacement_norm

### 2.3 explore — 創発的探索

temperatureを意図的に上げた探索的検索。予想外のつながりを発見する。

```json
{
  "name": "explore",
  "description": "創発的に記憶を探索する。温度を上げてランダム性を加え、普段は浮上しない記憶を発見する。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "探索の起点となるクエリ"
      },
      "diversity": {
        "type": "number",
        "default": 0.5,
        "minimum": 0.0,
        "maximum": 1.0,
        "description": "多様性（0=通常検索に近い, 1=最大限の探索）"
      },
      "top_k": {
        "type": "integer",
        "default": 10
      }
    },
    "required": ["query"]
  }
}
```

**実装方針**: diversityに応じてtemperature noiseを増幅 + 候補取得範囲を拡大（candidate_multiplier動的変更）。通常の `recall` では浮上しない異クラスタの記憶を意図的に引き出す。

### 2.4 reflect — 知識構造の自己分析

エージェントが自分の記憶状態を振り返る。「最近何をよく考えてる？」「どんな知識クラスタがある？」

```json
{
  "name": "reflect",
  "description": "記憶の状態を分析する。高mass（よく検索される）の知識、活発な共起クラスタ、最近の傾向を要約する。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "aspect": {
        "type": "string",
        "enum": ["summary", "hot_topics", "connections", "dormant"],
        "default": "summary",
        "description": "分析の観点。summary=全体要約, hot_topics=高mass知識, connections=強い共起, dormant=長期未アクセス"
      },
      "limit": {
        "type": "integer",
        "default": 10,
        "description": "返却するアイテム数"
      }
    }
  }
}
```

**レスポンス例** (aspect="summary"):
```json
{
  "total_memories": 12010,
  "active_memories": 351,
  "total_edges": 11000,
  "top_clusters": [
    {"topic": "AI・機械学習", "mass_sum": 45.2, "node_count": 28},
    {"topic": "プログラミング", "mass_sum": 38.7, "node_count": 22}
  ],
  "recent_focus": ["重力変位の設計", "MCP概念検討"],
  "dormant_ratio": 0.97
}
```

### 2.5 ingest — ファイル/ディレクトリ一括取り込み

外部ファイルから知識を一括登録。

```json
{
  "name": "ingest",
  "description": "ファイルまたはディレクトリから知識を一括取り込む。Markdown, テキスト, CSVに対応。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string",
        "description": "ファイルパスまたはディレクトリパス"
      },
      "source": {
        "type": "string",
        "default": "file",
        "description": "記憶の出所ラベル"
      },
      "recursive": {
        "type": "boolean",
        "default": false,
        "description": "ディレクトリの場合、再帰的に取り込むか"
      },
      "pattern": {
        "type": "string",
        "default": "*.md,*.txt",
        "description": "取り込むファイルのglob パターン（カンマ区切り）"
      },
      "chunk_size": {
        "type": "integer",
        "default": 2000,
        "description": "長文のチャンク分割サイズ（文字数）"
      }
    },
    "required": ["path"]
  }
}
```

**対応フォーマット**:
- `.md` — Markdownをセクション単位またはチャンク分割
- `.txt` — プレーンテキストを段落/チャンク分割
- `.csv` — 既存のload_csv.pyと同等の処理（カラム自動検出）
- ディレクトリ — glob パターンでファイルを収集、再帰オプション

## 3. MCPリソース設計

### 3.1 memory://stats

記憶全体の統計情報。

```
URI: memory://stats
```

```json
{
  "total_memories": 12010,
  "active_memories": 351,
  "total_edges": 11000,
  "displaced_nodes": 351,
  "max_mass": 12.1,
  "max_displacement": 0.3,
  "sources": {"tweet": 7658, "like": 4203, "agent": 42, "compaction": 15}
}
```

### 3.2 memory://hot

高massノード（よく使われる記憶）のリスト。

```
URI: memory://hot?limit=10
```

### 3.3 memory://graph

共起グラフの要約（エッジ数、クラスタ情報）。

```
URI: memory://graph?min_weight=1.0
```

### 3.4 memory://node/{id}

特定ノードの詳細状態。

```
URI: memory://node/{node_id}
```

## 4. MCPプロンプト設計

### 4.1 context-recall

現在の会話コンテキストに関連する記憶を呼び出す。

```json
{
  "name": "context-recall",
  "description": "現在の話題に関連する記憶を呼び出して回答に活用する",
  "arguments": [
    {"name": "topic", "description": "現在の話題", "required": true}
  ]
}
```

生成されるメッセージ:
```
以下の長期記憶を参考にして回答してください。

{recall(topic, top_k=5) の結果}

これらの記憶はGER-RAGの重力モデルにより、過去の検索パターンから関連性の高い順に並んでいます。
```

### 4.2 save-context

現在の会話コンテキストを長期記憶に退避する（コンパクティング用）。

```json
{
  "name": "save-context",
  "description": "現在の会話の重要なポイントを長期記憶に保存する",
  "arguments": [
    {"name": "summary", "description": "保存する内容の要約", "required": true}
  ]
}
```

### 4.3 explore-connections

異なるトピック間のつながりを探索する。

```json
{
  "name": "explore-connections",
  "description": "2つのトピック間の意外なつながりを探索する",
  "arguments": [
    {"name": "topic_a", "required": true},
    {"name": "topic_b", "required": true}
  ]
}
```

## 5. トランスポートとアーキテクチャ

### 5.1 構成

```
┌─────────────────────┐     ┌──────────────────────────┐
│  MCPクライアント      │     │  GER-RAG MCP Server       │
│  (Claude Code,       │────→│                           │
│   自作エージェント等)  │ MCP │  ┌─────────────────────┐  │
│                      │────→│  │  MCP Protocol Layer  │  │
│                      │     │  │  (Tools/Resources/   │  │
│                      │     │  │   Prompts)           │  │
│                      │     │  └──────────┬──────────┘  │
│                      │     │             │              │
│                      │     │  ┌──────────▼──────────┐  │
│                      │     │  │  GEREngine           │  │
│                      │     │  │  (既存コア、変更なし)    │  │
│                      │     │  └──────────────────────┘  │
└─────────────────────┘     └──────────────────────────┘
```

### 5.2 トランスポート選択

| トランスポート | 用途 | 利点 |
|-------------|------|------|
| **stdio** | Claude Code / Claude Desktop | 最も標準的、設定が簡単 |
| **SSE (HTTP)** | リモートクライアント / 自作エージェント | ネットワーク越しに接続可能 |

**推奨**: stdio をデフォルトとし、`--transport sse` オプションでHTTPも選べるようにする。

### 5.3 既存FastAPIとの関係

```
ger_rag/
├── server/
│   ├── app.py          # FastAPI (既存REST API、維持)
│   └── mcp_server.py   # MCP Server (新規)
```

FastAPIとMCPサーバーは同じ `GEREngine` を共有する。MCPサーバーはエンジンを直接呼び出し、FastAPIを経由しない。

## 6. コンパクティング連携

### フロー

```
1. エージェントの会話が長くなる
2. コンテキスト圧縮が発生
3. 圧縮前に重要な情報を remember(source="compaction") で退避
4. 次の会話で recall("前回の議論") → コンパクティングした記憶が重力で浮上
5. 繰り返すほど重要な知識のmassが蓄積 → 常にアクセスしやすくなる
```

### エージェント側のシステムプロンプト案

```
あなたはGER-RAG長期記憶にアクセスできます。

- 重要な判断や発見をしたとき → remember で記録
- 関連知識が必要なとき → recall で検索
- 新しい発想が欲しいとき → explore で探索
- コンテキストが長くなったら → save-context で退避
- 自分の知識状態を確認したいとき → reflect で振り返り
```

## 7. ファイル取り込み仕様

### ingestツールの処理フロー

```
入力パス
  │
  ├─ ファイルの場合 → 拡張子で分岐
  │   ├─ .md  → セクション分割（## 見出し単位）→ チャンクに分割
  │   ├─ .txt → 段落分割（空行区切り）→ チャンクに分割
  │   └─ .csv → カラム自動検出（content/text列）→ 行ごとに登録
  │
  ├─ ディレクトリの場合 → globでファイル収集 → 各ファイルを上記処理
  │
  └─ メタデータ付与
      ├─ source: 指定値 or "file"
      ├─ file_path: 元ファイルのパス
      ├─ file_name: ファイル名
      └─ section: セクション見出し（mdの場合）
```

### Markdown分割ルール

```markdown
# Title           → メタデータのtitle
## Section 1      → チャンク1 (見出し含む)
テキスト...
### Subsection    → チャンク1に含む（##単位で分割）
## Section 2      → チャンク2
テキスト...
```

- `##` 見出し単位で分割（`#` は文書タイトルとしてメタデータに）
- 分割後のチャンクが `chunk_size` を超える場合はさらに段落で分割

## 8. 実装ファイル構成

```
ger_rag/
├── server/
│   ├── app.py              # FastAPI (既存、維持)
│   └── mcp_server.py       # MCP Server (新規)
├── ingest/                  # 新規: ファイル取り込み
│   ├── __init__.py
│   ├── markdown.py          # Markdown分割
│   ├── plaintext.py         # プレーンテキスト分割
│   └── csv_loader.py        # CSV取り込み（既存load_csv.pyを移植）
└── ...
```

### 依存関係

```toml
# pyproject.toml に追加
"mcp[cli]>=1.0.0",
```

### 起動方法

```bash
# stdio (Claude Code / Claude Desktop用)
.venv/bin/python -m ger_rag.server.mcp_server

# SSE (リモートクライアント用)
.venv/bin/python -m ger_rag.server.mcp_server --transport sse --port 8001

# Claude Code設定 (~/.claude/claude_desktop_config.json)
{
  "mcpServers": {
    "ger-rag-memory": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "ger_rag.server.mcp_server"]
    }
  }
}
```

## 9. 段階的実装計画

### Step 1: 基盤
- MCPサーバーの骨格（stdio transport）
- `remember` + `recall` ツール（最小限の双方向記憶）
- GEREngine の直接利用

### Step 2: 取り込み
- `ingest` ツール
- Markdown / テキスト / CSV パーサー
- ディレクトリ再帰取り込み

### Step 3: 創発機能
- `explore` ツール（温度制御付き）
- `reflect` ツール（知識構造分析）

### Step 4: リソースとプロンプト
- MCPリソース (memory://stats, hot, graph, node)
- MCPプロンプト (context-recall, save-context, explore-connections)

### Step 5: SSEトランスポート
- HTTPベースの接続対応
- リモートクライアント向け
