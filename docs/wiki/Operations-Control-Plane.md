# Operations — Control Plane (MV4)

> MV4 (2026-07-03): Postgres-backed control plane の運用ガイド。テナント・宇宙の台帳 / 監査 / usage telemetry 収集点。
> 前提: [MV0–MV3](Operations-Multiverse-Setup.md) が完了していること。

## 概要

control plane は Multiverse デプロイの **aggregator / audit / billing 収集点** である。`gaottt/` engine パッケージとは **完全に独立したパッケージ** (`control/`) で、`asyncpg` + FastAPI + Postgres で構築され、同一ホスト内の独立プロセスとして動作する。engine コード (`gaottt/core/`、physics / observation 層) には **一切接触しない** — `gaottt/` パッケージは `asyncpg` を import しない (設計判断 J9)。

役割:

- **台帳** — テナント・ユーザー・ホスト・宇宙のメタデータを Postgres に保持
- **監査** — 全ての mutating endpoint が同一 DB トランザクションで `audit_log` に記録 (設計判断 J12)
- **usage telemetry 収集** — supervisor が `/route` 解決回数等を batch push (詳細は下記「usage telemetry の意味 (J1=A)」節)

> **★ 権限モデル (J5 — local 一次)**: control plane は aggregator / audit / billing 収集点であり、**運用 deprovisioning の権威ではない**。宇宙の状態の一次は local manifest / registry。control 側の `DELETE /admin/universes/{uid}` は台帳 row の論理削除 (`status='deleted'`) のみで、supervisor は次回 sync で矛盾を検知して **WARNING** を出す (即時削除しない)。operator が手動で対処する。

## アーキテクチャ

```
┌─────────────────────── 同一ホスト (localhost 通信のみ) ───────────────────────┐
│                                                                              │
│  supervisor (port 7880)          control plane (port 7881)                   │
│  ┌──────────────────┐            ┌─────────────────────┐    ┌──────────────┐ │
│  │ ControlClient    │ ── HTTP ─> │ FastAPI app         │ -> │ Postgres 16  │ │
│  │  - pull (sync)   │  (httpx)   │  - admin API        │    │  (7 tables   │ │
│  │  - push (usage)  │ <── HTTP ─ │  - host token API   │    │   + audit)   │ │
│  │  - spool (disk)  │            │  - audit_log (J12)  │    └──────────────┘ │
│  └──────────────────┘            └─────────────────────┘                     │
│         │                                                                     │
│         v                                                                     │
│  宇宙 backends (7890-7989)                                                    │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

- supervisor と control plane は **同じホスト内の別プロセス** (設計判断 J8)。network 越しの通信は v1 範囲外
- 信頼境界は **localhost のみ**: `CONTROL_LISTEN_HOST` に非 localhost を指定すると `SystemExit` で拒否される
- `control/` は `gaottt/` を import しない (J9)。supervisor 側の `gaottt/multiverse/control_client.py` は `httpx` のみ使い、`asyncpg` を import しない

## 前提

- **Postgres 16+** (開発/テストは `control/compose.yml` の disposable、本番は外部 Postgres)
- **`asyncpg` / `fastapi` / `uvicorn` / `pydantic`** (`control/pyproject.toml`、`gaottt` 非依存)
- [MV0–MV3](Operations-Multiverse-Setup.md) 完了 (supervisor が稼働済みであること)
- control plane と supervisor が **同一ホスト** で動くこと (J8)

## setup 手順

### 1. Postgres を用意する

**開発 / テスト (disposable)**:

```bash
docker compose -f control/compose.yml up -d
```

- image: `postgres:16-alpine`
- DB / user / password: `gaottt_control` / `gaottt` / `dev-only`
- host port: `55432` (5432 衝突回避)
- **volume なし** — `down` で全データ消失 (disposable 設計)

DSN:

```
postgresql://gaottt:dev-only@127.0.0.1:55432/gaottt_control
```

**本番**: 外部の Postgres 16+ インスタンスを用意し、DSN / 認証情報を `CONTROL_DATABASE_URL` に設定する。

### 2. migration を適用する

```bash
export CONTROL_DATABASE_URL=postgresql://gaottt:dev-only@127.0.0.1:55432/gaottt_control
python -m control.migrate
```

migration runner (`control/control/migrate.py`) の挙動:

1. `ensure_bootstrap()` が `schema_migrations` テーブルを `CREATE TABLE IF NOT EXISTS` で **番号付きファイル scan より前に** 作成 (chicken-and-egg 回避、設計判断 J4)
2. `schema/NNN_*.sql` を番号順に列挙し、未適用を apply
3. **1 ファイル = 1 トランザクション** — ファイルの SQL と `schema_migrations` INSERT が一緒に COMMIT され、失敗時はファイル全体が ROLLBACK (partial apply なし、atomicity 保証)
4. 冪等 — 既適用は skip

> `schema_migrations` は `001_initial.sql` の中には定義しない (runner のみが bootstrap する)。
> WP-2 の FastAPI lifespan が起動時に `ensure_bootstrap` + `run_migrations` を自動実行するので、初回 setup や debug 以外は `python -m control.migrate` を手動で叩く必要はない。

`001_initial.sql` が作成する 7 domain テーブル (設計判断 J1 / Codex review #2 反映):

| テーブル | 役割 |
|---|---|
| `tenants` | tenant 台帳。`tenant_id='default'` を bootstrap INSERT (FK 解決、J11) |
| `users` | user 台帳 (tenant FK) |
| `hosts` | host 台帳。`token_hash` (SHA-256) を保存、平文 token は持たない (J3) |
| `universes` | 宇宙台帳 (tenant / host / embedder FK) |
| `usage_batches` | usage batch の冪等性単位 (`batch_id` UUID PK、Codex B4) |
| `usage_events` | usage 明細 (`tenant_id` 非正規化、Codex B1) |
| `audit_log` | 監査ログ (全 mutating endpoint が同一トランザクションで INSERT、J12) |

### 3. control plane を起動する

control plane は `ControlConfig.from_env()` で環境変数を読み、FastAPI app を構築して uvicorn で起動する:

```bash
export CONTROL_DATABASE_URL=postgresql://gaottt:dev-only@127.0.0.1:55432/gaottt_control
export CONTROL_ADMIN_KEY="<強い乱数>"
python -m control
```

lifespan が `create_pool` → `ensure_bootstrap` → `run_migrations` → サービス開始 → (shutdown 時) `close_pool` を順に実行する。migration / pool 失敗は伝播し、uvicorn は壊れた DB に対してサービスを開始しない。

### ControlConfig 環境変数

| env var | field | default | 備考 |
|---|---|---|---|
| `CONTROL_DATABASE_URL` | `database_url` | (空) | Postgres DSN。WP-2 で空 = fail-fast |
| `CONTROL_ADMIN_KEY` | `admin_key` | (空) | admin API key。**空 = `create_app` が `RuntimeError`** (fail-fast) |
| `CONTROL_LISTEN_HOST` | `listen_host` | `127.0.0.1` | **非 localhost = `SystemExit`** (J8、loopback 信頼境界) |
| `CONTROL_LISTEN_PORT` | `listen_port` | `7881` | J2 (7878=単一 backend / 7879=embedding / 7880=supervisor の続き) |
| `CONTROL_DB_POOL_MIN_SIZE` | `db_pool_min_size` | `2` | asyncpg pool 最小接続数 (J6) |
| `CONTROL_DB_POOL_MAX_SIZE` | `db_pool_max_size` | `10` | asyncpg pool 最大接続数 (J6) |
| `CONTROL_SCHEMA_DIR` | `schema_dir` | bundled `schema/` | migration SQL ディレクトリ (test override 用) |

### 4. host を登録する

control plane に supervisor 用の host を登録し、平文 token を一度だけ受け取る (J3):

```bash
curl -X POST http://127.0.0.1:7881/admin/hosts \
  -H "X-Admin-Key: $CONTROL_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"label": "prod-host-1"}'
# -> {"host_id": "<hid>", "label": "prod-host-1", "token": "<平文 token — 一度だけ表示>"}
```

- 平文 token は **このレスポンスでのみ返る**。DB には SHA-256 hash (`token_hash`) のみ保存される
- token は高エントロピー (`secrets.token_urlsafe(32)`) なので、認証は hash 一致で検証する (`compare_digest` ではなく DB parameterized lookup、Codex non-blocking #3)
- `DELETE /admin/hosts/{hid}` で revoke (`revoked_at` 設定、即時無効化)

### 5. supervisor を control plane 連携させる

supervisor 起動時に **3 点セット** の env を設定する (3-point gate):

```bash
GAOTTT_CONTROL_PLANE_URL=http://127.0.0.1:7881 \
GAOTTT_CONTROL_HOST_ID=<hid> \
GAOTTT_CONTROL_HOST_TOKEN=<平文 token> \
GAOTTT_MULTIVERSE_ROOT=~/.local/share/gaottt-multiverse \
GAOTTT_SUPERVISOR_ADMIN_KEY=<supervisor admin key> \
/path/to/GaOTTT/.venv/bin/python -m gaottt.multiverse.supervisor
```

3 点とも設定された場合のみ `ControlClient` が構築され、supervisor の lifespan が `control_client.start()` を呼ぶ。**1 つでも欠けたら feature inert** (supervisor は従来通り local-only、default 不変、MV3 完全一致)。詳細は [Operations — Tuning](Operations-Tuning.md)「Multiverse control plane client (MV4)」節。

## API リファレンス

全ての text field は asyncpg parameterized query (`$1`, `$2`...) で bind され、SQL injection を防ぐ (Codex missing #10)。

| endpoint | method | auth | 動作 |
|---|---|---|---|
| `/health` | GET | なし | liveness probe。DB 接続確認、接続 NG は 503 |
| `/admin/tenants` | POST | admin key | tenant 作成、`tenant_id` 発行。同一 txn で `audit_log` (J12) |
| `/admin/tenants` | GET | admin key | tenant 一覧 |
| `/admin/tenants/{tid}/users` | POST | admin key | user 作成。未知 tenant は 404。同一 txn で `audit_log` |
| `/admin/hosts` | POST | admin key | host 登録。**平文 token を一度だけ返す** (J3)。同一 txn で `audit_log` |
| `/admin/hosts/{hid}` | DELETE | admin key | host revoke (`revoked_at` 設定、token 無効化)。同一 txn で `audit_log` |
| `/admin/hosts/{hid}/rotate-token` | POST | admin key | host token rotation + revoke 解除を **同一 host_id で** 原子的に行う (Codex B1)。**平文 token を一度だけ返す** (J3)。同一 txn で `audit_log` (`action='host_token_rotated'`)。未知 host は 404 |
| `/admin/universes` | POST | admin key | 宇宙登録 (supervisor が作った宇宙を台帳に反映)。FK 違反は 400。同一 txn で `audit_log` |
| `/admin/universes/{uid}` | DELETE | admin key | 宇宙の **台帳 row 論理削除** (`status='deleted'`)。**運用 deprovisioning ではない** (J5)。同一 txn で `audit_log` |
| `/admin/universes` | GET | admin key | 全宇宙一覧 |
| `/hosts/{hid}/universes` | GET | host token | 自ホストの active 宇宙一覧 (supervisor の pull 用)。token の host_id ≠ path `hid` は 403 |
| `/hosts/{hid}/usage` | POST | host token | **idempotent な usage batch 受領** (`batch_id` で重複検知、Codex B4)。同一 txn で `usage_batches` INSERT → `usage_events` INSERT → `audit_log` (FK 順序、review #2) |
| `/hosts/{hid}/sync` | POST | host token | local state 報告。control 側で矛盾を `audit_log` に記録 (自動修正しない、J5) |

### 認証

- **admin key**: `X-Admin-Key` or `Authorization: Bearer` ヘッダ。`secrets.compare_digest` で検証 (MV3 supervisor と同パターン)。**空 = fail-fast** (`create_app` で `RuntimeError`)
- **host token**: `Authorization: Bearer` ヘッダ。SHA-256 hash を計算 → DB parameterized lookup (`SELECT host_id FROM hosts WHERE token_hash=$1 AND revoked_at IS NULL`)。平文 token は DB に無いので hash 一致で検証 (Codex non-blocking #3)。token の `host_id` ≠ path の `hid` は 403

## usage telemetry の意味 (J1=A — PM 承認済み SoT deviation)

> **★ 重要**: v1 の usage telemetry は **`/route` 解決回数を activity telemetry として集計** したものであり、**recall / remember / ingest の正確な operation count ではない**。

[implementation-plan §MV4](../maintainers/multiverse-implementation-plan.md) の SoT は usage counter を「recall / remember / ingest 回数」と指定していた。しかし `engine.py` への usage counter 機構追加は **high-risk** (engine 横断ルール 1「physics 層に触らない」と衝突) なので、**PM 承認済み (A) の SoT deviation として v1 は `/route` 解決回数に置き換えた**。

`usage_events.event_type` は **原形** の分類名 (operation count ではないことを命名で明示):

| `event_type` | 意味 | 過小カウントの要因 |
|---|---|---|
| `route_resolution` | supervisor の `/route` 解決回数 | proxy reconnect で 1 回の operation が複数回カウントされうる、または idle respawn で過小 |
| `universe_create` | 宇宙作成 | — |
| `universe_delete` | 宇宙削除 | — |
| `universe_restore` | 宇宙復元 | — |

> `audit_log.action` は **過去形** (`universe_created` 等) で、`usage_events.event_type` は **原形** (`universe_create` 等) — 意図的な使い分け (Codex review #2)。

**billing-grade の正確な operation count は MV4.1 で導入** される予定 (backend が MCP notification で supervisor に報告する機構、別 PR)。v1 の `route_resolution` は activity の目安であり、課金の正確な根拠としては使えない。

## degraded mode (control plane 不可時)

control plane が到達不能でも **supervisor の機能は停止しない**:

| 状態 | supervisor の挙動 | usage の挙動 |
|---|---|---|
| network error / 5xx | `/route` / 宇宙作成 / 宇宙削除は **local registry で完結** (継続) | spool に蓄積 → 復旧で再送 |
| control plane 起動時不可 | 起動は **成功** する (fail しない) | 同上 |

- supervisor 起動時に control 不可でも起動は成功する。pull/push 共に WARNING log + skip
- usage は local spool (`<multiverse_root>/logs/usage-spool/`) に蓄積し、control plane 復旧後に `window_start` 昇順 (FIFO) で再送される

### usage spool の idempotency (設計判断 J10 / Codex B4)

- 各 batch に **`batch_id` (UUID4)** を付与
- 書き出しは **temp file → `flush()` → `os.fsync()` → `os.replace()` (atomic rename)**
- `flush_usage` は `asyncio.Lock` で直列化 (concurrent `arecord_event` と flush でカウントロストを防ぐ)
- control 側 `usage_batches(batch_id PK)` で **重複検知** — 同一 `batch_id` 再送は冪等 skip
- spool ファイル名 = `{window_start_basic_iso8601}_{batch_id}.jsonl` (例 `20260703T100000Z_550e8400....jsonl`) — ファイル名の辞書順ソートが `window_start` 昇順 (FIFO 再送、A7)
- disk full / permission denied は **ERROR log** (silent data loss でない) + in-memory counter に復元 (次回再試行)
- corrupt spool (JSON parse 失敗) は `quarantine/` へ移動し、後続の flush を block しない (Codex missing #6)

## permanent auth failure (401)

network error / 5xx とは **明確に区別** される (review #2 blocking #4):

| 状態 | 挙動 |
|---|---|
| network error / 5xx | WARNING log + spool 蓄積 + 次回再送 (degraded mode) |
| **401 (host token 不正 / revoke)** | **permanent auth failure**: ERROR log + `_auth_failed=True` + 以降の pull/push の **POST 試行を停止** (無駄防止)。**spool 書き込みは継続** (credential rotate 後の一括再送のため)。`/route` は local で継続 (supervisor 機能は停止しない) |

### 復旧手順 (operator)

> **★ 同一 host_id で rotate すること** (Codex B1): `POST /admin/hosts {label}` で *新規 host* を登録すると **別の host_id** が発行され、既存の `universes.host_id` 行が orphan する (usage POST の `WHERE universe_id=$1 AND host_id=$2` が弾き続け、spool は永遠に drain しない)。**必ず `POST /admin/hosts/{hid}/rotate-token` で同一 host_id の token を差し替えること** — これが revoke 解除 + 新 token hash 更新を 1 トランザクションで行い、`host_id` を不変に保つ。

1. control plane 側で **同一 host_id** の token を rotate する:
   ```bash
   curl -X POST http://127.0.0.1:7881/admin/hosts/<hid>/rotate-token \
     -H "X-Admin-Key: $CONTROL_ADMIN_KEY"
   # -> {"host_id": "<hid — 従来どおり不変>", "token": "<新平文 token — 一度だけ表示>"}
   ```
   これが `token_hash` 更新 + `revoked_at` クリアを同一 txn で行う (audit_log `host_token_rotated` 記録付き)
2. supervisor の `GAOTTT_CONTROL_HOST_TOKEN` env を新 token に更新 (`GAOTTT_CONTROL_HOST_ID` はそのまま)
3. supervisor restart → `_auth_failed` クリア → 起動時の残留 spool (通常 spool、**quarantine ではない**) を `window_start` 昇順で再送 (起動時 replay はバックグラウンド実行なので supervisor 起動を block しない — Codex B2)
4. `GET /admin/status` (supervisor 管理面) で `auth_failed=False` を確認

### 監視

supervisor の `GET /admin/status` が permanent auth failure state を expose する (review #2 remaining gap):

```bash
curl http://127.0.0.1:7880/admin/status -H "X-Admin-Key: $GAOTTT_SUPERVISOR_ADMIN_KEY"
# control_client 未設定: {"control": null}
# 設定済み: {"control": {"auth_failed": false, "since": null, "spool_pending": 0}}
```

operator はこれを polling して revoked token を早期検知し、spool の無限増殖を防ぐ。

> `GET /admin/status` は管理面 (parity 対象外、`/reset`・supervisor admin API と同じ例外クラス)。

## 制限事項 (v1)

- **localhost only / 単一ホスト** — control plane と supervisor は同じホストの別プロセス。network 越し (別ホスト配置) は v1 範囲外 (TLS + network 認証が追加で必要、J8)
- **usage は `route_resolution` telemetry で operation count ではない** — billing-grade の正確な operation count は [MV4.1](Plans-Multiverse-Scale-Out.md) で導入 (J1=A、上記「usage telemetry の意味」節参照)
- **control plane の HA 構成なし** — 単一インスタンス + systemd `Restart=always` のみ
- **課金計算ロジックなし** — `usage_events` / `usage_batches` を蓄えるのみ。課金計算は外部システム (Stripe 等) の責務
- **control plane から supervisor への push 通知なし** — supervisor は pull model のみ。control 側で宇宙削除されても supervisor は次回 sync で conflict 検知 (即時伝播しない、degraded mode 整合性維持のため)
- **control 側からの運用 deprovisioning なし** — control `DELETE /admin/universes/{uid}` は台帳 row の論理削除のみ。物理削除は operator が runbook で手動 (J5)
- **control `DELETE` は supervisor に即時伝播しない** — 次回 sync で WARNING (即時削除しない、conflict detection)

## トラブルシューティング

| 症状 | 原因 / 対処 |
|---|---|
| control plane が起動しない (`RuntimeError: admin_key must be non-empty`) | `CONTROL_ADMIN_KEY` が未設定。強い乱数を設定して再起動 |
| control plane が起動しない (`SystemExit: control plane must bind to localhost`) | `CONTROL_LISTEN_HOST` に非 localhost を指定した。`127.0.0.1` / `localhost` / `::1` のいずれかにする (J8) |
| control plane が起動しない (pool 接続失敗) | Postgres が起動しているか、`CONTROL_DATABASE_URL` が正しいか、認証情報が合っているか確認。disposable の場合は `docker compose -f control/compose.yml up -d` で起動 |
| migration が失敗する | runner は 1 ファイル = 1 トランザクションなので、失敗したファイルは全体が ROLLBACK され `schema_migrations` には INSERT されない (partial apply なし)。DB の状態を確認し、壊れた migration SQL を修正して再実行 |
| supervisor の log に `401 on usage POST — permanent auth failure` が連発 | host token が revoke されたか不正。`GET /admin/status` で `auth_failed=true` を確認 → 上記「復旧手順」に従い **同一 host_id で `rotate-token`** → env 更新 → supervisor restart |
| spool ファイルが蓄積し続ける | control plane が長期間不可、または permanent auth failure が未解決。`GET /admin/status` の `spool_pending` を監視。disk 容量に注意 (v1 は size 上限なし、disk 監視が運用の責務) |
| spool ファイルが `quarantine/` に移動されている | JSON parse 失敗の corrupt spool。手動で中身を確認し、原因 (disk corruption / bug) を特定。後続の flush は block されないので、quarantine の中身は調査後削除して良い |
| `auth_failed=true` なのに spool に蓄積されている | 期待どおり。permanent auth failure 中は POST を試みないが、credential rotate 後の一括再送のために spool 書き込みは継続する (review #2 blocking #4) |
| sync で `conflict` が `audit_log` に記録される | control 側と local で宇宙の状態が矛盾 (control 削除済み / local 生存、host_id / embedder 差分)。**自動修正しない** (J5) — operator が `audit_log` を確認し、どちらが正か判断して手動で解決 |

→ より詳細: [Operations — Troubleshooting](Operations-Troubleshooting.md)、[Operations — Tuning](Operations-Tuning.md)「Multiverse control plane client (MV4)」節

## 関連

- [Operations — Multiverse Setup (MV3)](Operations-Multiverse-Setup.md) — supervisor 本体の運用
- [Operations — Tuning](Operations-Tuning.md) — MV4 control plane knob 7 つ
- [Plans — Multiverse Scale-Out](Plans-Multiverse-Scale-Out.md) §Stage 3 — 戦略計画
- [multiverse-implementation-plan.md](../maintainers/multiverse-implementation-plan.md) §MV4 — 実装者向け作業計画
- [multiverse-mv4-execution-plan.md](../maintainers/multiverse-mv4-execution-plan.md) — PM execution plan (assumption ledger / 設計判断 J1–J12 含む)
