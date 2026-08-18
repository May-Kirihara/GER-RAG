# Plan: Embedder Lazy Spawn by Supervisor (v3)

> 起票: 2026-07-06
> v2: 2026-07-06 — Codex v1 review (revise) の 6 blocking + non-blocking + missing tests + WP 細分化を反映
> **v3: 2026-07-06 — Codex v2 review (revise、方針は approve 寄り) の新 blocking 6 件 + non-blocking + missing tests + docs gaps + WP リスク対応を反映。3 round 目の Codex review はスキップ → implementer 委任（PM second-failure re-diagnosis適用）**
> Status: **draft v3（ユーザー + Codex v1/v2 review 済み → implementer 委任）**
> Risk: **normal-high**
> Owner: PM（plan / verification）→ implementer（実装）

---

## Revision History

- **v1**: 初版起稿。Codex v1 review で revise 判定（6 blocking、**構造的問題**: sync/async 混在・mock seam 破壊・`/route` 経路見落とし等）
- **v2**: Codex v1 feedback を全面的に反映。状態遷移表・race 2 層（`asyncio.Lock` + `fcntl.flock`）・watchdog lifecycle・`/route` での `/info` 検証を追加
- **v3**: Codex v2 review (revise 判定、**方針は approve 寄り**) を反映。新 blocking 6 件（**実装ディテール**: mock seam 実態・event loop blocking・cache invalidation・例外 mapping 等）+ non-blocking + missing tests + docs gaps + WP リスク対応を盛り込み

### PM 自己診断（second-failure re-diagnosis ルール適用）

- **v1 失敗**: 既存コード（`_validate_embedder` が sync、`/route` が `_validate_embedder` を呼ばない）を grep 結果から読んだが、呼び出し階層まで追ってなかった
- **v2 失敗**: 既存パターン（`fcntl.flock` の `asyncio.to_thread` 経由・PermissionError → conflict 扱い・`EmbedderValidationError` の 503/400 mapping）を「抽象的な踏襲」として書いたが、**具体的なコード行レベルで確認しなかった**
- **v3 戦略変更**: 同じ「plan を深める」の 3 round 目より、implementer に plan + Codex v2 review 結果を渡して実装修正ループで回す方が効率的と判断（ユーザー承認）。blocking の性質が「構造的」→「既存パターン踏襲のディテール」に移行したので、残りは implementer の責務範囲

### implementer への引き継ぎ事項

implementer は本 plan doc と `/tmp/codex-review-embedder-spawn-v2.md`（Codex v2 review フル結果）の**両方**を読んで実装すること。v2 review に書かれた blocking 6 件 + non-blocking + missing tests + docs gaps + WP risks の**全て**に対応すること。

---

## 1. Goal / Background（v1 から不変、詳細は v1 doc 参照）

開発機で systemd 常駐させずに multiverse を利用するため、supervisor が embedding service を lazy spawn し、全 backend idle で SIGTERM で落とす。

---

## 2. Scope / Non-goals（v2 から不変）

詳細は v2 doc 参照。本 v3 は実装ディテールの追加のみ。

---

## 3. Approach v3 — v2 への差分追加

### 3.1 `_Supervisor` の embedder lifecycle state（v2 から不変）

3 状態（`unowned` / `owned_idle` / `owned_terminating`）・10 遷移の状態機械。詳細は v2 doc §3.1 参照。

### 3.2 `ensure_embedder_up()` API — v3 で B2-1 + B2-3 対応で修正

**B2-1（既存 mock seam が handler 経路で壊れる）対応**:

`ensure_embedder_up()` の**先頭で `_validate_embedder` を即座に試す**。`/info` が取れるなら `/healthz` 経路に行かない → 既存 `tests/unit/test_supervisor.py:57 _embedder_ok()`（`httpx.Client.get` for `/info` の mock）だけで `create_universe` が通る。これで「既存 mock seam を壊さない」が**本当に保証される**（v2 の表現は不正確だったので訂正）。

```python
async def ensure_embedder_up(self, *, validate_info: bool = True) -> dict:
    async with self._embedder_spawn_lock:
        # B2-1: まず _validate_embedder (/info) を叩く。既存 mock seam で通る経路を優先
        if self._embedder_info_cache is None:
            try:
                info = await asyncio.to_thread(_validate_embedder, self._config)
                # /info が取れた → embedder は既に立ってる (systemd or 前回 spawn 残存)
                if self._embedder_state == "unowned":
                    self._embedder_info_cache = info
                # owned_idle の場合は B2-3 の child 生存確認へ
                if self._embedder_state != "owned_idle":
                    return info
            except EmbedderValidationError:
                if not self._config.supervisor_spawn_embedder:
                    raise  # opt-out mode は従来通り即座に raise
                # /info 取れない → /healthz 経路（lazy spawn）へ

        # B2-3: owned cache は child 生存 + /healthz 確認してから返す
        if self._embedder_state == "owned_idle" and self._embedder_info_cache:
            if (self._embedder_pid is not None
                and _is_my_child_alive(self._embedder_pid)
                and await self._probe_embedder_health()):
                return self._embedder_info_cache
            # child 死亡 or /healthz NG → cache 破棄、spawn 経路へ
            self._reset_embedder_state()

        # B2-3 続き: unowned cache の確認
        if self._embedder_state == "unowned" and self._embedder_info_cache:
            if await self._probe_embedder_health():
                return self._embedder_info_cache
            self._embedder_info_cache = None  # 外部 embedder 落ちた

        # (3) lazy spawn 経路（v2 と同じ）
        if await self._probe_embedder_health():
            self._embedder_state = "unowned"
            self._embedder_pid = None
        elif self._config.supervisor_spawn_embedder:
            await self._spawn_embedder_owned()
            if self._embedder_state != "owned_idle":
                # race lost → unowned に倒れた
                if await self._probe_embedder_health():
                    self._embedder_state = "unowned"
                else:
                    raise EmbedderValidationError("embedder spawn race lost")
        else:
            raise EmbedderValidationError(
                "embedder service unreachable (supervisor_spawn_embedder=False)"
            )

        # (4) spawn 後にもう一度 /info（v2 と同じ）
        if validate_info and self._embedder_info_cache is None:
            info = await asyncio.to_thread(_validate_embedder, self._config)
            self._embedder_info_cache = info
        return self._embedder_info_cache
```

### 3.3 `_spawn_embedder_owned()` — v3 で B2-2 対応

**B2-2（`fcntl.flock` が event loop を塞ぐ）対応**: 既存 `ensure_backend`（[supervisor.py:397](../../gaottt/multiverse/supervisor.py)）と同じ `asyncio.to_thread` パターンに。

```python
async def _spawn_embedder_owned(self) -> None:
    lock_path = self._root / ".embedder.spawn.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        # B2-2: asyncio.to_thread で event loop を塞がない
        await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_EX)
        try:
            # flock 内で再確認
            if await self._probe_embedder_health():
                self._embedder_state = "unowned"
                return
            pid = _spawn_embedder_detached(host, port, log_path, model)
            # B2-5: readiness poll は bool 返し（詳細は §3.5）
            ready = await self._poll_embedder_readiness(pid)
            if ready:
                # race lost チェック
                if not _is_my_child_alive(pid) or not await self._probe_embedder_health():
                    self._embedder_state = "unowned"
                    self._embedder_pid = None
                    return
                self._embedder_pid = pid
                self._embedder_state = "owned_idle"
                self._last_backend_active_at = time.monotonic()
                # watchdog 開始（v2 と同じ）
                ...
            else:
                # readiness 失敗 → race lost 判定（詳細は §3.5）
                await self._handle_spawn_readiness_failure(pid)
        finally:
            await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
```

### 3.4 `_spawn_embedder_detached`（v2 から不変、Windows 分岐・FD close 含む）

詳細は v2 doc §3.4 参照。

### 3.5 `_poll_embedder_readiness` — v3 で B2-5 対応で契約変更

**B2-5（readiness timeout / race lost の契約が曖昧）対応**: `bool` を返す契約に変更。timeout 時は raise せず `False` を返し、呼び出し元（`_spawn_embedder_owned`）で race lost 判定に進む。

```python
async def _poll_embedder_readiness(self, pid: int) -> bool:
    """Returns True if embedder is /healthz-ready within timeout.
    
    Returns False on timeout or child death. Caller handles race lost.
    """
    deadline = time.monotonic() + self._config.embedder_spawn_readiness_timeout_seconds
    while time.monotonic() < deadline:
        if not _is_my_child_alive(pid):
            return False  # child が死んだ
        if await self._probe_embedder_health():
            return True
        await asyncio.sleep(1.0)
    return False  # timeout


async def _handle_spawn_readiness_failure(self, pid: int) -> None:
    """B2-5: spawn 直後の readiness 失敗を 4 パターンに分類して処理。
    
    1. child 死亡 + 外部 embedder 健康 → race lost、unowned に倒す
    2. child 死亡 + 外部 embedder 不健康 → spawn 失敗、EmbedderValidationError
    3. child 生存 + 外部 embedder 健康 → race lost（自分の child は Address already in use で殺す）、unowned
    4. child 生存 + 外部 embedder 不健康 → spawn 失敗、child を cleanup して EmbedderValidationError
    """
    external_ok = await self._probe_embedder_health()
    child_alive = _is_my_child_alive(pid)
    if external_ok:
        # race lost
        self._embedder_state = "unowned"
        self._embedder_pid = None
        if child_alive:
            # 自分の child を片付ける
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)  # zombie 回収
            except ChildProcessError:
                pass
        return
    # external も不健康 → spawn 失敗
    if child_alive:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
    raise EmbedderValidationError(
        f"spawned embedder pid={pid} did not become ready within "
        f"{self._config.embedder_spawn_readiness_timeout_seconds}s"
    )
```

### 3.6 `_terminate_embedder()` — v3 で B2-6 対応

**B2-6（termination path が所有権不整合を隠す）対応**: PermissionError や SIGKILL 後も alive の場合は state を `unowned` に**消さず**、既存 `_stop_backend`（[supervisor.py:575](../../gaottt/multiverse/supervisor.py)）の `_BackendAliveConflict` 相当の取り扱いに。`owned_terminating` を保持し、ERROR log で手動 recovery を促す。

```python
async def _terminate_embedder(self) -> None:
    if self._embedder_state != "owned_idle":
        return
    self._embedder_state = "owned_terminating"
    pid = self._embedder_pid
    if pid is None:
        self._reset_embedder_state()
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        self._reset_embedder_state()
        return
    except PermissionError:
        # B2-6: 既存 _stop_backend 踏襲。state を消さずに conflict 扱い
        logger.error(
            "embedder pid=%d PermissionError on SIGTERM (owned by another user?) — "
            "state stays owned_terminating, manual recovery required", pid,
        )
        return  # state は owned_terminating のまま（手動 recovery 待ち）

    if await self._wait_for_pid_exit(pid, timeout=5.0):
        self._reset_embedder_state()
        return

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        self._reset_embedder_state()
        return
    except PermissionError:
        logger.error(
            "embedder pid=%d PermissionError on SIGKILL — state stays owned_terminating", pid,
        )
        return

    if not await self._wait_for_pid_exit(pid, timeout=2.0):
        # B2-6: SIGKILL 後も alive → state を消さずに ERROR log
        logger.error(
            "embedder pid=%d survived SIGKILL — state stays owned_terminating, "
            "manual recovery required (kill -9 %d)", pid, pid,
        )
        return
    self._reset_embedder_state()
```

`owned_terminating` に留まった場合、次回 `ensure_embedder_up()` は `_embedder_state != "unowned" and != "owned_idle"` なので `asyncio.Lock` 内で待たず、`EmbedderValidationError` で失敗する（手動 recovery を強制）。

### 3.7 `/route` の例外 mapping — v3 で B2-4 対応（新規）

**B2-4（`/route` の `EmbedderValidationError` mapping 未定義）対応**: `/route` handler で catch して `HTTPException(503)` に mapping。`create_universe` は既存通り 400。

```python
# /route handler（supervisor.py 現行 ensure_backend 呼び出しの前、supervisor.py:838 周辺）
try:
    await sup.ensure_embedder_up()
except EmbedderValidationError as exc:
    logger.warning("embedder validation failed on /route: %s", exc)
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Embedder validation failed",
    )
url, token = await sup.ensure_backend(universe)
```

### 3.8 `/healthz` probe（v2 から不変）

`httpx.AsyncClient` で `/healthz` を叩く。既存 `httpx.Client.get`（`/info`）とは独立した seam。

### 3.9 supervisor lifespan shutdown（v2 から不変）

`_embedder_watchdog_task` を cancel → `_terminate_embedder()` を呼ぶ。

### 3.10 systemd 運用との両立（v2 から不変、ただし表現を精密化）

「本番 **正常な systemd embedder** が先に立っている場合の挙動は完全不変」（Codex v2 non-blocking 対応：systemd embedder が立っているが model/dim が不正な環境では、初回 `/route` の `/info` 検証で早めに失敗するようになる。これは良い変更だが、「完全不変」の表現は正常ケースに絞る）。

---

## 4. Acceptance Criteria（v2 の 12 件 + v3 で 4 件追加 = 16 件）

v2 の 1-12 に加えて:

13. **B2-1**: 既存 `tests/unit/test_supervisor.py` の `_embedder_ok()` だけで `create_universe` が通る（`/healthz` mock なしで）
14. **B2-3**: owned cache 状態で embedder child が死んだ後、次 `/route` が cache を返さず再 spawn する
15. **B2-4**: `/route` で `EmbedderValidationError` が 503 になる（`create_universe` は 400 のまま）
16. **B2-6**: `_terminate_embedder` で PermissionError / SIGKILL 後 alive の場合、state は `unowned` に消えず `owned_terminating` を保持、ERROR log が出る

---

## 5. Test Strategy（v3 で unit/integration に追加）

### unit に追加（Codex v2 missing tests 対応）
- **B2-1**: 既存 `_embedder_ok()` だけで create が通る regression test（`/healthz` mock なし）
- **B2-3**: owned cache + child 死亡 → 次 `/route` が再 spawn する test
- **B2-4**: `/route` で `EmbedderValidationError` が 503 になる test
- **B2-2**: `flock` が `asyncio.to_thread` 経由で呼ばれ、event loop を塞がない test（`asyncio.sleep` が別 task で進むことを確認）
- **B2-5**: `_handle_spawn_readiness_failure` の 4 パターン網羅 test
- **B2-6**: PermissionError / SIGKILL 後 alive の termination test
- **acceptance #6 具体化**: `multiprocessing` で同じ `multiverse_root` を使う 2 supervisor、共有 file counter または fake health sentinel で「Popen 1 回だけ」を検証
- **acceptance #7 具体化**: `_spawn_embedder_detached → pid`, `_is_my_child_alive → False`, `_probe_embedder_health → True` を組み合わせ、state が `unowned`・watchdog 未開始・info validation が続くことを assert

### integration に追加
- `/route` 経路で embedder down → lazy spawn → backend spawn の順序確認（v2 から）
- 上記 unit と同等の実プロセスでの確認

### suspicious test changes 対応
- 既存テストを通すために `supervisor_spawn_embedder=False` を広く入れるのは**禁止**（v2 Codex 指摘）。B2-1 対応で既存 mock seam がそのまま通るので、既存テストは無修正で green のはず
- `_probe_embedder_health` を丸ごと mock するだけでは `httpx.AsyncClient.get` seam の検証にならない。低レベル seam test と高レベル lifecycle test を分ける

---

## 6. Implementation Plan（WP 細分化 — v3 で WP-2a 拡張）

- **WP-1 (config)**: `gaottt/config.py` に 4 knob + `tests/unit/test_config.py` に env-override テスト
- **WP-2a (spawn tests, RED)**: `tests/unit/test_supervisor_embedder_spawn.py` 新設。**既存 `tests/unit/test_supervisor.py` の RED/互換確認（B2-1 regression test）を含む**。`_spawn_embedder_detached` / `_build_embedder_spawn_env` / `_probe_embedder_health` AsyncClient seam
- **WP-2b (spawn impl, GREEN)**: `_spawn_embedder_detached` / `_probe_embedder_health` / `_build_embedder_spawn_env` / `_spawn_embedder_owned`（B2-2 flock to_thread 対応）/ `_poll_embedder_readiness`（B2-5 bool 返し） / `_handle_spawn_readiness_failure`（B2-5 4 パターン） / `ensure_embedder_up` API（B2-1 + B2-3 対応）。`create_universe` / `/route` handler 組込（B2-4 例外 mapping 含む）
- **WP-3a (lifecycle tests, RED)**: watchdog / terminate（B2-6 PermissionError 含む）/ state 遷移 / race lost / zombie 回収 / untracked backend の各テスト
- **WP-3b (lifecycle impl, GREEN)**: `_embedder_idle_watchdog` / `_terminate_embedder`（B2-6 対応） / `_wait_for_pid_exit` / `_reset_embedder_state` / `_is_my_child_alive` / `_reap_dead_backend_pids` / `_has_tracked_live_backends` / `_has_untracked_live_backends`（non-blocking 改善：`_probe_backend_with_token` で stranger 区別、N 回延期後に warning） / lifespan shutdown 組込
- **WP-4 (integration tests)**: `tests/integration/test_supervisor_embedder_spawn.py`（実 `gaottt.embedding.service` + StubEmbedder）
- **WP-5 (docs)**: 後述の Docs Impact 12 ファイル

並列化: **直列**。WP-1 → (WP-2a → WP-2b) → (WP-3a → WP-3b) → WP-4 → WP-5。

---

## 7. Docs Impact（v2 の 10 ファイル + v3 で 2 件追加 = 12 ファイル）

| ファイル | 変更 |
|---|---|
| `docs/wiki/Operations-Tuning.md` | 新 config knob 4 つ追加 |
| `docs/wiki/Operations-Server-Setup.md` §embedding service | 「supervisor 経由で lazy spawn」運用を追加 |
| `docs/wiki/Operations-Multiverse-Setup.md` Step 0 | 「supervisor が lazy spawn する」に更新 |
| `docs/wiki/Operations-Multiverse-Operations.md` | 障害時の挙動を追記 |
| `docs/wiki/Operations-Multiverse-Import-Universe.md` | importer step 2 の embedder 停止可否明記 |
| `docs/wiki/Operations-Resource-Requirements.md` | 7879 手動起動と lazy spawn の関係を注記 |
| `docs/wiki/Operations-Troubleshooting.md` | lazy spawn troubleshooting 追加 |
| `docs/wiki/Architecture-Overview.md` 設計判断表 | 新設計判断 D-1 追加 |
| `docs/wiki/Plans-Multiverse-Scale-Out.md` §SPOF (line 190) | 設計判断更新 |
| `docs/notes/multiverse-migration-checklist.md` Step 0 | **必須更新**（現状「supervisor は embedding service を自動起動しません」と明記されているので確実に書き換え） |
| `deploy/gaottt-embedder.service` | コメントで「systemd 運用時。開発機は supervisor が lazy spawn」 |
| **`docs/wiki/Operations-Multiverse-Quick-Check.md`** (v3 追加) | "supervisor + embedder の 2 process 以上"前提と `systemctl restart gaottt-embedder` を更新（lazy spawn とズレるため） |
| **`docs/wiki/Operations-Backup-Multiverse.md`** (v3 追加) | "最初の `/route` で backend が spawn" 説明に embedder lazy spawn が前段に入ることを追記 |

---

## 8. Risks（v2 の R1-R6 + v3 で表現修正）

- **R1**: embedder spawn の readiness timeout（初回 RURI download で 90s 超過し得る）→ knob 化済み、runbook に明記
- **R2**: 既存 test_supervisor.py 破損 → **B2-1 対応で回避**（`ensure_embedder_up` の先頭で `_validate_embedder` を試すので既存 mock seam がそのまま通る）
- **R3**: 複数 supervisor プロセスの race → `fcntl.flock`（B2-2 で `to_thread` 経由）+ race lost 検出（B2-5）
- **R4**: SIGTERM を無視する embedder → 2 段階（SIGTERM 5s → SIGKILL 2s）+ `waitpid(WNOHANG)`
- **R5**: supervisor クラッシュ時に embedder が孤児化 → 別 supervisor 起動時に `/healthz` で検知、完全救済は v1 範囲外
- **R6**: `/info` cache が古くなる → **B2-3 対応**（`owned_idle` cache 返却前に child 生存 + `/healthz` 確認、`unowned` cache 返却前に `/healthz` 確認）
- **R7** (v3 追加): PermissionError や SIGKILL 後 alive で state が `owned_terminating` に留まった場合、次回 `ensure_embedder_up` が失敗する → **B2-6 対応**（手動 recovery を強制、安全側）。ERROR log で運用者に通知

---

## 9. Assumption Ledger（v2 から不変、A3/A4 のみ表現修正）

- A1-A2: v2 から不変
- **A3** (表現修正): `/healthz` が通れば embedder が立ってるとみなす。**ただし `/info` で model_name/dimension 検証が必ず続くので stranger port は弾かれる**（B2-1 + B2-4 で二重検証）
- **A4** (v2 で訂正済み): `ensure_embedder_up()` を `create_universe` と `/route` の明示的前段に
- **A5** (v3 追加): `owned_terminating` に留まった embedder は手動 recovery が必要。supervisor 自身は自動では再 spawn しない（安全側）

---

## 10. Decisions（ユーザー review 済み — 2026-07-06、v1 から不変）

- Q1: 事前 `/healthz` チェック → **入れない**（pure lazy）
- Q2: `embedder_spawn_readiness_timeout_seconds` default → **90s**（初回 download で超過し得ることを docs に明記）
- Q3: `supervisor_spawn_embedder` default → **`True`**（本番正常 systemd embedder が先に立ってる場合は不変、開発機は lazy spawn）

---

## 11. 設計判断の更新案（v2 から不変）

[Plans-Multiverse-Scale-Out.md:190](../wiki/Plans-Multiverse-Scale-Out.md) を更新。「supervisor による lazy spawn は開発機向け（`supervisor_spawn_embedder=True`、default 有効）。**正常な** 本番 systemd embedder が先に立ってる場合の挙動は完全不変、所有権は `_embedder_state` で `unowned` / `owned_idle` / `owned_terminating` の 3 状態で管理」。

---

## 12. 次のステップ（PM フロー）

1. ✅ ユーザー review（Q1-Q3）
2. ✅ Codex CLI plan review v1（revise → v2）
3. ✅ Codex CLI plan review v2（revise、方針 approve 寄り → v3）
4. ⏭ **Codex CLI plan review v3 スキップ**（PM second-failure re-diagnosis 適用、implementer に v2 review 結果も引き渡す）
5. ▶ **implementer 委任**（WP-1 → WP-2a → WP-2b → WP-3a → WP-3b → WP-4 → WP-5、直列・test-first）
6. 各 WP verify（git diff / test 実行 / PM 検査）
7. Codex CLI final diff review
8. QA review
9. GaOTTT writeback（設計判断更新 + 実装 lessons + PM self-postmortem）

---

## 参考（GaOTTT memory から、v2 から不変）

- id=60321aca — MV3 supervisor race 解消パターン（`asyncio.Lock + fcntl.flock` 二層）、`waitpid(WNOHANG)`、PermissionError → conflict（B2-6 で踏襲）、env 明示構築
- id=2df4ddb9 — 商用化アーキテクチャ
- id=18aa877f — MV1 実装 lessons（env-override 罠）
- id=f86a4a53 — MV5 backup 実装（default 不変 fence）

---

## 付録: Codex v2 review の blocking 6 件と v3 での対処（implementer 参照用）

| # | Codex v2 blocking | v3 での対処 |
|---|---|---|
| B2-1 | 既存 mock seam が handler 経路で壊れる | §3.2 で `ensure_embedder_up` の先頭で `_validate_embedder` を即座に試す。既存 `_embedder_ok()` だけで通る |
| B2-2 | `fcntl.flock` が event loop を塞ぐ | §3.3 で `asyncio.to_thread(fcntl.flock, ...)` に |
| B2-3 | owned cache が embedder 外部死亡を隠す | §3.2 で `owned_idle` cache 返却前に `_is_my_child_alive(pid)` + `/healthz` 確認 |
| B2-4 | `/route` の `EmbedderValidationError` mapping 未定義 | §3.7 で `/route` handler で catch → 503、`create_universe` は 400 のまま |
| B2-5 | readiness timeout / race lost の契約が曖昧 | §3.5 で `_poll_embedder_readiness` を bool 返しに、`_handle_spawn_readiness_failure` で 4 パターン分類 |
| B2-6 | termination の PermissionError 扱いが既存と矛盾 | §3.6 で state を `unowned` に消さず `owned_terminating` を保持、ERROR log で手動 recovery |

実装時は必ず `/tmp/codex-review-embedder-spawn-v2.md` のフルレビュー結果を読むこと。
