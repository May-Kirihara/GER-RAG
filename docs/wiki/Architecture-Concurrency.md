# Architecture — Concurrency

複数のプロセス（複数 MCP サーバー、複数エージェント）が同 DB を共有して並行アクセスする際の安全設計。

## SQLite WAL モード

```python
# gaottt/store/sqlite_store.py
PRAGMA journal_mode = WAL              # 並行 read + 単一 write
PRAGMA synchronous = NORMAL            # 性能とのバランス
PRAGMA busy_timeout = 30000            # ロック中は最大 30 秒待機
PRAGMA wal_autocheckpoint = 2000       # WAL 肥大化抑制
```

- **WAL モード**: 読み込みは複数プロセスから同時に可能、書き込みは 1 プロセスずつ
- **busy_timeout = 30000ms**: 書き込みロック取得失敗時は 30 秒リトライ
- **wal_autocheckpoint = 2000 pages (~8 MB)**: WAL ファイルの肥大化を防ぐ

## マルチプロセスでの動作

| 操作 | 動作 |
|---|---|
| 既存ノードの recall (mass/displacement 蓄積) | DB レベルで共有、cache はプロセス毎に独立 |
| 新規 remember (新ノード追加) | DB に書かれ、書き込みプロセスの FAISS は in-memory 即時反映、disk への永続化は `faiss_save_interval_seconds`（既定 5s）周期。別プロセスは startup 時に load するため、その時点の disk 状態が見える |
| edges/relations | DB に書かれる、別プロセスの cache は次回リロードまで stale |

### FAISS の罠（write-behind 導入で軽減）

各プロセスはそれぞれ FAISS インスタンスを持つ。歴史的に「`shutdown()` まで disk に save しない」設計だったため、shutdown しない長期常駐プロセス（MCP サーバー等）の `remember` は他プロセスから永久に invisible だった。

これは **FAISS write-behind** で解消（2026-05-10 修正）:

```python
# core/engine.py
async def _faiss_save_loop(self) -> None:
    """Background save: flush in-memory FAISS additions to disk every
    faiss_save_interval_seconds. Without this, brand-new `remember`
    lives only in the writing process's RAM until shutdown(),
    making other processes' recall() blind to it."""
```

- 周期は `config.faiss_save_interval_seconds`（既定 5s、`0` で無効化）
- `index_documents` と `_rebuild_faiss_index` で `_faiss_dirty=True` を立てる
- loop は `_faiss_dirty=False`（claim）→ `to_thread(faiss_index.save, path)`（IO はスレッドへ）→ 失敗時のみ `True` に戻す
- `shutdown()` で停止前に最終 save を呼ぶ（残った dirty を flush）
- 残る race: 「save 中の新規 add は次の tick で saved」（実用上影響なし）
- 残る競合: A と B が**同時に save** すると後勝ち → 重要書き込み後は `engine.compact(rebuild_faiss=True)` で再構築可（保険）

### virtual FAISS の write-behind（2026-05-13 追加）

raw FAISS の write-behind は新規 `remember` の他プロセス可視化を担うが、**virtual FAISS** (= raw + displacement, Phase H Stage 4) は別腹で、Phase J Stage 1 以前は `compact(rebuild_faiss=True)` または起動時 (disk file 欠落時) のみ rebuild されていた。Phase I/J query attraction で蓄積した displacement が次の compact まで他プロセスの seed pool に反映されない問題があった。

```python
# core/engine.py
async def _virtual_faiss_save_loop(self) -> None:
    """cache.virtual_faiss_dirty が立っていれば full rebuild + save。
    set_displacement / evict_node / restore で flag が立つ。"""
```

- 周期は `config.virtual_faiss_save_interval_seconds`（既定 60s、`0` で無効化）
- `cache.set_displacement` / `cache.evict_node` / `engine.restore` で `cache.virtual_faiss_dirty=True`
- loop は `dirty=False`（claim）→ `await self._rebuild_virtual_faiss_index()` → `to_thread(virtual_faiss_index.save, path)` → 失敗時のみ `True` に戻す
- rebuild は active node 全件の O(N) full rebuild（差分 update は FAISS IndexFlatIP の制約で困難）。23k 件規模で ~数百 ms、60s 周期なら負荷 < 1%
- `shutdown()` で停止前に最終 save を呼ぶ（既存 shutdown path がカバー）
- raw 5s + virtual 60s と周期が違うのは、virtual rebuild が O(N) で raw save (in-memory matrix → disk) より重いため。Phase I/J の incremental displacement は急ぐ伝播ではないという判断

### 逆方向上書きの罠（Bidirectional cache overwrite）

`cache.flush_to_store` は dirty フラグベースで、自プロセス内 cache の現在値を SQLite に push する。これは startup 時の `load_from_store` でしか他プロセスの変更を pull しない設計のため、**古い cache を持つプロセスが flush し続ける限り、別プロセスが書いた新しい値を逆方向に上書き** する。

具体例: 2026-05-10 Phase G Stage 0 priming セッションで、隔離スクリプト A がすべての node に `displacement` / `velocity` / `mass` を加算 → cache.flush_to_store で SQLite に書き込み。同時に古い MCP server プロセス B (cache に Stage 0 効果なし) が 5 秒後の write-behind tick で flush → A の書き込みを上書き。次の dry-run で「適用したはずの 500 件のうち 1 件しか pre-existing displacement > 0 にならない」現象として顕在化した。

対処:
- 一時的な bulk 書き換え（Stage 0 priming 等）の前に、他の MCP server プロセスを `kill` して flush ループを止める。書き込み完了後に再起動。
- 段階的に運用したい場合は、書き換え対象 node の `last_access` を更新するなど、**通常の write-behind tick で自然に dirty になる経路** を経由する。
- **構造的解** (2026-05-13): MCP を **streamable-http transport で 1 process だけ常駐** させ、複数 agent (Claude Code / opencode / 他) はその HTTP server に接続して engine を共有する。これで cache 自体が単一になり、bidirectional overwrite の前提が消える。詳細: [Operations — Server Setup](Operations-Server-Setup.md)「起動モード」。stdio mode は legacy として残るが multi-agent 環境では非推奨。
- **構造的解 (2): owner lease (MV2 — 2026-07-02)**: 同じ `data_dir` を複数プロセスが開くこと自体を機構で防ぐ。`OwnerLease`（`<data_dir>/owner.lock` + `fcntl.flock` guard file）が「1 宇宙 1 書き込みオーナー」を強制し、2 つ目のプロセスの `startup()` が `LeaseHeldError` で落ちる。オーナーは heartbeat（既定 10s 周期）で生存を更新し、heartbeat 停止 > `lease_stale_seconds`（既定 60s）で別プロセスが takeover 可（`--force-takeover` で強制も可）。lease 喪失時（heartbeat read-back で owner_id 不一致検出）は engine が **read-only に遷移**: cache flush / FAISS save の全永続化経路（write-behind loop / final flush / FAISS save loop / virtual FAISS save loop の 4 経路）を `_persist_blocked` latch で skip、mutating operation 14 種は `LeaseLostError` で拒否、`query` / `prefetch` は passive フォールバック（結果返す・field 更新しない）、read 系（`recall(passive=True)` / `get_node` / `reflect`）は継続。standalone 構成は `owner_lease_enabled=False`（default）で無効、supervisor 管理宇宙は `manifest.managed=True` で config に関係なく強制。詳細: [Plans — Multiverse Scale-Out](Plans-Multiverse-Scale-Out.md) §Stage 2、[Operations — Tuning](Operations-Tuning.md)「Multiverse owner lease」節。

### Dream loop（Phase G — Stage 2）

`startup()` で起動するもう一つのバックグラウンドタスク。`config.dream_interval_seconds`（既定 60s）周期で quiet node を synthetic recall し、co-occurrence と gravity 場を時間軸で育てる。

- 並行 task: `_dream_task`（停止 event は `_dream_stop`）
- shutdown 順: dream → lease heartbeat → raw faiss save → virtual faiss save → cache write-behind の順で停止（MV2 以降: write-behind 停止後に ownership 再検証 → final flush → final FAISS save → lease release）
- 例外は loop 内で握りつぶし、次 tick で retry
- `dream_enabled=False` または `dream_interval_seconds=0` で完全 skip
- マルチプロセス: 各プロセスが独自の dream loop を持つ。同じ DB に対して複数プロセスが synthetic recall を撃つ → mass 加算が二重に進む可能性は理論上あるが、`return_count` は更新しないので saturation は乱れず、運用上の影響は小さい
- **streamable-http mode** ([Operations — Server Setup](Operations-Server-Setup.md)) では server が 1 process なので dream loop も 1 つだけ、重複起動の心配なし。stdio + 複数 agent の場合のみ多重 dream に注意 (2026-05-13 follow-up: `await asyncio.sleep(0)` を candidates 間に挿入したので foreground starvation は解消、多重 dream でも recall は応答する)

詳細: [Plans — Phase G — Memory Genesis](Plans-Phase-G-Memory-Genesis.md)、[Architecture — Gravity Model](Architecture-Gravity-Model.md) の「夢による継続的な軌道捕獲」節

### 防御的境界チェック

`faiss_index.search` は `_id_map[idx]` の境界を明示的にチェック:
```python
if idx < 0 or idx >= id_map_len:
    continue  # IndexError ではなく skip
```

`.ids` ファイル破損やインデックスとマップのズレに対する防御。

## アーカイブ後の挙動

`archive`/`forget`/`merge`/`compact` 後は **prefetch cache を invalidate** する:

```python
# core/engine.py
async def archive(self, node_ids: list[str]) -> int:
    ...
    if affected:
        self.prefetch_cache.invalidate()
```

これにより destructive op 後の stale な hit を防ぐ。

## write-behind の安全性

`CacheLayer` は dirty フラグ付きで in-memory に書き込み、5 秒間隔（既定）でバックグラウンドタスクが flush:

```
remember
  ↓
cache.set_node (dirty=True)
  ↓
dirty_nodes に追加
  ↓
5 秒後 (or shutdown 時)
  ↓
flush_to_store(): SqliteStore に書き込み
```

shutdown 時に明示的 `flush_to_store` を呼ぶので、graceful な終了であればロスなし。

`Ctrl+C` 等で強制終了すると、最後の 5 秒分の dirty な変位等が失われる可能性あり（ただしドキュメント本体と embedding は永続化されているので再構築可）。

## 確認・運用

ベンチマーク中に並列実行をシミュレートしたい場合は隔離 DB を使用:

```bash
.venv/bin/bash scripts/run_benchmark_isolated.sh
# → /tmp/gaottt-bench/ で隔離実行、本番 DB は不可触
```

→ [Operations — Isolated Benchmark](Operations-Isolated-Benchmark.md)

## 関連

- [Storage & Schema](Architecture-Storage-And-Schema.md) — テーブル定義
- [Operations — Compact & Backup](Operations-Compact-And-Backup.md) — FAISS rebuild の詳細
- [Operations — Troubleshooting](Operations-Troubleshooting.md) — DB lock 関連
- [Multi-Agent Experiment](Research-Multi-Agent-Experiment.md) — 実運用での発見（busy_timeout 未設定で連続失敗 → 修正の経緯）
