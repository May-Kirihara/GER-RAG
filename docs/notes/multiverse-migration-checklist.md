# Multiverse 移行チェックリスト（クイック版）

> 作成: 2026-07-06 / 最終更新: 2026-07-08（embedder lazy spawn 完了反映）
> 目的: 既存の standalone 運用（`~/.local/share/gaottt/` 単一ユーザー）を multiverse layout の 1 宇宙へ移行する手順を、最小限のコマンドで端的に示す。
> 所要時間の目安: 開発機なら約 5 分、本番 systemd 運用なら +5 分（embedder 設定）。
>
> **このメモはクイック版です。** 理屈・前提条件の詳細・トラブル対応の全文は以下の公式マニュアル（SoT）を参照してください:
> - [Operations — Multiverse Setup](../wiki/Operations-Multiverse-Setup.md)（MV3 supervisor / 宇宙作成 / 削除 / セキュリティモデル）
> - [Operations — Multiverse Import Universe](../wiki/Operations-Multiverse-Import-Universe.md)（importer の完全仕様 / exit code / rollback 手順）
> - [Operations — Tuning §MV3 follow-on](../wiki/Operations-Tuning.md#multiverse-supervisor--embedder-lazy-spawn-mv3-follow-on2026-07-06)（lazy spawn の config knob と設計判断）

---

## ▶ Step 0: 前提（まだ standalone を使っている状態）

- [ ] GaOTTT リポジトリが multiverse 対応版（MV0–MV3 + embedder lazy spawn 入り）

### 開発機（systemd 無し・推奨） — **embedding service の事前起動は不要**

multiverse supervisor が初回の `create_universe` / `/route` で embedding service を **lazy spawn** します（`supervisor_spawn_embedder=True`、default 有効）。Step 4 で `GAOTTT_EMBEDDER_ENDPOINT` を設定するだけで、あとは supervisor が全自動で面倒を見ます。

- 全 backend が idle で落ちたら `embedder_spawn_idle_timeout_seconds`（default 300s）後に embedder も SIGTERM で終了
- 次回リクエストで再 spawn
- opt-out する場合のみ `GAOTTT_SUPERVISOR_SPAWN_EMBEDDER=0` を設定

> ⚠️ **lazy spawn 発動には endpoint 設定が必須**（B-F4）: `GAOTTT_EMBEDDER_ENDPOINT` が空文字列（default）だと lazy spawn は発動せず、従来通り in-process RuriEmbedder を使います。multiverse 運用では必ず endpoint を設定してください。

### 本番運用（systemd 常駐・任意）

systemd で embedding service を常駐させる場合は、supervisor は `/healthz` で「既に立ってる」を検知して lazy spawn せず `unowned` 扱い（**干渉ゼロ・両立可能**）。

systemd 雛形を install（OS 再起動で自動起動、クラッシュ時も自動復旧）:
```bash
sudo cp deploy/gaottt-embedder.service /etc/systemd/system/
# User= / WorkingDirectory= / .venv の path を環境に合わせて編集
sudo systemctl daemon-reload
sudo systemctl enable --now gaottt-embedder.service
journalctl -u gaottt-embedder.service -f   # "Application startup complete." で起動確認
```

## ▶ Step 1: 既存プロセスを**全部止める**（★一番重要）
write-behind の逆方向上書き罠（[Troubleshooting §5.5](../wiki/Operations-Troubleshooting.md)）を防ぐため、source を触る全プロセスを停止:
```bash
pkill -f 'gaottt.server.mcp_server'
pkill -f 'gaottt.server.app'
ps -ef | grep "gaottt.server.mcp_server.*streamable-http" | grep -v grep   # 残りが居たら kill
```

## ▶ Step 2: dry-run で移行計画を確認（副作用ゼロ）
```bash
.venv/bin/python scripts/import_universe.py \
    --source ~/.local/share/gaottt \
    --owner-label "main" \
    --multiverse-root ~/.local/share/gaottt-multiverse \
    --dry-run --yes
```
→ copy 対象 7 file / 合計サイズ / target uid を確認

## ▶ Step 3: 本番移行（**copy モード推奨** — source 無傷で rollback 容易）
```bash
.venv/bin/python scripts/import_universe.py \
    --source ~/.local/share/gaottt \
    --owner-label "main" \
    --multiverse-root ~/.local/share/gaottt-multiverse \
    --yes
```
> ⚠️ **`api_key` 平文は一度だけ表示される — すぐ安全な場所に記録**

## ▶ Step 4: supervisor を起動（admin key 必須・空は起動しない）

```bash
GAOTTT_MULTIVERSE_ROOT=~/.local/share/gaottt-multiverse \
GAOTTT_SUPERVISOR_ADMIN_KEY="<your-admin-key>" \
GAOTTT_EMBEDDER_ENDPOINT=http://127.0.0.1:7879 \
.venv/bin/python -m gaottt.multiverse.supervisor &
```

> ★ **`GAOTTT_EMBEDDER_ENDPOINT` は必須**: これが空だと lazy spawn が発動しません（B-F4）。multiverse 運用では必ず設定してください。
>
> ★ **開発機ではこれだけで embedder も立ちます**: supervisor が初回リクエストで lazy spawn します。systemd 運用の時だけ Step 0 で事前起動してください。

## ▶ Step 5: agent の config を supervisor 経由に切り替え

**Claude Code (`.mcp.json`)**:
```json
{
  "mcpServers": {
    "gaottt": {
      "command": "/path/to/GaOTTT/.venv/bin/python",
      "args": ["-m", "gaottt.server.mcp_server",
               "--transport", "proxy",
               "--supervisor-url", "http://127.0.0.1:7880"],
      "env": { "GAOTTT_API_KEY": "<step 3 の api_key>" },
      "cwd": "/path/to/GaOTTT"
    }
  }
}
```
**opencode / Codex CLI** も同じ `--supervisor-url` + `GAOTTT_API_KEY` を追加するだけ。

## ▶ Step 6: 動作確認

```bash
# (1) /route で spawned backend の url+token が返るか
curl -sf -X POST http://127.0.0.1:7880/route \
    -H "Content-Type: application/json" \
    -d '{"api_key": "<api-key>"}'
# → {"url": "http://127.0.0.1:7890/mcp", "token": "..."}
```

**開発機（lazy spawn）の場合は追加確認**:
```bash
# (2) supervisor が embedder を lazy spawn したか確認
ps -ef | grep "gaottt.embedding.service" | grep -v grep
# → supervisor 起源の embedder プロセスが 1 つ居るはず（初回 /route 後）

# (3) embedder の /healthz が応答するか
curl -sf http://127.0.0.1:7879/healthz   # → "ok"
```

→ agent を再起動し、`remember` / `recall` が通ることを確認

## ▶ Step 7: 旧データを退避（**十分に検証してから**・copy mode のみ）
```bash
mv ~/.local/share/gaottt ~/.local/share/gaottt.pre-import-$(date +%Y%m%d)
```

---

## 🚑 トラブル時の引っ掛かりポイント

| 症状 | 対処 |
|---|---|
| 宇宙作成が `400` / `/route` が `503` | embedding service が落ちている。開発機なら supervisor が lazy spawn するはず（`GAOTTT_EMBEDDER_ENDPOINT` 設定を確認）、本番なら `systemctl status gaottt-embedder` で確認・再起動 |
| `/route` が `503` で "Embedder validation failed" | `GAOTTT_EMBEDDER_ENDPOINT` が未設定（B-F4: lazy spawn 発動せず）。Step 4 で endpoint を設定して supervisor を再起動 |
| supervisor log に `owned_terminating` の ERROR が続く | supervisor が lazy spawn した embedder を SIGTERM/SIGKILL でも殺せなかった状態（他 uid 所有・SIGKILL survived）。**手動 recovery 必須**: `kill -9 <pid>` して supervisor を再起動。詳細: [Troubleshooting §owned_terminating](../wiki/Operations-Troubleshooting.md) |
| import `exit 3`（source backend 起動中） | Step 1 のプロセス停止漏れ。`--force` は**非推奨** |
| import `exit 4`（embedder unreachable / identity mismatch） | embedding service の起動確認、または `--embedder-id` で override |
| import `exit 5`（WAL サイズ超過） | source を正常 shutdown して SQLite checkpoint を待つ |
| `/route` が 401 | API key 不正・revoke 済み。宇宙一覧で確認 or 再発行 |
| supervisor が起動しない | `GAOTTT_SUPERVISOR_ADMIN_KEY` が空（fail-fast） |
| lazy spawn した embedder が readiness timeout（90s）で失敗 | 初回 RURI model download（~1.2GB）で超過し得る。2 回目以降は cache が効いて通る。継続する場合は `GAOTTT_EMBEDDER_SPAWN_READINESS_TIMEOUT_SECONDS` を長めに |
| import 後に recall 結果が変わる | **異常**。semantic drift / corruption 疑い → `diag_recall.py` で比較 |

詳細は [Multiverse Setup](../wiki/Operations-Multiverse-Setup.md) / [Import Universe](../wiki/Operations-Multiverse-Import-Universe.md) / [Troubleshooting](../wiki/Operations-Troubleshooting.md) のトラブルシューティング表へ。

---

## 設計の要点（lazy spawn by supervisor）

- **3 状態 state machine**: `unowned`（外部が立てた）/ `owned_idle`（supervisor が spawn した稼働中）/ `owned_terminating`（SIGTERM/SIGKILL 中・手動 recovery 待ち）
- **race safety 2 層**: `asyncio.Lock`（in-process）+ `<multiverse_root>/.embedder.spawn.lock` の `fcntl.flock`（cross-process）
- **B-F4**: `embedder_endpoint=""`（default）なら lazy spawn しない（feature inert）。multiverse 運用では endpoint 必須
- **B2-6**: PermissionError / SIGKILL 後 alive は state を `unowned` に消さず `owned_terminating` を保持、ERROR log で手動 recovery を促す（auto-respawn しない安全側）
- **opt-out**: `GAOTTT_SUPERVISOR_SPAWN_EMBEDDER=0` で従来挙動（embedder は必ず外部/systemd で立てる）

設計判断の経緯は [Plans-Multiverse-Scale-Out.md §SPOF](../wiki/Plans-Multiverse-Scale-Out.md) と [docs/plans/embedder-auto-spawn-supervisor.md](../plans/embedder-auto-spawn-supervisor.md) へ。
