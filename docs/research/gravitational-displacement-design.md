# Gravitational Displacement 設計書

**日付**: 2026-03-27
**ステータス**: Phase 2 設計提案
**前提**: [Phase 2 評価レポート](evaluation-report.md) の知見に基づく

## 1. 背景と動機

### Phase 1 評価からの学び

Phase 1のGER-RAGは動的スコアリング層で検索スコアを調整するアプローチを採った。評価の結果：

- nDCG +2.7%、MRR +13.2% の改善を達成
- しかし **top-10のドキュメントの顔ぶれはほぼ変わらなかった**
- 1,000以上の共起エッジが形成されたにもかかわらず、graph_boostは順位を大きく動かせなかった

原因: graph_boostがtop-K全員に均一に効くため、相対的な順位差が維持される。元のembedding空間での距離がスコアを支配し、動的要素は微調整にとどまる。

### 本来の設計意図

GER-RAGの「重力」「加速度」というメタファーは、**文書がembedding空間上で物理的に移動する**ことを意味していた。目的は検索精度の最適化ではなく、**セレンディピティと創発** — 使い込むほど予想外のつながりが発見される検索体験。

## 2. 設計概要

### コンセプト: 二重座標系

```
┌──────────────────────────────────────────────────────────┐
│  原始embedding空間 (immutable)                            │
│  ・RURI-v3の出力そのまま                                    │
│  ・FAISSインデックスに格納                                   │
│  ・広い候補取得（top-K * 3）に使用                            │
│  ・変更されない（embeddingモデルの意味的保証を維持）              │
└──────────────────────────────────────────────────────────┘
                          ↓ 候補取得
┌──────────────────────────────────────────────────────────┐
│  仮想座標空間 (mutable, gravity-driven)                    │
│  ・virtual_pos = original_embedding + displacement        │
│  ・共起した文書同士が重力で引き寄せ合う                         │
│  ・massが大きいノードほど強い引力                              │
│  ・最終ランキングの類似度計算に使用                             │
│  ・クエリのたびに更新される                                    │
└──────────────────────────────────────────────────────────┘
```

### 期待される効果

| シナリオ | Phase 1 (スコア調整) | Phase 2 (座標変位) |
|---------|--------------------|--------------------|
| AI×プログラミング頻出 | スコア微増、順位変わらず | AI文書がプログラミング空間に接近し、top-Kに浮上 |
| 映画×日常の共起 | 同上 | 映画鑑賞の日常ツイートが「日常」検索に出現 |
| 長期未アクセス | decayでスコア減少 | decayで元の座標に戻る（引力を失う） |
| 高mass文書 | 対数的スコア加算 | 周囲の文書を引き寄せるハブになる |

## 3. 物理モデル

### 3.1 ノードの拡張状態

Phase 1の `NodeState` に `displacement` ベクトルを追加する。

```
NodeState:
  id:            str          # ドキュメントID
  mass:          float        # 質量（従来通り）
  temperature:   float        # 温度（従来通り）
  last_access:   float        # 最終アクセス（従来通り）
  sim_history:   list[float]  # 類似度履歴（従来通り）
  displacement:  float[768]   # 【新規】重力による変位ベクトル
```

### 3.2 仮想座標

```
virtual_pos[i] = normalize(original_embedding[i] + displacement[i])
```

L2正規化を維持する（cosine similarity = inner product の前提を保つ）。

### 3.3 重力の計算

クエリ結果のtop-K内で、共起したノードペア (i, j) に対して重力が働く。

```
# ノードiがノードjから受ける重力
direction_ij = normalize(original_emb[j] - virtual_pos[i])
distance_ij  = ||virtual_pos[i] - virtual_pos[j]||
force_ij     = G * mass[i] * mass[j] / (distance_ij^2 + ε)

# 変位の更新（加速度 → 速度的な蓄積）
displacement[i] += η_g * force_ij * direction_ij
```

| パラメータ | 意味 | 推奨範囲 |
|-----------|------|---------|
| G | 万有引力定数 | 0.001 - 0.05 |
| η_g | 変位の学習率 | 0.001 - 0.01 |
| ε | ゼロ除算防止 | 1e-6 |

### 3.4 減衰（元の位置への回帰）

変位は時間とともに減衰する。長期間アクセスされない文書は元のembedding位置に戻る。

```
# 毎クエリまたは定期的に適用
displacement[i] *= displacement_decay  # 例: 0.99

# last_access が古いほど強く減衰
age_factor = exp(-δ_d * (now - last_access[i]))
displacement[i] *= age_factor
```

これにより：
- 継続的に共起する文書 → 引き寄せ合い続ける（変位が蓄積）
- 一時的に共起した文書 → 時間とともに元の位置に戻る
- 完全に忘れられた文書 → displacement ≈ 0（原始位置に回帰）

### 3.5 温度による揺らぎ

従来のtemp_noiseはスコアへのガウシアンノイズだったが、座標空間にも揺らぎを加える。

```
# 高温のノードは仮想座標が揺らぐ → 探索的な検索結果を生む
exploration_noise = Normal(0, temperature[i]) * random_unit_vector
virtual_pos[i] += exploration_noise  # クエリ時のみ一時的に適用
```

文脈によって異なるクエリに反応する不安定な知識は、温度が高い → 毎回少し違う位置に現れる → 意外な文脈で発見される。

## 4. 検索フローの変更

### Phase 1 (現在)

```
Query → Embed → FAISS top-K → Score調整 → Return
```

### Phase 2 (Gravitational Displacement)

```
Query → Embed(query_vec)
  │
  ├─ Step 1: FAISS候補取得（原始embedding、広めに top-K * 3）
  │           → 30候補を取得
  │
  ├─ Step 2: 仮想座標での再計算
  │           for each candidate:
  │             virtual = normalize(original_emb + displacement)
  │             virtual += temp_exploration_noise  (一時的)
  │             gravity_sim = dot(query_vec, virtual)
  │
  ├─ Step 3: 最終スコア
  │           final = gravity_sim * decay + mass_boost
  │           filter: final > 0
  │           sort by final, take top-K
  │
  ├─ Step 4: 重力更新（返却後、非同期）
  │           for each pair (i,j) in result:
  │             apply gravitational force i↔j
  │           update displacement, mass, temperature
  │
  └─ Return top-K
```

### Phase 1 との主な差分

| 工程 | Phase 1 | Phase 2 |
|------|---------|---------|
| FAISS取得数 | top-K (10) | top-K * 3 (30) |
| 類似度計算 | 原始embedding固定 | **仮想座標で再計算** |
| graph_boost | スコア加算 | **廃止（重力に統合）** |
| 状態更新 | mass, temp, 共起カウント | mass, temp, **displacement** |
| 結果の多様性 | 低い（静的RAGとほぼ同じ） | **高い（重力で異クラスタ文書が浮上）** |

## 5. 実装方針

### 5.1 変更ファイル

| ファイル | 変更内容 |
|---------|---------|
| `ger_rag/config.py` | 重力パラメータ追加 (G, η_g, ε, displacement_decay, candidate_multiplier) |
| `ger_rag/core/types.py` | NodeStateにdisplacementフィールド追加 |
| `ger_rag/core/engine.py` | query()を二段階検索に変更、重力更新ロジック追加 |
| `ger_rag/core/gravity.py` | 【新規】重力計算モジュール（force, displacement更新, 減衰） |
| `ger_rag/core/scorer.py` | graph_boost廃止、gravity_sim追加 |
| `ger_rag/index/faiss_index.py` | get_vector(id)メソッド追加（原始embeddingの取得） |
| `ger_rag/store/sqlite_store.py` | displacementの永続化（BLOB、numpy→bytes） |
| `ger_rag/store/cache.py` | displacementのキャッシュ |
| `ger_rag/graph/cooccurrence.py` | 共起カウントは維持、graph_boost計算は廃止 |

### 5.2 新規モジュール: gravity.py

```python
# ger_rag/core/gravity.py

def compute_gravitational_force(
    pos_i: np.ndarray,      # virtual_pos of node i
    pos_j: np.ndarray,      # virtual_pos of node j
    mass_i: float,
    mass_j: float,
    G: float,
    epsilon: float,
) -> np.ndarray:
    """Compute gravitational force vector on node i from node j."""
    ...

def update_displacements(
    result_ids: list[str],
    embeddings: dict[str, np.ndarray],
    displacements: dict[str, np.ndarray],
    masses: dict[str, float],
    config: GERConfig,
) -> dict[str, np.ndarray]:
    """Update displacement vectors for all co-retrieved pairs."""
    ...

def apply_decay(
    displacement: np.ndarray,
    displacement_decay: float,
    last_access: float,
    now: float,
    delta_d: float,
) -> np.ndarray:
    """Decay displacement toward zero (return to original position)."""
    ...

def compute_virtual_position(
    original_emb: np.ndarray,
    displacement: np.ndarray,
    temperature: float,
) -> np.ndarray:
    """Compute virtual position with optional thermal noise."""
    ...
```

### 5.3 ストレージ

displacementは768次元のfloat32ベクトル（3KB/ノード）。12,000ノードで約36MB。

```sql
-- nodes テーブルに追加
ALTER TABLE nodes ADD COLUMN displacement BLOB;
-- NULL = 変位なし（原始位置）
```

キャッシュ上では `dict[str, np.ndarray]` で保持。

### 5.4 FaissIndexの拡張

重力計算に原始embeddingが必要なため、IDから原始embeddingを逆引きできるようにする。

```python
class FaissIndex:
    def get_vectors(self, ids: list[str]) -> np.ndarray:
        """Retrieve original embedding vectors by IDs."""
        ...
```

## 6. ハイパーパラメータ

### 新規パラメータ

| パラメータ | 意味 | 推奨初期値 | 調整の方向 |
|-----------|------|----------|-----------|
| G | 万有引力定数 | 0.01 | 大きいほど急速に引き寄せ合う |
| η_g | 変位の学習率 | 0.005 | 大きいほど1回のクエリでの変位が大きい |
| ε | ゼロ除算防止 | 1e-6 | 固定 |
| displacement_decay | 変位の定期減衰率 | 0.995 | 小さいほど早く元に戻る |
| δ_d | アクセス間隔ベース減衰 | 0.005 | 大きいほど未アクセス文書が早く元に戻る |
| candidate_multiplier | FAISS候補倍率 | 3 | 大きいほど広い候補から選べる |
| max_displacement_norm | 変位ベクトルのL2ノルム上限 | 0.3 | 大きいほど原始位置から遠くへ移動可能 |

### 変位の上限

`max_displacement_norm` で変位の大きさを制限する。これにより、文書がembedding空間で「暴走」して全く無関係な領域に飛んでいくことを防ぐ。

```
if ||displacement[i]|| > max_displacement_norm:
    displacement[i] = normalize(displacement[i]) * max_displacement_norm
```

0.3という値は、embedding空間でのcosine distanceで約0.3の移動を許容する。これは「同じ大トピック内の別サブトピック」程度の距離に相当。

### Phase 1 パラメータとの関係

| Phase 1 パラメータ | Phase 2 での扱い |
|-------------------|-----------------|
| α (mass_boost) | 維持（軽微なスコア調整として残す） |
| δ (temporal decay) | 維持 |
| γ (temperature) | 維持 + 座標揺らぎにも使用 |
| η (mass growth) | 維持 |
| ρ (graph_boost) | **廃止**（重力に統合） |
| edge_threshold | 維持（共起カウントの閾値として） |
| G, η_g, δ_d | **新規** |

## 7. 可視化での確認ポイント

`visualize_3d.py` で以下の変化が観察できるはず：

1. **クラスタの融合**: AI文書とプログラミング文書が頻繁に共起すると、3D空間上で2つのクラスタが接近する
2. **ハブ形成**: 高massのノードが周囲の文書を引き寄せ、星のような構造を形成
3. **孤立文書の原始位置回帰**: アクセスされない文書は元の位置に留まる
4. **温度の高いノードの揺らぎ**: 可視化のたびに少し位置が変わるノード

## 8. リスクと対策

| リスク | 影響 | 対策 |
|-------|------|------|
| 変位が大きすぎてノイジーな結果 | 無関係な文書が上位に | max_displacement_normで制限 |
| 計算コストの増加 | レイテンシ劣化 | 重力更新は非同期、仮想座標計算はNumPyバッチ演算 |
| displacement の永続化サイズ | ストレージ増加 | numpy→bytes圧縮、変位0のノードはNULL |
| embeddingの意味的保証の喪失 | 検索品質劣化 | 原始embeddingでの候補取得は維持（フォールバック） |
| 重力の暴走 | クラスタ崩壊 | ノルム上限 + 定期的な全体減衰 |

## 9. 段階的導入

### Step 1: gravity.py の実装とユニットテスト
重力計算の純粋関数を実装し、ベクトル演算の正確性を確認。

### Step 2: engine.py の二段階検索化
FAISS候補取得 → 仮想座標再計算 → top-K選出のフローに変更。

### Step 3: 重力更新の組み込み
クエリ後の状態更新にdisplacement更新を追加。

### Step 4: 可視化で動作確認
`visualize_3d.py` で原始座標 vs 仮想座標のBefore/After比較。

### Step 5: 評価
eval_export.py → LLM判定 → eval_compute.py で Before/After のnDCG比較。
今度はドキュメントの入れ替わり（New docs > 0）が発生するはず。
