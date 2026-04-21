# Getting Started

GaOTTT をゼロからインストールして、最初の `remember` と `recall` を打つまで、約 5 分。

## 前提

- Python 3.11+（推奨 3.12）
- `uv`（推奨）
- 約 4GB のディスク（ruri-v3-310m モデル ~1.2GB + 余裕）
- CUDA GPU は任意（CPU でも動く）

## 1. インストール

```bash
git clone https://github.com/May-Kirihara/GER-RAG.git
cd GaOTTT
uv venv .venv --python 3.12
uv pip install -e ".[dev]"
```

CPU だけで動かすなら:

```bash
uv pip install torch --index-url https://download.pytorch.org/whl/cpu
uv pip install -e ".[dev]"
```

## 2. データディレクトリの確認

GaOTTT は OS ごとの固定ディレクトリに DB と FAISS index を保存します。どこから起動しても同じデータ:

| OS | データディレクトリ |
|---|---|
| Linux/macOS | `~/.local/share/gaottt/` |
| Windows | `%LOCALAPPDATA%\ger-rag\` |

カスタマイズ:
```bash
export GAOTTT_DATA_DIR=/path/to/data
```

## 3. MCP サーバー起動（Claude Code / Claude Desktop で使う場合）

`.mcp.json.example` を `.mcp.json` にコピーしてパスを書き換える、または直接起動:

```bash
.venv/bin/python -m gaottt.server.mcp_server
```

Claude Code に登録すると、SKILL.md が自動的にロードされ、`mcp__gaottt__remember` などのツールが使えます。

詳細は [Operations — Server Setup](Operations-Server-Setup.md)。

## 4. 最初の記憶を保存

MCP クライアント（Claude Code 等）から:

```
remember(
  content="今日 GaOTTT のインストールが完了した",
  source="agent",
  tags=["milestone"],
  emotion=0.5,
  certainty=1.0,
)
```

返り値: `Remembered. ID: <uuid>`

## 5. 思い出してみる

```
recall(query="GaOTTT のインストール", top_k=3)
```

返り値に先ほど保存した記憶が、`final_score`・`raw_score`・`source`・`displacement` の値とともに浮上します。

## 6. ファイル一括取り込み（任意）

ノートやドキュメントが既にある場合:

```bash
.venv/bin/python scripts/load_files.py ~/path/to/notes/ --recursive --source notes
```

または MCP 経由で:

```
ingest(path="~/path/to/notes/", pattern="*.md", recursive=true)
```

## 7. 状態を眺める

```
reflect(aspect="summary")
```

→ 全メモリ数、active ノード数、共起エッジ数、ソース内訳

```
reflect(aspect="hot_topics", limit=10)
```

→ 質量が大きい記憶（よく recall されるもの）

## 次に進む

- **長期記憶として育てる** → [Guides — Use as Memory](Guides-Use-As-Memory.md)
- **タスク管理に使う** → [Guides — Use as Task Manager](Guides-Use-As-Task-Manager.md)
- **人格保存基盤として** → [Guides — Use as Persona Base](Guides-Use-As-Persona-Base.md)
- **使えるツール一覧** → [MCP Tool Index](MCP-Reference-Index.md)

## 詰まったら

→ [Operations — Troubleshooting](Operations-Troubleshooting.md)
