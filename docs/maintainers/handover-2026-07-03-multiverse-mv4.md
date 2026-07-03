# 引き継ぎメモ — MV4 Control Plane (Postgres) + Supervisor 連携

## ステータス
- 状態: 完了（Codex final review fix-then-deliver → fix loop #1 → APPROVE-WITH-NOTES / deliver、QA **pass**）
- 日付: 2026-07-03
- 担当: PM (orchestrator) + implementer subagents (WP-1〜WP-5 + FIX-1) + QA subagent
- ブランチ: `docs/multiverse-scale-out-plan`（未 push、ローカルで5コミット先行）
- コミット: `d93117f feat(multiverse): MV4 control plane (Postgres) + supervisor integration`（32 files, +7294）
- 概要: テナント・宇宙の台帳・課金・監査の集約点として **同一ホスト内の独立プロセス（localhost 通信のみ）** で動く control plane を新設。engine コード（`gaottt/core/`）は一切接触、asyncpg は gaottt/ に import させない依存分離。default 不変（3-point gate）。

## 変更内容

### 新規ファイル（control/ 独立パッケージ、全 17 files）
- `control/pyproject.toml` — hatchling / asyncpg・fastapi・uvicorn・pydantic 依存（**gaottt 非依存**、J9）
- `control/compose.yml` — disposable Postgres（postgres:16-alpine、port 55432）
- `control/control/schema/001_initial.sql` — 7 domain テーブル（tenants[+default bootstrap INSERT] / users / hosts / universes / usage_batches / usage_events / audit_log）。**FK load-order: usage_batches before usage_events**（Codex review #2 blocking 対応）。`schema_migrations` は runner が bootstrap
- `control/control/migrate.py` — bootstrap-aware 番号付き SQL migration runner（`ensure_bootstrap` → `run_migrations`、**1 file = 1 transaction** atomicity）
- `control/control/db.py` — asyncpg pool（`SELECT 1` probe fail-fast）
- `control/control/config.py` — `ControlConfig`（CONTROL_DATABASE_URL / CONTROL_ADMIN_KEY 空=fail-fast / CONTROL_LISTEN_HOST 非 localhost=SystemExit / CONTROL_LISTEN_PORT=7881）
- `control/control/models.py` — Pydantic v2 domain + request body models
- `control/control/api.py` — `create_app` + 13 endpoints（tenants/users/hosts CRUD / universes register+logical delete / host universes / **idempotent usage batch** / sync conflict detection / **rotate-token** / health）。**audit log 同一トランザクション**（J12）。全 text field は asyncpg parameterized query（SQL injection 耐性）
- `control/control/auth.py` — `make_admin_checker`（`secrets.compare_digest`）/ `make_host_checker`（SHA-256 hash → DB parameterized lookup、revoked_at IS NULL、path hid ≠ token host で 403）
- `control/control/__main__.py` — `python -m control` entry（uvicorn run）
- `control/tests/` — conftest.py（`CONTROL_TEST_POSTGRES_HOST_PORT` env で port override、standalone compose file 生成）+ test_migrate.py / test_models.py / test_db.py / test_api.py（76 tests）
- `control/README.md` — control パッケージ概要（English、※scope 文が WP-1 時代で stale、QA non-blocking）

### 新規ファイル（gaottt/ 側）
- `gaottt/multiverse/control_client.py`（736 行）— `ControlClient`（async `arecord_event` [asyncio.Lock] / `flush_usage` [idempotent spool: batch_id UUID4 + temp/fsync/atomic rename + window_start 昇順 FIFO replay] / `pull_host_universes` / `reconcile_with_control` [J5 local 一次] / `auth_failure_state` / `start`+`stop` lifecycle [background replay task]）。**degraded mode**: network error で local 継続 + spool 蓄積 → 復旧で再送。**permanent auth failure**（401）: POST 試行停止 + spool 蓄積継続 + same-host token rotation で回復
- `tests/unit/test_control_client.py` — 17 tests（httpx MockTransport + tmp aiosqlite、docker 不要）
- `tests/integration/test_control_integration.py` — 10 scenarios（docker 必須、port 55433 で native 実行）
- `docs/wiki/Operations-Control-Plane.md` — MV4 control plane setup guide（アーキテクチャ / 前提 / 5 step / API リファレンス 12 endpoints / usage telemetry の意味 [J1=A] / degraded mode / permanent auth failure + 復旧手順 / 制限事項 / トラブルシューティング）
- `docs/maintainers/multiverse-mv4-execution-plan.md` — PM execution plan（v3、assumption ledger A1-A10 + Codex review #1/#2 反映ログ + J1-J12 設計判断）

### 変更ファイル
- `gaottt/config.py` — knob 7 つ追加（`control_plane_url` / `control_host_id` / `control_host_token` [SECRET] / `control_default_tenant_id` / `control_sync_interval_seconds=300.0` / `usage_push_interval_seconds=60.0` / `usage_spool_dir`）。**3-point gate**（URL + host_id + token 全て設定時のみ有効、未設定 = inert）
- `gaottt/multiverse/supervisor.py` — `_Supervisor.__init__(..., control_client=None)` / `create_supervisor_app(..., control_client=None)` / lifespan で `start()`+`stop()` / `/route`・create/delete handler で guarded `arecord_event`（`if sup._control is not None`）/ `GET /admin/status` で `auth_failure_state` expose / `CreateUniverseBody.tenant_id: str | None = None`（MV3 互換）/ `_main()` で 3-point gate check → ControlClient 構築 or None
- `gaottt/multiverse/__init__.py` — docstring 更新（MV4 言及）
- `docs/wiki/Operations-Tuning.md` — MV4 knob 7 つ
- `docs/wiki/Architecture-Overview.md` — 設計判断表 3 行追加（parity 対象外の延長 / aggregator・local 一次・J5 / J1=A route_resolution telemetry）
- `docs/wiki/_Sidebar.md` / `Home.md` / `Plans-Roadmap.md` — Operations-Control-Plane 追加
- `docs/wiki/Operations-Multiverse-Setup.md` — control plane 連携節
- `docs/maintainers/multiverse-implementation-plan.md` — MV4 ✅ 完了マーク

## 変更理由
MV3 で 1 ホスト・複数テナントの宇宙運用が可能になったが、**テナント・宇宙の台帳（誰の宇宙がどこで動いているか）・課金データの集約・監査ログ** が local SQLite registry のみで、ホストをまたいだ可視性・集計・監査証跡がなかった。MV4 はこれを **aggregator / audit / billing 収集点** として独立プロセス（Postgres + asyncpg + FastAPI）で新設する。control plane は **運用 deprovisioning 権威ではなく aggregator 限定**（J5: local manifest が運用の一次）。engine 非接触・default 不変を保ち、degraded mode（control 不可でもホスト自走）で可用性を確保。

## Work packages

| WP | Scope | Status | Files | Verification |
|---|---|---|---|---|
| WP-1 | control/ 基盤（pyproject + schema + migrate + db + models） | done-with-notes | control/ 全 17 files | docker-free 32 tests green / DB-backed 7 tests は port 55432 占有で skip（独立検証 5/5） |
| WP-2 | control/ API（auth + CRUD + sync + audit transactional + SQL injection 耐性）+ conftest port override | done | control/control/{api,auth,__main__,__init__,models}.py, control/tests/{conftest,test_api}.py, control/README.md | 76 tests green（port 55433 native） |
| WP-3 | control_client + config knobs 7 | done | gaottt/config.py, gaottt/multiverse/control_client.py, gaottt/multiverse/__init__.py, tests/unit/test_control_client.py | 16 new tests green + 663 regression green |
| WP-4 | supervisor 統合 + 9 integration scenarios | done | gaottt/multiverse/supervisor.py, tests/integration/test_control_integration.py | 9 scenarios green + MV3 regression 96 green + full 1044 passed |
| WP-5 | docs | done | docs/wiki/Operations-Control-Plane.md + Tuning + Sidebar/Home + Architecture + implementation-plan + Multiverse-Setup | docs review green |
| FIX-1 | Codex final review B1 (rotate-token endpoint) + B2 (start() background replay) | done | control/control/api.py, gaottt/multiverse/control_client.py, tests/{unit/integration}, docs/wiki/Operations-Control-Plane.md | +2 tests、full 1046 passed |

## 触ったファイル
- 実装: control/control/{config,db,migrate,models,api,auth,__main__,__init__}.py, control/control/schema/001_initial.sql, control/compose.yml, control/pyproject.toml, gaottt/config.py, gaottt/multiverse/{control_client,supervisor,__init__}.py
- テスト: control/tests/{conftest,test_migrate,test_models,test_db,test_api}.py, tests/unit/test_control_client.py, tests/integration/test_control_integration.py
- docs: docs/wiki/{Operations-Control-Plane,Operations-Tuning,Architecture-Overview,Home,_Sidebar,Plans-Roadmap,Operations-Multiverse-Setup}.md, docs/maintainers/{multiverse-implementation-plan,multiverse-mv4-execution-plan}.md, control/README.md, docs/maintainers/handover-2026-07-03-multiverse-{mv4,all}.md

## テスト
- `CONTROL_TEST_POSTGRES_HOST_PORT=55433 pytest tests/unit/ tests/integration/ -q` → **1046 passed, 1 skipped, 0 failed** (107.94s)
- `pytest control/tests/ -q` → **76 passed** (11.05s)
- `pytest tests/integration/test_supervisor.py tests/unit/test_supervisor.py -q` → **96 passed**（MV3 回帰、unchanged）
- `rest_smoke.py` / `mcp_smoke.py` → 7/7 green（default OFF で回帰 guard）
- `ruff check gaottt/multiverse/supervisor.py gaottt/multiverse/control_client.py tests/integration/test_control_integration.py` → All checks passed!（pre-existing 4 件は CLAUDE.md 記載通り無視）
- **engine 非汚染**: `grep -rn "import asyncpg" gaottt/` → empty / `git diff HEAD -- gaottt/core/` → empty
- **依存分離**: `grep "gaottt" control/pyproject.toml` → empty
- 未実行: `tests/perf/`（retrieval geometry 不接触のため不要）

## ドキュメント
- 新規 `Operations-Control-Plane.md`: setup guide（アーキテクチャ図 / 前提 / 5 step / ControlConfig env 7 つ / API リファレンス 12 endpoints / **usage telemetry の意味 [J1=A]** / degraded mode / permanent auth failure + 復旧手順 / 制限事項 / トラブルシューティング）
- `Architecture-Overview.md`: 設計判断表 3 行（control plane API も parity 対象外 / aggregator・audit・billing 収集点・local 一次・J5 / J1=A route_resolution telemetry）
- `Operations-Tuning.md`: MV4 knob 7 つ
- `Operations-Multiverse-Setup.md`: control plane 連携節
- `_Sidebar.md` / `Home.md` / `Plans-Roadmap.md`: Operations-Control-Plane 追加
- `multiverse-implementation-plan.md`: MV4 ✅ 完了マーク（詳細サマリー付き）

## 手動確認（dogfooding）
- [ ] `docker compose -f control/compose.yml up -d` → `python -m control.migrate` が `applied 1 migration(s): ['001']` を返す
- [ ] `CONTROL_ADMIN_KEY=$(openssl rand -hex 16) python -m control` が `/health` で `{"status":"ok"}` を返す（port 7881）
- [ ] `POST /admin/hosts {label}` → 平文 token を一度だけ受取 → `GAOTTT_CONTROL_PLANE_URL` + `GAOTTT_CONTROL_HOST_ID` + `GAOTTT_CONTROL_HOST_TOKEN` で supervisor 起動 → `/route` 後に control 側 `usage_events` に `event_type='route_resolution'` 行が出現
- [ ] **degraded mode**: control plane 停止中も supervisor の `/route`・宇宙作成/削除が成功継続、usage spool 蓄積 → control 復旧で再送で `usage_events` 反映
- [ ] **permanent auth failure**: `DELETE /admin/hosts/{hid}` → 401 → ERROR log → `GET /admin/status` が `auth_failed: true` → `POST /admin/hosts/{hid}/rotate-token` で新 token → env 更新 → supervisor restart → spool drain
- [ ] **default 不変**: `GAOTTT_CONTROL_PLANE_URL` 未設定で supervisor が MV3 完全一致で動く（`/admin/status` が `{"control": null}`）

## 既知の問題
1. **control plane も SPOF**（MV3 と同様）: Postgres + control app が落ちると usage telemetry と audit が停止。ただし **degraded mode で supervisor の `/route`・宇宙作成/削除は local registry で完結**（機能停止しない）。systemd `Restart=always` で自動復旧
2. **usage は activity telemetry で billing-grade ではない**（J1=A）: `route_resolution` は `/route` 解決回数で operation count ではない。proxy reconnect で過小カウントしうる。課金の正確な根拠としては使えない（MV4.1 で billing-grade 導入）
3. **`control/README.md` が WP-1 時代の記述で stale**: scope 文が「WP-2/WP-3/WP-4 は後で提供」と書いたまま。operator 向け `Operations-Control-Plane.md` は正確・完全なので user flow への影響はないが、`control/` を直接触る developer が混乱しうる
4. **`requires_postgres` marker が root `pyproject.toml` に未登録**: `PytestUnknownMarkWarning` 9 件。機能上の問題なし（marker は `pytest_configure` で動的登録）。1 行追加で解消
5. **`ControlClient.start()` の idempotency**: 2 回呼ぶと task leak。supervisor lifespan は1回のみ呼ぶので実害なし。将来 guard 推奨
6. **`/route` の `arecord_event` に defensive try/except なし**: `arecord_event` は O(1) dict update で raise しない設計だが、明示的な不変量化を推奨
7. **`uv.lock` をコミットから意図的に除外**: `.gitignore` L101 はコメントアウト（include 推奨）だが、既存 repo が未管理のため今回も追随
8. **port 55432 が環境で占有される可能性**: `infra-postgres-1` 等の別プロセスが持っていると DB-backed test が skip する。`CONTROL_TEST_POSTGRES_HOST_PORT=<free>` で回避（conftest が standalone compose file を生成）

## 残TODO
- **MV4.1（billing-grade usage）**: backend が MCP notification で supervisor に usage を報告 → supervisor が control plane に batch POST。J1=A で先送りした正確な recall/remember/ingest operation count の導入（3〜5 日）
- MV5（backup / DR）— Litestream + runbook + DR drill。注意: MV4 の Postgres 側 `usage_batches`/`usage_events`/`audit_log` は Litestream 対象外（Postgres のバックアップは別途）
- MV6（英語宇宙）— embedder per universe
- non-blocking 7 件の整理（README stale / marker 登録 / start() idempotency guard / `/route` defensive try-except / uv.lock 方針）
- `requires_postgres` marker の root `pyproject.toml` 登録

## リスク
- **localhost only**: control plane は同一ホスト内の独立プロセス前提（J8）。別ホストに配置する場合 TLS + network 認証が追加必要（v1 範囲外）
- **単一 default tenant**: `control_default_tenant_id` 未設定なら `"default"`。multi-tenant 拡張時は `CreateUniverseBody.tenant_id` で明示指定が必要
- **usage 課金精度**: `route_resolution` telemetry は activity の目安。proxy reconnect で過小カウント、idle respawn で過小カウントしうる。billing-grade は MV4.1
- **spool 無限増殖**: permanent auth failure 中や disk full で spool が蓄積し続ける。operator が `/admin/status` の `spool_pending` と disk 使用量を監視する必要
- **Postgres timezone**: 全 timestamp 列を `TIMESTAMPTZ` で UTC 統一済みだが、Postgres 側の timezone 設定差異には注意

## ロールバックメモ
- `GAOTTT_CONTROL_PLANE_URL` 未設定（default）→ ControlClient が構築されず、supervisor は **MV3 完全一致挙動**（3-point gate）。control plane プロセス自体を止めても同じ
- 物理 rollback（commit revert）は不要。config knob / env で機能を完全に無効化できる
- 万が一 control plane DB が壊れた場合: supervisor は degraded mode で継続（usage は spool 蓄積）。control plane 側は `docker compose down && docker compose up -d` で disposable 再構築 → `python -m control.migrate` で schema 再適用。**ただし台帳データ（tenants/universes/usage/audit）は消失** — 本番 Postgres は別途 backup 必須（MV5 scope）

## 次の担当者・エージェントへのメモ

### MV4 完了後の注意点
- **registry interface は MV3 のまま**: MV4 は registry の中身を Postgres に置き換えたのではなく、**`control/` を独立パッケージとして追加し、supervisor が `control_client` 経由で同期・usage telemetry を送る**設計。registry は引き続き local SQLite（MV3 互換）。control plane 側の universes テーブルは aggregator で、local が一次（J5）
- **J1=A を踏襲する**: usage telemetry は `route_resolution` で operation count ではない。billing-grade が必要なら MV4.1（backend MCP notification）で別途導入。docs 5 箇所に明記済み
- **permanent auth failure の回復**: `DELETE /admin/hosts/{hid}` 後の回復は **`POST /admin/hosts/{hid}/rotate-token`**（same host_id で token_hash 更新 + revoked_at clear）。**新 host 登録（`POST /admin/hosts`）ではダメ** — host_id が変わり universes FK が orphan する（Codex final review B1 で発見・修正済み）
- **`ControlClient.start()` の replay は background**: control 不可時に supervisor 起動を block しない（Codex B2 で修正済み）。ただし `start()` の idempotency は未 guard（2 回呼ぶと task leak、supervisor lifespan は1回のみ呼ぶので実害なし）
- **audit log は同一トランザクション**: mutating endpoint で domain mutation + audit INSERT が同一 txn。audit INSERT 失敗 = mutation rollback（J12 / Codex B5）
- **migration runner の bootstrap**: `schema_migrations` テーブルは `ensure_bootstrap()` が番号付きファイル scan より前に作成（chicken-and-egg 回避、Codex B7）。`001_initial.sql` には含まれない
- **event_type と audit_log.action の使い分け**: `usage_events.event_type` は **原形**（`route_resolution` / `universe_create` 等）、`audit_log.action` は **過去形**（`universe_created`d / `host_registered` / `host_token_rotated` 等）

### MV4.1 着手時の注意
- backend 側に usage counter 機構を足す必要がある（recall/remember/ingest 回数）。MCP notification で supervisor に報告 → 既存の spool 機構（batch_id idempotent）で control plane に batch POST
- `usage_events.event_type` に `recall` / `remember` / `ingest` を追加。`route_resolution` は activity proxy として残すか廃止するかは PM 判断
- backend が MCP notification を送る経路の設計が core（supervisor ↔ backend は HTTP /route 経路のみ。新たな push channel が必要）

### physics 層は 1 行も触っていない
`core/gravity.py` / `core/scorer.py` は MV4 でも無改変。`git diff e093819..d93117f -- gaottt/core/gravity.py gaottt/core/scorer.py` で空であることを確認済み（e093819 = MV3 commit、d93117f = MV4 commit）。
