# Research — Gravity as Optimizer

> **このドキュメントが GaOTTT という名前の根拠** である。
> 旧名 GER-RAG (Gravity-Based Event-Driven RAG) は「重力を使った RAG」を標榜していた。本ページでの整理を経て **「本質は TTT (Test-Time Training) である」という位置付けが確定** し、改名 GaOTTT (Gravity as Optimizer Test-Time Training) に至った。以下はその論証。

物理ベース更新と既存最適化アルゴリズム（Heavy ball SGD / Hebbian / Adam / SOM 等）の数学的同型関係を整理した研究ノート。

**完全版（参考文献付き）**: [`docs/research/gravity-as-optimizer.md`](../research/gravity-as-optimizer.md)

## TL;DR

GaOTTT の重力 + 軌道力学による更新は、形式的には **「Hebbian 引力 + L2 正則化を、Verlet 積分（leapfrog）で解く Heavy ball SGD」** と同型。明示的な loss 関数は無いが、暗黙の potential energy が最小化されている。

これは TTT の一形態であり、**「LLM の重みは frozen のまま、retrieval geometry を online で適応させる」** というアプローチに位置付けられる。**比喩ではなく数学的同型** であり、これが命名 GaOTTT の根拠。

## 同型関係の早見表

| 物理 (`gravity.py`) | 最適化 |
|---|---|
| 近傍引力 `G × m_j / r²` | Hebbian な負勾配 |
| Hooke 復元力 `-k × displacement` | **L2 weight decay** |
| Friction `v *= (1-f)` | momentum decay |
| Velocity / Displacement clamp | gradient clipping / trust region |
| Mass saturation `m_max=50` | adaptive LR の自然減衰 |
| Thermal escape `1/(1+T×scale)` | 不確実性に応じた step 減衰 |
| Verlet 積分 (Stage 1-2-3) | symplectic optimizer / Heavy ball |

## 暗黙の Loss

```
U = - Σ_{i,j} cooccur(i,j) × G × m_i × m_j × cos_sim(virtual_pos_i, virtual_pos_j)   ← Hebbian 引力
    + (k/2) × Σ_i ||displacement_i||²                                                 ← L2 正則化
```

第 1 項は **word2vec の skip-gram** とも **対照学習の positive pair loss** とも同型。
第 2 項は **Elastic Weight Consolidation (EWC)** の Fisher なし版。

## 既存アルゴリズムとの位置関係

- **Heavy ball method** (Polyak 1964): 物理慣性球から発想された SGD → GaOTTT はその直系
- **Hebbian Learning** (Hebb 1949): 共起で重みが育つ → GaOTTT の mass + edge 形成と同型
- **Self-Organizing Maps** (Kohonen 1982): topology 保存的競合学習 → GaOTTT の co-occurrence 版
- **Word2vec** (Mikolov 2013): skip-gram で共起を内積最大化 → 同じ目的関数を online で
- **Adam** (Kingma & Ba 2014): adaptive LR → mass scaling と類似構造
- **Hamiltonian Monte Carlo**: leapfrog 積分 → GaOTTT の Stage 1-2-3 そのもの

## TTT としての独自性

| 項目 | 既存 TTT (Sun et al. 2020) | GaOTTT |
|---|---|---|
| 更新対象 | LLM 重み | retrieval gravity field |
| 学習信号 | self-supervised loss | co-occurrence statistics (Hebbian) |
| 1 step | gradient step | 物理 step (leapfrog) |
| 共有可能性 | 困難（重み単位） | 簡単（DB 単位） |
| Catastrophic forgetting | リスクあり | アンカー復元力で原理的に回避 |

## 物理法則 = 正則化の集合

GaOTTT の独自性は、**物理的に自然な制約が、機械学習的な regularization と一致** すること:

- Hooke アンカー = L2 weight decay
- Friction = momentum decay
- Mass 飽和 = adaptive LR 減衰
- Velocity clamp = gradient clipping
- Verlet 積分 = symplectic step（発散しない）

設計時に意図したかは別として、**良い物理 = 良い正則化** の例として整理できる。

## 開いている問い

1. 暗黙 loss の完全な書き下し（BH + thermal を含めて）
2. Lyapunov 関数による収束性の保証
3. Adam / SGD-momentum との empirical 比較
4. 共有メモリ TTT の理論的位置づけ（federated learning との対比）
5. catastrophic forgetting 不在の理論的証明

## 関連

- 完全版（参考文献あり）: [`docs/research/gravity-as-optimizer.md`](../research/gravity-as-optimizer.md)
- 実装側のスコア式: [Architecture — Gravity Model](Architecture-Gravity-Model.md)
- 設計時の物理アナロジー意図: [`docs/research/gravitational-displacement-design.md`](../research/gravitational-displacement-design.md)
- 共有メモリでの distributed TTT 観察: [Multi-Agent Experiment](Research-Multi-Agent-Experiment.md)
- 哲学的位置付け: [Five-Layer Philosophy](Reflections-Five-Layer-Philosophy.md)
