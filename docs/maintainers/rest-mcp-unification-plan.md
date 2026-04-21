# REST × MCP Unification Plan — Service Layer Refactor

> **目的**: REST API (`gaottt/server/app.py`) を MCP サーバー (`gaottt/server/mcp_server.py`) と同等の機能レベルに引き上げ、同時に両者の実装を **共有サービス層** に集約する。
> 起票: 2026-04-22
> 状態: **全 Phase S0–S6 完了**（2026-04-22）
> 想定担当: 実装は複数セッションに分割可、Phase S1 だけで MCP 既存テスト parity の validation fence を立てる

**完了マーク**:
- ✅ Phase S0: スキャフォールド（`gaottt/services/` + `runtime.py` + `formatters.py`）
- ✅ Phase S1: memory サービス抜き出し、MCP 6 ツールを薄ラッパ化
- ✅ Phase S2: REST に memory エンドポイント追加、`/index` `/query` は互換レイヤ化、`tests/integration/test_rest_memory.py` 11 ケース
- ✅ Phase S3: reflection / relations / maintenance / ingest / auto_remember を services に移植
- ✅ Phase S4: Phase D 9 ツール（commit/start/complete/abandon/depend/declare_*/inherit_persona）を services に移植
- ✅ Phase S5: REST に Phase B/C/D の全エンドポイント追加（48 ルート）、`tests/integration/test_rest_parity.py` 15 ケース
- ✅ Phase S6: ドキュメント（REST-API-Reference 全書き換え、Architecture-Overview / Plans-Roadmap / CLAUDE.md 更新）

**最終結果（2026-04-22）**:
- pytest: **138 passed** (112 既存 + 11 REST memory + 15 REST parity)
- ruff: pre-existing 4 件のみ（新規違反 0）
- benchmark: 7/7 passed, **p50=15.9ms**（vs 15.4ms baseline — 退行なし）
- `mcp_server.py`: 1373 → 853 行（-38%）
- REST エンドポイント: 6 → 48（`/reset` 除き MCP parity）

## 0. 動機と現状

### 0.1 現状のギャップ

| 機能群 | REST (`app.py`, 127行) | MCP (`mcp_server.py`, 1373行) |
|---|---|---|
| Phase A core (index/query/node/graph/reset) | ○（素朴版） | ○（`remember`/`recall` として豊富版） |
| Phase A+ (emotion/certainty/ttl/source_filter) | ✗ | ○ |
| Phase B (forget/restore/merge/compact/revalidate/relate 系) | ✗ | ○ |
| Phase C (prefetch/explore/reflect/auto_remember/ingest) | ✗ | ○ |
| Phase D (commit/start/complete/abandon/depend + declare_* + inherit_persona) | ✗ | ○ |
| reset | ○ | ✗（意図的に非公開） |

REST は実質 Phase A 以前の素朴実装のまま置き去り。一方 MCP は各ツールが **出力を人間可読文字列にインライン整形** しているため、そのままでは REST から再利用できない。

### 0.2 解くべき問題

1. **REST を MCP parity まで引き上げる**（Phase B/C/D 相当のエンドポイントを生やす）
2. 同じロジックが二重実装にならないよう **共有レイヤを挟む**
3. MCP の LLM 向け人間可読出力は維持しつつ、REST は構造化 JSON を返せるようにする
4. 既存 MCP テスト 112 ケースを **1 ケースも壊さない**（リファクタ安全網）

## 1. 目標アーキテクチャ

### 1.1 3 層モデル

```
┌──────────────────────────────────────────────────────────────┐
│  gaottt/server/mcp_server.py          gaottt/server/app.py   │
│  ─ MCP ツール = service(...) →         ─ REST endpoint =     │
│    formatter(...) で string を返す       service(...) を     │
│                                          そのまま JSON 返却  │
└──────────────┬─────────────────────────────────┬─────────────┘
               │                                 │
               ▼                                 ▼
┌──────────────────────────────────────────────────────────────┐
│  gaottt/services/  ← 新設、engine を叩き Pydantic を返す     │
│    runtime.py       engine singleton factory                 │
│    memory.py        remember / recall / explore / forget ... │
│    reflection.py    reflect (全 aspect)                      │
│    relations.py     relate / unrelate / get_relations        │
│    phase_d.py       commit / complete / declare_* ...        │
│    maintenance.py   merge / compact / prefetch               │
│    ingest_service.py  ingest                                 │
│    formatters.py    Pydantic → MCP 向け human-readable text  │
└──────────────┬───────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────┐
│  gaottt/core/engine.py（既存、変更しない）                   │
└──────────────────────────────────────────────────────────────┘
```

### 1.2 各層の責務

| 層 | 責務 | 戻り値 |
|---|---|---|
| `core/engine.py` | 重力力学・FAISS・store の操作（変更しない） | Engine 内部型 |
| `services/*.py` | engine を叩いて **Pydantic モデル** を返す純粋関数。副作用はあっても良いが、フォーマット責務は持たない | `gaottt/core/types.py` のモデル |
| `services/formatters.py` | Pydantic → MCP 用人間可読文字列。**REST は使わない** | `str` |
| `server/mcp_server.py` | 各ツール = `result = await service(...)` → `return formatter(result)` の薄いラッパ | `str` |
| `server/app.py` | 各エンドポイント = `result = await service(...)` → `return result`（FastAPI が JSON 化） | Pydantic JSON |

### 1.3 engine ブートストラップの統一

- `services/runtime.py` に `build_engine(config) -> GaOTTTEngine` と `get_engine()`（lazy singleton）を配置
- REST (`app.py` の `lifespan`) は `build_engine` を直接呼んで `app.state.engine` に入れる
- MCP (`mcp_server.py` の `get_engine`) は `services/runtime.get_engine()` を呼ぶだけ
- テスト用に `set_engine(engine)` の override hook も用意（現行 MCP テストが inject している方式を継承）

## 2. 設計判断の記録

| 論点 | 決定 | 理由 |
|---|---|---|
| サービス層の戻り値 | **Pydantic モデル**（dict ではない） | FastAPI と自然に噛み合う + 既存 `core/types.py` 資産を活用 |
| MCP の人間可読出力 | **維持**（`formatters.py` に切り出す） | LLM 体験を壊さない。既存 MCP 統合テストがそのまま通ることを担保 |
| `/reset` | **REST のみに残す**（MCP には載せない） | MCP は LLM が叩くので、破壊的 reset を露出しない現状の判断を踏襲 |
| サービス関数の命名 | MCP ツール名と一致（`remember`, `recall`, ...） | 双方向トレースが容易。衝突は `services.memory.remember` のモジュール階層で回避 |
| MCP ツールシグネチャ | **破壊的変更禁止**（CLAUDE.md 既存ルール） | 既存 MCP クライアントを壊さない。オプショナル引数追加のみ可 |
| REST レスポンスモデル | サービス層の戻り値 Pydantic を**そのまま** | 中間 DTO を作らない（二重定義になる） |
| REST エンドポイント命名 | `POST /<verb>`（例: `POST /remember`, `POST /recall`） | MCP ツール名と 1:1 対応。RESTful 原理主義より可読性優先 |
| `reflect` aspect | REST は `POST /reflect/{aspect}` 形式で aspect ごとに型付きレスポンス | aspect ごとに形が違うので、単一エンドポイントで Union を返すよりこちらが読みやすい |
| 認証 | **今回スコープ外**（現状も未実装） | Phase 移行とスコープを切り離す。別計画で扱う |
| API バージョニング | **今回スコープ外**（`v1` prefix は入れない） | 利用者がまだ内部ユースのみ。破壊的変更が落ち着いたら検討 |

## 3. Phase 計画

サービス層は 1 モジュールずつ段階的に抜き出す。各 Phase の完了条件は「既存テスト 112 全 pass + 新サービス関数の直接テスト追加」。

### Phase S0: 土台（スキャフォールド）
- `gaottt/services/__init__.py`、`runtime.py`、`formatters.py`（空）を作成
- `runtime.py` に engine factory を集約（MCP + REST から import する形に）
- MCP / REST 両方の起動経路をこのファクトリに切り替え（挙動変更なし）
- テスト: `tests/integration/test_mcp_phase_d.py` + 全既存テスト通過

### Phase S1: memory サービス（remember / recall / explore / forget / restore / revalidate）
- `services/memory.py` に 6 関数を移植。各関数は Pydantic を返す
- `services/formatters.py` に現 MCP の文字列整形を切り出し
- `mcp_server.py` の該当 6 ツールを「service → formatter」の 2 行に書き換え
- `core/types.py` に不足するレスポンスモデルを追加（例: `RememberResponse`, `ForgetResponse`, `RevalidateResponse`, `ExploreResponse`）
- **REST はまだ触らない**。この Phase はリファクタのみで、既存テストで parity を証明する
- 完了条件: pytest 112/112 + ruff clean

### Phase S2: REST Phase A+ 拡張
- `app.py` に `POST /remember` / `POST /recall` / `POST /explore` / `POST /forget` / `POST /restore` / `POST /revalidate` を追加
- 既存 `POST /index` / `POST /query` は **維持**（後方互換）。内部で新サービスを呼ぶだけのラッパに書き換え
- 新規テスト: `tests/integration/test_rest_memory.py`（httpx.AsyncClient + 既存 StubEmbedder 流用）
- 完了条件: REST で remember → recall → forget のラウンドトリップが通る

### Phase S3: reflection / relations / maintenance サービス
- `services/reflection.py` — reflect の全 aspect（summary/hot_topics/connections/dormant/duplicates/relations/tasks_*/commitments/values/intentions/relationships/persona）を aspect ごとに別関数に分離し、Pydantic を返す
- `services/relations.py` — relate / unrelate / get_relations
- `services/maintenance.py` — merge / compact / prefetch / prefetch_status
- `services/ingest_service.py` — ingest
- `services/memory.py` に `auto_remember` を追加
- MCP ツールを全て service → formatter 形式に書き換え
- 完了条件: pytest 112/112 + ruff clean

### Phase S4: Phase D サービス
- `services/phase_d.py` — commit / start / complete / abandon / depend / declare_value / declare_intention / declare_commitment / inherit_persona
- MCP 側を書き換え
- 完了条件: `tests/integration/test_mcp_phase_d.py` が一切変更なしで通ること

### Phase S5: REST の残り parity
- Phase B/C/D 相当のエンドポイントを `app.py` に追加（サービス層は既にあるので 1 ルートあたり 5 行程度）
- `reflect` は `POST /reflect/{aspect}` 形式で実装
- OpenAPI スキーマを自動生成して `docs/wiki/Operations-REST-API.md`（新設）に添付
- 新規テスト: `tests/integration/test_rest_phase_d.py`, `tests/integration/test_rest_reflection.py`
- 完了条件: MCP と REST の機能カバレッジが一致（`/reset` を除く）

### Phase S6: ドキュメント更新
CLAUDE.md のチェックリストに従って:
- `SKILL.md`: 変更不要（MCP プロトコル仕様は変わらない）想定だが、念のため diff を取り同期
- `docs/wiki/MCP-Reference-*`: サービス内部構造には触れない。API は不変
- `docs/wiki/Operations-REST-API.md`: **新設**。全エンドポイント + OpenAPI スキーマ抜粋
- `docs/wiki/Architecture-Overview.md`「設計判断の記録」表: 今計画の決定を追加
- `docs/wiki/_Sidebar.md` + `Home.md`: REST API ページを追加
- `docs/wiki/Plans-Roadmap.md`: Phase S 完了マーク
- `README.md` / `README_ja.md`: REST の位置付けを更新（「MCP と parity」を明記）

## 4. サービス関数 ↔ MCP ツール ↔ REST エンドポイント対応表

> サービス関数は全て `async def`。引数名は MCP ツールと一致させる。

| サービス関数 | MCP ツール | REST エンドポイント | 追加 Pydantic モデル |
|---|---|---|---|
| `services.memory.remember` | `remember` | `POST /remember` | `RememberResponse` |
| `services.memory.recall` | `recall` | `POST /recall` | 既存 `QueryResponse` を拡張 |
| `services.memory.explore` | `explore` | `POST /explore` | `ExploreResponse` |
| `services.memory.forget` | `forget` | `POST /forget` | `ForgetResponse` |
| `services.memory.restore` | `restore` | `POST /restore` | `RestoreResponse` |
| `services.memory.revalidate` | `revalidate` | `POST /revalidate` | `RevalidateResponse` |
| `services.memory.auto_remember` | `auto_remember` | `POST /auto_remember` | `AutoRememberResponse` |
| `services.reflection.summary` | `reflect(aspect="summary")` | `POST /reflect/summary` | `ReflectSummaryResponse` |
| `services.reflection.hot_topics` | `reflect(aspect="hot_topics")` | `POST /reflect/hot_topics` | `ReflectHotTopicsResponse` |
| `services.reflection.connections` | `reflect(aspect="connections")` | `POST /reflect/connections` | `ReflectConnectionsResponse` |
| `services.reflection.dormant` | `reflect(aspect="dormant")` | `POST /reflect/dormant` | `ReflectDormantResponse` |
| `services.reflection.duplicates` | `reflect(aspect="duplicates")` | `POST /reflect/duplicates` | `ReflectDuplicatesResponse` |
| `services.reflection.relations_overview` | `reflect(aspect="relations")` | `POST /reflect/relations` | `ReflectRelationsResponse` |
| `services.reflection.tasks_todo` etc. | `reflect(aspect="tasks_*")` | `POST /reflect/tasks_*` | 各 aspect 専用 |
| `services.reflection.persona` | `reflect(aspect="persona")` | `POST /reflect/persona` | `PersonaResponse` |
| `services.relations.relate` | `relate` | `POST /relations` | `RelateResponse` |
| `services.relations.unrelate` | `unrelate` | `DELETE /relations` | `UnrelateResponse` |
| `services.relations.get_relations` | `get_relations` | `GET /relations/{node_id}` | `RelationsResponse` |
| `services.maintenance.merge` | `merge` | `POST /merge` | `MergeResponse` |
| `services.maintenance.compact` | `compact` | `POST /compact` | `CompactResponse` |
| `services.maintenance.prefetch` | `prefetch` | `POST /prefetch` | `PrefetchResponse` |
| `services.maintenance.prefetch_status` | `prefetch_status` | `GET /prefetch/status` | `PrefetchStatusResponse` |
| `services.phase_d.commit` | `commit` | `POST /tasks` | `CommitResponse` |
| `services.phase_d.start` | `start` | `POST /tasks/{id}/start` | `StartResponse` |
| `services.phase_d.complete` | `complete` | `POST /tasks/{id}/complete` | `CompleteResponse` |
| `services.phase_d.abandon` | `abandon` | `POST /tasks/{id}/abandon` | `AbandonResponse` |
| `services.phase_d.depend` | `depend` | `POST /tasks/{id}/depend` | `DependResponse` |
| `services.phase_d.declare_value` | `declare_value` | `POST /persona/values` | `DeclareResponse` |
| `services.phase_d.declare_intention` | `declare_intention` | `POST /persona/intentions` | `DeclareResponse` |
| `services.phase_d.declare_commitment` | `declare_commitment` | `POST /persona/commitments` | `DeclareResponse` |
| `services.phase_d.inherit_persona` | `inherit_persona` | `GET /persona` | `PersonaResponse` |
| `services.ingest_service.ingest` | `ingest` | `POST /ingest` | `IngestResponse` |
| （REST 専用） | — | `POST /reset` | 既存 `ResetResponse` |
| （REST 既存） | — | `POST /index` | 既存（`remember` のラッパ化） |
| （REST 既存） | — | `POST /query` | 既存（`recall` のラッパ化） |
| （REST 既存） | — | `GET /node/{id}` | 既存 |
| （REST 既存） | — | `GET /graph` | 既存 |

## 5. テスト戦略

### 5.1 安全網の順序

1. **既存 MCP 統合テスト 112 ケースが常に通る** — Phase S1 以降、各 commit で pytest を green に保つ
2. 各サービス関数に **直接テスト** を追加（`tests/unit/test_services_*.py`）— Pydantic 戻り値の構造を固定
3. 各 REST エンドポイントに **ラウンドトリップテスト** を追加（`tests/integration/test_rest_*.py`）— httpx.AsyncClient 経由

### 5.2 既存テストが壊れないための契約

- MCP ツールの戻り値文字列は **1 文字も変えない**（formatter を既存実装からコピーして切り出すのみ）
- `_save_memory` ヘルパーは `services/memory.py` 内の private 関数として残す（or `services/_helpers.py` に移す）
- `StubEmbedder` パターン（`tests/integration/test_engine_archive_ttl.py`）はサービス層テストでも流用

### 5.3 ベンチマーク

Phase S5 完了時に `scripts/run_benchmark_isolated.sh 200` で p50 退行なしを確認。サービス層が 1 層挟まるがほぼ関数呼び出し 1 段増えるだけで、hot path は engine 内部のまま。

## 6. リスクとトレードオフ

### 6.1 既知のリスク

| リスク | 軽減策 |
|---|---|
| MCP 出力文字列の偶発的変化で LLM 挙動が変わる | formatter は既存コードの逐語コピー。MCP 統合テストで担保 |
| Pydantic モデルの追加で `core/types.py` が肥大化 | 25+ モデル増加の可能性。必要なら `core/types/` ディレクトリ化も検討（Phase S3 以降で判断） |
| サービス関数と MCP ツールの名前衝突 | モジュール階層（`services.memory.remember` vs `mcp.tools.remember`）で回避 |
| REST レスポンスモデルのフィールド名ブレ | MCP formatter が読むフィールド名 = REST JSON のフィールド名。命名を最初に確定させる |
| 実装中に MCP 破壊的変更を誘発 | 各 Phase で `tests/integration/test_mcp_phase_d.py` ほか既存テストを **無変更で** pass させる |

### 6.2 採用しなかった代替案

**代替案 A**: REST を MCP ツールの文字列レスポンスごと JSON に包む
- Pro: 実装量が 1/3 程度
- Con: REST クライアントが人間向け文字列を正規表現でパースする羽目に → 長期コスト高
- 結論: 却下

**代替案 B**: MCP を廃止して REST に統一、LLM には OpenAPI spec を見せる
- Pro: 真のシングルソース
- Con: MCP エコシステム（Claude Code / Desktop 連携）を失う。現状の LLM 体験を大幅ダウン
- 結論: 却下

**代替案 C**: サービス層を作らず、MCP ツール関数を直接 REST から import
- Pro: 新規ファイル 0
- Con: MCP ツールが文字列を返すため、REST で使い物にならない。MCP ツールデコレータの型シグネチャが FastAPI 依存注入と噛み合わない
- 結論: 却下

## 7. 完了条件

本計画の完了 = 以下の全てが満たされること:

- [ ] `gaottt/services/` 下に 7 モジュール（runtime / memory / reflection / relations / phase_d / maintenance / ingest_service）+ formatters が存在
- [ ] MCP ツール 25 本すべてがサービス層経由（各ツール実装は 3-5 行）
- [ ] REST エンドポイントが MCP parity（`/reset` 除く）
- [ ] pytest 既存 112 ケース + 新規サービス/REST テストすべて green
- [ ] ruff pre-existing 4 件以外の新規違反なし
- [ ] `scripts/run_benchmark_isolated.sh 200` で p50 < 50ms 維持
- [ ] `docs/wiki/Operations-REST-API.md` 新設、`_Sidebar.md` / `Home.md` 更新
- [ ] `docs/wiki/Architecture-Overview.md` 設計判断表に追記
- [ ] `docs/wiki/Plans-Roadmap.md` に Phase S 完了マーク

## 8. 参考リンク

- 現行 REST: [`gaottt/server/app.py`](../../gaottt/server/app.py)
- 現行 MCP: [`gaottt/server/mcp_server.py`](../../gaottt/server/mcp_server.py)
- Pydantic モデル置き場: [`gaottt/core/types.py`](../../gaottt/core/types.py)
- 実装フロー（新機能追加時の既定ルート）: [`../../CLAUDE.md`](../../CLAUDE.md) §「実装フロー」
- ドキュメント更新チェックリスト: [`../../CLAUDE.md`](../../CLAUDE.md) §「ドキュメント更新フロー」
