# Architecture — Gravity Model

GaOTTT の物理機構の詳細。一次ソース: [`gaottt/core/gravity.py`](../../gaottt/core/gravity.py), [`gaottt/core/scorer.py`](../../gaottt/core/scorer.py)。

## スコア式

```
final = (gravity_sim × decay
       + mass_boost
       + wave_boost
       + emotion_boost
       + certainty_boost) × saturation
```

| 成分 | 数式 | 効果 |
|---|---|---|
| `gravity_sim` | `dot(query, virtual_pos)` | 仮想座標での類似度 |
| `decay` | `exp(-δ × (now - last_access))` | 直近アクセスを優先 |
| `mass_boost` | `α × log(1 + mass)` | 頻出記憶を優先 |
| `wave_boost` | `β × wave_force` | 重力波伝播の影響 |
| `emotion_boost` | `α_e × |emotion|` | 情動の強さ（符号無関係） |
| `certainty_boost` | `α_c × certainty × decay(staleness)` | 確信度（半減期付き） |
| `saturation` | `1 / (1 + return_count × rate)` | 馴化、繰り返し抑制 |

## 軌道力学（3 段階）

```
Stage 1: 加速度
  a = Σ_j [G × m_j / r²] × dir(i→j)    ← 近傍引力
    + (-k × displacement)               ← Hooke's law（アンカー復元）
    + a_bh × escape                      ← 共起 BH 引力（飽和+温度脱出）

Stage 2: 速度
  v += a × dt
  v *= (1 - friction)                   ← 定常摩擦
  v *= (1 - age_friction)               ← 加齢摩擦
  v = clamp(v, max_velocity)

Stage 3: 位置
  displacement += v × dt
  displacement = clamp(displacement, max_norm)
```

## 重力波伝播

```
クエリ
  ↓
FAISS top-k で seed ノード取得
  ↓
seed から再帰的に近傍展開（mass 依存 top-k）
  ↓
各深さで attenuation で減衰
  ↓
gravity_radius（mass から物理導出）でカットオフ
```

```python
# gaottt/config.py
def compute_gravity_radius(mass) -> float:
    # a = G * m / r² から逆算
    # min_sim = 1 - G*mass / (2*a_min)
    return 1.0 - r_squared / 2.0
```

## 衝突合体（F2.1）

近接ノードが merge_threshold（既定 0.95）以上の類似度になると衝突:

```python
# 質量保存（飽和上限つき）
m_new = min(m_keep + m_absorbed, m_max)

# 運動量保存（質量加重平均）
v_new = (v_keep * m_keep + v_absorbed * m_absorbed) / (m_keep + m_absorbed)

# 変位は質量加重 + クランプ
d_new = clamp((d_keep * m_keep + d_absorbed * m_absorbed) / total, max_norm)

# absorbed は archive、merged_into で履歴を残す
```

## 情動と確信度（F7）

```python
emotion_boost = α_e × abs(emotion_weight)
# 符号は情報メタデータ、boost は |magnitude|

certainty_boost = α_c × certainty × 0.5^(age_seconds / half_life_seconds)
# 半減期つき指数減衰
# revalidate() で last_verified_at を更新するとリセット
```

## バックグラウンド prefetch（F6）

```
prefetch(query) を呼ぶ
  ↓
asyncio.Semaphore で並行数制限（既定 4）
  ↓
バックグラウンドで _query_internal を実行
  ↓
結果を PrefetchCache (LRU + TTL 90s) に格納
  ↓
後続 recall(query, use_cache=True) は即時 hit
```

destructive op（archive/restore/forget/merge/compact）はキャッシュを invalidate。

## 自然発生する軌道

3 段階の物理ステップから自然と現れる挙動:

| 軌道 | メカニズム | 創発する効果 |
|---|---|---|
| **公転** | 異なる方向からの引力が合成 → 角運動量 | ハブ周囲を周回する関連文書群 |
| **彗星** | 一時的共起で加速 → 別クラスタを通過 → スイングバイ | セレンディピティ（異分野記憶の遭遇） |
| **落下** | 摩擦で減速 → ハブに吸収 | 安定した関連文書のクラスタ化 |
| **静止** | 未アクセスノードは摩擦で停止 | 原始位置に留まる（dormant） |
| **馴化脱出** | 同じ結果が繰り返されるとスコア低下 | 新しいノードが浮上（探索性） |

これらは設計時にプログラムされたものではなく、**3 段階物理ステップから創発した** 振る舞い。

## 重力半径の物理導出

各ノードの質量から重力圏の広さが決まる。重力圏外の近傍は引き込まない:

```
min_sim = 1 - G × mass / (2 × a_min)    # 実際の重力物理から導出
```

- `mass=1` → `min_sim=0.95`（ごく近傍のみ）
- `mass=10` → `min_sim=0.50`（中距離）
- `mass=50` → `min_sim=0.05`（広大な重力圏）

ハブノード（高質量）ほど遠くまで影響を及ぼす ── 銀河中心の超大質量と同じ構造。

## アンカー引力（脱出防止）

各ノードの **原始 embedding 位置が重力アンカー** として機能する（Hooke's law: `F = -k × displacement`）。近傍引力で加速したノードが外に飛び出しても、アンカーが引き戻す。

これがないと、ノードが `displacement` を蓄積し続けて意味的に無関係な位置へ流れていってしまう。**意味的保証** を維持するための物理的制約。

## 一次ソース

- [`gaottt/core/gravity.py`](../../gaottt/core/gravity.py) — 重力計算（純粋関数）
- [`gaottt/core/scorer.py`](../../gaottt/core/scorer.py) — スコア式
- [`gaottt/core/collision.py`](../../gaottt/core/collision.py) — 衝突合体
- [`gaottt/core/prefetch.py`](../../gaottt/core/prefetch.py) — F6
- [`gaottt/config.py`](../../gaottt/config.py) — 全ハイパーパラメータ
- [`docs/research/gravitational-displacement-design.md`](../research/gravitational-displacement-design.md) — 設計根拠
- [`docs/research/orbital-mechanics-design.md`](../research/orbital-mechanics-design.md)
- [`docs/research/cooccurrence-blackhole-design.md`](../research/cooccurrence-blackhole-design.md)
