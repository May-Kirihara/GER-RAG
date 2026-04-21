# Guide — Using GaOTTT as Long-Term Memory

GaOTTT を **AI エージェントの外部長期記憶** として使う基本フロー。

## 想定シナリオ

- Claude Code / Claude Desktop で日々作業し、**セッションをまたいで知識を蓄積したい**
- 過去のトラブルシュート、設計判断、ユーザーの好みを **次回も引き出したい**
- 試行錯誤の経験が **重力で互いに引き寄せ合い、創発的な発見を生む** ことを楽しみたい

## 基本サイクル

### 1. セッション冒頭で文脈復元

```
recall(query="前回のセッションで何をしたか", source_filter=["compaction", "agent"])
```

### 2. 重要な気づきを保存（即時）

ユーザーが好み・制約を言った瞬間:
```
remember(content="ユーザーは uv を使う、pip 禁止", source="user", tags=["preference", "tooling"])
```

エラー解決の瞬間（成功体験は失敗と同等以上に価値がある）:
```
remember(
  content="numpy配列にor演算子はValueError。原因はbool変換の曖昧さ。解決はif x is not None で明示的に分岐",
  source="agent", tags=["troubleshooting", "python", "numpy"], emotion=0.6,
)
```

### 3. 困ったら recall

```
recall(query="numpy ValueError or 演算子", source_filter=["agent"])
```
過去の失敗経験が重力で浮上し、同じ轍を踏まずに済む。

### 4. 行き詰まったら explore

```
explore(query="この問題に使えそうな過去の経験", diversity=0.8)
```
温度を上げ、異分野の記憶を引き寄せる。

### 5. コンパクション直前に退避

会話が長くなってきたら:
```
remember(
  content="本セッションの要点: 1)... 2)... 3)...",
  source="compaction", context="<日付> のセッション要約",
)
```

## ヒント

- **emotion を設定する** — 印象深かった瞬間は `emotion=0.7` などで boost。再 recall されやすくなる
- **tags を活用** — `["troubleshooting", "<言語名>", "<ライブラリ名>"]` 等で後の絞り込みを楽に
- **source_filter で制限** — `recall(source_filter=["agent"])` で「自分の判断だけ」検索できる
- **重複は自動 skip** — 同じ content を 2 回 remember しても DB は汚れない（SHA-256）
- **mass の蓄積を意識** — 何度も recall されたものは引力が増す（自然な重要度の創発）

## ファイル一括取り込み

既にあるノート、書籍、ログを GaOTTT の重力場に投入:

```bash
# 直接 CLI
.venv/bin/python scripts/load_files.py ~/notes/ --recursive --source notes
.venv/bin/python scripts/load_csv.py --csv path/to/data.csv

# MCP 経由
ingest(path="~/notes/", pattern="*.md", recursive=true)
```

## 状態を眺める

```
reflect(aspect="summary")          # 全体統計
reflect(aspect="hot_topics")       # よく recall される記憶
reflect(aspect="connections")      # 強い共起関係
reflect(aspect="dormant")          # 長期未アクセス
```

→ より発展的な使い方:
- [Use as Task Manager](Guides-Use-As-Task-Manager.md)
- [Use as Persona Base](Guides-Use-As-Persona-Base.md)
- [Multi-Agent Setup](Guides-Multi-Agent.md)

→ ツール詳細: [MCP Reference — Memory](MCP-Reference-Memory.md)
