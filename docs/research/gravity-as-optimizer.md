# Gravity as Optimizer — 重力式更新と最適化アルゴリズムの同型関係

> 「GER-RAG は RAG と名乗っているが、本質は **TTT (Test-Time Training)** ではないか？」
> その問いから出発し、物理ベースの更新則と既存最適化アルゴリズムの数学的対応を整理する。
> 最終更新: 2026-04-21

## TL;DR

GER-RAG の重力 + 軌道力学による更新は、形式的には **「Hebbian 引力 + L2 正則化を、Verlet 積分（leapfrog）で解く Heavy ball SGD」** に同型である。明示的な loss は無いが、暗黙の potential energy が最小化されている。これは TTT の一形態であり、「LLM 重みは frozen のまま、retrieval geometry を online で適応させる」というアプローチに位置付けられる。

物理アナロジーが**結果として**設計上の正則化と早期停止を兼ねている点が、独自性の核心。

---

## 1. 問いの設定

GER-RAG はクエリのたびに次を更新する:

- 各ノードの **質量** (`mass`)
- 各ノードの **速度** (`velocity`) と **変位** (`displacement`)
- 共起エッジの **重み** (`weight`)
- ノードの **温度** (`temperature` = sim_history の分散 × γ)

これは推論時の状態更新であり、**学習** と呼んで差し支えない構造を持つ。LLM の重みは frozen だが、retrieval を司る幾何が動いている。

問題: この更新則は最適化アルゴリズムとしてどう位置付けられるか？

---

## 2. 更新則の term-by-term マッピング

`ger_rag/core/gravity.py` の主要関数を gradient descent / momentum 系の言葉に翻訳する。

### 2.1 加速度の構成

```
a_i = Σ_j [G × m_j / (r_ij² + ε)] × dir(i→j)        ← 近傍引力
    + (-k × displacement_i)                          ← アンカー復元 (Hooke)
    + a_bh × escape(T_i)                             ← BH 引力 (温度脱出)
```

| 項 | 物理解釈 | 最適化での対応 |
|---|---|---|
| `G × m_j / r²` | 万有引力 | **Hebbian な負勾配** ─ 共起する質量ペアを引き寄せる |
| `-k × displacement_i` | Hooke 則 / バネの復元力 | **L2 正則化** (`-λ × θ`) ─ 原始位置への weight decay |
| `a_bh` | 共起クラスタ重心への引力 | **クラスタ平均への regularization** ─ 「グループ平均に向かう」 |
| `escape(T) = 1/(1+T×scale)` | 熱的脱出 | **Adaptive 温度減衰** ─ 不安定な点は引力を弱める |

### 2.2 速度の更新

```
v_i ← v_i + a_i × dt
v_i ← v_i × (1 - friction)
v_i ← clamp(v_i, max_velocity)
```

これは **Heavy ball method** (Polyak 1964) と同型:

```
m_t = β × m_{t-1} + g_t        # momentum 蓄積
θ_{t+1} = θ_t - η × m_t        # 位置更新
```

ただし `β` の代わりに `(1 - friction)` を使う、`g_t` の代わりに **物理的加速度**を使う点が違う。

`clamp` は **gradient clipping** に対応 ─ 暴走を防ぐ正則化。

### 2.3 位置の更新

```
displacement_i ← displacement_i + v_i × dt
displacement_i ← clamp(displacement_i, max_displacement_norm)
```

これは **Verlet 積分**（symplectic integrator）。物理シミュレーションで使われる手法で、エネルギー保存性が良い。最適化文脈では:

- **Symplectic optimizer** (Symplectic Adam 等) と関係深い
- **Hamiltonian Monte Carlo** の更新則とほぼ同じ形

`clamp(displacement, max_norm)` は **trust region** 制約に相当する。一度のステップで動ける距離に上限を設ける。

### 2.4 質量の更新

```
m_i ← m_i + η × force × (1 - m_i / m_max)
```

これは **logistic 飽和つき learning rate**。`m_i` が `m_max` に近づくほど更新が小さくなる ─ Adam の二次モーメント `v_t` が大きくなると effective LR が小さくなるのと同じ構造。

### 2.5 温度の更新

```
T_i = γ × var(sim_history_i)
```

直近の類似度履歴の分散。**Adam の v_t（二次モーメント）に類似** ─ 「最近どれだけ動いているか」を測る。さらに `escape(T)` で BH 引力を弱めるので、**動きが激しいノードは引力を逃れる** = 不確実性に応じた step 制御。

---

## 3. 暗黙の Loss Function

明示的な loss は無いが、以下の potential energy を最小化していると解釈できる:

```
U(positions) =
    - Σ_{i<j} cooccur(i,j) × G × m_i × m_j × cos_sim(virtual_pos_i, virtual_pos_j)   ← Hebbian 引力 (負: 最小化したい)
    + (k/2) × Σ_i ||displacement_i||²                                                  ← L2 正則化
    + (other terms for BH, thermal, etc.)
```

**第 1 項の Hebbian 引力**: 共起する質量ペアの内積を最大化したい（= 距離を最小化したい）。これは:
- **Word2vec の skip-gram 目的関数** と同型: `Σ log σ(v_i · v_j)` for co-occurring (i,j)
- **対照学習 (Contrastive Learning)** の positive pair loss と同型
- **Hebbian 学習則** `Δw_ij ∝ x_i × x_j` の連続版

**第 2 項の L2 正則化**: 原始位置からの距離を罰する。これは:
- **Weight decay** (`L2` reg) と完全同型
- **Elastic Weight Consolidation (EWC)** の Fisher information なしバージョン: 「元の重みから離れすぎるな」

両項の組み合わせは、**「co-occurrence で近づき、anchor で引き戻す」** という二つの力の均衡点を探す問題になっている。最適点は:

```
∂U/∂x_i = 0
↔ Σ_j cooccur(i,j) × G × m_i × m_j × ∂cos_sim/∂x_i = k × displacement_i
↔ 「Hebbian 引力 = アンカー復元力」のバランス点
```

これは **Self-Organizing Map (SOM)** や **Neural Gas** の収束条件と同じ構造。

---

## 4. 既存アルゴリズムとの対応

### 4.1 Heavy ball method (Polyak, 1964)

```
v_t = β × v_{t-1} - η × ∇L(θ_t)
θ_{t+1} = θ_t + v_t
```

GER-RAG の更新と直接同型。`-η × ∇L` が `acceleration` に置き換わっただけ。

**Heavy ball は SGD with momentum の最古の形** で、物理の慣性球の運動方程式から発想されている。GER-RAG はその「物理アナロジー」を文字通り採用している。

### 4.2 Hebbian Learning / Oja's Rule

Hebbian: `Δw_ij ∝ x_i × x_j`

GER-RAG: 共起する `(i,j)` ペアにおいて
- edge weight が +1 蓄積
- 引力が双方向に発生
- 結果として virtual_pos が引き寄せ合う

これは **2 層 Hebbian network** の連続時間版に相当。

### 4.3 Self-Organizing Maps (Kohonen, 1982)

SOM:
- ノードが topology を持つ
- 入力に最近い node が更新される
- 近傍 node も少し動く（Gaussian neighborhood）
- 時間とともに学習率と近傍幅が減衰

GER-RAG:
- ノードが embedding 空間に配置
- recall すると mass + displacement が更新
- 共起 node も引力で引き寄せられる
- friction + decay で時間とともに動きが止まる

両者は**位相保存的な競合学習** という点で本質的に同じ。SOM が grid topology、GER-RAG が co-occurrence topology という違い。

### 4.4 Word2vec (Mikolov et al., 2013)

skip-gram: 共起する単語ペアの embedding 内積を最大化。

GER-RAG: 共起する記憶ペアの virtual_pos 内積を最大化（暗黙の Hebbian 引力 ≡ 内積最大化）。

ただし word2vec は学習時に固定された corpus に対して数 epoch 回す。**GER-RAG は推論時に online で 1 step ずつ進める**。これが TTT 性。

### 4.5 Hamiltonian Monte Carlo (HMC)

HMC は position + momentum をペアで持って leapfrog 積分する MCMC 手法。GER-RAG の Stage 1-2-3 は leapfrog そのもの。違いは:

- HMC: 確率分布からサンプリングする（受容/棄却あり）
- GER-RAG: 決定論的に動かす（friction で散逸させる）

つまり **「散逸あり HMC」 = ランジュバン動力学 (Langevin Dynamics)** に近い。

### 4.6 Adam Optimizer

Adam の effective learning rate は `α / (√v_t + ε)`。GER-RAG の effective force は `G × m_j / (r² + ε)` で、`r²` が大きいほど力が小さくなる。両者は **「分母に何かの二乗が入って effective step が adaptive になる」** 構造を共有する。

ただし Adam の `v_t` は勾配の二次モーメント、GER-RAG の `r²` は空間距離。意味は違うが数学的構造が似ている。

---

## 5. TTT としての位置づけ

### 5.1 既存の TTT (Sun et al., 2020) との比較

| 項目 | Sun et al. の TTT | GER-RAG |
|---|---|---|
| 更新対象 | LLM の重み (auxiliary task で fine-tune) | retrieval gravity field (mass, displacement, edges) |
| 学習信号 | rotation prediction 等の self-supervised loss | co-occurrence statistics (Hebbian) |
| 1 step | gradient step | 物理 step (leapfrog) |
| 1 サンプル毎の効果 | 重みが変わる | 検索結果が変わる |
| LLM への影響 | 直接 | 間接 (recall 結果経由) |

### 5.2 何が嬉しいか

- **LLM 重みを触らない**: catastrophic forgetting のリスクなし
- **動的計算グラフが不要**: gradient backprop が無いので軽い
- **physical interpretability**: 「なぜそうなったか」が物理で説明できる
- **shared memory**: 複数のエージェントが同じ「学習状態」を読み書きできる（重みベースだと難しい）

### 5.3 何が制限か

- **更新できる空間が固定**: embedding 空間内での displacement に限定される
- **新しい意味は学べない**: 既存の embedding が表現できないものは扱えない（LLM 重みを動かさない代償）
- **明示的な目的関数が無い**: 何を達成しているか証明しにくい

---

## 6. 物理法則がそのまま正則化になる現象

GER-RAG 設計の独自性は、**物理的に自然な制約が、機械学習的な regularization と一致する** こと。意図したかは別として、結果として:

| 物理 | 正則化としての効果 |
|---|---|
| Hooke 則アンカー (`-k × displacement`) | **L2 weight decay** ─ 元の意味から離れすぎない |
| Friction (`v *= 1-f`) | **momentum decay** ─ 古い更新の影響を漸減 |
| Mass saturation (`m_max = 50`) | **学習率の自然減衰** ─ rich-get-richer の暴走を抑制 |
| Velocity clamp | **gradient clipping** ─ 1 step あたりの動きを制限 |
| Displacement clamp | **trust region** ─ 大域的な安定性 |
| Thermal escape | **adaptive damping** ─ 不安定領域での更新抑制 |
| Verlet 積分 | **symplectic step** ─ エネルギー保存性で発散しない |

これは「正則化の集合体 = 物理法則」とも言える。Heavy ball method が物理の慣性球から発想されたのと同じく、**良い正則化は良い物理に対応する** という観察の例。

---

## 7. 相転移と emergent behavior

最適化アルゴリズムには通常見られない、しかし GER-RAG では起きる現象:

### 7.1 BH 化 (Phase Transition)

mass が `bh_mass_scale × log(1 + Σ edge_weight)` を超える節点は、周辺ノードに対して支配的引力源になる。これは:

- **Adam の effective LR が極端に変化する点** に相当（実用上、optimizer の挙動が質的に変わる）
- **「rich get richer」相転移** ─ 強い記憶が周囲を吸収していく

明示的な閾値ではなく、**質量分布の log scaling** から自然に出てくる相転移。

### 7.2 軌道形成

近傍引力 + アンカー復元 + 友円的な角運動量で、ノードが **公転軌道** を持つ。これは普通の SGD では起きない（friction で必ず止まるので）。

GER-RAG では `orbital_friction` を低めに設定すると、安定した周期軌道が観察される。**最適化が収束しない代わりに振動的構造を保つ** ─ 通常 GD では病理的、ここでは設計意図通り。

### 7.3 銀河衝突合体 (F2.1)

十分接近したノードペアが質量加算 + 運動量保存で merge する。これは **online clustering** の一形態。Mean shift や DBSCAN との違いは、merge が **不可逆** な点。

最適化文脈では、merge は **次元削減** と **正則化** を同時に行う操作と解釈できる。

---

## 8. 含意 ─ TTT 研究への接続

GER-RAG が示唆するのは:

### 8.1 「LLM weight を動かさない TTT」の可能性

retrieval geometry を online で適応させるだけで、TTT が達成可能。これは:
- LoRA の retrieval 版とも言える
- shared memory 上で複数エージェントが学習を共有できる ─ 重み更新では難しい
- catastrophic forgetting がない

### 8.2 「物理ベース正則化」の体系的研究

GER-RAG の設計は、物理シミュレーションのパラメータがそのまま regularization のハイパラになる。`gravity_G` `friction` `max_displacement_norm` 等が直接「学習の安定性」を決める。

これは**「物理シミュレーションを最適化として転用する」**という枠組みの実例。系統的に研究されれば、新しい optimizer 族として展開できる可能性がある。

### 8.3 共有メモリでの distributed TTT

複数の MCP プロセスが同じ DB を共有する状況は、**「複数のエージェントが共通の TTT 学習状態を共有する」** 構図。これは federated learning とも online distributed learning とも違う、新しい実例。

マルチエージェント実験 ([Multi-Agent Experiment](multi-agent-experiment-2026-04-21.md)) で観察された「共有メモリでの暗黙協調」は、この distributed TTT の emergent な現れである。

---

## 9. 開いている問い

1. **暗黙の loss を陽に書き下したら、何になるか？** 第 6 節で部分的に書いたが、BH 引力や thermal escape を含めた完全形は未整理。
2. **収束性の理論保証**: friction + bounded forces から Lyapunov 関数を構成できるか？
3. **Adam / SGD-momentum との empirical 比較**: 同じタスクで両者を比較したベンチマークは未実施。
4. **distributed TTT の理論**: 複数プロセスでの共有 DB 学習は、federated learning と何が同じで何が違うか？
5. **catastrophic forgetting の不在の証明**: アンカー復元力があれば、原理的に embedding が破壊されないことを示せるか？

---

## 10. 参考文献（系譜）

- **Polyak, B. T. (1964)**. *Some methods of speeding up the convergence of iteration methods.* USSR Computational Mathematics. ─ Heavy ball method の原典
- **Hebb, D. O. (1949)**. *The Organization of Behavior.* ─ Hebbian 学習の原典
- **Kohonen, T. (1982)**. *Self-organized formation of topologically correct feature maps.* ─ SOM
- **Mikolov, T. et al. (2013)**. *Efficient Estimation of Word Representations in Vector Space.* ─ word2vec / skip-gram
- **Kingma & Ba (2014)**. *Adam: A Method for Stochastic Optimization.* ─ Adam optimizer
- **Sun, Y. et al. (2020)**. *Test-Time Training with Self-Supervision for Generalization under Distribution Shifts.* ─ TTT 原典
- **Friston, K. (2010)**. *The free-energy principle.* ─ 予測的符号化と暗黙最適化の理論枠組
- **Neal, R. M. (2011)**. *MCMC using Hamiltonian dynamics.* ─ HMC / leapfrog 積分

## 関連ドキュメント

- [Architecture — Gravity Model](../wiki/Architecture-Gravity-Model.md) ─ 実装側のスコア式と軌道力学
- [Gravitational Displacement Design](gravitational-displacement-design.md) ─ 設計時の物理アナロジー意図
- [Orbital Mechanics Design](orbital-mechanics-design.md) ─ 軌道力学の詳細
- [Multi-Agent Experiment](multi-agent-experiment-2026-04-21.md) ─ 共有メモリでの distributed TTT 観察
- [Five-Layer Philosophy](../wiki/Reflections-Five-Layer-Philosophy.md) ─ 物理 → TTT 機構 → 生物 → 関係 → 人格の五層論
