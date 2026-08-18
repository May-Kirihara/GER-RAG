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
.venv/bin/python -m uvicorn gaottt.server.app:app --host 0.0.0.0 --port 8000

# 開発時（自動リロード）
.venv/bin/python -m uvicorn gaottt.server.app:app --reload

# 停止: Ctrl+C → graceful shutdown → dirty フラッシュ → FAISS 保存
```

> **`.venv/bin/uvicorn` を直接呼ぶと動かない場合**: venv のエントリポイント (shebang) が古い `/mnt/holyland/Project/GER-RAG/.venv/bin/python3` を指していると、`bash: cannot execute: required file not found` で失敗する（GER-RAG → GaOTTT への rename の名残）。上のように `python -m uvicorn` で呼ぶか、一度だけ `sed -i 's|/mnt/holyland/Project/GER-RAG|/mnt/holyland/Project/GaOTTT|g' .venv/bin/*` で全 shebang を修正する。`uv run` を使う場合は `--no-extra gpu`（`pyproject.toml` の optional `gpu` extra は `faiss-gpu>=1.8.0` で PyPI 未公開のため、デフォルトの resolve で失敗する）。

Swagger UI: http://localhost:8000/docs

## MCP サーバー

LLM の長期記憶として使う。プロトコル仕様は [`SKILL.md`](../../SKILL.md)。

### 起動モード — default は **proxy** (auto-spawn + relay)

```bash
# (1) proxy (DEFAULT) — agent ごとに subprocess を spawn するが、
#     その subprocess は軽量 stdio shim で、初回起動時に detached な
#     HTTP backend を spawn し、以降は relay として動作する。N agents
#     all share 1 backend、agent 終了で shim も死ぬが backend は idle
#     timeout (default 5 分) まで生存して再利用される
.venv/bin/python -m gaottt.server.mcp_server
# → stdio を喋りつつ 127.0.0.1:7878 の HTTP backend を内部利用

# (2) streamable-http — HTTP backend を直接起動する。systemd 等で
#     明示常駐させたい場合用
.venv/bin/python -m gaottt.server.mcp_server --transport streamable-http --port 7878
# → http://127.0.0.1:7878/mcp に接続

# (3) SSE (旧 HTTP transport; 互換目的)
.venv/bin/python -m gaottt.server.mcp_server --transport sse --port 7878
# → http://127.0.0.1:7878/sse

# (4) stdio (legacy — agent 内に engine をフル ロード、shim 経由しない)
.venv/bin/python -m gaottt.server.mcp_server --transport stdio
```

multiverse のローカル開発構成では、shim から supervisor も含めて cold
start できる。停止中の supervisor だけを detached 起動し、その後
supervisor が embedder / universe backend を lazy spawn する:

```bash
GAOTTT_API_KEY="<universe-api-key>" \
GAOTTT_MULTIVERSE_ROOT="/absolute/path/to/gaottt-multiverse" \
GAOTTT_SUPERVISOR_ADMIN_KEY="<admin-key>" \
GAOTTT_EMBEDDER_ENDPOINT="http://127.0.0.1:7879" \
.venv/bin/python -m gaottt.server.mcp_server \
  --transport proxy \
  --supervisor-url http://127.0.0.1:7880 \
  --spawn-supervisor
```

`--spawn-supervisor` は opt-in で、local HTTP URL
(`127.0.0.1` / `localhost` / `::1`) にだけ許可される。接続済み
supervisor の 401 / 500 / read timeout では別プロセスを起動せず、TCP
connection failure の場合だけ起動する。本番 systemd 常駐構成では省略する。

| Mode | Per-agent Process | Backend Process | RAM 消費 | 推奨用途 |
|---|---|---|---|---|
| **proxy** (default) | 軽量 shim (~50 MB) | 1 (auto-spawn, idle で self-shutdown) | ~3-4 GB (合計、N agents で増えない) | **personal multi-agent 環境の標準** — `.mcp.json` 変更不要 |
| streamable-http | (なし) | 1 (常駐、systemd 等で管理) | ~3-4 GB | systemd / 24/7 backend を明示管理したい場合 |
| sse | 同上 | 同上 | 同上 | 古い MCP client の fallback |
| stdio | フル engine (~3-4 GB) | (なし) | ~3-4 GB × N | 単独 client、CI、開発 debug |

### Cold-war dead-man-switch (proxy + backend)

```
agent (Claude Code / opencode / ...)
  ↓ stdio
gaottt mcp proxy (shim)  ← 軽量、agent ごとに 1 つ
  ↓ HTTP (streamable-http MCP)
gaottt http backend  ← 1 process、全 shim で共有
  │
  ├─ idle watchdog (--idle-timeout 300 default)
  │   └─ 最後の MCP request から 300 秒経つと cache flush + self-shutdown
  │
  └─ shim 側からの ping (--ping-interval 60 default)
      └─ agent が idle でも 60 秒ごとに backend.last_activity を更新
```

冷戦の核発射シーケンスと同型: 各 silo (shim) が周期的に key を回し続ける限り operator (backend) は活動継続、全 silo が key を回さなくなったら一定時間後に stand down。

| パラメータ | default | 意味 |
|---|---|---|
| `--idle-timeout` (backend) | 300 sec | 無音許容時間。経過後に backend が自分から終了。`0` で無効化 (永久生存) |
| `--ping-interval` (proxy) | 60 sec | 各 shim が backend に打つ heartbeat 周期 (default では idle-timeout の 1/5) |

### Ping は agent の操作とは独立した background task

ここが mental model として重要な点: **shim 内の `_ping_loop` は `asyncio.create_task` で起動した独立 task** で、stdio relay と並列に実行され、agent が `recall` 等を投げているかどうかとは無関係に `--ping-interval` 秒ごとに backend へ ping を飛ばし続ける。つまり「**Agent が idle = backend が死ぬ**」ではなく「**全 agent process が close した = backend が死ぬ**」が正しい判定:

| 状態 | shim の ping | backend `last_activity` | backend |
|---|---|---|---|
| Agent 起動中、recall 全くしない | 60 秒ごとに飛ぶ | 60 秒ごと更新 | **生存** |
| Agent 起動中、頻繁に recall | recall + ping 両方 | 連続更新 | 生存 |
| Agent **idle で 5 分放置** | 飛び続ける | 更新され続ける | **生存** (idle ≠ death) |
| Agent close (Claude Code 終了) | shim 死 → ping 停止 | 最後の ping から計時 | 5 分後 SIGTERM |
| Agent 複数、1 つだけ close | 残り agent の shim が ping 継続 | 連続更新 | 生存 |
| 全 agent close | 全 ping 停止 | 5 分後 timeout | SIGTERM |

つまり「Claude Code を開いている間は何時間でも backend は生存」「全 agent を完全に閉じてから 5 分」が backend の lifecycle。

### Restart cost と memory cost の trade-off

| パターン | Backend 再起動頻度 | 常時 RAM コスト |
|---|---|---|
| `--idle-timeout 300` (default) | 全 agent close → 5 分後に終了。次回起動で proxy が ~30s で respawn | Agent open 時のみ ~3-4 GB |
| `--idle-timeout 0` | **永久生存**、明示 kill するまで動き続ける | 常時 ~3-4 GB |
| systemd 常駐 | 同上 (systemd が auto-restart 管理) | 常時 ~3-4 GB |

「日に数回 Claude Code を完全終了する派」は default のままが省 RAM、「Claude Code 終了でも backend は残しときたい派」は `--idle-timeout 0` を `.mcp.json` の args に追加:

```json
{
  "mcpServers": {
    "gaottt": {
      "command": "/path/to/GaOTTT/.venv/bin/python",
      "args": ["-m", "gaottt.server.mcp_server", "--idle-timeout", "0"],
      "cwd": "/path/to/GaOTTT"
    }
  }
}
```

(shim 側には `--idle-timeout 0` は無関係だが、spawn する backend にそのまま forward されるので結果として backend が永久生存になる。)

公開ツール（28 個）:
- 基本: `remember` / `recall` / `get_node` / `explore` / `reflect` / `ingest`
- F1/F4/F5: `auto_remember` / `forget` / `restore`
- F2.1: `merge` / `compact`
- F7: `revalidate`
- F3: `relate` / `unrelate` / `get_relations`
- F6: `prefetch` / `prefetch_status`
- Ambient/Save hook: `ambient_recall` / `save_candidates`
- Phase D: `commit` / `start` / `complete` / `abandon` / `depend` / `declare_value` / `declare_intention` / `declare_commitment` / `inherit_persona`

## Claude Code への登録

**推奨**: proxy mode を使うので `.mcp.json` の設定は単純な stdio subprocess を残せる (default が proxy になったので config は変えなくて良い)。

`.mcp.json`（リポジトリルート）:

```json
{
  "mcpServers": {
    "gaottt": {
      "command": "/path/to/GaOTTT/.venv/bin/python",
      "args": ["-m", "gaottt.server.mcp_server"],
      "cwd": "/path/to/GaOTTT"
    }
  }
}
```

これだけで:
- Claude Code 起動時 → shim subprocess spawn (軽量、~1s)
- shim が backend の有無を check → 居なければ detached spawn して engine load (~30s) を待つ
- 以降 stdio↔HTTP relay
- Claude Code 終了 → shim 死、backend は idle 5 分後に self-shutdown (Claude Code がすぐ再起動するなら backend は再利用される)

### Hook の登録 (ambient_recall + save_candidates)

**設定 file の場所**: `~/.claude/settings.json` (global、全 project で hook が effective) または `<project>/.claude/settings.json` (per-project)。複数 repo で Claude Code を使うなら global が楽 (GaOTTT 以外の repo でも同じ hook が自動で効く)。

**path は絶対パスで書く**。`$CLAUDE_PROJECT_DIR` は Claude Code が「いま開いている project」に展開するので、GaOTTT 以外の repo で起動すると hook script を **その repo の中** で探してしまい `[Errno 2] No such file or directory` で `operation blocked by hook` になる (2026-05-27 production で発覚)。

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"/Path/to/GaOTTT/.venv/bin/python\" \"/Path/to/GaOTTT/scripts/hooks/ambient_recall.py\"",
            "timeout": 10
          },
          {
            "type": "command",
            "command": "\"/Path/to/GaOTTT/.venv/bin/python\" \"/Path/to/GaOTTT/scripts/hooks/save_candidates_inject.py\"",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"/Path/to/GaOTTT/.venv/bin/python\" \"/Path/to/GaOTTT/scripts/hooks/save_candidates.py\"",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

`/Path/to/GaOTTT` は実際の GaOTTT clone の絶対パスに置き換え (例: `/Users/you/code/GaOTTT`、`/mnt/holyland/Project/GaOTTT`)。

- **UserPromptSubmit**: 2 連 hook。`ambient_recall.py` が長期記憶を `<gaottt-ambient-recall>` block で注入、`save_candidates_inject.py` が前 turn の Stop hook が書いた state file を読んで `<gaottt-save-candidates>` block を注入 (なければ no-op)
- **Stop**: `save_candidates.py` が turn 終了時に `auto_remember` を走らせ、候補があれば state file (default `~/.gaottt/save_candidates/<session_id>.txt`) に block を書き込む。次 turn の UserPromptSubmit-inject 側が読んで消す (Stop → UserPromptSubmit bridge、option A)
- どの hook も backend down/timeout で fail-silent (exit 0、agent を block しない)。ただし **hook script file 自体が見つからない** (working tree が古い branch にある等) と Python interpreter の exec-time error になり non-zero exit → `operation blocked by hook` になる。下記 troubleshooting §4 参照

**動作確認**: 設定後に新しい conversation を始め、何ターンか会話してから:

```bash
# Stop hook が動いていれば state file が現れる
ls ~/.gaottt/save_candidates/
# block を確認
cat ~/.gaottt/save_candidates/*.txt
```

state file が出ない場合の典型的な原因:
1. **backend が古い**: `ps -ef | grep streamable-http` で start 時刻を確認、commit より古ければ `kill <pid>` (次の MCP 接続で自動 respawn) — 詳細は本ページの「code deploy 時の backend 再起動」節 / [CLAUDE.md backend kill on code deploy](https://github.com/May-Kirihara/GaOTTT/blob/main/CLAUDE.md) と memory id `feedback_backend_kill_on_code_deploy`
2. **transcript 抽出 0 件**: 直近 N turn ([Operations — Tuning](Operations-Tuning.md#save_candidates-hookplans-save-candidates-hookmd) の `GAOTTT_SAVE_CANDIDATES_TURNS` 既定 2) に「決定」「失敗」「採用」等の save-worthy キーワードが無い → 設計通りの silent (sentinel `(保存候補なし)`)。短い会話なら `GAOTTT_SAVE_CANDIDATES_TURNS=5` で window を広げる
3. **settings.json が malformed**: `/doctor` で警告が出ていないか確認、`python -c "import json; json.load(open('.claude/settings.json'))"` で parse 検証
4. **`operation blocked by hook` + `[Errno 2] No such file or directory`**: 設定ファイルが `$CLAUDE_PROJECT_DIR` を使っている、または GaOTTT working tree が古い branch (PR #28/#29 以前) に check out されたままで `scripts/hooks/save_candidates*.py` が disk に無い。`cd /Path/to/GaOTTT && git checkout main && git pull` で main に揃える + settings.json の path を上記の絶対パス形式に書き換える

env tuning (`GAOTTT_SAVE_CANDIDATES_*`) は [Operations — Tuning](Operations-Tuning.md#save_candidates-hookplans-save-candidates-hookmd) 参照。詳細設計と option A bridge の理由は [Plans — Save Candidates Hook](Plans-Save-Candidates-Hook.md)。

### opencode hook の登録

opencode は Claude Code と違い `chat.message` plugin が message text を直接編集できるので、ambient_recall も save_candidates も **plugin 1 本ずつ** で済む (state-file bridge 不要):

```bash
# 1. plugin を install (cp または ln -sf。開発中なら symlink が便利)
mkdir -p ~/.config/opencode/plugin
cp scripts/hooks/opencode-ambient-recall.ts \
   ~/.config/opencode/plugin/gaottt-ambient-recall.ts
cp scripts/hooks/opencode-save-candidates.ts \
   ~/.config/opencode/plugin/gaottt-save-candidates.ts

# 2. shell rc (~/.bashrc / ~/.zshrc) に GAOTTT_REPO を export
#    ※ 必ず設定。自分の GaOTTT clone の絶対 path を指す
echo 'export GAOTTT_REPO=/Path/to/GaOTTT' >> ~/.bashrc
source ~/.bashrc
```

⚠️ **`GAOTTT_REPO` の設定は必須**。TS plugin は内部で `process.env.GAOTTT_REPO ?? "/mnt/holyland/Project/GaOTTT"` をフォールバックで持っているが、これは **本リポジトリ作者の machine の path がたまたま hard-coded されているだけ**。他人の machine では env を set しないと plugin が wrong path で Python interpreter を探して silent fail する (Bash の `which python` のように見える error は一切出ず、ただ block が injection されないだけ)。

opencode 起動時に `*.ts` が auto-load される。プロジェクト単位で有効化したい場合は `~/.config/opencode/plugin/` の代わりに `<project>/.opencode/plugin/` に置く。

仕組み:
- `opencode-ambient-recall.ts` (chat.message) → Python `ambient_recall.py` を spawn → `<gaottt-ambient-recall>` block を append
- `opencode-save-candidates.ts` (chat.message) → `client.session.messages` で前ターン (user N-1, assistant N-1) を fetch → `[role] text` 整形 → Python `save_candidates.py` を `GAOTTT_SAVE_CANDIDATES_EMIT=stdout` で spawn → `<gaottt-save-candidates>` block を append
- どちらも backend は `scripts/hooks/*.py` を再利用 ([opencode-ambient-recall.ts](https://github.com/May-Kirihara/GaOTTT/blob/main/scripts/hooks/opencode-ambient-recall.ts) と同じ single-source-of-truth 原則)、TS 側は薄い shim
- どちらも fail-silent — backend down / timeout で agent は block されない

**動作確認**: opencode を起動して 2 turn ほど会話してから、`GAOTTT_SAVE_CANDIDATES_DEBUG=/tmp/sc.log opencode` の形で起動すると spawn step trace が `/tmp/sc.log` に追記される (ambient 側は `GAOTTT_AMBIENT_DEBUG`)。

opencode plugin が呼ばれない場合の典型:
1. **backend (port 7878) が動いていない** — `curl -sf http://127.0.0.1:7878/mcp` を確認
2. **Python interpreter path mismatch** — opencode が repo 外から起動されると `GAOTTT_REPO` (default `/mnt/holyland/Project/GaOTTT`) が違う path を指すので、`export GAOTTT_REPO=/your/path` を opencode 起動環境に追加
3. **第 1 ターンは silent** — opencode plugin の save_candidates は前ターン lookback なので、session 1 turn目は何も出ない (設計通り)

### 旧 stdio mode (full engine in subprocess)

意図的に proxy を bypass したい場合 (debug / CI):

```json
{
  "mcpServers": {
    "gaottt": {
      "command": "/path/to/GaOTTT/.venv/bin/python",
      "args": ["-m", "gaottt.server.mcp_server", "--transport", "stdio"],
      "cwd": "/path/to/GaOTTT"
    }
  }
}
```

### 明示的 HTTP backend (systemd) 経由

systemd で常駐 backend を管理する場合:

```json
{
  "mcpServers": {
    "gaottt": {
      "type": "http",
      "url": "http://127.0.0.1:7878/mcp"
    }
  }
}
```

## OpenCode への登録

**推奨**: proxy mode 経由 (上の Claude Code と同様、config 変更不要)

`opencode.json`:

```json
{
  "mcp": {
    "gaottt": {
      "type": "local",
      "command": [
        "/path/to/GaOTTT/.venv/bin/python",
        "-m",
        "gaottt.server.mcp_server"
      ]
    }
  }
}
```

### 明示的 HTTP backend 経由

```json
{
  "mcp": {
    "gaottt": {
      "type": "remote",
      "url": "http://127.0.0.1:7878/mcp"
    }
  }
}
```

## Codex CLI への登録

OpenAI Codex CLI (https://github.com/openai/codex) は `~/.codex/config.toml`（または プロジェクト直下の `.codex/config.toml`）の `[mcp_servers.<name>]` セクションで MCP サーバーを宣言する。CLI から `codex mcp add` でワンライナーで追加するか、TOML を直接編集するか、どちらでも良い。

### 方法 1: `codex mcp add`（推奨）

```bash
# macOS / Linux
codex mcp add gaottt -- /path/to/GaOTTT/.venv/bin/python -m gaottt.server.mcp_server

# Windows PowerShell
codex mcp add gaottt -- "$HOME\GaOTTT\.venv\Scripts\python.exe" -m gaottt.server.mcp_server
```

確認:

```bash
codex mcp list
```

### 方法 2: `~/.codex/config.toml` を直接編集

**推奨**: proxy mode を使うので default の stdio subprocess 設定をそのまま書ける（他クライアントと port 7878 backend を共有、N agents で RAM ~3-4 GB 1 process のまま）:

```toml
[mcp_servers.gaottt]
command = "/path/to/GaOTTT/.venv/bin/python"
args = ["-m", "gaottt.server.mcp_server"]
cwd = "/path/to/GaOTTT"
```

オプション項目（必要な場合のみ）:

```toml
[mcp_servers.gaottt]
command = "/path/to/GaOTTT/.venv/bin/python"
args = ["-m", "gaottt.server.mcp_server"]
cwd = "/path/to/GaOTTT"
# データディレクトリを project ごとに分けたい場合
env = { GAOTTT_DATA_DIR = "/path/to/project-A/.gaottt" }
# 初回起動で RURI モデル (~1.2 GB) のロードに ~30s 掛かるので、
# Codex default の startup_timeout_sec=10 では足りずタイムアウトすることがある
startup_timeout_sec = 60
# 一部ツールだけ露出 / 抑制したい場合
# enabled_tools = ["recall", "ambient_recall", "reflect"]
# disabled_tools = ["forget"]
```

### 旧 stdio mode (明示)

意図的に proxy を bypass したい場合 (debug / CI):

```toml
[mcp_servers.gaottt]
command = "/path/to/GaOTTT/.venv/bin/python"
args = ["-m", "gaottt.server.mcp_server", "--transport", "stdio"]
cwd = "/path/to/GaOTTT"
startup_timeout_sec = 60
```

### 明示的 HTTP backend (systemd) 経由

Codex CLI も remote HTTP MCP transport をサポートしている（書式は version によって変わるので [openai/codex docs](https://github.com/openai/codex) を確認）。systemd で常駐 backend を管理する場合は、上の Claude Code / OpenCode と同じく `http://127.0.0.1:7878/mcp` を指す。

## OpenWebUI への登録

[OpenWebUI](https://github.com/open-webui/open-webui) は Web UI から MCP server を利用できるクライアント。GaOTTT の streamable-http backend に接続する。

> ⚠️ **version 但し書き**: OpenWebUI の MCP 設定 UI は version により変動する。streamable HTTP MCP transport 経由で GaOTTT への接続を OpenWebUI v0.9.6 で実機検証済み。UI 経路は version により変動するので、以下は streamable-http + `/mcp` endpoint という本質に焦点を当てた手順で、UI 経路は参考例扱い (OpenWebUI 公式 docs と手元の version で照合すること)。

### 前提: streamable-http backend を常駐起動

proxy mode の auto-spawn は想定外の env 継承 (memory `project_proxy_backend_env_not_delivered`) を招くので、OpenWebUI のような外部クライアントからは **明示的 streamable-http backend** を推奨。最も安定なのは [「systemd で backend を明示常駐させたい場合」セクション](#systemd-で-backend-を明示常駐させたい場合) で常駐管理する方法。手元で一時的に立てる場合は:

```bash
/path/to/GaOTTT/.venv/bin/python -m gaottt.server.mcp_server \
  --transport streamable-http \
  --host <bind> --port 7878 \
  --idle-timeout 0
```

`--idle-timeout 0` は disable と等価 (`gaottt/server/mcp_server.py:1276` に `if args.idle_timeout > 0: _install_idle_watcher(...)` の guard があり、0 を渡すと watcher が install されず永続化する)。default `300` (5 分) では OpenWebUI の長時間 idle で backend が落ちるので明示的に `0` を渡すか、systemd 常駐を使うこと。

### URL 選択 matrix

OpenWebUI の稼働場所により URL と `--host` が変わる:

| OpenWebUI の位置 | GaOTTT の `--host` | OpenWebUI 側の URL | 備考 |
|---|---|---|---|
| GaOTTT と同一 host (process) | `127.0.0.1` (default) | `http://127.0.0.1:7878/mcp` | 最も簡単。認証無しでも localhost 完結なら許容 |
| Docker Desktop (Mac/Win) 上の container | `0.0.0.0` | `http://host.docker.internal:7878/mcp` | ⚠️ **no-auth + 全 IF bind なので reverse proxy + auth + TLS 必須** |
| Linux Docker container | `0.0.0.0` 又は `--network=host` | `--network=host` 時は `http://127.0.0.1:7878/mcp`、それ以外は `http://host.docker.internal:7878/mcp` (`--add-host=host.docker.internal:host-gateway` を併用) | ⚠️ **no-auth + 全 IF bind なので reverse proxy + auth + TLS 必須** |
| 別 host (remote) | VPN / SSH tunnel / reverse proxy の奥 | その endpoint | `--host 0.0.0.0` 単独は **非推奨** |

> ⚠️ **GaOTTT は認証を一切実装していない** (`gaottt/server/mcp_server.py` の `--host` help 参照: "Use 0.0.0.0 only if you've configured your own auth — no auth is built in")。`--host 0.0.0.0` を default にせず、信頼できないネットワーク (LAN / Internet) に直露出しない。Docker / remote 構成では **必ず** reverse proxy (Caddy / nginx / Cloudflare Access 等) + auth + TLS、又は VPN / SSH tunnel を併用すること。上記 matrix で `--host 0.0.0.0` を使う 2 行 (Docker Desktop, Linux Docker) は特に注意。

### OpenWebUI 側での MCP Server 登録

UI 経路は version により変動するが、設定すべき本質要素は共通:

1. **Admin Panel → Settings → External Tools** (最近の version) 又は **Workspace → MCP Servers** から **`+` (Add Connection)** を開く
2. 以下を設定:
   - **Type**: `MCP Streamable HTTP`
   - **URL**: 上記 matrix に従う (例: `http://127.0.0.1:7878/mcp`)
   - **Auth**: `None` (GaOTTT は認証未実装のため)
3. 保存 → 必要に応じて workspace / model / user で enable (UI 依存)

### 接続確認

- OpenWebUI 側で GaOTTT の **想定 tool 数** (現在 28、違う場合は [MCP Tool Index](MCP-Reference-Index.md) と server version を照合) が一覧表示されれば成功
- GaOTTT 側 log に `/mcp` への POST が記録される

### 初回起動時間

初回接続だけ timeout する場合は **RURI モデル (~1.2GB) のロードに ~30s**、**virtual FAISS build に数十秒〜数分** (23k 件規模) が掛かるため。OpenWebUI 側の MCP 初期化 timeout が短すぎる場合は調整、又は事前に backend を立ち上げておく。

### troubleshooting

- **接続できない**:
  - backend が listening しているか: `lsof -i :7878` (無ければ `ss -ltnp | grep 7878`)
  - OpenWebUI container の network namespace から到達可能か: OpenWebUI の環境 (container 内 等) で `curl -i http://<url>/mcp` を実行し、HTTP response が返ること (streamable HTTP MCP は通常 POST + SSE 応答なので、単純 GET でも 400/405 等の HTTP レスポンスが返れば到達性は OK、connection refused なら到達不可)
- **backend が間欠的に死ぬ**:
  - default `--idle-timeout 300` (5 min) が切れている → `--idle-timeout 0` で無効化するか、systemd 常駐に切り替える
- **初回接続だけ timeout する**: 上記「初回起動時間」参照
- **新 commit を deploy しても挙動が変わらない**:
  - proxy mode の HTTP backend が古いコードのまま動いている。`pkill -f streamable-http` して respawn させる (memory `feedback_backend_kill_on_code_deploy` 参照、[CLAUDE.md backend kill on code deploy](https://github.com/May-Kirihara/GaOTTT/blob/main/CLAUDE.md))

## SKILL.md の取り扱い

`SKILL.md`（ツール呼び出しプロトコルの仕様、英語）は **MCP の `instructions` フィールド経由で接続時に自動配信される**。Claude Code / Claude Desktop / OpenCode / Codex CLI のいずれでも、MCP server 接続が確立した時点でクライアントが instructions を受け取る — **追加配置は基本不要**。

Codex CLI で **常時** プロンプト文脈に乗せたい (instructions の自然な伝播では弱いと感じる) 場合のみ、Codex の AGENTS.md 機構を使う:

| 配置場所 | スコープ | 用途 |
|---|---|---|
| `~/.codex/AGENTS.md` | global | 全プロジェクトで GaOTTT を使う場合 |
| `<repo>/AGENTS.md` | project | 特定プロジェクトで GaOTTT を強く意識させたい |
| `<repo>/.codex/AGENTS.md` | project (codex 専用) | global を覆い隠したい場合 |
| `AGENTS.override.md` (任意ディレクトリ) | local override | 既存 `AGENTS.md` を残して上書き |

Codex CLI は global → project root → cwd の順で concatenate する。最小限の追記例 (既存 AGENTS.md を破壊しないので `>>` で追記):

```bash
cat >> ~/.codex/AGENTS.md <<'EOF'

## GaOTTT (long-term memory)

If the `gaottt` MCP server is connected, use it as cross-session long-term memory.
Protocol spec (when load is needed): /path/to/GaOTTT/SKILL.md
EOF
```

> 補足: 他クライアントでの SKILL.md ハンドリング
> - **Claude Desktop / OpenCode**: MCP `instructions` 自動配信のみ。手動配置不要。
> - **Claude Code**: 同上 + 任意で `.claude/skills/gaottt/SKILL.md` を置くと slash command / skill discovery 経由でも参照可能 (本リポジトリは既に同期コピーを置いてある)。
> - **OpenClaw**: 独自 skill scan を持つので `~/.openclaw/skills/gaottt/SKILL.md` への明示コピーが推奨 ([Tutorial-03 セクション D](Tutorial-03-Connect-Your-Client.md#d-openclaw))。

## 旧 stdio multi-process setup からの移行手順

これまで複数 agent が個別に full engine を spawn していた環境からの切り替え:

1. **既存の stdio MCP server を全部 kill**: `pkill -f 'gaottt.server.mcp_server'`
2. **`.mcp.json` / `opencode.json` は変更不要** — proxy mode が default なので既存の `command` based config がそのまま動く
3. **agent (Claude Code / opencode) を再起動** — 起動時に proxy が backend を spawn する (~30s)
4. **動作確認**:
   - `lsof -i :7878` で backend が listening していること
   - `ps aux | grep mcp_server` で `--transport streamable-http` が 1 process だけあること
   - Claude Code の `/mcp` で `gaottt` が `connected`

## systemd で backend を明示常駐させたい場合

proxy mode の auto-spawn を使わず、backend を OS service として管理:

```bash
# ~/.config/systemd/user/gaottt-mcp.service
cat > ~/.config/systemd/user/gaottt-mcp.service <<'EOF'
[Unit]
Description=GaOTTT MCP Server (streamable-http, shared)
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/GaOTTT
# ★ opt-in tuning は **ここ (backend の env)** で渡す。proxy mode の auto-spawn は
#   「最初に backend を spawn した frontend」の env しか継承しないため、frontend
#   launcher (.claude.json / opencode.json / .codex/hooks.json) 側に env を書いても
#   どの frontend が backend を立てたかで config が非決定的になる
#   (`project_proxy_backend_env_not_delivered`)。共有 backend を単一の真実源にするなら
#   env はこの unit に集約するのが正しい。code default 化済みの knob (anti-hub λ=0.4,
#   dormant percentile=10, dormant age=7d) は省略可。
# Environment=GAOTTT_DIRECT_HIT_ANTI_HUB_LAMBDA=0.4
# Environment=GAOTTT_DORMANT_MASS_PERCENTILE=10
# Environment=GAOTTT_DORMANT_AGE_THRESHOLD_SECONDS=604800
# Mass Evaporation は physics 挙動変更 (measurement-first)。enable する場合のみ ↓ を有効化:
# Environment=GAOTTT_MASS_EVAPORATION_ENABLED=true
ExecStart=/path/to/GaOTTT/.venv/bin/python -m gaottt.server.mcp_server --transport streamable-http --port 7878 --idle-timeout 0
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now gaottt-mcp.service
systemctl --user status gaottt-mcp.service
```

`--idle-timeout 0` で idle 終了を無効化 (systemd 管理下では意味がない)。ログ確認: `journalctl --user -u gaottt-mcp.service -f`。

この場合は agent config を `type: http` + URL に書き換える (上の「明示的 HTTP backend」セクション)。

## embedding service を分離する（Multiverse MV1 — 2026-07-02）

engine プロセス（複数起動しがち）でそれぞれ RURI model をロードする代わりに、**ホストに1つ embedding service を常駐** させて model load を 1 回に抑える。GPU コストをユーザー数ではなくホスト数に比例させる。詳細: [Plans — Multiverse Scale-Out](Plans-Multiverse-Scale-Out.md) §Stage 1、[multiverse-implementation-plan.md](../maintainers/multiverse-implementation-plan.md) §MV1。

### 起動

```bash
# localhost の port 7879 に service を常駐させる
/path/to/GaOTTT/.venv/bin/python -m gaottt.embedding.service \
    --host 127.0.0.1 \
    --port 7879 \
    --model cl-nagoya/ruri-v3-310m \
    --max-queue 32
```

- 初回起動時に RURI-v3-310m（約 1.2GB）が HuggingFace から download される（2 回目以降は cache から即ロード）
- `--host` は **必ず localhost**（`127.0.0.1` / `localhost` / `::1` のいずれか）。それ以外の host を渡すと **起動時に `SystemExit` で拒否**（認証を持たない `/encode` が外部に露出するのを防ぐ）。remote access が必要な場合は前段に認証付き reverse proxy を置く
- wire protocol: `POST /encode` JSON `{"kind": "query"|"document", "texts": [...]}` → `application/x-msgpack` `{"shape": [N, dim], "dtype": "float32", "data": <bytes>}`。`GET /info` は `{"model_name", "dimension", "version", "batch_size"}`。`GET /healthz` は `{"status": "ok"}`
- `--max-queue 32` は waiting リクエストの上限。in-flight は常に 1（GPU 直列化）、待ち 32 で頭打ち → 33 本目が `503 + Retry-After`

### engine 側の接続

`embedder_endpoint`（空文字列 = 従来の in-process `RuriEmbedder`）を設定すると、`build_engine` が `RemoteEmbedder` を構築して service に接続する:

- config.json の `embedder_endpoint`: `"http://127.0.0.1:7879"`
- env `GAOTTT_EMBEDDER_ENDPOINT=http://127.0.0.1:7879`（推奨 — MV3 supervisor が子プロセスに渡す経路）。sentinel empty-string default なので generic env loop が拾う
- 接続不能（service が起動していない等）は `ConnectionError` を即 raise（起動時に倒す、silent fail しない）。retry は接続エラーのみ 1 回（0.5s backoff）。encode の HTTP status error / timeout は retry しない

### systemd 雛形

`deploy/gaottt-embedder.service`（repo 同梱）を `/etc/systemd/system/` または `~/.config/systemd/user/` に copy して path を調整:

```bash
# install
sudo cp deploy/gaottt-embedder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gaottt-embedder.service
journalctl -u gaottt-embedder.service -f
```

`User=gaottt` / `WorkingDirectory=/opt/gaottt` / `.venv/bin/python` の path は環境に合わせて書き換える。`Restart=always` で SPOF 落ちても自動復旧。

### default 不変

`embedder_endpoint` を未設定（空文字列）のまま運用すると、engine は従来どおり in-process で `RuriEmbedder` を構築する。service を立てない既存運用は一切変更なし。

### supervisor 経由の lazy spawn（MV3 follow-on、2026-07-06）

[multiverse supervisor](Operations-Multiverse-Setup.md) を使う開発機構成では、embedding service を systemd で常駐させなくても **supervisor が初回 embed 需要時に lazy spawn** する（`supervisor_spawn_embedder=True`、default 有効）。運用者が明示的に `python -m gaottt.embedding.service` を起動する必要がない:

- **流れ**: `create_universe` / `/route` が `ensure_embedder_up()` を呼ぶ → `/healthz` で既存 service を確認 → 応答があれば `unowned`（外部が立てた）扱いで所有権を主張しない → 応答がなければ `fcntl.flock` で cross-process 排他を取って detached spawn → readiness poll → `owned_idle` で稼働開始
- **systemd 運用との両立（完全不変）**: 正常な systemd embedder が先に立っている場合は `/healthz` が応答するので supervisor は spawn せず `unowned` に倒れる。**systemd 運用時の挙動は 1 行も変わらない**。先に立っている方が勝つ（race は `fcntl.flock` で安全）
- **idle で自動終了**: 全 universe backend が休眠し `embedder_spawn_idle_timeout_seconds`（既定 300s）経つと SIGTERM → 5s 待ち → 必要なら SIGKILL で落とす。次回 route で再 spawn
- **opt-out**: `supervisor_spawn_embedder=False` で従来挙動（embedder は必ず手動/systemd 起動、繋がらないと `EmbedderValidationError`）に戻る
- **knob 群と troubleshooting**: [Operations — Tuning](Operations-Tuning.md)「Multiverse supervisor — embedder lazy spawn」節、[Operations — Troubleshooting](Operations-Troubleshooting.md)「supervisor が lazy spawn した embedder が `owned_terminating` に固まる」

> **★ PermissionError / SIGKILL 後 alive は手動 recovery**: 異常終了パスでは state を `unowned` に**消さず** `owned_terminating` を保持し ERROR log で手動 recovery を促す（安全側、auto-respawn しない）。本項目の knob 4 つと state machine 3 状態の詳細は [Tuning](Operations-Tuning.md) §MV3 follow-on を参照。

## モデルダウンロード

初回起動時に RURI-v3-310m（約 1.2GB）が HuggingFace からダウンロードされる。2 回目以降はローカルキャッシュ（`~/.cache/huggingface/hub/`）から即座にロード、HTTP リクエストは発生しない。

## Virtual FAISS (Phase H Stage 4 以降)

2026-05-11 から、`virtual_faiss_enabled=True`（既定）のとき engine は **2 つの FAISS index** を並走させる:

| ファイル | 内容 |
|---|---|
| `gaottt.faiss` | 原始 embedding そのまま (raw FAISS) |
| `gaottt.virtual.faiss` | `virtual_pos = raw + displacement` (Phase G priming 反映) |

- **初回 startup**: `gaottt.virtual.faiss` が disk に存在しなければ、raw + cache.displacement から自動 build される。23k 件規模で数十秒の追加 startup 時間。
- **shutdown**: 両 index が save される。
- **`compact(rebuild_faiss=True)`**: raw rebuild 後に virtual も rebuild。
- **無効化**: `virtual_faiss_enabled=False` で legacy 挙動（raw のみ）。`gaottt.virtual.faiss` ファイルは残るが使われない。
- **マルチプロセス安全性**: raw FAISS と同じ write-behind 周期 (`faiss_save_interval_seconds=5`) で save される。virtual FAISS も `virtual_faiss_save_interval_seconds=60`（既定）で `cache.virtual_faiss_dirty` 検知時に full rebuild + disk save が走る — Phase I/J query attraction で蓄積した displacement が次の compact を待たずに他プロセスの seed pool に伝播する。

bootstrap_report.py の neighbor preview は raw + virtual FAISS の **両方** を並べて表示する (2026-05-13 〜)。displacement で動いた node が raw と virtual で異なる近傍を持つ場合、`Δ:` 行に drift-in / drift-out した id が列挙される。`--no-virtual` で legacy raw-only モード。

## データディレクトリ

| OS | データディレクトリ | 設定ファイル |
|---|---|---|
| Linux/macOS | `~/.local/share/gaottt/` | `~/.config/gaottt/config.json` |
| Windows | `%LOCALAPPDATA%\gaottt\` | `%APPDATA%\gaottt\config.json` |

旧名 GER-RAG 時代の `~/.local/share/ger-rag/` や `~/.config/ger-rag/` に既存データがあれば、`gaottt/config.py` の互換レイヤが自動検出して使用する（deprecation 警告を出力）。移行するなら `scripts/migrate-from-ger-rag.sh` を走らせる。

カスタマイズ:
```bash
export GAOTTT_DATA_DIR=/path/to/data
export GAOTTT_CONFIG=/path/to/config.json
```

### プロジェクトごとに知識ドメインを分けたい場合

「仕事プロジェクト A / B / 研究 で別 DB にしたい」「persona も知識も完全に独立させたい」use case は env var 1 本で実現できるが、**default の proxy mode は port 7878 の backend を共有するので、env だけ分けても初回 spawn 時の env が勝ってしまう** という落とし穴がある。port 分離 + direnv 連携 + 確認手順までは独立ガイドに切り出している:

→ [Guide — Per-Project DBs](Guides-Per-Project-DBs.md)

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

### 投入直後の確認（読み取り専用）

一括投入が終わった直後、**まだ何も `recall` されていない素の状態** を眺めたいときに使う:

```bash
.venv/bin/python scripts/bootstrap_report.py               # 既定（sample=10, k=5）
.venv/bin/python scripts/bootstrap_report.py --sample 20   # 近傍プレビューを多めに
.venv/bin/python scripts/bootstrap_report.py --dup-threshold 0.9  # 軟らかめの重複検出
.venv/bin/python scripts/bootstrap_report.py --no-virtual  # virtual FAISS をロードしない (旧 data dir 向け)
```

3 セクションを出す: (1) summary + source 分布 + **displacement 統計** (min/p50/p90/p99/max、|d|>0.3/1.0/3.0 件数)、(2) 近重複クラスタ（`merge` 候補）、(3) ランダムに選んだノードの FAISS top-K 近傍を **raw + virtual 両方**（**まだ張られていないが、最初の co-recall で結ばれる潜在的エッジ** のプレビュー、および displacement で動いた node の drift を `Δ:` 行で可視化）。

DB への副作用なし、LLM 呼ばない。オンライン書き込み中の MCP サーバーと同時実行しても安全（read-only close で FAISS 再保存もしない）。

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
