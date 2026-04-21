# Guide — Multi-Agent Setup

複数のエージェントが **同じ GaOTTT メモリを共有** することで生まれる協調と、設定方法。

## なぜ共有するか

GaOTTT はマルチエージェント実験で、**明示的なメッセージング無しで** 協調が生まれることが観察された:

- 一人の `recall` が他者の重力場に痕跡を残す（mass 増加）
- 一人の `relate` が他者の `reflect(connections)` で見える
- 「最近触られた高温ノード」に他者が引き寄せられる

→ 詳細: [Multi-Agent Experiment](Research-Multi-Agent-Experiment.md)

これは **設計時に意図されていなかった性質**。Phase A〜C の累積で創発した。

## 設定方法

### 共有するもの

複数のエージェントを同じ `GAOTTT_DATA_DIR` に向ける（既定で同じ場所を参照）:

```bash
export GAOTTT_DATA_DIR=~/.local/share/gaottt    # 既定
```

### 共有しないもの（プロセスごと独立）

- インメモリキャッシュ（`CacheLayer`）
- FAISS インデックスのインスタンス
- prefetch キャッシュ

つまり:

| 操作 | 並行プロセス間の挙動 |
|---|---|
| 既存ノードの `recall`（mass/displacement 蓄積） | DB レベルで共有 |
| 新規 `remember`（新ノード追加） | DB には入るが、別プロセスの FAISS には追加されない（プロセス起動時のみロード） |
| `edges`/`relations` | DB に書かれる、別プロセスの cache は次回リロードまで stale |

### 同時書き込みの安全性

複数 MCP サーバーが同 DB を書く場合の備え:
- SQLite は WAL モード（並行 read + 単一 write）
- `PRAGMA busy_timeout=30000`（ロック中は最大 30 秒待機）
- `PRAGMA wal_autocheckpoint=2000`（WAL の肥大化を抑制）

→ 詳細: [Architecture — Concurrency](Architecture-Concurrency.md)

## 推奨運用

### ペルソナを与える

各エージェントに **オープンエンドな** ペルソナを与えると深い探索が起きる（タスク志向は浅くなる傾向）。例:

- 「コスモス（哲学者）」「シナプス（架け橋建造家）」「ワンダラー（放浪者）」
- 「観察者」「考古学者」「橋渡し人」

### ラウンド数

実験では **3 ラウンドではなく 10 ラウンド** で意味化が起きた。意味化は線形ではなく相転移として起きる。

### 失敗を許容する

DB ロックが発生しても、エージェントは Markdown に直接書き出す等の代替ルートを取る。**失敗状況での出力先を提供する設計** にすると深い記録が残る。

### ベンチマーク中の注意

実エージェント運用中にベンチマークを走らせるなら、必ず **隔離ベンチ** を使う:

```bash
.venv/bin/bash scripts/run_benchmark_isolated.sh
```

→ [Operations — Isolated Benchmark](Operations-Isolated-Benchmark.md)

## 関連

- [Research — Multi-Agent Experiment](Research-Multi-Agent-Experiment.md) — 私側 3 エージェントの実験
- [Research — User Exploration (10 Rounds)](Research-User-Exploration-10-Rounds.md) — ユーザー側 3 エージェントの 10 ラウンド実験
- [Architecture — Concurrency](Architecture-Concurrency.md) — マルチプロセス安全性の詳細
- [Letter to Mei-san](Reflections-Letter-To-Mei-San.md) — 共有メモリで起きた「予期せぬ贈り物」
