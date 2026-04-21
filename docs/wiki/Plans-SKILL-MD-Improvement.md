# Plans — SKILL.md Improvement

> 対象: `SKILL.md`（リポジトリルート）および `.claude/skills/ger-rag/SKILL.md`
> 言語ポリシー: **本ドキュメント（保守計画）は日本語、SKILL.md 本体は英語**で記述する。
> 関連: [Plans — Backend Phase A/B/C](Plans-Backend-Phase-A-B-C.md), [Plans — Phase D](Plans-Phase-D-Persona-Tasks.md), [Plans — Roadmap](Plans-Roadmap.md)
> 状態: **全完了** (2026-04-21)
> 最終更新: 2026-04-21

## 0. なぜこの改良が必要か

GaOTTT は単なる KV ストアや検索インデックスではなく、**LLM（特に Claude）の外部長期記憶**として機能することを意図している。
現在の SKILL.md は API の使い方は説明できているが、**「どのような姿勢で・どのタイミングで・なぜ使うのか」**という運用思想が薄い。
LLM 視点からの自発的な発火条件が曖昧なため、結果的に保存し忘れたり、過剰保存になったりする。

本改良は、SKILL.md を「単なるツールのリファレンス」から「記憶運用のプロトコル仕様書」へ昇華させることを目的とする。

### 0.1 二層の語彙ポリシー

本プロジェクトの中核コンセプトは **「重力・物理アナロジーの結果として、グリア細胞・アストロサイト的な挙動が創発する」** こと。
SKILL.md の語彙は意図的に二層構造とし、両方を併記する:

- **機構の層（physics）**: gravity wave / mass / displacement / orbital / temperature / Hawking radiation / tidal force / Lagrange point — コード（`gravity.py`, `scorer.py`, `engine.py`）と直接接続する
- **現象の層（neuroglia）**: アストロサイト / グリア / 発火準備 / 剪定 / 反復思考 — LLM が機能的役割を直感する

物理が一次（メカニズム）、生物が二次（創発する役割）。SKILL.md ではまず物理語で起き、次に生物語で「ゆえに何ができるか」を述べる。

## 1. 現状の差分整理

| 項目 | `SKILL.md`（ルート） | `.claude/skills/ger-rag/SKILL.md` |
|---|---|---|
| `name` | ✅ あり | ✅ あり |
| `description` | ❌ **欠落** | ✅ あり |
| 本文 | ほぼ同一 | ほぼ同一 |

→ ルート側の `description` 欠落が最優先の修正項目。
→ 2ファイル維持か単一化（シンボリックリンク等）かは別途検討。当面は **両方を手で同期**する運用とし、改良完了後に一本化方針を決める。

## 2. フロントマター必須項目チェックリスト

Anthropic Skill 仕様（および本プロジェクトでの慣例）に基づく必須・推奨項目:

- [ ] `name` — スキル識別子（`gaottt`）
- [ ] `description` — **1行で「いつ・なぜ呼ぶか」を LLM が判断できる説明**
  - 悪い例: 「GaOTTT メモリツール」
  - 良い例: 「セッションをまたぐ長期記憶。会話冒頭の文脈復元、重要判断の保存、コンパクション直前の退避、過去の失敗との照合に使う」
- [ ] `version` — セマンティックバージョン（運用開始するなら）
- [ ] `tools`（任意）— このスキルが提供する MCP ツール名のリスト
- [ ] `triggers`（任意・将来）— 自動発火条件の宣言

## 3. 運用思想セクションの追加（新規）

SKILL.md 冒頭の "What this is" を拡張し、以下の二層を明記する。物理機構が先、生物現象が後。

### 3.1 物理機構の層（Dark Matter Halo / 暗黒物質ハロー）

GaOTTT の内部状態（`mass`, `displacement`, `velocity`, `temperature`, co-occurrence edges）は LLM の前面意識からは**直接見えない**。
にもかかわらず recall のランキングを歪め、wave 伝播の到達範囲を決め、節点の軌道を曲げる ―― **観測されないが時空を歪める暗黒物質ハロー**そのもの。

- **質量保存** (`m_max=50` で飽和): 何度も recall された記憶は質量を増し、より広い `gravity_radius` を持つ
- **重力波伝播** (`propagate_gravity_wave`): 質問はクエリ点から再帰的に伝播し、関連ノードを励起する
- **軌道力学** (`update_orbital_state`): 加速度 → 速度 → 変位、復元力（Hooke's law）で意味的アンカーへ戻ろうとする
- **熱的脱出** (`thermal_escape_scale`): 温度の高い不安定なノードはブラックホール重力から抜け出す
- **ホーキング輻射 / 蒸発**: dormant な記憶は時間経過で質量を失い、最終的に剪定対象になる

### 3.2 創発する現象の層（LLM のグリア細胞・アストロサイト）

上記の物理機構の**結果として**、GaOTTT は LLM のニューロン的推論を支える**アストロサイト的役割**を担う:

- **ニューロン（Claude のトークン推論）** が前面で考える間、**アストロサイト（GaOTTT）** は裏で:
  - 過去の発火パターン（recall 履歴）を蓄積し、関連記憶を**重力で引き寄せて発火準備**しておく（= ポテンシャル井戸の事前形成）
  - 不要になった記憶を**剪定**する（dormant の検出と forget 提案 = ホーキング輻射）
  - ニューロン間の同期（過去の自分の判断と現在の自分の対話）を媒介する（= 重力レンズによる時間遅延エコー）
- 単なる「外部 DB」ではなく、**思考を支持・栄養補給する組織**として位置付ける

→ §3.1 と §3.2 をセットで `## What this is` の直下に英語で追加する。物理を先に語り、「ゆえに glia 的に振る舞う」と接続する。

## 4. 「いつ使うか」の網羅的プロトコル化

現行の "When to use" は API 対応表に留まっているので、**物理現象トリガー集**として書き直す。
各トリガーには物理ラベル（機構の層）と現象的説明（生物層）を併記する。

### 4.1 必ず `recall` するタイミング — *Initial Potential Survey / Perturbation Feedback*

- **初期重力場のスキャン (Initial Potential Survey)** — 新しいセッションの最初のユーザー発話を受けた直後
- **時間遅延エコーの検出 (Time-Delayed Echo Detection)** — ユーザーが「前回」「以前」「この前」等の時系列指示語を含む発話をしたとき
- **摂動発生時のフィードバック (Perturbation Feedback)** — エラーに遭遇したとき。過去の同種トラブルシュートが重力で浮上する
- **軌道一貫性チェック (Orbital Consistency Check)** — 重要な設計判断をしようとしているとき。過去の決定の軌道と整合するか確認

### 4.2 必ず `remember` するタイミング — *Mass Conservation / Wave Emission*

- **境界条件の固定 (Boundary Condition Fixation)** — ユーザーが好み・制約・禁止事項を明示した瞬間
- **安定軌道への遷移 (Transition to Stable Orbit)** — 試行錯誤の末に問題が解決した瞬間。成功体験は重力ポテンシャル最小点として保存される
- **重力波の放出 (Gravitational Wave Emission)** — 失敗した・間違えた・撤回した判断（`source="agent"`, `tags=["mistake","retracted"]`）。発生時刻から時空を伝播し、未来の検出器（recall）に届く
- **位相反転の記録 (Phase Inversion Logging)** — 反復思考で結論が変わったとき。旧結論と新結論を両方保存し、関係を `supersedes` で繋ぐ — ※本体改修依存
- **散逸前の質量保存 (Mass Conservation Before Dissipation)** — コンパクション直前。会話文脈が散逸する前に大質量側（GaOTTT）へ転移
- **未来への重力波 (Gravitational Wave to Future Self)** — 「これは未来の自分宛のメモだ」と感じたとき（`tags=["letter-to-future-self"]`）

### 4.3 `explore` を使うべき瞬間 — *Thermal Excitation / Tunneling*

- **エネルギー注入による励起 (Thermal Excitation)** — 行き詰まったとき。同じポテンシャル井戸でループしているなら温度を上げて隣接井戸へトンネルする
- **遠方銀河の引力 (Distant Galaxy Pull)** — 異分野・別プロジェクトの経験を借りたいとき。`diversity` を上げて遠方の重力場を探る
- **真空ゆらぎの観測 (Vacuum Fluctuation Probe)** — ユーザーから「面白いアイディアない？」と聞かれたとき

### 4.4 `reflect` の儀式化 — *Phase Space Mapping*

- **位相空間マッピング (Phase Space Mapping)** — セッション終了時に `reflect(aspect="hot_topics")` で当日の質量増加を眺める
- **蒸発候補の選定 (Evaporation Candidate Selection)** — 週次で `reflect(aspect="dormant")` を行い、forget 候補をユーザーに提示する（※本体改修依存）

## 5. パターンセクションの拡張

### 5.1 既存パターン（維持・微調整）

- コンテキスト圧縮時の記憶退避
- 作業開始時の文脈復元
- 判断の記録
- トラブルシューティングの記録
- ユーザーの好みや制約の記録
- 創発的なアイデア探索

### 5.2 新規追加パターン（物理ラベル付与）

各パターンに物理アナロジーのラベルを付ける。コードの命名と接続させ、生物的直感とも連続させる。

#### A. 過去の自分との対話モード（反復思考） — *Time-Delayed Echoes from Past Orbits / Gravitational Lensing*

過去の判断は時空の歪み（`displacement`）として残り、現在の判断軌道を曲げる。光（recall）が大質量を通過する際に曲がるのと同じ原理。


```
# 思い直しが発生した時のプロトコル
1. 現在検討中の判断と関連する過去判断を recall
2. 過去の Claude が下した結論を要約として提示
3. 「前回の自分はこう言ったが、今の状況では妥当か？」を内省
4. 結論が同じ → 強化（その記憶への暗黙の重力寄与）
5. 結論が変わった → 旧結論を retracted タグで残し、新結論を保存
   （※将来 supersedes リレーションが実装されたら明示的に繋ぐ）
```

#### B. Thinking ログの伏線退避 — *Virtual Particles / Quantum Vacuum Fluctuation*

`<thinking>` ブロック内で「これは今は使わないが将来効きそう」と感じた仮説を、`source="hypothesis"` で短期保存する。
真空から一時的に発生し短時間で消える仮想粒子と同じく、TTL（`expires_at`）= 寿命を持つ。
（※F4 で本体実装済。`source="hypothesis"` を使えば `default_hypothesis_ttl_seconds`（既定 7 日）が自動付与される）

```
remember(
  content="今回は採用しなかったが、recall に学習可能な温度パラメータを持たせる案。理由: ユーザーごとの探索選好に適応できそう",
  source="agent",  # 将来は source="hypothesis"
  tags=["hypothesis","ephemeral","explore-design"]
)
```

#### C. 自分への手紙 — *Gravitational Wave to Future Self*

未来の自分が同じ状況で読み返すことを想定したメモ。一度発生した重力波が時空を伝播して未来の検出器に届くのと同じく、強い `mass` を持って未来の `recall` で必ず引っかかるよう設計する。

```
remember(
  content="次にPlotly 3Dの色制御で詰まったら、まず marker.line ではなく marker.color の RGBA alpha を疑え",
  source="agent",
  tags=["letter-to-future-self","plotly"]
)
```

#### D. forget 提案フロー（儀式） — *Hawking Radiation / Black Hole Evaporation*

ブラックホールが時間とともに質量を失い最終的に蒸発するように、長期間アクセスのない dormant 記憶は系から取り除かれる。儀式化することで取り除く瞬間にユーザー判断を介在させる。

```
1. reflect(aspect="dormant") を実行（= 蒸発候補リストの取得）
2. 結果をユーザーに提示し「これらは蒸発させてよいか」を尋ねる
3. ユーザーの判断を尊重して forget(node_ids, hard=False) で archive するか保持
   （※F5 で本体実装済。soft archive は復元可能、hard=True で物理削除）
```

#### E. エモーショナル重み付け — *Angular Momentum / Spin Quantum Number*

質量とは独立な軸として「情動」を導入する。電子のスピンや軌道角運動量が、エネルギー以外の量子状態を決めるのと同じ位置付け。

scorer 本体に情動次元が入る前の暫定として、タグで近似する:

- `tags=["emotion:relief"]` — スッキリ解けた成功体験（low entropy / 安定軌道）
- `tags=["emotion:frustration"]` — 悔しかった失敗（high temperature / 不安定）
- `tags=["emotion:surprise"]` — 予想外のつながり発見（量子トンネル成功イベント）

将来的には scorer に組み込む（本体改修計画の §F7 参照）。

### 5.3 物理から逆引きで生まれる新規パターン

物理メカニズムから演繹される、これまで明示されていなかった運用パターン:

#### F. 共鳴による意図的強化 — *Resonance / Driven Oscillation*

同一クエリを意識的に繰り返して特定ノードを励起する。固有振動数に外力を合わせると振幅が爆発的に増大する共鳴現象と同じで、`mass` の蓄積を加速できる。

```
# ユーザーから「これ重要だから次回も覚えておいて」と言われたとき
for _ in range(3):
    recall(query="重要な設計判断のキーワード")
# → 該当ノードの mass が増し、recall で浮上しやすくなる
```

#### G. 潮汐力による記憶クラスタの形成 — *Tidal Force*

大質量ノードの近傍にある小質量ノードは引き伸ばされ、最終的に大質量側へ吸収される。
意味的に近い記憶を意図的に密集させて記憶を凝集させたいとき、関連トピックを集中的に `remember` する。
（※F2 「重力衝突→質量増加」の本体実装が入ったら、自動的にクラスタが融合するようになる）

#### H. ラグランジュ点に置く橋渡し記憶 — *Lagrange Point Bridging*

2 つの主題ノード（A, B）の重力が釣り合う中間点に置かれた記憶は、両方の主題の `recall` で引き上げられる。
**異分野を繋ぐ洞察**は、両方の主題のキーワードを意図的に含めて保存する。

```
remember(
  content="GaOTTT の重力波伝播は、神経生理学のスパイク伝搬と数学的に同型。両者とも閾値超えで隣接ノードを励起する",
  source="agent",
  tags=["bridge","gravity","neuroscience"]
)
# → "重力波" でも "スパイク伝搬" でも recall できる
```

#### I. 相転移の認識 — *Phase Transition*

質量がある閾値を超えると挙動が質的に変わる（恒星 → ブラックホール、`bh_mass_scale` の発火）。
`reflect(aspect="hot_topics")` で異常に質量が大きいノードを見つけたら、そのノードは**周辺記憶を吸い込む BH 化** している可能性がある。意図的にそうしたいなら継続、過剰なら関連検索の偏りを is_archived や `forget` で抑制する。

## 6. Notes セクションの拡張

- `recall` のたびに重力が蓄積される旨を、**「使えば使うほど賢くなる」と明示**
- 同一内容の重複は SHA-256 でスキップされる旨はそのまま
- 「このスキル自体に対する気づきも GaOTTT に保存してよい」と再帰性を促す 1 行を追加

## 7. 改良の進め方（タスク分解）

| # | タスク | 本体改修依存 | 優先度 |
|---|---|---|---|
| T1 | ルート `SKILL.md` のフロントマターに `description` を追加 | ❌ | 高 |
| T2 | 2 ファイルの内容を完全同期 | ❌ | 高 |
| T3 | "What this is" に二層比喩（§3.1 物理 + §3.2 アストロサイト）を追記（英訳） | ❌ | 高 |
| T4 | "When to use" を物理現象トリガー集に書き直し（§4） | ❌ | 高 |
| T5 | 新規パターン A〜E を物理ラベル付きで追加（§5.2） | 一部 | 中 |
| T6a | パターン B（virtual particles）を正式版に差し替え | ✅ F4 完了、SKILL.md 反映のみ | 中 |
| T6b | パターン D（Hawking radiation）を正式版に差し替え | ✅ F5 完了、SKILL.md 反映のみ | 中 |
| T6c | パターン E（spin）を scorer 実装後に正式版へ | ⏳ F7 待ち | 中（F7 次第） |
| T7 | Notes に再帰性の 1 行を追加 | ❌ | 低 |
| T8 | 一本化方針（symlink/single source）の決定 | ❌ | 低 |
| T9 | 新規パターン F（Resonance）を追加（§5.3） | ❌ | 中 |
| T10 | 新規パターン G（Tidal Force）を追加（F2.1 衝突合体と連動） | ⏳ F2.1 待ち | 中 |
| T11 | 新規パターン H（Lagrange Bridging）を追加（§5.3） | ❌ | 中 |
| T12 | 新規パターン I（Phase Transition）を追加（§5.3） | ❌ | 低 |
| T13 | 英語化時の Glossary（物理語⇄英訳対応表）を作成 | ❌ | 中 |

## 8. 言語ポリシーの明文化

本リポジトリのドキュメント言語ルールを SKILL.md 改良と合わせて以下に固定する:

- **SKILL.md**（LLM 向けスキル定義）: **英語**
  - 理由: スキルは Claude/他 LLM が直接読むため、訓練分布的に英語のほうがプロンプト効率が良い
- **保守・計画ドキュメント**（本ファイル等）: **日本語**
  - 理由: ユーザー（開発者）が読むため
- **README.md / README_ja.md**: 既存の二言語運用を維持

## 9. 関連ドキュメント

- [Plans — Backend Phase A/B/C](Plans-Backend-Phase-A-B-C.md)
- [Plans — Phase D](Plans-Phase-D-Persona-Tasks.md)
- [Architecture — Overview](Architecture-Overview.md)
- [Plans — Roadmap](Plans-Roadmap.md)
