# MV4 Execution Plan — Control Plane (Postgres)

> 起票: 2026-07-03 / 改訂: 2026-07-03 (v3 — Codex plan review #1 → v2 → Codex plan review #2 で B3/B5/B6/B7 RESOLVED、B1/B2/B4 と新 blocking 4 件を反映)
> リスク分類: **high-risk**（新規依存 asyncpg / 新規外部 DB (Postgres) + 番号付き migration / 認証境界 (host token) / 監査データ完全性 / supervisor 連携）。ただし engine コード（`gaottt/core/` / physics / observation 層）は**一切接触しない**ので engine 汚染リスクは低い
> 前提: MV0 (manifest) / MV1 (embedding service) / MV2 (owner lease) / MV3 (supervisor + multiverse layout) 完了済み
> SoT: [implementation-plan §MV4](multiverse-implementation-plan.md#mv4--control-planepostgres)（**J1 は PM 承認済み SoT deviation**、下記参照）
> 関連: [handover-2026-07-03-multiverse-all.md](handover-2026-07-03-multiverse-all.md) §10 / [Codex plan review #1](#codex-plan-review-1-結果と反映ログ)

## 目標

テナント・宇宙の台帳（**aggregator / audit / billing 収集点**）、supervisor 連携の同期先としての **control plane (Postgres)** を、`gaottt/` パッケージの外に **同一ホスト内の独立プロセス** としてデプロイ可能なパッケージで構築する。supervisor は起動時 + 定期的に control plane と同期し、usage telemetry を batch push する。control plane 到達不能時は **degraded mode**（local manifest 一次でホスト自走）する。

> **権限モデル（Codex scope concern 反映）**: control plane は **aggregator / audit / billing 収集点** であり、運用 deprovisioning の権威ではない。local manifest が運用の一次（J5）。control 側の `DELETE` は台帳 row の論理削除のみで、supervisor は次回 sync で **conflict を検知して WARNING**（即時削除しない、operator が手動で）。この 2 つのモデルを混ぜない。

## スコープ（本 stage でやること）

1. **`control/` 独立パッケージ**（`gaottt/` の外、`control/pyproject.toml`）— asyncpg + FastAPI + uvicorn + pydantic、engine と依存を混ぜない（**同一ホスト内の別プロセス**）
2. **番号付き plain SQL migration**（`control/control/schema/NNN_*.sql` + bootstrap-aware 軽量 runner、alembic 不使用）
3. **control plane API**（`control/control/api.py`）— host token 認証 / tenants CRUD / universes CRUD / supervisor 向け sync API / audit log（**同一トランザクション**）
4. **disposable Postgres**（`control/compose.yml`、開発/テスト用。CI 自動化しない）
5. **`gaottt/multiverse/control_client.py`** — supervisor → control plane の pull/push client + **idempotent な local spool**（`batch_id` 付き、degraded mode）
6. **config knobs**（`gaottt/config.py` に 7 つ追加、全て default 不変）
7. **supervisor 統合**（lifespan に control_client の pull loop / push loop を組込み、control 不可時は警告して継続）
8. **integration tests**（degraded mode / idempotency / revoked token / supervisor status expose 含む 9 シナリオ）
9. **docs** — [Operations — Control Plane](../wiki/Operations-Control-Plane.md) 新規 + Tuning + Sidebar/Home + Architecture 設計判断表 + implementation-plan 完了マーク

## 非スコープ（やらないこと）

- **engine コードへの能力追加** — MCP 新ツール / REST 新エンドポイント 0。control plane API は管理面（parity 対象外、`/reset`・supervisor admin API と同じ例外クラス）
- **physics / observation 層への一切の接触** — `gaottt/core/gravity.py` / `gaottt/core/scorer.py` / mass・displacement・velocity 更新則は 1 行も変更しない（acceptance で `git diff` が空であることを assert）
- **engine への usage counter 機構追加** — recall/remember/ingest の正確な回数集計は engine.py 改修が必要になり high-risk。**v1 は supervisor が `/route` 解決回数を activity telemetry として集計**（J1=A、PM 承認済み SoT deviation）。billing-grade の正確な operation count は **MV4.1**（backend が MCP notification で supervisor に報告する機構、別 PR）で導入
- **control plane の HA 構成** — 単一インスタンス + systemd `Restart=always` のみ
- **課金計算ロジック** — `usage_events` / `usage_batches` を蓄えるのみ。課金計算は外部システム（Stripe 等）の責務
- **control plane から supervisor への push 通知** — supervisor は pull model のみ。control 側で宇宙削除されても supervisor は次回 sync で conflict 検知（即時伝播しない、degraded mode 整合性維持のため）
- **control 側からの運用 deprovisioning** — control `DELETE /admin/universes/{uid}` は台帳 row の論理削除のみ。物理削除フローは operator が runbook で手動（J5 local 一次）
- **REST 経路の宇宙提供** — MV3 と同じく MCP のみ（lease 構造的拒否）
- **MV5 backup/DR / MV6 英語宇宙** — 本 stage 外

## 設計判断（確定）

| # | 判断 | 仕様 | 根拠 |
|---|---|---|---|
| J1 | usage telemetry 集計（**PM 承認済み SoT deviation、ユーザー確認 (A)**） | **v1 は supervisor が `/route` 解決回数を activity telemetry として集計**。`event_type`（`usage_events`、原形）は `route_resolution` / `universe_create` / `universe_delete` / `universe_restore` の 4 種（`audit_log.action` は過去形 `universe_created` 等と使い分け、別記）。**命名で「operation count ではない」を明示**（`route_resolution` = route 解決回数、proxy reconnect で過小カウントしうる）。recall/remember 正確回数は v1 では取らない。**billing-grade の正確な operation count は MV4.1**（backend MCP notification 機構、別 PR）で導入 | 横断ルール 1「physics 層に触らない」+ engine.py の usage counter 追加は high-risk。SoT（implementation-plan §MV4）の「recall/remember/ingest 回数」から逸脱するため PM 承認を取得済み（Codex B6 対応） |
| J2 | control plane listen port | **7881**（7878=単一 backend / 7879=embedding service / 7880=supervisor / 7890-7989=宇宙 backend の既存マッピングの続き） | 既存予約 port との衝突回避 |
| J3 | host token 発行フロー | control plane 側 `POST /admin/hosts {label}` → 平文 token を一度だけ返す（SHA-256 hash を DB 保存、registry の API key と同パターン）。supervisor は `GAOTTT_CONTROL_HOST_TOKEN` env で受け取る。**admin key と host token は別物**: admin key = control plane 全体管理（`compare_digest` で検証）、host token = 個別 supervisor 認証（**SHA-256 hash → DB parameterized lookup**、平文は DB に無いので `compare_digest` ではなく hash 一致で検証 — Codex non-blocking #3 反映） | registry.py の API key パターン踏襲（実績あり） |
| J4 | migration runner（**bootstrap-aware、Codex B7 反映**） | `control/control/migrate.py` に `scripts/migrate.py` の versioned 思想を踏襲した軽量 runner。**`ensure_bootstrap()` が `schema_migrations` テーブルを bootstrap トランザクションで先に作成**（番号付きファイルを scan する前に必要）。`schema/NNN_*.sql` を番号順 apply、**1 ファイル = 1 トランザクション**（失敗時はそのファイル全体 rollback、`schema_migrations` には INSERT されない）。冪等（既適用は skip）。alembic は依存が重いので不使用 | implementation-plan §MV4「alembic は依存が重いので v1 では入れない」+ 横断ルール 3 + B7 |
| J5 | sync 競合解決（**権限モデル明文化、Codex B3 反映**） | **local manifest が運用の一次、control plane は aggregator / audit / billing 収集点**。pull で control 側の認識を取得 → local registry と突き合わせ → 矛盾時は **local を正として control に報告**（`POST /hosts/{hid}/sync`）。**control 側で削除（status='deleted'）された宇宙が local にある場合 → supervisor は即時削除せず WARNING log + admin 通知（conflict detection、誤削除防止）**。control `DELETE /admin/universes/{uid}` は台帳 row の論理削除のみで運用 deprovisioning ではない。audit log に全変更を記録 | implementation-plan「local manifest が一次」+ 誤削除防止 + 権限モデルの 2 つを混ぜない（Codex scope concern） |
| J6 | asyncpg pool lifecycle | app lifespan で pool 作成/破棄。`db_pool_min_size: int = 2` / `db_pool_max_size: int = 10`（config 名統一、Codex non-blocking #1 反映）。接続失敗時は supervisor 側の control_client が spool に退避して再試行（degraded mode） | FastAPI 標準パターン + MV1 RemoteEmbedder の retry 戦略踏襲 |
| J7 | テスト戦略 | ① 純粋関数（migration version parse / SQL ファイル読み込み / Pydantic validation）は docker 不要 unit test。② DB 接続を伴う test は **docker-compose の disposable Postgres** 必須、`@pytest.mark.requires_postgres` + skip-if-unavailable（CI 自動化しない、tests/perf/ と同じ思想）。③ control_client は httpx MockTransport で unit test（MV1 RemoteEmbedder 同パターン）。④ **exact SQL boot test**（real Postgres で `001_initial.sql` を実行、Codex missing #2 反映） | tests/perf/ の CI 非自動化思想 + MV1 実績パターン |
| J8 | control plane localhost bind | `_validate_embedder` と同様、**非 localhost bind は `SystemExit` で拒否**。認証は host token だが信頼境界は localhost のみ。**「独立デプロイ物」= 同一ホスト内の独立プロセス**（control と supervisor は同じ host、別プロセス。network 越しは v1 範囲外 — Codex non-blocking #5 反映） | MV1 embedding service のセキュリティ設計踏襲 |
| J9 | `control/` の import 方向 | **`gaottt/` → `control/` の依存なし（双方向に独立）**。`control/` は `gaottt/` を import しない。supervisor 側 `gaottt/multiverse/control_client.py` は `gaottt.config` / `gaottt.multiverse.registry` / `httpx` のみ使い（**asyncpg を import しない**） | engine/依存汚染防止（MV1 の成果を無効化しない） |
| J10 | usage spool 形式（**idempotency 強化、Codex B4 反映**） | `<multiverse_root>/logs/usage-spool/` に JSON Lines。**各行に `batch_id` (UUID4) を付与**。書き込みは **temp file + `flush()` + `os.fsync()` + atomic `os.replace()`**。flush は **`asyncio.Lock` で直列化**（concurrent record_event と flush_usage でカウントロストを防ぐ）。disk full は ERROR log + spool 書き込み失敗 = in-memory counter は保持（次回再試行）。control 側 `usage_batches(batch_id PK, host_id, window_start, window_end, received_at, event_count)` で **重複検知**（同一 batch_id 再送は冪等 skip） | billing/audit grade の durability（Codex B4）+ MV3 のログ運用 |
| J11 | tenant mapping（**Codex B2 反映**） | supervisor config に `control_default_tenant_id: str = ""`（未設定 = 単一暗黙 tenant `"default"`、handover §1「単一ホスト・複数テナント」の v1 制約と整合）。`create_universe` はオプションで `tenant_id` 受け取り（未指定なら config の default）。control 側は `tenant_id` が未登録なら `POST /hosts/{hid}/sync` で WARNING（auto-create しない、operator が明示登録）。これで v1 単純化 + 将来 multi-tenant 拡張が両立 | handover §1 の v1 制約 + B2 |
| J12 | audit log transactionality（**Codex B5 反映**） | **mutating endpoint（tenants / users / hosts / universes の作成・削除・revoke）は同一 DB トランザクションで domain mutation + audit_log INSERT を実行**。audit INSERT 失敗 = mutation も rollback（監査完全性）。secondary diagnostic log（debug 用）は best-effort のままで OK | high-risk 認証境界の監査完全性（B5） |

## 実装アプローチ

### WP-1: `control/` パッケージ基盤（test-first）

**ディレクトリ構成**:

```
control/
├── pyproject.toml              # 独立パッケージ。asyncpg / fastapi / uvicorn / pydantic 依存（gaottt 非依存）
├── README.md                   # control plane の概要・起動・migration 手順
├── compose.yml                 # disposable Postgres (開発/テスト用)
├── control/
│   ├── __init__.py
│   ├── config.py               # ControlConfig (env から読込)
│   ├── db.py                   # asyncpg pool 管理 (create_pool / close)
│   ├── migrate.py              # bootstrap-aware 番号付き SQL migration runner
│   ├── models.py               # Pydantic models
│   └── schema/
│       └── 001_initial.sql     # 初期 schema (6 domain tables、schema_migrations は runner が bootstrap)
└── tests/
    ├── conftest.py             # requires_postgres marker / disposable Postgres fixture
    ├── test_migrate.py         # runner 純粋関数 + 実 DB 適用 + failure atomicity
    ├── test_models.py          # Pydantic validation
    └── test_db.py              # pool lifecycle (docker 必須)
```

**`control/pyproject.toml`**:
- `[project] dependencies`: `asyncpg>=0.29.0`, `fastapi>=0.115.0`, `uvicorn[standard]>=0.30.0`, `pydantic>=2.0.0`
- `[project.optional-dependencies] dev`: `pytest>=8.0.0`, `pytest-asyncio>=0.23.0`, `httpx>=0.27.0`
- `[build-system] hatchling`（`gaottt/` と同じ）
- `[tool.pytest.ini_options] asyncio_mode = "auto"`, testpaths = `["tests"]`, markers に `requires_postgres`
- **`gaottt` を依存に含めない**（J9、acceptance で grep が空を assert）

**`001_initial.sql`**（番号付き plain SQL。Codex B1/B2/B4/B7 + review #2 新 blocking #1/#2 反映 — `tenant_id` 非正規化 / `usage_batches` を `usage_events` の **前** に移動（FK 解決順序）/ `default` tenant の bootstrap INSERT / `schema_migrations` は runner が bootstrap）:

```sql
-- schema_migrations は migrate.py の ensure_bootstrap() が作成する（chicken-and-egg 回避、Codex B7）

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id  TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- default tenant を bootstrap（Codex review #2 新 blocking #2 — tenant_id="default" の FK 解決）
INSERT INTO tenants (tenant_id, name) VALUES ('default', 'Default tenant (implicit, MV4 v1)')
ON CONFLICT (tenant_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL REFERENCES tenants(tenant_id),
    email      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, email)
);

CREATE TABLE IF NOT EXISTS hosts (
    host_id     TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,   -- SHA-256 of host token (plaintext issued once)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS universes (
    universe_id      TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL REFERENCES tenants(tenant_id),
    owner_user_id    TEXT REFERENCES users(user_id),
    host_id          TEXT REFERENCES hosts(host_id),
    embedder_id      TEXT NOT NULL,
    embedder_version TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_universes_tenant ON universes(tenant_id);
CREATE INDEX IF NOT EXISTS idx_universes_host   ON universes(host_id);

-- usage_batches を usage_events の **前に** 作成（Codex review #2 新 blocking #1 — FK 解決順序）
CREATE TABLE IF NOT EXISTS usage_batches (
    batch_id     UUID PRIMARY KEY,
    host_id      TEXT NOT NULL REFERENCES hosts(host_id),
    window_start TIMESTAMPTZ NOT NULL,
    window_end   TIMESTAMPTZ NOT NULL,
    event_count  INTEGER NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- usage_events は tenant_id を非正規化で保持（Codex B1 — subquery index は Postgres が拒否する）
CREATE TABLE IF NOT EXISTS usage_events (
    id           BIGSERIAL PRIMARY KEY,
    batch_id     UUID NOT NULL REFERENCES usage_batches(batch_id),  -- 冪等性の単位（Codex B4）
    universe_id  TEXT NOT NULL REFERENCES universes(universe_id),
    tenant_id    TEXT NOT NULL REFERENCES tenants(tenant_id),       -- 非正規化（B1）
    host_id      TEXT NOT NULL REFERENCES hosts(host_id),
    event_type   TEXT NOT NULL,          -- 'route_resolution' | 'universe_create' | 'universe_delete' | 'universe_restore' (J1=A、原形統一)
    count        INTEGER NOT NULL DEFAULT 1,
    window_start TIMESTAMPTZ NOT NULL,
    window_end   TIMESTAMPTZ NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_usage_tenant_time ON usage_events(tenant_id, window_start);
CREATE INDEX IF NOT EXISTS idx_usage_universe_time ON usage_events(universe_id, window_start);

CREATE TABLE IF NOT EXISTS audit_log (
    id        BIGSERIAL PRIMARY KEY,
    tenant_id TEXT,
    actor     TEXT NOT NULL,             -- host_id or 'admin'
    action    TEXT NOT NULL,             -- 過去形: 'universe_created' | 'universe_deleted' | 'host_registered' | ...（audit_log.action は過去形、usage_events.event_type は原形 — 使い分け明記）
    target    TEXT,
    at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    detail    JSONB
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_time ON audit_log(tenant_id, at);
CREATE INDEX IF NOT EXISTS idx_audit_actor_time ON audit_log(actor, at);
```

> **event_type vs audit_log.action の使い分け（Codex review #2 non-blocking 反映）**: `usage_events.event_type` は **原形**（`route_resolution` / `universe_create` / `universe_delete` / `universe_restore` — 分類名）、`audit_log.action` は **過去形**（`universe_created` / `universe_deleted` / `host_registered` / `host_revoked` — 監査記録の自然な表現）。両者の vocabulary を WP-2 実装時に定数として定義し、test で検証。

**`control/control/migrate.py`**（bootstrap-aware runner、Codex B7 反映）:
- `async def ensure_bootstrap(pool) -> None`: `schema_migrations(version TEXT PK, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())` を冪等作成（`CREATE TABLE IF NOT EXISTS`）。**番号付きファイル scan より前に呼ぶ**
- `async def run_migrations(pool, schema_dir: Path) -> list[str]`: `schema/` 配下の `NNN_*.sql` を番号順に列挙、未適用を apply
- 適用は **1 ファイル = 1 トランザクション**（`BEGIN` / `COMMIT`、失敗時は `ROLLBACK` でそのファイル全体が巻き戻り、`schema_migrations` には INSERT されない = partial apply なし）
- 適用成功で `schema_migrations` に INSERT（同一トランザクション内）
- 戻り値は新規適用した version list（空なら no-op）
- **純粋関数**: `parse_version(filename) -> str`（`001_initial.sql` → `"001"`）、`list_migrations(schema_dir) -> list[tuple[str, Path]]`（番号順）は docker 不要で unit test 可能

**`control/control/db.py`**:
- `async def create_pool(dsn: str, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool`
- `async def close_pool(pool) -> None`
- 接続失敗は `ConnectionError` を即 raise（起動時に倒す）

**`control/compose.yml`**（開発/テスト用 disposable）:
- `postgres:16-alpine`、`POSTGRES_DB=gaottt_control`、`POSTGRES_USER=gaottt`、`POSTGRES_PASSWORD=dev-only`、port 55432（衝突回避）
- volume なし（disposable）

**unit tests**（`control/tests/`）:
- `test_migrate.py::test_parse_version` — ファイル名 → version 変換
- `test_migrate.py::test_list_migrations_sorted` — 番号順ソート
- `test_migrate.py::test_ensure_bootstrap_idempotent` — docker 必須、2 回呼び出しで 2 回目 no-op
- `test_migrate.py::test_run_migrations_idempotent` — docker 必須、2 回実行で 2 回目 no-op
- `test_migrate.py::test_run_migrations_applies_schema` — docker 必須、**6 domain テーブル** が作られる（Codex non-blocking #2 訂正）
- `test_migrate.py::test_run_migrations_failure_atomicity` — docker 必須、**意図的に壊れた第 2 migration で partial apply が無いこと**（`schema_migrations` に第 2 が無い、第 2 が作ろうとした DDL が無い — Codex missing #1 反映）
- `test_migrate.py::test_exact_sql_boot` — docker 必須、**real Postgres で `001_initial.sql` をそのまま実行**して全テーブル + index が作られる（Codex missing #2 反映）
- `test_models.py` — Pydantic validation（必須 field / 型 / default）
- `test_db.py::test_pool_lifecycle` — docker 必須、create/close

### WP-2: `control/` API（test-first、WP-1 依存）

**`control/control/api.py`** — FastAPI app factory `create_app(config: ControlConfig) -> FastAPI`:

| endpoint | auth | 動作 |
|---|---|---|
| `POST /admin/tenants {name}` | admin key | tenant 作成、`tenant_id` 発行。**同一トランザクションで audit_log**（J12）|
| `GET /admin/tenants` | admin key | tenant 一覧 |
| `POST /admin/tenants/{tid}/users {email}` | admin key | user 作成、**同一トランザクションで audit_log** |
| `POST /admin/hosts {label}` | admin key | host 登録、**平文 host token を一度だけ返す**（SHA-256 hash を DB 保存）。**同一トランザクションで audit_log** |
| `DELETE /admin/hosts/{hid}` | admin key | host revoke（`revoked_at` 設定、token 無効化）。**同一トランザクションで audit_log** |
| `POST /admin/universes {tenant_id, owner_user_id?, host_id, universe_id, embedder_id, embedder_version}` | admin key | 宇宙登録（supervisor が作った宇宙を台帳に反映）。**同一トランザクションで audit_log** |
| `DELETE /admin/universes/{uid}` | admin key | 宇宙の**台帳 row 論理削除**（`status='deleted'`、**運用 deprovisioning ではない** — J5）。**同一トランザクションで audit_log** |
| `GET /admin/universes` | admin key | 全宇宙一覧 |
| `GET /hosts/{hid}/universes` | **host token** | 自ホストの宇宙一覧（supervisor の pull 用）|
| `POST /hosts/{hid}/usage` | **host token** | **idempotent な usage batch 受領**（`batch_id` で重複検知、Codex B4）。`hid` は token が属する host と一致確認（不一致は 403）。**同一トランザクションで usage_batches INSERT + usage_events INSERT + audit_log** |
| `POST /hosts/{hid}/sync` | **host token** | local state 報告（universe 追加/削除の突き合わせ結果）。control 側で矛盾を **audit_log に記録**（記録のみ、自動削除しない — J5）|
| `GET /health` | なし | liveness probe（DB 接続確認、接続NG は 503）|

**`POST /hosts/{hid}/sync` payload schema（review #2 non-blocking #3 反映で明記）**:
```json
{
  "local_universes": [
    {"universe_id": "u1", "tenant_id": "default", "host_id": "h1",
     "embedder_id": "cl-nagoya/ruri-v3-310m", "embedder_version": "abc123",
     "status": "active"}
  ],
  "deleted_universes": ["u2"]
}
```
- `local_universes` の各 row を control 側で upsert（存在しない `universe_id` は INSERT、存在するが `host_id` / `embedder_id` 等に差分があれば `audit_log` に conflict 記録、自動修正しない）
- `deleted_universes` の各 `universe_id` について、control 側で既に `status='deleted'` なら矛盾なし、`status='active'` なのに local で削除と言われたら `audit_log` に conflict 記録（即時削除しない、J5）
- 全て同一トランザクションで audit_log 記録まで完了（J12）

**`POST /hosts/{hid}/usage` 挿入順序（review #2 remaining gap 反映）**:
- 同一トランザクション内で **`usage_batches` INSERT を先、`usage_events` INSERT を後**（FK 制約のため自明だが明記）
- 同一トランザクションで `audit_log` にも INSERT（J12）

**認証**（`control/control/auth.py`）:
- `_make_admin_checker(config)`: admin key を `X-Admin-Key` or `Authorization: Bearer` で受付、**`secrets.compare_digest`**（MV3 supervisor の `_make_admin_checker` と同パターン、Codex non-blocking #3 反映）
- `_make_host_checker(config, pool)`: host token → **SHA-256 hash 計算 → DB parameterized lookup**（`SELECT host_id FROM hosts WHERE token_hash=$1 AND revoked_at IS NULL`）。平文 token は DB に無いので `compare_digest` ではなく hash 一致で検証（high-entropy token なので acceptable — Codex non-blocking #3 反映）。token の host_id と path の `hid` が一致しないと 403
- **admin key 空 = fail-fast**（MV3 と同様、`create_app` で `RuntimeError`）
- **全ての text field は asyncpg parameterized query**（`$1`, `$2`...）で bind — SQL injection 防御（Codex missing #10 反映）

**audit log transactionality（J12 / Codex B5）**:
- mutating endpoint は **同一 `async with pool.acquire() as conn: async with conn.transaction():` ブロック内** で domain mutation + audit_log INSERT を実行
- audit INSERT 失敗 = `transaction()` exit 時に rollback、endpoint は 500 で失敗（mutation も取り消される）
- secondary diagnostic log（debug 用の `logger.debug` 等）は best-effort のままで OK

**`ControlConfig`**（`control/control/config.py`、Codex non-blocking #1 反映で config 名統一）:
- `database_url: str`（`CONTROL_DATABASE_URL` env、必須）
- `admin_key: str`（`CONTROL_ADMIN_KEY` env、**空 = fail-fast**）
- `listen_host: str = "127.0.0.1"`（`CONTROL_LISTEN_HOST`、**非 localhost は `SystemExit`** — J8）
- `listen_port: int = 7881`（`CONTROL_LISTEN_PORT`）
- `db_pool_min_size: int = 2`（`CONTROL_DB_POOL_MIN_SIZE`）
- `db_pool_max_size: int = 10`（`CONTROL_DB_POOL_MAX_SIZE`）
- `schema_dir: Path = Path(__file__).parent / "schema"`

**`__main__` entry**: `python -m control` → `ControlConfig.from_env()` → `create_app(config)` → lifespan で `create_pool` + `ensure_bootstrap` + `run_migrations` + `close_pool` → uvicorn run

**unit tests**（docker 必須のものは `@pytest.mark.requires_postgres`）:
- `test_api.py::test_admin_auth_*` — 空 admin key fail-fast / 正しい key 通過 / 不正 401 / header 2 形式
- `test_api.py::test_host_auth_*` — host token 発行 / 正しい token 通過 / 不正 401 / revoke 後 401 / path hid ≠ token host で 403
- `test_api.py::test_host_token_revoked_is_permanent` — revoke 後は **permanent auth failure**（単なる degraded とは区別 — Codex non-blocking #4 反映）
- `test_api.py::test_tenant_crud` — 作成/一覧
- `test_api.py::test_universe_register` — supervisor が作った宇宙を台帳に反映
- `test_api.py::test_usage_batch_idempotent` — **同一 `batch_id` の再送は 2 回目が冪等 skip** される（Codex missing #3 反映）
- `test_api.py::test_sync_*` — local state 報告 / 矛盾で audit_log 記録（自動削除しない）
- `test_api.py::test_sync_control_deleted_local_alive` — control 側削除済み + local に生存 → audit_log 記録のみ（即時削除しない、J5）
- `test_api.py::test_health` — DB 接続 OK / DB down で 503
- `test_api.py::test_audit_log_transactional_rollback` — **audit INSERT を意図的に失敗させて mutation が rollback する**（Codex missing #9 反映）
- `test_api.py::test_sql_injection_*` — `email` / `tenant_id` / `universe_id` / `label` / `host_id` に injection string を入れて parameterized query で無害化される（Codex missing #10 反映）
- `test_api.py::test_tenant_mapping_sync` — supervisor が未登録 `tenant_id` で sync → WARNING（auto-create しない — Codex missing #8 反映）

### WP-3: `control_client` + config knobs（test-first、WP-2 の API contract 依存）

**config knobs**（`gaottt/config.py` の MV3 multiverse knob 群の後に追加。**7 つ**、J11 で +1）:

| knob | default | 意味 |
|---|---|---|
| `control_plane_url: str = ""` | `""` (未設定 = control plane 不使用) | `GAOTTT_CONTROL_PLANE_URL` |
| `control_host_id: str = ""` | `""` | `GAOTTT_CONTROL_HOST_ID`（control plane 側で発行された host_id）|
| `control_host_token: str = ""` | `""` | `GAOTTT_CONTROL_HOST_TOKEN`（control plane 側で発行された平文 token）|
| `control_default_tenant_id: str = ""` | `""` (未設定 = 単一暗黙 tenant `"default"`、J11) | `GAOTTT_CONTROL_DEFAULT_TENANT_ID` |
| `control_sync_interval_seconds: float = 300.0` | 300.0 | pull 周期 |
| `usage_push_interval_seconds: float = 60.0` | 60.0 | push 周期 |
| `usage_spool_dir: str = ""` | `""` (未設定 = `<multiverse_root>/logs/usage-spool/`、`multiverse_root` も未設定なら機能不使用) | spool ディレクトリ（degraded mode 用）|

> **発動条件**: `control_plane_url` と `control_host_id` と `control_host_token` の **3 点とも設定時のみ** control_client が有効。1 つでも欠けたら WARNING log + control 不使用（supervisor は従来通り local-only）。**default 不変**

> **★ `control_host_token` は secret 扱い**: `_build_spawn_env` と同様に log に出さない（spawn される backend にも継承させない — supervisor 自身のみが使う）

**`gaottt/multiverse/control_client.py`**（Codex B4 反応で idempotent spool + review #2 新 blocking #3/#4 反応で async `record_event` + recovery 明記）:

```python
class ControlClient:
    """Supervisor → control plane の pull/push client (WP-3).

    async HTTP client (httpx.AsyncClient) で control plane と通信。
    全ての通信失敗は例外を吐かず WARNING log + リトライ/spool 退避で
    degraded mode を維持する（control plane 落ちでもホスト自走）。
    ただし **401 (host token 不正/revoke) は permanent auth failure** として
    ERROR log + 以降の POST 試行停止 + spool 蓄積は継続（review #2 新 blocking #4）。
    """
    def __init__(self, config: GaOTTTConfig, registry: MultiverseRegistry):
        self._auth_failed: bool = False  # permanent auth failure flag
        ...

    # -- pull -----------------------------------------------------------
    async def pull_host_universes(self) -> list[dict] | None:
        """GET /hosts/{hid}/universes。失敗は None（caller は local 継続）。
        401 は permanent auth failure フラグを立て、以降の pull を skip。"""

    async def reconcile_with_control(self) -> None:
        """pull → local registry と突き合わせ → 矛盾を POST /hosts/{hid}/sync で報告。
        local を正とする（J5）。control 側で削除された宇宙が local にある場合は
        WARNING log + sync で報告（即時削除しない、conflict detection）。"""

    # -- push (usage) ---------------------------------------------------
    async def record_event(self, universe_id: str, event_type: str, tenant_id: str, count: int = 1) -> None:
        """**async**（review #2 新 blocking #3 反映 — asyncio.Lock を正しく使える）。
        メモリ counter に event を蓄積。**asyncio.Lock で保護**（concurrent flush と
        カウントロストを防ぐ）。lock 保持時間は dict update のみ（O(1)、`/route`
        latency 影響は無視できる）。supervisor の /route が **url/token を返した後** に
        `await record_event(...)` で呼ぶ（Codex non-blocking #6 反映）。"""

    async def flush_usage(self) -> None:
        """**asyncio.Lock で直列化**（concurrent record_event と安全）。
        蓄積した counter で **batch_id (UUID4) を生成** → JSON Lines で spool に
        **temp file + fsync + atomic rename** で書き出し（Codex B4 反映）→
        POST /hosts/{hid}/usage で batch 送信。
        成功で spool ファイル削除、失敗は残留（次回再送）。
        **permanent auth failure 中は spool 書き込みは継続するが POST を試みない**
        （無駄を防ぐ、review #2 新 blocking #4 反映）。
        起動時に残留 spool を **window_start 昇順** で再送（FIFO、A7 修正）。"""

    # -- spool durability -----------------------------------------------
    def _write_spool_atomically(self, batch_id: str, window_start: str, payload: dict) -> Path:
        """spool ファイル名 = `{window_start_iso}_{batch_id}.jsonl`（review #2 non-blocking —
        UUID4 単独では FIFO 順序が意味ないため window_start を prefix で sort）。
        temp file 作成 → JSON 書き出し → flush → os.fsync → os.replace で atomic。
        disk full / permission denied は ERROR log + 例外伝播（caller は in-memory 保持）。"""

    def _quarantine_corrupt_spool(self, path: Path) -> None:
        """corrupt spool（JSON parse 失敗）を `usage-spool/quarantine/` へ移動。
        **corrupt = JSON parse 失敗のみ**。permanent auth failure 中の spool は
        **quarantine ではなく通常 spool に蓄積**（再送対象、review #2 新 blocking #4）。
        後続の正常 spool を block しない（Codex missing #6 反映）。"""

    # -- permanent auth failure recovery (review #2 新 blocking #4) ------
    def auth_failure_state(self) -> dict:
        """supervisor の status endpoint が permanent auth failure state を expose
        するための accessor。`{'auth_failed': bool, 'since': float|None,
        'spool_pending': int}` を返す。operator はこれで早期検知する。"""

    # -- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        """pull loop + push loop を asyncio task で起動。
        起動時に残留 spool を window_start 昇順で再送。"""

    async def stop(self) -> None:
        """loop 停止 → 最終 flush_usage → httpx client close。"""
```

**Permanent auth failure recovery（review #2 新 blocking #4 — 明記）**:

| 状態 | 挙動 |
|---|---|
| network error / 5xx | WARNING log + spool 蓄積 + 次回再送（degraded mode）|
| **401 (host token 不正/revoke)** | **permanent auth failure**: ERROR log + `_auth_failed=True` + 以降の pull/push の **POST 試行を停止**（無駄防止）。**spool 書き込みは継続**（credential rotate 後の一括再送のため）。`/route` は local で継続（supervisor 機能は停止しない）|
| corrupt spool（JSON parse 失敗）| `quarantine/` へ移動（permanent auth failure とは無関係）|

**復旧手順（operator）**:
1. control plane 側で host token を再発行（`POST /admin/hosts {label}` で新 token、または既存 host の revoke 解除）
2. supervisor の `GAOTTT_CONTROL_HOST_TOKEN` env を新 token に更新
3. supervisor restart → `_auth_failed` クリア → 起動時の残留 spool（通常 spool、quarantine ではない）を `window_start` 昇順で再送
4. `/health` または supervisor status endpoint（`GET /admin/status` 等の管理面、parity 対象外）で `auth_failed=False` を確認

**spool 形式**（JSON Lines、1 行 = 1 batch、Codex B4 で `batch_id` 付き）:
```json
{"batch_id": "550e8400-e29b-41d4-a716-446655440000", "window_start": "2026-07-03T10:00:00Z", "window_end": "2026-07-03T10:01:00Z", "host_id": "h1", "events": [{"universe_id": "u1", "tenant_id": "default", "event_type": "route_resolution", "count": 42}]}
```

**degraded mode 挙動**:
- control plane 接続不能（network error / 5xx）→ pull/push 共に WARNING log + skip。supervisor の宇宙作成/削除/route は **local registry で完結**（機能停止しない）
- **401 (host token 不正/revoke)** → **permanent auth failure**：ERROR log + `_auth_failed=True` + 以降の pull/push の **POST 試行を停止**（無駄防止）。**spool 書き込みは継続**（credential rotate 後の一括再送のため）。`/route` は local で継続（supervisor 機能は停止しない）。**network error とは明確に区別**（review #2 新 blocking #4 反映）
- spool に蓄積した usage は control 復旧（network error のみ）後に再送。**401 の場合は credential rotate + supervisor restart が必要なので、restart まで再送しない**（起動時の残留 spool 再送で一括処理）
- supervisor 起動時に control 不可でも起動は成功する（fail しない）
- **supervisor status endpoint**: permanent auth failure state を operator が検知できるよう、supervisor の管理面（`GET /admin/status` 等、parity 対象外）で `auth_failed` / `spool_pending` を expose（review #2 remaining gap 反映）

**unit tests**（`tests/unit/test_control_client.py`、httpx MockTransport）:
- `test_record_event_accumulates` — 同一 (universe, event_type) で count が加算
- `test_record_event_concurrent_safe` — 並行 `record_event` + `flush_usage` + `stop()` で **カウントロストが無い**（asyncio.Lock、Codex missing #5 反映）
- `test_flush_usage_success` — MockTransport で 200 → spool ファイル削除
- `test_flush_usage_failure_spool` — MockTransport で 500 → spool ファイル残留
- `test_flush_usage_idempotent_replay` — **同一 spool を 2 回再送しても control 側で重複カウントされない**（batch_id で冪等、Codex missing #3 反映）
- `test_flush_usage_crash_after_post_before_delete` — **POST 成功後の spool 削除前に crash → 再起動で再送しても control 側が重複カウントしない**（batch_id 冪等、review #2 remaining gap 反映）
- `test_flush_usage_stale_spool` — 起動時に残留 spool を window_start 昇順で再送
- `test_spool_atomic_write` — temp + fsync + rename（disk full で spool 書き込み失敗 = in-memory 保持、Codex B4）
- `test_spool_disk_full_error_observable` — disk full で ERROR log が出る（silent data loss でない、Codex missing #4 反映）
- `test_quarantine_corrupt_spool` — corrupt spool が quarantine/ へ移動し後続を block しない（Codex missing #6 反映）
- `test_pull_host_universes_failure_returns_none` — 接続エラーで None
- `test_pull_host_universes_401_permanent_auth_failure` — 401 で permanent auth failure フラグ、以降の pull/push 停止（Codex missing #7 反映）
- `test_reconcile_local_authoritative` — local を正として矛盾を sync 報告
- `test_reconcile_control_deleted_universe_local_warning` — control 側削除済み + local ある → WARNING（即時削除しない、J5）
- `test_disabled_when_config_incomplete` — `control_plane_url` 空で全メソッド no-op

### WP-4: supervisor 統合 + integration test（WP-3 依存）

**`gaottt/multiverse/supervisor.py` の修正**（最小差分）:
- `_Supervisor.__init__` に `control_client: ControlClient | None = None` を追加（default None = 従来通り）
- `create_supervisor_app` の lifespan で `control_client.start()` / `stop()` を呼ぶ（control_client が None なら skip）
- **`/route` handler で route 解決成功後・応答返却前に** `await control_client.arecord_event(uid, "route_resolution", tenant_id)` を呼ぶ（None check 付き、Codex non-blocking #6 反映。実装時の命名は `arecord_event` 等 async 明示を推奨）
- `create_universe` / `delete_universe` handler で `await control_client.arecord_event(uid, "universe_create"|"universe_delete", tenant_id)`（tenant_id は `CreateUniverseBody.tenant_id` or config default、J11）
- **`CreateUniverseBody` に optional `tenant_id: str | None = None` 追加**（J11、MV3 との後方互換性維持 — 未指定なら config の `control_default_tenant_id` or `"default"`）
- **`_main()` で ControlClient 構築**: config の 3 点セット（`control_plane_url` / `control_host_id` / `control_host_token`）揃っていれば構築、欠けていれば None

**`tests/integration/test_control_integration.py`**（docker 必須 + supervisor、**9 シナリオ**）:
- `@pytest.mark.requires_postgres` 付き、未利用可能時 skip
- **シナリオ 1**: supervisor 起動 → control 連携 → 宇宙作成 → control 側台帳に反映（pull で見える）
- **シナリオ 2**: `/route` ×N → control 側 `usage_events` に `event_type='route_resolution'` の count が N（**operation count ではない**ことを test でも明示、J1=A）
- **シナリオ 3 (degraded)**: supervisor 起動後に control plane 停止 → `/route` が成功継続、usage spool に蓄積 → control 復旧 → spool 再送で `usage_events` 反映
- **シナリオ 4 (permanent auth failure、Codex non-blocking #4)**: host token revoke → 401 → ERROR log + 以降 pull/push 停止 + `/route` は local で継続（**network error とは区別**）
- **シナリオ 5 (default 不変)**: `control_plane_url` 未設定 → control_client None → 従来 supervisor 挙動と完全一致（**既存 MV3 test 1 件も壊れない** ことの回帰 guard）。特に **MV3 の `POST /admin/universes {owner_label: ...}` が `tenant_id` 無しで従来通り動く** ことを確認（review #2 remaining gap 反映 — `CreateUniverseBody.tenant_id` optional 追加が MV3 互換性を壊さない）
- **シナリオ 6 (idempotent replay、Codex B4)**: spool 再送で control 側が重複カウントしない（`batch_id` で冪等）
- **シナリオ 7 (tenant mapping、Codex B2)**: `control_default_tenant_id` 未設定で宇宙作成 → `tenant_id="default"` で台帳反映
- **シナリオ 8 (audit transactional、Codex B5)**: audit INSERT 失敗で mutation rollback（control 側 unit test で検証済み、integration でも念のため）
- **シナリオ 9 (supervisor status expose、review #2 remaining gap)**: permanent auth failure 発生中に supervisor の管理面 status endpoint（`GET /admin/status` 等、parity 対象外）が `auth_failed=True` / `spool_pending=N` を返す。operator がこれで早期検知できる

### WP-5: docs

- 新規 [Operations — Control Plane](../wiki/Operations-Control-Plane.md)（アーキテクチャ / 前提 / setup 手順 / migration / degraded mode / permanent auth failure / トラブルシューティング）
- `docs/wiki/_Sidebar.md` + `Home.md` 更新（新ページ登録）
- [Operations — Tuning](../wiki/Operations-Tuning.md) に knob 7 つ追記
- [Architecture — Overview](../wiki/Architecture-Overview.md) 設計判断表に「control plane API は MCP/REST parity 対象外（管理面）」「control plane は aggregator/audit/billing 収集点（運用 deprovisioning 権威なし、local 一次）」追記
- [Plans — Roadmap](../wiki/Plans-Roadmap.md) + [multiverse-implementation-plan.md](multiverse-implementation-plan.md) の MV4 完了マーク
- [Operations — Multiverse Setup](../wiki/Operations-Multiverse-Setup.md) に control plane 連携節を追記
- **J1=A の SoT deviation 明示**: implementation-plan §MV4 の「usage counter（recall/remember/ingest 回数）」に対する PM 承認済み deviation として、本 stage が `route_resolution` telemetry に置き換えたこと + billing-grade は MV4.1 であることを docs に明記

## acceptance criteria

1. **default 不変**: `control_plane_url` 未設定で control_client は None、supervisor 既存挙動と完全一致。既存 suite 全緑 + 両 smoke green
2. **physics 層ゼロ接触**: `git diff <mv3-commit>..HEAD -- gaottt/core/gravity.py gaottt/core/scorer.py` が空（acceptance で assert）
3. **engine 非汚染**: `gaottt/` 内で `import asyncpg` が無い（acceptance で `grep` が空）
4. **`control/` 独立**: `control/pyproject.toml` が `gaottt` を依存に含まない（acceptance で `grep` が空）
5. **migration 冪等**: `run_migrations` を 2 回実行で 2 回目 no-op
6. **migration atomicity**: 意図的に壊れた migration で partial apply が無い（Codex B7）
7. **exact SQL boot**: real Postgres で `001_initial.sql` がそのまま実行できる（Codex B1 — index subquery 無し）
8. **host token 認証**: 正しい token 通過 / 不正 401 / revoke 後 401（permanent auth failure）/ path hid ≠ token host で 403
9. **admin key fail-fast**: 空 admin key で control plane 起動不可
10. **非 localhost 拒否**: `CONTROL_LISTEN_HOST` に外部アドレスで `SystemExit`
11. **degraded mode**: control plane 停止中も supervisor の `/route` / 宇宙作成/削除が成功継続、usage spool 蓄積 → 復旧で再送
12. **usage idempotency**: 同一 `batch_id` 再送で control 側が重複カウントしない（Codex B4）
13. **usage telemetry 命名**: `event_type='route_resolution'` で operation count ではないことが命名で明示（J1=A）
14. **local 一次 (J5)**: sync で矛盾時は local を正として control に報告
15. **control 削除 conflict detection**: control 側で削除された宇宙が local にある → WARNING log + sync 報告（即時削除しない）
16. **audit transactional**: tenants/universes/hosts の mutating endpoint で audit_log が同一トランザクション。audit INSERT 失敗で mutation rollback（J12 / Codex B5）
17. **SQL injection 耐性**: asyncpg parameterized query で text field が無害化（Codex missing #10）
18. **tenant mapping**: `control_default_tenant_id` 未設定で `tenant_id="default"`（J11）

## test strategy

- **unit (control/)**: 純粋関数（migrate parse/list / models validation）は docker 不要。DB 接続を伴うものは `@pytest.mark.requires_postgres` + skip-if-unavailable
- **unit (gaottt/)**: control_client は httpx MockTransport で wire protocol 検証（MV1 RemoteEmbedder パターン）。config knob は既存パターン
- **integration**: docker-compose の disposable Postgres + 実 supervisor で 9 シナリオ（degraded mode / permanent auth failure / idempotent replay / tenant mapping / audit transaction / supervisor status expose 含む）
- **smoke**: `rest_smoke.py` / `mcp_smoke.py` が default OFF で green（回帰 guard）
- **perf**: retrieval geometry に触れないので `tests/perf/` 実行不要

## 削除・スキップするテスト

なし。既存テストは一切変更しない（default 不変の回帰 guard）。

## assumption ledger（改訂、Codex review 反映）

| # | assumption | basis | falsification condition | blast radius |
|---|---|---|---|---|
| A1 | `asyncpg.Pool` が FastAPI lifespan で作成/破棄できる | asyncpg + FastAPI の標準パターン | 実装で pool lifecycle が FastAPI と噛み合わない | db.py 設計全体 |
| A2 | `gaottt/` が asyncpg を import しない構成で control_client を実装できる（httpx のみ） | J9（依存方向）+ control plane は HTTP API のみ提供する設計 | 実装で asyncpg 型や Postgres 固有機能が必要になる | 依存分離方針全体（MV1 成果無効化） |
| A3 | **(確定)** v1 は `/route` 解決回数を activity telemetry とし、billing-grade は MV4.1 で導入（PM 承認済み J1=A） | ユーザー承認 (A) + engine 非接触優先。metric 名 `route_resolution` で「operation count ではない」を明示 | ユーザーが差し戻しで (B) engine 改修 を指示 → high-risk 昇格 | usage 課金の精度（v1 は activity のみ） |
| A4 | `control/compose.yml` の Postgres が CI / ローカルで安定起動する | postgres:16-alpine の実績 | docker 利用不可環境でテスト skip が多発 | integration test カバレッジ |
| A5 | 番号付き plain SQL migration + bootstrap-aware runner で v1 の schema 変更需要をカバーできる | J4 + implementation-plan §MV4 | schema 変更が複雑化して runner 自作が維持困難 | migrate.py の将来負債 |
| A6 | **(確定)** control plane は supervisor と同一ホストの別プロセス（localhost 通信のみ） | J8 + ユーザー承認 (a) で現ブランチ継続 = 単一ホスト運用 | 別ホストに配置する場合 TLS + network 認証が追加必要（v1 範囲外） | デプロイモデル |
| A7 | control_client の spool 再送が FIFO 順序を保証できる（spool ファイル名 `{window_start_iso}_{batch_id}.jsonl` で window_start 昇順 sort、J10） | spool ファイル名に window_start timestamp を prefix して sort | disk full で spool 書き込み失敗が連続すると in-memory 保持のみ（順序は保たれるが durability 低下） | usage 課金の時系列正確性（低） |
| A8 | `asyncpg.Record` を dict に変換して Pydantic に渡す変換が性能上問題ない | API のトラフィック量は管理面程度（低） | 高トラフィックで pool 枯渇 / 変換 overhead 顕在化 | API 実装方式 |
| A9 | **(Codex B5 対応)** audit_log の同一トランザクション化が asyncpg で素直に書ける | `async with conn.transaction():` ブロック内で複数 INSERT は標準パターン | 実装でトランザクション境界が FastAPI dependency と衝突する | audit 完全性（J12） |
| A10 | **(Codex B4 対応)** `batch_id` (UUID4) の衝突確率が無視できる（2^122 space） | UUID4 の乱数性 | 衝突で冪等 skip が誤発動 → usage ロスト（確率的にほぼ起きない） | usage 課金の正確性 |

## risks

| リスク | 緩和 |
|---|---|
| asyncpg pool 接続失敗で control plane 起動不可 | lifespan で接続試行、失敗は明確なエラーメッセージ（DB URL / 認証 / Postgres 起動状況）|
| docker 未利用可能環境で integration test が全 skip | skip-if-unavailable で明示、純粋関数 unit test は必須実行 |
| supervisor 統合で MV3 のテストが壊れる | control_client は default None、WP-4 で「3 点セット未設定 = None」を徹底、既存 test は全て不変更で green を acceptance に |
| control plane と supervisor の API contract 不一致 | WP-2 で API を確定させ、WP-3 はその contract に対して test を書く。Codex review で契約整合性を検証 |
| usage spool が無限増殖（disk full） | spool ディレクトリに size 上限は将来課題。v1 は disk full で ERROR log（silent でない）+ in-memory counter 保持。運用で disk 監視（Codex B4）|
| permanent auth failure (401) で spool が無限増殖 | credential rotate まで spool 蓄積を続けるが、operator への通知（`/health` や log 監視）で早期検知を促す（Codex non-blocking #4）|
| host token leak（env / log） | `GAOTTT_CONTROL_HOST_TOKEN` を log に出さない（secret 扱い）。supervisor が backend を spawn する env にも継承させない |
| Postgres の timezone 設定差異 | 全 timestamp 列を `TIMESTAMPTZ` で統一、UTC で格納 |
| J1=A の SoT deviation が将来混乱を招く | docs（WP-5）+ implementation-plan 完了マークに **「v1 は route_resolution telemetry、billing-grade は MV4.1」** を明記。GaOTTT writeback にも記録 |

## WP 順序と依存関係

```
WP-1 (control/ 基盤: pyproject + schema + migrate + db + models)
   ↓ (API contract を WP-2 が定義するため直列)
WP-2 (control/ API: auth + CRUD + sync + audit transactional)
   ↓ (API contract 確定後に control_client を実装)
WP-3 (control_client + config knobs)
   ↓ (control_client を supervisor に統合)
WP-4 (supervisor 統合 + integration test)
   ↓ (実装確定後に docs)
WP-5 (docs)
```

**並列化しない理由**: high-risk かつ API contract 依存が連鎖するため（Codex plan review #1 も「WP-1/WP-2 は API/schema contract が安定するまで直列」「WP-3/WP-4 は wire contract 修正後に」と指摘）。各 WP 完了後に PM 検証（git status / diff / test 実行）。

## gate plan

| gate | 実施 | 理由 |
|---|---|---|
| GaOTTT recall | ✅ 済（MV4 直接ヒット弱、handover §10 + implementation-plan §MV4 を主ソース）| high-risk |
| Planning doc | ✅ 本ファイル（v2）| high-risk |
| Codex plan review | ✅ #1 REQUEST-CHANGES → v2 → #2 REQUEST-CHANGES（fix-then-delegate、B3/B5/B6/B7 RESOLVED + 新 blocking 4 件）→ v3 で全面反映 → #3 で最終確認 | high-risk |
| QA plan review | ✅ 必須（high-risk、新規 DB + 認証境界 + 監査完全性）| high-risk |
| Test-first delegation | ✅（WP-1〜WP-4 各 test-first）| 標準 |
| Codex test-diff review | ✅ 必須 | high-risk |
| QA test-diff review | ✅ 必須（degraded mode / permanent auth failure の user flow）| high-risk |
| Implementation delegation | ✅（各 WP）| 標準 |
| Test execution | ✅ | 標準 |
| PM diff inspection | ✅ | 標準 |
| Codex final diff review | ✅ 必須 | high-risk |
| QA final review | ✅ 必須 | high-risk |
| GaOTTT writeback | ✅ | high-risk |

## 仮登録予測（pre-registered predictions）

- WP-1 migration: `run_migrations` 2 回実行で 2 回目 no-op、**6 domain テーブル + schema_migrations** が作られる
- WP-1 migration atomicity: 意図的に壊れた第 2 migration で partial apply が無い（schema_migrations に第 2 が無い、DDL が無い）
- WP-1 exact SQL boot: real Postgres で `001_initial.sql` がそのまま実行できる（index subquery 無し、Codex B1 修正確認）
- WP-2 API: 空 admin key で `create_app` が RuntimeError、非 localhost bind で SystemExit
- WP-2 audit transactional: audit INSERT 失敗で mutation rollback
- WP-2 usage idempotent: 同一 batch_id 再送で 2 回目が冪等 skip
- WP-3 control_client: control 不可時に pull が None を返し、supervisor 既存機能が停止しない
- WP-3 permanent auth failure: 401 で以降 pull/push 停止（network error とは区別）
- WP-4 integration シナリオ 3 (degraded): control 停止中の `/route` が成功、spool 蓄積、復旧で再送されて `usage_events` に反映される
- WP-4 integration シナリオ 5 (default 不変): `control_plane_url` 未設定で既存 MV3 test が 1 件も壊れない
- WP-4 integration シナリオ 6 (idempotent replay): spool 再送で control 側が重複カウントしない
- full suite（control/ の docker 必須 test 除く）: 1019+ 件が 0 失敗

## Codex plan review #1 結果と反映ログ

**VERDICT: REQUEST-CHANGES**（7 blocking + 6 non-blocking + 10 missing tests + 2 scope concerns）

| Codex 指摘 | v2 での対応 | 反映箇所 |
|---|---|---|
| B1: Postgres index subquery は拒否される | `tenant_id` を `usage_events` に非正規化、index は単純 `(tenant_id, window_start)` | J4 / 001_initial.sql |
| B2: tenant mapping 未定義 | `control_default_tenant_id` config + `CreateUniverseBody.tenant_id` optional（J11）| J11 / WP-3 / WP-4 |
| B3: control 削除伝播が矛盾 | 「control 側削除の conflict detection（WARNING のみ）」に改名。権限モデル明文化（control = aggregator のみ）| J5 / 目標 / 非スコープ |
| B4: usage spool idempotency 不足 | `batch_id` (UUID) + `usage_batches` テーブル + temp/fsync/atomic rename + asyncio.Lock + disk full ERROR | J10 / WP-1 schema / WP-3 / acceptance |
| B5: audit best-effort は不適切 | mutating endpoint は同一トランザクションで domain + audit INSERT、失敗で rollback | J12 / WP-2 / acceptance |
| B6: J1 は SoT deviation | **PM 承認済み (A)**、metric 名 `route_resolution` に rename、billing-grade は MV4.1 と明示 | J1 / 非スコープ / WP-5 docs |
| B7: migration bootstrap chicken-and-egg | `ensure_bootstrap()` が `schema_migrations` を番号付き scan 前に作成 | J4 / WP-1 migrate.py |
| non-blocking #1: config 名不整合 | `db_pool_min_size` / `db_pool_max_size` に統一 | J6 / ControlConfig |
| non-blocking #2: テーブル数 | 「6 domain テーブル + schema_migrations」に訂正 | WP-1 unit test |
| non-blocking #3: host token compare_digest 記述 | hash → DB parameterized lookup に修正（admin key のみ compare_digest）| J3 / WP-2 auth |
| non-blocking #4: revoked token = degraded 不適切 | permanent auth failure に分離（ERROR log + spool quarantine + resend 停止）| WP-3 / シナリオ 4 |
| non-blocking #5: 「independent」の意味 | 「同一ホスト内の独立プロセス」と明記 | J8 / 目標 |
| non-blocking #6: record_event 呼び出しタイミング | `/route` が url/token 返却後に呼ぶ | WP-3 / WP-4 |
| missing #1: migration failure atomicity test | `test_run_migrations_failure_atomicity` 追加 | WP-1 unit test |
| missing #2: exact SQL boot test | `test_exact_sql_boot` 追加 | WP-1 unit test |
| missing #3: spool crash/idempotency test | `test_flush_usage_idempotent_replay` + シナリオ 6 | WP-3 / WP-4 |
| missing #4: disk full spool test | `test_spool_disk_full_error_observable` 追加 | WP-3 unit test |
| missing #5: concurrent record+flush+stop test | `test_record_event_concurrent_safe` 追加 | WP-3 unit test |
| missing #6: corrupt spool quarantine test | `test_quarantine_corrupt_spool` + `_quarantine_corrupt_spool` | WP-3 unit test |
| missing #7: token revoked vs down test | `test_pull_host_universes_401_permanent_auth_failure` + シナリオ 4 | WP-3 / WP-4 |
| missing #8: tenant mapping sync test | `test_tenant_mapping_sync` + シナリオ 7 | WP-2 / WP-4 |
| missing #9: audit transaction test | `test_audit_log_transactional_rollback` + シナリオ 8 | WP-2 / WP-4 |
| missing #10: SQL injection test | `test_sql_injection_*` 追加 | WP-2 unit test |
| scope concern: control 権限モデル混同 | 「aggregator/audit/billing 収集点（運用 deprovisioning 権威なし）」を明文 | 目標 / J5 / 非スコープ |

## Codex plan review #2 結果と反映ログ（v3）

**VERDICT: REQUEST-CHANGES（fix-then-delegate）** — B3/B5/B6/B7 は RESOLVED、B1/B2/B4 は PARTIALLY-RESOLVED、新 blocking 4 件 + 新 non-blocking 3 件

| Codex review #2 指摘 | v3 での対応 | 反映箇所 |
|---|---|---|
| B1 partial: schema load-order（usage_events が usage_batches より前で FK 解決不能） | `usage_batches` を `usage_events` の **前** に移動 | 001_initial.sql |
| B2 partial: default tenant FK 失敗 | `001_initial.sql` で `INSERT INTO tenants ('default', ...) ON CONFLICT DO NOTHING` で bootstrap | 001_initial.sql |
| B4 partial: `record_event` が sync なのに `asyncio.Lock` を主張（実装不能） | `record_event` を **async** に変更、`asyncio.Lock` を正しく使用、`/route` から `await record_event(...)` で呼ぶ | WP-3 ControlClient |
| 新 blocking #1: schema load-order | B1 と同一（usage_batches を先に）| 001_initial.sql |
| 新 blocking #2: default tenant bootstrap | B2 と同一（INSERT bootstrap）| 001_initial.sql |
| 新 blocking #3: record_event locking contract | B4 と同一（async 化）| WP-3 ControlClient |
| 新 blocking #4: permanent auth failure recovery 不明 | quarantine と pause の区別明記（corrupt = quarantine / 401 = pause with spool 蓄積継続）+ 復旧手順（token 再発行 → env 更新 → restart → spool 再送）+ supervisor status endpoint で expose | WP-3 / degraded mode / シナリオ 4/9 |
| non-blocking: event 名不整合（`universe_creator` vs `universe_create`） | `usage_events.event_type` は **原形**（`universe_create` 系）、`audit_log.action` は **過去形**（`universe_created` 系）と使い分け明記 | 001_initial.sql 注釈 |
| non-blocking: FIFO by batch_id は UUID4 で無意味 | spool ファイル名を `{window_start_iso}_{batch_id}.jsonl` にし `window_start` で sort | WP-3 `_write_spool_atomically` |
| non-blocking: `/sync` payload schema 未定義 | `local_universes` / `deleted_universes` の JSON schema + control 側 upsert/conflict 記録 semantics を明記 | WP-2 API |
| remaining gap: crash-after-POST-before-delete test | `test_flush_usage_crash_after_post_before_delete` 追加 | WP-3 unit test |
| remaining gap: batch insert before event insert | 同一トランザクションで `usage_batches` INSERT → `usage_events` INSERT の順を明記 | WP-2 API |
| remaining gap: MV3 regression for tenant_id optional | シナリオ 5 で MV3 `POST /admin/universes {owner_label}` が `tenant_id` 無しで従来通り動くことを確認 | WP-4 シナリオ 5 |
| remaining gap: /health で permanent auth failure expose | supervisor 管理面 status endpoint（`GET /admin/status` 等）で `auth_failed` / `spool_pending` を expose、シナリオ 9 で検証 | WP-3 / WP-4 シナリオ 9 |
