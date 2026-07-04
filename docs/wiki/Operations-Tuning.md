# Operations — Tuning Hyperparameters

`gaottt/config.py` の `GaOTTTConfig` を編集してサーバー再起動で反映。

すべてのハイパーパラメータの一次ソース: [`gaottt/config.py`](../../gaottt/config.py)

## 設定の優先順位 (H5)

スカラーなハイパーパラメータは以下の優先順 (上が勝つ) で解決される:

1. **環境変数 `GAOTTT_<FIELD>`** — フィールド名を大文字化 (例: `gamma` → `GAOTTT_GAMMA=0.8`、`mass_bh_theta` → `GAOTTT_MASS_BH_THETA=6.0`)。`config.json` を編集せず 1 つの knob だけ一時変更したいとき (例: Phase M Stage 2 の θ チューニング観測) に使う。`bool` は `1/true/yes/on` (大小無視) のみ True、それ以外 (`false`/`0`/空) は False — `bool("false")==True` の罠を回避。型不正な値は WARNING ログを出して無視 (起動は継続)。旧 `GER_RAG_<FIELD>` も deprecation 警告つきで受理。
2. **`config.json`** — `~/.config/gaottt/config.json` (Linux/macOS) / `%APPDATA%/gaottt/config.json` (Windows)。複数フィールドの恒久設定はこちら。
3. **dataclass 既定値** — `gaottt/config.py`。

> `data_dir` のような `field(default_factory=...)` / コレクション型は env 個別上書きの対象外 (`data_dir` は専用の `GAOTTT_DATA_DIR` で解決、config ファイルパスは `GAOTTT_CONFIG`)。

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
| max_displacement_norm | 1e6 | 変位の上限 (Phase I で実質 ∞ 化) | n/a (cap が事実上 off) | 小さい値で疑似的なハードキャップに戻せる（緊急ノブ）。**Phase Q orbit mode (`orbital_tick_enabled=True`) では `2.0` 程度の有限値が必須** — 近傍 1/r² 近接特異点の runaway backstop（下記「公転・閉軌道」節） |
| candidate_multiplier | 3 | FAISS 候補倍率 | 広い候補から選べる | 高速だが候補が狭い |

## 軌道力学

| パラメータ | 既定 | 影響 |
|---|---|---|
| orbital_friction | 0.05 | 速度の摩擦（毎ステップ） |
| orbital_friction_age_factor | 0.1 | 未アクセスノードへの追加摩擦（軌道 tick では強制 0） |
| orbital_max_velocity | 0.05 | 速度の上限ノルム |
| orbital_anchor_strength | 0.02 | アンカー復元力（Hooke's k） |

## 公転・閉軌道（Phase Q — Orbital Mechanics、2026-05-30）

ノードを **自分の anchor（原始 embedding）を中心に閉軌道（ロゼット）で公転** させる保存系レジーム。核心の発見: Hooke アンカー `F = -k·d` は **Bertrand の定理で閉軌道を生む 2 つの中心力の 1 つ（等方調和振動子）** なので、軌道化に足りないのは新しい力ではなく **接線速度（角運動量）だけ**。公転中心 = 自分の articulated self なので **anchor migration ゼロ**（衛星化・彗星脱出は公転中心が他者 = 線の外で、Phase Q は内側に留まる）。詳細: [Plans — Phase Q](Plans-Phase-Q-Orbital-Mechanics.md)。

| パラメータ | 既定 | 影響 | 上げると | 下げると |
|---|---|---|---|---|
| **orbital_tangential_alpha (α_t)** | **`0.0`** | seed 時に付与する接線速度の大きさ（radial 速度ノルムに対する倍率）。`compute_gravity_kick` と supernova seed で displacement（∥ gravity radial）と非共線な velocity を作り `L = d × v ≠ 0` にする | `0.5–1.0` で楕円〜円に近づく（角運動量 ↑、軌道が太る） | `0.0` で完全 no-op（直線往復、legacy bit-for-bit） |
| **orbital_integrator** | **`"euler"`** | `update_orbital_state` の積分法。`"verlet"` で velocity-Verlet（symplectic、O(dt²)、力 2 回評価）に切り替え、長時間の軌道安定性が向上 | — | `"euler"` で legacy semi-implicit Euler（production 不変） |
| **orbital_tick_enabled** | **`False`** | dream loop が毎サイクル軽量 `_orbital_tick` を回し、**lively なノード**（`\|v\| > orbital_lively_v_min`）だけを recall 抜きで積分（recall = エネルギー注入 / tick = 自由発展、の役割分離） | `True` で連続時計（自走公転・歳差・螺旋落下） | `False` で従来どおり recall 駆動のみ |
| orbital_lively_v_min | 0.001 | これ未満の `\|v\|` は "cold" として tick から除外 | 上げると tick 対象が減りコスト ↓（早く cold 化） | 下げると微速ノードも回り続ける |
| orbital_tick_max_nodes | 256 | tick 1 回あたりの処理上限（コスト bound）。超過分は次 tick へ繰り越し + ログ | 上げると 1 tick の網羅性 ↑（CPU 負荷 ↑） | 下げると安全側（大規模 lively set でも tick が軽い） |
| **orbital_tick_neighbor_gravity_enabled** | **`False`** | tick が **lively set を相互近傍として** 近傍重力を効かせるか。`False`（既定）= tick 内で `gravity_G=0` → **純 self-anchor 楕円公転**（各ノードが自分の x₀ を周回、β は有効） | `True` で結合（**本番 field では coherent 暴走、実験専用** — 下記★★） | `False` が安全側（推奨）。本番実測で確定した既定 |

> **★★ tick の近傍重力は既定 OFF（2026-05-30 本番実測の rollout finding）**: 本番 41K field の隔離コピー実測で、`_orbital_tick` が散在 lively set を相互近傍として渡す実装（plan §3.3 の per-node FAISS 近傍探索と乖離）は、RURI の狭 cosine 帯で近傍重力が **coherent に加算** し net `|a|` p50≈10/max≈640（単一ペア最大 0.7、1/r² 特異点ではない）vs anchor 0.005 → ~1000倍、displacement を clamp に張り付け self-limiting を殺すと判明。**純 self-anchor（G=0）は bounded + self-limiting で健全**。よって既定で tick 近傍重力を切る。`True` は tamed 近傍重力（per-node 真 FAISS 近傍 or 大幅減 G）の再設計（= rosette、future work）の実験用。詳細: [Plans — Phase Q §8](Plans-Phase-Q-Orbital-Mechanics.md#8-rollout-findings-2026-05-30-本番隔離コピー実測)。

> **★ orbit mode では `max_displacement_norm` を有限値に必須**: Phase I が `max_displacement_norm=1e6`（実質 ∞）にしたのは「Hooke + friction + velocity cap が自然均衡を作るから cap 不要」だったが、これは **近傍重力が穏やかな relax regime 限定**。接線速度入り orbit + 近傍の 1/r² 近接特異点は velocity clamp(0.05) では止まらない正味の外向きドリフトを生み、500 step で `\|d\|=26` まで発散する（Stage 3 で実測・特性化）。orbit regime の runaway backstop は **`max_displacement_norm` clamp そのもの** なので、`orbital_tick_enabled=True` のときは `2.0` 程度の有限値を設定する（`config.__post_init__` が `max_displacement_norm > 100.0` のとき警告を出す）。

> **チューニング助言 (Phase Q bundle、2026-05-30 本番実測で改訂)**: 公転を本番で有効化する推奨束は `orbital_tangential_alpha=0.5–1.0` / `orbital_integrator="verlet"` / `orbital_friction=0.005`（0.05 → 1/10、e-fold ~100 分で数十周後に井戸へ螺旋落下 = 熱力学的終末）/ `mass_anchor_extra_strength=1.0`（質量依存周期 — 重い星ほど緩い anchor・長周期・広い軌道、**Kepler 第3法則ではなく** 周回 star 自身の質量がバネ定数を決める調和振動子）/ `max_displacement_norm=2.0`（上記必須）/ `orbital_tick_enabled=True` / **`orbital_tick_neighbor_gravity_enabled=False`（上記★★ より必須 — 純 self-anchor 公転）**。すべて default OFF（`α_t=0` で bit-for-bit rollback）なので、**measurement-first** で env opt-in（`GAOTTT_ORBITAL_TANGENTIAL_ALPHA=0.5` 等）→ 1–2 週観測 → `tests/perf/test_tier4_phase_q_orbital.py` で displacement 分布を見て確定、の運用 pattern を踏む。本番投入前に DB backup + 他 MCP/REST プロセス停止（write-behind 上書き罠）。本番 velocity field は飽和（median `|v|=0.05`）だが、純 self-anchor 公転下では gentle に settle する（bounded、clamp 不到達）ので強制 cool-down は不要。

## 重力スケール — 近傍重力 governor（Phase Q2 — Gravitational Scale、2026-05-31）

密な corpus では近傍重力ベクトルが RURI の狭 cosine 帯で **coherent に加算**（coherence ~0.8-1.0、net∝N）し、anchor 復元力の ~10⁴-10⁵倍・重い裾（ratio p90~10⁵）になる。単一の大域 `gravity_G` ではこの裾を飼えないので、per-node で attractive 近傍力（近傍重力 + mass-BH）を `g_i = min(1, α · k_eff(m_i) · max(‖d_i‖, d_floor) / ‖acc_neigh‖)` で cap する（密度適応オートゲイン、方向は保存、anchor / query 引力 / Λ は cap しない、source 分岐ゼロ）。詳細: [Plans — Phase Q2](Plans-Phase-Q2-Gravitational-Scale.md)。

| パラメータ | 既定 | 説明 |
|---|---|---|
| **gravity_neighbor_governor_enabled** | **`True`** | 近傍重力 governor。**2026-05-31 に default ON へ昇格**（段階4 acceptance + 本番 live healthy を承けた owner 判断、規約「新 field は default OFF」からの意図的 promotion）。`False` で bit-exact pre-Q2 legacy |
| gravity_neighbor_governor_alpha (α) | 0.2 | cap 目標 = α × anchor 力スケール。query 引力の effective 学習率を実質決める（governor が近傍重力を抑えると query 引力項が un-mask される） |
| gravity_neighbor_governor_disp_floor | 0.1 | `‖d‖` の床。anchor 直近（d≈0）でも cap ref が 0 にならないようにする |

> **チューニング助言**: `α` が肝。**上げる**と近傍重力の許容量が増え（cluster 結合が強まる）query 引力の effective 学習率は相対的に下がる、**下げる**と近傍重力をより強く抑え query 引力ドリフトが効く（段階4 実測: ranking は単一クエリで中立だが、recall を重ねた drift が α 依存で OFF 0.018 → ON 0.832）。`0.2` は保守的初期値。**measurement-first**: 1-2 週観測して「query 方向ドリフトが relevance を改善するか / しすぎないか」を見て調整、env `GAOTTT_GRAVITY_NEIGHBOR_GOVERNOR_ALPHA=0.3` 等で JSON 編集なしに動かせる。velocity 飽和が起きている既存 DB は M006 cooldown（[Operations — Migration](Operations-Migration.md) §Phase Q2）とセットで投入。pre-Q2 挙動が要るときだけ `GAOTTT_GRAVITY_NEIGHBOR_GOVERNOR_ENABLED=false`。

## Query 引力（Phase I — Stage 2 + Stage 3 + Stage 4）

`compute_acceleration` の 2 番目と 4 番目の項。recall 時に retrieved nodes へ query 方向の小さな引力 (kick、4 項目) を加える一方、anchor (raw embedding) への復元力 (Hooke、2 項目) を低 mass で増幅する (Stage 4)。`F_kick = α · score · gate · (q - pos)`, `a_kick = F_kick / m_i` で **mass damping** が自動で効く (BH 化 node はほぼ動かない)。**Stage 3** では `gate = tanh(m_i / θ)` で新規 (低 mass) ノードが anchor に守られる — 単一アトラクタ pathology の防止策。**Stage 4** はその対称形 — Hooke の effective k を `k · (1 + β · (1 - tanh(m / θ)))` に拡張し、軽い星を anchor 側からも守る。**transient force** — Hooke が raw embedding を anchor として引き続き保持するので anchor migration ではない。詳細: [Plans — Phase I](Plans-Phase-I-Free-Star-Movement.md) §Stage 2-4。

| パラメータ | 既定 | 影響 | 上げると | 下げると |
|---|---|---|---|---|
| query_kick_strength | 0.01 | 結合定数 α (G に類似) | recall ごとの drift ↑（短期で query 方向に集まる） | drift が緩慢、長期累積でしか効かない。`0` で完全 no-op (roll-back) |
| query_kick_enabled | `True` | グローバル off スイッチ | n/a | `False` で 4 項目を完全 skip (config 即時 off) |
| mass_anchor_threshold (θ) | 3.0 | Stage 3/4 の gate 特徴点 (`tanh(1)≈0.76` が ここ)。kick と Hooke で共有 | 攻撃的 (`θ=1` → 新規 m=1 で gate=0.76、ほぼ満額)。新規ノードの drift 即時化 | 保守的 (`θ=10` → 新規 m=1 で gate=0.10、ほぼ動かない)。`0` で Stage 2 へ rollback (gate=1.0 強制、Stage 4 も `θ_eff=1.0` の安全 fallback) |
| **mass_anchor_extra_strength (β)** | **`0.0`** | **Stage 4 — 低 mass 系の Hooke 増幅倍率** | `1.0` で m=1 ノード anchor 1.7×、`2.0` で 2.4×。低 mass 系の displacement 均衡点が下がる、軽い星の drift が更に抑制される | `0.0` で完全 no-op (Stage 1-3 へ rollback、bit-for-bit) |

> **チューニング助言 (Stage 3 kick)**: per-step acceleration は `orbital_max_velocity=0.05` で cap されるので、`α / m × score × gate × \|q-pos\|` が ~0.05 を超えると効きが頭打ち。質量 1 の新規 node + score=1 + |q-pos|=1.4 (unit-norm 直交) + θ=3 で gate=0.32 → α=0.11 が cap 境界 (Stage 2 単体の 0.035 から余裕拡大)。`α=0.01` (既定) は安全側、`mass_anchor_threshold=3.0` で **新規ノードは ~32%、mature ノード (mass≥10) はほぼ満額** という世代論的挙動。pathology が再発したら θ を上げる、新規ノードの surface が遅すぎたら θ を下げる。
>
> **チューニング助言 (Stage 4 Hooke β)**: 既定 `β=0` は opt-in 安全側 — `mass_anchor_threshold` を 1-2 週間運用した上で本番 `tests/perf/test_tier4_*.py` で displacement 分布を見て活性化を判断。低 mass 系 (`mass≤2`) の `|d|` p99 が anchor 側からまだ過剰に見えるなら β=1 を試す (anchor 1.7× 増、mature 系は影響ほぼ無し)。**θ は Stage 3 と共有** なので、両者を別々に持つ場合は Stage 5 候補 (`source-aware θ` と並列でロードマップ)。`β` を変えた後の検証は `scripts/perf_baseline.py --label "stage4-β1"` で before/after baseline 取って `perf_diff.py` で 25% gate を超えないか確認するのが運用 pattern。

## 馴化・温度脱出

| パラメータ | 既定 | 影響 | 上げると | 下げると |
|---|---|---|---|---|
| saturation_rate | 0.2 | 返却飽和の速さ | 少ない返却で飽和（新鮮さ重視） | 何度も同じ結果（安定重視） |
| habituation_recovery_rate | 0.01 | 馴化からの回復速度 | 早く新鮮さ回復 | 長く飽和持続 |
| thermal_escape_scale | 5000 | （Phase M で deprecated）共起 BH の温度脱出効果。`compute_acceleration` から呼び出し削除済、`scripts/visualize_3d.py` 互換のため定義は残存 |
| bh_mass_scale | 0.5 | （Phase M で deprecated）共起 BH 質量スケーリング。同上 |

## TTT Observability (Phase O Stage 1)

`recall` / `explore` のレスポンス各 item に additive な `score_breakdown` を付与する。`expected_sum = (virtual_cosine · decay_factor + wave_score + mass_boost + emotion_term + certainty_term) × saturation` で `final_score` を literal に再現できる。詳細: [Plans — Phase O](Plans-Phase-O-TTT-Observability.md)、[MCP-Reference-Memory.md](MCP-Reference-Memory.md)、[REST-API-Reference.md](REST-API-Reference.md)。

| パラメータ | 既定 | 影響 |
|---|---|---|
| expose_score_breakdown | `True` | each QueryResultItem に `ScoreBreakdown` を attach。`False` で `None` を返す (legacy 互換、context 数 byte 節約) |
| training_delta_enabled | `True` | recall/explore response に `TrainingDelta` (state 変化 trailer) を attach。`False` で `None` を返す (legacy 互換) |
| training_delta_topk_only | `True` | delta dicts (`displacement_changes` / `mass_changes`) を top-K 結果の node のみに限定 (context 経済)。`False` で全 reached node を含める (debug / observability mode) |
| auto_route_enabled | `True` | recall/explore の query 形式が構造化 aspect に match したら `reflect` を並走実行して `routing_hint.reflect_summary` に attach。`False` で完全 off (legacy 自由文 recall のみ) |
| list_mode_excerpt_chars | `80` | Phase O Stage 4 — `recall(mode='list')` で各結果の `content` を切り詰める文字数 (改行は空白に置換)。下げれば context 節約、上げれば 1 件あたりの情報量増。20 で 1 行 ~12 件相当、80 で 1 行 ~3 件相当 (typical agent memo 240 字 ≈ 3 行) |
| dormant_age_threshold_seconds | `7 × 86400` (7 日) | Phase O Stage 5 — `explore(mode='dormant')` で「忘れた」と見なす `last_access` cutoff。短くすると最近のものまで surface、長くすると本当に枯れた node のみ。**2026-06-01 に 30d → 7d へ既定昇格** — active-user corpus では 30d gate が pre-filter を 0 件にし dormant lens が死ぬ (実測: 41k corpus で age=30d→0 候補 / age=7d→p10 で 8 候補)。元々は env `GAOTTT_DORMANT_AGE_THRESHOLD_SECONDS=604800` で 7d にする想定だったが、proxy 共有 backend が per-frontend env を継承しない罠 (`project_proxy_backend_env_not_delivered`) で env が engine に届かず、GLM acceptance (handover-2026-06-01 §4.3 / evaluation-2026-05-27 §3.4) で 2 度 0 件が再発 → env 非依存にするため code default 化 |
| dormant_mass_threshold | `2.0` | Stage 5 mass 上限 — mature gate point (`gate=tanh(m/3) ≈ 0.58 で 1.0 飽和の 58%`) と整合。これを超えた node は「raw cosine 弱者として埋もれてる」状態ではないので dormant 対象外 |
| dormant_source_classes | `("agent", "value", "intention", "commitment", "note", "reference", "exploration-report", "compaction")` | Stage 5 — surface 対象の **structural identifier 列挙**。Phase D 系 (value/intention/commitment) + agent/note/reference + exploration-report (self-authored 化石レポート) + compaction (context compression 層)。tweet / file / hypothesis は除外 (これらは自己発信ではない) |
| dormant_mass_percentile | `10.0` | **Lateral Association Stage 7.2 (2026-05-26、暫定デフォルト active 2026-05-26)**: 分布相対の mass cut。設定すると `dormant_mass_threshold` の絶対値 (`2.0`) を捨て、active corpus mass の P パーセンタイル値を cut として使う。`None` で legacy 絶対値挙動 rollback。production で「26k memo の mass 分布が上振れし `≤ 2.0` が常に 0 件」となる失敗 (`project_phase_o_stage_5_production_observation`) への対処として `10.0` をデフォルト化。本番チューニングは `scripts/diag_dormant.py --data-dir <prod>` で「~5-15 dormant 候補が surface する最小パーセンタイル」を採用 (典型は 10-30 の範囲)。**重要**: パーセンタイルは `dormant_age_threshold_seconds` の age gate と併用して初めて効く — age が厳しすぎると pre-filter が 0 件になり percentile cut の手前で全滅する。この理由で age 既定は 2026-06-01 に 30d → 7d へ昇格した (上行参照、env 非依存化)。診断は `scripts/diag_dormant.py --age-days 7` で age gate を合わせて見ること |
| recall_trailer_verbose_modes | `"detail,full"` | Instruction Surface Hygiene Stage 2b — breakdown 行と `## 訓練差分` trailer を出力する output_mode の comma 区切り。compact/ids/list では抑制 |
| recall_reason_line_modes | `"detail,full,compact,ids,list"` | Observation Apparatus Round 2 Stage B — `reason:` 1 行を出力する output_mode。breakdown/trailer と独立に制御。全 mode が既定なので triage 中でも dominance artifact 警告が見える |

> **チューニング助言 (Stage 1)**: 既定 `True` のまま運用推奨。breakdown は scoring loop と同じパスで構築されるので overhead は無視可能 (BM25 hit set 算出に 1 query × O(n) 程度)、context payload も 1 item あたり ~140 byte 増のみ。LLM caller (Claude / agent) が `breakdown.raw_cosine` を見て「semantic 弱いのに mass で勝った結果」を自律的に弾けるようになるので、Sonnet 本番 acceptance で観察された "score deception" 系の罠を機構レベルで防げる。`False` にする状況は (a) extreme low-context client (b) breakdown 表示で混乱する non-TTT-aware caller 向け fallback (c) emergency rollback の 3 ケースのみ。

> **チューニング助言 (Stage 2)**: 既定 `True` + `topk_only=True` のまま運用推奨。delta capture は `_update_simulation` の前後で `displacement_norm` + `mass` を snapshot するだけなので overhead は O(top_k) で無視可能、payload も top_k=5 で ~250 byte 増程度。`topk_only=False` は **デバッグ専用** — `wave_initial_k × max_depth^depth` の reached node 全件 (大規模 DB で数百件) が delta dict に乗るので context を消費する。「mass が累積していく感覚」を LLM caller が掴めるのは Hebbian deliberate rehearsal の literal な前提条件、`cache_hit=True` の trailer は「自分が field を訓練できる時 / できない時」の境界を明示する効果。`training_delta_enabled=False` は emergency rollback 用、`expose_score_breakdown` と同じ判断基準。

> **チューニング助言 (Stage 3)**: 既定 `True` のまま運用推奨。classifier 自体は O(patterns) 個の compiled regex を 1 query に走らせるだけ (現在 ~15 pattern、< 10 µs)、pattern が match した場合のみ並走で `reflect` aspect 1 件を走らせる。`reflect` aspect は最大で `cache.get_all_nodes()` を一巡 + `store.get_document()` を最大 limit=10 回叩く程度なので 1k node 規模で数 ms、22k node 規模でも 100ms 以下。`False` にする状況は (a) classifier の誤 routing が頻発する environment (現状の Japanese + English pattern では未観測) (b) 並走で別 path を走らせたくない low-latency 用途 (c) reflect summary が context 圧迫する extreme tight loop。per-call の `auto_route=False` で test / 一時診断にも対応。**Phase M の「source 分岐ゼロの単一規則」を侵さない**: pattern は caller の query 形式 (surface form) を classify するだけで、physics rule (mass update / Hooke / kick) は一切触らない — query intent layer の routing。

> **チューニング助言 (Stage 4)**: `list_mode_excerpt_chars=80` で 1 行に収まる切り詰めが既定。`recall(mode='list', top_k=20)` の典型 payload は ~1.8 KB (header + breakdown + 80字 excerpt × 20)、`mode='detail'` の同 top_k=20 は agent memo 平均 240字 × 20 = ~5 KB 越。Context-tight な caller 程効く。**下げる**: 20 まで下げると tabular scan に近付くが、source-class を見ない caller には情報量不足。**上げる**: 120-200 で「だいたい何の話か分かる」レベルになるが、200 を超えると `output_mode="compact"` (300字 trunc) との差が薄れる。**意思決定の経路**: list-mode で id 列見て興味あれば `recall(query=..., top_k=1, mode="detail")` で深掘り、という 2-step pattern が **caller の認知負荷を減らす** Phase O Stage 4 の本来意図。MCP `output_mode` (formatter 側 truncate) との差: Stage 4 の `mode='list'` は **service 層** で truncate するので REST にも同じ payload が乗る (wire 上の context 経済)、`output_mode` は MCP 文字列のみ。

> **チューニング助言 (Stage 5)**: dormant は **物理機構ではない operation** — counter-importance sampling で「重力場 (mass + raw cosine) が surface しなくなった自己発信 memo」を意図的に蘇生する別経路。`dormant_age_threshold_seconds` を **短くしすぎる** と recent な低 mass node まで surface してしまい dormant の意味が希薄に (`reflect(aspect="dormant")` と差が薄れる)、**長すぎる** と本当に枯れた node が出ない (3 ヶ月以上未参照は実用上稀)。30 日が「session 内で意識から落ちる時間スケール」と整合。`dormant_mass_threshold=2.0` は **mass-gated kick `gate=tanh(m/3.0)`** の 58% 飽和点 — これを超える node は既に gravity 場で十分動く資格があるので「埋もれてる」と見なさない。`dormant_source_classes` への追加は慎重に — Phase D 人格層 + agent/note/reference は **「私が能動的に書いた memo」** という structural class で、これに `file` (受動 ingest) を混ぜると counter-importance が「忘れた自己発信」から「忘れた素材」に意味がぼやける。**設計判断: source 列挙は physics 違反ではない** — Phase M の単一規則は physics rule (mass update / Hooke / kick) に対する制約、Stage 5 の列挙は query intent (どの class を surface するか) に対する filter — physics は一切触らない。詳細: [Plans — Phase O §Stage 5 設計判断](Plans-Phase-O-TTT-Observability.md)。

## Mass Conservation + mass-based BH (Phase M Stage 1)

`engine._update_simulation` の mass update が **「外部 (`original_id` / `cohort_id` 一致しない parent) からの引力寄与のみで増える」** 規則に切り替わった。`compute_acceleration` 第 3 項の BH 引力も共起 cluster centroid 方式から **「mass しきい値を超えた neighbor からの直接引力」** に置き換え。詳細: [Plans — Phase M](Plans-Phase-M-Mass-Conservation.md)。

| パラメータ | 既定 | 影響 |
|---|---|---|
| mass_conservation_enabled | `True` | self-force filter の on/off。`False` で legacy「内輪取引も mass を生む」挙動に rollback |
| mass_bh_enabled | `True` | mass-based BH attractor の on/off。`False` で第 3 項を完全に切る (neighbor gravity + anchor + query attraction のみ) |
| mass_bh_theta (θ) | 5.0 | BH attractor 発動しきい値 — `bh_factor(m) = tanh((m-θ)/σ)`、`m ≤ θ-2σ` でクランプ 0 |
| mass_bh_sigma (σ) | 1.5 | tanh 遷移幅 — 小さいほど θ 付近で急峻な on/off、大きいほどなだらか |

> **本番ロールアウト手順**: 旧規則下で蓄積した chunk 内輪取引 inflation を一度ゼロにしてから新規則で観察する。(1) 他 MCP / REST プロセス停止 → (2) DB backup → (3) `.venv/bin/python scripts/reset_masses.py --apply` → (4) サーバー再起動 → (5) 1-2 週の自然蓄積を観測 → (6) `p99 mass` を θ に、`(p99.9 - p99)/2` を σ にして Stage 2 で確定。`reset_masses` は **MCP に非露出** (LLM 用途なし)、REST `POST /admin/reset_masses` のみ。

> **チューニング助言**: 新規 `remember` の original_id は `node_id` 自身 (自己一致なので自己フィルタの影響 0)。file ingest (`scripts/load_files.py`) は `original_id = file_path` で chunk 群を共通グループ化。Phase K supernova batch は `cohort_id = uuid4().hex[:12]` を共有 — `cohort_id` が **同じ** node 同士の force は mass update に寄与しない (Articulation as Carrier の literal な物理実装)。`mass_bh_theta` を下げ過ぎると低 mass node も attractor 化 → homogenization リスク。上げ過ぎると 1-2 週観測しても BH が発生しない (期待 1.7-5% の節点が θ 超え)。Stage 1 暫定 θ=5.0 は旧規則下の p99=26.5 から「新規則下は inflation が消えるので θ は大幅に下がる」前提の placeholder。

## Mass Evaporation (Phase N — β 確定、Stage 1.5 本番 opt-in)

Phase M 「自己関与は mass を生まない」(入力側) の対称形として「使われない mass は時間で蒸発する」(出力側) を物理化。単一規則: `mass -= ε · max(mass - floor, 0)^β · (t_idle / τ_idle)^γ`、`mass > floor AND t_idle > τ_grace` のとき。source 分岐ゼロ (Phase M と整合)、`evaporate_mass` 純粋関数。詳細: [Plans — Phase N candidate β](Plans-Phase-N-Mass-Evaporation.md)。

| パラメータ | 既定 | 影響 |
|---|---|---|
| mass_evaporation_enabled | `False` | Stage 1 は merge 安全のため default OFF。本番は env `GAOTTT_MASS_EVAPORATION_ENABLED=1` で opt-in (Stage 1.5 起動済 2026-05-26、123.92 mass drained / `scripts/diag_pressure.py` dry-run 予測との一致 99.9%) |
| mass_evaporation_floor (M_floor) | 1.0 | 初期質量。下まで decay しない (新規ノード保護)。変更非推奨 (Phase G 初期化と一致)。|
| mass_evaporation_grace_seconds (τ_grace) | 7d = 604800 | recall 直後の即時 decay 抑止窓。短くすると aggressive、長くすると "使ったらしばらく守られる" 挙動。|
| mass_evaporation_idle_normalize_seconds (τ_idle) | 30d = 2592000 | 1 単位の decay が起こる基準 idle 期間。`t_idle/τ_idle` の正規化。 |
| mass_evaporation_rate (ε) | 0.01 | excess^β · idle_ratio^γ にかかる係数。Stage 1 placeholder、観測で 1 桁単位調整。 |
| mass_evaporation_mass_exponent (β) | 1.5 | mass 増幅。1.0 = excess 比例、2.0 = excess² で hub に強く効く。 |
| mass_evaporation_time_exponent (γ) | 1.0 | time 増幅。1.0 = linear、>1 で長期 idle に急峻。 |
| mass_evaporation_eager_cron_seconds | 0.0 | Stage 2 用 — `>0` で background sweep cron loop を起動。Stage 1 は lazy + startup sweep のみ。 |

> **D2=C hybrid 評価**: lazy 経路は `engine._update_simulation` 内 Hebbian 更新の直前で 1 回呼び (touch 時に dt 補正)、startup 経路は `engine.startup` 末尾で全 active node に 1 回適用 (engine 停止中の "cold-start mass debt" を一括清算)。lazy だけだと touch されないノードが永遠に stale になるので両方必要。Idempotent: 同じ `last_access` を基準にする限り再実行で同じ結果。

> **本番ロールアウト手順 (Stage 1.5、Phase M Stage 2 後)**: (1) `scripts/mass_distribution.py` (要新設) で production の p50/p90/p99 mass を測定 → (2) `ε / β / γ / τ_idle` の本決め → (3) 他 MCP / REST プロセス停止 → (4) DB backup → (5) `mass_evaporation_enabled=True` で engine 起動 → startup sweep で cold-start debt 一括清算 → (6) 1-2 週 monitor して `hot_topics` 上位陣の入れ替わり / Phase O Stage 5 dormant の母集団復元 / 新規 `agent` memo の top1 取得確率 を観測。仮説 4 件は [Plans — Phase N β §5](Plans-Phase-N-Mass-Evaporation.md#5-副次予測--検証可能な仮説) 参照。

> **チューニング助言**: `ε=0.01, β=1.5, γ=1.0` の初期値 sanity check — `mass=5.0` で 30d idle なら decay = `0.01 · 4^1.5 · 1 = 0.08` → 5.0 → 4.92 (∝ 1.6% loss)。`mass=10.0` で 30d idle なら decay = `0.01 · 9^1.5 = 0.27` → 10.0 → 9.73 (∝ 2.7% loss)。`mass=2.0` で 30d idle なら decay = `0.01 · 1^1.5 = 0.01` → 2.0 → 1.99 (塵レベルはほぼ不変)。Stage 1.5 で本番 mass 分布測定後、p99 が 1-2 週で 10% 程度 evaporate するレートに ε を調整するのが現在の目安。

## Pressure Terms (Phase P — Cosmological Λ + Langevin Temperature)

Phase M (mass 増の単一規則、入力側) + Phase N (mass 減の単一規則、時間側) に続く「gravity への対抗 pressure」第 3 法則。**Λ** (P-α) は長距離斥力 (Hubble flow 類比) を `compute_acceleration` に加算、**Langevin** (P-β) は熱的揺らぎ (SGLD 類比) を position-update step に加算。Stage 7.1 anti-hub の scope 外として残った **individual-node high-mass dominance** (`ffe48a30` 等 singleton hub) を、ranking 層ではなく acceleration / displacement step で構造的に押し返す第 3 法則。両 knob は数学的に直交、両 default OFF、本番 opt-in は Phase N β Stage 1.5 完了 + 1-2 週観測後。詳細: [Plans — Phase P](Plans-Phase-P-Pressure-Terms.md)。

| パラメータ | 既定 | 影響 |
|---|---|---|
| cosmological_lambda_enabled | `False` | P-α — Λ 項の on/off。`True` で `compute_acceleration` の 5 番目の項として加算、`a_Λ(i) += +H · (pos_i - pos_j)` を wave neighbor scope の全 pair に適用 |
| cosmological_lambda_h | `0.001` | Hubble 定数 H (`gravity_G=0.01` の 1/10)。典型 displacement (‖Δ‖~0.5) で `a_Λ ~ 5e-4` (gravity `a_grav ~ 5e-3` の ~10%)。上げると long-range 斥力が強く dense cluster が分解しやすい、下げると Λ がほぼ no-op |
| langevin_temperature_enabled | `False` | P-β — Langevin 項の on/off。`True` で `position` 更新時に `√(2·T·dt)·ξ` (`ξ~N(0,I)`) を加算 |
| langevin_temperature_t0 | `0.001` | 温度定数 T₀。`σ = √(2·T₀) ≈ 0.045` per step (L2)、Hooke 平衡 `(G·m/k)^(1/3) ≈ 0.8` の ~5%。上げると singleton hub から escape しやすい代わりに健全な cluster も noisy に、下げると no-op |

> **チューニング助言 (P-α Λ)**: Λ は **gravity と filter 共有** ([Plan §3.1](Plans-Phase-P-Pressure-Terms.md))、neighbor scope (`propagate_gravity_wave` が渡す) を gravity と共有する。Self-force filter (Phase M `is_self_force`) は Λ には適用されない (Λ は mass を生まない、distance の関数のみ)。本番 opt-in 前に `scripts/diag_pressure.py --enable lambda --h <値>` で dry-run projection (mass / displacement 分布の予測) を取って観測、`p99 displacement` が現状から 20% 以内なら安全側。`h=0.001` 既定は保守的、`0.005` 程度まで実機検証可能と試算。

> **チューニング助言 (P-β Langevin)**: Langevin は legacy `state.temperature` (read-time noise、measurement side) とは **別経路** — write-time noise (dynamics side) で、SGLD (Welling-Teh 2011) の literal な実装。1 つの singleton high-mass attractor (Stage 7.1 limit、Phase N β でも届かない領域) が周囲を完全に支配する状況で、確率的 escape を加える。`T₀=0.001` は escape Boltzmann factor `exp(-ΔE/T)` で「mass 5 の well に mass 1 の node が捕まる」典型シナリオを 1 週間スケールで 1-2 回 escape させる目安。上げる前に `scripts/diag_pressure.py --enable langevin --t0 <値>` で displacement 分布の RMS 変化を確認、本番 opt-in は Phase N β 観測が落ち着いてから。

> **両者の関係**: P-α (Λ) は **deterministic** な長距離斥力 (時間平均的に cluster 構造が緩む)、P-β (Langevin) は **stochastic** な per-step ゆらぎ (個別 node が確率的に hop)。**両方有効** にしても干渉せず — Λ は acceleration loop、Langevin は post-acceleration position update に作用。両方とも default OFF で merge 済、env で個別に `GAOTTT_COSMOLOGICAL_LAMBDA_ENABLED=1` / `GAOTTT_LANGEVIN_TEMPERATURE_ENABLED=1` opt-in。

## Ambient Recall Enrichment

`ambient_recall` ツール（[Claude Code フックが毎ターン呼ぶ](Guides-Ambient-Recall.md)）の構造化 passive recall を制御。詳細は [Plans — Ambient Recall Enrichment](Plans-Ambient-Recall-Enrichment.md)。

| パラメータ | 既定 | 影響 |
|---|---|---|
| `ambient_gate_use_bm25` | `True` | relevance gate のモード。`True`=語単位 BM25「強一致」gate（主）、`False`=`virtual_score` gate（旧、分離不能）。gate index 不在時は自動でフォールバック |
| `ambient_gate_tokenizer` | `"sudachi"` | gate 専用 BM25 index のトークナイザ。語単位（Sudachi）が必須 — char-3gram は日本語の共通形態素を拾いすぎて分離できない。`bm25-sudachi` extra (`uv pip install -e '.[bm25-sudachi]'`) が要る。未導入なら gate index は構築されず virtual_score へフォールバック |
| `ambient_bm25_min_score` | `32.0` | **語 BM25「強一致」gate** のしきい値 — top-1 BM25 スコアがこれ未満なら応答を空に。32k コーパス校正 (2026-05-21、4 ラウンド): off-topic ≤~29 / 強い on-topic ≥~34 の谷。コーパスに真の off-topic は無く（生活+仕事まるごと）、gate は「強一致 vs 弱一致」を分ける高精度・低再現。**コーパス規模・クエリ長依存**、増えたら再校正 |
| `ambient_min_score` | `0.70` | **フォールバック** `virtual_score` gate のしきい値（`ambient_gate_use_bm25=False` または gate index 不在時のみ）。候補プールの最大 `virtual_score` 未満で空に。分離は弱い |
| `ambient_excerpt_chars` | `240` | 各スロットの content 抜粋長 |
| `ambient_lensing_enabled` | `True` | ② 重力レンズ枠（`virtual_cosine − raw_cosine` gap 最大の記憶）の on/off |
| `ambient_lensing_min_score` | `0.5` | レンズ枠の noise floor — pick の `virtual_cosine` がこれ未満なら採用しない（単なる外れ値の除外） |
| `ambient_lensing_min_gap` | `0.05` | レンズ枠の最小 gap — 「場が曲げた」と言える最小の `virtual − raw` 差 |
| `ambient_lensing_max_k` | `2` | **Lateral Association Stage 3**: lensing slot の **top-K cap**。`1`=Stage 1/2 完全互換 (旧挙動、ロールバック路)、`2`=既定 (controlled increase、ambient block +1 行 / ~+15% token)、`3`=最大推奨。各 pick は独立に `min_score`/`min_gap` を clear する必要あり (quota 緩和なし)、ranking は novelty 適用後の decayed gap で取り直す。複数の lateral 連想 ("X といえば Y で、Y といえば Z") を 1 turn 同時発火させる「〇〇といえば〜だったよな」の literal な拡張 |
| `ambient_lensing_dynamic_k` | `False` | **Lateral Association Stage 3**: 動的 K mode。`True` で query 抽象度 (= 到達ノードの raw_cosine 分散) に応じて `[1, ambient_lensing_max_k]` で K が浮動。Stage 3 では opt-in のみ (Stage 6a corpus で lateral hit rate を測ってから enable する想定、未実装の予約 knob) |
| `ambient_lensing_resonance_scale` | `10.0` | **Lateral Association Stage 5**: lensing resonance signal の saturation 定数。`resonance = raw / (raw + scale)` の `scale` で、`raw=10` で `resonance=0.5`、`raw=90` で `0.9` (常に `[0,1)`)。`raw = Σ_{d∈direct} cache.get_neighbors(lensing)[d]` = 「過去 active recall で direct hits と一緒に引かれた回数の合計」。`scale=5` で半飽和点を下げ短い履歴でも resonance を立てる、`scale=20` で長い履歴を要求する。`scale=0` は degenerate "any cooccurrence = full trust" mode (test 用) |
| `ambient_lensing_resonance_min` | `0.0` | **Lateral Association Stage 5**: 既定 `0.0` = フィルタなし (resonance を agent 可視に出すだけ)。`>0.0` で「resonance がこれ未満の lensing pick を drop」optional gate。drop された分の backfill はない (Stage 3 と同じ no-quota-relaxation 原則)。production 観察で resonance の自然分布を取ってから raise する想定 (典型値 ~0.1-0.3 で純 noise pick を落とす) |
| `ambient_reasoning_enabled` | `True` | ④ `derived_from`/`supersedes` 親を `because` として添える |
| `ambient_tension_enabled` | `True` | ⑤ `contradicts` ペアを矛盾フラグとして surface |
| `ambient_persona_enabled` | `True` | ⑥ active な declared value/intention を 1 行 grounding |
| `ambient_persona_pool_size` | `10` | **Refinement Stage 1**: persona slot を query 関連で再ランクする際の候補プール上限 (mass 上位 N)。N を小さくすると低コスト・低再現、大きくすると候補広いが per-call cosine 計算と FAISS get_vectors が線形に増える |
| `ambient_persona_min_relevance` | `0.65` | **Refinement Stage 1**: persona slot を surface する cosine しきい値。最高スコア候補の `cos(query, persona_vec)` がこれ未満なら slot を空に。Phase A literal 失敗 (MCP-smoke intention が embedder 議論に乱入) を防ぐためのガード。**2026-07-02 に 0.5 → 0.65 へ昇格** — dense 日本語 embeddings は query が単にキーワードを共有するだけでも cos ≈ 0.52 を与え、旧 0.5 floor を slip して heavy persona が surface し続ける観察 (harakiriworks dominance) への対処。0 に下げると常に top1 surface (旧挙動相当)、上げすぎると onboarding 期で永久空になりやすい |
| `ambient_persona_mass_weight` | `0.3` | **Refinement follow-up (b) — Heavy Persona Dominance 対処**: ranking 式 `score = (mass ** w) × cos(query, persona_vec)` の指数。`0.0`=cos のみ (`relevance_dominant` 相当)、`0.3`=既定 (2026-07-02 昇格、`mass^0.3 × cos` で runaway-mass persona を抑制しつつ mass は効かせる)、`0.5`=`sqrt(mass) × cos` (log-scale 相当の抑制)、`1.0`=Stage 1 完全互換 (rollback path、旧既定)。production の heavy persona (mass=2.82 等) が query 横断で persona slot を独占する現象への knob。**昇格経緯**: env opt-in で w=0.3 単独では不十分 (cos ≈ 0.52 が min_relevance=0.5 を通過) と判明し、`ambient_persona_min_relevance` の 0.65 昇格と同時に code default 化 (measurement-first を skip したことは ToDo 6-7 に明記、事後 baseline 測定を推奨) |
| `ambient_novelty_decay` | `0.7` | **Lateral Association Stage 1**: session-aware novelty decay の指数。`{node_id: count}` (フックが過去 N turn の `<!-- ambient-ids ... -->` manifest から組み立てて forward する `recently_surfaced` arg) に対し各スロット ranking score を `decay ** count` 倍する。`0.7`=既定 (1 回再 surface で 70%、2 回で 49% に圧縮、強すぎず弱すぎず)、`0.5`=積極的 rotation、`1.0`=完全 no-op (Stage 1 ロールバック)、`0.0`=同 id 再 surface を完全禁止 (実質常時 rotation、test 用)。direct slot は `final_score × novelty` で re-sort、lensing slot は `gap × novelty` で再 argmax (露出 gap は raw を保持)、persona slot は `(mass^w) × cos × novelty` で再 winner 決定。フック側 env `GAOTTT_AMBIENT_NOVELTY_TURNS` (既定 5、0 で無効) が scan する直近 turn 数を決める。`recently_surfaced` 未送付 or `{}` ならコード路は no-op (legacy)。詳細: [Plans — Ambient Recall Lateral Association](Plans-Ambient-Recall-Lateral-Association.md) Stage 1 |
| `ambient_conversational_source_factor` | `0.5` (default ON、2026-06-12 焼き込み) | **Observation Apparatus Round 2 E1 (2026-06-12)**: ambient slot (direct / lensing) の ranking score を、`ambient_conversational_sources` に含まれる source の item に対し factor 倍する乗算 lens。novelty decay と同じ combined multiplier (`final_score × novelty × src_factor`) に合成。`1.0`=OFF (bit-exact legacy)。背景: 2026-06-12 dogfooding で direct hits 6 件中 5 件が chat-history raw chunk (state-dict ダンプ・依頼定型句一致)。本番実機評価 (id=fa1d03a8) で near-tie の際に agent/file source が浮上することを確認 → default `0.5` に昇格 (旧 ambient は会話ログ raw chunk に支配され実用性低かった経緯)。観測・注入層のみ — recall 本体 / force / mass には不適用 (Phase M 単一規則不変)。env `GAOTTT_AMBIENT_CONVERSATIONAL_SOURCE_FACTOR` |
| `ambient_conversational_sources` | `("openai", "claude-web", "claude-code")` | E1 の damping 対象 source class 列挙。ingest された会話コーパスが日本語会話文の lexical 空間を支配する構造への対処 |
| `ambient_dump_symbol_ratio` | `0.45` (default ON、2026-06-12 焼き込み) | **Observation Apparatus Round 2 E2 (2026-06-12)**: dump-shape gate。content 先頭 400 字の dump score = `max(記号比率, 長 ASCII 識別子トークン比率)` がこれを超える候補を ambient slot (direct / lensing) から除外、次点繰り上げ。state-dict キー羅列・生コード断片の注入防止。`>= 1.0`=OFF (E1 と同じ規約 — None default だと GAOTTT_* env 自動マップが効かない)。本番 dump サンプルで較正 (id=fa1d03a8): 真の dump (state-dict / base64 / hex) は 0.97-1.0、readable code ≤0.40、自然文 <0.05 で `0.45` は両者の間の clean gap → default 昇格。env `GAOTTT_AMBIENT_DUMP_SYMBOL_RATIO` |
| `direct_hit_anti_hub_lambda` | `0.4` | **Lateral Association Stage 7.1 (2026-05-26、暫定デフォルト active 2026-05-26)**: direct-hit anti-hub。`ambient_recall.direct` の top-K 構成で同一 cluster key (`cohort_id` OR `original_id`、後者は file ingest の chunk グループ化に使われる) の連続採用にペナルティ `λ × count_of_shared_cluster` をかけ、greedy MMR で並び替え。両キーとも None (pre-Phase-M 旧 memo のみ) は penalty 対象外。`0.0`=完全 rollback、`0.4`=暫定 default (`tests/perf/test_tier3_cluster_monoculture.py` で ambient 直接スロット `avg_unique 2.67→4.00 / avg_max_dom 2.33→2.00` 確認、本番 GLM acceptance で 638-chunk 本 cluster が 1/5 に capped 確認)、`1.0` 近辺=強い分散。Phase M 単一規則整合 (cluster identifier は両方とも構造的 id、source/tag 分岐なし)。**重要な architectural note**: raw `recall` service は `engine.query` の `top_k` を広げない (prefetch cache 互換のため) ので anti-hub は **engine が返した top_K 内の reorder のみ** で、ambient_recall (pool=10〜) ほど強くは効かない。production で見えるのは主に ambient block の直接スロット。**limitation**: individual-node high-mass dominance (一握りの singleton agent/intention/commitment memo が cluster 関係なく top-K を占有する production 観察) には効かない、それは Phase N Mass Evaporation の領域。詳細: [Plans — Lateral Association Stage 7](Plans-Ambient-Recall-Lateral-Association.md) |
| `cooccurrence_assoc_normalization` | `"none"` | **Lateral Association Stage 8 (2026-06-02、default OFF)**: 共起 weight を degree 正規化した **連想強度**に変換 (共起グラフ層 anti-hub)。`"none"`=生 co-recall カウント (legacy bit-exact)、`"cosine"`=`w/√(deg(a)·deg(b))`、`"pmi"`=`max(0, log(w·W/(deg·deg)))`。promiscuous hub (多数の recall セッションに出た高 degree ノード) は degree で割られて全相手への association が下がり、rare で specific な共起だけが高く残る = 「〇〇といえば〜」の信号。consumer は Stage 5 lensing resonance (`_lensing_resonance`)。**正規化 ON 時は `ambient_lensing_resonance_scale` を要再調整** (正規化値は raw count より遥かに小レンジ、`scale=10` だと resonance が全部 0 付近に潰れる)。physics 不変 (observation/ranking signal の変換のみ)。`GAOTTT_COOCCURRENCE_ASSOC_NORMALIZATION` で env 可。詳細: [Plans — Lateral Association Stage 8](Plans-Ambient-Recall-Lateral-Association.md) |
| `cooccurrence_hub_degree_percentile_cut` | `None` | **Lateral Association Stage 8**: explicit な anti-hub floor。degree が active 分布の P パーセンタイル超の neighbor を association strength 返却前に drop (Stage 7.2 mass percentile cut の degree 軸版)。`None`=cut なし。本番 percentile sweep (2026-06-02) の結論: halo の degree 分布は **bimodal** ({deg≈6 の specific gem} ∪ {deg 67-105 の bulk の壁}) で p90↔p95 に cliff、percentile では「gem だけ (高 precision・低 recall)」か「gem + bulk」の二択しか作れず **recall は増やせない**。precision-mode なら `p≈85` + `cosine` で future-self への手紙のような specific 連想を 1 件 surface 可。volume を出す lever は knob ではなく **共起グラフのデータ品質 (Accretion Recall P3)**。`scripts/diag_assoc_halo.py --assoc-mode cosine --hub-degree-cut <P>` で観察。`\| None` 既定ゆえ JSON-only (env 非対応) |

> スロット単位の `*_enabled` フラグで個別 rollback 可。

> ⚠️ **Heavy persona dominance (2026-05-25 本番 acceptance で発見、follow-up (b) で knob 化、2026-07-02 に default 昇格)**: 旧既定 `ambient_persona_mass_weight=1.0` では ranking 式が `score = mass × cos(query, persona_vec)` となり、production DB に **1 つだけ mass が突出した persona** (例: `mass=2.82` vs 他 `mass=1.0` 付近) があると、cos 軸の差では決着せず query 横断で同一 persona が picked される (実例: `harakiriworks-art-website` intention)。**2026-07-02 昇格**: env opt-in で `w=0.3` 単独を運用しても dense 日本語 embeddings の cos ≈ 0.52 が旧 `min_relevance=0.5` floor を slip し症状が継続したため、`ambient_persona_mass_weight=0.3` (1.0→0.3) と `ambient_persona_min_relevance=0.65` (0.5→0.65) の **両 knob を同時に code default 化**。critical exponent は本番質量分布で `w* = log(cos_ratio) / log(mass_ratio)` (例: mass_ratio=2.82, 目当ての cos_ratio=1.3 なら `w* ≈ 0.25`)。事後 baseline は `Refinement Stage 5 (test_tier3_ambient_quality.py)` で取って性能改善か feature 好みかを数値で分離する (measurement-first を skip したことは ToDo 6-7 に開示済)。詳細: [Plans — Ambient Recall Refinement](Plans-Ambient-Recall-Refinement.md) 「follow-up (b)」節。gate しきい値の校正は本番 DB の content から `BM25Index(tokenizer="sudachi")` を再構築し代表プロンプト（強い on-topic / 弱い on-topic / off-topic）を `search` して top スコアの谷を観察する（H5 env override は `GAOTTT_AMBIENT_BM25_MIN_SCORE`）。gate index は startup で構築（Sudachi は char-3gram より遅く 32k で +30-60s）、`remember` で逐次追加、`compact` で再構築。

## 重力波伝播

| パラメータ | 既定 | 影響 |
|---|---|---|
| wave_initial_k | 3 | seed top-k |
| wave_max_depth | 3 | 再帰最大深度 (Phase M follow-up 2026-05-13 で 2→3。1 recall で touch される displacement 範囲が ~20-50 → ~60-150 nodes に拡大、per-recall latency +20-30%。`wave_attenuation=0.5` で第 3 段以降は force<0.001 で自動 filter) |
| wave_attenuation | 0.7 | 深度ごとの減衰係数 |
| wave_mass_scale | 1.5 | mass 依存 top-k のスケール |
| wave_k_with_filter | 1000 | `recall(source_filter=...)` 指定時の seed top-k（dense corpus で sparse class を救済、Phase H Stage 2 で 200→500、2026-05-12 本番 23k DB 検証で 500→1000 引き上げ） |
| wave_seed_mass_alpha | **0.0** | seed 段階の mass-aware rerank 重み（Phase H Stage 1）。`raw + α*log(1+mass)` で pool を再 rank。**2026-05-14 に 0.1 → 0.02 → 0.0 と段階的に下げて最終 disabled**(下記注を参照)。`> 0` に戻すと Phase L Stage 1 の RRF score scale (~0.03 max) と Phase H Stage 1 の mass term (cosine scale ~0.9 想定) が干渉し、mass の重い無関係 chunk が semantic 距離を上書きする structural bug が発生 |
| wave_seed_pool_size | 50 | seed 再 rank の pool 大きさ（Phase H Stage 1） |
| wave_dynamic_k_enabled | `True` | top-N 密度応答型の seed 拡大（Phase H Stage 3）。`False` で固定 initial_k |
| wave_density_window | 10 | density 評価で見る top-N の N |
| wave_density_threshold | 0.95 | tail/top 比率の閾値。これ未満で「sparse」と判定して seed 拡大 |
| wave_initial_k_max | 50 | sparse 判定時の effective_k 上限（Phase H Stage 3） |

> **⚠️ `wave_seed_mass_alpha` の scale 不整合バグ (2026-05-14 発見、`0.0` に固定で回避)**
>
> Phase L Stage 1 (RRF fusion) と Phase H Stage 1 (`raw + α × log(1+mass)`) は **score scale が異なる**:
> - `raw` に来る値 — RRF mode: ~0.018–0.033 / cosine mode (legacy): ~0.5–1.0
> - `α × log(1+mass)` — 例: α=0.02 × log(23) = **0.062** ← RRF max の 2 倍!
>
> 結果、RRF が semantic にランクした top を **mass の重い無関係 chunk が完全に上書き**する。例: 「あの航空機事故はこうして起きた」を recall すると、cosine 0.92 で raw FAISS top の book chunks (mass 1.4) が、cosine 0.79 で **mass 22 の京都大学入試 / 会社四季報 chunk** に seed step で押し出される。
>
> **修正**: `wave_seed_mass_alpha = 0.0` で seed boost を完全 disable。RRF fusion が既に raw cosine + virtual + BM25 を scale-invariant に組み合わせているので、seed step での mass 介入は不要。Phase H Stage 1 の意図(heavy node lift)を RRF mode で正しく実現する **RRF-scale aware mass boost** は Phase N で再設計予定([Plans — Roadmap](Plans-Roadmap.md))。
>
> 詳細・診断手順は [Operations — Troubleshooting](Operations-Troubleshooting.md) の「ファイルで登録した文書が recall に出てこない」節。

## Persona-anchored seed boost (Phase J Stage 1)

`propagate_gravity_wave` の seed step で `α_persona × proximity` を加算。declared value / intention / commitment から `fulfills`/`derived_from`/`completed` で graph 連結するノードを優先入場させる。詳細: [Plans — Phase J](Plans-Phase-J-Persona-Anchored-Retrieval.md)。

| パラメータ | 既定 | 影響 | 上げると | 下げると |
|---|---|---|---|---|
| persona_boost_enabled | `True` | グローバル off スイッチ | n/a | `False` で完全 skip、collect/proximity 計算も走らない |
| persona_boost_alpha (α_persona) | 0.5 | 結合定数 (`wave_seed_mass_alpha=0.1` の 5×) | persona-tied ノードが seed pool で勝ちやすい (intention 直下の task/agent が surface しやすい) | 弱まる、`0.0` で計算は走るが boost なし |
| persona_max_hop | 2 | graph traversal の hop 上限 | 3 hop で間接的関連も拾える (false positive ↑) | 1 hop で fulfills 直下のみ (狭い) |
| persona_hop_decay | 0.5 | hop あたり減衰率 | 0.7 で 2 hop=0.49 (遠隔まで強い) | 0.3 で 2 hop=0.09 (急減衰) |
| persona_active_ttl_seconds | 14 日 | active 判定の TTL (Stage 2 で commitment に適用) | n/a | n/a — Stage 1 では未使用 |

> **チューニング助言**: `persona_boost_alpha=0.5` は acceptance test (本番 23k DB) で「persona-tied ノードが seed pool に届く」を目的に置いた初期値。届かなければ `1.0` まで上げる、効きすぎ (persona ノードが全 query で top1 を独占) なら `0.2` まで下げる。`persona_max_hop=2` は Phase D の典型チェーン (intention → task → outcome) を拾える深さ、3 以上にすると間接的な関連 (誰かが derive した知識の派生) も混入。`persona_boost_enabled=False` で Stage 0 (Phase J 前) 挙動に完全 rollback。

## Hybrid retrieval — BM25 union seed (Phase L Stage 1)

Seed pool は raw FAISS (semantic) ∪ virtual FAISS (semantic+history) ∪ BM25 (lexical) の 3-way union。異なる metric tensor の重ね合わせで、embedder の hidden ranking が拾えない surface-form 一致 (例: 「Eleventy Pipeline」 → `.eleventy.js`) を BM25 が直接 catch する。RRF fusion (Reciprocal Rank Fusion) が default で scale 不変・query 間安定。詳細: [Plans — Phase L](Plans-Phase-L-Hybrid-Retrieval.md)。

| パラメータ | 既定 | 役割 |
|---|---|---|
| hybrid_bm25_enabled | `True` | Phase L Stage 1 全体の on/off。`False` で Phase H Stage 4 (raw+virtual) と同等挙動に rollback |
| bm25_seed_k | `50` | BM25 が seed pool に提供する top-N 数。raw/virtual の `wave_seed_pool_size` / `wave_k_with_filter` と同オーダー |
| bm25_k1 | `1.5` | Robertson-Sparck-Jones の term-saturation 制御。文献標準 1.2-2.0 の中央付近 |
| bm25_b | `0.75` | length-normalization。文献標準 0.75 が標準 corpus 向き、固定長 doc 多めなら 0.5、長文ばかりなら 0.9 |
| bm25_score_mode | `"rrf"` | `"rrf"` で Reciprocal Rank Fusion (Cormack 2009 標準、scale 不変)、`"weighted_sum"` で legacy 2-way max-merge + BM25 normalize blend |
| rrf_k | `60` | RRF の rank-fusion 定数。Cormack 2009 標準 |
| bm25_score_alpha | `0.5` | `"weighted_sum"` mode 専用、BM25 normalized share。`"rrf"` mode では無視 |
| bm25_tokenizer | `"trigram"` | char 3-gram (依存ゼロ、日英混在に頑健)。`"sudachi"` は `uv pip install -e ".[bm25-sudachi]"` 後に選択可 |

> **チューニング助言**: 本番 acceptance で「Surface ✅ / Semantic 整合 ⚠️」が分離した query (Eleventy Pipeline 等) を起点に、BM25 寄与を確認。RRF default で大半は十分だが、lexical match が overshoot して semantic 整合が落ちる場合は `"weighted_sum"` + `bm25_score_alpha=0.3` で BM25 影響を弱める。tokenizer は trigram で「重力」「Eleventy」両方扱えるが、固有名詞の同形異義語が頻出する corpus なら Sudachi extra に切り替えて形態素解析する。Stage 1 では disk persistence なし — `compact()` か再起動で SQLite content から rebuild される (24k docs で数秒)。複数 MCP プロセス共存環境は raw FAISS と同じ可視性問題があり、片方プロセスでの `remember` が他方プロセスの BM25 index に届くのは次回 startup or compact 後。

## Multi-Source Query — query as a mass distribution

複合プロンプトを 1 つの pooled centroid に潰さず、節に分割して各節を独立した点質量として扱う。各節が自分の `_union_pool` を引き、per-segment pool を RRF 融合した seed pool から wave を **1 回** 伝播する。pooled embedding は語彙的に重い側に引っ張られる（centroid drag）— その唯一の非物理ステップを seed 段の場の重ね合わせで修正する。詳細: [Plans — Query as Mass Distribution](Plans-Query-Mass-Distribution.md)。

| パラメータ | 既定 | 役割 |
|---|---|---|
| multi_source_enabled | `True` | `recall` / `explore` の multi-source seeding on/off。`False` で単一 vector seeding の legacy 経路に bit-for-bit 復帰（rollback は 1 行） |
| multi_source_ambient_enabled | `True` | 毎ターン発火する `ambient_recall` パス専用の別フラグ（perf 隔離のため独立）。`False` で ambient だけ単一 source に戻す |
| multi_source_max_segments | `4` | 分割数 N の上限。超過分は最長 N 節を残す。FAISS search は `N × O(corpus)` なので上限で予測可能に保つ |
| multi_source_min_segment_chars | `12` | これ未満の断片は隣接節に併合（"as an SPA" のような短片が degenerate な点質量にならないように） |

> **チューニング助言**: 両フラグとも **default ON**（2026-05-21）。実 RURI 検証で複合クエリの recall は single-source の ~2×（p50 15→32ms / p95 17→40ms）だが、Tier 6 ゲート（`p95 < 120ms`）・ambient フック予算（~500ms）に対し余裕、と確認済み。単純（非複合）プロンプトは分割されないのでコストはゼロ。`training_delta` の `intent-centers=N` 行で実際の分割数を観測できる。perf を抑えたい / 挙動を切り戻したいときは `multi_source_enabled=False`（recall/explore）または `multi_source_ambient_enabled=False`（ambient のみ）で 1 行ロールバック。分割は正規表現（句読点ベース）なので Sudachi extra 不要。

## Stellar supernova cohort (Phase K Stage 1)

`index_documents` の batch を 1 超新星イベントとして扱い、batch 内 N 件 (N≥`supernova_min_cohort_size`) に **相互 co-occurrence edge** + **centroid からの outward velocity** を付与。新規 cohort が「互いに重力を持たない散発的塵」状態から「同イベント残骸群」になる。Phase G genesis kick (個別重力) の隣で適用。詳細: [Plans — Phase K](Plans-Phase-K-Stellar-Supernova-Cohort.md)。

| パラメータ | 既定 | 影響 | 上げると | 下げると |
|---|---|---|---|---|
| supernova_enabled | `True` | グローバル off スイッチ | n/a | `False` で完全 skip、Phase G 単独に rollback |
| supernova_min_cohort_size | 2 | 発火する最小 batch サイズ | 3+ で小規模 batch が cohort 化しない | 1 にすると単独 remember で edge を張ろうとする (相手いない) |
| supernova_initial_weight | 1.0 | 相互 edge の初期 weight | 2.0+ で強い cohort (seed pool の mass-aware boost が `log(1+w)` で強く効く) | 0.5 で弱い cohort、0.0 で edge 形成 skip |
| supernova_velocity_alpha | 0.03 | 爆発の運動量 α | 0.05 で `orbital_max_velocity` cap に到達 (爆発が一気に膨張) | 0.01 で穏やかな爆発、0.0 で velocity 形成 skip |

> **チューニング助言**: Phase K は **将来 session の新規 cohort** に効くが、**既存 orphan ノードは遡及できない** (cohort 形成は index 時のみ)。既存 harakiriworks-self-knowledge 112 件のような遡及対象には別途 ritual script で edge + velocity を後付けする必要。`supernova_initial_weight=1.0` は Phase B `edge_threshold=5` と独立 (Phase K は event-driven、Phase B は recall 累積 driven)。一括投入では cohort が大きくなりすぎる場合は `index_documents` の batch を分割するのが運用上の手段 (例: 100 件投入を 4×25 件に分けて 4 つの cohort にする)。
| virtual_faiss_enabled | `True` | virtual_pos でビルドした第二 FAISS を並走（Phase H Stage 4）。priming 後の displacement を seed step に反映する |
| virtual_faiss_save_interval_seconds | 60.0 | virtual FAISS の write-behind 周期。`cache.virtual_faiss_dirty` が立つたびに次の tick で full rebuild + disk save。`0` で無効化（compact / shutdown 時のみ rebuild、Phase J Stage 1 以前のレガシー挙動）。**長期常駐 MCP サーバーでは非ゼロ必須** — そうしないと Phase I/J の query attraction 累積が次の起動まで他プロセスの seed pool に反映されない。23k node 規模で rebuild ~数百 ms、60s 間隔なら 1% 未満の負荷 |
| wave_neighbor_use_virtual | `True` | Phase H Stage 5。wave propagation の per-frontier `search_by_id` を virtual FAISS で行う（「星同士の引力」原則に沿った形）。raw FAISS は静的な天球で displacement を見ないため、`False` でレガシー（raw のみ）に戻せる。virtual FAISS が空 / None / 無効のときは自動 fallback。p50 への影響は誤差範囲（同サイズの index search） |

## 誕生時の重力 kick（Phase G — Stage 1）

新規 `remember` 時に既存重力場から 1 step の kick を適用、新規ノードを「裸」(mass=1, displacement=0, velocity=0) で gravity 場に置かないための補正。詳細: [Plans — Phase G — Memory Genesis](Plans-Phase-G-Memory-Genesis.md)。

| パラメータ | 既定 | 影響 | 上げると | 下げると |
|---|---|---|---|---|
| genesis_kick_enabled | `True` | Phase G G.1 の全体 ON/OFF | — | レガシー挙動（裸投入） |
| genesis_kick_neighbor_k | 5 | kick 計算で使う高 mass 近傍数 | 多くの近傍からの引力で軌道が安定 | ノイズ少、近傍偏在に弱い |
| genesis_kick_pool_size | 50 | FAISS top-N pool（mass 降順で K に絞る前段） | 真の重力中心を見つけやすい | 計算速い |
| genesis_mass_boost_alpha | 0.5 | `|acc|` → mass boost 変換係数 | 新規が surface しやすい | homogenization 抑制 |

## 夢による継続的軌道捕獲（Phase G — Stage 2）

quiet node を idle 時間に synthetic recall で再活性化し、co-occurrence エッジ + gravity 場を時間軸で build up するバックグラウンドループ。`_is_synthetic=True` で `return_count` は増やさない（saturation 非発火）。

| パラメータ | 既定 | 影響 | 上げると | 下げると |
|---|---|---|---|---|
| dream_enabled | `True` | Phase G G.2 の全体 ON/OFF | — | 夢ループ無し（Stage 1 のみ） |
| dream_interval_seconds | 30.0 | 夢 tick 周期 (Phase M follow-up 2026-05-13 で 60→10 にしたが foreground starvation が判明し 10→30 に再調整、`_dream_loop` 内で `await asyncio.sleep(0)` 挿入で per-tick yield も追加) | CPU 占有率↓、quiet 救済が遅い | 早く quiet が育つが CPU↑ |
| dream_batch_size | 10 | 1 tick で再活性化する quiet node 数 (50 → 10 へダウン、batch 中の連続 CPU 占有 2.5s → 0.5s に短縮して foreground recall が timeout しなくなった) | 多数同時に育つ | レイテンシ少、深く育つ |
| dream_mass_ceiling | 1.5 | quiet と判定する mass 上限 | 高 mass まで再活性化 | 真に育っていないノードのみ救済 |
| dream_min_idle_seconds | 300.0 | 最終 access からこれ以上経った node のみ対象 | 多くが対象になる | 本当に休眠中のもののみ |
| dream_top_k | 10 | 各 synthetic recall の top_k | 広く co-occurrence | 焦点絞った re-activation |

> **チューニング助言**: dream loop のチューニング結果を実測したいときは `scripts/bench_dream_loop.py` を使う。本番 DB を汚さず `/tmp/gaottt-dream-bench-*` で隔離実行し、N 件 add 後の baseline と M dream tick 後の差分 (edge 重み / mass 分布 / probe top-K stability) を計測。`--docs 200 --ticks 30 --batch 10` で 30 秒程度。Phase K supernova が edge **count** を index 時に saturate させるので、dream の signal は edge **weight** に出ることに注意。

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

## save_candidates Hook（Plans-Save-Candidates-Hook.md）

`auto_remember` の Stop / turn-end hook ラッパー。backend service ([Plans-Save-Candidates-Hook.md](Plans-Save-Candidates-Hook.md)) 自体は `auto_remember_*` の knob を再利用する (max_candidates の既定だけ 3、token-budget 配慮)。hook script 側の env:

| 環境変数 | 既定 | 説明 |
|---|---|---|
| `GAOTTT_SAVE_CANDIDATES_ENABLED` | `1` | `0`/`false`/`off` で Stop + UserPromptSubmit-inject 両方を無効化 |
| `GAOTTT_SAVE_CANDIDATES_URL` | `http://127.0.0.1:7878/mcp` | proxy mode backend MCP endpoint (ambient_recall hook と共有) |
| `GAOTTT_SAVE_CANDIDATES_TIMEOUT` | `3.0` | 秒。heuristic 抽出のみ (embedder 不使用) で steady-state ~10-50ms、3s は cold-start 余裕分 |
| `GAOTTT_SAVE_CANDIDATES_MAX` | `3` | block に surface する上位 N 件 |
| `GAOTTT_SAVE_CANDIDATES_TURNS` | `2` | Stop 側 — transcript から拾う直近 user+assistant 交換数 |
| `GAOTTT_SAVE_CANDIDATES_STATE_DIR` | `~/.gaottt/save_candidates` | Stop → UserPromptSubmit bridge の per-session state file ディレクトリ |
| `GAOTTT_SAVE_CANDIDATES_INCLUDE_PERSONA` | `1` | `0` で persona slot 省略 (ambient_recall persona slot との重複回避) |
| `GAOTTT_SAVE_CANDIDATES_EMIT` | `state` | 出力モード。`state` = state file 書き込み (Claude Code Stop+Inject bridge)、`stdout` = block を直接 stdout に出力 (opencode plugin パス、`opencode-save-candidates.ts` が設定) |
| `GAOTTT_HOOK_ANTI_RESTACK` | `1` | `0`/`false`/`no` で ambient_recall Python hook の再注入 marker ガードを無効化 (default on)。ガードは prompt に既注入 marker が含まれている場合にバックエンド呼び出しをスキップする (frontend parity: opencode plugin と同じ marker 文字列をチェック) |

opencode plugin (`scripts/hooks/opencode-save-candidates.ts`) 専用の追加 env:

| 環境変数 | 既定 | 説明 |
|---|---|---|
| `GAOTTT_REPO` | `/mnt/holyland/Project/GaOTTT` | opencode-ambient-recall.ts と共有。Python interpreter + script path の base |
| `GAOTTT_SAVE_CANDIDATES_PYTHON` | `$GAOTTT_REPO/.venv/bin/python` | Python interpreter override |
| `GAOTTT_SAVE_CANDIDATES_SCRIPT` | `$GAOTTT_REPO/scripts/hooks/save_candidates.py` | Hook script override |
| `GAOTTT_SAVE_CANDIDATES_DEBUG` | (unset) | ファイル path をセットすると plugin の step trace を append (silent-by-default なので診断時のみ) |

`.claude/settings.json` への hook 登録 + opencode plugin の install path は [Operations — Server Setup](Operations-Server-Setup.md) 参照。

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
| faiss_save_interval_seconds | 5.0 | in-memory FAISS → `.faiss` ファイル保存間隔。`0` で無効化（shutdown 時のみ save、レガシー挙動）。**MCP サーバーのような長期常駐プロセスでは必ず非ゼロ**にしないと他プロセスから新規 remember が見えなくなる |
| faiss_persist_guard_enabled | True | 逆方向上書きガード。in-memory FAISS が DB active 数に対し過小なとき全 FAISS 永続を拒否し、壊れた index で正常な on-disk index を上書きするのを防ぐ（[問題5.5](Operations-Troubleshooting.md)）。`GAOTTT_FAISS_PERSIST_GUARD_ENABLED=0` で無効化 |
| faiss_persist_floor | 100 | ガードが効き始める最小 active ノード数。これ未満は守るべき index が無いので不活性（小規模 / reset 直後の DB で誤発動しない） |
| faiss_persist_min_ratio | 0.5 | `faiss.size >= active × この比` のときのみ永続。下回ると corrupt とみなし書き込み拒否。正当な bulk forget/compact は cache active も同時に減るので誤発動しない |
| proxy_serialize_requests_enabled | True | **並列 recall ハードニング (2026-06-01)**。proxy mode で単一 upstream `ClientSession` への全 forwarder + ping を `asyncio.Lock` で 1 in-flight に直列化。同一ターンの 2 並列 recall が streamable-http session を壊す「`MCP error 32600: Session terminated`」を防ぐ（[Troubleshooting](Operations-Troubleshooting.md)）。`GAOTTT_PROXY_SERIALIZE_REQUESTS_ENABLED=0` で legacy 無ロック挙動に rollback（auto-reconnect も無効化 — rebuild に lock が要るため） |
| proxy_auto_reconnect_enabled | True | proxy が session 終了系の例外で upstream session を rebuild し 1 回 retry。`_ensure_backend` 再利用で backend 死 / idle watchdog / cold-start も自己修復。正常な tool error (`isError=True`) は素通し。serialize が ON のときのみ有効。`GAOTTT_PROXY_AUTO_RECONNECT_ENABLED=0` で無効化 |
| faiss_index_lock_enabled | True | `FaissIndex` の add/save/search/reset/get_vectors を `threading.Lock` で保護。背景の `to_thread` save（実ワーカースレッド）が event-loop の `add()` と競合するクロススレッド race を封じる（asyncio lock では防げない）。recall hot path への影響は p50 +1.9% / p95 +0.1% / p99 +7.4%（予算内）。`GAOTTT_FAISS_INDEX_LOCK_ENABLED=0` で無効化 |

## Embedding

| パラメータ | 既定 | 用途 |
|---|---|---|
| model_name | `cl-nagoya/ruri-v3-310m` | Embedding モデル |
| embedding_dim | 768 | 次元数（モデル変更時は要連動） |
| batch_size | 32 | バッチエンコード時 |

## Multiverse manifest (MV0 — 2026-07-02)

宇宙ごとの embedder identity・次元を `<data_dir>/manifest.json` に記録し、起動時に整合を検証する。embedder が変わると全ベクトルが無意味化するため、その事故を構造的に防ぐガード。詳細: [Plans — Multiverse Scale-Out](Plans-Multiverse-Scale-Out.md) §Stage 1、[multiverse-implementation-plan.md](../maintainers/multiverse-implementation-plan.md) §MV0。

| パラメータ | 既定 | 用途 |
|---|---|---|
| manifest_check_enabled | `True` | manifest 起因の整合 check を有効化。**escape hatch** — `=False` で manifest `embedding_dim` と `embedder_id` の不一致を warning 透過（宇宙を救済する最終手段、通常は触らない）。**FAISS 次元保護（`embedder.dimension != config.embedding_dim`）はこの knob に関わらず常に RuntimeError** — FAISS と次元が違うと検索が壊れるため |

> **v1 の既知挙動**: `ensure_manifest` は config のみから生成するため、初回 manifest の `embedder_version` は `"unpinned"`。実 RuriEmbedder の `embedder_version`（HF commit hash）と比較すると、`build_engine` で毎回 version mismatch warning が出る。これは v1 の warn-only 仕様（実装計画 §MV0-2）で、HF revision が動く運用を v1 では止めない。DR 用の model artifact pinning は MV5 で runbook 要件化される。

## Multiverse embedding service (MV1 — 2026-07-02)

embedding model（RURI）のロードを engine プロセスから分離し、ホスト共有サービスとして切り出す。GPU コストをユーザー数ではなくホスト数に比例させる。詳細: [Plans — Multiverse Scale-Out](Plans-Multiverse-Scale-Out.md) §Stage 1、[multiverse-implementation-plan.md](../maintainers/multiverse-implementation-plan.md) §MV1。

| パラメータ | 既定 | 用途 |
|---|---|---|
| embedder_endpoint | `""`（空文字列 = in-process RuriEmbedder） | 設定時（非空）`build_engine` が `RemoteEmbedder` を構築して service に接続。env `GAOTTT_EMBEDDER_ENDPOINT=http://127.0.0.1:7879` で上書き可（sentinel empty-string default なので generic env loop が拾う — `Optional[str] = None` だと拾わない罠を回避）。空文字列は falsy 扱いで従来の in-process `RuriEmbedder` 経路 |
| embedder_request_timeout_seconds | `30.0` | `RemoteEmbedder` の HTTP timeout（秒）。env `GAOTTT_EMBEDDER_REQUEST_TIMEOUT_SECONDS` |

> **起動方法**: `python -m gaottt.embedding.service --host 127.0.0.1 --port 7879 --model cl-nagoya/ruri-v3-310m`。**非 localhost への bind は拒否**（認証を持たない service なので `/encode` が外部に露出するのを防ぐ）。systemd 雛形: `deploy/gaottt-embedder.service`。詳細は [Operations — Server Setup](Operations-Server-Setup.md)「embedding service を分離する」節

## Multiverse owner lease (MV2 — 2026-07-02)

同じ `data_dir` を複数プロセスが開いて write-behind が後勝ちする事故クラス（[bidirectional cache overwrite](Architecture-Concurrency.md) / [FAISS reverse-overwrite](Operations-Troubleshooting.md)）を機構で閉じる。1 宇宙 1 書き込みオーナーを強制し、lease 喪失時は read-only 遷移する。詳細: [Plans — Multiverse Scale-Out](Plans-Multiverse-Scale-Out.md) §Stage 2、[multiverse-implementation-plan.md](../maintainers/multiverse-implementation-plan.md) §MV2。

| パラメータ | 既定 | 用途 |
|---|---|---|
| owner_lease_enabled | `False` | **standalone 構成向け**。`True` で lease を有効化。**managed 宇宙（`manifest.managed=True`）はこの knob に関わらず強制** — supervisor 管理下の宇宙をどの entry point から開いても lease が効く |
| lease_force_takeover | `False` | takeover の canonical 経路。`GAOTTT_LEASE_FORCE_TAKEOVER=true` または CLI `--force-takeover`（mcp_server）で立てる。stale でない（fresh な heartbeat の）lease でも強制奪取する |
| lease_heartbeat_seconds | `10.0` | lease オーナーの生存通知周期（秒）。この周期で `owner.lock` の `heartbeat_at` を更新 |
| lease_stale_seconds | `60.0` | heartbeat がこの秒数以上停止した lease は stale と判定され、別プロセスが takeover 可能になる。`now - heartbeat_at > lease_stale_seconds`（厳密 `>`、`==` は stale ではない） |

> **lease 喪失時の挙動**: heartbeat read-back で `owner_id` 不一致を検出すると、engine は即座に read-only に遷移する。cache flush / FAISS save の全永続化経路（4 経路）を `_persist_blocked` latch で skip、mutating operation 14 種は `LeaseLostError` で拒否、`query` / `prefetch` は passive フォールバック（結果を返すが field を更新しない）。read 系（`recall(passive=True)` / `get_node` / `reflect`）は継続。詳細: [Operations — Troubleshooting](Operations-Troubleshooting.md)「LeaseHeldError / LeaseLostError が出る」

> **filesystem 要件**: lease 機構は `O_CREAT\|O_EXCL` / `fcntl.flock` / `os.replace` の POSIX semantics に依存する。NFS / CIFS 等 network filesystem 上では信頼できない — v1 は local FS のみサポート

> **default OFF の理由と昇格計画**: 既存 standalone 構成の挙動を変えないため OFF で導入。managed 宇宙は上記のとおり manifest で強制。この機構は standalone でも事故防止に有効（reverse-overwrite incident の再発防止と同型）なので、1-2 週の dogfooding 後に code default ON への昇格を判断する

## Multiverse supervisor (MV3 — 2026-07-02)

1 ホストで複数テナントの宇宙を独立 `data_dir` で運用する universe supervisor（port 7880）の knob。supervisor が宇宙 engine を spawn / respawn し、API key でユーザー → 宇宙をルーティングする。詳細: [Plans — Multiverse Scale-Out](Plans-Multiverse-Scale-Out.md) §Stage 2、[multiverse-implementation-plan.md](../maintainers/multiverse-implementation-plan.md) §MV3、運用手順は [Operations — Multiverse Setup](Operations-Multiverse-Setup.md)。

| パラメータ | 既定 | 用途 |
|---|---|---|
| multiverse_root | `""` (空 = 機能不使用) | multiverse のルートディレクトリ。env `GAOTTT_MULTIVERSE_ROOT`。空なら supervisor も shim の supervisor mode も使われない（default 不変）。推奨 `~/.local/share/gaottt-multiverse` |
| supervisor_port | `7880` | supervisor の HTTP listen port |
| supervisor_admin_key | `""` | admin API key。env `GAOTTT_SUPERVISOR_ADMIN_KEY` 推奨（config file に書かない）。**空 = supervisor 起動 fail-fast**（unauthenticated admin endpoint を絶対に露出しない） |
| universe_port_range_start | `7890` | 宇宙 backend の動的 port 割当開始 |
| universe_port_range_end | `7989` | 宇宙 backend の動的 port 割当終了。`end - start + 1 = 100` が 1 ホストあたりの宇宙数上限（v1 制約） |
| supervisor_spawn_concurrency | `3` | 同時 spawn 上限（semaphore）。cold respawn spike（朝の始業時刻に多数の宇宙が一斉起床等）対策 |
| supervisor_readiness_timeout | `90.0` | spawn readiness poll timeout（秒）。FAISS load + BM25 build を含む初回起動を待つ猶予 |

> **★ `backend_token_enabled` knob は作らない**: backend token middleware の発動スイッチは **`GAOTTT_BACKEND_TOKEN` env の有無そのもの**。boolean knob を作ると「token 設定済みだが middleware 無効」の dangerous state が生まれる。`GAOTTT_BACKEND_TOKEN` 未設定 → 素通し（default 不変、既存 7878 単一 backend 無影響）/ 設定済み → 全リクエストで `Authorization: Bearer` 検証。supervisor が spawn 時にこの env を注入する（token は `secrets.token_urlsafe(32)` で生成、`<universe_dir>/backend.token` 0600 に永続化、再起動時に読み戻し）

> **REST 経路は提供しない (v1)**: managed 宇宙は owner lease (MV2) で二重 engine を構造的に拒否するため、REST app (`build_engine()` で独自 engine を立てる) が開けない。宇宙に露出する経路は MCP のみ。テナント管理者の操作は supervisor admin API で提供（MCP/REST parity 鉄則は管理面に適用されない — [Architecture — Overview](Architecture-Overview.md) 設計判断表「Multiverse supervisor は MCP/REST parity 対象外」）

> **信頼境界**: `multiverse_root` は 0700 / `manifest.json` / `owner.lock` / `backend.token` は 0600。同一 OS ユーザー内の manifest 改変は v1 信頼境界外（root / 同一ユーザーを敵とするモデルは v1 で守らない）。NFS / CIFS は lease の POSIX semantics が保証できないため非サポート

## Multiverse control plane client (MV4 — 2026-07-03)

supervisor が Postgres-backed control plane（port 7881、独立プロセス）と同期・usage telemetry を送信するための knob。control plane は aggregator / audit / billing 収集点で、engine コードには一切接触しない（設計判断 J9）。詳細: [Operations — Control Plane](Operations-Control-Plane.md)、[Plans — Multiverse Scale-Out](Plans-Multiverse-Scale-Out.md) §Stage 3、[multiverse-implementation-plan.md](../maintainers/multiverse-implementation-plan.md) §MV4。

| パラメータ | 既定 | 用途 |
|---|---|---|
| control_plane_url | `""` (空 = control plane 不使用) | control plane の URL。env `GAOTTT_CONTROL_PLANE_URL`（例 `http://127.0.0.1:7881`） |
| control_host_id | `""` | control plane 側で発行された host_id。env `GAOTTT_CONTROL_HOST_ID` |
| control_host_token | `""` | **SECRET** — control plane 側で発行された平文 host token。env `GAOTTT_CONTROL_HOST_TOKEN`。log に出さない、spawn される backend にも継承させない（supervisor 自身のみが使う） |
| control_default_tenant_id | `""` (空 = 単一暗黙 tenant `"default"`、J11) | local 宇宙が報告する tenant_id。env `GAOTTT_CONTROL_DEFAULT_TENANT_ID`。未設定なら `"default"` を使用（`001_initial.sql` が bootstrap INSERT する tenant、FK 解決済み） |
| control_sync_interval_seconds | `300.0` | pull（reconcile）周期（秒）。env `GAOTTT_CONTROL_SYNC_INTERVAL_SECONDS` |
| usage_push_interval_seconds | `60.0` | usage flush 周期（秒）。env `GAOTTT_USAGE_PUSH_INTERVAL_SECONDS` |
| usage_spool_dir | `""` | usage spool ディレクトリ。env `GAOTTT_USAGE_SPOOL_DIR`。空 = `<multiverse_root>/logs/usage-spool/` に解決（`multiverse_root` も空なら usage push 無効化、durability target 無し） |

> **★ 発動条件 — 3-point gate**: `control_plane_url` AND `control_host_id` AND `control_host_token` の **3 点とも設定時のみ** ControlClient が有効。1 つでも欠けたら WARNING log + control 不使用（supervisor は従来通り local-only、**default 不変**、MV3 完全一致）。gate は `ControlClient.__init__` と supervisor `_main()` の両方でチェックされる。

> **★ `control_host_token` は SECRET 扱い**: `_build_spawn_env` と同様に log に出さない。supervisor が spawn する backend プロセスの env にも **継承させない** — supervisor 自身のみが読む。

> **★ J1=A (PM 承認済み SoT deviation)**: v1 の usage は **`/route` 解決回数を activity telemetry として集計** したもので、recall / remember / ingest の正確な operation count ではない（`event_type='route_resolution'`）。billing-grade の正確な operation count は [MV4.1](Plans-Multiverse-Scale-Out.md) で導入予定。詳細: [Operations — Control Plane](Operations-Control-Plane.md)「usage telemetry の意味」節。

## Multiverse backup (MV5 — 2026-07-04)

supervisor が universe 作成/削除の成功経路で litestream 設定ファイル（`dbs:` ブロック）を再生成するための knob。設定時、supervisor は `<multiverse_root>/universes/*/` を scan し純粋関数で YAML を生成、atomic write（`tmp + fsync + os.replace`）で指定パスへ書く。失敗は ERROR log のみで supervisor 応答に影響しない（best-effort）。詳細: [Operations — Backup & DR](Operations-Backup-Multiverse.md)、[Plans — Multiverse Scale-Out](Plans-Multiverse-Scale-Out.md) §Stage 4、[multiverse-implementation-plan.md](../maintainers/multiverse-implementation-plan.md) §MV5。

| パラメータ | 既定 | 用途 |
|---|---|---|
| litestream_config_path | `""` (空 = hook 不使用) | litestream 設定ファイルの出力パス。env `GAOTTT_LITESTREAM_CONFIG_PATH`（例 `/etc/litestream/gaottt.yml`）。空なら hook は一切呼ばれず、MV3/MV4 構成は 1 行も変わらず動く（**default 不変**）。設定時は `POST /admin/universes` と `DELETE /admin/universes/{uid}` の成功経路で再生成 |

> **★ `backup_gen_timeout_seconds` knob は意図的に未設置**: hook は subprocess ではなく direct import の純粋関数呼び出しなので timeout は不要。knob を置くと「subprocess で動く」と誤解を招くため（Codex review round-2 #5）。

> **★ scan + write は `_backup_hook_lock` で直列化**: 並行 create/delete hook が out-of-order に完了しても、最後の writer の scan 時点の on-disk 状態が最終 YAML になる（stale write の構造的排除）。hook は best-effort で、例外は ERROR log のみ（supervisor 応答は成功のまま）。spawn env（`_build_spawn_env`）には litestream / backup 関連 knob は **一切漏れない**（supervisor プロセス内のみで動く）。

---

## チューニングの典型シナリオ

### 「もっと探索的にしたい」

- `gamma` ↑（temperature が大きくなる）
- `gravity_G` ↑（引力が強い）
- `wave_max_depth` ↑（広く伝播）

> Note: Phase I 以降、`max_displacement_norm` は事実上 ∞ (`1e6`)。displacement の届く距離は Hooke (`orbital_anchor_strength`) + `displacement_decay` + `orbital_max_velocity` で物理的に均衡する。「もっと遠くまで」したい時は `orbital_anchor_strength` ↓ または `gravity_G` ↑。

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

### 「`recall(source_filter=...)` で agent / value / commitment が surface しない」

DB が大きくなる（~10k 超）と、デフォルト `wave_initial_k=3` の seed 段階で dense cluster（Twitter / 書籍 / コーパス系）が独占し、sparse class（`agent` / `value` / `intention` / `commitment` / `compaction`）が seed に入らないまま post-filter で空集合になる。対処:

- `wave_k_with_filter` ↑（200 → 500/1000）— seed pool を広げて sparse class を含める。レイテンシは線形に増えるので `scripts/run_benchmark_isolated.sh` で p50 < 50ms を確認
- それでも不足なら呼び出し側で `recall(query, source_filter=[...], wave_k=N)` を明示
- target が極端に sparse（< 50 件）な場合、`tag` ベースの `reflect` で発掘する方が確実

→ より広い文脈: [Operations — Troubleshooting](Operations-Troubleshooting.md)
