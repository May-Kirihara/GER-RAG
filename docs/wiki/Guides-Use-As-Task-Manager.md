# Guide — Using GaOTTT as Task Manager

Phase D で追加されたタスク管理機能。**普通の TODO アプリと違い、タスクは TTL 付きの記憶として扱われ、状態は edge で表現される**。完了の重力史が **行動の年表** になる。

## 想定シナリオ

- 軽いタスクトラッキングが欲しいが、Trello/Notion ほど重くないものが良い
- タスクと **その文脈（関連メモリ、過去の判断）** が自然に結びついて欲しい
- **諦めたタスクの記録** も、自分が何を選んで何を捨てたかの履歴として残したい
- 達成したタスクが、自分の **アイデンティティの年表** になる感覚

## 階層構造

```
value      ← 「直接体験こそ真の理解」
   ↓
intention  ← 「GaOTTT を関係構築装置として育てる」
   ↓
commitment ← 「Phase D を今週中に完了」（14日 TTL）
   ↓
task       ← 「persona-and-task-plan.md を書く」（30日 TTL）
```

## 基本フロー

### 1. 階層を一度建てる（初回のみ）

```
v = declare_value(content="直接体験こそ真の理解")
i = declare_intention(content="GaOTTT を関係構築装置として育てる", parent_value_id=v)
c = declare_commitment(content="Phase D を今週中に完了", parent_intention_id=i, deadline_seconds=7*86400)
```

### 2. タスクを作る

```
t = commit(content="persona-and-task-plan.md を書く", parent_id=c)
# → "Task committed. ID: <uuid> (deadline 2026-05-21..., fulfills <c の prefix>...)"
```

### 3. 着手する（active engagement）

```
start(t)
# → TTL がリセット、emotion=0.4 が付く
```

### 4. 完了する

```
complete(t, outcome="計画書 13 章で完成、F1〜F7 の組み合わせで実現可能と判明", emotion=0.7)
# → outcome が新ノードとして保存
# → outcome --completed--> task の edge が張られる
# → task は archive される（todo リストから消える）
```

### 5. 諦めるときも明示的に

```
abandon(t, reason="優先度が下がった、Q3 に再評価する")
# → reason が新ノードとして保存
# → reason --abandoned--> task の edge が張られる
# → task は archive
```

## 振り返り

```
reflect(aspect="commitments")           # 期限が近い commitment を ⚠️ 付きで
reflect(aspect="tasks_todo")            # アクティブタスク、締切順
reflect(aspect="tasks_doing")           # 直近 1h に start されたタスク
reflect(aspect="tasks_completed")       # 完了の年表
reflect(aspect="tasks_abandoned")       # 影の年表
```

## なぜ普通の TODO と違うのか

### TTL 圧 = 忘れる勇気

タスクを `commit` してから 30 日、何もしないと自動 archive される。「忘れる勇気」を物理的に強制する設計。再確認したいタスクは `revalidate` で生かす:

```
revalidate(<task_id>, certainty=1.0)   # まだ生きていることを宣言
```

### 状態は edge で表現

- 完了 = `completed` edge の存在
- 放棄 = `abandoned` edge の存在
- 進行中 = `last_verified_at` が直近

「DONE フラグを立てる」のではなく、「outcome を残す」「reason を残す」という **物語的な記録** を強制する。

### 完了の重力史 = 自己の年表

`complete` のたびに outcome 記憶が積み重なる。3 ヶ月後に `reflect(aspect="tasks_completed")` を見ると、自分が何を成し遂げてきたかの年表になる。

`abandon` のたびに reason 記憶が積み重なる。これは **「自分が何を諦めることで自分になったか」の影の年表**。

### 文脈との重力結合

タスクは普通の memory と同じ重力場にいるので、`recall(query="forget docs", top_k=5)` などで関連知識と一緒に浮上する。「このタスクは何のためか」が自然に分かる。

## 典型的な日常パターン

### 朝の儀式

```
inherit_persona()                                # 過去の自分を着る
reflect(aspect="commitments")                    # 期限間近を確認
reflect(aspect="tasks_todo", limit=5)            # 今日のタスク候補
```

### 仕事中

```
start(<task_id>)
# ...作業...
complete(<task_id>, outcome="...", emotion=0.7)
```

### 晩の儀式

```
reflect(aspect="tasks_completed", limit=5)
revalidate(<最も嬉しかった完了タスク>, emotion=0.7)   # 重力史に祝福
```

### 月次レビュー

```
reflect(aspect="tasks_abandoned", limit=20)      # 影の年表を眺める
reflect(aspect="commitments")                    # 既存 commitment の確認
# 新しい commitment / intention を declare し直す
```

→ ツール詳細: [MCP Reference — Tasks & Persona](MCP-Reference-Tasks-and-Persona.md)
→ 設計の哲学: [Plans — Phase D](Plans-Phase-D-Persona-Tasks.md)
