# MV2 Owner Lease — PM 実行計画（改訂版、Codex plan review 反映）

> 起票: 2026-07-02 / 改訂: 2026-07-02（Codex plan review 5 blocking issues 反映）
> リスク分類: **high-risk** — engine.py write path、concurrency/consistency、data safety、新 background task、CLI 変更
> 前提: [multiverse-implementation-plan.md §MV2](multiverse-implementation-plan.md)（Codex レビュー2巡済み）

## 目標

「同じ data_dir を複数プロセスが開いて write-behind が後勝ちする」事故クラスを機構で閉じる。MV3 supervisor の前提。

## スコープ / non-goals

- **やる**: `gaottt/store/lease.py`（OwnerLease）、engine 横断 persist guard（**cache 層に gate を置き全呼出元を網羅**）、read-only 遷移（**mutating method 完全網羅**）、config knob 4 つ、`managed` 強制、`--force-takeover` CLI（proxy 伝播含む）、unit + integration test、docs
- **やらない**: supervisor（MV3）、physics 層、engine API 追加、standalone の default ON 昇格

## Guard API 設計（Codex B1 解決 — 単一 gate で全経路網羅）

**問題**: `engine._persist_blocked` を mutating method entry で check するだけでは、`mcp_server.py:1146` の idle watchdog が `cache.flush_to_store()` を直接呼んで bypass する。test/perf も直接呼ぶ。

**解決**: persist gate を **cache 層** に置く:

```python
# CacheLayer に新 flag
cache.persist_blocked: bool = False   # engine が lease 喪失時に True に set

# flush_to_store entry で check（全呼出元を1箇所で網羅）
async def flush_to_store(self, store):
    if self.persist_blocked:
        return   # read-only: no-op
    ...

# Engine 側
engine._persist_blocked: bool = False   # read-only latch
# lease 喪失時:
self._persist_blocked = True
self.cache.persist_blocked = True
```

**二層防御**:
- **Layer 1 (read-only 契約)**: mutating method entry で `engine._persist_blocked` check → `LeaseLostError` raise。caller に明示エラー
- **Layer 2 (persist 安全網)**: `cache.flush_to_store()` entry で `cache.persist_blocked` check → no-op。FAISS save loops + virtual FAISS save loops で `engine._persist_blocked` 追加 check。idle watchdog / write-behind loop / race window を通じた書き込みを封じる

Layer 1 が正常に効けば Layer 2 には到達しない。Layer 2 は defense-in-depth。

## Mutating surface 完全網羅（Codex B2 解決）

read-only 時に **明示エラーで拒否** する全 mutating method（engine.py 行番号）:

| method | 行 | REST | MCP | 備考 |
|---|---|---|---|---|
| `index_documents` | 650 | /memory | (ingest) | |
| `archive` | 1519 | /forget(soft) | forget | |
| `restore` | 1539 | /restore | restore | |
| `forget` | 1565 | /forget(hard) | forget | |
| `relate` | 1636 | /relations | relate | |
| `unrelate` | 1662 | /relations DELETE | unrelate | |
| `revalidate` | 1682 | /revalidate | revalidate | |
| `merge` | 1746 | /merge | merge | |
| `compact` | 1807 | /compact | compact | rebuild_faiss=True は derived state も書く |
| `reset_orbital_state` | 2051 | — | — | |
| `reset_velocities` | 2085 | — | — | |
| `reset_masses` | 2113 | /admin/reset_masses | — | |
| `warm_displacement` | 2138 | /admin/warm_displacement | — | |
| `reset` | 2205 | /reset | — | |

**特殊扱い**:
- `query` (895): 訓練 step が mass/displacement を書く。read-only 時は `passive=True` フォールバック（結果は返すが field は更新しない）
- `_orbital_tick` (533): dream loop 内。read-only 時は skip（mass/displacement を書かない）
- `prefetch` (1591): recall を schedule するが、schedule 先の recall は上記 query フォールバックに従う

## Work package 分解（直列）

### WP-1: lease unit tests（test-first, RED）
- 新規 `tests/unit/test_owner_lease.py`
- ケース: 新規取得（O_EXCL 成功）/ 既存ありで `LeaseHeldError` / stale takeover（heartbeat_at 過去）/ force takeover / read-back で owner_id 不一致検出 → `is_active()` False / **multiprocessing で N=5 プロセス同時 acquire → 成功 1 件**（O_EXCL + flock の regression fence、複数回実行）/ release が owner_id 不一致で他者 lock を消さない / guard ファイルの flock 臨界区間検証

### WP-2: lease.py + config knobs（→ WP-1 GREEN）
- 新規 `gaottt/store/lease.py`:
  - `LeaseHeldError(Exception)`
  - `OwnerLease(data_dir, config)`:
    - `acquire(force=False)`: 新規は `os.open(O_CREAT|O_EXCL)`。既存ありは `<data_dir>/owner.lock.guard` を `fcntl.flock(LOCK_EX)` した臨界区間内で read→judge(stale/force)→replace。stale = `heartbeat_at` が `lease_stale_seconds` 超
    - `heartbeat_loop()`: `lease_heartbeat_seconds` 周期。更新前に read-back、owner_id 不一致なら `is_active=False` + ERROR log（persist block の trigger は engine 側）
    - `release()`: read-back して owner_id 一致時のみ削除
    - `owner_id` property（uuid4().hex）
    - `is_active` property
  - `<data_dir>/owner.lock` JSON: `{owner_id, pid, hostname, started_at, heartbeat_at, takeover_count}`
- `gaottt/config.py` knob 4 つ（default 不変）:
  - `owner_lease_enabled: bool = False`
  - `lease_force_takeover: bool = False`
  - `lease_heartbeat_seconds: float = 10.0`
  - `lease_stale_seconds: float = 60.0`

### WP-3: engine integration tests（test-first, RED）
- 新規 `tests/integration/test_engine_lease.py`。guard API 契約に対して test を書く:

**4-path persist block 独立テスト（Codex B3 解決）**:
- (a) `_persist_blocked=True` 後に write-behind loop が flush しない（dirty cache を置いて heartbeat 周期待ち → SQLite 変更なし）
- (b) final flush（shutdown）が skip される
- (c) FAISS save loop が skip される（`_faiss_dirty=True` で周期待ち → file 変更なし）
- (d) virtual FAISS save loop が skip される

**read-only 遷移テスト（Codex B2 解決）**:
- `_persist_blocked=True` 後に mutating method 14 個すべてが `LeaseLostError`（または RuntimeError）を raise
- `query` が passive フォールバックで結果を返す（field 更新なし）
- `_orbital_tick` が skip される
- `recall(passive=True)` / `get_node` / `reflect` は成功

**lease-loss race テスト（Codex B4 解決）**:
- owner.lock の owner_id を外部書き換え → heartbeat read-back → engine が read-only 遷移
- dirty cache 状態で lease 喪失 → flush が no-op
- lease 喪失中の shutdown → final flush skip + FAISS save skip + release が B の lock を消さない

**lifecycle テスト**:
- 同一 tmp data_dir で engine A 起動中に engine B startup → `LeaseHeldError`
- A shutdown 後 → B 取得成功
- stale シミュレーション（heartbeat_at 過去書き換え）→ takeover 成功

**managed 強制 + default-OFF assertion（Codex missing test 解決）**:
- `manifest.managed=True` + `owner_lease_enabled=False` → lease 強制（owner.lock 作成）
- `owner_lease_enabled=False` + `managed=False` → **owner.lock / owner.lock.guard 未作成 / heartbeat task 未起動**（default 不変の証明）

**proxy CLI 伝播テスト（Codex B5 解決）**:
- `--force-takeover` flag が config.lease_force_takeover を立てる
- proxy mode で spawn される backend に env 経由で伝播することを確認（spawn env に `GAOTTT_LEASE_FORCE_TAKEOVER` が含まれる）

### WP-4: engine impl（→ WP-3 GREEN）
単一 delegation、内部 sequence 明示:

1. **guard API**: `cache.persist_blocked` flag + `flush_to_store` entry gate + `engine._persist_blocked` + FAISS save loop / virtual FAISS save loop / shutdown final save の各 check 追加
2. **read-only surface**: 14 mutator の entry check → `LeaseLostError` raise。`query` passive フォールバック。`_orbital_tick` skip
3. **lifecycle**: startup で `owner_lease_enabled OR manifest.managed` なら acquire（LeaseHeldError 素通し raise）、heartbeat loop 起動（stop 順: dream → lease heartbeat → faiss save）、shutdown で final flush **後** に release。両方 false なら lease 全体 skip（owner.lock 未作成 = default 不変）
4. **managed 強制**: startup の発動条件 `owner_lease_enabled OR manifest.managed`
5. **CLI**: `mcp_server --force-takeover`（config の糖衣）+ proxy spawn env 伝播

### WP-5: docs + handoff
- `docs/wiki/Architecture-Concurrency.md`「構造的解 (2): owner lease」
- `docs/wiki/Operations-Troubleshooting.md`「LeaseHeldError が出る」
- `docs/wiki/Operations-Tuning.md` knob 4 つ
- `multiverse-implementation-plan.md` MV2 完了マーク
- handover note

## Gate plan（high-risk）

| Gate | 実施 | 備考 |
|---|---|---|
| Codex plan review | ✅ 済（本改訂で 5 blocking 解決） | |
| Test-first (WP-1, WP-3) | ✅ | |
| Codex test-diff review | ✅ WP-1後 + WP-3後 | 4-path / race / mutating 網羅 が焦点 |
| QA test-diff review | ✅ WP-3後 | read-only error は user-facing |
| Codex final diff review | ✅ WP-4後 | |
| QA final review | ✅ WP-4後 | |
| GaOTTT writeback | ✅ | |

## Assumption ledger（Codex suspicious assumptions 反映）

| # | assumption | basis | falsification condition | blast radius |
|---|---|---|---|---|
| A1 | `O_CREAT\|O_EXCL` で新規取得は race-free | POSIX 保証 | 同時作成で両方成功（不可能） | 二重取得 = 根幹崩壊 |
| A2 | flock(LOCK_EX) 臨界区間が stale/force の TOCTOU を閉じる | 標準パターン | stale/force code path が guard lock 保持なしで owner.lock を書く / 2 contender が同時 takeover で両方成功 | 同時二重取得 |
| A3 | owner_id (uuid4) が PID 再利用に耐える | uuid 衝突 ≈ 0 | read-back が pid 比較になっている | 奪われた lease を自分と誤認 |
| A4 | default OFF + managed=false で既存テスト全緑 | ensure_manifest は managed=false 生成 | 既存テストが managed=true を設定 / heartbeat task が leak / config 解析が変化 | 既存 suite 大量 RED |
| A5 | 両 flag false で startup は lease 全体を skip（owner.lock/guard 未作成・heartbeat 未起動） | default 不変要件 | 既存テストの tmp data_dir に owner.lock ができる | file 衝突 / cleanup 問題 |
| A6 | cache.persist_blocked gate が flush_to_store の全呼出元を網羅する | flush_to_store は cache 層の単一メソッド、全呼出元がこれを経由する | flush_to_store を bypass する別の SQLite 書き込み経路がある | silent write |
| A7 | mutating method 14 個の entry check で read-only 契約が成立 | engine.py grep で完全網羅（行番号確認済み） | mutator の追加実装忘れ / 新規 mutator 追加時に check 忘れ | read-only 中の state 変更 |
| A8 | recall 訓練 step の passive フォールバックが完全 | passive=True 経路が既存 | query 内の mass/displacement を書く全分岐が fallback に漏れがある | read-only 中の field 更新 |
| A9 | **data_dir は POSIX semantics を持つ filesystem（O_EXCL/flock/os.replace が期待通り動く）** | 標準的な local filesystem 想定 | network FS（NFS/CIFS）で semantics が異なる | lease 機構全体が信頼できない — v1 は local FS のみ support と docs に明記 |

## ロールバック

- standalone: `owner_lease_enabled=False`（default）
- managed 宇宙: manifest `managed` を false に書き換え（runbook 専用）

## 所要見込み

3-4 日（implementation-plan 見積もり + Codex 反映で WP-4 が膨張）。本セッションで WP-1〜WP-4 の完了を目指す。
