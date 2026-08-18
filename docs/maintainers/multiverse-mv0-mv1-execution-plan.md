# Multiverse MV0+MV1 — 実行計画（PM 管理）

> 起票: 2026-07-02
> 担当: PM (orchestration) → implementer (subagent) → Codex CLI / QA (review)
> 対象 stage: **MV0 (EmbedderProtocol + 宇宙 manifest)** + **MV1 (embedding service + RemoteEmbedder)**
> 戦略 SoT: [Plans — Multiverse Scale-Out](../wiki/Plans-Multiverse-Scale-Out.md)
> 実装 SoT: [multiverse-implementation-plan.md](multiverse-implementation-plan.md) §MV0, §MV1（両段とも Codex レビュー2巡済み）
> 状態: 🟢 計画確定（Codex plan review APPROVE-WITH-NOTES + QA plan review APPROVE-WITH-NOTES 反映済み。実装委任フェーズへ）

## 1. 目標と背景

「1 プロセスが宇宙全体の RURI モデルを RAM/VRAM に持つ」デプロイ上の最重量問題を解消し、embedder をホスト共有サービスとして切り出す。実装計画 §依存関係の **最初の出荷可能ライン (MV0+MV1)** をこのセッションで成立させる。MV2-MV6 は別セッション。

独立価値: 単一ユーザー運用でも RURI 常駐を engine プロセスから分離できる（テスト・複数 frontend 起動時の model load 重複が消える）。

## 2. スコープ

### 対象
- **MV0**: `EmbedderProtocol` 抽出 / `RuriEmbedder` に `embedder_id`・`embedder_version` 追加 / `UniverseManifest` モジュール / `engine.startup()` hard gate / `build_engine` での identity 照合 / config knob `manifest_check_enabled`
- **MV1**: `gaottt/embedding/service.py` (FastAPI) / `gaottt/embedding/remote.py` / wire protocol / config knobs `embedder_endpoint`・`embedder_request_timeout_seconds` / `build_engine` factory 分岐 / httpx 本体依存昇格 / deploy 雛形 / 3 テストファイル

### 非対象（明示）
- MV2 (owner lease), MV3 (supervisor), MV4 (Postgres control plane), MV5 (backup/DR), MV6 (EN embedder)
- engine / gravity / scorer / store の physics 実装は **1 行も触らない**
- MCP 新ツール 0、REST 新エンドポイント 0（parity 鉄則対象外、管理面のみ）

## 3. リスク分類: **high-risk**

理由:
1. `engine.startup()` への hard gate 追加 — 起動拒否可能性（manifest dim mismatch で RuntimeError が素通しされる）
2. embedder version mismatch → 宇宙破壊リスク（全ベクトル無意味化）。これを防ぐためのガード自体が本作業
3. 新規 HTTP service 新設 — localhost bind・認証境界の設計ミスが情報漏洩に直結
4. 新規依存 httpx の本体依存への昇格
5. 新規コンポーネント 4 ファイル (base/manifest/service/remote) + deploy unit

緩和: **default 不変**（`embedder_endpoint` 未設定で完全従来経路、`manifest_check_enabled=False` で manifest 起因の check 全て無効化 ※FAISS 次元保護は常に有効、既存 DB は manifest 自动生成で後方互換）。既存 suite 全緑が acceptance の本体。

## 4. Work package 分解

直列で進める（normal/high-risk 既定: core は1 serial WP）。各 WP 完了ごとに verify。

### WP-1: MV0 test-first
- **Goal**: `tests/unit/test_manifest.py` 新設。実装前に失敗し、実装後に通るテストを書く
- **内容**:
  - manifest roundtrip / atomic write (`tmp + os.replace`)
  - `ensure_manifest` が既存 config から生成（既存 DB の後方互換パス）
  - `manifest.embedding_dim != config.embedding_dim` で startup 拒否（`manifest_check_enabled=True`）
  - `manifest_check_enabled=False` で同 dim mismatch が warning 透過（Codex B1 反映）
  - **`build_engine` での embedder identity check**: `manifest.embedder_id != embedder.embedder_id` で `RuntimeError`（check enabled）/ warning 透過（check disabled）（Codex B1 反映 — missing test 1, 2）
- **Risk**: tiny（テストのみ）
- **Acceptance**: テストファイルが存在し、MV0 core 実装前に fail することを確認（implementer が red→green を実演）

### WP-2: MV0 core
- **Goal**: 挙動変更ゼロで Protocol + manifest + gate を実装
- **Files**:
  - 新規 `gaottt/embedding/base.py` (`EmbedderProtocol`, `@runtime_checkable`)
  - 新規 `gaottt/store/manifest.py` (`UniverseManifest`, `load_manifest` / `write_manifest` / `ensure_manifest` / **`verify_embedder_identity`**)
  - **`verify_embedder_identity` の位置**（Codex contract mismatch 反映）: 実装計画 §MV0-2 line 84/99 では `load/write/ensure` のみが manifest module public API で、identity verify は `build_engine` 内に置く想定だった。本計画では **unit test で直接叩けるよう、純粋関数 `verify_embedder_identity(manifest, embedder, config) -> None` を `gaottt.store.manifest` に置き、`build_engine` から呼ぶ helper とする**。これにより RURI model 無しで unit test 可能（WP-1 の方針）。実装計画 §MV0-2 を本計画で補完する形
  - 変更 `gaottt/embedding/ruri.py` (`embedder_id` / `embedder_version` property 追加、`embedder_version` は `scan_cache_dir` から取得、不能時 `"unpinned"`)
  - 変更 `gaottt/core/engine.py:78` 型ヒント `RuriEmbedder` → `EmbedderProtocol`、`startup()` 冒頭（line 143 `store.initialize()` の前）に manifest hard gate
  - 変更 `gaottt/services/runtime.py:build_engine` で embedder identity 照合
  - 変更 `gaottt/config.py` に `manifest_check_enabled: bool = True` 追加（`# Multiverse manifest` セクション新設）
  - **`ensure_manifest` 実装注記**（QA NB1 反映）: gate は `store.initialize()` の前に走るため、新規 DB 初回起動時は `data_dir` が未作成。`ensure_manifest` / `write_manifest` 内で `data_dir.mkdir(parents=True, exist_ok=True)` を呼んでから manifest.json を書く（既存 DB はディレクトリがあるので no-op）
- **`manifest_check_enabled` の適用範囲**（Codex B1 反映 — 明示仕様）:
  - **常に RuntimeError（escape 不可）**: `embedder.dimension != config.embedding_dim` — FAISS 次元保護。manifest 非依存
  - **`manifest_check_enabled=True` で RuntimeError / `=False` で warning 透過**:
    - `manifest.embedding_dim != config.embedding_dim`（startup gate）
    - `manifest.embedder_id != embedder.embedder_id`（factory identity check）
  - escape hatch として一貫: manifest 起因の全 check を1つの knob で無効化。FAISS 次元保護だけは独立（manifest 無しでも効く）
- **Forbidden**: physics 層 (`core/gravity.py`, `core/scorer.py`, mass/displacement/velocity 更新則) / MV1 ファイル
- **Acceptance**: 
  - `pytest tests/ -q` 全緑維持（**挙動変更ゼロが本体**）
  - `ruff check gaottt/ tests/` pre-existing 4 件のみ
  - `tests/unit/test_manifest.py` 全 green（factory identity check 含む — Codex missing test 1, 2）
  - 既存 fixture が `build_engine` を通らないことで manifest 照合が skip されることを確認（Codex が grep で確認済み: `tests/` 配下に `build_engine` 呼び出しなし）

### WP-3: MV0 docs + 完了マーク
- **Goal**: ドキュメント更新 + `multiverse-implementation-plan.md` の MV0 完了マーク
- **Files**:
  - `docs/wiki/Operations-Tuning.md` に `manifest_check_enabled` 行追加
  - `docs/maintainers/multiverse-implementation-plan.md` の `⬜ MV0` → `✅ MV0`
- **Acceptance**: 該当箇所の更新確認

### WP-4: MV1 test-first
- **Goal**: MV1 実装前に fail するテストを3ファイル新設
- **Files**:
  - `tests/unit/test_remote_embedder.py` — `httpx.MockTransport` で wire protocol / shape / dtype / 正規化 / 503 / timeout / `/info` dim mismatch 拒否（Codex missing test 3）/ DI 用 `client: httpx.Client | None = None` 引数。httpx 0.28.1 で MockTransport が sync Client で動くことは Codex が確認済み
  - `tests/integration/test_engine_remote_embedder.py` — `create_app(StubEmbedder())` を uvicorn background thread で起動。**port は ephemeral (port=0)** で bind し、実際に bind された port を取り出して使う（Codex NB3 — 固定 7879 は並列実行や既存 service と衝突）。server の起動・停止・cleanup を明示的に検証（Codex missing test 4）。`embedder_endpoint` 設定 engine で remember→recall round-trip（実 HTTP、`test_engine_archive_ttl.py` fixture 流用）
  - `tests/perf/test_tier6_remote_embedder.py` — **スケルトンのみ**（Codex B2 — WP が own する）。`@pytest.mark.skip(reason="requires real RURI + running embedding service; run manually per Operations-Performance-Testing")` で skip。3 段の数値等価（`np.allclose(atol=1e-5)` / cosine 差 <1e-6 / golden queries top-K 一致）を assert する test 関数を書くが、default skip。実行は次セッションで real RURI + 手動（Tier 6 は CI 自動化しない設計、CLAUDE.md）
- **MV1-specific stub**（Codex NB4）: canonical `StubEmbedder` (`test_engine_archive_ttl.py:17`) は `embedder_id` / `embedder_version` / `encode_queries` を持たない。service の `/info` 応答と remote manifest check にはこれらが必要。WP-4 で **MV1 テスト用に `embedder_id="stub-test"` / `embedder_version="unpinned"` / `encode_queries` を持つ拡張 stub**（例: `tests/integration/test_engine_remote_embedder.py` 内に `StubServiceEmbedder` として定義、既存 StubEmbedder を継承または wrap）を作る。本体の canonical StubEmbedder は触らない（他 test の安定性優先）
- **Risk**: normal（テストのみ、httpx は既に dev 依存）
- **Acceptance**: MV1 core 実装前に fail、実装後に green。Tier 6 skeleton は default skip で存在確認のみ

### WP-5: MV1 embedding service
- **Goal**: `gaottt/embedding/service.py` 新設
- **Files**:
  - 新規 `gaottt/embedding/service.py` — `create_app(embedder: EmbedderProtocol) -> FastAPI` 公開 API + `__main__` entry（`python -m gaottt.embedding.service --host 127.0.0.1 --port 7879 --model cl-nagoya/ruri-v3-310m`）。localhost bind 強制（外部アドレスで WARNING）。lifespan で `RuriEmbedder` を 1 回ロード（encode ロジック二重実装しない）。`to_thread` + `Semaphore(1)` で GPU 直列化。`--max-queue 32`（超過 503）。入力上限（texts ≤256 / 文字 ≤200k / body ≤10MB、超過 413）
  - 新規 `deploy/gaottt-embedder.service` (systemd unit, `Restart=always`)
- **wire protocol**:
  - `POST /encode` JSON `{"kind": "query"|"document", "texts": [...]}`
  - response `application/x-msgpack` `{"shape": [N, dim], "dtype": "float32", "data": <bytes>}`
  - L2 正規化は service 側（RuriEmbedder が既に実装済み）。prefix 適用も service 側（client は生テキストのみ）
  - `GET /info` JSON `{"model_name", "dimension", "version", "batch_size"}`。`version` は HF snapshot commit hash（`embedder_version` と同じ来源）
  - error: queue 満杯 → 503 + `Retry-After` / texts 空 → 400 / CUDA OOM → 500（batch_size 縮小の導線）
- **Forbidden**: RemoteEmbedder / `build_engine` 分岐 / config knobs（WP-6）
- **Acceptance**（QA blocking 反映 — WP 単独の検証ポイント）:
  - `python -c "from gaottt.embedding.service import create_app"` が import 可能
  - `create_app(StubServiceEmbedder())` が `/encode` と `/info` route を持つ FastAPI app を返す（`TestClient` で `GET /info` が 200 を返す程度の sanity）
  - `deploy/gaottt-embedder.service` が存在
  - **WP-4 の `test_remote_embedder.py` / `test_engine_remote_embedder.py` は WP-5 完了時点でも `gaottt.embedding.remote` (WP-6) 未存在のため import error で RED。これは想定内**（QA 指摘 — PM が回帰と誤認しないよう明記）。WP-6 完了で green に転じる

### WP-6: MV1 RemoteEmbedder + wiring
- **Goal**: `gaottt/embedding/remote.py` + factory 分岐 + httpx 昇格
- **Files**:
  - 新規 `gaottt/embedding/remote.py` — `RemoteEmbedder(endpoint, timeout=30.0, client=None)`。`__init__` で `GET /info` → cache、接続不能なら `ConnectionError` 即 raise。`dimension` / `embedder_id` / `embedder_version` / `encode_documents` / `encode_queries` / `encode_query`。retry は接続エラーのみ 1 回（backoff 0.5s）、encode 4xx/5xx は retry しない。DI 用 `client: httpx.Client | None = None`
  - 変更 `gaottt/config.py` に `embedder_endpoint: str | None = None` / `embedder_request_timeout_seconds: float = 30.0` 追加（`# Multiverse embedding` セクション）
  - 変更 `gaottt/services/runtime.py:build_engine` — `config.embedder_endpoint` があれば `RemoteEmbedder`、なければ従来 `RuriEmbedder`。`embedder.dimension != config.embedding_dim` で `RuntimeError`
  - 変更 `pyproject.toml` — `httpx>=0.27.0` を本体 `dependencies` に昇格（`[dev]` からは削除しない、二重定義可）
- **Acceptance**:
  - `pytest tests/ -q` 全緑
  - `pytest tests/unit/test_remote_embedder.py tests/integration/test_engine_remote_embedder.py -v` green
  - `embedder_endpoint` 未設定で従来経路（既存 suite がそのまま通る）
  - 両 smoke green（`rest_smoke.py` / `mcp_smoke.py`）

### WP-7: MV0+MV1 docs + handoff + 完了マーク
- **Goal**: ドキュメント更新 + handoff 作成 + MV0/MV1 完了マーク
- **Files**:
  - `docs/wiki/Operations-Server-Setup.md` に「embedding service を分離する」節（起動方法・localhost bind・systemd unit の参照）
  - `docs/wiki/Operations-Tuning.md` に knob 3 つ（`manifest_check_enabled` / `embedder_endpoint` / `embedder_request_timeout_seconds`）
  - `docs/wiki/Operations-Resource-Requirements.md` に「モデル抜き engine RAM」の計測手順を追記（Codex doc issue — 実装計画 line 196 の要求）。**実測値は real RURI が必要なので「次セッションで計測・追記」の placeholder とする**（計測コマンド: engine と embedding service を別プロセスで起動し、engine プロセスの RSS からモデル分を引く）
  - `docs/maintainers/multiverse-implementation-plan.md` の `⬜ MV0` → `✅ MV0`、`⬜ MV1` → `✅ MV1`
  - **新規 `docs/maintainers/handover-2026-07-02-multiverse-mv0-mv1.md`** — 引き継ぎノート（日本語、CLAUDE.md §Handoff note sections 準拠）。Codex handoff issue — §7 で要求しているが WP-7 acceptance と §6 acceptance に明示
- **Acceptance**: 該当箇所の更新確認 + handoff file の存在 + 実装計画の MV0/MV1 完了マーク更新

### 手動 acceptance（PM が別途、real RURI 必須）
- `tests/perf/test_tier6_remote_embedder.py` — **WP-4 で skeleton を default skip で作成**（Codex B2 反映）。実行は次セッション以降（real RURI 必要 + embedding service 実プロセス起動）。下記「検証」参照

### 計画的 ownership overlap（Codex WP risks 反映）
直列で進めるため安全だが、明示的に記録する:
- `gaottt/services/runtime.py`: WP-2（embedder identity 照合追加）と WP-6（RemoteEmbedder 分岐追加）で両方 touch。WP-2 → WP-6 の順で積み重ねる
- `gaottt/config.py`: WP-2（`manifest_check_enabled`）と WP-6（`embedder_endpoint` / `embedder_request_timeout_seconds`）で別セクションに追加。衝突なし

## 5. テスト戦略

| WP | test 種別 | ファイル | real RURI 要 |
|---|---|---|---|
| WP-1 | unit | `test_manifest.py`（factory identity check bypass 含む — Codex missing test 1, 2） | 否 |
| WP-4 | unit + integration + perf-skeleton | `test_remote_embedder.py` (MockTransport) / `test_engine_remote_embedder.py` (uvicorn background, ephemeral port) / `test_tier6_remote_embedder.py` (skip marker, Codex B2) | 否（StubServiceEmbedder で service を駆動） |
| 次セッション | perf Tier 6 実行 | `test_tier6_remote_embedder.py` の skip を外して実行 | **要** |

test-first 原則: WP-1 と WP-4 は実装前に書き、fail することを実演してから実装。

数値等価検証（MV1 の本質的 acceptance）は `np.allclose` (atol=1e-5) + cosine 差 <1e-6 + golden queries top-K 一致の 3 段。bit-exact は要求しない（service 側バッチングで batch shape が変わり得る — 実装計画 §MV1-4）。これは WP-4 の Tier 6 skeleton に skip marker で組み込まれ、次セッションで実行する。

## 6. acceptance criteria（全体）

1. `pytest tests/ -q` 全緑（822+α passed、既存 suite への影響ゼロ）
2. `ruff check gaottt/ tests/` pre-existing 4 件のみ
3. `.venv/bin/python scripts/rest_smoke.py && .venv/bin/python scripts/mcp_smoke.py` 両 green
4. **rollback 経路の明示的テスト**（Codex B1 反映）: `manifest_check_enabled=False` で manifest dim/identity mismatch が warning 透過すること、`embedder_endpoint=None` で従来 RuriEmbedder 経路になることを unit test が assert
5. 新規テスト全 green（default skip の Tier 6 skeleton を含む）: `test_manifest.py` / `test_remote_embedder.py` / `test_engine_remote_embedder.py` / `test_tier6_remote_embedder.py` (skip)
6. `multiverse-implementation-plan.md` の MV0, MV1 完了マーク更新
7. ドキュメント（Server-Setup, Tuning, Resource-Requirements）に該当節/行が追加されている
8. **handoff file** `docs/maintainers/handover-2026-07-02-multiverse-mv0-mv1.md` が存在し、CLAUDE.md §Handoff note sections 準拠（Codex handoff issue 反映）

## 7. ドキュメント / 引き継ぎ / 言語ポリシー

- **ドキュメント**: Server-Setup / Tuning の該当節（日本語）、実装計画の完了マーク
- **引き継ぎ**: MV0+MV1 完了時に `docs/maintainers/handover-2026-07-02-multiverse-mv0-mv1.md` を作成（日本語、§Handoff note sections 準拠）
- **言語**: 日本語（durable artifact）、code identifier / command / path / API 名は英語ママ

## 8. リスク

| リスク | 影響 | 緩和 |
|---|---|---|
| manifest gate が既存 fixture で誤発火 | テスト大量失敗 | direct engine 構築は `build_engine` を通らないので照合 skip（Codex が grep で確認）。`engine.startup()` gate は manifest 不在時 `ensure_manifest` で自動生成（既存 DB 後方互換） |
| RuriEmbedder の `embedder_version` 取得が環境次第で失敗 | "unpinned" になるだけ（warning）。hard check は `embedder_id` + `embedding_dim` のみ | 取得不能時 `"unpinned"` で warning、v1 では stop しない |
| httpx 本体昇格が依存衝突 | install 失敗 | 既に dev 依存にあるので本体昇格は安全（version 範囲同一） |
| service の localhost bind 強制が既存運用を壊す | 既存ユーザー影響 | default 不変（service は opt-in、未使用なら影響ゼロ） |
| RemoteEmbedder の接続エラーが silent fail | 宇宙破壊 | `__init__` で `/info` 取得失敗時 `ConnectionError` 即 raise（起動時に倒す） |
| service integration test の port collision（Codex NB3） | 並列実行や既存 service との衝突で flaky | `test_engine_remote_embedder.py` は **port=0 (ephemeral)** で bind → 実ポートを取り出して使う |
| canonical `StubEmbedder` が `embedder_id`/`embedder_version`/`encode_queries` を持たない（Codex NB4） | service `/info` や remote manifest check で AttributeError | WP-4 で MV1 専用 `StubServiceEmbedder` を定義（既存 StubEmbedder は触らない） |
| manifest 並行作成 race（Codex NB5） | 同一 data_dir で 2 プロセスが同時 `ensure_manifest` で last-writer-wins の `universe_id` | `write_manifest` の atomic replace は torn write を防ぐが race は防がない。MV0 では accept（MV2 lease で構造的に解決）。dogfooding で問題が出たら MV1 に backport 検討 |

## 9. Assumption ledger

| assumption | basis | falsification condition | blast radius |
|---|---|---|---|
| 既存 test fixture は direct engine 構築で `build_engine` を通らない | `test_engine_archive_ttl.py:70` が `GaOTTTEngine(...)` 直接呼び出しを確認済み | fixture が `build_engine` を使い始めていた → manifest 照合が走り、StubEmbedder が `embedder_id` を持たず照合 fail | 中（fixture 改修が必要だが局所） |
| `scan_cache_dir()` で HF revision が取れる | `ruri.py:13-22` で既に `scan_cache_dir` を import 済み（API 可用性は実証済み）。revision 文字列の抽出は同一 API の新規使用 | snapshot ではなく手動配置モデルで revision が取れない → `"unpinned"`（設計どおり） | 小（warning のみ） |
| httpx 0.27+ が `MockTransport` を sync Client でサポート | httpx 公式 API（MockTransport は 0.18+ で利用可能、pyproject.toml floor は `>=0.27.0` で十分）。Codex は 0.28.1 で動作確認済み | async 専用だった → unit test 戦略変更（ASGITransport + AsyncClient 版に作り直し） | 中（WP-4 テスト戦略の再構築） |
| `engine.startup()` gate 挿入位置（`store.initialize()` の前）が正しい | 実装計画 §MV0-2 の Codex レビューで確定、現 `engine.py:142-143` を確認 | gate が `store` / `cache` / FAISS のいずれかに依存していた → 位置修正 | 小（位置変更のみ） |
| RURI の L2 正規化・prefix 適用を service 側に寄せても client から透過 | `ruri.py:42-71` が encode 内部で prefix + normalize を完結 | 二重 prefix / 二重 normalize の bug | 中（実装時に unit test で検出可能） |

## 10. Gate 計画

✅ = 実施済み、⬜ = 予定（Codex NB 反映 — 記号を明確化）

| Gate | 状態 | 理由 |
|---|---|---|
| GaOTTT recall | ✅ 実施済み（multiverse 関連メモリ無し、関連設計判断は CLAUDE.md/wiki に集約） | high-risk |
| Planning doc (本ファイル) | ✅ 起稿 + Codex B1/B2 反映済み | high-risk |
| Codex plan review | ✅ 1巡目実施 → BLOCK → B1/B2 反応済み → ⬜ 修正後 re-review | high-risk |
| QA plan review | ⬜ Codex re-review 通過後 | high-risk |
| Test-first delegation (WP-1, WP-4) | ⬜ | high-risk |
| Codex test-diff review (WP-1, WP-4 後) | ⬜ | high-risk |
| QA test-diff review (WP-1, WP-4 後) | ⬜ | user-facing service |
| Implementation delegation (WP-2, WP-5, WP-6) | ⬜ | high-risk |
| Test execution (各 WP 後) | ⬜ | high-risk |
| PM diff inspection (各 WP 後) | ⬜ | 全 WP |
| Docs/handoff impact check (WP-3, WP-7 + 最終) | ⬜ | high-risk |
| Codex final diff review | ⬜ 最終 | high-risk |
| QA final review | ⬜ 最終 | high-risk |
| GaOTTT writeback | ⬜ 最終（durable 決定事項） | high-risk |

## 11. Open questions

- なし（実装計画が Codex レビュー2巡済みで仕様確定、本 doc は実行計画に徹する）

## 12. 検証コマンド（各 WP 締め）

```bash
# MV0 / MV1 共通
.venv/bin/python -m pytest tests/ -q
ruff check gaottt/ tests/
.venv/bin/python scripts/rest_smoke.py && .venv/bin/python scripts/mcp_smoke.py

# MV0 個別
.venv/bin/python -m pytest tests/unit/test_manifest.py -v

# MV1 個別
.venv/bin/python -m pytest tests/unit/test_remote_embedder.py tests/integration/test_engine_remote_embedder.py -v

# MV1 perf（real RURI 必須、次セッション）
.venv/bin/python -m pytest tests/perf/test_tier6_remote_embedder.py -v -s
```
