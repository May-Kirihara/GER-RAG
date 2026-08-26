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
