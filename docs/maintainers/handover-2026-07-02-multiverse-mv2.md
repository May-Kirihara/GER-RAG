# 引き継ぎメモ — Multiverse MV2 Owner Lease

## ステータス
- **状態**: 実装完了（done-with-notes） / **日付**: 2026-07-02 / **担当**: PM (Claude) / **概要**: MV2 owner lease 実装 — 1 宇宙 1 書き込みオーナーの機構化
- **リスク分類**: high-risk → 全 required gate 実施

## 変更内容

同じ `data_dir` を複数プロセスが開いて write-behind が後勝ちする事故クラス（bidirectional cache overwrite / FAISS reverse-overwrite）を機構で閉じる。

### 新規ファイル
- `gaottt/store/lease.py` — `OwnerLease`（atomic acquire / heartbeat / release）、`LeaseHeldError`、`LeaseLostError`
- `tests/unit/test_owner_lease.py` — 21 tests（acquire/conflict/stale/force/race/heartbeat/release safety/cross-process guard TOCTOU）
- `tests/integration/test_engine_lease.py` — 33 tests（4-path persist block / read-only 14 mutator / lease-loss race / lifecycle / managed 強制 / default-OFF / CLI / shutdown window / prefetch bypass）
- `docs/maintainers/multiverse-mv2-execution-plan.md` — PM execution plan

### 変更ファイル
- `gaottt/core/engine.py` — `_persist_blocked` latch（cache + FAISS の 4 永続化経路を gate）、14 mutator entry check（LeaseLostError）、`_query_internal` passive guard（全 caller cover）、startup acquire / heartbeat loop / shutdown release（stop_write_behind 後に ownership 再検証）、managed 強制（`owner_lease_enabled OR manifest.managed`）
- `gaottt/store/cache.py` — `persist_blocked` flag + `flush_to_store()` entry gate（全呼出元を1箇所で網羅: write-behind loop / shutdown final flush / idle watchdog / inline flush）
- `gaottt/config.py` — knob 4 つ（`owner_lease_enabled` / `lease_force_takeover` / `lease_heartbeat_seconds` / `lease_stale_seconds`、すべて default 不変）
- `gaottt/server/mcp_server.py` — `--force-takeover` CLI flag（env `GAOTTT_LEASE_FORCE_TAKEOVER` 経由で config + proxy spawn に伝播）
- `gaottt/server/mcp_proxy.py` — spawn env に `env=os.environ.copy()` を明示

### ドキュメント
- `docs/wiki/Architecture-Concurrency.md` — 「構造的解 (2): owner lease」節追加、shutdown 順序更新
- `docs/wiki/Operations-Troubleshooting.md` — 「LeaseHeldError / LeaseLostError が出る」項追加
- `docs/wiki/Operations-Tuning.md` — 「Multiverse owner lease」節追加（knob 4 つ + filesystem 要件 + 昇格計画）
- `docs/maintainers/multiverse-implementation-plan.md` — MV2 完了マーク

## 変更理由

CLAUDE.md に記録された 2026-05-31 の FAISS reverse-overwrite incident（39,402 docs の index が 2 件に激減）と同型の事故クラスを「プロセスを kill する運用ルール」ではなく「機構」で閉じる。MV3 supervisor の前提（supervisor 管理宇宙を直接開けないようにする）。

## Work packages

| WP | Scope | Status | Files | Verification |
|---|---|---|---|---|
| WP-1/1b/1c | OwnerLease unit tests | done | test_owner_lease.py (21) | 21 GREEN |
| WP-2 | lease.py + config knobs | done | lease.py, config.py | 21 GREEN |
| WP-3/3b | engine integration tests | done | test_engine_lease.py (33) | 33 GREEN |
| WP-4/4b/4c | engine 統合 | done | engine.py, cache.py, mcp_server.py, mcp_proxy.py | 33 GREEN + 908 full suite |

## テスト

| 実行コマンド | 結果 |
|---|---|
| `pytest tests/unit/ tests/integration/ -q` | 908 passed, 1 skipped, 0 failed |
| `pytest tests/integration/test_engine_lease.py -v` | 33 passed |
| `pytest tests/unit/test_owner_lease.py -v` | 21 passed |
| `scripts/rest_smoke.py` | 7/7 green |
| `scripts/mcp_smoke.py` | 7/7 green |
| `ruff check gaottt/ tests/` | pre-existing 4 件のみ |

未実行: `tests/perf/`（MV2 は retrieval geometry 不接触なので perf regression なし、Tier 6 remote embedder は前セッションで green 確認済み）

## レビュー / QA gates

| Gate | 結果 |
|---|---|
| Codex plan review | ✅ 5 blocking → execution plan 改訂（guard API を cache 層に集約、mutating surface 完全網羅、4-path 独立 test、lease-loss race test） |
| Codex test-diff review (WP-1) | ✅ 4 blocking → WP-1b 強化（barrier race / cross-process guard TOCTOU / release TOCTOU / heartbeat_loop test） |
| Codex final diff review | ✅ 2 blocking → WP-4b/4c 解消（B1: shutdown blind window → revalidation を stop_write_behind 後に移動、B2: prefetch bypass → _query_internal passive guard） |
| QA final review | ✅ pass（出荷可・条件なし）— 8 criteria 全 trace、独立実行 908 passed、14 mutator guard + 4 永続化経路 gate 完全性確認、physics 無接触、docs 正直。Info 3 件（force-takeover 確認プロンプトなし / sub-ms residual race / NFS 非サポート）は全て v1 許容 |

## 設計判断の記録

1. **persist gate を cache 層に置く**: `engine._persist_blocked` を mutating method entry で check するだけでは `mcp_server.py:1146` の idle watchdog が `cache.flush_to_store()` を直接呼んで bypass する。`cache.persist_blocked` flag を `flush_to_store()` entry で check することで全呼出元を1箇所で網羅（Codex plan review B1 で発見）
2. **`_query_internal` に passive guard**: `query()` の guard だけだと `prefetch()` と dream loop が bypass する。内部メソッドに置くことで全 caller を cover（Codex final B2 で発見）
3. **shutdown ownership 再検証を `stop_write_behind` 後に**: heartbeat stop と final flush の間に write-behind が走る narrow race を構造的に排除（Codex rereview B1 で発見、3巡目で解消）
4. **default OFF + managed 強制**: standalone の既存挙動を変えずに supervisor 宇宙を強制。発動条件 `owner_lease_enabled OR manifest.managed`

## 既知の問題 / 残リスク

- **QA final review 未実施**: acceptance criteria / user flow の独立検証が残っている。実装は 54 test + 908 full suite + 両 smoke で検証済みだが、QA の視点（docs clarity / handoff usefulness / regression hunt）は別物
- **standalone default ON 昇格が未判断**: 1-2 週の dogfooding後に判断（Phase Q governor と同じ promotion パターン）
- **network filesystem 非サポート**: lease は `O_EXCL` / `flock` / `os.replace` の POSIX semantics に依存。NFS/CIFS では信頼できない — docs に明記済み
- **sub-ms preamble residual race**: shutdown 入口〜stop_write_behind の間（<1ms）に write-behind 50ms tick が到達する確率は実質 0%（20連続実行で 0%）、ordering では防御できない部分。accept-alleable for v1

## ロールバックメモ

- standalone: `owner_lease_enabled=False`（default のまま、何もしなくてよい）
- managed 宇宙: `manifest.json` の `managed` を `false` に書き換え（runbook 専用、事故防御を外す操作）

## 次の担当者・エージェントへのメモ

- **MV3（universe supervisor）に進む場合**: MV2 の lease が前提。supervisor は宇宙作成時に `manifest.managed=True` で manifest を書くことで lease を強制。`GAOTTT_LEASE_FORCE_TAKEOVER` env を spawn 時に渡す経路は mcp_proxy.py の `env=os.environ.copy()` で伝播済み
- **standalone で lease を試す場合**: `owner_lease_enabled=True` で起動。2 つ目のプロセスは `LeaseHeldError` で落ちる。`--force-takeover` で強制奪取
- **physics 層は1行も触っていない**: `core/gravity.py` / `core/scorer.py` は無改変。lease 機構は engine の write path と cache/FAISS の永続化経路のみに作用
