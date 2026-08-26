# 引き継ぎメモ — Phase U Review Hardening (実 MCP レビュー 5 課題 R1-R5 対応)

## ステータス

- 状態: **implemented + production rollout 検証済み + final review (WP-8) 3 blocker 解消。R3 は user 指示① (3-arm 再較正) を実施し v3 新規 probe で gate 不達 → "or" 維持確定・既知制約として close (§10 追記参照、done-with-notes)**
- 日付: 2026-08-26
- 起点レビュー: [2026-08-25-post-recovery-retrieval-quality.md](2026-08-25-post-recovery-retrieval-quality.md) §改善実装後の実MCP再検証 (490 行以降、Codex 実測)
- 最終レビュー: Codex final review (verdict reject → WP-8 で 3 blocking + 2 non-blocking 対応、下記「WP-8 — final review fixes」節)
- 計画書: [docs/wiki/Plans-Phase-U-Review-Hardening.md](../wiki/Plans-Phase-U-Review-Hardening.md) (v3 — plan review reject → v2 改訂 → QA fail 3 blocker → 反映済み)
- 概要: 実 MCP レビューで残った 5 課題 (R1 qualification env 不達 / R2 explore diversity 非反映 / R3 ambient off-topic 通過 / R4 既知障害 node top-5 外 / R5 cold start 129s) に WP-1〜7 で対応。R1/R2/R4/R5 は解消、**R3 は composite gate の較正が事前登録昇格 gate (held-out FP=0 ∧ FN≤10%) を満たさず `"or"` default のまま user に escalate** ([較正記録](../notes/phase-u/ambient-composite-calibration.md))。WP-8 (final review fixes) で proxy/supervisor timeout 契約・readiness rollback 経路・BM25 snapshot trust boundary の 3 blocker を解消。

## 変更内容 (WP table)

| WP | 機構 | default | rollback env |
|---|---|---|---|
| 1 | `direct_qualification_enabled` / `ttt_qualification_enabled` を code default True 昇格 + promoted-combination suite + `scripts/diag_config.py` (config provenance: default/env/config-file、`GaOTTTConfig.resolve_config_with_sources()`) | **True** | `GAOTTT_DIRECT_QUALIFICATION_ENABLED=false` / `GAOTTT_TTT_QUALIFICATION_ENABLED=false` |
| 2 | supervisor の tuning env 伝播を **完全名 allowlist 28 変数** (`gaottt/multiverse/tuning_env.py:RUNTIME_TUNING_ENV_ALLOWLIST`、WP-8 で composite 5 件追加) で開放。exact-name 一致のみ / `GAOTTT_CONFIG` は伝播しない / 不正値 (bool token 外・unparseable・NaN/Inf・gate mode enum 外) は spawn 拒否 → `/route` 500 / identity 系 4 key は明示上書きが常に勝つ / MV5 review #7 assertion 維持 | (常時有効) | 恒久 rollback は supervisor service env (systemd) — allowlist 経由で env を渡さなければ default が効く |
| 3 | ambient composite gate (`ambient_gate_mode="or"\|"composite"`) + `gaottt/services/ambient_composite.py` (純関数) + `scripts/calibrate_ambient_gate.py` + `scripts/ambient_probes_default.json`。composite は fail-closed (参照 artifact 欠損・破損・fingerprint 不一致・count drift 超過 → BM25 のみ accept 経路)。`empty_reason` に `composite_reject` / `composite_pool_too_small` / `composite_reference_unavailable` 追加、gate 行に `pct=`/`margin=`/`raw=`/`sig=` segment | **"or"** (composite は実装のみ・未昇格) | `GAOTTT_AMBIENT_GATE_MODE=or` (明示) — default が既に "or" |
| 4/4b | `scripts/diag_target_trace.py` (query + target node ID → raw/virtual/hybrid BM25/ambient word-BM25 rank + qualification verdict + final rank + pool diagnosis、passive 契約・**production COPY 専用**)。既知障害 node `de1b528f` の根因特定 (raw rank 1・BM25 rank 1・qualified True でも virtual cosine 低迷で final_score 最下位) → **raw-top rescue** (`direct_rescue_raw_rank=3`: qualified item の pool 内 raw rank ≤3 を qualified group 先頭に lift、presentation 専用) | `3` | `GAOTTT_DIRECT_RESCUE_RAW_RANK=0` (無効化)。`direct_qualification_enabled=false` でも rescue は無効 |
| 5 | `explore_diversified_presentation_enabled` を default True 昇格。`ScoreBreakdown` に `cohort` / `provenance` / `in_learn_set` (MCP segment `c=`/`src=`/`learn=±`、strictly trailing)、`ExploreResponse` に `wave_depth` / `wave_reached` (header 行 `wave: depth=N reached=M`) | **True** | `GAOTTT_EXPLORE_DIVERSIFIED_PRESENTATION_ENABLED=false` |
| 6a | `engine.startup_timings` 計装 (manifest/lease/store_init/ttl_scan/cache_load/faiss_load/virtual_faiss_load/bm25_build/background_loops/diagnostics/startup_total + node_count/index_size)。production copy 実測: **bm25_build 147.3s = startup_total 152.9s の 96%** → 6c/6d GO ([計測記録](../notes/phase-u/startup-timings.md)) | (常時) | — |
| 6b | staged readiness protocol — transport (app lifespan) 起動と同時に単一 engine startup task、MCP handler は `readiness_wait_timeout_seconds=30s` bounded wait → retryable 構造 error、`GET /admin/readiness` (Bearer `GAOTTT_BACKEND_TOKEN`、STARTING→SEMANTIC_READY→HYBRID_READY / FAILED、FastMCP HTTP app 専用 = parity 例外)、supervisor `/route` poll (`route_readiness_timeout_seconds=35s`、STARTING 超過は `readiness:"starting"` 付き応答 / FAILED 503 / 404 legacy fallback)、per-session lifespan では engine tear down なし (warm reconnect 再利用)、Tier B bm25 size check は build 中 `tier_b_bm25_size_pending` (INFO) に格下げ | **True** | `GAOTTT_READINESS_PROTOCOL_ENABLED=0` |
| 6c | BM25 background build — 新規 index object への snapshot build + build 窓内 mutation の journal replay + engine-level lock 下 atomic swap (in-place 変更なし)。`engine.bm25_build_state` ∈ idle/building/ready/failed (failed でも検索継続 — readiness HYBRID_READY + `bm25:"failed"`) | **True** | `GAOTTT_BM25_BACKGROUND_BUILD_ENABLED=0` (同期 build) |
| 6d | BM25 snapshot 永続化 — `data_dir/bm25.snapshot` (checksum + **trusted-file policy**、tmp write (0o600) → fsync → atomic rename → dir fsync)。fingerprint = content digest (sorted (id, sha256(content)) の sha256) + tokenizer identity + k1/b + format version + universe id。一致 → build skip、不一致 → rebuild。保存は build 完了時と graceful shutdown 時のみ。初回本番 boot 390MB。**unpickle 前に所有権・権限検査 (WP-8): checksum は偶然の破損検出のみ、真正性は euid 一致 + group/world-writable 拒否 (data_dir 含む) で担保** | **True** | `GAOTTT_BM25_SNAPSHOT_ENABLED=0` (毎回 build) |
| 7 | docs 一式 (Tuning / Troubleshooting / MCP-Reference ×2 / REST / Architecture / Roadmap / Sidebar / CLAUDE.md / SKILL.md / 本 handoff) + production backend restart + 固定 probe set 実行 | — | — |
| 8 | **final review fixes (Codex reject 3 blocker + 2 alignment)** — (1) proxy shim `/route` timeout を 3 config bound から derive した `PROXY_ROUTE_TIMEOUT_SECONDS=230s` に拡大 (embedder lazy-spawn readiness 90s + backend spawn probe 90s + engine readiness poll 35s + transport margin 15s。round-2 review で旧 130s (=90+35+margin、embedder 段を見落とし) から改訂 — 定数は `dataclasses.fields` 経由で定義 site から導出し硬結合を排除、auto-spawn 経路は supervisor 起動 poll と /route を別 budget に分離。`tests/unit/test_proxy_route_timeout.py` が fence)、(2) `readiness_protocol_enabled=False` で `/admin/readiness` route 自体を登録しない (= 404 → supervisor 即時 legacy、35s poll 退化解消)、(3) BM25 snapshot unpickle 前 trusted-file policy + snapshot write の dir fsync + tmp file の `fchmod(0o600)` 強制 (round-2: umask で owner bit が削られる環境対策)、(4) allowlist に composite 5 knob 追加 (23→28) + `GAOTTT_AMBIENT_GATE_MODE` enum 検査、(5) 本 handoff の記述整合 (SKILL.md 済み・known issue 解消) | (常時有効) | — (各機構の既存 rollback env 変更なし) |
| 9 | **R3 follow-up (§10、user 指示①)** — composite 判定を 3-arm (`bm25_strong OR virt≥virt_hi OR (bm25≥bm25_mid ∧ virt≥virt_mid)`) に置換 (旧 percentile/margin/raw 軸と 3 knob は廃止、diagnostics は `virt_top1`/`bm25_top` に、gate 行 segment は `virt=/bm25=/sig=` に、per-call の raw FAISS 検索を削除し純改善)。較正 script に 3-arm grid + **BM25 background build 完了待ち** (v3 run 1 が空窓計測で VOID になった欠陥を修理、`diag_target_trace.py` にも同 wait)。新規 probe v3 (16/14) で再較正 → **held-out FP=14.3% で gate 不達 → `"or"` 維持確定・R3 close**。3-arm は opt-in 実験機構として残存 (knob 暫定値は未検証と明記) | `"or"` (不変) | — (composite は未昇格のため無効) |

## 変更理由 (review 5 課題との対応)

| # | 課題 (review 実測) | 対応 WP | 結果 |
|---|---|---|---|
| R1 (P0) | Stage 3/4 qualification が production で無効 — multiverse supervisor が `GAOTTT_*` env を strip するため env opt-in が届かない。breakdown に `q/d/f/gap` なし | WP-1 (code default 昇格) + WP-2 (allowlist で rollback 経路を確保) | 解消 — production breakdown に `q/d/f/gap` 表示 (probe 2 PASS) |
| R2 (P1) | explore diversity が結果に反映されない (Stage 6 も default OFF + env 不達)。active explore で低関連 node に mass/displacement 更新 | WP-5 (MMR 昇格) + WP-1 (ttt qualification で低関連 TTT 更新保護) | 解消 — Jaccard@5 < 1.0 (probe 3 PASS) |
| R3 (P0) | ambient OR gate が off-topic を通す (ペンギン潜水艇 query が gate passed、RURI cosine が狭帯 0.8 前後に集中し絶対閾値で拒否できない) | WP-3 + §10 R3 follow-up (単一 arm → 3-arm、2 度の較正) | **close (既知制約)** — v2/v3 とも gate 不達。corpus 隣接 absent topic (中世写本の顔料: bm25 20.09/virt 0.8794) が positive 領域内部に位置し bm25/virt 2 軸では分離不能。再挑戦は別判別軸 (LLM relevance 等) 待ち |
| R4 (P1) | 既知障害 node `de1b528f` が近同語彙 query で top-5 外 | WP-4/4b (trace + raw-top rescue) | 解消 — top-1 到達 (probe 6 PASS) |
| R5 (P0) | cold start 129s — 最初の `reflect(summary)` が約 129 秒 | WP-6a-d (計装 → readiness + background build + snapshot) | 解消 — cold SEMANTIC_READY **7.3s**、warm 再利用 (probe 1 PASS) |

## Work packages

| WP | Scope | Status | Files | Verification | Remaining risks |
|---|---|---|---|---|---|
| WP-1 | Stage 3/4 昇格 + catalog test 対応 + diag_config | done | `gaottt/config.py` (default + `resolve_config_with_sources`)、`scripts/diag_config.py`、`tests/unit/test_config_provenance.py`、`tests/integration/test_promoted_combination.py`、`test_engine_query_kick.py` (test 再設計: mass 軌道非依存契約)、`test_engine_explore_diversity.py` (corpus 再構成 + mechanism isolation pin) | promoted-combination suite (gate 対象 / 非 gate 対象 / dream exemption / breakdown `q/d/f/gap` default 表示 / config-matrix) green、full suite green、production-scale latency 比較で tier6 budget 維持 | virtual_cos_norm の temperature noise で閾値境界がわずかに非決定的になり得る (既知・緩和済み) |
| WP-2 | supervisor exact-name allowlist | done | `gaottt/multiverse/tuning_env.py` (新規、WP-8 で composite 5 件 + enum 検査追加)、`gaottt/multiverse/supervisor.py` (`_build_spawn_env` + `/route` 500 mapping)、`tests/unit/test_tuning_env.py` (111 tests)、`tests/integration/test_supervisor_tuning_env.py` | allowlist 外 strip / identity 上書き優先 / 不正値 spawn 拒否 / 伝播 / MV5 assertion / rollback env 3 件 (WP-6b/6c/6d) 登録 — すべて green。supervisor 経由で flag=false の backend が legacy 挙動に戻ることを integration test で担保 | allowlist への追加は review 経由のみ (staleness guard が実 field 追跡を強制) |
| WP-3 | ambient composite gate + 本番経路較正 | done-with-notes | `gaottt/services/ambient_composite.py` (新規)、`gaottt/services/memory.py`、`gaottt/core/types.py` (gate_diagnostics 拡張)、`gaottt/config.py` (6 knob)、`scripts/calibrate_ambient_gate.py` + `scripts/ambient_probes_default.json`、`tests/unit/test_ambient_composite.py`、`tests/integration/test_ambient_composite_gate.py`、`tests/integration/test_rest_parity.py`、`docs/notes/phase-u/ambient-composite-calibration.md` | unit/integration green、REST parity roundtrip green、較正 (production copy 962MB、seed 42、層化 50/50) 実施 — **事前登録 gate 未達** (単一 semantic arm 式では incident query と negative 最高値が virt1 で -0.0014 差で不可分)。3-arm 構造 (post-hoc) は FN=1/16・FP=0/14 だが事前登録の趣旨により昇格根拠にしない | **composite 未昇格 — R3 残存**。`"or"` ではペンギン false positive が通る |
| WP-4/4b | target-ID trace + raw-top rescue | done | `scripts/diag_target_trace.py` (新規)、`tests/unit/test_target_trace_helpers.py`、`gaottt/core/engine.py` (rescue)、`gaottt/config.py` (`direct_rescue_raw_rank`)、`tests/integration/test_direct_rescue.py`、`docs/notes/phase-u/wp4-trace-findings.md` | test-first (rescue なし=top-K 外 / あり=先頭、knob=0 fence) green、production copy trace で de1b528f top-5 (予測を上回り top-1)、live probe PASS | rescue は presentation 専用 (physics 不変) だが ranking 広域変化の可能性は tier3/7 green で確認済み |
| WP-5 | explore MMR 昇格 + selection trace | done | `gaottt/core/engine.py` (trace + wave fields)、`gaottt/core/types.py` (ScoreBreakdown 3 field / ExploreResponse 2 field)、`gaottt/services/memory.py`、`gaottt/services/formatters.py` (trailing segments + `wave:` header)、`gaottt/config.py` (default 昇格) | (a) relevance floor 排除 / (b) lateral 1 件以上 / (c) deterministic 再現 (rng seed) + Jaccard@5 < 1.0 + 低関連 lateral へ TTT 更新なし — green。REST parity (trace fields) green | golden corpus では MMR 効果は限定的という Phase T の既知観察は本番 probe で解消を確認 |
| WP-6a | startup 計装 (decision gate) | done | `gaottt/core/engine.py` (`startup_timings`)、`tests/integration/test_engine_startup_timings.py`、`docs/notes/phase-u/startup-timings.md` | production copy (42,060 nodes) 実測 — bm25_build 147.31s / cache_load 4.37s / startup_total 152.89s。**BM25 支配 → 6c/6d GO** | — |
| WP-6b | readiness protocol | done | `gaottt/server/mcp_server.py` (custom_route + lifespan wrap + bounded wait、WP-8 で flag OFF 時 route 未登録に修正)、`gaottt/multiverse/supervisor.py` (`_await_backend_readiness` + `/route`)、`gaottt/server/mcp_proxy.py` (readiness:"starting" INFO log、WP-8 で timeout 拡大 — round-2 で 3 bound derive の 230s 化 + auto-spawn budget 分離)、`gaottt/diagnostics/startup.py` (Tier B 格下げ)、`tests/integration/test_readiness_protocol.py` | state mapping / token auth / bounded wait / 並行初回 call 共有 / rollback bit-for-bit / per-session tear down なし / Tier B INFO 格下げ / `/route` poll (starting / 503 / legacy fallback) / **flag OFF 時 404 + 即時 legacy (WP-8)** — green。production rollout: cold SEMANTIC_READY 7.3s (旧 129s) | — (WP-8 で 35s 退化は解消) |
| WP-6c | BM25 background build | done | `gaottt/core/engine.py` (build state machine + journal + atomic swap)、`tests/integration/test_bm25_background_build.py` | search during build の atomic 切替 / 全 mutation 種 (remember/archive/restore/forget/merge/compact/expiry) の build 中競合なし — green | build 窓内は hybrid が raw/virtual 縮退運転・ambient gate は semantic fallback (仕様) |
| WP-6d | BM25 snapshot | done | `gaottt/core/engine.py` (persist/load + fingerprint、WP-8 で trusted-file policy + dir fsync 追加)、`tests/integration/test_bm25_snapshot.py` | snapshot 一致で cold start 再 build なし / fingerprint 不一致 (content・tokenizer・cross-universe) で rebuild / **symlink・異 uid 所有・world-writable file/dir を unpickle 前に拒否 (WP-8)** — green。初回本番 boot 390MB 書き込み | snapshot size が大きい (backup 対象に含めるかは運用判断)。旧 code が umask 0o002 環境で書いた snapshot (0o664) は WP-8 code 初回 boot で 1 回 rebuild される (自己修復) |
| WP-7 | docs + handoff + rollout 検証 | done | 下記「ドキュメント」参照 | 全編集ファイル read-back、link/wire 整合、secret なし、git status 意図ファイルのみ | — (SKILL.md は WP-7 で更新済み、下記) |
| WP-8 | final review fixes (3 blocker + 2 alignment) | done | `gaottt/server/mcp_proxy.py` (route timeout 定数)、`gaottt/server/mcp_server.py` (flag OFF 時 route 未登録)、`gaottt/core/engine.py` (snapshot trust + dir fsync + tmp 0o600)、`gaottt/multiverse/tuning_env.py` (allowlist 28 化 + enum 検査)、`tests/unit/test_proxy_route_timeout.py` (新規) / `test_tuning_env.py` / `test_proxy_supervisor_mode.py`、`tests/integration/test_readiness_protocol.py` / `test_bm25_snapshot.py`、`docs/wiki/Operations-Tuning.md`、本 handoff | 新規 test red→green (proxy timeout fence / flag-OFF 404 + /route 即時 legacy / snapshot policy 5 case / gate mode enum) + unit/integration suite green + smokes green | production backend は WP-8 code でまだ再起動していない — merge 後に CLAUDE.md「code deploy 時の backend 再起動」を実行のこと |

## 触ったファイル

- 実装: `gaottt/config.py`、`gaottt/core/{engine,types}.py`、`gaottt/services/{memory,formatters,ambient_composite}.py`、`gaottt/server/{mcp_server,mcp_proxy}.py`、`gaottt/multiverse/{supervisor,tuning_env}.py`、`gaottt/diagnostics/startup.py`
- 新規 script: `scripts/diag_config.py`、`scripts/diag_target_trace.py`、`scripts/calibrate_ambient_gate.py`、`scripts/ambient_probes_default.json`
- test: 新規 12 file (unit 4 + integration 8) + 既存 4 file 改修 (`test_engine_query_kick` / `test_engine_explore_diversity` / `test_rest_parity` / `tests/perf/_helpers.py`)
- docs: `docs/wiki/` 8 page、`docs/notes/phase-u/` 3 artifact、CLAUDE.md、本 handoff
- **未 commit** (Phase U 全体、commit split は残 TODO)

## テスト

各 WP の検証として実施 (2026-08-26、実装 WP での実行結果):

- full suite: **1560 passed** (known flakies は単独実行 green — `test_probe_initialize_success_returns_ok` 等)
- perf (`tests/perf/`, real RURI): **71 passed** — tier3/4/6/7 込み、tier6 latency 回帰なし
- smoke: `scripts/mcp_smoke.py` + `scripts/rest_smoke.py` 両方 green
- WP-1: production-scale copy で flags OFF と比較し latency 回帰なし (tier6 budget p50<60ms / p95<120ms / p99<250ms 維持)
- 新規 test はすべて test-first red→green

コマンド (再実行用):

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/perf    # full suite
.venv/bin/python -m pytest tests/perf/ -q                   # 7 階層 perf (real RURI)
.venv/bin/python scripts/rest_smoke.py && .venv/bin/python scripts/mcp_smoke.py
ruff check gaottt/ tests/
```

## ドキュメント

- [Operations — Tuning](../wiki/Operations-Tuning.md): Phase U 節 (新規 knob 12 件表 + 昇格 3 flag + allowlist 28 変数と意味論 — WP-8 で composite 5 件追加、reference filename は config-file only と明記)、Phase T 表の default 更新
- [Operations — Troubleshooting](../wiki/Operations-Troubleshooting.md): readiness triage 節 / composite gate 節 (fail-closed + 再較正手順) / BM25 snapshot 節 / empty_reason triage 表 3 値追加 / 診断ツール表 3 script 追加
- [MCP-Reference-Memory](../wiki/MCP-Reference-Memory.md) + [Index](../wiki/MCP-Reference-Index.md): breakdown `c=/src=/learn=`、explore `wave:` header、composite 軸 diagnostics、昇格に伴う default 記述更新
- [REST-API-Reference](../wiki/REST-API-Reference.md): breakdown trace field / gate_diagnostics 拡張 / explore wave field / `/admin/readiness` parity 例外節
- [Architecture-Overview](../wiki/Architecture-Overview.md) 設計判断表: 4 row 追加 (readiness parity 例外 / BM25 background+snapshot / raw-top rescue / allowlist) + Phase T row の default 記述更新
- [Plans-Roadmap](../wiki/Plans-Roadmap.md) + [_Sidebar](../wiki/_Sidebar.md): Phase U entry
- CLAUDE.md: Last updated + Phase U 段落
- SKILL.md (+ `.claude/skills/` 同期 copy): 昇格済み explore default・composite `empty_reason` 3 値・`wave:` header を反映済み (当初未編集だったが WP-7 で更新、stale 2 件は解消済み)

## 手動確認

**production backend restart (CLAUDE.md「code deploy 時の backend 再起動」手順) + 固定 probe set (起点 handoff 772-808 行) 実行、2026-08-26**:

| probe | 期待値 | 結果 |
|---|---|---|
| 1. `reflect(aspect="summary")` (cold) | semantic-ready 30 秒以内、以後 warm 再利用 | ✅ PASS — cold SEMANTIC_READY **7.3s** (旧 129s)、warm reconnect で engine 再構築なし |
| 2. recall 設計思想 query (passive, full) | GaOTTT 設計記録が複数 + breakdown に `q/d/f/gap` | ✅ PASS — `q/d/f/gap` default 表示 (WP-1 昇格が multiverse 経由で効いている) |
| 3. explore diversity=0.8 | probe 2 と完全一致せず、低関連候補を更新しない | ✅ PASS — Jaccard@5 < 1.0、低関連候補への TTT 更新なし (learn set gate) |
| 4. ambient 障害 query (expose_breakdown) | gate 通過し障害・Phase T 記録を返す | ✅ PASS — gate passed、障害/運用系記録を surface |
| 5. ambient ペンギン潜水艇 query | `empty_reason` 付きで空返し | ❌ **FAIL (既知・R3 closed)** — `"or"` mode では off-topic が semantic 軸を通過。composite は v2/v3 両較正で gate 不達のため未昇格 (既知制約として close) |
| 6. recall 近同文 query (passive) | `de1b528f-f95a-46e8-a28d-7a4fbd580806` が top-5 | ✅ PASS — raw-top rescue により **top-1** 到達 (trace 予測どおり) |

**集計: 5/6 PASS、1 FAIL (probe 5 = R3 の既知挙動)**。

## 既知の問題

1. ~~R3 (user 判断待ち)~~ → **対応済み (2026-08-26 深夜)**: user 指示① により plan §10 で 3-arm を事前登録 → 新規 probe v3 (16/14) で再較正 → **held-out FP=14.3% で gate 不達、`"or"` 維持確定・R3 close**。詳細は較正記録 v3 round + plan §10 結果節。再挑戦には別判別軸が必要 (将来 phase)
2. ~~flag-OFF 時の `/route` 35s 退化~~ **WP-8 で解消** — `readiness_protocol_enabled=False` では `/admin/readiness` route 自体を登録しない (= 404)。supervisor `_fetch_backend_readiness` は 404 を `READINESS_LEGACY` と読んで即時 legacy 挙動に fallback するため、poll deadline を一切消費しない (`test_route_returns_promptly_against_readiness_disabled_backend` が実 HTTP で fence)
3. ~~shim 10s vs supervisor 35s~~ **WP-8 + round-2 で解消** — proxy shim の `/route` timeout は完全 cold route の 3 stage (embedder lazy-spawn 90s + backend spawn probe 90s + readiness poll 35s = 215s) を cover するよう config field default から derive された `PROXY_ROUTE_TIMEOUT_SECONDS` (現行 230s = 215s + margin 15s、round-2 review で 130s から改訂)。auto-spawn (`--spawn-supervisor`) 経路も supervisor 起動 poll (`DEFAULT_SUPERVISOR_READINESS_TIMEOUT=200s`、起動専用 budget) と /route 本体 (full 230s) の 2 budget に分離 — 起動に時間が掛かっても /route の timeout が短縮されない。cold client は shim 側で timeout する代わりに deadline 内の `readiness:"starting"` 応答 (または ready 応答) を観測する。`tests/unit/test_proxy_route_timeout.py` が「3 bound の和以上」および「起動後の /route が full bound を使う」ことを fence
4. **supervisor test slowdown**: readiness 待ちを入れた supervisor 系 test が poll sleep 分遅い (CI 時間増)

## 残TODO

1. **R3 判断 (user)**: 3-arm 再較正 (上記 a) or `"or"` 維持 (b)。composite 実装・較正基盤・fail-closed は本番に乗っており、昇格は config 1 つ + artifact 配置 (`--emit-artifact` 手順は Troubleshooting)
2. **commit split**: Phase U 全体が未 commit。WP 単位 (または 1: WP-1+5 昇格 / 2: WP-2 / 3: WP-3 / 4: WP-4 / 5: WP-6+8 (readiness/snapshot/proxy contract) / 6: WP-7 docs) への分割を推奨 (Phase T handoff と同じ趣旨)
3. **production backend を WP-8 code で再起動** — 稼働中 backend は WP-8 前の code のまま (shim timeout 10s / flag-OFF route 登録 / snapshot trust 無し)。merge 後に CLAUDE.md「code deploy 時の backend 再起動」を実行
4. BM25 snapshot (390MB) の backup 対象への取り込み判断
5. 2,053 orphan nodes の調査 (Phase T から継続、未着手)

## リスク

- 昇格 3 flag の ranking 広域変化 — promoted-combination suite + tier3/4/7 + production copy 比較で検証済み、env rollback 可
- BM25 snapshot の disk 消費 (corpus 比例、初回 390MB) と backup 時間
- **BM25 snapshot の trust policy (WP-8)**: unpickle 前に所有権 (euid 一致) と権限 (group/world-writable 拒否、data_dir 含む、symlink 拒否) を検査 — checksum は偶然の破損検出のみで真正性の根拠ではない。信頼境界は data_dir。旧 code が umask 0o002 環境で書いた 0o664 の snapshot は初回 boot で 1 回 rebuild (自己修復、データ損失なし)
- composite gate 昇格時の言い換え落とし — held-out FP/FN 報告なしには昇格しない契約は plan に事前登録済み
- readiness の積み上げ待ち (handler bounded wait 30s + supervisor poll 35s) は最長 ~65s になり得るが (SEMANTIC_READY 実測 7.3s に対し通常は発火しない)、WP-8 (round-2 fix) で shim 側 `/route` timeout が 3 bound 由来の derive 値 (現行 230s) に拡大されたため cold client は待ちの途中で timeout せず `readiness:"starting"` を観測してから接続する

## ロールバックメモ

各 knob の env (standalone では直接、multiverse では supervisor allowlist 経由で伝播):

| 対象 | env | 効果 |
|---|---|---|
| direct qualification | `GAOTTT_DIRECT_QUALIFICATION_ENABLED=false` | Stage 3 legacy 順序 (rescue も無効) |
| ttt qualification | `GAOTTT_TTT_QUALIFICATION_ENABLED=false` | Stage 4 learn set = all reached |
| explore MMR | `GAOTTT_EXPLORE_DIVERSIFIED_PRESENTATION_ENABLED=false` | Stage 6 legacy presentation |
| raw-top rescue | `GAOTTT_DIRECT_RESCUE_RAW_RANK=0` | rescue 無効 (Stage 3 は維持) |
| readiness protocol | `GAOTTT_READINESS_PROTOCOL_ENABLED=0` | legacy lazy 初回生成 (route も未登録 = 404 → supervisor は即時 legacy、WP-8 で退化解消) |
| BM25 background build | `GAOTTT_BM25_BACKGROUND_BUILD_ENABLED=0` | 同期 build |
| BM25 snapshot | `GAOTTT_BM25_SNAPSHOT_ENABLED=0` | 永続化/load 無効 (毎回 build) |
| supervisor /route 待ち | `GAOTTT_ROUTE_READINESS_TIMEOUT_SECONDS` (supervisor 側) | poll deadline 短縮 |
| ambient composite | `GAOTTT_AMBIENT_GATE_MODE=or` | (default が既に "or") |

- allowlist 本体: `gaottt/multiverse/tuning_env.py` の `RUNTIME_TUNING_ENV_ALLOWLIST` (28 変数、WP-8 で composite 5 件追加)。**恒久** rollback は supervisor を起動する service 定義 (systemd) に env を置く (`GAOTTT_CONFIG` は伝播しない)
- 較正 artifact: `data_dir/ambient_composite_reference.json` — composite 未使用なら削除しても `"or"` 運用に影響なし

## 次の担当者へのメモ

- **最初に R3 の user 判断を取りにいくこと** (残 TODO 1)。判断次第で Phase U の acceptance が partial → done に変わる。較正の生 data は `/tmp/opencode/calib-v2-full.log` (v2 probe set) と [docs/notes/phase-u/ambient-composite-calibration.md](../notes/phase-u/ambient-composite-calibration.md)
- production backend は Phase U WP-1〜7 コードで稼働中 (2026-08-26 restart 済み) が **WP-8 (proxy timeout / readiness rollback / snapshot trust) はまだ稼働 backend に載っていない** — merge 後に CLAUDE.md「code deploy 時の backend 再起動」を忘れずに (残 TODO 3)
- multiverse で knob を変えるときは `scripts/diag_config.py --knobs-only` で backend の effective value を確認してから (allowlist 経由かどうかの見極め)
- 障害 node の順位調査は `scripts/diag_target_trace.py` (**production copy のみ**、本番 DB に直接使わない)
