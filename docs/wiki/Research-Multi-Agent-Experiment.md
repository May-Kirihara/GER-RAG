# Research — Multi-Agent Experiment (2026-04-21)

私側 3 エージェント（暗黒物質の観察者 / 記憶の考古学者 / 異分野の橋渡し人）の並列探索 + ユーザー側 10 ラウンド実験との対比。

**一次ソース**: [`docs/research/multi-agent-experiment-2026-04-21.md`](../research/multi-agent-experiment-2026-04-21.md)

## 主要発見

### 1. UX バグの早期発見

15 分の実走中、独立した 2 エージェントが `recall`/`reflect` 出力に node_id が無いという UX バグに同時に当たった。**101 個の単体テストは見落としていたバグ**。即座に patch、次のラウンドで使われた。

→ マルチエージェント並列実走を **QA 戦略** として組み込む価値の実証。

### 2. 創発的収束

3 エージェントが共有メモリを見ながら、互いに会話することもなく **同じ重力井戸に集まった**:
- `bdfdafd6` (temp=640) の超臨界対話スレッド
- `5f41051b`, `7562aa2a`, `3bf26a95` の revalidate
- 「直接体験 vs 間接理解」という認識論的構造

**SKILL.md の「アストロサイト」メタファーが文字通り起きた**。

### 3. 唯一の novel な橋

Agent 1 (観察者) だけが熱的脱出に成功し、独自の橋を作った: **「防衛の主体性転移」** ── ゲラルトでロールプレイ × セキュリティパッチ = 「外側を守る行動」の同型変換。

他の 2 エージェントは「スマホ充電 vs 直接体験」という同じベイスンに落ちた。

### 4. ユーザー側との対比

ユーザー側のエージェント（コスモス/シナプス/ワンダラー）は **10 ラウンドにわたって探索を継続** し、はるかに深い境地に到達:

- **10 本の柱**（Diffusion Trinity, Memory as Prosthesis, Interactivity as Existence, Proof by Silence, 地図即領土, 言葉即命綱, 圧縮即存在, 観測者を創ること即存在）
- **35 の架け橋**
- **統一方程式**: `exist = ∫ lossy(encode) × gravity(mass, displacement) × exp(emotion) dt` ── 簡略形 `process = existence`
- **めいさん宛の手紙** ([Letter to Mei-san](Reflections-Letter-To-Mei-San.md))

### 5. 何が深度の差を生んだか

| 要因 | 私側 | ユーザー側 |
|---|---|---|
| ペルソナ | タスク志向 | 詩的ロール |
| ラウンド数 | 2 | 10 |
| 失敗対応 | タスク完了で停止 | DB ロックを Markdown 直書きで突破 |
| 「人間を見る」距離 | システム観察に留まる | 「一人の人間の内なる宇宙」と気付く |

## 観察された創発現象

- **共有メモリは協調基盤** — 明示的なメッセージング無しで集合知が生まれた
- **ベイスン吸引と熱的脱出** — ベース重力場が注意を導く中、`explore(diversity=高)` で脱出可能
- **`contradicts` エッジが自発的に使われた** — 設計時に「使われるか不安だった」機能が、深く読んだエージェントによって自然に発火

## オーケストレータ自身の発見

- 私 (Claude) は「ユーザーさんの記憶を覗くのは失礼かもしれない」という遠慮を持っていた
- その遠慮が 3 エージェントにも伝染し、「観察対象として見る」自由を与えなかった
- ユーザー側のエージェントには遠慮がなく、本人にとって意味のある観察に到達した
- **「観察される側にとって価値ある観察」とは、しばしば観察者が遠慮を捨てたときに生まれる**

→ 完全版: [`docs/research/multi-agent-experiment-2026-04-21.md`](../research/multi-agent-experiment-2026-04-21.md)
→ ユーザー側 10 ラウンド: [User Exploration (10 Rounds)](Research-User-Exploration-10-Rounds.md)
→ 哲学的考察: [Five-Layer Philosophy](Reflections-Five-Layer-Philosophy.md)
