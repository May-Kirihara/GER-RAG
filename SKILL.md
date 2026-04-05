---
name: ger-rag-memory
---

# GER-RAG: Gravitational Long-Term Memory

## What this is

GER-RAGはあなたの**外部長期記憶**です。会話をまたいで知識を蓄積し、使い込むほど関連する記憶が見つかりやすくなります。

記憶はembedding空間上で**重力**により自己組織化されます。頻繁に一緒に検索される知識は互いに引き寄せられ、次の検索で予想外のつながりが発見されます。

## When to use

- **何かを覚えておきたいとき** → `remember`
- **過去の知識や文脈が必要なとき** → `recall`
- **発想の転換・新しいつながりが欲しいとき** → `explore`
- **自分の知識状態を把握したいとき** → `reflect`
- **ファイルから知識を一括取り込みたいとき** → `ingest`

## Tools

### remember

知識を長期記憶に保存する。

```
remember(content="ユーザーはuvでPython環境を管理する。pip禁止。", source="agent", tags=["preference"])
remember(content="GER-RAGのPhase 2では重力変位を実装した", source="compaction", context="コンテキスト圧縮時の退避")
```

**sourceの使い分け:**
- `agent` — あなた自身の判断・発見・学び
- `user` — ユーザーの発言・好み・指示
- `compaction` — コンテキスト圧縮時に失われそうな重要情報の退避
- `system` — システム情報・設定

### recall

重力変位付きの記憶検索。過去に一緒に検索された記憶は互いに近くなっており、見つかりやすい。

```
recall(query="Pythonの環境構築方法", top_k=5)
recall(query="前回の設計判断", source_filter=["agent", "compaction"])
```

### explore

温度を上げた創発的探索。通常の検索では出てこない、意外なつながりを発見する。

```
explore(query="新しいアーキテクチャのアイデア", diversity=0.7)
```

- `diversity=0.0` → 通常の検索に近い
- `diversity=0.5` → 適度な探索（デフォルト）
- `diversity=1.0` → 最大限の多様性

### reflect

記憶の状態を分析する。

```
reflect(aspect="summary")        # 全体の統計
reflect(aspect="hot_topics")     # よく検索される知識
reflect(aspect="connections")    # 強い共起関係
reflect(aspect="dormant")        # 長期間アクセスされていない知識
```

### ingest

ファイルやディレクトリから知識を一括取り込み。

```
ingest(path="~/docs/notes.md")
ingest(path="~/books/", pattern="*.md", recursive=true)
```

## Patterns

### コンテキスト圧縮時の記憶退避

会話が長くなりコンテキストが圧縮される前に、重要な情報を退避する:

```
remember(
  content="本セッションの要点: 1) GER-RAGのMCPサーバーを実装した 2) ベンチマーク全項目PASS 3) 重力変位でnDCG+15%改善",
  source="compaction",
  context="2026-03-28のセッション要約"
)
```

### 作業開始時の文脈復元

新しいセッションの冒頭で、前回の文脈を復元する:

```
recall(query="前回のセッションで何をしたか", source_filter=["compaction", "agent"])
```

### 判断の記録

重要な設計判断をしたとき、理由とともに記録する:

```
remember(
  content="SQLiteからPostgreSQLへの移行は現時点では不要と判断。理由: 100K文書でレイテンシ20ms達成済み",
  source="agent",
  tags=["design-decision", "database"]
)
```

### トラブルシューティングの記録

エラーや失敗の経験は非常に価値が高い。原因と解決策をセットで記録する:

```
remember(
  content="numpy配列にPythonのor演算子を使うとValueError。原因: 配列のbool変換が曖昧。解決: if x is not None で明示的に分岐する",
  source="agent",
  tags=["troubleshooting", "python", "numpy"]
)
```

```
remember(
  content="Plotly 6.xではscatter3d.marker.line.widthにリストを渡せない。スカラーのみ。色のRGBA alphaで個別ノードの視覚的差異を表現する方式に変更して解決",
  source="agent",
  tags=["troubleshooting", "plotly", "visualization"]
)
```

次に似たエラーに遭遇したとき:

```
recall(query="numpy 配列 ValueError", source_filter=["agent"])
recall(query="Plotly scatter3d エラー", source_filter=["agent"])
```

過去の失敗経験が重力で浮上し、同じ轍を踏まずに済む。

### ユーザーの好みや制約の記録

ユーザーの明示的・暗黙的な好みを記録しておくと、次のセッションで自然に反映できる:

```
remember(content="pip禁止。Python環境はuvを使用する", source="user", tags=["preference", "tooling"])
remember(content="ドキュメントは日本語で書く", source="user", tags=["preference", "language"])
remember(content="宇宙テーマのUI/可視化が好き", source="user", tags=["preference", "design"])
```

### 創発的なアイデア探索

行き詰まったときや、異分野のつながりを探したいとき:

```
explore(query="この問題に使えそうな過去の経験", diversity=0.8)
```

トラブルシューティングの記憶と設計判断の記憶が、exploreの重力によって意外な形でつながることがある。

## Notes

- 重複するcontentは自動スキップされる（SHA-256ハッシュ）
- 記憶はセッションをまたいで永続化される
- `recall`するたびに重力が蓄積され、関連する記憶が互いに近づく
- `source_filter`を使うと、自分の過去の判断だけ、ユーザーの指示だけ、等の絞り込みが可能
