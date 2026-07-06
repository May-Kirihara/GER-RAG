# Multiverse 移行チェックリスト（クイック版）

> 作成: 2026-07-06
> 目的: 既存の standalone 運用（`~/.local/share/gaottt/` 単一ユーザー）を multiverse layout の 1 宇宙へ移行する手順を、最小限のコマンドで端的に示す。
> 所要時間の目安: 約 10 分（＋動作検証）。
>
> **このメモはクイック版です。** 理屈・前提条件の詳細・トラブル対応の全文は以下の公式マニュアル（SoT）を参照してください:
> - [Operations — Multiverse Setup](../wiki/Operations-Multiverse-Setup.md)（MV3 supervisor / 宇宙作成 / 削除 / セキュリティモデル）
> - [Operations — Multiverse Import Universe](../wiki/Operations-Multiverse-Import-Universe.md)（importer の完全仕様 / exit code / rollback 手順）

---

## ▶ Step 0: 前提（まだ standalone を使っている状態）
- [ ] GaOTTT リポジトリが multiverse 対応版（MV0–MV3 入り）
- [ ] **embedding service を port 7879 で起動**（全宇宙がここを見る。SPOF なので **systemd 常駐を強く推奨**）

  systemd 雛形を install（推奨 — OS 再起動で自動起動、クラッシュ時も自動復旧）:
  ```bash
  sudo cp deploy/gaottt-embedder.service /etc/systemd/system/
  # User= / WorkingDirectory= / .venv の path を環境に合わせて編集
  sudo systemctl daemon-reload
  sudo systemctl enable --now gaottt-embedder.service
  journalctl -u gaottt-embedder.service -f   # "Application startup complete." で起動確認
  ```

  または手動起動（テスト用途 — ターミナルを閉じると落ちる）:
  ```bash
  /path/to/GaOTTT/.venv/bin/python -m gaottt.embedding.service \
      --host 127.0.0.1 --port 7879
  ```

  > **注意**: multiverse supervisor は embedding service を **自動起動しません**。
  > service が落ちていると宇宙作成が `400`、`/route` が `503` で失敗します（[Tuning §MV1](../wiki/Operations-Tuning.md#multiverse-embedding-service-mv1--2026-07-02)、[Server Setup §systemd 雛形](../wiki/Operations-Server-Setup.md#systemd-雛形)）。

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
# (1) embedding service が生きているか（"ok" が返れば OK）
curl -sf http://127.0.0.1:7879/healthz

# (2) /route で spawned backend の url+token が返るか
curl -sf -X POST http://127.0.0.1:7880/route \
    -H "Content-Type: application/json" \
    -d '{"api_key": "<api-key>"}'
# → {"url": "http://127.0.0.1:7890/mcp", "token": "..."}
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
| 宇宙作成が `400` / `/route` が `503` | embedding service（port 7879）が落ちている。`systemctl status gaottt-embedder` で確認・再起動 |
| import `exit 3`（source backend 起動中） | Step 1 のプロセス停止漏れ。`--force` は**非推奨** |
| import `exit 4`（embedder unreachable / identity mismatch） | embedding service の起動確認、または `--embedder-id` で override |
| import `exit 5`（WAL サイズ超過） | source を正常 shutdown して SQLite checkpoint を待つ |
| `/route` が 401 | API key 不正・revoke 済み。宇宙一覧で確認 or 再発行 |
| supervisor が起動しない | `GAOTTT_SUPERVISOR_ADMIN_KEY` が空（fail-fast） |
| import 後に recall 結果が変わる | **異常**。semantic drift / corruption 疑い → `diag_recall.py` で比較 |

詳細は [Multiverse Setup](../wiki/Operations-Multiverse-Setup.md) / [Import Universe](../wiki/Operations-Multiverse-Import-Universe.md) のトラブルシューティング表へ。
