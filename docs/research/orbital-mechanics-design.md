# Orbital Mechanics 設計書

**日付**: 2026-04-06 (設計) → 2026-04-07 (実装完了)
**ステータス**: 実装済み
**前提**: [Gravity Wave Propagation](gravity-wave-propagation-design.md) の重力モデルを拡張

## 1. 背景

### 現在のモデルの限界

現在の重力変位モデルは「力が直接座標を変える」瞬間移動型であり、**慣性がない**。

```
現在: F → displacement += η * F * direction
         力がなくなると停止。記憶のない移動。
```

実際の物理では、力は加速度を生み、加速度は速度を変え、速度が位置を変える：

```
物理: F → a = F/m → v += a*dt → x += v*dt
         力がなくなっても速度で動き続ける。慣性がある。
```

### 速度ベクトル導入で生まれる現象

| 現象 | 意味 | GER-RAGでの効果 |
|------|------|----------------|
| **慣性** | 力がなくなっても動き続ける | 一度引き寄せられた文書が、関心が移っても惰性で近傍に留まる |
| **公転** | 中心天体の周囲を回る | 頻出知識（ハブ）の周囲を関連文書が周回、検索のたびに少し違う面が見える |
| **彗星軌道** | 遠方から高速接近→通過→離脱 | 一時的に強く共起した文書が加速、別クラスタに飛んで新しい架橋を作る |
| **スイングバイ** | 大質量天体の近傍を通過し方向転換 | 高massハブを経由して、元のクラスタとは無関係な領域に射出される |
| **摩擦減速** | 媒質の抵抗で徐々に停止 | 長期間アクセスされない文書が減速し、最終的に元の位置に定着 |

## 2. 物理モデル

### 2.1 ノードの拡張状態

```
NodeState:
  id:            str          # ドキュメントID
  mass:          float        # 質量（従来通り）
  temperature:   float        # 温度（従来通り）
  last_access:   float        # 最終アクセス（従来通り）
  sim_history:   list[float]  # 類似度履歴（従来通り）

StorageState（永続化層）:
  displacement:  float[768]   # 位置変位（従来通り）
  velocity:      float[768]   # 【新規】速度ベクトル
```

### 2.2 運動方程式

各タイムステップ（= 各クエリ）で、以下の3段階を順に適用する。

#### Stage 1: 加速度の計算

各ノード i に対し、共起ペア j からの重力加速度を合算する。

```
a_i = Σ_j [ G * m_j / (r_ij² + ε) * direction(i→j) ]
```

- `G`: 万有引力定数（既存パラメータ）
- `m_j`: ノードjの質量（ノードiの質量は慣性として分母に入る — 後述）
- `r_ij`: ノードi,j間の仮想座標上の距離
- `direction(i→j)`: i から j への単位ベクトル

注: 古典力学では `a = F/m = G * m_j / r²` で、自身の質量は加速度に影響しない（等価原理）。GER-RAGでもこれに従い、**質量は引力の強さ（能動側）を決めるが、加速のされやすさ（受動側）は全ノード同一**とする。

#### Stage 2: 速度の更新

```
v_i += a_i * dt
v_i *= (1 - friction)    # 摩擦項による減速
v_i = clamp(v_i, max_velocity)  # 速度上限
```

- `dt`: タイムステップ幅（1.0固定、各クエリ=1ステップ）
- `friction`: 速度の減衰率（0に近いほど慣性が強い）
- `max_velocity`: 速度ベクトルのL2ノルム上限（暴走防止）

#### Stage 3: 位置の更新

```
displacement_i += v_i * dt
displacement_i = clamp(displacement_i, max_displacement_norm)  # 位置上限（既存）
```

### 2.3 摩擦モデル

摩擦は2種類：

```
# 定常摩擦: 毎ステップ一定の割合で減速
v *= (1 - friction)

# アクセス間隔ベース摩擦: 長期間未アクセスのノードはさらに減速
age = now - last_access
age_friction = friction_age_factor * (1 - exp(-age_delta * age))
v *= (1 - age_friction)
```

- 継続的にアクセスされるノード → 摩擦が弱い → 速い軌道を維持
- 放置されたノード → 摩擦が強い → 減速 → 最終的に停止して元の位置方向へ

### 2.4 軌道の分類

速度ベクトルの方向と大きさ、および中心天体との位置関係で、自然に以下の軌道が形成される。

| 軌道タイプ | 条件 | GER-RAGでの意味 |
|-----------|------|----------------|
| **束縛軌道 (公転)** | v < 脱出速度, 角運動量 ≠ 0 | ハブの周囲を安定周回する関連文書 |
| **落下 (捕獲)** | v ≈ 0, ハブに向かう | 新しくハブに引き込まれた文書、やがてハブの一部に |
| **双曲線軌道 (彗星)** | v > 脱出速度 | 一時的に接近して通過、別の領域へ飛んでいく |
| **静止** | v ≈ 0, 孤立 | アクセスされない文書、原始位置に留まる |

脱出速度は質量と距離から自動的に決まる（明示的な計算は不要、物理が自然に分類する）。

## 3. 検索フローへの統合

### Phase 3 (Wave + 二層分離) からの変更

```
Wave探索 → N ノード到達
  │
  ├─ シミュレーション層（全到達ノード）
  │    ├─ Stage 1: 加速度計算 (共起ペア間の重力)
  │    ├─ Stage 2: 速度更新 (v += a*dt, 摩擦適用)
  │    ├─ Stage 3: 位置更新 (displacement += v*dt, クランプ)
  │    ├─ mass/temperature 更新（従来通り）
  │    └─ 永続化（displacement + velocity）
  │
  └─ プレゼンテーション層（top-k=5 をLLMに返却）
```

変更点は Stage 1-3 のみ。Wave探索、スコアリング、二層分離はそのまま。

### 仮想座標の計算（変更なし）

```
virtual_pos = normalize(original_emb + displacement)
```

velocityは位置の計算には直接使わない（displacementを経由）。

## 4. パラメータ

### 新規パラメータ

| パラメータ | 意味 | 推奨値 | config.json |
|-----------|------|--------|------------|
| orbital_friction | 定常摩擦係数 | 0.05 | Yes |
| orbital_friction_age_factor | 未アクセス追加摩擦の強さ | 0.1 | Yes |
| orbital_max_velocity | 速度ベクトルのL2ノルム上限 | 0.05 | Yes |
| orbital_anchor_strength | 原始位置への復元力（Hooke定数） | 0.02 | Yes |

### 既存パラメータとの関係

| パラメータ | 従来の役割 | 軌道力学での役割 |
|-----------|----------|----------------|
| `gravity_G` | 力の計算 | 加速度の計算（同じ） |
| `gravity_eta` | displacement直接加算の学習率 | **廃止** — 速度→位置の物理に置換 |
| `displacement_decay` | 位置の定期減衰 | **摩擦に置換** — 速度の減衰で自然に位置が収束 |
| `max_displacement_norm` | 位置の上限 | 維持（安全弁） |

`gravity_eta` と `displacement_decay` は軌道力学導入後は不要になる。加速度→速度→位置の物理ステップが自然にこれらの役割を果たす。

## 5. ストレージ

### velocity の永続化

displacement と同様に BLOB として SQLite に保存。

```sql
ALTER TABLE nodes ADD COLUMN velocity BLOB;
-- NULL = 静止状態 (零ベクトル)
```

768次元 × float32 × 2（displacement + velocity）= 6KB/ノード。12,000ノードで ~72MB。

### キャッシュ

```python
class CacheLayer:
    velocity_cache: dict[str, np.ndarray]    # 【新規】
    dirty_velocities: set[str]               # 【新規】
```

## 6. gravity.py の変更

### 現在の関数

```python
# 廃止対象
compute_gravitational_force()    → 力を直接displacementに加算
update_displacements_for_cooccurrence()  → 全ペアのdisplacement更新

# 維持
compute_virtual_position()       → 変更なし
clamp_displacement()             → 変更なし
propagate_gravity_wave()         → 変更なし
apply_displacement_decay()       → 摩擦に置換（後方互換のため残す）
```

### 新規関数

```python
def compute_acceleration(
    pos_i: np.ndarray,
    positions_j: list[np.ndarray],
    masses_j: list[float],
    config: GERConfig,
) -> np.ndarray:
    """Compute gravitational acceleration on node i from all neighbors j.
    
    a_i = Σ_j [ G * m_j / (r_ij² + ε) * direction(i→j) ]
    """
    ...

def update_velocity(
    velocity: np.ndarray,
    acceleration: np.ndarray,
    mass: float,
    last_access: float,
    now: float,
    config: GERConfig,
) -> np.ndarray:
    """Update velocity: v += a*dt, apply friction, clamp.
    
    Two friction sources:
    - Constant: v *= (1 - friction)
    - Age-based: older nodes get more friction
    """
    ...

def update_orbital_state(
    node_ids: list[str],
    original_embeddings: dict[str, np.ndarray],
    displacements: dict[str, np.ndarray],
    velocities: dict[str, np.ndarray],
    masses: dict[str, float],
    config: GERConfig,
    now: float,
    last_accesses: dict[str, float],
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Full orbital mechanics step for all nodes.
    
    Returns: (updated_displacements, updated_velocities)
    
    Stage 1: Compute accelerations from all pairs
    Stage 2: Update velocities (+ friction)
    Stage 3: Update displacements (+ clamp)
    """
    ...
```

## 7. 公転の発生メカニズム

実際にコードで公転を強制する必要はない。物理が正しければ**自然に発生する**。

```
初期状態:
  ドキュメントA (mass=10, ハブ)
  ドキュメントB (mass=1, 初めてAと共起)

Step 1: BがAに引き寄せられる → 加速度がA方向に発生
Step 2: Bが速度を得てA方向に移動
Step 3: 次のクエリでBはAの近くにいるが、別のクエリCとも共起
        → Cからの引力が横方向に加わる
Step 4: A方向の速度 + C方向の加速度 = 斜め方向の合成速度
        → Aの周囲を回り始める（角運動量の発生）
Step 5: 摩擦が弱ければ、公転が維持される
        摩擦が強ければ、徐々にAに落下して吸収される
```

### friction パラメータの効果

| friction | 効果 | 天文アナロジー |
|----------|------|--------------|
| 0.01 | 慣性が強い、長期間公転が維持される | 真空に近い宇宙空間 |
| 0.05 | 適度な減衰、数十クエリで軌道が変化 | 推奨値 |
| 0.2 | 急速に減速、ほぼ直線落下 | 大気圏内 |
| 1.0 | 即座に停止 | 現在の「慣性なし」モデルと同等 |

## 8. 彗星軌道のシナリオ

```
1. ユーザーが「AI」と「料理」を同じセッションで検索
2. 「AIの効率的な学習方法」ドキュメントが「AI」クラスタから加速
3. 「料理」クラスタの高massハブ「レシピの効率化」に引き寄せられる
4. スイングバイ: 「AIの効率的な学習方法」が「料理」ハブの近傍を高速通過
5. 射出: ハブの反対側に飛び出し、「生活の効率化」クラスタに接近
6. 次回「生活の効率化」を検索すると、AI文書が予想外に浮上する
   → セレンディピティの発生
```

## 9. 可視化での表現

`visualize_3d.py` で速度ベクトルを矢印として描画できる。

```
ノード: ● (サイズ=mass, 色=temperature)
速度:   → (矢印の長さ=速度の大きさ, 方向=速度の向き)
軌跡:   --- (過去N回のdisplacement履歴を線で結ぶ)
```

これにより、ドキュメントが「どの方向に動いているか」が一目でわかる。公転中のノードは円弧状の軌跡を描き、彗星ノードは長い直線の尾を引く。

## 10. アンカー引力（脱出防止機構）

### 問題

近傍ノードからの引力で加速したノードが外方向の速度を持つと、そのまま宇宙の外に飛んでいく。`max_displacement_norm` による位置クランプは安全弁だが、ノードが境界に張り付く不自然な挙動になる。

### 解決: Hooke's law による原始位置への復元力

各ノードの原始embedding位置が重力アンカー（ブラックホール）として機能する。

```
a_anchor = -k * displacement
```

加速度の計算に統合:
```
a_total = a_neighbors + a_anchor
        = Σ [G * m_j / r²] * direction(i→j)  -  k * displacement_i
```

| anchor_strength | 効果 | 天文アナロジー |
|----------------|------|--------------|
| 0.0 | 復元力なし（脱出可能） | 空虚な宇宙 |
| 0.02 | 穏やかな復元（推奨値） | 銀河の暗黒物質ハロー |
| 0.1 | 強い復元（ほぼ原始位置に留まる） | 巨大ブラックホール |

### 物理的性質

- **原始位置にいるノード**: アンカー力 = 0（邪魔しない）
- **離れたノード**: displacement に比例して引き戻される
- **摩擦と合わせると**: 減衰振動（スパイラルイン）→ 原始位置近傍で安定
- **近傍引力とのバランス**: 公転は維持されるが脱出は不可能

## 11. 実装ファイル

| ファイル | 変更内容 | ステータス |
|---------|---------|----------|
| `config.py` | orbital_* パラメータ4つ + anchor_strength、config.json読み込み | 実装済み |
| `core/gravity.py` | compute_acceleration() (アンカー引力統合), update_velocity(), update_orbital_state() | 実装済み |
| `core/engine.py` | _update_simulation() → 3段階物理ステップ + 二層分離 | 実装済み |
| `store/base.py` | save_velocities(), load_velocities() | 実装済み |
| `store/sqlite_store.py` | velocity BLOB永続化 + 自動マイグレーション | 実装済み |
| `store/cache.py` | velocity_cache, dirty_velocities | 実装済み |
| `config.json.example` | orbital_* + anchor_strength パラメータ | 実装済み |
| `scripts/visualize_3d.py` | 速度ベクトル矢印、重力圏リング、velocity読み込み | 実装済み |

## 12. 可視化 (実装済み)

`visualize_3d.py` で以下を表示:

| 要素 | 表現 | 条件 |
|------|------|------|
| 恒星 | ● サイズ=mass, 色=temperature | 全ノード |
| 速度ベクトル | シアン矢印（1ステップの実移動量） | velocity > 0.001 |
| 重力圏 | 金色リング（XY+XZ面）| mass > 2.0 |
| フィラメント | 薄い線 | 共起エッジ |
| ホバー情報 | mass, temp, disp, vel, gravity radius | 全ノード |
