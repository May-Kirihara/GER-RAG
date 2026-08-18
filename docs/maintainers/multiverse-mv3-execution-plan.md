# MV3 Execution Plan — Universe Supervisor + Multiverse Layout

> 起票: 2026-07-02
> リスク分類: **high-risk**（新規制御面 / 認証 surface / プロセス spawn / ASGI middleware / filesystem permission）
> 前提: MV0 (manifest) / MV1 (embedding service) / MV2 (owner lease) 完了済み
> SoT: [implementation-plan §MV3](multiverse-implementation-plan.md#mv3--universe-supervisor--multiverse-layout)

## 目標

ユーザー→宇宙のルーティングと宇宙 engine のライフサイクル管理。1 ホストで複数テナントの宇宙を独立 data_dir で運用し、API key でルーティング、supervisor が backend を spawn/respawn する。

## スコープ（本 stage でやること）

1. **multiverse layout + local registry**（`gaottt/multiverse/registry.py`）— port 割当 / key hash / filesystem scan reconcile
2. **backend token middleware**（`mcp_server.py` に ASGI middleware 追加）— `GAOTTT_BACKEND_TOKEN` 設定時のみ `Authorization: Bearer` 検証、未設定は素通し（default 不変）
3. **supervisor**（`gaottt/multiverse/supervisor.py`）— FastAPI port 7880 / admin API / `/route` / per-universe backend ensure + spawn + token 生成/永続化 / embedder 検証
4. **shim supervisor mode**（`mcp_proxy.py` に `--supervisor-url` 追加）— `/route` 経由で接続先解決
5. **integration tests** — 6 シナリオ（相互不可視性 / idle respawn / spawn 競合ロック / 不正キー / token 経路 / supervisor 再起動）
6. **docs** — Operations-Multiverse-Setup 新規 + Sidebar/Home + Architecture 設計判断表

## 非スコープ（やらないこと）

- **engine コードへの能力追加** — MCP 新ツール / REST 新エンドポイント 0（管理面は parity 対象外）
- **physics 層への一切の接触** — gravity / scorer / mass / displacement / velocity は 1 行も変更しない
- **REST 経路の宇宙提供** — v1 では MCP のみ（managed 宇宙は lease で二重 engine を拒否するため REST app が構造的に開けない）
- **Postgres control plane** — MV4
- **Litestream backup** — MV5

## 設計判断（implementation-plan から継承、確定済み）

| 判断 | 仕様 | 根拠 |
|---|---|---|
| 信頼境界 | `multiverse_root` は 0700 / manifest / owner.lock / backend.token は 0600。同一 OS ユーザー内の改変は v1 信頼境界外 | Codex レビュー反映 |
| port レンジ | 7890–7989 動的割当（100 宇宙/ホスト上限 = v1 制約） | 計画 §4 Stage 3 |
| key hash | SHA-256。平文は発行時に一度だけ返す | 計画 §4 Stage 3 |
| backend token | supervisor が spawn 時に乱数生成 → env + `<universe_dir>/backend.token` (0600) | Codex レビュー反映 |
| token 再起動 | supervisor 再起動時は `backend.token` 読み戻しで既存 backend route 継続 | 再レビュー反映 |
| embedder 検証 | 宇宙作成時に embedding service `/info` に照会、取れなければ 400 | 再レビュー反映 |
| spawn env | 継承 env に頼らず明示構築（proxy backend env 継承罠の解消） | 計画 §4 Stage 3 |
| spawn 上限 | semaphore default 3（cold respawn spike 対策） | 計画 §5 |
| REST 不提供 | v1 は MCP のみ（lease 構造的拒否 + parity 鉄則は管理面に適用されない） | Codex レビュー反映 |
| probe / ping token | token 有効時に `Authorization: Bearer` を付ける。ping 付け忘れ = dead-man-switch 誤発動 regression | 再レビュー反映 |

## 実装アプローチ

### WP-1: config knobs + registry（test-first）

**config knobs**（`gaottt/config.py`）:

| knob | default | 意味 |
|---|---|---|
| `multiverse_root: str = ""` | `""` (未設定 = 機能不使用) | `GAOTTT_MULTIVERSE_ROOT`。空なら supervisor/shim 共に supervisor mode 不使用 |
| `supervisor_port: int = 7880` | 7880 | supervisor HTTP listen port |
| `supervisor_admin_key: str = ""` | `""` | admin API key（**空 = supervisor 起動 fail-fast**、admin endpoint を絶対に unauthenticated で露出しない）。環境変数 `GAOTTT_SUPERVISOR_ADMIN_KEY` 推奨 |
| `universe_port_range_start: int = 7890` | 7890 | 動的 port 割当開始 |
| `universe_port_range_end: int = 7989` | 7989 | 動的 port 割当終了（100 宇宙上限） |
| `supervisor_spawn_concurrency: int = 3` | 3 | 同時 spawn 上限 semaphore |
| `supervisor_readiness_timeout: float = 90.0` | 90.0 | spawn readiness poll timeout |

> **★ `backend_token_enabled` knob は作らない**（Codex review B3 反映）: token middleware の発動スイッチは **`GAOTTT_BACKEND_TOKEN` env の有無そのもの**。boolean knob を作ると「token 設定済みだが middleware 無効」の dangerous state が生まれる。`GAOTTT_BACKEND_TOKEN` 未設定 → 素通し（default 不変、既存 7878 無影響）/ 設定済み → 全リクエストで `Authorization: Bearer` 検証。supervisor が spawn 時にこの env を注入する。

**registry**（`gaottt/multiverse/registry.py`）:
- `aiosqlite` local SQLite `<multiverse_root>/registry.db`
- schema: `universes(universe_id PK, owner_label, port, status, embedder_id, embedder_version, created_at)`, `api_keys(key_hash PK, universe_id, created_at, revoked_at)`
- `port 割当`: `universe_port_range_start..end` から registry の `port` 列に未使用の最小値を選ぶ。**割当前に OS port bind check を実施**（`socket.bind` → close で実際に空いているか確認、Codex review non-blocking 反映）。registry だけ見て OS が使っている port を配ると backend spawn が `Address already in use` で落ちる
- `key hash`: `hashlib.sha256(plaintext.encode()).hexdigest()`。**平文キーは `secrets.token_urlsafe(32)` で生成**（CSPRNG、十分な entropy — Codex review security 反映）。SHA-256 only storage は高 entropy ランダムキー前提で acceptable
- `key 比較`: 全認証比較（admin key / API key / backend token）は **`secrets.compare_digest`** を使用（timing attack 防御 — Codex review security 反映）
- `reconcile()`: startup 時に `<multiverse_root>/universes/` を scan → registry と突き合わせ。ディレクトリあり registry なし → 追加（manifest から embedder_id 等を読む、manifest 無し/破損は WARNING + skip）。ディレクトリなし registry あり → status を `orphan` に mark（自動削除しない、admin が判断）。`trash/` ディレクトリは scan 対象外
- 削除: `universe_dir` を `<multiverse_root>/trash/` へ move（即時物理削除しない）。**move 前に backend 停止を確認**（port probe で backend が応答しなくなるまで待つ、timeout 付き — Codex review non-blocking 反映）

**unit tests**（`tests/unit/test_multiverse_registry.py`）:
- port 割当: 重複しない / range 枯渇で error
- key hash: 同一平文 → 同一 hash / 異平文 → 異 hash
- scan 突き合わせ: ディレクトリあり registry なし → 追加 / ディレクトリなし registry あり → orphan mark
- trash move: 削除で trash/ へ移動、即時 rm しない

### WP-2: backend token middleware（`mcp_server.py`）

`_install_token_middleware`（`_install_idle_watcher` と同パターン）:
- `GAOTTT_BACKEND_TOKEN` env が設定時のみ `Authorization: Bearer <token>` を検証
- 未設定 → 素通し（default 不変、既存 7878 単一 backend 無影響）
- **Bearer parsing spec**（Codex review security 反映）: `Authorization: ` prefix で始まる場合のみ parse、`Bearer ` の次の token を比較。malformed header（`Bearer` のみ / 複数 token / 不正 prefix）→ 401。header 不在 → 401
- **token 比較は `secrets.compare_digest`**（timing attack 防御）
- **401 応答は idle activity を refresh しない**（認証前のリクエストで watchdog timer をリセットすると、brute-force 攻撃が idle shutdown を妨げる — Codex review security 反映）
- middleware は `streamable_http_app` / `sse_app` の両方に patch（idle watcher と同じ）。**middleware 順序**: token middleware が **外側**（idle watcher より先に dispatch）、認証失敗は activity refresh に届かない
- token 不一致 / header 不在 / malformed → 401 JSON `{"detail": "Unauthorized"}`（secret を含まない）

**unit tests**:
- token 未設定 → 全リクエスト素通し
- token 設定 + 正しい Bearer → 通過
- token 設定 + 不一致 Bearer → 401
- token 設定 + ヘッダなし → 401
- token 設定 + malformed Bearer（`Bearer` のみ / `Basic xxx` / 複数 token）→ 401
- 401 応答は idle activity を refresh しない（watchdog timer が進む）
- ping/probe が token 付きで通る
- **admin key 空 = supervisor 起動 fail-fast**（Codex review B2 反映）

### WP-3: supervisor（`gaottt/multiverse/supervisor.py`）

FastAPI app、port 7880、localhost bind:

| endpoint | auth | 動作 |
|---|---|---|
| `POST /admin/universes {owner_label, embedder_id?}` | admin key | 宇宙作成: embedder `/info` 検証 → dir + manifest(managed=True) + port 割当 + API key 発行 |
| `DELETE /admin/universes/{id}` | admin key | backend 停止 → dir を `trash/` へ move |
| `GET /admin/universes` | admin key | 一覧 + 稼働状態 |
| `POST /route {api_key}` | 宇宙 key | key → universe 解決 → backend ensure → `{url, token}` |

`_ensure_backend`（per-universe 版）:
- **`asyncio.Lock` per universe_id + file lock `<universe_dir>/.spawn.lock`**（Codex review B1 反映）。file lock は supervisor プロセス再起動時や複数 supervisor instance でも spawn 競合を防止。`fcntl.flock(LOCK_EX)` で臨界区間内で spawn + readiness poll を行う
- port probe（`_probe_backend` 流用、token 付き）→ 生きていれば URL + token 返却
- spawn: `_spawn_backend_detached` 相当だが **env を明示構築**（継承 env に頼らない）
  - `GAOTTT_DATA_DIR`, `GAOTTT_EMBEDDER_ENDPOINT`, `GAOTTT_OWNER_LEASE_ENABLED=true`, `GAOTTT_BACKEND_TOKEN=<generated>`, manifest 由来 knob のみ allowlist
  - **`GAOTTT_BACKEND_TOKEN` の権威**（Codex review B3 反映）: supervisor が `secrets.token_urlsafe(32)` で生成。この env の有無が backend の token 検証 ON/OFF を決める（boolean knob なし）
- token 永続化: `<universe_dir>/backend.token` (0600)
- readiness poll (90s)
- spawn semaphore (default 3)
- **token stale path**（Codex review missing test 反映）: probe が 401 を返した場合 = 「backend は生きているが自分の token が古い」→ `backend.token` ファイルを読み直して再 probe。それでも 401 なら re-spawn

supervisor 再起動:
- `backend.token` 読み戻し → 既存 backend への route 継続
- registry `reconcile()` → ディレクトリと突き合わせ

**unit tests**:
- create_universe: embedder 検証 pass → dir + manifest(managed=True) + port + key
- create_universe: embedder 検証 fail → 400
- route: 正しい key → URL + token
- route: 不正 key → 401
- route: 存在しない universe → 404
- delete: trash へ move

### WP-4: shim supervisor mode（`mcp_proxy.py`）

`run_proxy` に optional 引数:
- `supervisor_url: str | None`, `api_key: str | None`
- 指定時: `POST /route {api_key}` → 返った URL + token で接続（自前 spawn しない）
- 接続断 → route 再取得 → 再接続
- 未指定時: 現行 7878 auto-spawn 経路そのまま（default 不変）

CLI: `--supervisor-url http://127.0.0.1:7880` + `GAOTTT_API_KEY`
- `_Upstream` が `Authorization: Bearer` ヘッダを streamablehttp_client の headers に付ける

**unit tests**:
- supervisor_url 指定 → /route 経由で URL 解決
- supervisor_url 未指定 → 現行経路（default 不変）
- route 取得失敗 → error

### WP-5: integration tests（`tests/integration/test_supervisor.py`）

StubEmbedder service + 短い idle_timeout で（**port は per-test ephemeral range**、Codex B4 反映）:
1. 宇宙 A/B 作成 → A に remember → B の recall に出ない（**相互不可視性**）
2. idle → backend 自然消滅 → 再 route で respawn → データ保持
3. 同一宇宙へ並行 route ×5 → backend は 1 つ（spawn 競合ロック、**file lock 含む**）
4. 不正キー → 401
5. **token 経路**: token なし直叩き → 401 / token 付き probe・ping が通り dead-man-switch が誤発動しない
6. **supervisor 再起動**: 稼働中 backend を残したまま supervisor restart → `backend.token` 読み戻しで route 継続（re-spawn されないこと）
7. **admin auth**: 空 admin key → supervisor 起動 fail-fast / 正しい admin key → admin endpoint 通過 / 不正 → 401（Codex B2 反映）
8. **revoked key**: revoke された API key → `/route` で 401（Codex missing test 反映）
9. **delete 停止確認**: `DELETE /admin/universes/{id}` が backend 停止を確認してから trash move（Codex non-blocking 反映）
10. **file modes**: `multiverse_root` 0700 / `manifest.json` 0600 / `owner.lock` 0600 / `backend.token` 0600（Codex security 反映）
11. **port occupied**: OS port を別プロセスが持っている状態で宇宙作成 → 別 port に割当（Codex non-blocking 反映）
12. **token stale**: probe 401 → token file reread → route 成功 → それでも 401 → re-spawn（Codex missing test 反映）

### WP-6: docs

- 新規 [Operations — Multiverse Setup](../wiki/Operations-Multiverse-Setup.md)
- `docs/wiki/_Sidebar.md` + `Home.md` 更新
- [Architecture — Overview](../wiki/Architecture-Overview.md) 設計判断表に「supervisor API は MCP/REST parity 対象外（管理面）」追記
- [Operations — Tuning](../wiki/Operations-Tuning.md) に knob 追加

## acceptance criteria

1. **default 不変**: `multiverse_root` 未設定で supervisor も shim supervisor mode も起動しない。既存 suite 全緑 + 両 smoke green
2. **相互不可視性**: 宇宙 A の記憶が宇宙 B の recall に出ない
3. **idle respawn**: backend が自然消滅後、再 route で respawn しデータ保持
4. **spawn 競合ロック**: 同一宇宙への並行 route で backend は 1 つ
5. **不正キー拒否**: 不正 API key → 401
6. **token 経路**: token なし直叩き → 401 / token 付き probe・ping で dead-man-switch 誤発動しない
7. **supervisor 再起動**: 稼働 backend を残した restart で token 読み戻し → route 継続（re-spawn しない）
8. **manifest managed=True**: supervisor が作る宇宙は manifest.managed=True（MV2 lease 強制が効く）
9. **embedder 検証**: 宇宙作成時に `/info` 照会、取れなければ 400
10. **permission**: `multiverse_root` 0700 / `manifest.json` 0600 / `owner.lock` 0600 / `backend.token` 0600
11. **admin key fail-fast**: 空 admin key で supervisor 起動不可（unauthenticated admin を防ぐ）
12. **token stale recovery**: probe 401 → token reread → 成功 / 不可なら re-spawn
13. **revoked key**: revoke された API key → 401
14. **OS port check**: port 割当前に OS bind check、別プロセスが持っていれば別 port

## test strategy

- **unit**: registry / token middleware / supervisor API 個別（StubEmbedder、MockTransport 相当）
- **integration**: 実プロセス（StubEmbedder embedding service を uvicorn background thread で、supervisor も background、短い idle_timeout で自然消滅を現実的時間で検証）
- **smoke**: `rest_smoke.py` / `mcp_smoke.py` が default OFF で green（回帰 guard）
- **perf**: retrieval geometry に触れないので `tests/perf/` は実行不要（CLAUDE.md 規約：hot path / config default / retrieval geometry に触れた場合のみ）

## 削除・スキップするテスト

なし。既存テストは一切変更しない。

## risks

| リスク | 緩和 |
|---|---|
| integration test で実プロセスが不安定 | StubEmbedder + 短い idle_timeout で高速化、timeout を generous に |
| port 衝突（test で動的割当） | test は独立 tmp multiverse_root で隔離 |
| token middleware が既存 7878 に影響 | `GAOTTT_BACKEND_TOKEN` 未設定 = 素通し、integration test で明示 |
| supervisor spawn env 継承罠 | 明示 env 構築、allowlist のみ |

## assumption ledger

| # | assumption | basis | falsification condition | blast radius |
|---|---|---|---|---|
| A1 | `streamablehttp_client` が `headers` kwarg を受け付ける（shim が Bearer token を付ける） | MCP SDK の client API 慣行 | 実装時に SDK signature 確認、不可なら `extra_headers` or transport-level | shim の token 付与経路全体 |
| A2 | `BaseHTTPMiddleware` に token check を追加しても idle watcher と両立する | 同じ patch パターン（`streamable_http_app` wrapping）で 2 つの middleware が積める | 積めない場合は 1 つの middleware に統合 | middleware 実装方式 |
| A3 | integration test が tmp multiverse_root + StubEmbedder service で完結する | MV1 の `test_engine_remote_embedder.py` が同パターンで成功 | 実プロセスの spawn/port bind が test env で安定しない | integration test 全体 |
| A4 | registry.db と universe data_dir の reconcile で「ディレクトリが正」が一貫する | 計画 §4 Stage 3 の source-of-truth の向き | 同時削除/作成 race で orphan/ghost が発生 | registry 整合性 |
| A5 | port range 7890-7989 が test で衝突しない | **修正（Codex B4 反応）**: test は host-global port を使うため、固定 range ではなく **per-test で ephemeral range を割当てる**（例: `universe_port_range_start` を test 開始時に空いている range から動的選択）。`socket.bind((127.0.0.1, 0))` で OS から空き port を取得 → そこから連続 N 個を test range とする | CI 並行で衝突 | test 安定性 |
| A6 | `streamablehttp_client(url, headers=...)` が Bearer header を伝播する | MCP SDK の client API（A1 と統合、Codex が有効確認済み） | SDK 確認で不可の場合 `extra_headers` or transport-level に fallback | shim token 付与経路 |

## WP 順序

```
WP-1 (config + registry, test-first)
  ↓
WP-2 (token middleware)     ← WP-1 と並行可（file disjoint）
  ↓
WP-3 (supervisor)
  ↓
WP-4 (shim)
  ↓
WP-5 (integration tests)
  ↓
WP-6 (docs)
```

各 WP 完了後に PM 検証（git status / diff / test 実行）。WP-1+WP-2 は並行実施（file ownership disjoint: `config.py`+`registry.py` vs `mcp_server.py`）。

## gate plan

| gate | 実施 |
|---|---|
| GaOTTT recall | ✅ 済 |
| Planning doc | ✅ 本ファイル |
| Codex plan review | ✅ 本ファイルに対して実施 |
| QA plan review | ⭕（high-risk だが既存計画の具現化、スコープ明確） |
| Test-first delegation | ✅（WP-1〜WP-4 各 test-first） |
| Codex test-diff review | ✅ |
| QA test-diff review | ⭕ |
| Implementation delegation | ✅（各 WP） |
| Test execution | ✅ |
| PM diff inspection | ✅ |
| Codex final diff review | ✅ |
| QA final review | ✅ |
| GaOTTT writeback | ✅ |

## 仮登録予測（pre-registered predictions）

- WP-1 registry unit tests: port 割当の重複検出と range 枯渇が正しく error になる
- WP-2 token middleware: `GAOTTT_BACKEND_TOKEN` 未設定時は全リクエスト素通し（既存 smoke が green で確認）
- WP-3 supervisor: `/route` が正しい key で URL+token を返す
- WP-5 integration: 6 シナリオ全通過、特に token なし直叩き 401 と supervisor 再起動 route 継続
- full suite: 908+ 件が 0 失敗（default OFF で既存挙動不変）
