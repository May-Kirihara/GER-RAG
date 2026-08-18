# 引き継ぎメモ — Multiverse MV0 + MV1 完了（2026-07-02）

## ステータス

- **状態**: ✅ MV0 + MV1 完了（このセッションの範囲）
- **日付**: 2026-07-02
- **担当**: PM（orchestration）→ implementer subagent（実装）→ Codex CLI / QA（review）
- **概要**: Multiverse Scale-Out の最初の出荷可能ライン（MV0 EmbedderProtocol + 宇宙 manifest / MV1 embedding service + RemoteEmbedder）を完了。physics / observation 層は 1 行も触らず、default 不変を維持。既存 822 suite + 新規 32 test = 854 passed / 1 skipped / 0 failed、両 smoke green。

## 変更内容

### MV0 — EmbedderProtocol + 宇宙 manifest（土台、挙動変更ゼロ）
- 新規 `gaottt/embedding/base.py`（`EmbedderProtocol`、`@runtime_checkable`）
- 新規 `gaottt/store/manifest.py`（`UniverseManifest` + `load/write/ensure_manifest` + `verify_embedder_identity`）
- 変更 `gaottt/embedding/ruri.py`（`embedder_id` / `embedder_version` property 追加、encode ロジック不改変）
- 変更 `gaottt/core/engine.py`（型ヒント L78 / `startup()` 冒頭 L142 に manifest dim hard gate）
- 変更 `gaottt/services/runtime.py:build_engine`（manifest 確保 + `verify_embedder_identity` 呼び出し）
- 変更 `gaottt/config.py`（`manifest_check_enabled: bool = True`）

### MV1 — embedding service + RemoteEmbedder
- 新規 `gaottt/embedding/service.py`（FastAPI `create_app(embedder)` DI seam + `__main__` entry）
- 新規 `gaottt/embedding/remote.py`（`RemoteEmbedder`）
- 新規 `deploy/gaottt-embedder.service`（systemd unit）
- 変更 `gaottt/services/runtime.py:build_engine`（`embedder_endpoint` で `RemoteEmbedder` 分岐）
- 変更 `gaottt/config.py`（`embedder_endpoint: str = ""` / `embedder_request_timeout_seconds: float = 30.0`）
- 変更 `pyproject.toml`（`httpx>=0.27.0` を `[project] dependencies` に昇格）

## 変更理由

「1 プロセスが宇宙全体の RURI model を RAM/VRAM に持つ」デプロイ上の最重量問題を解消。GPU コストをユーザー数ではなくホスト数に比例させる。Multiverse 全体計画（[Plans — Multiverse Scale-Out](../wiki/Plans-Multiverse-Scale-Out.md)）の最初の出荷可能ライン。単一ユーザー運用でも model load 重複が消える独立価値。

## Work packages

| WP | Scope | Status | Files | Verification | Remaining risks |
|---|---|---|---|---|---|
| WP-1 | MV0 test-first (`test_manifest.py`) | ✅ done | `tests/unit/test_manifest.py`（17 tests） | RED→GREEN 実演、Codex test-diff review APPROVE | なし |
| WP-2 | MV0 core (Protocol + manifest + gate) | ✅ done | `base.py` / `manifest.py` / `ruri.py` / `engine.py` / `runtime.py` / `config.py` | Codex final diff review APPROVE（B1 disabled-warning を修正後に re-review APPROVE） | 「毎回 version mismatch warning」v1 warn-only 仕様、info severity |
| WP-3 | MV0 docs | ✅ done | `Operations-Tuning.md` / `multiverse-implementation-plan.md` | PM 自前 | なし |
| WP-4 | MV1 test-first | ✅ done | `test_remote_embedder.py`（11 tests）/ `test_engine_remote_embedder.py`（4 tests + StubServiceEmbedder）/ `test_tier6_remote_embedder.py`（3 skip skeleton） | Codex test-diff review APPROVE-WITH-NOTES | 0.5s backoff / generic 4xx/5xx / encode-time connect error の executable pin が未対応（non-blocking、follow-up） |
| WP-5 | MV1 embedding service | ✅ done | `service.py` / `deploy/gaottt-embedder.service` | Codex final review で security BLOCK → localhost 強制拒否に修正 → re-review APPROVE | なし（非 localhost は `SystemExit` で拒否） |
| WP-6 | MV1 RemoteEmbedder + wiring | ✅ done | `remote.py` / `runtime.py` / `config.py` / `pyproject.toml` / `test_engine_remote_embedder.py`（socket() bug fix） | Codex final review APPROVE | `embedder_endpoint` は sentinel empty-string で env override 可能（`Optional[str] = None` だと拾わない罠を回避） |
| WP-7 | docs + handoff | ✅ done | `Operations-Tuning.md` / `Operations-Server-Setup.md` / `Operations-Resource-Requirements.md` / `multiverse-implementation-plan.md` / 本 handover | PM 自前 | なし |

## 触ったファイル

### 新規（8 ファイル）
- `gaottt/embedding/base.py`（MV0）
- `gaottt/store/manifest.py`（MV0）
- `gaottt/embedding/service.py`（MV1）
- `gaottt/embedding/remote.py`（MV1）
- `deploy/gaottt-embedder.service`（MV1）
- `tests/unit/test_manifest.py`（17 tests、WP-1）
- `tests/unit/test_remote_embedder.py`（11 tests、WP-4）
- `tests/integration/test_engine_remote_embedder.py`（4 tests + StubServiceEmbedder、WP-4 / WP-6 で socket() fix）
- `tests/perf/test_tier6_remote_embedder.py`（3 skip skeleton、WP-4）
- `docs/maintainers/multiverse-mv0-mv1-execution-plan.md`（PM execution plan）
- `docs/maintainers/handover-2026-07-02-multiverse-mv0-mv1.md`（本ファイル）

### 変更（5 ファイル）
- `gaottt/embedding/ruri.py`（+29行、`embedder_id`/`embedder_version` property、encode ロジック不改変）
- `gaottt/core/engine.py`（+25/-2行、型ヒント + startup manifest gate）
- `gaottt/services/runtime.py`（+数十行、manifest 照合 + factory 分岐）
- `gaottt/config.py`（+数十行、`manifest_check_enabled` + `embedder_endpoint` + `embedder_request_timeout_seconds`）
- `pyproject.toml`（httpx を `[project] dependencies` に昇格）
- `docs/wiki/Operations-Tuning.md`（Multiverse manifest / embedding service 節）
- `docs/wiki/Operations-Server-Setup.md`（embedding service を分離する節）
- `docs/wiki/Operations-Resource-Requirements.md`（モデル抜き engine RAM 計測手順）
- `docs/maintainers/multiverse-implementation-plan.md`（MV0 / MV1 完了マーク）

### physics 層（触っていない、確認済み）
- `gaottt/core/gravity.py` / `gaottt/core/scorer.py` — `git diff` 空
- `engine.py` の mass・displacement・velocity 更新則 — 触っていない（Codex が git diff で確認）

## テスト

### 実行コマンドと結果
```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/perf
# → 854 passed, 1 skipped, 0 failed（既存 822 + WP-1 の17 + WP-4 の15）

.venv/bin/python -m pytest tests/unit/test_manifest.py tests/unit/test_remote_embedder.py tests/integration/test_engine_remote_embedder.py -q
# → 32 passed

.venv/bin/python scripts/rest_smoke.py   # → All scenarios passed (7/7)
.venv/bin/python scripts/mcp_smoke.py    # → All scenarios passed (7/7)

ruff check gaottt/ tests/
# → pre-existing 4 件のみ（ruri.py os / cooccurrence.py time / mcp_server.py pathlib.Path / test_ambient_truncate.py E741）
```

### 未実行と理由
- `tests/perf/test_tier6_remote_embedder.py`（3 tests）— **default skip**。real RURI と embedding service 実プロセスが必要。次セッションで手動実行（Tier 6 = CI 自動化しない設計、CLAUDE.md）
- `scripts/perf_baseline.py --label pre-mv1` / `--label post-mv1` / `perf_diff.py` — real RURI 必須、次セッション

### 数値等価検証（MV1 の本質的 acceptance）
次セッションで実施:
1. `np.allclose(in_process, remote, atol=1e-5)` — `encode_documents` / `encode_queries`
2. cosine 差 < 1e-6
3. golden queries（`tests/perf/golden_corpus/queries.json`）top-K 一致

bit-exact は要求しない（service 側バッチングで batch shape が変わり得る — 実装計画 §MV1-4）。

## ドキュメント

更新済み:
- `docs/wiki/Operations-Tuning.md` — Multiverse manifest (MV0) / Multiverse embedding service (MV1) 節
- `docs/wiki/Operations-Server-Setup.md` — 「embedding service を分離する」節（起動・連携・systemd）
- `docs/wiki/Operations-Resource-Requirements.md` — モデル抜き engine RAM 計測手順（実測値は次セッション placeholder）
- `docs/maintainers/multiverse-implementation-plan.md` — MV0 / MV1 完了マーク
- `docs/maintainers/multiverse-mv0-mv1-execution-plan.md` — PM execution plan（このセッションの作業計画 SoT）
- 本 handover

未更新（次セッション）:
- `Operations-Resource-Requirements.md` の実測値（モデル抜き engine RAM）
- `Architecture-Overview.md` の設計判断表に「embedding service 分離」を追加するか検討

## 手動確認

### 実施済み
- service 単体 sanity（`/info` JSON / `/encode` msgpack shape (2,32) float32 / server 起動・停止）— implementer と PM で独立に確認
- 非 localhost で `SystemExit` 拒否 — PM で確認
- env override（`GAOTTT_EMBEDDER_ENDPOINT` + `from_config_file()`）— PM で確認

### 次セッションで実施
- real RURI での service ↔ RemoteEmbedder end-to-end（`python -m gaottt.embedding.service` を起動し `GAOTTT_EMBEDDER_ENDPOINT` で MCP/REST server を build_engine → remember/recall が動くか）
- `tests/perf/test_tier6_remote_embedder.py` の skip を外して3 tests を実行
- モデル抜き engine RAM の実測（`Operations-Resource-Requirements.md` の手順）

## 既知の問題

1. **毎回 `build_engine` 初回起動で version mismatch warning が出る** — `ensure_manifest` が config のみから生成するため、初回 manifest の `embedder_version` は `"unpinned"`。実 RuriEmbedder の commit hash と比較すると warning。v1 の warn-only 仕様（実装計画 §MV0-2）で、HF revision が動く運用を v1 では止めない。次セッションで「manifest を embedder の実 version で上書き」を検討する余地あり（`ensure_manifest` の signature 拡張が必要、WP-1 の API 契約変更を伴う）
2. **WP-4 test の pin 強度に gap**（Codex test-diff review non-blocking）:
   - 0.5s backoff が docstring にあるが executable-pin でない（WP-6 実装で `_CONNECT_RETRY_BACKOFF_SECONDS = 0.5` 定数は入れたが、test が検証しない）
   - generic 4xx/5xx は 503 のみ test
   - encode-time connect error は実装で共有 `_request_with_retry` で解決済みだが、直接 test していない
3. **`build_engine` 経路のテスト pin が弱い** — WP-4 integration test は direct engine 構築で `build_engine` を bypass。factory 分岐（`embedder_endpoint` / `verify_embedder_identity` 呼び出し）は code review のみ。必要なら別途 `build_engine` 経由の test を追加

## 残 TODO

### 次セッション（MV1 完結）
- [ ] real RURI での `tests/perf/test_tier6_remote_embedder.py` 3 tests 実行（skip 外して）
- [ ] `perf_baseline.py --label pre-mv1` / `post-mv1` で latency before/after 計測
- [ ] モデル抜き engine RAM 実測（`Operations-Resource-Requirements.md` の手順）
- [ ] 実機結合検証（service 起動 + `GAOTTT_EMBEDDER_ENDPOINT` + MCP/REST server で remember/recall）

### 別セッション（MV2 以降）
- [ ] MV2: owner lease（1 宇宙 1 書き込みオーナー）
- [ ] MV3: universe supervisor + multiverse layout（`GAOTTT_EMBEDDER_ENDPOINT` env 渡しを利用）
- [ ] MV4 / MV5 並行可
- [ ] MV6: 英語宇宙

## リスク

1. **`embedder_endpoint` env override は `from_config_file()` 経由のみ** — `GaOTTTConfig()` 直接呼び出しは dataclass default の `""` を使うだけで env を見ない。MV3 supervisor は `GAOTTT_EMBEDDER_ENDPOINT` env で子プロセスに渡す設計だが、子プロセスの初期化が `from_config_file()` を呼ぶ前提。確認要（MV3 実装時）
2. **embedding service は SPOF** — 全ユーザーの remember/recall が止まる。systemd `Restart=always` で自動復旧。in-process fallback は model load が重いので自動では行かない（設計どおり）
3. **manifest 並行作成 race**（NB5）— 同一 `data_dir` で 2 プロセスが同時 `ensure_manifest` で last-writer-wins の `universe_id`。atomic replace は torn write を防ぐが race は防がない。MV0 では accept、MV2 lease で構造的に解決

## ロールバックメモ

完全に default 不変なので、escape hatch は2つ:
- **MV0 manifest gate を無効化**: `GAOTTT_MANIFEST_CHECK_ENABLED=false` で manifest 起因の check（dim / embedder_id）を warning 透過。**FAISS 次元保護（`embedder.dimension != config.embedding_dim`）はこの knob に関わらず常に RuntimeError**
- **MV1 RemoteEmbedder を無効化**: `embedder_endpoint=""`（空文字列、default）で従来の in-process `RuriEmbedder` 経路。service を起動しなければ影響ゼロ

物理的な rollback（commit revert）は不要。config knob だけで挙動を旧来に戻せる。

## 次の担当者・エージェントへのメモ

1. **最初に読むドキュメント**:
   - `docs/maintainers/multiverse-implementation-plan.md`（MV0-MV6 全体、MV0/MV1 が ✅ 済み）
   - `docs/maintainers/multiverse-mv0-mv1-execution-plan.md`（このセッションの PM execution plan、 assumption ledger が参考）
   - `docs/wiki/Plans-Multiverse-Scale-Out.md`（戦略 SoT）

2. **次セッションの最初のタスク**: real RURI で `tests/perf/test_tier6_remote_embedder.py` を実行して、数値等価 3 段（allclose / cosine / top-K 一致）を確認。これが MV1 の本質的 acceptance で、未実施のまま handoff している

3. **MV2 着手時の注意**:
   - `manifest.managed: bool = False`（MV0 で導入、`UniverseManifest` フィールド）を supervisor が `true` で書く想定。lease 強制の信頼根になる
   - `verify_embedder_identity` を `engine.startup()` ではなく `build_engine` に置いた経緯（unit test で直接叩くため）は PM execution plan §WP-2 の Codex contract mismatch 反映に詳しい
   - `embedder_endpoint` が sentinel empty-string（`str = ""`）なのは env override を有効にするため。`Optional[str] = None` だと generic env loop が拾わない罠（`config.py:1218-1247` 参照）

4. **GaOTTT save 済みの教訓**（このセッションで得た）:
   - test-first RED は module top level import ではなく関数内 import で（collection interrupted を防ぐ）
   - atomic-write の tmp name は `.tmp` 終端（`glob("*.tmp")` cleanup test が検出できる）
   - `socket()` を呼ぶ file では `import socket`（module）と naming collision に注意（`socket.socket(...)` を使う）
   - httpx の `ConnectError` は builtin `ConnectionError` の subclass ではない（明示 re-raise が必要）
   - 認証を持たない service の host check は WARNING でなく拒否（`SystemExit`）

5. **Git の状態**: このセッションの全変更は **未 commit**。ブランチ `docs/multiverse-scale-out-plan`。コミット前に `git diff` を確認すること。推奨 commit 単位: MV0（WP-1/2/3）→ MV1（WP-4/5/6）→ docs/handoff（WP-7）。ただし1コミットでも機能する
