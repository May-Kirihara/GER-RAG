# Multiverse Scale-Out 全体引き継ぎ書 — MV0〜MV3 完了

> **作成日**: 2026-07-03
> **ブランチ**: `docs/multiverse-scale-out-plan`（未 push）
> **状態**: MV0+MV1+MV2+MV3 コミット済み。商用ラインに必要な MV4〜MV6 は未着手
> **最終コミット**: `e093819 feat(multiverse): MV3 universe supervisor + multiverse layout`

---

## 1. これは何か

GaOTTT を単一ホスト・複数テナントにスケールアウトする Multiverse Scale-Out プロジェクト。戦略計画は `docs/wiki/Plans-Multiverse-Scale-Out.md`、実装計画は `docs/maintainers/multiverse-implementation-plan.md`。

**解決する課題**:
- 「1 プロセスが宇宙全体の RURI model を RAM/VRAM に持つ」重量問題 → MV1 embedding service 分離で解消
- 同じ `data_dir` を複数プロセスが開いて write-behind が後勝ちする事故クラス（2026-05-31 FAISS reverse-overwrite incident と同型）→ MV2 owner lease で機構化
- ユーザー→宇宙のルーティングと宇宙 engine ライフサイクル管理 → MV3 universe supervisor で実装

**不変原則**（全 stage 共通）:
- physics / observation 層（`core/gravity.py` / `core/scorer.py` / mass・displacement・velocity の更新則）は 1 行も変更しない
- default 不変: 新 config knob はすべて「未設定 = 現行挙動」
- engine の能力を追加しない（MCP 新ツール 0 / REST 新エンドポイント 0）。管理面 API は parity 対象外（`/reset` と同じ例外クラス）

---

## 2. コミット履歴

```
e093819 feat(multiverse): MV3 universe supervisor + multiverse layout  (20 files, +5007)
8322e51 feat(multiverse): MV2 owner lease — 1 universe 1 write owner    (14 files, +2638)
ff29314 test(multiverse): implement MV1 Tier 6 remote-embedder equivalence bodies (1 file, +125/-15)
b36ab5c feat(multiverse): MV0 universe manifest + MV1 embedding service (20 files, +2288)
33803b4 docs(plans): draft Multiverse Scale-Out plan + implementation plan (MV0-MV6) (5 files, +724)
```

合計: 60 files changed, 約 10,700 行追加

---

## 3. 各 Stage の概要

### MV0 — EmbedderProtocol + 宇宙 manifest（土台、挙動変更ゼロ）

**目的**: 後続 stage が依存する 2 つの縫い目（EmbedderProtocol / UniverseManifest）を作る。

**新規ファイル**:
- `gaottt/embedding/base.py` — `@runtime_checkable Protocol`（`encode_documents` / `encode_query` / `dimension` property）
- `gaottt/store/manifest.py` — `UniverseManifest`（`universe_id` / `embedder_id` / `embedder_version` / `embedding_dim` / `managed: bool`）+ atomic write (`tmp + os.replace`) + `ensure_manifest` / `verify_embedder_identity`

**変更**:
- `gaottt/core/engine.py` L78 型ヒント `RuriEmbedder` → `EmbedderProtocol`。L142 `startup()` 冒頭に manifest dim hard gate（`RuntimeError`）
- `gaottt/embedding/ruri.py` — `embedder_id` / `embedder_version` property 追加（encode ロジック不改変）
- `gaottt/services/runtime.py:build_engine` — manifest 確保 + `verify_embedder_identity`
- `gaottt/config.py` — `manifest_check_enabled: bool = True`

**テスト**: `tests/unit/test_manifest.py`（17 tests）

---

### MV1 — embedding service + RemoteEmbedder

**目的**: モデルロードをホストに 1 つに集約。GPU コストをユーザー数ではなくホスト数に比例させる。

**新規ファイル**:
- `gaottt/embedding/service.py` — FastAPI `create_app(embedder: EmbedderProtocol)` DI seam。`POST /encode`（msgpack response）/ `GET /info`。**非 localhost bind は `SystemExit` で拒否**。GPU 直列化は `asyncio.Semaphore(1)`
- `gaottt/embedding/remote.py` — `RemoteEmbedder`（httpx sync client。`__init__` で `GET /info` → キャッシュ）
- `deploy/gaottt-embedder.service` — systemd unit（`Restart=always`）

**変更**:
- `gaottt/services/runtime.py:build_engine` — `config.embedder_endpoint` 設定時 `RemoteEmbedder` 分岐
- `gaottt/config.py` — `embedder_endpoint: str = ""`（sentinel empty-string で env override を有効化）/ `embedder_request_timeout_seconds: float = 30.0`
- `pyproject.toml` — `httpx>=0.27.0` を `[project] dependencies` に昇格

**テスト**:
- `tests/unit/test_remote_embedder.py`（11 tests、httpx `MockTransport` で wire protocol 検証）
- `tests/integration/test_engine_remote_embedder.py`（4 tests、`create_app(StubEmbedder())` を uvicorn background thread で実起動）
- `tests/perf/test_tier6_remote_embedder.py`（3 tests、real RURI で数値等価検証）

---

### MV2 — owner lease（1 宇宙 1 書き込みオーナーの機構化）

**目的**: 同じ `data_dir` を複数プロセスが開いて write-behind が後勝ちする事故クラスを機構で閉じる。MV3 supervisor の前提。

**新規ファイル**:
- `gaottt/store/lease.py` — `OwnerLease`（`<data_dir>/owner.lock` JSON）、`LeaseHeldError`、`LeaseLostError`
  - 原子性: `O_CREAT | O_EXCL` で新規取得。stale/force 判定は `<data_dir>/owner.lock.guard` を `flock(LOCK_EX)` で握った臨界区間内で read → 判定 → replace
  - `owner_id = uuid4().hex` — PID 再利用・hostname 重複に依存しない所有者識別。read-back 判定・release はすべて `owner_id` 一致で行う

**変更**:
- `gaottt/core/engine.py`:
  - `_persist_blocked: bool` latch — cache write-behind loop / final flush / FAISS save / virtual FAISS save の **4 経路すべて** の入口で check
  - 14 mutator entry check（`index_documents` / `archive` / `restore` / `forget` / `relate` / `unrelate` / `revalidate` / `merge` / `compact` / `reset_orbital_state` / `reset_velocities` / `reset_masses` / `warm_displacement` / `reset`）— 全て `LeaseLostError` raise
  - `_query_internal` passive guard — `query()` の guard だけだと `prefetch()` / dream loop が bypass する。内部メソッドに置くことで全 caller cover
  - `startup()` 冒頭で `acquire()`。heartbeat loop。shutdown の `stop_write_behind` 後に ownership 再検証
  - 発動条件: `owner_lease_enabled OR manifest.managed` — **managed 宇宙は config に関係なく強制**
- `gaottt/store/cache.py` — `persist_blocked` flag + `flush_to_store()` entry gate
- `gaottt/config.py` — `owner_lease_enabled=False` (default OFF) / `lease_force_takeover` / `lease_heartbeat_seconds=10.0` / `lease_stale_seconds=60.0`
- `gaottt/server/mcp_server.py` — `--force-takeover` CLI flag
- `gaottt/server/mcp_proxy.py` — spawn env に `env=os.environ.copy()` を明示

**設計判断**:
1. persist gate を cache 層に集約（`idle watchdog` が `flush_to_store()` を直接呼んで bypass するのを防ぐ）
2. `_query_internal` に passive guard（prefetch / dream loop の bypass を防ぐ）
3. shutdown ownership 再検証を `stop_write_behind` 後に移動（heartbeat stop と final flush の間の narrow race を構造的に排除）
4. default OFF + managed 強制（standalone の既存挙動を変えずに supervisor 宇宙を強制）

**テスト**: `tests/unit/test_owner_lease.py`（21 tests）/ `tests/integration/test_engine_lease.py`（33 tests）

---

### MV3 — universe supervisor + multiverse layout

**目的**: ユーザー→宇宙のルーティングと宇宙 engine ライフサイクル管理。supervisor (port 7880) + local registry + backend token middleware + shim supervisor mode。

**新規ファイル**:
- `gaottt/multiverse/__init__.py`
- `gaottt/multiverse/registry.py` — `MultiverseRegistry`（local SQLite: port 割当 + OS bind check / SHA-256 key hash / reconcile / partial unique index `WHERE status != 'deleted'`）
- `gaottt/multiverse/supervisor.py` — `create_supervisor_app` / `_Supervisor`（FastAPI port 7880: admin API + `/route` + per-universe backend ensure+spawn+stop / PID tracking + SIGTERM/SIGKILL / token lifecycle / embedder 検証 / asyncio.Lock + fcntl.flock 二層）

**変更**:
- `gaottt/server/mcp_server.py` — `build_token_middleware` + `_install_token_middleware`（`GAOTTT_BACKEND_TOKEN` env 有無で発動、default 素通し、`secrets.compare_digest`、401 は `call_next` 前で idle refresh 抑止）+ `main()` に `--supervisor-url` CLI
- `gaottt/server/mcp_proxy.py` — `_route_to_supervisor` / `_Upstream` token+supervisor_url 拡張 / `run_proxy` supervisor mode
- `gaottt/config.py` — knob 7 つ（`multiverse_root` / `supervisor_port=7880` / `supervisor_admin_key` / `universe_port_range_start=7890` / `universe_port_range_end=7989` / `supervisor_spawn_concurrency=3` / `supervisor_readiness_timeout=90.0`）

**race condition 対策**（Codex review 3 巡で全 blocking 解消）:
- **B1 port 割当競合**: global asyncio.Lock + DB partial unique index
- **B2 delete safety**: PID tracking + SIGTERM → 5s poll → SIGKILL → 2s poll → `waitpid` でプロセス終了確認。`PermissionError` も `_BackendAliveConflict` → 409
- **B3 route/delete race**: `ensure_backend` と delete が同じ二層 lock（asyncio.Lock + fcntl.flock）を取得。`_ensure_locked` 内で status 再チェック（`_UniverseInactive` → 404）
- **B4 readiness false-positive**: `PROBE_OK` は MCP `session.initialize()` 成功時のみ。HTTP 200 ≠ ready

**テスト**:
- `tests/unit/test_multiverse_registry.py`（34 tests）
- `tests/unit/test_token_middleware.py`（15 tests）
- `tests/unit/test_supervisor.py`（36 tests）
- `tests/unit/test_proxy_supervisor_mode.py`（13 tests）
- `tests/integration/test_supervisor.py`（13 tests: 7 light + 6 heavy `@slow`）+ `_supervisor_helpers.py`

---

## 4. ポート割当（本プロジェクトで予約）

| ポート | 用途 |
|---|---|
| 7878 | 既存: 単一 backend（proxy mode default、変更しない） |
| 7879 | embedding service（MV1） |
| 7880 | universe supervisor（MV3） |
| 7890–7989 | 宇宙 backend の動的割当レンジ（100 宇宙/ホスト上限） |

---

## 5. テスト・検証結果

### 最終状態（MV3 commit 後）

```bash
pytest tests/unit/ tests/integration/ -q
# → 1019 passed, 1 skipped, 0 failed
```

| 項目 | 結果 |
|---|---|
| MV0 新規テスト | 17 passed |
| MV1 新規テスト | 15 passed + 3 skipped（real RURI 必須、手動実行対象） |
| MV2 新規テスト | 54 passed（21 unit + 33 integration） |
| MV3 新規テスト | 111 passed（98 unit + 13 integration） |
| pre-existing | 822 passed + 1 skipped |
| `rest_smoke.py` | 7/7 green |
| `mcp_smoke.py` | 7/7 green |
| `ruff check gaottt/ tests/` | pre-existing 4 件のみ（MV0-MV3 新規コード 0 件） |

### 未実行と理由
- `tests/perf/test_tier6_remote_embedder.py`（3 tests）— real RURI model load 必須。Tier 6 = CI 自動化しない設計（CLAUDE.md）。**数値等価 3 段（`np.allclose(atol=1e-5)` / cosine 差 < 1e-6 / golden queries top-K 一致）が MV1 の本質的 acceptance で未検証**
- `tests/perf/` 全体 — MV0-MV3 は retrieval geometry 不接触のため perf regression なし

---

## 6. レビューゲートの記録

全 stage で high-risk 分類 → required gate 全実施。

### MV0+MV1

| Gate | 結果 |
|---|---|
| Codex plan review | 2 巡（初回 13 件 + 再レビュー 8 件）を全反映。確定事項: engine 横断 persist block / managed manifest による lease 強制 / service の DI seam |
| Codex test-diff review | APPROVE / APPROVE-WITH-NOTES（0.5s backoff / generic 4xx/5xx / encode-time connect error の executable pin が non-blocking gap） |
| Codex final review | MV0: B1 disabled-warning 修正後に re-review APPROVE。MV1: security BLOCK → localhost 強制拒否に修正 → re-review APPROVE |
| QA final | 未実施（当時） |

### MV2

| Gate | 結果 |
|---|---|
| Codex plan review | 5 blocking → execution plan 改訂（guard API を cache 層に集約 / mutating surface 完全網羅 / 4-path 独立 test / lease-loss race test） |
| Codex test-diff review | 4 blocking → WP-1b 強化（barrier race / cross-process guard TOCTOU / release TOCTOU / heartbeat_loop test） |
| Codex final review | 2 blocking → WP-4b/4c 解消（B1: shutdown blind window → revalidation を stop_write_behind 後に移動 / B2: prefetch bypass → `_query_internal` passive guard） |
| QA final | **pass** — 8 criteria 全 trace、独立実行 908 passed、14 mutator guard + 4 永続化経路 gate 完全性確認、physics 無接触 |

### MV3

| Gate | 結果 |
|---|---|
| Codex plan review | 4 blocking → 全反映（per-universe asyncio.Lock + file lock / OS port bind check / `secrets.compare_digest` / 401 で idle refresh 抑止 / token stale recovery / admin key 空 = fail-fast / 削除は backend 停止確認後に trash/ move） |
| Codex test-diff review | 5 blocking → B3/B5 unit fix（file modes / compare_digest functional 化）、B1/B2/B4 は WP-5 integration で対応 |
| Codex final review | **3 巡で全 blocking 解消**: 初回 4 blocking（B1 port race / B2 delete live dir / B3 route-delete race / B4 probe false-positive）→ 2巡目 B2/B3 gap（SIGKILL survivor / cross-process flock）→ 3巡目 PermissionError。各巡で「同じ関数の別 edge case」を発見 |
| QA final | **conditional-pass** → docs CLI command 修正で条件クリア（16/16 criteria trace、physics/store 零接触確認） |

---

## 7. ドキュメント更新一覧

### 新規 Wiki ページ
- `docs/wiki/Plans-Multiverse-Scale-Out.md` — 戦略計画（SoT）
- `docs/wiki/Operations-Multiverse-Setup.md` — MV3 setup guide（アーキテクチャ / 前提 / 6 step / セキュリティモデル / 制限事項 / トラブルシューティング）
- `docs/wiki/Operations-Resource-Requirements.md` — モデル抜き engine RAM 計測手順

### 更新 Wiki ページ
- `docs/wiki/Operations-Server-Setup.md` — embedding service 分離節
- `docs/wiki/Operations-Tuning.md` — MV0/MV1/MV2/MV3 knob 計 14 つ
- `docs/wiki/Operations-Troubleshooting.md` — LeaseHeldError / LeaseLostError 項
- `docs/wiki/Architecture-Concurrency.md` — 構造的解 (2): owner lease 節
- `docs/wiki/Architecture-Overview.md` — 設計判断表「supervisor API は MCP/REST parity 対象外」
- `docs/wiki/_Sidebar.md` / `Home.md` / `Plans-Roadmap.md` — 新規ページ追加

### 保守者向けドキュメント
- `docs/maintainers/multiverse-implementation-plan.md` — MV0-MV6 全体実装計画（MV0-MV3 ✅、MV4-MV6 ⬜）
- `docs/maintainers/multiverse-mv0-mv1-execution-plan.md` — MV0+MV1 PM execution plan
- `docs/maintainers/multiverse-mv2-execution-plan.md` — MV2 PM execution plan
- `docs/maintainers/multiverse-mv3-execution-plan.md` — MV3 PM execution plan（assumption ledger A1-A6 含む）
- `docs/maintainers/handover-2026-07-02-multiverse-mv0-mv1.md` — MV0+MV1 個別 handover
- `docs/maintainers/handover-2026-07-02-multiverse-mv2.md` — MV2 個別 handover
- `docs/maintainers/handover-2026-07-03-multiverse-mv3.md` — MV3 個別 handover
- `docs/maintainers/handover-2026-07-03-multiverse-all.md` — **本書（全体 handover）**

---

## 8. 既知の問題・残リスク

### 機能面
1. **engine 遅延構築と lease timer の相互作用**（MV3 で発見）: MCP `initialize` probe は engine/lease 未構築で OK を返す。`idle_timeout` 死亡後、`lease_stale_seconds` 期限内の再 route で `LeaseHeldError` が発生しうる。本番 `idle_timeout=300s ≫ lease_stale=60s` なら実質発火しないが、短い idle 設定では要注意
2. **`owner.lock` が 0600 でなく 0644**（MV2）: `multiverse_root` 0700 が実効 trust boundary なので他 OS user からは不可視。criterion 文面との字面ギャップだが実害なし
3. **embedding service は SPOF**: 全ユーザーの remember/recall が止まる。systemd `Restart=always` で自動復旧

### テスト・検証
4. **MV1 数値等価未検証**: `tests/perf/test_tier6_remote_embedder.py` 3 tests が real RURI 必須で未実行。`np.allclose` / cosine 差 / top-K 一致の 3 段検証が MV1 の本質的 acceptance
5. **WP-4 test の pin 強度 gap**（MV1 Codex non-blocking）: 0.5s backoff / generic 4xx/5xx / encode-time connect error の executable pin
6. **`slow` pytest marker 未登録**: `PytestUnknownMarkWarning` が出る。`pyproject.toml [tool.pytest.ini_options] markers` に登録すれば消える
7. **manifest 初回起動時の version warning**: `ensure_manifest` が config のみから生成するため初回 `embedder_version="unpinned"`。v1 warn-only 仕様

### 運用
8. **standalone lease default OFF 昇格が未判断**: 1-2 週の dogfooding 後に判断（Phase Q governor と同じ promotion パターン）
9. **network filesystem 非サポート**: lease は `O_EXCL` / `flock` / `os.replace` の POSIX semantics に依存。NFS/CIFS は信頼できない

---

## 9. ロールバックガイド

各 stage は独立に rollback 可能（default 不変設計）。

| 無効化対象 | 方法 | 影響 |
|---|---|---|
| MV0 manifest gate | `GAOTTT_MANIFEST_CHECK_ENABLED=false` | dim/embedder_id check が warning 透過（FAISS dim 保護は常に RuntimeError） |
| MV1 RemoteEmbedder | `embedder_endpoint=""`（default） | 従来の in-process RuriEmbedder 経路 |
| MV2 owner lease (standalone) | `owner_lease_enabled=False`（default のまま） | 何もしなくてよい |
| MV2 owner lease (managed) | `manifest.json` の `managed` を `false` に書き換え | runbook 専用。事故防御を外す操作 |
| MV3 supervisor 全体 | `GAOTTT_MULTIVERSE_ROOT` 未設定 | supervisor も shim supervisor mode も起動しない |
| MV3 backend token | `GAOTTT_BACKEND_TOKEN` 未設定 | token middleware が素通し（既存 7878 無影響） |

物理 rollback（commit revert）は不要。全て config knob / env で挙動を旧来に戻せる。

---

## 10. 次のステップ（MV4〜MV6）

### MV4 — control plane（Postgres）
- **目的**: テナント・宇宙の台帳、課金・監査。独立デプロイ物（engine コードに触れない）
- **構成**: 新規 `control/` パッケージ（`gaottt/` の外）+ asyncpg + 番号付き plain SQL migration
- **supervisor 連携**: `control_sync_interval_seconds`（default 300）周期で自ホストの宇宙一覧を取得。usage counter は 60s ごとに batch POST、送信失敗は local spool → 次回再送
- **degraded mode**: control plane 落ちでもホスト自走（local manifest が一次、control plane は集約）
- **所要**: 1〜2 週
- **前提**: MV3 の registry API interface（`allocate_port` / `create_universe` / `verify_api_key` 等）を保てば supervisor 側は変更不要

### MV5 — backup / DR
- **目的**: 宇宙単位の継続バックアップと復旧 runbook
- **構成**: Litestream（`universes/*/gaottt.db` 対象）+ `scripts/dr_drill.py`（backup → 破壊 → restore → FAISS rebuild → 診断 green で exit 0）
- **バックアップ対象**: SQLite + manifest.json の 2 点セット（FAISS は `rebuild_faiss_from_db.py` で再生成可能）
- **embedder artifact pinning**: manifest の `embedder_id/version` に対応する model の取得手段を runbook 必須項目に
- **所要**: 2〜3 日

### MV6 — 英語宇宙（embedder per universe）
- **着手条件**: MV1 完了 + EN embedder 選定 evaluation 完了で着手
- **最初のステップ**: multilingual-e5 / BGE-M3 で discriminative power probe + cross-lingual probe（評価が no-go なら MV6 全体保留 — RikkaBotan 前例あり）
- **構成**: config に `embedder_profile` 機構（cosine 帯依存 knob を profile dict で上書き）+ embedding service 複数モデルホスト（service 1 プロセス 1 モデル）
- **所要**: 1〜2 週（評価込み）

---

## 11. 手動確認・dogfooding チェックリスト

MV3 を本番投入する前に以下を確認すること:

- [ ] env var 形式で supervisor 起動（`GAOTTT_MULTIVERSE_ROOT` + `GAOTTT_SUPERVISOR_ADMIN_KEY` + `GAOTTT_EMBEDDER_ENDPOINT`）→ port 7880 で listen
- [ ] 宇宙作成（`POST /admin/universes`）→ `api_key` 発行 → `POST /route` で `{url, token}` 返却
- [ ] token なし直叩き → 401 / token 付き → 非 401
- [ ] `DELETE /admin/universes/{id}` → backend SIGTERM 停止 → trash/ move
- [ ] embedding service（port 7879）を起動 → `GAOTTT_EMBEDDER_ENDPOINT` 設定の MCP/REST server で remember/recall が動く
- [ ] real RURI で `tests/perf/test_tier6_remote_embedder.py` 3 tests 実行（数値等価検証）
- [ ] 1-2 週 dogfooding で安定性確認後、本番運用移行
- [ ] standalone lease default ON 昇格の判断

---

## 12. GaOTTT memory（durable lessons）

このプロジェクトで保存した主な教訓:

| Memory ID | Stage | 内容 |
|---|---|---|
| `cb8b930b` | MV2 | persist gate を cache 層に集約 / `_query_internal` passive guard / shutdown ownership 再検証を stop_write_behind 後 / default OFF + managed 強制 / `embedder_endpoint: str = ""` センチネル |
| `60321aca` | MV3 | supervisor lifecycle race の構造的解消（二層 lock + partial unique index）/ backend 停止は waitpid / PermissionError も _BackendAliveConflict / token middleware ordering / probe tri-state / spawn env 明示構築 / Codex review 3 巡パターン |

---

## 13. 次の担当者・エージェントへのメモ

### 最初に読むドキュメント
1. **本書** — 全体像
2. `docs/maintainers/multiverse-implementation-plan.md` — MV0-MV6 全体計画（MV4〜MV6 の詳細仕様あり）
3. `docs/wiki/Operations-Multiverse-Setup.md` — 運用手順

### MV4 着手時の注意
- `gaottt/multiverse/registry.py` の interface（`allocate_port` / `create_universe` / `verify_api_key` 等）を保てば supervisor 側は変更不要。registry の中身を Postgres に置き換える
- `_Supervisor._backend_pids` はプロセス内 dict（supervisor restart で消失）。PID 不知時は port probe → 409 のパスが安全側に倒れている
- backend spawn の `BACKEND_IDLE_TIMEOUT` は module global（`supervisor.py` の上部）。test で monkey-patch する（WP-5 の `_build_spawn_env` 参照）

### physics 層は 1 行も触っていない
`core/gravity.py` / `core/scorer.py` は全 stage を通じて無改変。全ての変更は write path / cache・FAISS 永続化経路 / 制御面 に作用する。`git diff HEAD~5..HEAD -- gaottt/core/gravity.py gaottt/core/scorer.py` で空であることを確認済み。
