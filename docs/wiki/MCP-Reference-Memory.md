# MCP Reference — Memory Tools

GaOTTT の中核となる 6 つの記憶ツール。詳細仕様は [`SKILL.md`](../../SKILL.md) のツールセクションを正とする。

## remember

知識を長期記憶に保存する。

```
remember(
  content: str,
  source: str = "agent",                  # agent/user/system/compaction/hypothesis/task/commitment/value/intention/style/relationship:<name>
  tags: list[str] | None = None,
  context: str | None = None,
  ttl_seconds: float | None = None,       # 既定: source が hypothesis なら 7日、task なら 30日、commitment なら 14日
  emotion: float = 0.0,                   # [-1.0, 1.0]、|magnitude| が boost
  certainty: float = 1.0,                 # [0.0, 1.0]、半減期 30日で減衰
)
→ "Remembered. ID: <uuid>" or "Already exists in memory (duplicate content)."
```

## recall

重力波伝播による検索。`prefetch` キャッシュを透過消費する。

```
recall(
  query: str,
  top_k: int = 5,
  source_filter: list[str] | None = None,
  wave_depth: int | None = None,
  wave_k: int | None = None,
  force_refresh: bool = False,            # True で prefetch キャッシュを無視
)
→ 各結果に id=<uuid> が含まれる
```

## explore

温度を上げた創発的探索。離れた記憶も引き寄せる。

```
explore(query=..., diversity=0.0-1.0, top_k=10)
```

- `diversity=0.0` 通常検索に近い
- `diversity=0.5` 適度な探索（既定）
- `diversity=1.0` 最大多様性

## reflect

メモリ状態の分析。**11 種類の aspect**:

| aspect | 内容 |
|---|---|
| `summary` | 全体統計 |
| `hot_topics` | 高質量ノード |
| `connections` | 強い共起エッジ |
| `dormant` | 長期間未アクセス |
| `duplicates` | 近接重複クラスタ |
| `relations` | 有向リレーション |
| `tasks_todo` / `tasks_doing` / `tasks_completed` / `tasks_abandoned` | Phase D タスク系 |
| `commitments` / `values` / `intentions` / `relationships` / `persona` | Phase D 人格系 |

→ Phase D の使い方は [Tasks & Persona](MCP-Reference-Tasks-and-Persona.md)

## ingest

ファイル / ディレクトリの一括取り込み。

```
ingest(
  path: str,
  source: str = "file",
  recursive: bool = False,
  pattern: str = "*.md,*.txt",
  chunk_size: int = 2000,
)
```

対応形式: `.md`（`##` 見出し or サイズで分割）、`.txt`（段落で分割）、`.csv`（行単位、`content`/`text`/`body` 列を自動検出）

## auto_remember

会話 transcript から保存候補をヒューリスティック抽出（**保存はしない**）。

```
auto_remember(transcript=..., max_candidates=5, include_reasons=True)
```

抽出される傾向:
- 決定・結論・採用/却下
- 失敗・成功・エラー・解決
- ユーザーの好み・禁止・制約
- 教訓・次回への申し送り
- 数値（メトリクス候補）

返り値の各候補には推奨 `source` と `tags` が付くので、内容を確認してから `remember` で正式保存する。

## ソースの使い分け

| source | TTL | 用途 |
|---|---|---|
| `agent` | 永続 | あなた自身の判断・発見・学び |
| `user` | 永続 | ユーザーの発言・好み・指示 |
| `compaction` | 永続 | コンテキスト圧縮時の退避 |
| `system` | 永続 | システム情報 |
| `hypothesis` | 7 日 | 仮説（自動消滅） |
| `task` | 30 日 | タスク（要 complete/abandon/revalidate） |
| `commitment` | 14 日 | 期限付き約束 |
| `value` / `intention` / `style` / `relationship:<name>` | 永続 | Phase D 人格層 |

→ 関連: [Tasks & Persona](MCP-Reference-Tasks-and-Persona.md), [Maintenance](MCP-Reference-Maintenance.md)
