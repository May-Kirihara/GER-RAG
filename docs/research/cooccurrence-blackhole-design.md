# Co-occurrence Black Hole 設計書

**日付**: 2026-04-07 (設計・実装完了)
**ステータス**: 実装済み
**前提**: [Orbital Mechanics](orbital-mechanics-design.md) の軌道力学モデル上に構築

## 1. 背景

### 現在の共起エッジの問題

共起エッジは記録・可視化されているが、**軌道力学に関与していない**。

```
現在の力の構成:
  a = a_neighbors (wave到達ペア間の引力)
    + a_anchor   (原始位置への復元力)

共起エッジの扱い:
  - update_cooccurrence() でカウント・重み記録
  - cache.graph_cache に保存
  - visualize_3d.py でフィラメント描画
  - compute_graph_boost() は存在するが呼ばれていない
  → 物理モデルに不参加
```

### 着想

共起エッジのクラスタ重心に**仮想的なブラックホール**（超大質量引力源）を配置する。頻繁に共起するノード群の中心に引力が生まれ、クラスタが重力的に束縛される。

これは銀河の形成メカニズムと同じ — 頻繁にinteractする星々の集団の重心に超大質量ブラックホールが存在し、銀河全体を束縛する。

## 2. 物理モデル

### 2.1 共起クラスタの重心ブラックホール

各ノード i に対し、共起ネイバーの加重重心を「ブラックホール」として計算する。

```
neighbors(i) = {(j, w_ij)} from co-occurrence graph

# 加重重心位置（BHの位置）
centroid_i = Σ_j (w_ij * virtual_pos_j) / Σ_j w_ij

# BHの質量（共起の強さの総和）
bh_mass_i = Σ_j w_ij * mass_scale_factor

# ノードiへのBH引力
a_bh = G * bh_mass_i / (distance(i, centroid)² + ε) * direction(i → centroid)
```

### 2.2 加速度への統合

```
a_total = a_neighbors     # wave到達ペア間の直接引力
        + a_anchor        # 原始位置への復元力 (Hooke)
        + a_bh            # 【新規】共起クラスタBHへの引力
```

3つの力のバランス:

| 力 | 方向 | 効果 |
|---|------|------|
| a_neighbors | 共起ノードに向かう | 直接的な引き寄せ（短期的） |
| a_anchor | 原始位置に向かう | 脱出防止（安定性） |
| a_bh | 共起クラスタ重心に向かう | **クラスタ構造の維持**（中期的） |

### 2.3 BH質量のスケーリング

共起edge weightの生の合計をそのまま使うとBH質量が大きくなりすぎる。スケーリングが必要。

```
raw_bh_mass = Σ_j w_ij
bh_mass = bh_mass_scale * log(1 + raw_bh_mass)
```

対数飽和により、共起が増えてもBH質量が際限なく増加することを防ぐ。

| raw_bh_mass (Σ weight) | bh_mass (scale=0.5) | 天文アナロジー |
|------------------------|---------------------|--------------|
| 5 | 0.90 | 小さなBH（疎なクラスタ） |
| 20 | 1.52 | 中規模BH |
| 100 | 2.31 | 大規模BH（密なクラスタ） |
| 1000 | 3.45 | 超大質量BH（巨大銀河中心） |

### 2.4 重心位置の計算

重心は共起ネイバーの**仮想座標**の加重平均。

```
centroid_i = Σ_j (w_ij * virtual_pos_j) / Σ_j w_ij
```

仮想座標を使うことで、重力変位で移動したノードの現在位置を反映する。ノードが動けば重心も動く — BHは静的ではなく、クラスタの動きに追従する。

### 2.5 共起ネイバーがいないノード

共起エッジを持たないノードには a_bh = 0（BH引力なし）。a_neighbors と a_anchor のみで動作。

## 3. 銀河形成のメカニズム

### 3.1 クラスタの束縛

```
初期状態: ノードA, B, C が頻繁に共起
  → edge(A,B)=10, edge(A,C)=8, edge(B,C)=12

重心BH形成:
  centroid_A ≈ (10*pos_B + 8*pos_C) / 18    Aから見たBH
  centroid_B ≈ (10*pos_A + 12*pos_C) / 22   Bから見たBH
  centroid_C ≈ (8*pos_A + 12*pos_B) / 20    Cから見たBH

  → 3つのBHは概ね同じ位置（A,B,Cの重心付近）
  → A,B,Cすべてがこの重心に引き寄せられる
  → 「銀河」が形成される
```

### 3.2 銀河間の相互作用

2つの共起クラスタが部分的に重なる場合（橋渡しノードが存在）:

```
銀河1: {A, B, C}    橋: D   銀河2: {D, E, F}
  D はABCとも、EFとも共起

  → Dの重心BHは銀河1と銀河2の中間付近
  → Dは両銀河の間を行き来する（彗星軌道）
  → DがEFの近くにいるとき、Eから「AI×料理」のような意外な情報が浮上
```

### 3.3 時間経過での進化

```
Phase 1: 共起エッジが蓄積される（edge_thresholdを超えるまで）
Phase 2: BHが形成され、クラスタが緩やかに収縮
Phase 3: 安定した銀河構造 — 公転軌道が確立
Phase 4: 新しいクエリパターンで新しいBHが形成 → 銀河再編
```

## 4. パラメータ

### 新規パラメータ

| パラメータ | 意味 | 推奨値 | config.json |
|-----------|------|--------|------------|
| bh_mass_scale | BH質量のスケーリング係数 | 0.5 | Yes |
| bh_gravity_G | BH専用の重力定数（通常Gとは別にチューニング可能） | config.gravity_G を流用 | Yes |

`bh_gravity_G` を `gravity_G` と別にすることで、「ノード間引力」と「クラスタBH引力」の強さを独立にチューニングできる。同じ値で良い場合は `gravity_G` を流用。

### 既存パラメータとの関係

| パラメータ | BHとの関係 |
|-----------|-----------|
| edge_threshold | BH形成条件（共起がこの回数以上でedge化→BH質量に寄与） |
| edge_decay | BHの弱体化（edgeが減衰→BH質量が減少→クラスタ解散） |
| prune_threshold | BH消滅条件（edgeが消滅→BHの質量源が失われる） |
| max_degree | 1ノードあたりのBH質量上限に間接的に影響 |

## 5. 実装

### 5.1 gravity.py への追加

```python
def compute_bh_acceleration(
    node_id: str,
    virtual_pos: np.ndarray,
    cache: CacheLayer,
    positions: dict[str, np.ndarray],
    config: GERConfig,
) -> np.ndarray:
    """Compute gravitational acceleration from co-occurrence cluster black hole.

    The BH is located at the weighted centroid of co-occurrence neighbors.
    Its mass is proportional to the total edge weight.
    """
    neighbors = cache.get_neighbors(node_id)
    if not neighbors:
        return np.zeros_like(virtual_pos)

    # Compute weighted centroid and BH mass
    total_weight = 0.0
    centroid = np.zeros_like(virtual_pos)
    for neighbor_id, weight in neighbors.items():
        pos_j = positions.get(neighbor_id)
        if pos_j is None:
            continue
        centroid += weight * pos_j
        total_weight += weight

    if total_weight < 1e-8:
        return np.zeros_like(virtual_pos)

    centroid /= total_weight
    bh_mass = config.bh_mass_scale * math.log(1.0 + total_weight)

    # Gravitational acceleration toward BH
    diff = centroid - virtual_pos
    distance_sq = float(np.dot(diff, diff)) + config.gravity_epsilon
    distance = math.sqrt(distance_sq)
    G = getattr(config, 'bh_gravity_G', config.gravity_G)
    magnitude = G * bh_mass / distance_sq
    direction = diff / distance

    return (magnitude * direction).astype(np.float32)
```

### 5.2 compute_acceleration への統合

```python
def compute_acceleration(pos_i, original_pos_i, displacement_i, neighbors, config,
                         node_id=None, cache=None, all_positions=None):
    acc = np.zeros_like(pos_i)

    # Neighbor gravity (existing)
    for pos_j, mass_j in neighbors:
        ...

    # Anchor restoring force (existing)
    acc -= config.orbital_anchor_strength * displacement_i

    # Co-occurrence black hole (new)
    if node_id and cache and all_positions:
        acc += compute_bh_acceleration(node_id, pos_i, cache, all_positions, config)

    return acc
```

### 5.3 update_orbital_state の変更

`compute_acceleration` にcache/positionsを渡すだけ。大きな構造変更は不要。

## 6. 可視化

### BHの描画

`visualize_3d.py` で共起クラスタのBHを表示:

```
● 通常のノード（恒星）
◆ BH位置（クラスタ重心）— 金色/紫色の特別なマーカー
○ BHの重力圏リング（BH質量に比例した半径）
```

BH位置の計算は可視化時にも同じ `compute_bh_centroid()` を使える。

### 銀河構造の強調

同じBHに束縛されているノード群を同じ色で着色するか、BHからの引力線を描画すると銀河構造が見える。

## 7. 実装ファイル

| ファイル | 変更内容 |
|---------|---------|
| `config.py` | bh_mass_scale, bh_gravity_G 追加 |
| `core/gravity.py` | compute_bh_acceleration() 追加、compute_acceleration() にBH引力統合 |
| `core/engine.py` | _update_simulation() でcache/positionsを加速度計算に渡す |
| `config.json.example` | bh_* パラメータ追加 |
| `scripts/visualize_3d.py` | BH重心マーカー表示（将来） |

## 8. 力のバランス設計指針

3つの力 + 摩擦の全体バランスが重要。

```
脱出しようとするノード:
  a_neighbors (外に引っ張る)  ← 一時的な共起
  a_anchor (原始位置に戻す)   ← 常に作用
  a_bh (クラスタ重心に引く)   ← 共起が蓄積されるほど強い
  friction (減速)             ← 常に作用

→ a_bh + a_anchor + friction > a_neighbors なら脱出不可能
→ 強い共起クラスタほどBH質量が大きく、メンバーの脱出が困難
→ 弱い共起のノードはBH引力が弱く、彗星軌道で通過可能
```

| シナリオ | 力のバランス | 結果 |
|---------|------------|------|
| 強い共起クラスタ | BH引力 >> anchor | 銀河形成、メンバーが安定周回 |
| 弱い共起 | BH引力 ≈ anchor | 緩い結合、時々離脱 |
| 共起なし | BH引力 = 0, anchor のみ | 原始位置付近に静止 |
| 一時的共起 | BH引力が一瞬発生→edge decay→消滅 | 彗星通過後、元に戻る |

## 9. edge decayとBHの寿命

共起エッジは `edge_decay` で減衰し、`prune_threshold` で消滅する。BH質量はエッジ重みの合計に依存するため、**エッジが減衰すればBHも弱くなり、最終的にクラスタが解散する**。

```
活発なクラスタ: 継続的にクエリ → edge weight増加 → BH質量維持 → 銀河安定
放置されたクラスタ: クエリなし → edge decay → BH質量減少 → 銀河解散 → ノードが原始位置に回帰
```

これは「忘却」の自然なメカニズムとなる。使われなくなった知識のクラスタは徐々に解散し、ノードは原始embedding位置に戻る。

## 10. 段階的実装

### Step 1: gravity.py に compute_bh_acceleration()
- 共起ネイバーの重心計算
- BH質量のlog飽和スケーリング
- BH方向への重力加速度

### Step 2: compute_acceleration() への統合
- 引数にcache/positionsを追加
- a_bh を a_total に加算

### Step 3: engine.py の _update_simulation() 更新
- positionsをcompute_accelerationに渡す

### Step 4: 可視化
- BH重心マーカー
- 銀河構造の強調表示
