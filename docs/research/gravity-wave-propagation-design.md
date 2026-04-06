# Gravity Wave Propagation 設計書

**日付**: 2026-04-06
**ステータス**: 設計
**前提**: [Gravitational Displacement 設計書](gravitational-displacement-design.md) の重力モデルを拡張

## 1. 背景

### Phase 2 の限界

Phase 2の重力変位モデルでは、FAISS top-K×3 の候補をフラットに取得して仮想座標で再計算していた。これは「広いが浅い」探索であり、同一クラスタ内の文書しか候補に入らない。

### 本設計の目的

重力場を**再帰的に伝播**させることで、意味空間上を「歩く」探索を実現する。さらに、**massが大きいノードほど広い重力圏**を持つことで、ハブノードが知識構造の要として機能する。

## 2. 設計概要

### 再帰的重力波伝播

```
Query
  │
  ▼
FAISS top-k=3 (depth 0, force=1.0)
  ├── Doc A (mass=8.0 → 近傍 top-k=5)
  │     ├── Doc D (depth 1, force=0.3)
  │     │     ├── Doc M (depth 2, force=0.09)
  │     │     └── ...
  │     ├── Doc E
  │     ├── Doc F
  │     ├── Doc G  ← 高massだから5つ引き込む
  │     └── Doc H
  ├── Doc B (mass=1.2 → 近傍 top-k=2)
  │     ├── Doc I (depth 1, force=0.3)
  │     └── Doc J
  └── Doc C (mass=3.0 → 近傍 top-k=3)
        ├── Doc K
        ├── Doc L
        └── Doc N
```

### massによるtop-k変調

ノードの質量が大きいほど、そのノードの重力場で引き込む近傍の数が増える。

```
node_top_k = base_k + floor(mass_scale * log(1 + mass))
```

| mass | node_top_k (base=2, scale=2) | 恒星アナロジー |
|------|------------------------------|--------------|
| 1.0 | 2 + 1 = 3 | 赤色矮星（小さな重力圏） |
| 3.0 | 2 + 2 = 4 | 主系列星 |
| 8.0 | 2 + 4 = 6 | 赤色巨星（広い重力圏） |
| 20.0 | 2 + 6 = 8 | 超巨星（多くの文書を支配） |
| 50.0 | 2 + 7 = 9 | ブラックホール級（max_node_top_kで制限） |

### 力の減衰

depthが深くなるほど重力が弱まる。

```
force_at_depth = base_force * attenuation^depth
```

| depth | force (attenuation=0.3) | 意味 |
|-------|------------------------|------|
| 0 | 1.000 | クエリ直接ヒット |
| 1 | 0.300 | 1ホップ先 |
| 2 | 0.090 | 2ホップ先（セレンディピティ領域） |
| 3 | 0.027 | 3ホップ先（かすかな引力） |

### massによる減衰の変調

高massノードは重力の減衰が緩やかになる（遠くまで力が届く）。

```
effective_attenuation = attenuation * (1.0 - mass_attenuation_factor * log(1 + mass) / log(1 + m_max))
```

mass=1.0 → attenuation=0.30 (通常の減衰)
mass=12.0 → attenuation=0.22 (減衰が緩やか、遠くまで届く)
mass=50.0 → attenuation=0.15 (広い重力圏)

## 3. 検索フロー

### Phase 2 (現在)

```
Query → FAISS top-K×3 (flat) → 仮想座標再計算 → top-K返却
```

### Phase 3 (Gravity Wave)

```
Query → embed(query)
  │
  ├─ Step 1: FAISS初期検索 (query, wave_initial_k=3)
  │           → seed nodes (depth=0, force=1.0)
  │
  ├─ Step 2: 再帰的近傍展開
  │    for each depth in range(wave_max_depth):
  │      for each node at current depth:
  │        node_k = base_k + floor(mass_scale * log(1 + mass))
  │        neighbors = FAISS search(node.embedding, node_k)
  │        add neighbors at depth+1, force *= attenuation
  │      deduplicate (keep max force per node)
  │
  ├─ Step 3: 仮想座標での最終スコアリング
  │    for each reached node:
  │      virtual_pos = normalize(original_emb + displacement)
  │      gravity_sim = dot(query, virtual_pos)
  │      wave_boost = force * mass_factor
  │      final = gravity_sim * decay + mass_boost + wave_boost
  │
  ├─ Step 4: top-K選出 → 返却
  │
  └─ Step 5: 重力更新（従来通り + 波及範囲のdisplacement更新）
```

### 計算コスト分析

wave_initial_k=3, wave_max_depth=2, base_k=2, mass_scale=2

最悪ケース（全ノードがmass=50）:
```
depth 0: 3 nodes, 3 FAISS searches
depth 1: 3 × 9 = 27 nodes, 27 FAISS searches  
depth 2: 27 × 9 = 243 nodes, 243 FAISS searches
Total: ~273 FAISS top-9 searches
```

現実的なケース（大半がmass=1-3、一部のハブがmass=8-12）:
```
depth 0: 3 nodes, 3 FAISS searches
depth 1: 3 × avg(3-4) ≈ 10 nodes, 10 FAISS searches
depth 2: 10 × avg(3-4) ≈ 35 nodes, 35 FAISS searches
Total: ~48 FAISS top-4 searches, ~48 unique nodes
```

FAISS top-3~9 は ~0.1ms/回なので、48回でも ~5ms。embedding生成(~20ms)に比べて十分小さい。

## 4. パラメータ

### 新規パラメータ

| パラメータ | 意味 | デフォルト | config/引数 |
|-----------|------|----------|------------|
| wave_initial_k | 初期FAISS検索のtop-k | 3 | 両方 |
| wave_max_depth | 再帰の最大深さ | 2 | 両方 |
| wave_base_k | 各ノードの最小近傍数 | 2 | config |
| wave_mass_scale | massによるtop-k増分の係数 | 2.0 | config |
| wave_max_node_k | 1ノードあたりの最大近傍数 | 10 | config |
| wave_attenuation | depth毎の力の減衰率 | 0.3 | config |
| wave_mass_attenuation_factor | massによる減衰緩和の強さ | 0.5 | config |

### 引数としての指定（MCP/API）

`recall` や `/query` で以下をオーバーライド可能:

```python
# MCP
recall(query="...", wave_depth=3, wave_k=5)

# API
POST /query {"text": "...", "wave_depth": 3, "wave_k": 5}
```

指定しない場合はconfigのデフォルト値を使用。

### 既存パラメータとの関係

| Phase 2 パラメータ | Phase 3 での扱い |
|-------------------|-----------------|
| candidate_multiplier (3) | **廃止** — wave探索に置き換え |
| gravity_G, gravity_eta | 維持（displacement更新に使用） |
| displacement_decay | 維持 |
| max_displacement_norm | 維持 |

## 5. 重複到達時の処理

同じノードに複数のパスで到達した場合:

```
# 各パスのforceを合算
total_force[node] = sum(force from each path)

# ただし上限あり
total_force[node] = min(total_force[node], max_force_cap)
```

合算することで「複数の文脈から参照されるノード」がより強く引き寄せられる。これは共起グラフのハブ効果と整合する。

## 6. wave_boostのスコアリングへの統合

到達したノードの最終スコアに、重力波による到達力を加味する:

```
final = gravity_sim * decay + mass_boost + wave_boost

wave_boost = β * total_force[node]
```

| パラメータ | 意味 | デフォルト |
|-----------|------|----------|
| β (wave_boost_weight) | 波及力のスコアへの重み | 0.05 |

depth=0（直接ヒット）のノードは force=1.0 → wave_boost=0.05
depth=2のノードは force≈0.09 → wave_boost≈0.005

これにより、直接ヒットしたノードが依然として上位に来やすいが、波及で到達したノードも結果に混ざる。

## 7. displacement更新への影響

### 現在のモデル

top-K結果内の全ペアでdisplacementを更新。

### 拡張

wave探索で到達した全ノード（depth 0-2）に対してdisplacementを更新する。ただし、forceに比例して更新量を減衰:

```
displacement_delta[i] *= total_force[i]
```

depth=2 で到達したノードは微小な変位しか受けない。しかし、繰り返しのクエリで蓄積されると、遠いクラスタの文書が少しずつ接近してくる。

## 8. FaissIndexの拡張

近傍検索に「ノードのembeddingを起点にした検索」が必要:

```python
class FaissIndex:
    def search_by_id(self, node_id: str, top_k: int) -> list[tuple[str, float]]:
        """Search nearest neighbors of a specific node's embedding."""
        vec = self.get_vectors([node_id]).get(node_id)
        if vec is None:
            return []
        return self.search(vec.reshape(1, -1), top_k)
```

## 9. 実装ファイル

| ファイル | 変更内容 |
|---------|---------|
| `config.py` | wave_*パラメータ7つ追加 |
| `core/gravity.py` | `propagate_gravity_wave()` 関数追加 |
| `core/engine.py` | query()をwave探索に変更 |
| `core/types.py` | QueryRequestにwave_depth, wave_k追加 |
| `index/faiss_index.py` | `search_by_id()` 追加 |
| `server/app.py` | /query パラメータ拡張 |
| `server/mcp_server.py` | recall/exploreにwave引数追加 |

## 10. 段階的実装

### Step 1: gravity.py に波伝播関数
- `propagate_gravity_wave()`: seed nodes → 再帰展開 → force計算 → 全到達ノード返却
- `compute_node_top_k()`: massからノードごとのtop-kを算出
- 純粋関数としてテスト可能

### Step 2: FaissIndex.search_by_id()
- ノードIDからそのノードの近傍を検索

### Step 3: engine.query() の書き換え
- flat候補取得 → wave探索に置き換え
- wave_boostをスコアリングに統合
- displacement更新をforce加重に

### Step 4: API/MCP引数の拡張
- QueryRequestにwave_depth, wave_k追加
- MCP recall/exploreに引数追加

### Step 5: 評価・可視化
- eval_export.py で Before/After 比較
- visualize_3d.py で波及範囲の可視化（depthに応じた色分け？）

## 11. 可視化のアイデア

wave探索の経路を可視化に反映:

```
depth 0 (直接ヒット) → 明るい恒星
depth 1 (1ホップ)    → やや暗い星
depth 2 (2ホップ)    → かすかに光る星
wave経路             → 重力波のフィラメントとして描画
```

これにより、クエリがどのように知識空間を「伝播」したかが視覚的に確認できる。
