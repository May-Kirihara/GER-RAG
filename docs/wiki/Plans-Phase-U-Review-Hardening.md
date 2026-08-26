# Phase U — 実MCPレビュー対応 (Production Hardening)

- 状態: **実装完了** (2026-08-26) — WP-1/2/4/5/6 完了、WP-3 は実装完了だが較正 gate FAIL (`docs/notes/phase-u/ambient-composite-calibration.md`) により **composite 昇格は user 判断待ち** (default `"or"` のまま)。本番 rollout probe 5/6 PASS (失敗は probe 5 = R3 のみ)。経緯: v1 → Codex reject (7 blocking) → v2 → QA fail (3 blocker) → v3 で承認 → 実装。
- 起点レビュー: `docs/handoff/2026-08-25-post-recovery-retrieval-quality.md` §改善実装後の実MCP再検証 (490行目以降, 2026-08-25 Codex 実測)
- 親 handoff: 同 doc は Phase 2 (本 phase) 完了まで open
- review 記録: `/tmp/opencode/codex-phase-u-plan-review.md` (要約: reject — 7 blocking) / QA review (fail — 3 blocker、いずれも文面修正で解決)

## 1. 背景

Phase T 実装 (Stage 1-6) 後の実 MCP 再検証で、semantic decay 修正・ambient 空返し解消・通常 recall の直接性回復を確認した一方、以下 5 課題が残った。

| # | 重要度 | 課題 | 実測証拠 |
|---|---|---|---|
| R1 | P0 | Stage 3/4 (direct/TTT qualification) が production で無効 — multiverse supervisor が `GAOTTT_*` env を strip するため env opt-in が届かない | breakdown に `q/d/f/gap` 表示なし |
| R2 | P1 | explore diversity が結果に反映されない (Stage 6 も default OFF + env 不達)。active explore で低関連 node に mass/displacement 更新 | `diversity=0.8` で recall と Jaccard@5=1.0, Δmass +0.0488 (openai 会話 node) |
| R3 | P0 | ambient OR gate が off-topic を通す — RURI cosine が大規模 corpus で 0.8 前後の狭帯に集中し、絶対閾値では拒否できない | ペンギン潜水艇 query が gate passed (bm25 17.8, virt 0.835, raw 0.805) |
| R4 | P1 | 既知障害 node `de1b528f-…` が近同語彙 query で top-5 外 | `get_node` では取得可能 (データ消失ではない) |
| R5 | P0 | cold start 129s — 最初の `reflect(summary)` が約129秒。検索処理ではなく startup 同期処理が主要因 | warm call は 2-5s |

## 2. 目標

レビューの「受け入れ条件」を満たすこと。R4 は **blocking acceptance** とする (§4 WP-4)。

## 3. 非目標

- 2,053 orphan nodes の調査 (別課題)
- universe config の新たな解決順序の導入 (既存機構の範囲で対応)
- cache load 自体の高速化 — WP-6a の計装結果で semantic-ready 30s が達成不能な場合は **descope 判断を user に提案** してから着手
- cosine 狭帯問題そのものの解決 (embedder 差し替え等) — 相対軸 (percentile/margin) で回避

## 4. Work packages と受け入れ条件

### WP-1: Stage 3/4 昇格 — 完全 catalog 対応 + 昇格組み合わせ test (R1)

`direct_qualification_enabled` / `ttt_qualification_enabled` を code default `True` へ。

**flags-ON 失敗 catalog** (`docs/notes/phase-t/calibration-round.md` §3 の 12 件。うち #5-11 の ambient 7 件は Phase T WP-D で既に pin 済み、#12 は既知 flaky。対象は #1-#4):

| # | test | 分類 | 対応 |
|---|---|---|---|
| 1 | `test_engine_query_kick.py::test_stage3_gate_dampens_*` (ttt) | 数値比較の margin 反転 (契約違反ではない) | **test 再設計**: mass 軌道非依存な契約比較へ (例: 同一 mass 固定下で gate 有無の kick 項を直接比較、または step 数短縮 + mass pin)。assertion の強度は維持 (gate が new node を dampen することを検証し続ける) |
| 2 | `test_reason_line.py::test_dominance_artifact_fires_on_high_mass` (direct/ttt) | 観測層の branch 優先度衝突 (**実害**) | Phase T WP-E の `_LENSING_PICK_MIN_GAP=0.02` で既に修正済みの可能性が高い → flags ON で再検証し、未修正なら explain.py の gap 最小閾値/branch 順を修正 (**pin で隠さない**) |
| 3 | `test_engine_explore_diversity.py::test_relevance_floor_excludes_low_semantic_candidates` (direct) | fixture 前提が Stage 3 の狙った挙動で崩壊 | 当該 test は Stage 6 floor の test なので `direct_qualification_enabled=False` を明示 pin (mechanism isolation)。assertion 不変 |
| 4 | `test_engine_explore_diversity.py::test_presented_only_updates_pool_members_untouched` (ttt) | stub corpus が 0.75 閾値を超えられず learn set 空 | **corpus を再構成** (共通 token で StubEmbedder cosine ≥0.75 を出す) し、昇格後の production 組み合わせ (presented ∩ learn) を test し続ける。pin しない |

**新規 test**:
- default pin test (3 flag とも True の回帰 fence)
- **promoted-combination integration suite**: direct+ttt+explore 同時 ON で、更新カテゴリ契約を **精密に列挙** して検証 —
  - **gate 対象** (unqualified 候補は更新されない): mass growth (confidence-scaled) / query kick (displacement の query 方向成分) / cooccurrence edge 構築
  - **非 gate 対象** (Phase T Stage 4 契約どおり更新され続ける): `last_access` / evaporation / temperature / orbital N-body displacement / `return_count` (presented 全件 — presentation 的事実)
  - (b) dream/synthetic exemption 維持、(c) MCP/REST の recall breakdown に `q/d/f/gap` が default 設定で表示される
- **config-matrix**: direct×ttt の 4 组み合わせ + 不一致組み合わせ (ttt ON/direct OFF 等) で breakdown/更新の doc に明記される挙動差の test
- perf: production-scale copy で recall / diversity=0.8 explore の latency を flags OFF と比較 (回帰なしこと)

**`scripts/diag_config.py`**: `GaOTTTConfig` の解決時に per-field source map (default / env / config-file) を記録する仕組みを `from_env` に追加し、それを表示。heuristic 比較ではなく真の source。

受け入れ条件 (レビュー原文): 実 multiverse MCP の recall breakdown に `q/d/f/gap` が表示される (WP-7 rollout) / low-relevance 候補が `q=-` となり active recall でも physics 更新されない / config diagnostic で effective value と設定由来を取得できる。

### WP-2: supervisor exact-name allowlist (R1 の rollback 経路)

- `_build_spawn_env` に **閉じた完全名 allowlist** (module 定数 `RUNTIME_TUNING_ENV_ALLOWLIST: frozenset[str]`)。対象は Phase T/U の tuning knob のみ (例: `GAOTTT_SEMANTIC_HALFLIFE_ENABLED`, `GAOTTT_SEMANTIC_HALF_LIFE_SECONDS`, `GAOTTT_SEMANTIC_FLOOR`, `GAOTTT_DIRECT_QUALIFICATION_ENABLED`, `GAOTTT_TTT_QUALIFICATION_ENABLED`, `GAOTTT_EXPLORE_DIVERSIFIED_PRESENTATION_ENABLED`, `GAOTTT_EXPLORE_COHORT_PENALTY`, `GAOTTT_EXPLORE_MIN_SEMANTIC`, `GAOTTT_EXPLORE_DIVERSITY_POOL_MULTIPLIER`, `GAOTTT_AMBIENT_GATE_OR_SEMANTIC`, `GAOTTT_AMBIENT_SEMANTIC_RAW_MIN`, `GAOTTT_AMBIENT_BM25_MIN_SCORE`, `GAOTTT_AMBIENT_MIN_SCORE`, `GAOTTT_AMBIENT_GATE_USE_BM25` + WP-3/6 の新 knob)。**prefix/wildcard 一致は禁止** — 未来の field は自動的に deny。
- **`GAOTTT_CONFIG` は伝播しない** (任意 field を含め得るため tuning-only allowlist と矛盾する、という review 指摘を採用)。永続 rollback は supervisor を起動する service 定義 (systemd 等) に env を置く運用で対応し、Operations-Tuning に明記。
- 値検証: bool/int/float を config の coercion 規則で parse。**不正値 (非 bool、NaN/Inf、enum 外) は backend spawn を拒否** し `/route` に validation error を返す (fail-fast・観測可能)。
- identity 系 (`GAOTTT_DATA_DIR` / `GAOTTT_OWNER_LEASE_ENABLED` / `GAOTTT_BACKEND_TOKEN` / `GAOTTT_EMBEDDER_ENDPOINT`) は allowlist 外・従来どおり明示上書きで **常に勝つ** (allowlist 通過値や parent env との衝突時に上書き優先を test)。
- MV5 review #7 維持: `LITESTREAM` / `BACKUP` 系が spawn env に存在しないことの assertion を残す。
- test: allowlist 外の `GAOTTT_*` は strip / identity 上書き優先 / 不正値で spawn 拒否 / allowlist 値の伝播 / MV5 assertion。

受け入れ条件: supervisor 経由で flag を false にした backend が legacy 挙動に戻ることを integration test で担保 (production rollout は WP-7)。

### WP-3: ambient composite gate + 本番経路較正 (R3)

- `ambient_gate_mode`: `"or"` (現行, 初期 default) | `"composite"` (較正後に昇格判断)。
- composite 判定:
  ```
  accept_direct = bm25_strong
               OR ( virtual_percentile >= percentile_min
                    AND top_margin >= margin_min
                    AND raw_top1 >= raw_floor )
  ```
  - **母集団の定義**: 参照分布 = 「ambient と同一の候補生成経路 (`recall(passive=True)` → pool) を通った **較正 query population** の top-1 virtual cosine 分布」。percentile は狭帯の正規化 (分布を [0,100] に写像) であり、**分離は labeled 閾値選定で担保する** — percentile 自体は判別器ではない。
  - 較正 query population: labeled probe set — positive (障害/運用系 query + **日本語言い換え** ≈20)、negative (off-topic ≈20)。calibration split で閾値を決め、**held-out split で FP/FN 率 + bootstrap CI** を報告。同じ probe で選定と検証をしない。
  - `top_margin` = top-1 virtual − pool virtual median。
  - **raw 軸は `expose_score_breakdown` に非依存**: composite gate は ambient_recall 内部で独自に raw FAISS 検索を行い top-1 raw cosine を得る (breakdown population に依存しない)。`expose_score_breakdown=False` 運用でも composite が動作することを test する (Phase T 既知の「raw 軸欠損」問題の再発防止)。
  - 新 knob: `ambient_semantic_percentile_min` / `ambient_margin_min` / `ambient_raw_floor_composite` (較正で決定)。
- **較正は本番経路で**: script は production **copy** 上に engine を構築し、実際の `ambient_recall(engine, query, expose_breakdown=True)` (passive 契約 — 物理を乱さない、かつ copy) を呼んで pool 統計を記録。pipeline の再実装はしない (review 指摘「read-only scoring seam は本物の service path」を採用)。
- **artifact の fingerprint**: embedder identity (manifest の embedder_id/version) + corpus fingerprint (**各 active document の indexed content の digest**: sorted (id, sha256(content)) の sha256 — store scan 時に算出。timestamp 系は content 変更を取りこぼすため使わない) + 関連 config knob + format/script version。fingerprint 不一致・破損・欠損時は composite を **fail-closed** (BM25-only + diagnostics `composite_reference_unavailable`) — 既知の false-positive 経路 (`"or"`) への open fallback はしない。再較正手順を Operations-Troubleshooting に明記。
- edge case 契約: pool <2 → margin 未定義 → reject (`composite_pool_too_small`) / raw 軸欠損 (breakdown なし) → reject / 非有限値 → reject / 同順は midrank percentile。
- `gate_diagnostics` 拡張: virtual/raw percentile・margin・accept signal。`empty_reason` に `composite_reject` / `composite_pool_too_small` / `composite_reference_unavailable` 追加。
- persona/lensing slot が direct reject を反転させないことの test (gate 判定は slot 構成より前にある構造の fence)。
- **昇格判断の数値 gate (事前登録)**: composite への default 昇格は「negative set (off-topic ≈20) の **FP=0** かつ positive set (障害/運用/言い換え ≈20) の **FN≤10%**」を held-out で満たす場合のみ。この基準は較正実施前に本 plan に登録済みとみなす。満たさない場合は `"or"` default のまま Phase U を **R3 未達の partial** として user に escalate する (事後判断の余地を残さない)。
- REST parity: `gate_diagnostics` 新 field・`empty_reason` 3 新値の REST roundtrip test を `tests/integration/test_rest_parity.py` に追加。
- 受け入れ条件 (レビュー原文): 既知障害 query は通過 / ペンギン query は空返し / 日本語言い換えは BM25 弱でも semantic 側から通過 / diagnostics に percentile・margin・accept signal 表示。加えて held-out FP/FN 報告。

### WP-4: target-ID score trace + 障害 node 順位 (R4) — blocking

- `scripts/diag_target_trace.py`: query + target node ID → raw rank / virtual rank / hybrid BM25 rank / ambient word-BM25 rank / qualification verdict / final rank + score breakdown を一括表示。engine を直接構築せず store/index の read-only 参照 + passive 経路で算出 (物理を乱さない)。
- production copy で `de1b528f-f95a-46e8-a28d-7a4fbd580806` + 近同文 query を調査 → 原因特定 (候補生成 / 狭帯 cosine / mass・saturation / chunk size)。
- **top-5 到達は Phase U の受け入れ条件 (blocking)**: trace 結果に基づき scoped fix を実施 (例: 候補 pool 拡大)。**embedding 再検証も copy 上で先に行い**、本番側は code/config fix で対応する。data fix (本番 node 書き換え) が必要な場合は backup・他プロセス停止の手順を user に提示して承認を得る。構造的修正が必要な場合は findings + 対応案を user に提示して判断を仰ぐ (その場合本 WP は blocked 扱い)。
- 受け入れ条件: 対象 node 本文から作った近同文 query で top-5 へ入る (production copy + live probe 両方) / 診断 script が 5 rank + qualification を一括表示。

### WP-5: explore MMR 昇格 + selection trace (R2)

- `explore_diversified_presentation_enabled` を code default `True` へ。
- 低関連候補の TTT 更新保護は WP-1 (ttt ON) で担保。WP-1 の promoted-combination suite に「low-relevance lateral candidate へ TTT 更新なし」を含める。
- **selection trace** (breakdown 追加行、既存行の書式不変): wave depth / wave_reached / raw vs virtual 由来 / cohort (cohort_id or original_id) / qualification (`q=±`) / 当該 item が更新対象 (learn set) に入ったか — レビュー要求の全 field。
- acceptance の質的基準を具体化: Jaccard@5 < 1.0 **かつ** (a) `explore_min_semantic` floor 以下の候補は排除されている、(b) lateral result (top-1 と cohort が異なる、または tag/source class が異なる) が 1 件以上 (レビューの「1〜2件」は 1 件以上 + relevance floor で妥当性を担保)、(c) deterministic 再現 (test fixture から `gravity.py` の rng seam に seed を注入して固定)。relevance は floor + golden query median で担保 (tier3/7 green 維持)。
- 受け入れ条件: 上記 (a)(b)(c) + レビュー原文の Jaccard/lateral/更新なし。

### WP-6a: startup 計装 — decision gate (R5)

- `engine.startup_timings: dict[str, float]` (manifest / store init / expire scan / cache load / FAISS load / virtual FAISS load・build / BM25 build (2 index 別) / diagnostics)。log 出力 + `GET /admin/readiness` (WP-6b) で取得。
- **production scale copy で実測し、semantic-ready 構成要素の内訳を docs/notes/phase-u/startup-timings.md に記録**。
- **acceptance**: production-scale copy で `SEMANTIC_READY` まで ≤30 秒 (WP-6b 実装後の測定でよい)。WP-6a 時点の内訳で「BM25 build を除いても 30s 超え」が確定した時点で descope 判断を user に相談。
- **decision gate**: BM25 build が支配的でなければ WP-6c/6d は着手しない (cache load 支配なら 30s 目標のために別策を user に提案)。
- 現 lifecycle の確認: engine は初回 tool call で lazy 生成 (`mcp_server.py:59`)、supervisor の backend ready は MCP initialize handshake (engine startup を含まない) — WP-6b でこの 2 つを分離する。

### WP-6b: readiness protocol (R5)

- backend: transport listener 起動と同時に **単一の engine startup task** を開始 (lazy 初回生成を廃止)。全 MCP handler は共有 startup future を bounded wait し、timeout 時は構造化された retryable error (state: STARTING + 経過時間) を返す。
- readiness 露出: FastMCP HTTP app に `GET /admin/readiness` を追加 (idle watcher と同じ Starlette app injection seam を利用、backend token 認証)。response: `{state: STARTING|SEMANTIC_READY|HYBRID_READY, timings, bm25_size}`。state は単調遷移のみ。
- supervisor `/route`: transport ready と `SEMANTIC_READY` を区別 (readiness endpoint を poll)。STARTING 中は bounded deadline まで待ち、超過なら starting 状態つきの応答 (即時 error ではなく観測可能な状態)。
- 診断・Phase N mass evaporation sweep は SEMANTIC_READY **前に** 完了する位置を維持 (共有 state を検証・変更するため serving 前に実行)。
- 部分初期化 engine の shutdown 挙動、build 失敗時の retry/readonly 降格を定義。
- **rollback**: `readiness_protocol_enabled=False` (default True) → lazy 初回生成 (現行) に復帰。handler の bounded wait も無効化。
- 受け入れ条件: cold client が timeout せず starting 状態を観測できる / warm reconnect で engine 再構築が走らない。

### WP-6c: background BM25 build — 条件付き (WP-6a の gate 次第)

- **新規 index object に対して build** (store の stable snapshot から) → build 開始時の mutation generation を記録 → build 完了までの mutation (remember/archive/restore/forget/merge/compact/expiry) を journal に記録 → 完了後に replay → **engine-level lock 下で atomic swap**。search は swap 時に安定した参照を取得 (build 中は旧 index or 空参照)。
- 2 index (hybrid 用 / ambient gate 用) で同一手順。CPU-bound な tokenization は thread で実行し、shared state への直接書き込みを排除 (review 指摘の race 構造対応)。
- task の cancel/例外/retry、lease loss 時の挙動、HYBRID_READY 遷移条件を定義。
- task の cancel/例外/retry、lease loss 時の挙動、HYBRID_READY 遷移条件を定義。
- **rollback**: `bm25_background_build_enabled=False` (default True) → 同期 build (現行) に復帰。
- 受け入れ条件 (レビュー原文の関連部): search during build で古い/空 index から新 index へ atomic に切り替わる / 全 mutation 種が build 中に競合しない (test)。

### WP-6d: BM25 snapshot — 条件付き (WP-6c と同時に)

- 永続化: data_dir に tmp write → fsync → atomic rename → checksum 付き。load 時 validation。
- fingerprint: **indexed content の digest** (sorted (id, sha256(content)) の sha256 — build と同一の store scan で算出) + tokenizer identity/version + k1/b + index format version + universe id。ID/timestamp 系のみ (count+max(updated_at)、id+updated_at) は content 変更を取りこぼすため使わない (collision 指摘対応)。
- owner-lease / persist-block (FAISS guard と同様の) 恒久 block 規則を適用。
- **rollback**: `bm25_snapshot_enabled=False` (default True) → 永続化せず毎回 build。
- 受け入れ条件: snapshot 一致時の cold start で BM25 再 build が走らない / fingerprint 不一致 (content 変更・tokenizer 変更・cross-universe) で rebuild。

### WP-7: docs + handoff + production rollout 検証

- docs 一式: Tuning (allowlist・新 knob) / Troubleshooting (composite fail-closed と再較正手順・readiness triage・`diag_config.py` / `diag_target_trace.py` の usage) / MCP-Reference-Memory + **MCP-Reference-Index** (行追加) / **REST-API-Reference** (`/admin/readiness` に加え、新 `empty_reason` 3 値・`gate_diagnostics` 拡張・selection trace は REST response にも現れるため記載) / **Architecture-Overview 設計判断表** (`/admin/readiness` を FastMCP HTTP app のみに置く parity 例外の記録 — `/reset` と同じ扱い) / Roadmap / _Sidebar / CLAUDE.md / SKILL.md (+ `.claude/skills/` へ cp 同期)。
- production backend restart (CLAUDE.md「code deploy 時の backend 再起動」手順) + レビューの**固定 probe set** (handoff 772-808 行) 実行、期待値 6 項目確認。**probe 5 (ペンギン空返し) は composite gate の default 昇格が前提** — 昇格しなかった場合は R3 未達として報告 (§4 WP-3 の事前登録 gate 参照)。probe 実行は secondopinion-MCP sub-agent 方式 (CLAUDE.md 本番 acceptance workflow)。
- deploy 順序注意: WP-2 (allowlist) が入るまでは multiverse backend への新 code deploy を行わない (rollout は WP-7 限定)。
- handoff note `docs/handoff/2026-08-25-phase-u-review-hardening.md` + 元 review handoff に対応記録への pointer 1 行。

## 5. テスト戦略

- test-first。修正は assertion 不変・config 隔離のみ (WP-1 catalog の分類どおり)。**#2 の reason line は pin せず修正**。
- 昇格組み合わせは pin で隠さない: promoted-combination suite が default ON の実挙動を検証する。
- 較正・計測は production copy のみ。copy 手順: `sqlite3 .backup` (online-safe) + FAISS/virtual/manifest の file copy。**copy は較正専用で restore source にしない** (sidecar との数秒の race は許容し、その旨を script docstring に明記)。copy 先は `mkdtemp` で一意にし、PM が測定後削除する。
- full suite + ruff + perf tier3/4/6/7 + 両 smoke + (WP-1) production-scale latency 比較 (**閾値: tier6 budget (p50<60ms / p95<120ms / p99<250ms) 維持 + `perf_diff.py` の 25% rule**)。
- REST parity: WP-3 (gate_diagnostics 拡張)・WP-5 (selection trace) の response は REST にも現れるため roundtrip test を必ず追加 (Phase T `test_rest_parity.py` 前例どおり)。

## 6. リスクと ロールバック

| リスク | 対処 |
|---|---|
| 昇格による ranking 広域変化 | promoted-combination suite + tier3/4/7 + production copy で品質/latency 検証 / allowlist で env rollback (`GAOTTT_DIRECT_QUALIFICATION_ENABLED=false` 等) |
| composite gate が言い換えを落とす | held-out FP/FN 報告なしには default 昇格しない (初期 default `"or"` のまま) |
| fail-closed で ambient が沈黙しすぎる | diagnostics の `composite_reference_unavailable` で即時気づける + 再較正手順 docs |
| BM25 background の race | 新 object build + journal replay + atomic swap (in-place 変更なし) |
| readiness 待ちで逆に client が掴まる | bounded wait + retryable error + supervisor の bounded deadline |
| WP-6a で cache load が支配的 | 6c/6d を descope し 30s 目標の別策を user に提案 (誤った commit をしない) |

## 7. 実装順序

WP-1 → WP-2 → WP-5 → WP-3 → WP-4 → WP-6a → WP-6b → (6a gate を満たす場合) WP-6c → WP-6d → WP-7。
high-risk のため並列化なし。iteration budget: test 修正 loop 3 round / gate 判定は都度。

## 8. 仮定 ledger

| 仮定 | 根拠 | 反証条件 | 影響 |
|---|---|---|---|
| 129s の主要因は BM25 build ×2 (または cache load) | embedder が分離サービスで RURI load が backend 外 / warm 2-5s | WP-6a 計装 | 6c/6d の descope または対象再協議 |
| allowlist の閉じた knob 集合は運用十分 | Phase T/U knob が tuning の主体 | 運用で不足指摘 | allowlist への追加は review 経由で拡張 |
| percentile 正規化 + labeled 閾値で狭帯を分離できる | penguin virt 0.835 vs incident 0.856 の差は小さいが pool 統計・margin との組合せで分離を見込む | 較正 held-out で分離不能 | gate 設計再協議 (z-score 等) |
| `de1b528f` は scoped fix で top-5 可能 | 近同語彙 query なら raw cosine が高くなるはず (`get_node` で内容確認済み) | WP-4 trace で raw rank が低い | user に findings 提示 (WP blocked) |
| `#include <sqlite3 .backup>` + sidecar copy で較正用 copy が十分一貫する | 較正は分布測定が主で数秒の lag は許容 | copy で manifest/diagnostics が startup gate に掛かる | backend 短時間停止してから copy に切替 |
| FastMCP app への readiness route injection が可能 | idle watcher が同一 seam (streamable_http_app monkey-patch) で middleware を入れている実績 | FastMCP 版差異で不可 | MCP tool (`prefetch_status` 拡張) に変更し parity 対応 |

## 9. Open questions (文書仮定 — user 判断不要)

- composite gate の default 昇格は held-out 較正結果で判断 (判断根拠を docs/notes/phase-u/ に残す)。
- WP-6a の結果が 30s 目標不達成を示す場合のみ user に descope 判断を相談する。
