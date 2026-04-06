# Habituation & Thermal Escape 設計書

**日付**: 2026-04-07 (設計・実装完了)
**ステータス**: 実装済み
**前提**: [Co-occurrence Black Hole](cooccurrence-blackhole-design.md) のBHモデル上に構築

## 1. 背景

### 正のフィードバックループ問題

共起BHモデルでは、同じクエリパターンが繰り返されるとBH質量が増加し続ける。

```
クエリ → 同じノードがヒット → 共起エッジ強化 → BH質量↑ → さらに引き込み
  ↑                                                              │
  └──────────────── 正のフィードバック ←──────────────────────────┘
```

これは勾配降下法の局所最適解に相当する。知識空間が硬直化し、創発性が失われる。

### 生物学的アナロジー: 馴化（Habituation）

脳は同じ刺激の繰り返しに対して反応が弱まる（馴化）。これにより注意が新しい刺激に向けられ、環境の変化に適応できる。

GER-RAGにも同じメカニズムを導入する。

## 2. 返却飽和（Presentation Saturation）

### 2.1 コンセプト

LLMに同じ文書を繰り返し返すほど、その文書の影響力が減衰する。

```
初回返却: saturation = 1.0 (フルパワー)
5回返却:  saturation = 0.5 (半減)
20回返却: saturation = 0.2 (ほぼ飽和)
```

### 2.2 物理モデル

各ノードに `return_count`（LLMに返却された回数）を追加する。

```
saturation(node) = 1.0 / (1.0 + return_count * saturation_rate)
```

飽和係数はBH質量とスコアリングの両方に影響する。

### 2.3 BH質量への影響

BHの加重重心を計算する際、各エッジの有効重みを飽和で減衰させる。

```
# 従来
effective_weight = raw_edge_weight

# 飽和適用後
effective_weight = raw_edge_weight * saturation(neighbor)
```

頻繁にLLMに返却されたノードはBHへの寄与が弱まる → BH質量が自然に減少 → クラスタの束縛が緩む → 新しいノードが入りやすくなる。

### 2.4 スコアリングへの影響

最終スコアにも飽和係数を適用。

```
# 従来
final = gravity_sim * decay + mass_boost + wave_boost

# 飽和適用後
final = (gravity_sim * decay + mass_boost + wave_boost) * saturation(node)
```

同じ文書を何度返しても飽和でスコアが下がり、まだ返していない文書が相対的に浮上する。

### 2.5 return_countの更新タイミング

- **プレゼンテーション層（top-k返却時）のみ**カウント
- シミュレーション層（wave到達だけ）ではカウントしない
- つまり「LLMが見た」回数だけが飽和に影響する

### 2.6 return_countの減衰

return_countは時間とともに減衰する（馴化からの回復 = 脱馴化）。

```
# 毎クエリステップで全ノードのreturn_countを減衰
return_count *= (1 - habituation_recovery_rate)
```

長期間返却されなかったノードは自然に新鮮さを取り戻す。

### 2.7 パラメータ

| パラメータ | 意味 | 推奨値 |
|-----------|------|--------|
| saturation_rate | 飽和の速さ（高い=早く飽和） | 0.2 |
| habituation_recovery_rate | 馴化からの回復速度 | 0.01 |

飽和曲線:

| return_count | saturation (rate=0.2) | 意味 |
|-------------|----------------------|------|
| 0 | 1.00 | 新鮮（未返却） |
| 1 | 0.83 | わずかに飽和 |
| 5 | 0.50 | 半減 |
| 10 | 0.33 | かなり飽和 |
| 20 | 0.20 | ほぼ飽和 |

## 3. 温度ベースBH脱出（Thermal Escape）

### 3.1 コンセプト

高temperatureのノードはBHの束縛を振り切る。低temperatureのノードはBHに捕まりやすい。

これは熱力学的に正しい — 高温のガスは重力井戸から脱出できる（恒星風、銀河からのガス流出）。

### 3.2 物理モデル

BH引力にtemperatureベースの遮蔽係数を適用する。

```
# 脱出係数: temperatureが高いほどBH引力が弱い
escape_factor = 1.0 / (1.0 + temperature * thermal_escape_scale)

# BH加速度に遮蔽適用
a_bh_effective = a_bh * escape_factor
```

### 3.3 temperatureとの関係

GER-RAGのtemperatureは `gamma * var(sim_history)` — 検索文脈の変動性を表す。

| 状態 | temperature | escape_factor | 効果 |
|------|-------------|--------------|------|
| 常に同じ文脈で検索 | 低い (~0) | ~1.0 | BHに束縛される（安定クラスタ） |
| 多様な文脈で検索 | 高い (~0.001) | ~0.5 | BHから半分脱出（探索的） |
| 非常に多様 | 高い (~0.01) | ~0.1 | BHをほぼ無視（自由飛行） |

### 3.4 天文学的アナロジー

```
低温のノード = 冷たいガス雲
  → 銀河の重力に束縛され、クラスタ内に留まる
  → 安定した知識構造の一部

高温のノード = ホットプラズマ / 恒星風
  → 銀河の重力を振り切って脱出
  → 異なるクラスタに到達し、架橋を形成
  → 創発的つながりの原動力
```

### 3.5 パラメータ

| パラメータ | 意味 | 推奨値 |
|-----------|------|--------|
| thermal_escape_scale | 温度による脱出効果のスケール | 5000.0 |

実際のtemperature値（~0.00002-0.00023）を考慮してスケーリング。

| temperature | escape_factor (scale=5000) |
|-------------|--------------------------|
| 0 (dormant) | 1.00 (完全束縛) |
| 0.00002 (median) | 0.91 (軽い脱出力) |
| 0.0001 (warm) | 0.67 (部分脱出) |
| 0.0005 (hot) | 0.29 (ほぼ脱出) |

## 4. 二つのメカニズムの組み合わせ

### 4.1 相互補完

| 状況 | 返却飽和 | 温度脱出 | 結果 |
|------|---------|---------|------|
| 同じ文書を何度も返却 | 飽和でスコア低下 | — | 新しい文書が浮上 |
| 多様な文脈で検索 | — | 高温でBH脱出 | クラスタ間を移動 |
| 両方 | 飽和 + 脱出 | 相乗効果 | 最大の探索圧力 |
| 新しいノード | 飽和なし | 温度なし | 通常通りBHに束縛 |

### 4.2 BH引力の最終計算

```
# 従来
a_bh = G * bh_mass / r² * direction

# 飽和 + 温度脱出を統合
bh_mass_effective = bh_mass * avg_saturation(neighbors)  # BH自体が弱まる
escape_factor = 1 / (1 + temperature * thermal_escape_scale)  # 高温ノードが脱出
a_bh = G * bh_mass_effective / r² * direction * escape_factor
```

### 4.3 スコアリングの最終計算

```
saturation = 1 / (1 + return_count * saturation_rate)
final = (gravity_sim * decay + mass_boost + wave_boost) * saturation
```

## 5. ストレージ

### return_countの保存

NodeStateに `return_count` フィールドを追加。

```
NodeState:
  ...
  return_count: float = 0.0   # LLMへの返却回数（減衰するのでfloat）
```

SQLite: 既存の nodes テーブルにカラム追加（自動マイグレーション）。

## 6. 実装計画

### Step 1: NodeState / Storage に return_count 追加
- types.py, sqlite_store.py, cache.py の更新
- 自動マイグレーション

### Step 2: engine.py の返却時にreturn_count更新
- プレゼンテーション層（top-k返却時）でのみインクリメント
- 毎ステップでhabituation_recovery_rateで減衰

### Step 3: スコアリングにsaturation適用
- engine.query() のfinal_score計算にsaturation係数

### Step 4: gravity.py のBH引力にsaturation + thermal escape適用
- compute_bh_acceleration() でedge weightにsaturation
- escape_factorでBH引力を遮蔽

### Step 5: config.py にパラメータ追加
- saturation_rate, habituation_recovery_rate, thermal_escape_scale

## 7. 創発性への期待効果

```
Phase 1: クエリ蓄積 → BH形成 → クラスタ構造化（銀河形成）
Phase 2: 同じクエリ繰り返し → 返却飽和 → 古いクラスタのスコア低下
Phase 3: 飽和によりBH引力が弱まる → 高温ノードがBHから脱出
Phase 4: 脱出したノードが新しいクラスタに合流 → 新しいBH形成
Phase 5: 知識構造が常に進化し、硬直化しない
```

これは脳の学習メカニズム（馴化→脱馴化→再学習）と同じサイクルであり、GER-RAGの創発性を長期的に維持する。
