# Operations — Tuning Hyperparameters

`gaottt/config.py` の `GERConfig` を編集してサーバー再起動で反映。

すべてのハイパーパラメータの一次ソース: [`gaottt/config.py`](../../gaottt/config.py)

---

## スコアリング・質量

| パラメータ | 既定 | 影響 | 上げると | 下げると |
|---|---|---|---|---|
| alpha | 0.05 | mass boost の重み | 頻出ドキュメントを強く優先 | 類似度ベースに近づく |
| delta | 0.01 | 時間減衰の速さ | 古いアクセスが早く忘れられる | 長期間アクセスが維持 |
| gamma | 0.5 | temperature の感度 | ノイズが大きくなり探索的に | 安定的な検索結果 |
| eta | 0.05 | mass 増加速度 | 少ないクエリで重要度↑ | ゆっくり蓄積 |
| edge_threshold | 5 | 共起エッジ形成の閾値 | 強い共起のみエッジ化 | 弱い共起でもエッジ化 |
| top_k | 10 | 既定返却件数 | 多くの結果を返す | 上位のみに絞る |

## 重力変位

| パラメータ | 既定 | 影響 | 上げると | 下げると |
|---|---|---|---|---|
| gravity_G | 0.01 | 万有引力定数 | 急速に引き寄せ合う（創発的） | 穏やかな変位（安定） |
| gravity_eta | 0.005 | 変位の学習率 | 1 回のクエリでの変位↑ | 段階的に変位 |
| displacement_decay | 0.995 | 変位の定期減衰 | 変位が長く維持 | 早く元に戻る |
| max_displacement_norm | 0.3 | 変位の上限 | 探索的（遠くまで移動可能） | 原始位置から離れにくい（安全） |
| candidate_multiplier | 3 | FAISS 候補倍率 | 広い候補から選べる | 高速だが候補が狭い |

## 軌道力学

| パラメータ | 既定 | 影響 |
|---|---|---|
| orbital_friction | 0.05 | 速度の摩擦（毎ステップ） |
| orbital_max_velocity | 0.05 | 速度の上限ノルム |
| orbital_anchor_strength | 0.02 | アンカー復元力（Hooke's k） |

## 馴化・温度脱出

| パラメータ | 既定 | 影響 | 上げると | 下げると |
|---|---|---|---|---|
| saturation_rate | 0.2 | 返却飽和の速さ | 少ない返却で飽和（新鮮さ重視） | 何度も同じ結果（安定重視） |
| habituation_recovery_rate | 0.01 | 馴化からの回復速度 | 早く新鮮さ回復 | 長く飽和持続 |
| thermal_escape_scale | 5000 | 温度による BH 脱出効果 | 高温ノードが BH から脱出しやすい | 温度に関わらず束縛 |
| bh_mass_scale | 0.5 | BH 質量のスケーリング | BH 引力が強い（密なクラスタ） | BH 引力が弱い |

## 重力波伝播

| パラメータ | 既定 | 影響 |
|---|---|---|
| wave_initial_k | 3 | seed top-k |
| wave_max_depth | 2 | 再帰最大深度 |
| wave_attenuation | 0.7 | 深度ごとの減衰係数 |
| wave_mass_scale | 1.5 | mass 依存 top-k のスケール |

## TTL 短期記憶（F4 + Phase D）

| パラメータ | 既定 | 用途 |
|---|---|---|
| default_hypothesis_ttl_seconds | 7 日 | hypothesis ソース |
| default_task_ttl_seconds | 30 日 | task ソース |
| default_commitment_ttl_seconds | 14 日 | commitment ソース |

`remember(ttl_seconds=...)` / `commit(deadline_seconds=...)` で個別上書き可能。

## auto_remember（F1）

| パラメータ | 既定 | 影響 |
|---|---|---|
| auto_remember_default_max | 5 | 候補数の既定 |
| auto_remember_min_chars | 12 | 候補の最短文字数 |
| auto_remember_max_chars | 400 | 候補の最長文字数 |

## 情動・確信度（F7）

| パラメータ | 既定 | 影響 | 上げると | 下げると |
|---|---|---|---|---|
| emotion_alpha | 0.04 | \|emotion\| の boost 重み | 情動的記憶を強く優先 | 影響を抑える |
| certainty_alpha | 0.02 | certainty の boost 重み | 高確信度を強く優先 | 影響を抑える |
| certainty_half_life_seconds | 30 日 | 確信度の半減期 | 確信度が長く保たれる | 早く減衰、`revalidate` 推奨頻度↑ |

## バックグラウンド prefetch（F6）

| パラメータ | 既定 | 影響 |
|---|---|---|
| prefetch_cache_size | 64 | LRU エントリ上限 |
| prefetch_ttl_seconds | 90 | キャッシュ寿命 |
| prefetch_max_concurrent | 4 | 並列 prefetch 数 |

## Write-behind

| パラメータ | 既定 | 影響 |
|---|---|---|
| flush_interval_seconds | 5.0 | キャッシュ → DB の flush 間隔 |
| flush_threshold | 100 | dirty 件数による即時 flush 閾値 |

## Embedding

| パラメータ | 既定 | 用途 |
|---|---|---|
| model_name | `cl-nagoya/ruri-v3-310m` | Embedding モデル |
| embedding_dim | 768 | 次元数（モデル変更時は要連動） |
| batch_size | 32 | バッチエンコード時 |

---

## チューニングの典型シナリオ

### 「もっと探索的にしたい」

- `gamma` ↑（temperature が大きくなる）
- `gravity_G` ↑（引力が強い）
- `max_displacement_norm` ↑（遠くまで動ける）
- `wave_max_depth` ↑（広く伝播）

### 「もっと安定的にしたい」

- `saturation_rate` ↓（馴化を緩める）
- `gravity_G` ↓
- `thermal_escape_scale` ↓（温度脱出を抑える）

### 「タスクが消えやすすぎる」

- `default_task_ttl_seconds` を大きく（例 90 日）
- `default_commitment_ttl_seconds` を大きく（例 30 日）

### 「prefetch のヒット率を上げたい」

- `prefetch_ttl_seconds` ↑（90 → 300）
- `prefetch_cache_size` ↑

→ より広い文脈: [Operations — Troubleshooting](Operations-Troubleshooting.md)
