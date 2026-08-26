# Phase U WP-3 — ambient composite gate 較正記録 (2026-08-26)

- 対象: production universe `7b068385a92c` の copy (`sqlite3 .backup` + FAISS/virtual/manifest sidecar、962M、/tmp/opencode/gaottt-prod-calib.*)
- embedder: 本番 embedder service (127.0.0.1:7879) を共用 (read-only encode)
- script: `scripts/calibrate_ambient_gate.py` (seed 42, 層化 50/50 split)
- 生 log: `/tmp/opencode/calib-v2-full.log` (v2 probe set) + v1 run (script 同梱 seed)

## 結論

**事前登録 gate (FP=0 ∧ FN≤10%) は plan §4 WP-3 の単一 semantic arm 式では達成不能。R3 は `"or"` default のまま user に escalate** (決定構造の変更が必要なため閾値調整の範囲を超える)。

## 較正データ (v2 probe set: positives 16 / negatives 14、per-probe 全値は生 log 参照)

| 集団 | virt_top1 範囲 | bm25 範囲 |
|---|---|---|
| positives | 0.8194-0.9092 | 19.1-96.2 (9/16 は ≥32 = bm25_strong で自力 accept) |
| negatives (absent-content) | 0.7855-0.8480 | 13.5-30.8 (全員 <32) |

- **重なり帯**: virt1 0.819-0.848。review 必須 probe の incident query は virt1 0.8466 で negative 最高値 (競走馬 0.8480) に **-0.0014 で負ける** → 純 virt 閾値では incident と FP が不可分。
- しかし incident は bm25 26.98 / margin 0.0527 / raw 0.8814 で、**軸ごとの強みが異なる**: 競走馬は bm25 15.3 / raw 0.8479 → 複数軸の組み合わせなら分離可能。
- ペンギン query (review 必須): virt1 0.8144 / bm25 17.8 / raw 0.8243 — negative band の中央で、広い閾値域で reject 可能 ✓。
- 言い換え positives 3 件: virt1 0.8509-0.8800 — negative band より上 ✓ (bm25 弱でも semantic で accept 可能)。

## v1 からの測定妥当性修正 (開示)

v1 probe set の negative を「GaOTTT 无関の日常 query」にしたところ、**本人の健康・生活会話記憶に実際に関連する content を取得する query が FP 扱いになっていた** (例: 「睡眠の質を上げる生活習慣」が健康 chat を取得)。個人記憶 corpus では off-topic = 「**記憶に存在しない話題**」と再操作化し v2 を作成。これは threshold fishing ではなく構成概念の定義修正 (review 自身の acceptance が penguin query = absent content で定義しているため)。

## 探索的解析 (post-hoc — 昇格根拠にはしない)

bm25_strong OR (virt1 ≥ ~0.850) OR (bm25 ≥ ~22 ∧ virt1 ≥ ~0.845) の 3-arm 構造は v2 全 probe で FN=1/16 (6.25%: owner-lease query のみ) / FP=0/14 を達成。ただし:
- 決定構造の追加は plan の式変更であり、事前登録の趣旨 (選定と検証の分離) を守るには **新規 probe set での再検証** が必要
- virt1 の分布は displacement field の drift で日々動く (incident は review 時 0.856 → 本日 0.8466)。0.0014 差のような閾値は脆弱

## 残された選択肢 (user 判断)

1. **3-arm 構造を plan 改訂の上で再事前登録 → 新規 probe set で再検証 → PASS なら composite 昇格** (推奨。もう 1-2 較正 round)
2. **`"or"` 維持** (P0 のペンギン false positive は残るが、incident/運用系は現状通り pass。review の probe 5 は未達として報告)
3. LLM 側 (hook caller) での relevance check 等、別設計 (将来)

いずれの場合も composite 実装・較正基盤・fail-closed は本番に乗っており、`ambient_gate_mode=composite` への切替は config 一つ (artifact は `--emit-artifact` で生成済み手順)。

---

# v3 round (2026-08-26 深夜、user 指示 ① により実施)

- plan §10 で 3-arm 構造 (bm25_strong OR virt≥virt_hi OR (bm25≥bm25_mid ∧ virt≥virt_mid)) を事前登録 → R3-impl で実装 (旧 percentile/margin/raw_floor arm は廃止)
- probe set v3: **新規 16 positive / 14 negative** (v2 と非重複。`ambient-probes-v3.json`)
- **run 1 (VOID)**: 全 probe `bm25= n/a` — WP-6c の background build 化により較正 script が BM25 構築完了前の空 window で計測していた (測定器欠陥。`wait_for_bm25_ready` を両 script に追加して修理。probe/grid/gate は不変)
- **run 2 (有効評価): 事前登録 gate FAIL** — held-out **FP=14.3%** (1/7) / FN=0%。grid は FP_cal=0 を達成する最緩角 (0.83/16/0.83) を推奷したが、held-out の「中世の写本に使われた顔料の作り方」が突破。

## 分離不能性の分析 (run 2 実測軸)

| 集団 | bm25_top 範囲 | virt_top1 範囲 |
|---|---|---|
| positives (16) | 14.5-34.3 | 0.8315-0.8874 |
| negatives (14) | 12.6-22.1 | 0.8013-0.8794 |

- **「中世の写本の顔料」(neg): bm25=20.09 / virt=0.8794** — 「復元したデータベースで検索が壊れた」(pos: bm25=20.05 / virt=0.8411) と bm25 軸が **0.04 で交錯**、virt は positive 帯中央より上。arm2 を 0.8794 超えに上げると今度は「夢ループとは何か」(pos: virt 0.8825) との間の **0.003 幅の窓** しか残らない (v2 で警告した脆弱閾値病理)。
- 結論: **本 corpus (個人記憶 + 技術/創作混合) では、文化・工芸に隣接する absent topic が bm25/virt 両軸で positive 領域内に現れる**。3-arm でも 2 軸特徴空間の分離限界。v2 で 3-arm が分離したのは negative が偶然遠い話題だったため (選択運)。

## 判定 (plan §10 事前登録に従う)

**FAIL → `ambient_gate_mode="or"` 維持を確定、R3 は Phase U の既知制約として close。threshold fishing 不実施。**

- composite mode (3-arm) は opt-in 実装として残存 (将来の再挑戦用機構。default `"or"` 不変)。
- 再挑戦に必要なのは閾値ではなく**別の判別軸** (例: LLM relevance 判断、corpus topic model による既知トピック集合との一致) — 別 phase の課題。
- 暫定 default (virt_hi=0.85 / bm25_mid=22.0 / virt_mid=0.845) は**未検証**である旨を Tuning に明記。
