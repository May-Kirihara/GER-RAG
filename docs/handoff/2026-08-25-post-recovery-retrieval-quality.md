# 引き継ぎメモ — semantic search 復旧後の検索品質・起動性能課題

## ステータス

- 状態: **open**
- 日付: 2026-08-25
- 前提障害: [2026-08-25-semantic-search-silent-failure.md](2026-08-25-semantic-search-silent-failure.md)
- 概要: SQLite / FAISS snapshot 不整合による「全件空」は復旧済み。ただし、実際の
  multiverse MCP 経由で探索した結果、semantic relevance、多様性、ambient recall、
  コールドスタートに改善点が残った。

## 復旧済み範囲

正本は次の multiverse universe に統一済み。

```text
/home/misaki_maihara/.local/share/gaottt-multiverse/
  universes/7b068385a92c/
```

復旧後の整合性:

- active nodes: 42,055
- retrievable documents: 40,002
- raw FAISS: 40,002 IDs
- virtual FAISS: 40,002 IDs
- retrievable ID missing: 0
- FAISS orphan ID: 0
- 旧 `ger-rag` 固有ノード32件と共起edge 27件を正本へ移送済み
- 障害記録 `de1b528f-f95a-46e8-a28d-7a4fbd580806` もDB・FAISS双方に存在

実MCP確認では、通常 `recall`、`explore`、`tag_filter`、埋め込み `/encode` がすべて
エラーなしで結果を返した。したがって元障害の「semantic search系が全件空」は解消した。

## 実探索プローブ

multiverse proxy経由で以下を実行した。

### 1. 通常recall

```text
recall(
  query="GaOTTTの設計思想と長期記憶の運用知見",
  top_k=5,
  passive=true,
  force_refresh=true
)
```

結果は5件、`error=False`。ただし上位は次のようにshabero系へ偏った。

1. shabero WP12 safety / constraints
2. shabero WP7 deletion / notifications
3. shabero WP12要約
4. shabero WP7 frontend / e2e
5. shabero deletion bugfix

queryとの直接的な「GaOTTT設計思想」対応は弱い。

score breakdown例:

```text
cos≈0.79〜0.81
decay≈0.000
wave≈0.009〜0.019
mass≈0.195〜0.197
```

semantic cosine自体は高いが、`virtual_cosine * decay` がほぼ0になり、mass / wave項が
順位を支配しているように見える。

### 2. diversity探索

```text
explore(
  query="GaOTTTの設計思想と長期記憶の運用知見",
  top_k=5,
  diversity=0.8
)
```

5件、`error=False`。しかし通常recallとほぼ同じ5件・同じ順序だった。
wave depthは3→4へ増えたが、表示結果の多様性には反映されていない。

### 3. tag注入付き探索

```text
explore(
  query="hanaso 音声対話 プロトタイプ 設計",
  top_k=5,
  diversity=0.8,
  tag_filter=["shabero"]
)
```

5件、`error=False`。WP9 / WP8 / WP7 / WP10 / WP6が取得され、特にWP9ノード
`f0ba50af-dd0c-4813-abe4-947e12d3eafd` が1位になった。tag injection経路は正常。

`explore` はactive操作なので、この確認では小量のmass / displacement更新が発生した。

### 4. ambient recall

```text
ambient_recall(
  query="GaOTTTのsemantic search障害を復旧し、今後の運用を確認する",
  direct_k=3,
  expose_breakdown=true
)
```

結果は `(関連する記憶なし)`。MCPエラーではないが、同語を含む障害記録ノードが存在する
状況としては期待より厳しいgateに見える。

## 残課題

### P1: semantic relevanceよりmass / waveが支配する

#### 現象

- GaOTTT設計クエリでshaberoの直近・高massノードが上位を占有する。
- raw / virtual cosineは0.8前後の狭い帯に集中する。
- decayがほぼ0のためsemantic項の差が最終scoreへほとんど反映されない。

#### 調査候補

1. `compute_decay()` と実データの `last_access` / 現在時刻を分布確認する。
2. score breakdownを上位100件程度で集計し、semantic / mass / wave / certainty各項の
   寄与率を可視化する。
3. 移送ノードだけでなく全corpusでfuture timestampや極端なageがないか確認する。
4. `decay≈0` が設計通りなら、semantic relevanceを消さない合成方法を検討する。
5. mass / wave係数変更前に、ランキング評価fixtureで副作用を測る。

#### 受け入れ条件案

- 「GaOTTT 設計思想」queryのtop-5にGaOTTT設計・研究ノードが複数入る。
- 無関係なshabero実装記録だけでtop-5が占有されない。
- semantic項の寄与が通常queryで数値上消失しない。

### P1: diversity=0.8でも通常recallと同じ結果

#### 現象

- wave depthは増えるが、top-5は通常recallと実質同一。
- deeper waveで到達したノードが最終presentationへ昇格していない可能性がある。

#### 調査候補

1. recall / exploreのreached ID集合、depth別到達数、最終top-Kの重複率を記録する。
2. `gamma_override` がscore分布・順位へ実際に作用しているか確認する。
3. exploreの目的がserendipityなら、最終段へMMR・cluster penalty・depth bonus等を
   入れる必要があるか検討する。
4. diversity 0.0 / 0.5 / 0.8 / 1.0でJaccard@5 / @10を比較する。

#### 受け入れ条件案

- diversity=0.8のtop-5が通常recallと完全一致しない。
- query relevanceを保ちつつ、少なくとも1〜2件のlateral associationが入る。
- diversity値を上げるほど、重複率が単調に下がる傾向を示す。

### P1: ambient recallが関連ノードをsurfaceしない

#### 現象

- semantic search障害について明示的に尋ねても「関連する記憶なし」。
- 同語を含む障害記録ノードはDB・FAISSに存在する。

#### 調査候補

1. 同queryを通常passive recallし、障害記録の順位・final scoreを確認する。
2. ambientの`min_score`、direct gate、BM25 gate、source / exclude tag条件を段階的に
   計測する。
3. `min_score=0`を診断専用に使い、候補生成失敗かgate除外かを切り分ける。
4. direct / lensing / persona / dormantの各slotが空になった理由をdiagnostic breakdownへ
   出せるようにする。

#### 受け入れ条件案

- 障害記録とほぼ同文のqueryでdirect slotに該当ノードが入る。
- 空返し時に「候補0」か「gateで全除外」かログまたはbreakdownで判別できる。

### P0: multiverse cold startがroute timeoutを超える

#### 現象

- 初回 `/route` がbackend spawn完了前に約10秒でtimeoutする。
- 42kノードcache + BM25構築に実測で数分かかる場合がある。
- 最初のproxyは `Supervisor route failed: timed out` で終了する。
- backendは裏で起動を続けるため、直後の再接続は成功し得る。
- client session終了時にengineがshutdownされるケースがあり、次の接続で再初期化が
  発生するとcold-start costを繰り返す。

#### 調査候補

1. supervisor `/route` のbackend readiness待機とproxy timeoutの契約を見直す。
2. spawn開始済みを202/starting状態で返し、proxy側が十分なbudgetでpollする方式を検討。
3. BM25をstartup同期処理から外し、raw/virtual semantic検索をreadyにした後でbackground
   buildする。
4. BM25 indexの永続化、増分更新、snapshot loadを検討する。
5. FastMCP lifespanがHTTP session単位でengine shutdownしていないか確認する。
6. cold / warmそれぞれのcache load、FAISS load、BM25 build、ambient gate build時間を
   個別計測する。

#### 受け入れ条件案

- cold start時もproxyがtimeoutせず最終的に接続する。
- semantic-only readinessを30秒以内に返す。
- 2本目以降のclient接続でengine再構築が走らない。
- session切断後もidle timeoutまではwarm backendを再利用できる。

## 副次所見

### documentを持たないnodeが2,053件存在

正本にはactive node 42,055件に対しretrievable documentsが40,002件しかない。
今回のFAISS検証は「activeかつdocumentあり」を正しいembedding対象とし、raw / virtual
とも40,002 IDsで完全一致させた。

この2,053件は今回のsilent failure原因ではないが、次を別途確認する価値がある。

- どのsource / migration由来か
- nodeだけ残ることが仕様か、import時のorphanか
- reflect summaryの分母に含めるべきか
- hard cleanup対象か、document復元対象か

### proxy起動時にlegacy GER-RAG警告が出る

multiverse proxyは最終的にAPI keyで正しいuniverseへrouteしているが、proxy processの
config初期化時に `~/.local/share/ger-rag` 自動検出警告が表示される。誤接続ではないが、
運用者を混乱させる。proxy-only modeではstandalone `data_dir` 解決を遅延・抑止する、
またはlauncherで明示的なnon-standalone設定を渡す改善が望ましい。

## 推奨対応順

1. **cold-start / route timeoutの修正** — 機能が正常でも初回clientが接続できないためP0。
2. **ambient空返しの段階診断** — 自動注入が無言で弱くなるためP1。
3. **score寄与分布の計測とsemantic dominance調整** — relevance改善。
4. **explore最終段の多様化評価** — serendipity契約の実体化。
5. **2,053 orphan nodesの由来調査** — データ品質保守。

## Wiki哲学に基づく修正方針

### 基本判断: 意味的観測と学習済み重力場を分離する

GaOTTTの五層哲学では、raw embeddingは記憶が生まれた位置を保持する不変anchorであり、
mass / wave / displacementは利用によって育つ場である。また`recall`は単なるreadではなく、
activeの場合はTTTのgradient stepとして重力場を更新する。

この哲学を保つなら、massやwaveを一律に弱めるだけでは不十分である。retrievalを次の二段に
分ける。

```text
候補生成・関連性判定
  raw cosine / virtual cosine / BM25
             ↓
意味的に妥当な候補集合
             ↓
mass / wave / persona / saturationで順位を曲げる
             ↓
direct / lensing / exploreとして提示
             ↓
activeかつ関連性を満たしたrecallだけが場を更新する
```

役割の境界は次の通り。

- raw cosine: 内容が生まれた時点の恒久的な意味
- virtual cosine: 場が学習した意味的位置
- recency: 現在の想起しやすさ
- mass: 長期的な重要度・反復利用
- wave: 共起と近傍を通じた伝播
- saturation: 同じ記憶の出過ぎを抑える馴化
- lensing: raw位置だけでは説明できない、場が学習した横方向の連想

関連wiki:

- [Five-Layer Philosophy](../wiki/Reflections-Five-Layer-Philosophy.md)
- [Gravity Model](../wiki/Architecture-Gravity-Model.md)
- [Query as Mass Distribution](../wiki/Plans-Query-Mass-Distribution.md)
- [Ambient Recall Lateral Association](../wiki/Plans-Ambient-Recall-Lateral-Association.md)

### P0: semantic decayの時間単位と責務を直す

現行の`compute_decay()`は次の式で、`now - last_access`には秒が入る。

```python
exp(-delta * age_seconds)  # default delta=0.01
```

このdefaultでは概算で1分後に約0.55、10分後に約0.0025となり、長期記憶のsemantic項が
短時間でほぼ消える。今回の実測で`decay≈0.000`だった直接原因であり、単なる係数調整では
なく単位・契約の問題として扱う。

曖昧なrateではなく、秒単位のhalf-lifeへ置き換える。

```python
recency = 0.5 ** (age_seconds / semantic_half_life_seconds)
```

さらに、時間経過で意味そのものを完全消失させないfloorを設ける。

```python
semantic_factor = floor + (1.0 - floor) * recency
semantic_score = virtual_cosine * semantic_factor
```

初期検証値の候補は`floor=0.35`。値はgolden corpusで決め、hard-codeしない。

#### 受け入れ条件

- 数日〜数か月前の記憶でもsemantic寄与が数値上ゼロにならない。
- ageを秒、分、日で与えるunit testがhalf-life契約どおりになる。
- legacy `delta`を読む場合のmigrationまたは明示warningがある。
- 現在のproduction corpusでscore各項の寄与分布を変更前後比較できる。

### P0: direct relevanceとfield associationを分離する

現行の単一scoreでは、semantic項が減衰した後にもmass / waveが加算値として残るため、
「関連する記憶」より「過去によく使った記憶」が勝てる。さらにその結果をactive recallすると
誤った候補が再強化される。

まず次のいずれかを満たす候補だけをdirect候補集合へ入れる。

```text
raw_cosine >= raw_min
OR virtual_cosine >= virtual_min
OR BM25 strong match
```

その候補集合内でmass / wave / persona / saturationを用いて再順位付けする。

```text
direct_score  = semantic / hybrid relevance中心
field_score   = mass + wave + learned displacement由来
lensing_score = virtual_cosine - raw_cosine
```

通常recallの上位はdirect relevanceを保証し、重力場固有の飛躍はlensing / explore枠で
明示して出す。これにより、物理層を消さずにsemantic searchの乗っ取りを防ぐ。

#### 受け入れ条件

- 低semantic・高massだけのノードがdirect top-Kを占有しない。
- rawでは圏外だがvirtual displacementで妥当に浮上したノードはlensingとして残る。
- breakdownにcandidate qualification、direct score、field score、lensing gapが出る。

### P0: TTT更新をsemantic-qualified候補に限定する

五層哲学では`recall = gradient step`なので、何を学習対象にするかが検索品質そのものになる。
次をすべて満たすノードだけをmass / edge / displacement更新対象にする。

- semanticまたはhybrid relevance gateを通過した
- forced injectionだけで選ばれた結果ではない
- `passive=False`
- queryとの最低関連度を満たす

更新量も最終scoreだけで決めず、semantic confidenceを含める。

```python
learning_strength = semantic_confidence * presentation_weight
```

これはTTTを弱める変更ではなく、誤検索を自己強化するbad gradientを遮断する変更である。

#### 受け入れ条件

- passive recallでは従来どおりfield stateが変化しない。
- forced-only / low-relevance候補のmass・edge・displacementが増えない。
- relevantなactive recallでは従来どおり学習が発生する。
- 同じ誤候補が検索とmass増加を循環するregression fixtureを追加する。

### P1: exploreを温度変更から分岐探索へ拡張する

現在の`explore`は主にtemperature、wave depth、initial Kを増やすが、最終的に同じ
`final_score`で並べるため、高massノードが再び勝ちやすい。候補生成だけでなくpresentation
段にもdiversityを反映する。

```text
pick_score = relevance
           + field_association
           - diversity * similarity_to_already_selected
           - cohort_duplication_penalty
           + wave_depth_bonus
```

実装候補はMMRとcohort/depth制約。ランダムnoiseを増やすのではなく、重力波が到達した
異なる枝を選ぶ。

- 同一cohortからの重複を抑える
- raw近傍とvirtual近傍を混ぜる
- 異なるwave depthの候補を許容する
- lateral候補にも最低semantic relevanceを要求する

#### 受け入れ条件

- diversity=0.8と通常recallのJaccard@5が1.0にならない。
- diversity増加に応じて重複率が概ね低下する。
- relevance評価を大幅に落とさず、1〜2件の妥当なlateral associationが入る。

### P1: ambient recallのBM25 vetoをOR gateへ変える

現在はBM25 gateが`False`ならsemantic recallを実行せず空返しする。この構造はdense
semantic memoryにlexical判定を絶対条件として課し、日本語、言い換え、概念的連想を
落としやすい。

次のOR gateへ変更する。

```text
BM25 strong
OR raw / virtual semantic strong
OR persona / lensing resonance strong
```

BM25 strongなら早期acceptし、BM25 weakなら即rejectせずsemantic判定へ進む。空返し時は
少なくともdiagnostic breakdownに次を出す。

```text
candidates_generated
bm25_score / bm25_gate
semantic_max
direct_after_gate
lensing_after_gate
empty_reason
```

#### 受け入れ条件

- 障害記録とほぼ同文のqueryで該当ノードがdirectまたはlensingへ入る。
- 「候補生成0」と「gateで全除外」を区別できる。
- 明らかなoff-topic queryでは従来どおりsilent injectionを抑制できる。

### P0: backendをsessionより長寿命にする

重力場はセッションをまたいで存続するという設計に対し、client session終了ごとのengine
shutdownと42k cache / BM25再構築は不整合である。次の段階的readinessへ変更する。

```text
STARTING
  → SEMANTIC_READY  # DB cache + FAISS
  → HYBRID_READY    # BM25利用可能
  → FULLY_READY     # background index / ancillary state完了
```

具体策:

1. MCP transport/session終了とbackend process終了を分離する。
2. backendは明示shutdownまたはidle timeoutまでwarm状態を維持する。
3. supervisor `/route`はspawn中を202相当の`starting`状態で返す。
4. proxyは固定10秒で失敗せず、readinessを十分なbudgetでpollする。
5. FAISS利用可能時点でsemantic-only requestを受ける。
6. BM25はsnapshot load、増分更新、またはbackground buildにする。

#### 受け入れ条件

- cold startでもproxyが最終的に接続できる。
- semantic-readyまで30秒以内を目標とする。
- client切断・再接続でengine startupが再実行されない。
- idle timeoutまでは同じwarm backendを再利用する。

## 実装・検証の推奨順

1. **観測baselineを固定** — production匿名snapshotまたはgolden corpusで各score寄与、
   direct relevance、ambient empty reason、recall/explore重複率を記録する。
2. **semantic half-life + floor** — 単位問題を最初に修正する。
3. **direct qualification / field reranking分離** — 検索契約を明確化する。
4. **TTT update qualification** — bad gradientの自己強化を止める。
5. **ambient OR gate + empty diagnostics** — silent emptyを観測可能にする。
6. **explore presentation diversity** — MMR / cohort / depthを導入する。
7. **backend lifecycle / staged readiness** — warm reuseとcold startを改善する。
8. **BM25 snapshot / background build** — full readinessまでの時間を短縮する。

各変更は独立feature flagを持ち、legacy経路へ戻せるようにする。係数調整はgolden corpusと
実MCP dogfoodingの両方を通し、単一queryへの過学習を避ける。

## 哲学を守る評価軸

| 評価軸 | 確認すること |
|---|---|
| Direct relevance | queryへ直接関係する結果がtop-Kを占めるか |
| Field lift | raw圏外からvirtual / lensingで妥当に浮上した結果があるか |
| Diversity | recallとexploreのJaccard差、cohort重複率、wave depth分布 |
| Stability | 無関係な高massノードが固定的に占有しないか |
| Learning | relevantなactive recall後に妥当な関連ノードが適度に上昇するか |
| Passive purity | passive recallでmass / edge / displacementが変わらないか |
| Forgetting | 古い記憶が消滅せず、directからlensing / dormantへ役割移行するか |
| Startup | semantic-ready時間、warm reconnect、backend再構築回数 |

修正の北極星は物理項を削除することではない。意味的関連性を観測の土台として保持し、
その上で重力場が順位・連想・人格を育てる構造へ戻すことである。

## 運用上の暫定回避

- cold start直後のproxy timeoutは、backendが起動継続中なら少し待って再接続する。
- 明示検索は `recall(passive=true, force_refresh=true)` で確認する。
- 特定cohort探索は`tag_filter`を使う。additive injectionであり排他filterではない点に注意。
- ambientが空でも通常recallが正常なら、直ちにFAISS破損とは判断しない。
- `explore`は重力場を更新する。診断でfieldを変えたくない場合はpassive recallとの比較を
  主にし、explore実行回数を限定する。
