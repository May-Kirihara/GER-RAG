# Operations — Server Setup

GaOTTT の環境構築、データ投入、2 種類のサーバー（REST と MCP）の起動・停止・登録方法。

## 環境要件

| 項目 | 要件 |
|---|---|
| Python | 3.11 以上（推奨 3.12） |
| GPU | CUDA 対応 GPU（任意、CPU でも動く） |
| メモリ | 4GB 以上推奨（モデル + キャッシュ） |
| ディスク | 初回モデルダウンロードに約 2GB |
| パッケージ管理 | uv（推奨） |

## セットアップ

```bash
# 仮想環境作成
uv venv .venv --python 3.12

# 依存関係インストール
uv pip install -e ".[dev]"

# 可視化ツール（任意）
uv pip install plotly umap-learn

# GPU 版 FAISS を使う場合
uv pip install -e ".[gpu]"

# CPU 版 PyTorch を明示的に使う場合（軽量）
uv pip install torch --index-url https://download.pytorch.org/whl/cpu
uv pip install -e ".[dev]"
```



## REST API サーバー（FastAPI）

ベンチマーク・評価・REST クライアントから使う。

```bash
# 起動
.venv/bin/uvicorn gaottt.server.app:app --host 0.0.0.0 --port 8000

# 開発時（自動リロード）
.venv/bin/uvicorn gaottt.server.app:app --reload

# 停止: Ctrl+C → graceful shutdown → dirty フラッシュ → FAISS 保存
```

Swagger UI: http://localhost:8000/docs

## MCP サーバー

LLM の長期記憶として使う。プロトコル仕様は [`SKILL.md`](../../SKILL.md)。

```bash
# stdio (Claude Code / Claude Desktop)
.venv/bin/python -m gaottt.server.mcp_server

# SSE (リモートクライアント)
.venv/bin/python -m gaottt.server.mcp_server --transport sse --port 8001
```

公開ツール（25 個）:
- 基本: `remember` / `recall` / `explore` / `reflect` / `ingest`
- F1/F4/F5: `auto_remember` / `forget` / `restore`
- F2.1: `merge` / `compact`
- F7: `revalidate`
- F3: `relate` / `unrelate` / `get_relations`
- F6: `prefetch` / `prefetch_status`
- Phase D: `commit` / `start` / `complete` / `abandon` / `depend` / `declare_value` / `declare_intention` / `declare_commitment` / `inherit_persona`

## Claude Code への登録

`.mcp.json` （リポジトリルート）:

```json
{
  "mcpServers": {
    "gaottt": {
      "command": "/path/to/GER-RAG/.venv/bin/python",
      "args": ["-m", "gaottt.server.mcp_server"],
      "cwd": "/path/to/GER-RAG"
    }
  }
}
```

## OpenCode への登録

`opencode.json`:

```json
{
  "mcp": {
    "gaottt": {
      "type": "local",
      "command": [
        "/path/to/GER-RAG/.venv/bin/python",
        "-m",
        "gaottt.server.mcp_server"
      ]
    }
  }
}
```

## モデルダウンロード

初回起動時に RURI-v3-310m（約 1.2GB）が HuggingFace からダウンロードされる。2 回目以降はローカルキャッシュ（`~/.cache/huggingface/hub/`）から即座にロード、HTTP リクエストは発生しない。

## データディレクトリ

| OS | データディレクトリ | 設定ファイル |
|---|---|---|
| Linux/macOS | `~/.local/share/gaottt/` | `~/.config/ger-rag/config.json` |
| Windows | `%LOCALAPPDATA%\ger-rag\` | `%APPDATA%\ger-rag\config.json` |

カスタマイズ:
```bash
export GAOTTT_DATA_DIR=/path/to/data
export GAOTTT_CONFIG=/path/to/config.json
```

## データ投入

### CSV から一括投入

```bash
.venv/bin/python scripts/load_csv.py                   # 全件
.venv/bin/python scripts/load_csv.py --limit 100       # テスト用
.venv/bin/python scripts/load_csv.py --max-chunk-chars 3000
```

DM/group_DM は自動除外。長文は `---` セパレータまたは段落区切りで自動チャンク分割。重複 content は SHA-256 で自動スキップ（同じ CSV を複数回投入しても安全）。

### ファイル/ディレクトリから一括投入

```bash
.venv/bin/python scripts/load_files.py ~/notes/ --recursive
.venv/bin/python scripts/load_files.py ~/notes/ --pattern "*.md,*.txt" -r
.venv/bin/python scripts/load_files.py ~/notes/ --source notes -r
.venv/bin/python scripts/load_files.py ~/notes/ -r --dry-run    # 確認用
```

### MCP 経由

```
ingest(path="~/notes/", pattern="*.md", recursive=true)
ingest(path="~/data.csv")
```

## テストクエリの実行

```bash
.venv/bin/python scripts/test_queries.py --mode basic         # 5 クエリ
.venv/bin/python scripts/test_queries.py --mode full --rounds 3   # 37 トピック
.venv/bin/python scripts/test_queries.py --mode stress --rounds 10  # 82 クエリ/ラウンド
```

| モード | クエリ数/ラウンド | 用途 |
|---|---|---|
| basic | 5 | 動作確認 |
| full | 37（毎回シャッフル） | 多様なトピック網羅、共起パターン生成 |
| stress | 82（37 多様 + 45 バースト） | 可視化デモ前の大量蓄積 |

→ 関連: [Tuning](Operations-Tuning.md), [Compact & Backup](Operations-Compact-And-Backup.md), [Isolated Benchmark](Operations-Isolated-Benchmark.md), [Troubleshooting](Operations-Troubleshooting.md)
