# Operations — Multiverse Setup

> MV3 (2026-07-02): universe supervisor + multiverse layout の運用ガイド。

GaOTTT で **1 ホスト上に複数テナントの宇宙を独立 `data_dir` で運用する** 構成。supervisor (port 7880) が宇宙 engine のライフサイクルを管理し、API key でユーザー → 宇宙をルーティングする。詳細: [Plans — Multiverse Scale-Out](Plans-Multiverse-Scale-Out.md) §Stage 2、[multiverse-implementation-plan.md](../maintainers/multiverse-implementation-plan.md) §MV3。

## 概要

| 役割 | port | 備考 |
|---|---|---|
| embedding service (MV1) | 7879 | RURI model をホスト共有、全宇宙がここを見る |
| universe supervisor (MV3) | 7880 | 宇宙の作成 / 一覧 / 削除 + `/route` で API key → 宇宙解決 |
| 宇宙 backend (per-universe) | 7890–7989 | 動的割当。各宇宙 = 1 つの `mcp_server --transport streamable-http` |

- 宇宙は `<multiverse_root>/universes/<universe_id>/` に 1 つ置かれ、それがその宇宙の `GAOTTT_DATA_DIR` になる
- supervisor は初回 route 時に backend を spawn し、idle watchdog (既定 5 分) で自然休眠させる。再 route で respawn する（データは保持）
- 宇宙同士は構造的に不可視（独立 DB / FAISS / cache）

## アーキテクチャ

```
agent (Claude Code / opencode / Codex / ...)
  ↓ stdio
mcp_proxy shim (--supervisor-url http://127.0.0.1:7880)
  ↓ POST /route {api_key}        ← supervisor が key → universe 解決 + backend ensure
  ↓ 返る {url, token}
universe backend (http://127.0.0.1:<port>/mcp, GAOTTT_BACKEND_TOKEN 必須)
  ↓
embedding service (port 7879, MV1)
```

- **shim は自前 spawn しない**: `--supervisor-url` 指定時は `/route` 経由で接続先を解決するだけ（未指定時は従来の 7878 auto-spawn 経路、default 不変）
- **backend は token 保護**: supervisor が spawn 時に `secrets.token_urlsafe(32)` で生成し、`GAOTTT_BACKEND_TOKEN` env で注入。`mcp_server` の streamable-http middleware が `Authorization: Bearer` を検証（env 未設定 = 素通し、既存単一 backend は無影響）
- **lease 強制**: supervisor が作る宇宙は `manifest.managed=True` なので、[owner lease (MV2)](Operations-Tuning.md#multiverse-owner-lease-mv2--2026-07-02) が config に関係なく強制される

## 前提

- [MV0 (manifest)](Operations-Tuning.md#multiverse-manifest-mv0--2026-07-02) / [MV1 (embedding service)](Operations-Server-Setup.md#embedding-service-を分離するmultiverse-mv1--2026-07-02) / [MV2 (owner lease)](Operations-Tuning.md#multiverse-owner-lease-mv2--2026-07-02) が完了していること
- embedding service が `127.0.0.1:7879` で稼働中であること（[起動手順](Operations-Server-Setup.md#embedding-service-を分離するmultiverse-mv1--2026-07-02)）
- **専用 OS ユーザー推奨** — `<multiverse_root>` を他ユーザーから読み書き不可にする信頼境界（下記「セキュリティモデル」）
- local filesystem のみ（NFS / CIFS は lease の POSIX semantics が保証できない → [制限事項](#制限事項-v1))

## セットアップ手順

### 1. multiverse root の作成

```bash
# 推奨: ~/.local/share/gaottt-multiverse（default は空 = 機能不使用、明示的に設定する必要がある）
mkdir -p ~/.local/share/gaottt-multiverse
chmod 0700 ~/.local/share/gaottt-multiverse
```

supervisor は起動時の lifespan で `multiverse_root` を 0700 に chmod するので、事前作成しなくても良い。ただし親ディレクトリの permission は呼び出し側の責任。

### 2. supervisor の起動

```bash
GAOTTT_MULTIVERSE_ROOT=~/.local/share/gaottt-multiverse \
GAOTTT_SUPERVISOR_ADMIN_KEY="<your-admin-key>" \
GAOTTT_EMBEDDER_ENDPOINT=http://127.0.0.1:7879 \
/path/to/GaOTTT/.venv/bin/python -m gaottt.multiverse.supervisor
```

- `GAOTTT_SUPERVISOR_ADMIN_KEY` は **必須**（空 = 起動 fail-fast、unauthenticated admin を絶対に露出しない）。config file に書かず環境変数で渡す
- `GAOTTT_EMBEDDER_ENDPOINT` は宇宙作成時の embedder 検証 (`GET /info`) と、spawn する backend の `GAOTTT_EMBEDDER_ENDPOINT` に使う
- `GAOTTT_MULTIVERSE_ROOT` は multiverse の root directory（未設定 = 機能不使用）
- CLI 引数は `--host` / `--port` のみ（default: `127.0.0.1:7880`）。上記 3 つの値は **環境変数** 経由で `GaOTTTConfig.from_config_file()` が読む
- localhost bind のみ（`--host` は `127.0.0.1` / `localhost` / `::1` のいずれか）。外部公開は前段に認証付き reverse proxy を置く

systemd 運用する場合は `deploy/gaottt-embedder.service` と同パターンの unit を作る（`Restart=always`、env に admin key）。

### 3. 宇宙の作成

```bash
curl -X POST http://127.0.0.1:7880/admin/universes \
    -H "X-Admin-Key: $GAOTTT_SUPERVISOR_ADMIN_KEY" \
    -H "Content-Type: application/json" \
    -d '{"owner_label": "user1"}'
```

応答:

```json
{
  "universe_id": "a1b2c3d4e5f6",
  "api_key": "<平文キー — 発行時に一度だけ返る>",
  "port": 7890
}
```

- `embedder_id` は省略可能（省略時は `GAOTTT_EMBEDDER_ENDPOINT` の embedding service が使う model になる）。指定した場合も supervisor は embedding service の `/info` の `model_name` を権威値として manifest に記録する（body.embedder_id は forward-compat 用で現在は無視される）。`/info` が取れなければ **400**
- `api_key` の平文は **この応答で一度だけ** 返る。registry には SHA-256 hash で保存される（`hashlib.sha256`、CSPRNG `secrets.token_urlsafe(32)` で生成）。紛失した場合は再発行 API（または宇宙再作成）
- 作成された宇宙の `manifest.json` は `managed: true`（MV2 lease 強制のトリガー）

### 4. agent 側の shim 設定

`.mcp.json` (Claude Code) の場合:

```json
{
  "mcpServers": {
    "gaottt": {
      "command": "/path/to/GaOTTT/.venv/bin/python",
      "args": [
        "-m", "gaottt.server.mcp_server",
        "--transport", "proxy",
        "--supervisor-url", "http://127.0.0.1:7880"
      ],
      "env": {
        "GAOTTT_API_KEY": "<api-key>"
      },
      "cwd": "/path/to/GaOTTT"
    }
  }
}
```

- `--supervisor-url` 指定で shim は `/route {api_key}` を呼び、返った `{url, token}` で backend に接続する（自前 spawn しない）
- 接続断 → `/route` 再取得 → 再接続（backend が休眠していたら supervisor が respawn）
- `GAOTTT_API_KEY` を間違えると `/route` が 401 を返す（[トラブルシューティング](#トラブルシューティング)）

opencode / Codex CLI も同じ `--supervisor-url` + `GAOTTT_API_KEY` で動く（[Server Setup](Operations-Server-Setup.md) の各クライアント節を参照、args に `--supervisor-url` を追加するだけ）。

### 5. 動作確認

```bash
# supervisor の稼働
curl -sf http://127.0.0.1:7880/admin/universes \
    -H "X-Admin-Key: $GAOTTT_SUPERVISOR_ADMIN_KEY"

# 宇宙への route（api_key が正しければ url + token が返る）
curl -sf -X POST http://127.0.0.1:7880/route \
    -H "Content-Type: application/json" \
    -d '{"api_key": "<api-key>"}'
# → {"url": "http://127.0.0.1:7890/mcp", "token": "..."}

# backend が token で保護されていること（token なしは 401）
curl -i http://127.0.0.1:7890/mcp
# → HTTP/1.1 401 Unauthorized
```

agent からは通常どおり `remember` / `recall` が使える。他テナントの宇宙には干渉しない（acceptance: 宇宙 A の `remember` が宇宙 B の `recall` に出ない）。

### 6. 宇宙の削除

```bash
curl -X DELETE http://127.0.0.1:7880/admin/universes/<universe_id> \
    -H "X-Admin-Key: $GAOTTT_SUPERVISOR_ADMIN_KEY"
```

- supervisor が backend 停止を確認（port probe で応答しなくなるまで待つ、timeout 付き）してからディレクトリを `<multiverse_root>/trash/` へ move（即時物理削除しない、猶予付き）
- port は解放され、別宇宙の割当に再利用可能

## セキュリティモデル

| asset | permission | 備考 |
|---|---|---|
| `<multiverse_root>` | 0700 | supervisor が lifespan で chmod |
| `<universe_dir>/manifest.json` | 0600 | MV0、managed flag を含む |
| `<universe_dir>/owner.lock` | 0600 | MV2、`O_CREAT\|O_EXCL` + `fcntl.flock` で原子取得 |
| `<universe_dir>/backend.token` | 0600 | supervisor が spawn 時に生成、再起動時に読み戻し |

- **backend token** (`GAOTTT_BACKEND_TOKEN` env) の有無が backend の token 検証 ON/OFF を決める（boolean knob は作らない — 「token 設定済みだが middleware 無効」の dangerous state を防ぐ）。supervisor が spawn 時にこの env を注入する
- **admin key** は環境変数推奨（config file に書かない）。空 = supervisor 起動 fail-fast
- **localhost bind のみ**: supervisor / embedding service / 各宇宙 backend 全て。外部公開する場合は前段に認証付き reverse proxy (Caddy / nginx / Cloudflare Access 等) + TLS、または VPN / SSH tunnel
- **全認証比較は `secrets.compare_digest`** (admin key / API key / backend token) — timing attack 防御
- **401 応答は idle activity を refresh しない** — 認証前のリクエストで watchdog timer をリセットすると、brute-force 攻撃が idle shutdown を妨げる

## 制限事項 (v1)

- **100 宇宙 / ホスト上限** — port range 7890–7989 (100 本) が上限。枯渇すると宇宙作成が 503
- **REST 経路の宇宙提供なし** — managed 宇宙は owner lease で二重 engine を構造的に拒否するため REST app (`build_engine()` で独自 engine を立てる) が開けない。v1 で宇宙に露出する経路は MCP のみ。テナント管理者の操作は supervisor admin API で提供
- **NFS / CIFS 非サポート** — lease 機構（`O_EXCL` / `fcntl.flock` / `os.replace`）が POSIX semantics に依存するため、local FS のみ
- **同一 OS ユーザー内の manifest 改変は信頼境界外** — root / 同一ユーザーを敵とするモデルは v1 で守らない。`managed=false` への書き換え（lease 解除）は runbook の復旧手順としてのみ案内（supervisor 停止 + 対象宇宙 backend 停止が前提）

## トラブルシューティング

| 症状 | 原因 / 対処 |
|---|---|
| `401 Unauthorized` on `/route` | API key が不正、または revoke 済み。宇宙一覧 (`GET /admin/universes`) で確認、必要なら再発行 |
| `401 Unauthorized` on backend `/mcp` (token なし直叩き) | 期待どおり。token は `/route` 経由で取得した shim のみが持つ。supervisor 再起動後は `backend.token` を読み戻して既存 backend への route を継続（probe 401 → token reread → 再 probe、それでも 401 なら re-spawn） |
| `LeaseHeldError` / `LeaseLostError` | [Operations — Troubleshooting](Operations-Troubleshooting.md)「LeaseHeldError / LeaseLostError が出る」節参照。managed 宇宙では lease 強制なので `--force-takeover` は runbook 専用 |
| `503` on 宇宙作成 | port range (7890–7989) 枯渇。不要宇宙を `DELETE /admin/universes/{id}` で削除 |
| `400` on 宇宙作成 | embedder 検証失敗 (`GET /info` で `model_name` / `dimension` が取れない)。embedding service が起動しているか、`embedder_id` が service の model と一致しているか確認 |
| backend が idle で落ちた | 期待どおり（既定 5 分）。再 route で supervisor が respawn、データは保持 |
| supervisor 再起動後に既存 backend に繋がらない | `backend.token` の読み戻しが走る。disk 上の token file (0600) が健全か確認。それでも 401 なら re-spawnされるはず（supervisor log を見る） |
| admin endpoint に空キーで起動してしまった | 起動しない（fail-fast）。`GAOTTT_SUPERVISOR_ADMIN_KEY` を設定して再起動 |

→ より詳細: [Operations — Troubleshooting](Operations-Troubleshooting.md)、[Operations — Tuning](Operations-Tuning.md)「Multiverse supervisor (MV3)」節
