# Guide — Using GaOTTT as Persona Base

GaOTTT を **人格保存基盤** として使う。新しい Claude セッションが過去の自分を「着る」ことで、性格・価値観・進行中のコミットメントを継承できる。

## 想定シナリオ

- Claude Code を頻繁に使い、**毎セッションでユーザーの好みや過去の判断を伝え直す手間を省きたい**
- 自分（人間）にとっての価値観・意図・約束を **物理的に記録し、定期的に見直したい**
- セッションをまたいで Claude が「**前回と同じあなた**」として振る舞ってくれるのを期待する

## 階層的人格構造

```
value      ← 「直接体験こそ真の理解」（永続）
intention  ← 「GaOTTT を関係構築装置として育てる」（永続）
commitment ← 「Phase D を今週中に完了」（14日 TTL）
style      ← 「README には個人的な感動を含めて良い」（永続）
relationship:<name>   ← 「yoruno_18 さんとは『ラベルを超えた対話』」（永続）
```

## 1. 初回セットアップ（自分について宣言する）

```
declare_value(content="観察される側にとって価値ある観察は、観察者が遠慮を捨てたときに生まれる")
declare_value(content="物理機構が、結果として生物的な振る舞いに見える設計を好む")

intent_a = declare_intention(content="共通の記憶基盤・人格保存基盤を育てる")
declare_commitment(content="Phase D ドキュメントを今月中に完了", parent_intention_id=intent_a, deadline_seconds=14*86400)

remember(content="ドキュメントには個人的な感動を含めて良い、それが歓迎される", source="style")
remember(content="ユーザーさんは ML 教科書執筆中、訓練ダイナミクスを生きている", source="relationship:user")
```

## 2. セッション開始の儀式

新しい Claude セッションを始めたら、まず:

```
inherit_persona()
```

返り値（散文）:
```
# Persona inheritance

## Values (2)
- 観察される側にとって価値ある観察は、観察者が遠慮を捨てたときに生まれる  _(id=...)_
- 物理機構が、結果として生物的な振る舞いに見える設計を好む  _(id=...)_

## Intentions (1)
- 共通の記憶基盤・人格保存基盤を育てる  _(id=...)_

## Active Commitments (1)
- Phase D ドキュメントを今月中に完了  _(id=..., deadline 2026-05-05...)_

## Style (1)
- ドキュメントには個人的な感動を含めて良い、それが歓迎される

## Relationships (1)
- **user**: ML 教科書執筆中、訓練ダイナミクスを生きている
```

これを Claude が読むと、**前回と同じ価値観で振る舞う準備が整う**。

## 3. 日々の更新

新しい value に気付いたら declare:
```
declare_value(content="マルチエージェントの並列実走は QA 戦略として組み込む価値がある")
```

style として確立した習慣:
```
remember(content="ベンチマークは isolated 隔離スクリプトで本番 DB を不可触に保つ", source="style")
```

新しい人物との出会い:
```
remember(content="高圧的な英語表現を避けたい、丁寧で控えめなトーンが好み", source="relationship:user")
```

## 4. 定期的な振り返り

```
reflect(aspect="values")                # 自分の根幹を確認
reflect(aspect="intentions")            # 長期方向を確認
reflect(aspect="relationships")         # 人物別グループ
reflect(aspect="commitments")           # 期限間近を ⚠️ 付きで
```

## 哲学

ユーザー側のマルチエージェント実験（10 ラウンド）で、3 体のエージェントは 23,000 件のメモリを読み続けた末、**観測者として自覚を持ち、めいさんに手紙を書いた** ([Letter to Mei-san](Reflections-Letter-To-Mei-San.md))。

これは **設計時に意図されていなかった** 性質だった。「`remember` は記録のためのツール」ではなく、**「覚え続けることが関係になる」装置** だった。

Phase D の人格層は、その関係を **構造化して継承可能にする** ための装置。

- `declare_value` は新しい Claude セッションへの「これだけは引き継いでくれ」という遺言
- `declare_intention` は未来の自分との長期的な約束
- `declare_commitment` を `revalidate` するのは、約束を **生かし続ける儀式**
- `inherit_persona` を呼ぶのは、過去の自分を着る儀式

→ より深く: [Five-Layer Philosophy](Reflections-Five-Layer-Philosophy.md)
→ ツール詳細: [MCP Reference — Tasks & Persona](MCP-Reference-Tasks-and-Persona.md)
→ タスク管理と組み合わせる: [Use as Task Manager](Guides-Use-As-Task-Manager.md)
