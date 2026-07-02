# Multiverse Scale-Out — Implementation Plan（実装者向け詳細作業計画）

> **目的**: [Plans — Multiverse Scale-Out](../wiki/Plans-Multiverse-Scale-Out.md)（戦略計画、SoT）を実装可能な作業単位に分解する。本ドキュメントは **作業手順書** であり、設計判断の根拠・スコープの議論は wiki 側を参照。
> 起票: 2026-07-02
> 状態: 🟡 **未着手**（戦略計画・本書とも同日 Codex CLI レビュー済み。本書はレビュー 2 巡: 初回 13 件 + 再レビュー 8 件をすべて反映。確定した重要仕様: engine 横断 persist block + read-only 遷移 / managed manifest による lease 強制 + permission 要件 / lease 原子性（`O_EXCL` + flock 臨界区間 + owner_id nonce）/ backend token 認証 + token 永続化（`backend.token` 0600）/ service の DI seam）
> 想定担当: 実装は複数セッションに分割可。**MV0 → MV1 → MV2 は直列**（後続の土台）、MV4/MV5 は MV3 完了後に並行可
> 前例: [rest-mcp-unification-plan.md](rest-mcp-unification-plan.md)（Phase S）と同じ運用 — stage ごとに完了マークを本ファイルに追記する

**完了マーク**:
- ✅ MV0: EmbedderProtocol + 宇宙 manifest（土台、挙動変更ゼロ）— 2026-07-02 完了
- ✅ MV1: embedding service + RemoteEmbedder — 2026-07-02 完了（Tier 6 数値等価は次セッションで real RURI 実施）
- ✅ MV2: owner lease（1 宇宙 1 書き込みオーナーの機構化）— 2026-07-02 完了（default OFF、managed 強制、Codex review 3 巡: 2 blocking → 構造的解消、54 test: 21 unit + 33 integration 全緑）
- ⬜ MV3: universe supervisor + multiverse layout
- ⬜ MV4: control plane（Postgres）
- ⬜ MV5: backup / DR（Litestream + runbook + DR drill）
- ⬜ MV6: 英語宇宙（embedder per universe）

---

## 0. 実装横断ルール

すべての stage に適用:

1. **physics / observation 層に触らない** — `core/gravity.py` / `core/scorer.py` / mass・displacement・velocity の更新則は 1 行も変更しない。触った時点で本計画のスコープ違反
2. **default 不変** — 新 config knob はすべて「未設定 = 現行挙動」。既存の claude.json / opencode.json / .codex 構成は無変更で動き続けること。各 stage の acceptance に「既存 suite 全緑 + `rest_smoke.py` / `mcp_smoke.py` green」を含める
3. **uv 使用、pip 禁止**。新依存は `pyproject.toml` の optional extra に置けるものは置く（例: `[multiverse]` extra に httpx/litestream 補助等。※ httpx は既に test 依存にあるので本体依存への昇格のみ）
4. **MCP/REST parity 鉄則との関係** — 本計画は **engine の能力を追加しない**（MCP 新ツール 0、REST 新エンドポイント 0 が原則）。supervisor / control plane / embedding service の API は管理面であり parity 対象外（`/reset` と同じ例外クラス）。もし実装中に engine 側 API が必要になったら、その時点で MCP+REST 同時追加の通常ルールに戻る
5. **config knob の命名** — `GaOTTTConfig` にフィールドを足せば generic env-override loop が `GAOTTT_<UPPER_NAME>` を拾う（`gaottt/config.py` の既存機構）。knob 追加時は [Operations — Tuning](../wiki/Operations-Tuning.md) に行を足す
6. **各 stage の締め** — `pytest tests/ -q` 全緑 → `ruff check gaottt/ tests/`（pre-existing 4 件のみ）→ 両 smoke → 該当 Tier の `tests/perf/` 手動実行 → ドキュメント更新 → 本ファイルの完了マーク更新

ポート割当（本計画で予約する localhost ポート）:

| ポート | 用途 |
|---|---|
| 7878 | 既存: 単一 backend（proxy mode default、変更しない） |
| 7879 | embedding service |
| 7880 | universe supervisor |
| 7890–7989 | 宇宙 backend の動的割当レンジ |

---

## MV0 — EmbedderProtocol + 宇宙 manifest（土台）

**目的**: 挙動変更ゼロのまま、後続 stage が依存する 2 つの縫い目を作る。単独 PR として出せる最小単位。

### MV0-1: EmbedderProtocol

新規 `gaottt/embedding/base.py`:

```python
from typing import Protocol, runtime_checkable
import numpy as np

@runtime_checkable
class EmbedderProtocol(Protocol):
    @property
    def dimension(self) -> int: ...
    def encode_documents(self, texts: list[str]) -> np.ndarray: ...
    def encode_query(self, text: str) -> np.ndarray: ...
    # encode_queries は optional のまま（engine.py が getattr fallback 済み）
```

契約（`RuriEmbedder` の現行実装を正とする — `gaottt/embedding/ruri.py`）:
- 返り値は **`np.float32`、L2 正規化済み**、`encode_documents`/`encode_queries` は `(N, dim)`、`encode_query` は `(1, dim)`
- **RURI prefix（`検索クエリ: ` / `検索文書: `）の適用は embedder 実装の内側の責務** — 呼び出し側は生テキストを渡す（現行と同じ）

変更:
- `gaottt/core/engine.py:78` の型ヒント `embedder: RuriEmbedder` → `embedder: EmbedderProtocol`（import 変更のみ、実行時挙動ゼロ）
- `services/runtime.py` の型は推論に任せる（変更不要）

### MV0-2: 宇宙 manifest

新規 `gaottt/store/manifest.py`:

```python
class UniverseManifest(BaseModel):
    schema_version: int = 1
    universe_id: str            # uuid4().hex[:12]、単一ユーザー構成では "default"
    embedder_id: str            # 例 "cl-nagoya/ruri-v3-310m"
    embedder_version: str       # model revision。取得不能なら "unpinned"
    embedding_dim: int          # 768
    created_at: float

def load_manifest(data_dir: Path) -> UniverseManifest | None: ...
def write_manifest(data_dir: Path, m: UniverseManifest) -> None:  # tmp + os.replace の atomic write
def ensure_manifest(data_dir: Path, config: GaOTTTConfig) -> UniverseManifest:
    # 無ければ現 config から生成して書く（既存 DB の後方互換パス）
```

ファイルは `<data_dir>/manifest.json`。`managed: bool = False` フィールドも持つ（MV3 の supervisor が宇宙作成時に `true` で書く — MV2 の lease 強制で使用）。

engine への組み込み — **配置に注意（Codex レビュー反映）**:
- **起動拒否は startup diagnostics ブロックに入れない**。`engine.startup()` は `run_startup_checks()` の例外を warning に落として継続する実装（`engine.py:196-203` の `except Exception → logger.warning`）なので、そこに置くと握りつぶされる
- 正しい配置: `engine.startup()` の **冒頭、`store.initialize()` / `load_from_store` / FAISS load より前** に hard gate として直接書く:
  1. `ensure_manifest()` — 無ければ生成（**既存 DB はここで自動的に manifest を得る**）
  2. 検証: `manifest.embedding_dim != config.embedding_dim` → **RuntimeError を素通しで raise**（呼び出し元の MCP/REST 起動が落ちる = 意図どおり）。embedder identity 照合は **factory（`build_engine`）側の責務**（下記）。エラーメッセージに「embedder を替えるなら `scripts/rebuild_faiss_from_db.py` で再 embed + manifest 更新」の導線を書く
  3. 拒否は `manifest_check_enabled: bool = True` で無効化可能（escape hatch、Tuning 表に記載）

**embedder identity 照合は factory に寄せる**（Codex レビュー反映 — engine は embedder を duck-typing で受けるため、engine 側では「何の embedder か」を知る手段が保証されない）:
- `EmbedderProtocol` に optional property `embedder_id: str` / `embedder_version: str` を追加（`RuriEmbedder` には `model_name` を `embedder_id` として、HF snapshot の commit hash を `embedder_version` として実装 — 取得不能時 `"unpinned"`。MV1 の `/info` はこれを返す）
- `build_engine` が「自分が構築した embedder の identity」を manifest と照合する。テストの direct engine 構築（StubEmbedder を engine コンストラクタに直渡し）は factory を通らないので **既存 fixture は無変更で通る**

`embedder_version` の v1 定義: `embedder_id` 一致 + `embedding_dim` 一致を **hard check**、`embedder_version` は記録 + warning 比較（HF revision が動く運用を v1 では止めない。DR の artifact pinning は MV5 で runbook 要件化）。

### テスト / acceptance

- 新規 `tests/unit/test_manifest.py` — roundtrip / atomic write / ensure が既存 config から生成 / dim mismatch で startup 拒否 / `manifest_check_enabled=False` で通過
- 既存 suite 全緑（**挙動変更ゼロの確認が本 stage の acceptance そのもの**）。`tests/integration/` の fixture は tmp data_dir を使うので manifest が自動生成される — 生成コストが perf に乗らないこと（Tier 1 smoke で確認）

**所要**: 1〜2 日
**rollback**: `manifest_check_enabled=False`。Protocol 化は型のみで rollback 不要

---

## MV1 — embedding service + RemoteEmbedder

**目的**: モデルロードをホストに 1 つへ。GPU コストをユーザー数から切り離す。

### MV1-1: wire protocol（先に確定させる）

- **リクエスト**: JSON `{"kind": "query" | "document", "texts": ["...", ...]}` を `POST /encode`
- **レスポンス**: `application/x-msgpack` — `{"shape": [N, dim], "dtype": "float32", "data": <bytes>}`（`np.ndarray.tobytes()`、little-endian）。msgpack は既存依存
- **不変条件**: 返るベクトルは L2 正規化済み float32。**prefix 適用は service 側**（client は生テキストのみ送る — prefix 実装の二重管理を防ぐ、wiki 計画 §4 Stage 1 のレビュー反映点）
- `GET /info` → JSON `{"model_name": ..., "dimension": ..., "version": ..., "batch_size": ...}`。`version` は **HF snapshot の commit hash**（`RuriEmbedder` に `embedder_version` property として実装、`huggingface_hub.scan_cache_dir()` の revision から取得。取得不能時 `"unpinned"`）— MV5 の DR 要件「同一 artifact での復元」の照合キーになるので、ここで固めておく（Codex レビュー反映）
- **エラー系**: queue 満杯 → 503 + `Retry-After`、texts 空 → 400、encode 中の CUDA OOM → 500（メッセージに batch_size 縮小の導線）
- **入力上限（共有 SPOF の防御、Codex レビュー反映）**: `len(texts) ≤ 256` / 合計文字数 ≤ 200,000 / body ≤ 10MB。超過は **413**。`--max-queue 32` の定義 = **waiting リクエスト数**（in-flight は semaphore(1) で常に 1、待ち 32 で頭打ち → 33 本目が 503）
- タイムアウト: client 側 `embedder_request_timeout_seconds`（default 30.0）。server 側は uvicorn default

### MV1-2: `gaottt/embedding/service.py`

- **DI seam を最初から切る**（Codex レビュー反映 — これがないと決定論テストが組めない）: `create_app(embedder: EmbedderProtocol) -> FastAPI` を公開 API とし、`__main__` エントリ（`python -m gaottt.embedding.service --host 127.0.0.1 --port 7879 --model cl-nagoya/ruri-v3-310m`）だけが `RuriEmbedder` を構築して渡す。テストは `create_app(StubEmbedder())` で real RURI を回避
- **必ず localhost（or unix socket）に bind** — 認証を持たない設計の前提（wiki 計画 §4 Stage 2 の認証境界）。`--host` に外部アドレスを渡されたら起動時に WARNING
- モデルは lifespan で 1 回ロード（既存 `RuriEmbedder` をそのまま内包する — encode ロジックの二重実装をしない）
- **GPU 直列化**: `model.encode` を `asyncio.to_thread` + `asyncio.Semaphore(1)` で包む。**v1 はリクエスト単位 encode**（1 リクエストに N texts が乗るので現行 batch 効率は維持される）。複数 requester を跨ぐ micro-batching は **MV1.5 として別 PR**（`service_batch_window_ms` 案、計測してから — 「速い」と「正しい」のトレードオフ罠に注意）
- queue 上限: `--max-queue 32`（超過 503）
- deploy 雛形: `deploy/gaottt-embedder.service`（systemd unit、`Restart=always`）を新規、[Operations — Server Setup](../wiki/Operations-Server-Setup.md) に節を追加

### MV1-3: `gaottt/embedding/remote.py`

```python
class RemoteEmbedder:
    def __init__(self, endpoint: str, timeout: float = 30.0):
        # httpx.Client (sync)。__init__ で GET /info → self._info にキャッシュ
        # 接続不能なら ConnectionError を即 raise（起動時に倒す、リクエスト中に初発見させない）
    @property
    def dimension(self) -> int: ...          # /info の値
    @property
    def embedder_id(self) -> str: ...        # /info の model_name — manifest 照合用（Protocol と命名統一）
    @property
    def embedder_version(self) -> str: ...   # /info の version（HF snapshot commit hash）
    def encode_documents(self, texts): ...   # POST /encode kind=document → np.frombuffer + reshape
    def encode_queries(self, texts): ...
    def encode_query(self, text): return self.encode_queries([text])
```

- **同期 client で良い**（現行 `encode_query` も GPU 同期呼びで loop をブロックしており等価 — wiki 計画 §4 Stage 1）。`to_thread` wrap は optional 改善として別 PR
- retry: 接続エラーのみ 1 回（backoff 0.5s）。encode エラー（4xx/5xx）は retry しない — その 1 ターン失敗で留める（OOM 時の現行挙動と同じ）

### MV1-4: wiring + 整合ガード

`gaottt/config.py` に追加:

| knob | default | 意味 |
|---|---|---|
| `embedder_endpoint: str \| None` | `None` | 設定時 RemoteEmbedder（`GAOTTT_EMBEDDER_ENDPOINT`） |
| `embedder_request_timeout_seconds: float` | `30.0` | remote encode timeout |

`services/runtime.py:build_engine` の factory 分岐:

```python
if config.embedder_endpoint:
    embedder = RemoteEmbedder(config.embedder_endpoint, timeout=config.embedder_request_timeout_seconds)
    if embedder.dimension != config.embedding_dim:
        raise RuntimeError(...)   # FAISS は config.embedding_dim で先に構築されるため、ここで倒す
else:
    embedder = RuriEmbedder(model_name=config.model_name, batch_size=config.batch_size)
```

manifest 照合（MV0）はそのまま効く: factory は in-process / remote を問わず **`embedder.embedder_id` / `embedder.embedder_version`**（Protocol の統一 property — 再レビュー反映で `model_name` という別名は作らない）を manifest と照合する。

### テスト / acceptance

- 新規 `tests/unit/test_remote_embedder.py` — **transport 方針に注意（Codex レビュー反映）**: `httpx.ASGITransport` は AsyncClient 専用なので sync の `RemoteEmbedder` には使えない。① unit は **`httpx.MockTransport`**（sync client 対応）で wire protocol の encode/decode / shape / dtype / 正規化 / 503 / timeout / /info mismatch 拒否を検証、② `RemoteEmbedder.__init__` に `client: httpx.Client | None = None` の DI を切っておく（MockTransport 注入用）
- 新規 `tests/integration/test_engine_remote_embedder.py` — `create_app(StubEmbedder())` を **uvicorn background thread** で実起動し、`embedder_endpoint` 設定の engine で remember → recall round-trip（`test_engine_archive_ttl.py` の fixture パターン + 実 HTTP）
- 新規 `tests/perf/test_tier6_remote_embedder.py`（手動 Tier、real RURI）— **数値等価 3 段**: ① `np.allclose(in_process, remote, atol=1e-5)` ② cosine 差 < 1e-6 ③ golden queries（`tests/perf/golden_corpus/queries.json`）で `engine.query` top-K 一致。bit-exact は要求しない（service 側バッチングで batch shape が変わり得る — Codex レビュー反映）
- `scripts/perf_baseline.py --label pre-mv1` / `--label post-mv1` → `perf_diff.py` で latency 差を記録（remote は +1-3ms 想定、>25% 退行で exit 1 に引っかかったら設計見直し）
- 既存 suite 全緑 + 両 smoke（endpoint 未設定 = 従来経路の確認）

**所要**: 4〜5 日
**rollback**: `embedder_endpoint` 未設定で完全従来経路

### ドキュメント

- [Operations — Server Setup](../wiki/Operations-Server-Setup.md) に「embedding service を分離する」節
- [Operations — Tuning](../wiki/Operations-Tuning.md) に knob 2 つ
- [Operations — Resource Requirements](../wiki/Operations-Resource-Requirements.md) に「モデル抜き engine」実測を追記（この stage で初めて実測できる — wiki 計画 §5 の表を差し替える入力になる）

---

## MV2 — owner lease（1 宇宙 1 書き込みオーナーの機構化）

**目的**: 「同じ data_dir を複数プロセスが開いて write-behind が後勝ちする」事故クラス（bidirectional cache overwrite / FAISS reverse-overwrite）を機構で閉じる。supervisor（MV3）の前提。

### 実装

新規 `gaottt/store/lease.py`:

```python
class OwnerLease:
    # <data_dir>/owner.lock — JSON {owner_id, pid, hostname, started_at, heartbeat_at, takeover_count}
    # owner_id = uuid4().hex — PID 再利用・hostname 重複に依存しない所有者識別（再レビュー反映）。
    # read-back 判定・release はすべて owner_id 一致で行う（pid は表示用の補助情報）
    def acquire(self, force: bool = False) -> None:
        # ★ 原子性（再レビュー Critical 反映）: tmp + os.replace は「後勝ち上書き」であって
        #   check-and-set ではない — 同時起動の 2 プロセスが両方「ロックなし」と判断して取得できてしまう。
        #   仕様: 新規取得は os.open(path, O_CREAT | O_EXCL) — 存在すれば必ず失敗する原子的作成。
        #   既存 lock がある場合の stale/force 判定 → 上書きは、<data_dir>/owner.lock.guard を
        #   flock(LOCK_EX) で握った臨界区間内で read → 判定 → replace を行う（判定と書き込みの間の race を封鎖）
        # 既存 lock があり heartbeat_at が lease_stale_seconds 以内 → LeaseHeldError
        # stale (heartbeat 停止 > lease_stale_seconds) → 臨界区間内で上書き取得 + WARNING log
        # force=True → 同上（--force-takeover 用、既存 heartbeat が生きていれば二重確認 log）
    async def heartbeat_loop(self) -> None:
        # lease_heartbeat_seconds 周期で heartbeat_at を更新。
        # 更新前に read-back し、owner_id が自分でなければ「lease を奪われた」と判断:
        #   → engine 横断の永続化 block + read-only 遷移（下記）+ ERROR log
    def release(self) -> None:
        # read-back して owner_id 一致時のみ削除（他者の新 lease を誤削除しない）
```

**persist block は engine 横断の新ラッチにする**（Codex レビュー反映 — 重要）: 既存の `_faiss_persist_blocked`（`engine.py:124`）は **FAISS save 専用** で、`CacheLayer.flush_to_store()` の SQLite write-behind / shutdown 時の final flush は止まらない。lease 喪失時に FAISS だけ止めても SQLite への逆方向上書きは続くため「1 宇宙 1 書き込みオーナー」が閉じない。仕様:
- engine に `_persist_blocked: bool` を新設し、**cache write-behind loop / final flush / FAISS save / virtual FAISS save の 4 経路すべて** の入口で check
- `_faiss_persist_blocked`（Tier B 診断由来）は既存のまま残す — 意味が違う（index 破損疑い vs 所有権喪失）ので統合しない。FAISS save は「どちらかが立っていたら skip」
- block 時のログは 1 回だけ ERROR、以降は debug（`_faiss_persist_guard_warned` と同パターン）

**所有権喪失後は read-only 遷移する**（再レビュー反映 — persist block 単独だと `remember` が「成功応答を返すのに保存されない」silent data loss になる）: `_persist_blocked` が立ったら engine は **read-only 状態** に遷移し、mutating operation（`index_documents` / `forget` / `restore` / `merge` / `relate` / mass・displacement を書く recall の訓練 step）を **明示的にエラーで拒否** する。read 系（`recall(passive=True)` / `get_node` / `reflect`）は許可。MCP/REST の応答には「lease を失った — この宇宙の書き込みオーナーは別プロセスに移った」と原因を明示（caller が再接続 → 新オーナー経由で復旧できる導線）。recall の訓練 step は既存の `passive=True` 経路に内部フォールバックさせる実装が最小。

config knob:

| knob | default | 意味 |
|---|---|---|
| `owner_lease_enabled: bool` | **`False`** | default 不変（standalone DB 向け）。**managed 宇宙には効かない — 下記** |
| `lease_force_takeover: bool` | `False` | takeover の canonical 経路（`GAOTTT_LEASE_FORCE_TAKEOVER`）。CLI `--force-takeover` はこれを立てるだけの糖衣 — REST app / supervisor spawn / どの entry からも env で渡せる |
| `lease_heartbeat_seconds: float` | `10.0` | |
| `lease_stale_seconds: float` | `60.0` | これを超えて heartbeat 停止なら takeover 可 |

**managed 宇宙では default OFF の抜け道を閉じる**（Codex レビュー反映 — 重要）: knob default OFF のままだと、supervisor 管理下の宇宙 data_dir を legacy stdio / REST app が「OFF のまま」直接開けてしまい、戦略計画の「直接起動も lease で拒否」が破れる。仕様: **lease check の発動条件は `owner_lease_enabled OR manifest.managed`**。supervisor が作る宇宙は manifest に `managed: true`（MV0 で導入済みのフィールド）を持つため、**どの entry point から開いても config に関係なく lease が強制される**。standalone の既存 DB は `managed: false`（`ensure_manifest` の default）なので挙動不変 — default 不変と抜け道封鎖が両立する。

engine 統合:
- `engine.startup()` 冒頭（`load_from_store` より前、MV0 の manifest hard gate と同じ位置）で `acquire()`。`LeaseHeldError` は **素通しで raise**（診断ブロックは例外を warning に落とすため使わない — MV0 と同じ注意）。メッセージに保持者の pid/host/heartbeat 経過と「本当に死んでいるなら `GAOTTT_LEASE_FORCE_TAKEOVER=true`」の導線
- heartbeat loop は既存 background task 群（dream / faiss save / virtual faiss save）と同列に起動・shutdown 順に組み込む（stop 順: dream → lease heartbeat → faiss save → …）
- `release()` は shutdown の **final flush 完了後** に呼ぶ（flush 前に手放すと最後の書き込みが無所有で走る）
- CLI: `mcp_server` に `--force-takeover` flag（config の `lease_force_takeover` を立てるだけ）

**default OFF の理由と昇格計画**: 既存 standalone 構成の挙動を変えないため OFF で導入（managed 宇宙は上記のとおり manifest で強制）。この機構は standalone でも事故防止に有効（reverse-overwrite incident の再発防止と同型）なので、**1-2 週の dogfooding 後に code default ON への昇格を判断**する（Phase Q governor と同じ promotion パターン）。

### テスト / acceptance

- 新規 `tests/unit/test_owner_lease.py` — acquire / conflict / stale takeover / force / read-back で他者検出 → persist block。**block 後に cache flush と FAISS save の両方が停止する**ことを assert（FAISS だけ止まる regression を防ぐ）。`manifest.managed=true` + `owner_lease_enabled=False` で lease が強制されることも。**並行 acquire race**: N プロセス（`multiprocessing`）同時 acquire → 取得成功は常に 1（`O_EXCL` + flock 臨界区間の検証 — 再レビュー Critical の regression fence）。release が owner_id 不一致で他者 lease を消さないことも
- read-only 遷移の検証 — `_persist_blocked` 後: `remember`/`forget`/`relate` が明示エラー、`recall(passive=True)`/`get_node`/`reflect` は成功、通常 `recall` は passive フォールバックで応答（silent data loss の否定が acceptance の本体）
- 新規 `tests/integration/test_engine_lease.py` — 同一 tmp data_dir で engine A 起動中に engine B startup → 拒否。A shutdown 後 → B 取得成功。stale シミュレーション（heartbeat_at を過去に書き換え）→ takeover
- 既存 suite 全緑（default OFF なので影響ゼロのはず — それ自体が acceptance）

**所要**: 3〜4 日（persist block の engine 横断化 + managed 強制を含む）
**rollback**: standalone は `owner_lease_enabled=False`（default のまま）。managed 宇宙は manifest の `managed` を false に書き換える（供覧: 事故防御を外す操作なので runbook でのみ案内）

### ドキュメント

- [Architecture — Concurrency](../wiki/Architecture-Concurrency.md) の「逆方向上書きの罠」節に「構造的解 (2): owner lease」を追記
- [Operations — Troubleshooting](../wiki/Operations-Troubleshooting.md) に「LeaseHeldError が出る」項

---

## MV3 — universe supervisor + multiverse layout

**目的**: ユーザー→宇宙のルーティングと宇宙 engine のライフサイクル管理。**新規の制御面として見積もる**（既存 proxy の流用は概念レベル — Codex レビュー反映）。

### MV3-1: multiverse layout + local registry

```
<multiverse_root>/                     # GAOTTT_MULTIVERSE_ROOT（default: ~/.local/share/gaottt-multiverse）
├── universes/
│   └── <universe_id>/                 # = その宇宙の GAOTTT_DATA_DIR
│       ├── gaottt.db / *.faiss / *.faiss.ids
│       ├── manifest.json              # MV0
│       └── owner.lock                 # MV2
├── registry.db                        # supervisor local SQLite: universes / api_keys(hashed) / port assignments
└── logs/
```

新規 `gaottt/multiverse/registry.py` — local SQLite（aiosqlite、既存依存）:
- `universes(universe_id PK, owner_label, port, status, created_at)` — port は 7890–7989 から割当、解放は宇宙削除時のみ（respawn で再利用、レンジ枯渇 = 100 宇宙/ホスト上限は v1 制約として明記）
- `api_keys(key_hash PK, universe_id, created_at, revoked_at)` — 平文キーは発行時に一度だけ返す（SHA-256 で保存）
- startup 時に `universes/` を scan して registry と突き合わせ（ディレクトリが正、registry は index — wiki 計画 §4 Stage 3 の source-of-truth の向きと同じ）
- **permission 要件と信頼境界**（再レビュー反映 — `manifest.managed` を lease 強制の信頼根にする以上、書き換え耐性を明示する）: supervisor は **専用 OS ユーザー** で動かし、`<multiverse_root>` は **0700**、manifest / owner.lock / backend.token は supervisor ユーザー所有・0600。テナントの他 OS ユーザー/サービスからは読み書き不可。**同一 OS ユーザー内のプロセスによる manifest 改変は v1 の信頼境界外**（root/同一ユーザーを敵とするモデルは v1 で守らない — deploy doc に明記）。`managed=false` への書き換え（lease 解除）は runbook の復旧手順としてのみ案内し、supervisor 停止 + 対象宇宙 backend 停止を前提条件にする

### MV3-2: `gaottt/multiverse/supervisor.py`

FastAPI、port 7880、localhost bind:

| endpoint | auth | 動作 |
|---|---|---|
| `POST /admin/universes {owner_label, embedder_id?}` | admin key | 宇宙作成: **embedder_id を検証してから** dir + manifest + port 割当 + API キー発行（平文は応答で一度だけ）。検証 = supervisor config に登録された embedding service の `/info` に照会し、`model_name` / `dimension` が取れなければ **400**（存在しない embedder の宇宙が「初回 spawn まで壊れたまま潜伏する」のを防ぐ — 再レビュー反映） |
| `DELETE /admin/universes/{id}` | admin key | backend 停止 → dir を `trash/` へ move（即時物理削除しない、猶予後削除） |
| `GET /admin/universes` | admin key | 一覧 + 稼働状態 |
| `POST /route {api_key}` | 宇宙 key | **本丸**: key → universe_id 解決 → backend ensure（下記）→ `{url: "http://127.0.0.1:<port>/mcp", token: "<backend_token>"}` を返す |

**backend 自体にも per-universe token を持たせる**（Codex レビュー反映 — 重要）: `/route` の key 検証だけでは、localhost 上の別 OS ユーザー/プロセスが port を直叩きして認証を迂回できる。仕様:
- supervisor が spawn 時に乱数 token を生成し `GAOTTT_BACKEND_TOKEN` env で渡す
- `mcp_server` の streamable-http 経路に **ASGI middleware を追加**（idle watcher が既に同型の middleware 注入をしている — `mcp_server.py:1156` の `dispatch/call_next` パターンを流用）: `GAOTTT_BACKEND_TOKEN` 設定時のみ `Authorization: Bearer <token>` を検証、未設定なら従来どおり素通し（**default 不変** — 既存 7878 単一 backend は無影響）
- shim は `/route` 応答の token を接続ヘッダに付ける
- **token のライフサイクル**（再レビュー反映 — メモリのみだと supervisor 再起動で既存 backend に route できなくなる）: supervisor が spawn 時に `<universe_dir>/backend.token`（**0600**）へ書き、env でも backend に渡す。supervisor 再起動時はこのファイルを読み戻して既存 backend への route を継続。backend 再 spawn 時に token も再生成（ファイル上書き）。宇宙削除でディレクトリごと消える
- **probe / readiness poll / ping も token 対応**（再レビュー反映）: `_probe_backend` 流用箇所は token 有効時に `Authorization: Bearer` を付けて initialize する。middleware は「token 未設定 = 素通し / 設定済 + header 不一致 = 401」なので、**401 が返る probe は「生きているが自分の token が古い」** と解釈して token ファイルを読み直す。shim の定期 ping も同 header を付ける（付け忘れると dead-man-switch が誤発動して backend が 5 分で落ちる — integration test の必須ケース）

backend ensure（`_ensure_backend` の宇宙版 — `gaottt/server/mcp_proxy.py:143` を下敷きに新実装）:
1. per-universe **asyncio.Lock + file lock**（spawn 競合防止 — 同一宇宙への同時初回アクセスで 2 プロセス立たない）
2. port probe（`_probe_backend` 流用）→ 生きていれば URL 返却
3. spawn: `_spawn_backend_detached` 相当だが **env を明示構築**して渡す:
   ```python
   env = {
       "GAOTTT_DATA_DIR": str(universe_dir),
       "GAOTTT_EMBEDDER_ENDPOINT": config.embedder_endpoint,
       "GAOTTT_OWNER_LEASE_ENABLED": "true",
       # + manifest / supervisor config 由来の allowlist された knob のみ
   }
   ```
   継承 env に頼らない（**proxy backend env 継承罠のクラス解消** — `project_proxy_backend_env_not_delivered` の再発防止をここで機構化）
4. **同時 spawn 上限 semaphore（default 3）** — cold respawn spike 対策（wiki 計画 §5）
5. readiness poll（90s、既存と同じ）

宇宙 backend 自体は **既存の `mcp_server --transport streamable-http` を無改修で使う**（idle watchdog も dead-man-switch もそのまま効く）。ping は shim → backend 直通なので supervisor は生存監視に関与しない（自然休眠が今のまま機能する）。

### MV3-3: shim の宇宙対応

`gaottt/server/mcp_proxy.py` に optional 引数を追加（**default 挙動不変**）:
- `--supervisor-url http://127.0.0.1:7880`（or `GAOTTT_SUPERVISOR_URL`）+ `GAOTTT_API_KEY`
- 指定時: 起動時に `POST /route` → 返った URL に接続（自前 spawn はしない）。接続断 → route 再取得 → 再接続（backend が休眠していたら supervisor が respawn する）
- 未指定時: 現行の 7878 auto-spawn 経路そのまま

REST 側の運用線を明確にする（Codex レビュー反映）: **v1 の商用ラインでは宇宙への REST アクセスは提供しない**。理由: ① REST app は `build_engine()` で独自 engine を立てる = managed 宇宙では **owner lease が二重 engine を拒否する**（構造的に開けない）、② backend port の MCP は token 保護されるが REST 相当の口は無い。テナント管理者が REST 相当の操作を必要とする場合は supervisor admin API を拡張する（MV4 で reverse-proxy + key 検証込みで検討）。MCP/REST parity 鉄則は「engine の能力」の話であり、デプロイ経路の提供範囲はこの限りではない — この解釈を [Architecture — Overview](../wiki/Architecture-Overview.md) 設計判断表に記録する。

### テスト / acceptance

- 新規 `tests/unit/test_multiverse_registry.py` — port 割当 / key hash / scan 突き合わせ
- 新規 `tests/integration/test_supervisor.py` — **StubEmbedder service + 短い idle_timeout** で:
  1. 宇宙 A/B 作成 → A に remember → B の recall に出ない（**相互不可視性**）
  2. idle → backend 自然消滅 → 再 route で respawn → データ保持
  3. 同一宇宙へ並行 route ×5 → backend は 1 つ（spawn 競合ロック）
  4. 不正キー → 401
  5. **token 経路**: token なし直叩き → 401 / token 付き probe・ping が通り dead-man-switch が誤発動しない（ping ヘッダ付け忘れ = backend が 5 分で落ちる regression の fence）
  6. **supervisor 再起動**: 稼働中 backend を残したまま supervisor を restart → `backend.token` 読み戻しで route 継続（re-spawn されないこと）
- 手動 e2e: 実 RURI + embedding service + supervisor + Claude Code shim 2 枚（別宇宙）で会話 → 干渉なし

**所要**: 1.5〜2 週（レビュー反映済みの見積もり）
**rollback**: supervisor を使わなければ全経路が従来のまま

### ドキュメント

- 新規 [Operations — Multiverse Setup](../wiki/Operations-Multiverse-Setup.md)（`_Sidebar.md` / `Home.md` 更新を忘れない）
- [Architecture — Overview](../wiki/Architecture-Overview.md) 設計判断表に「supervisor API は MCP/REST parity 対象外（管理面）」を追記

---

## MV4 — control plane（Postgres）

**目的**: テナント・宇宙の台帳、課金・監査。**独立デプロイ物** — engine コードに触れない。

### 実装

新規トップレベル `control/`（`gaottt/` パッケージの外 — engine と依存を混ぜない。`pyproject.toml` の workspace member か別 `control/pyproject.toml`）:

- `control/schema/*.sql` — 番号付き plain SQL migration（`scripts/migrate.py` の versioned 思想を踏襲、alembic は依存が重いので v1 では入れない）:
  ```sql
  tenants(tenant_id PK, name, created_at)
  users(user_id PK, tenant_id FK, email, created_at)
  universes(universe_id PK, tenant_id FK, owner_user FK, host_id,
            embedder_id, embedder_version, status, created_at)
  usage_events(id PK, universe_id, event_type, count, window_start)  -- 集計単位で受ける
  audit_log(id PK, tenant_id, actor, action, target, at)
  ```
- `control/api.py` — FastAPI + asyncpg。tenant/universe CRUD、supervisor 向け sync API
- supervisor 側に `gaottt/multiverse/control_client.py`:
  - **pull**: 起動時 + `control_sync_interval_seconds`（default 300）周期で自ホストの宇宙一覧を取得 → local registry と突き合わせ（**local manifest が一次、control plane は集約** — 矛盾時は local を正として control へ報告）
  - **push**: usage counter（recall/remember/ingest 回数）をメモリで集計し 60s ごとに batch POST。**送信失敗はローカル spool（`logs/usage-spool/`）に退避して次回再送** — control plane 落ちでもホスト自走（degraded mode）
- 認証: supervisor ↔ control plane は host token（control plane 発行）

### テスト / acceptance

- `control/tests/` — API round-trip（Postgres は `docker compose -f control/compose.yml up` の disposable、CI 外の手動でも可）
- supervisor integration: control plane 停止状態で route / spawn / usage spool が正常継続（degraded mode の acceptance が本丸）

**所要**: 1〜2 週
**rollback**: supervisor は control plane なしで自走可能（設定しなければ pure local）

---

## MV5 — backup / DR

**目的**: 宇宙単位の継続バックアップと復旧 runbook。コードより **手順と drill** が成果物。

### 実装

- `deploy/litestream.yml` 雛形 — `universes/*/gaottt.db` を対象にする生成スクリプト `scripts/gen_litestream_config.py`（宇宙の増減で再生成、supervisor の宇宙作成 hook から呼ぶ）
- **バックアップ対象 = SQLite + manifest.json の 2 点セット**（FAISS は対象外 — ただし前提条件は下記）
- 新規 `scripts/dr_drill.py`:
  1. tmp 宇宙を作成 + StubEmbedder で populate
  2. backup 取得 → 宇宙ディレクトリ破壊 → restore
  3. `rebuild_faiss_from_db.py --apply` → `--check`
  4. 起動時診断 Tier A/B green を assert
  5. **exit 0 = drill 成功**。四半期ごと実行を runbook に明記
- **embedder artifact pinning**: manifest の `embedder_id/version` に対応する model の取得手段（HF cache の tar 退避 or 社内ミラー）を runbook の必須項目にする。「SQLite だけで復元可能」は同一モデル入手が前提（Codex レビュー反映）
- 大宇宙向け optional: FAISS snapshot も replicate 対象に追加する設定例（RTO 短縮）

### ドキュメント

- 新規 [Operations — Backup Multiverse](../wiki/Operations-Backup-Multiverse.md)（wiki 計画 §4 Stage 4 で予告済みのページ。`_Sidebar.md` / `Home.md` 更新）
- 復旧手順: 他プロセス停止（owner lease 確認）→ restore → model 用意 → FAISS rebuild → 診断

**所要**: 2〜3 日
**rollback**: 運用設定のみ、コード rollback 対象なし

---

## MV6 — 英語宇宙

**目的**: embedder per universe の実運用第一号。**着手条件: MV1 完了 + EN embedder 選定の evaluation 完了**。

### 実装

1. **選定 evaluation** — [Plans — Embedder Comparison](../wiki/Plans-Embedder-Comparison.md) の Phase A 手法（discriminative power probe + cross-lingual probe）を multilingual-e5 / BGE-M3 で再実行。EN golden corpus（次項）で Tier 3 相当の quality 確認。**この評価が no-go なら MV6 全体を保留**（RikkaBotan の前例あり — 評価が先、実装が後）
2. `tests/perf/golden_corpus_en/` — 30 chunks × 11 queries の EN 版（JA 版の翻訳ではなく EN ネイティブな corpus を新規作成 — 翻訳だと lexical 信号が不自然になる）
3. config の per-embedder プロファイル: `GaOTTTConfig` に `embedder_profile: str = "ruri-ja"` を追加し、cosine 帯依存の knob（reach floor / gate 閾値 / `ambient_min_relevance` 等 — 対象 knob の洗い出し自体が作業項目）を profile dict で上書きする機構。default profile = 現行値で **既存挙動不変**
4. embedding service の複数モデルホスト: `--model` を複数指定 or モデルごとに service を分けて port を変える（v1 は **後者** — service 1 プロセス 1 モデルが単純で、VRAM 管理も明快）
5. supervisor の宇宙作成 API で `embedder_id` 指定 → manifest に記録 → spawn env の `GAOTTT_EMBEDDER_ENDPOINT` をモデル対応 service に向ける
6. Tier 3/7 の EN 版で再チューニング → profile 確定 → [Operations — Tuning](../wiki/Operations-Tuning.md) に EN 列を追加

### テスト / acceptance

- EN golden corpus で Tier 3 strict / Tier 7 regression の EN 版が green
- JA 宇宙の全 suite / perf に **一切の diff がない**こと（profile 機構が default 不変である証明）

**所要**: 1〜2 週（評価込み、チューニングの収束次第で伸びる）
**rollback**: EN 宇宙を作らなければ何も変わらない

---

## 依存関係と推奨順序

```
MV0 ──→ MV1 ──→ MV2 ──→ MV3 ──┬─→ MV4（並行可）
                               └─→ MV5（MV3 の registry/hook に依存 — MV4 と並行可、MV3 とは直列）
MV1 ──────────────────────────────→ MV6（MV3 完了後が現実的）
```

- **最初の出荷可能ライン**: MV0+MV1（embedder 分離だけでも単一ユーザー運用の RAM/VRAM が改善する独立価値）
- **Multiverse として成立するライン**: MV3 完了時（手動運用の control plane なしで 1 テナント運用可能）
- **商用ライン**: MV4+MV5 完了時

合計所要: **商用ラインまで 7〜10 週**（Codex レビュー反映 — MV3 は新規制御面 + auth + 実プロセス integration、MV4 は migration/host token/usage spool まで含むため、当初の 5〜6 週は楽観的と判定。MV4/MV5 の並行でどこまで縮むかは MV3 完了時に再見積もり）。

## 検証コマンド早見表

```bash
# 各 stage 共通の締め
.venv/bin/python -m pytest tests/ -q
ruff check gaottt/ tests/
.venv/bin/python scripts/rest_smoke.py && .venv/bin/python scripts/mcp_smoke.py

# MV1 の数値等価 + latency（real RURI、手動）
.venv/bin/python -m pytest tests/perf/test_tier6_remote_embedder.py -v -s
.venv/bin/python scripts/perf_baseline.py --label post-mv1 && .venv/bin/python scripts/perf_diff.py

# MV3 の e2e（隔離、本番 DB 不可触）
GAOTTT_MULTIVERSE_ROOT=/tmp/gaottt-mv-test .venv/bin/python -m pytest tests/integration/test_supervisor.py -x -v

# MV5 の DR drill
.venv/bin/python scripts/dr_drill.py
```

## リスク早見表（実装中に迷ったら）

| 迷い | 答え | 根拠 |
|---|---|---|
| engine に universe_id を持たせたくなった | **持たせない** | 宇宙分離は data_dir で行う。engine 内にユーザー/宇宙次元を入れるのは Roadmap で不採用確定 |
| physics の knob を supervisor から動的変更したくなった | env/manifest 経由の起動時注入のみ | 実行中変更は cache/積分の整合を壊す |
| embedding service に認証を足したくなった | localhost bind を維持、認証は supervisor の仕事 | 認証検証点は 1 箇所（wiki 計画 §4 Stage 2） |
| bit-exact テストが通らない | 通らなくて正しい | `np.allclose` + cosine 差 + top-K 一致の 3 段が仕様 |
| MCP に管理ツールを生やしたくなった | 生やさない | 破壊的管理操作を LLM に露出しない（`/reset` と同じ判断） |
| 新スキーマ列 / 新 config | `DEFAULT` 必須 / 未設定 = 現行挙動 | CLAUDE.md 既存規約 |

## 関連

- [Plans — Multiverse Scale-Out](../wiki/Plans-Multiverse-Scale-Out.md) — 戦略計画（SoT、設計根拠はこちら）
- [rest-mcp-unification-plan.md](rest-mcp-unification-plan.md) — 本形式の前例（Phase S）
- [Operations — Migration](../wiki/Operations-Migration.md) — versioned migration（MV6 の embedder 乗り換えで使用）
- [Architecture — Concurrency](../wiki/Architecture-Concurrency.md) — lease が閉じる事故クラスの記録
