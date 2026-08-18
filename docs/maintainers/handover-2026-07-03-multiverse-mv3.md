# 引き継ぎメモ — MV3 Universe Supervisor + Multiverse Layout

## ステータス
- 状態: 完了（QA conditional-pass → docs 修正で条件クリア）
- 日付: 2026-07-03
- 担当: PM (orchestrator) + implementer subagents + QA subagent
- ブランチ: `docs/multiverse-scale-out-plan`（未 push）
- 概要: ユーザー→宇宙のルーティングと宇宙 engine ライフサイクル管理。supervisor (port 7880) + local registry + backend token middleware + shim supervisor mode

## 変更内容

### 新規ファイル
- `gaottt/multiverse/__init__.py` — multiverse ops package
- `gaottt/multiverse/registry.py` — MultiverseRegistry（local SQLite: port 割当 / key hash / reconcile / partial unique index）
- `gaottt/multiverse/supervisor.py` — create_supervisor_app / _Supervisor（FastAPI port 7880, admin API, /route, per-universe backend ensure/spawn/stop, PID tracking, token lifecycle）
- `tests/unit/test_multiverse_registry.py` — 34 tests
- `tests/unit/test_token_middleware.py` — 15 tests
- `tests/unit/test_supervisor.py` — 36 tests
- `tests/unit/test_proxy_supervisor_mode.py` — 13 tests
- `tests/integration/test_supervisor.py` — 13 tests（7 light + 6 heavy @slow）
- `tests/integration/_supervisor_helpers.py` — StubServiceEmbedder / uvicorn lifecycle / MCP call helpers
- `docs/wiki/Operations-Multiverse-Setup.md` — multiverse setup guide
- `docs/maintainers/multiverse-mv3-execution-plan.md` — PM execution plan

### 変更ファイル
- `gaottt/config.py` — knob 7 つ追加（multiverse_root / supervisor_port / supervisor_admin_key / universe_port_range_start/end / supervisor_spawn_concurrency / supervisor_readiness_timeout）
- `gaottt/server/mcp_server.py` — build_token_middleware / _install_token_middleware（GAOTTT_BACKEND_TOKEN env の有無で発動、default 素通し）
- `gaottt/server/mcp_proxy.py` — _route_to_supervisor / _Upstream token 拡張 / run_proxy supervisor mode
- `docs/wiki/_Sidebar.md` / `Home.md` / `Architecture-Overview.md` / `Operations-Tuning.md` — multiverse 追加
- `docs/maintainers/multiverse-implementation-plan.md` — MV3 ✅ 完了マーク

## 変更理由
MV2 で 1 宇宙 1 書き込みオーナーを機構化した上で、複数テナントの宇宙を 1 ホストで独立 data_dir で運用するための制御面。API key ルーティング、supervisor による backend spawn/respawn、token 認証で localhost 上の別プロセスからの port 直叩きを防ぐ。

## Work packages

| WP | Scope | Status | Files | Verification |
|---|---|---|---|---|
| WP-1 | config knobs + registry | done | config.py, multiverse/registry.py, __init__.py, test_multiverse_registry.py | 34 tests green |
| WP-2 | backend token middleware | done | mcp_server.py, test_token_middleware.py | 15 tests green + smoke green |
| WP-3 | supervisor | done | multiverse/supervisor.py, test_supervisor.py | 36 tests green |
| WP-4 | shim supervisor mode | done | mcp_proxy.py, mcp_server.py main(), test_proxy_supervisor_mode.py | 13 tests green + smoke green |
| WP-5 | integration tests | done | test_supervisor.py, _supervisor_helpers.py | 13 tests green (7 light + 6 heavy) |
| WP-6 | docs | done | Operations-Multiverse-Setup.md, _Sidebar.md, Home.md, Architecture-Overview.md, Operations-Tuning.md, implementation-plan.md | docs review green |
| B3+B5 fix | file modes + compare_digest | done | test_supervisor.py, test_token_middleware.py | +5 tests green |
| Codex B1-B4 fix | lifecycle races | done | supervisor.py, registry.py, test_supervisor.py | +11 tests green |

## 触ったファイル
実装: config.py, mcp_server.py, mcp_proxy.py, multiverse/registry.py, multiverse/supervisor.py, multiverse/__init__.py
テスト: test_multiverse_registry.py, test_token_middleware.py, test_supervisor.py, test_proxy_supervisor_mode.py, test_supervisor.py (integration), _supervisor_helpers.py
docs: Operations-Multiverse-Setup.md, _Sidebar.md, Home.md, Architecture-Overview.md, Operations-Tuning.md, multiverse-implementation-plan.md, multiverse-mv3-execution-plan.md, handover-2026-07-03-multiverse-mv3.md

## テスト
- `pytest tests/unit/ tests/integration/ -q` → 1019 passed, 1 skipped, 0 failed
- `pytest tests/integration/test_supervisor.py -q` → 13 passed (7 light + 6 heavy @slow, 70s)
- `rest_smoke.py` → 7/7 green
- `mcp_smoke.py` → 7/7 green
- `ruff check` → pre-existing 4 件のみ（MV3 新規コードは 0 件）
- 未実行: `tests/perf/`（retrieval geometry 不接触のため不要）

## ドキュメント
- 新規 `Operations-Multiverse-Setup.md`: setup guide（アーキテクチャ図 / 前提 / 6 step / セキュリティモデル / 制限事項 / トラブルシューティング）
- `Architecture-Overview.md`: 設計判断表「supervisor API は MCP/REST parity 対象外」
- `Operations-Tuning.md`: MV3 knob 7 つ
- `_Sidebar.md` / `Home.md`: 新規ページ追加

## 手動確認
- [ ] env var 形式で supervisor 起動 → port 7880 で listen
- [ ] 宇宙作成 → api_key 発行 → /route で {url, token} 返却
- [ ] token なし直叩き → 401 / token 付き → 非 401
- [ ] DELETE → backend SIGTERM 停止 → trash move
- [ ] 1-2 週 dogfooding で安定性確認後、本番運用移行

## 既知の問題
1. **engine 遅延構築と lease の相互作用**（WP-5 implementer が発見）: MCP `initialize` probe は engine/lease 未構築で OK を返す。idle_timeout 死亡後、lease stale 期限内（本番 60s）の再 route で `LeaseHeldError` になり得る。本番の idle_timeout=300s ≫ lease_stale=60s なら実質発火しないが、短い idle で運用する場合は要注意。
2. **owner.lock が 0600 でなく 0644**: MV2 の `lease.py` で作成される。multiverse_root 0700 が実効 trust boundary なので他 OS user からは不可視。criterion 文面との字面ギャップだが実害なし。MV2 scope 外。
3. **`slow` pytest marker が未登録**: `PytestUnknownMarkWarning` が出る。`pyproject.toml [tool.pytest.ini_options] markers` に登録すれば消える（別 WP scope）。

## 残TODO
- MV4 (control plane / Postgres) — MV3 の registry を Postgres に昇格
- MV5 (backup / DR) — Litestream + runbook + DR drill
- MV6 (英語宇宙) — embedder per universe
- `slow` marker の pyproject.toml 登録
- `--host` help 文の "no auth is built in" を更新（token auth 追加済み）

## リスク
- supervisor は v1 で単一プロセス前提（複数 supervisor instance は file lock で保護されているが、推奨構成ではない）
- 100 宇宙/ホスト上限（port range 7890-7989）
- REST 経路の宇宙提供なし（lease 構造的拒否）

## ロールバックメモ
- `GAOTTT_MULTIVERSE_ROOT` 未設定 → supervisor も shim supervisor mode も起動しない（完全に従来経路）
- `GAOTTT_BACKEND_TOKEN` 未設定 → token middleware が素通し（既存 7878 無影響）
- supervisor を使わなければ全経路が従来のまま

## 次の担当者・エージェントへのメモ
- MV4 は `gaottt/multiverse/registry.py` を Postgres に置き換える。registry API（`allocate_port` / `create_universe` / `verify_api_key` 等）の interface を保てば supervisor 側は変更不要
- `_Supervisor._backend_pids` はプロセス内 dict（supervisor restart で消失）。PID 不知時は port probe → 409 のパスが安全側に倒れている
- backend spawn の `BACKEND_IDLE_TIMEOUT` は module global（supervisor.py の上部）。test で monkey-patch する（WP-5 の `_build_spawn_env` 参照）
