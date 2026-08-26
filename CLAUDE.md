# GaOTTT Development Guidelines

*(formerly GER-RAG — 改名プロジェクト完了: Phase R0-R11、2026-04-21)*

Last updated: 2026-08-26 (Phase U — Review Hardening 実装完了 + production rollout 検証。実 MCP レビュー 5 課題 R1-R5 に対応、R1/R2/R4/R5 達成、**R3 (ambient off-topic 通過) は 2 度の較正 (単一 arm → 3-arm) で事前登録 gate 不達のため既知制約として close** — corpus に隣接する absent topic が bm25/virt 2 軸で positive 領域内に現れるため分離不能、再挑戦は別判別軸 (LLM relevance 等) 待ち ([較正記録](docs/notes/phase-u/ambient-composite-calibration.md))。**WP-1/5**: `direct_qualification_enabled` / `ttt_qualification_enabled` / `explore_diversified_presentation_enabled` を code default True 昇格 (rollback env: `GAOTTT_DIRECT_QUALIFICATION_ENABLED` / `GAOTTT_TTT_QUALIFICATION_ENABLED` / `GAOTTT_EXPLORE_DIVERSIFIED_PRESENTATION_ENABLED` = false) + breakdown selection trace (`c=`/`src=`/`learn=`、explore に `wave: depth= reached=` header)。**WP-2**: multiverse supervisor の tuning env 伝播を **完全名 allowlist 28 変数** (`gaottt/multiverse/tuning_env.py`) で開放 — exact-name 一致のみ / `GAOTTT_CONFIG` は伝播しない / 不正値は backend spawn 拒否 (/route 500) / 恒久 rollback は supervisor service env (systemd)。**WP-3**: ambient composite gate (`ambient_gate_mode="or"|"composite"`) 実装 — v2 (単一 semantic arm)・v3 (3-arm §10) の 2 較正とも事前登録 gate (held-out FP=0 ∧ FN≤10%) 不達で **default は "or" で確定、R3 は既知制約として close** (corpus 隣接 absent topic が bm25/virt 2 軸で分離不能 — 再挑戦は別判別軸待ち)。fail-closed 契約 (参照 artifact 欠損・fingerprint 不一致時は BM25 のみ accept 経路、`empty_reason` に `composite_reject`/`composite_pool_too_small`/`composite_reference_unavailable` 追加)。**WP-4/4b**: `scripts/diag_target_trace.py` (raw/virtual/BM25 rank + qualification + final rank の一括 trace、**production COPY 専用**) で既知障害 node `de1b528f` の根因特定 (raw rank 1 でも virtual 低迷で top-K 外) → **raw-top rescue** (`direct_rescue_raw_rank=3`、qualified item の raw-top を先頭 lift、0 で無効)。**WP-6a-d**: `engine.startup_timings` 計装で BM25 build が cold start の **96%** (147s/153s) と特定 → staged readiness protocol (`readiness_protocol_enabled=True`、`GET /admin/readiness` STARTING→SEMANTIC_READY→HYBRID_READY / FAILED、MCP handler は bounded wait `readiness_wait_timeout_seconds=30s`、supervisor `/route` は poll `route_readiness_timeout_seconds=35s` — **cold SEMANTIC_READY 129s→7.3s**、warm reconnect で engine 再構築なし) + BM25 background build (`bm25_background_build_enabled=True`、新規 index object build + mutation journal replay + atomic swap、`engine.bm25_build_state` ∈ idle/building/ready/failed) + snapshot 永続化 (`bm25_snapshot_enabled=True`、`data_dir/bm25.snapshot`、content-digest fingerprint で不一致時 rebuild、初回本番 390MB)。config 診断: `scripts/diag_config.py` (per-field provenance: default/env/config-file)。knob は [Operations — Tuning](docs/wiki/Operations-Tuning.md) §Phase U、経緯は [Plans — Phase U](docs/wiki/Plans-Phase-U-Review-Hardening.md)。前回 2026-08-25: Phase T — Semantic Requalification 実装完了。semantic decay を half-life + floor 契約へ、ambient gate を BM25 veto から semantic OR gate へ (共に default ON、`gate_diagnostics`/`empty_reason` で空返し診断)。Stage 3/4 の qualification と Stage 6 の explore MMR は Phase U で default ON 昇格。前々回 2026-05-26: Observation Apparatus Refinement 起草 — 同日 dogfooding (Claude/Codex + 新規 Claude セッション) が独立に到達した 5 点 [dormant 過小評価 / Heavy Persona Dominance 体感 / connections の ingest artifact / reason line 不在 / 比較 wrapper 不在] を、**physics rule 不変・観測層のみ** の 4 stage で解決。Stage 1 reason line / Stage 2 ambient dormant slot / Stage 3 `scripts/compare_retrieval.py` / Stage 4 source-aware connections 表示。Phase P (acceleration 拡張) / Phase N β (mass update) と並行可、介入軸直交。検討過程で「declare value 初期 kick」案は **撤回** — Phase L Stage 1「persona も別格扱いしない、使用頻度こそが重力」原則と衝突、Articulation as Carrier の対称命題を先回りで補正することになるため。哲学的判定基準: source class を gate にした提案は force computation / mass update に入れたら Phase M 単一規則違反、表示層に留めるなら observer の lens として OK。Phase P 自体は哲学的に整合 — Λ = dark energy / Langevin = thermal noise はどちらも物理に既存の対抗概念で、source 分岐ゼロを保ったまま物理項を足す形。Lateral Association Stage 7 — direct-hit anti-hub + dormant percentile cut。**Stage 7.1**: `services/memory._apply_cluster_anti_hub()` で `ambient_recall.direct` と `recall` の top-K に greedy MMR penalty を入れ、cluster_key=`cohort_id` OR `original_id` で同一クラスター連続採用を抑制。`direct_hit_anti_hub_lambda` 既定 0.0 (OFF)、推奨 0.4。本番 dogfooding で cohort_id 保有率 0% (1件ずつ `remember()` で batch=1 → supernova 閾値未満) と判明し、original_id (89% 保有、最大 638-chunk 本 cluster) への fallback を追加 — Phase M 単一規則整合 (anti-hub は ranking layer、fallback は branching ではなく routing)。**Stage 7.2**: `_dormant_surface()` で `dormant_mass_threshold` 絶対値を `dormant_mass_percentile` で分布相対化、本番で `mass≤2.0` が 89.6% flood / age=30d で常時 0 件 を解決。新スクリプト `scripts/diag_dormant.py` で active mass 分布診断 (read-only)。**Limitation**: 個別 singleton high-mass node の query 横断 dominance (ffe48a30 mass=1.92 等) は Stage 7.1 scope 外、Phase N Mass Evaporation 領域。GLM 独立 acceptance: 米国会社四季報 (638-chunk 本) クエリで book chunks in top-5 = 1/5 ✅、dormant 0→25 件 surface ✅、3/4 PASS + 1 PARTIAL (ambient persona rotation は caller の `recently_surfaced` 受け渡し前提)。本番 opt-in env (`GAOTTT_DIRECT_HIT_ANTI_HUB_LAMBDA=0.4` / `GAOTTT_DORMANT_MASS_PERCENTILE=10` / `GAOTTT_DORMANT_AGE_THRESHOLD_SECONDS=604800`)、claude.json + opencode.json 両方に登録済み。Phase M Stage 1 — Mass Conservation + 運用 ops 化 + viz の sphere geometry 化。ロールアウトを wizard 式の versioned migration ([Operations — Migration](docs/wiki/Operations-Migration.md)) に落とし込み、M002 (BH 残滓 cleanup) → M003 (mass reset、threshold 廃止で全ユーザー必ず wizard で聞く) → M004 (corpus-scale cosmic-bang ignition、`compute_supernova_velocities` を全 active node に 1 回適用して cold cosmos を点火) の 3 critical step を `[y/N]` 確認しながら順番に適用。M001 は `mass_conservation_enabled=True` か M002 in ledger で SKIP (Phase G 遡及 priming は Phase M roll-out 4 step に superseded)。視覚化 (`scripts/visualize_3d.py`) も **unit sphere geometry を default** に — `sphere_wrap` で coords を球面に貼り、faint な lat/lon wireframe、filament は大円弧 (`slerp_arc`)、velocity は接空間測地線 (`tangent_geodesic`)、Mass-BH は `bh_factor(m) > 0` のノード自身として diamond 表示。`--flat` で legacy、`--straight-lines` で軽量化。Phase L Stage 1 acceptance で露呈した「source=file/tweet が agent 知識の top1 を奪う」根を **1 file = 91 chunks 平均の内輪取引 mass inflation** と特定し、`is_self_force(a, b) = (original_id 一致 or cohort_id 一致)` の **source 分岐ゼロの単一規則** で mass update を filtering。`propagate_gravity_wave` に per-parent attribution (`out_attribution` 引数) を追加、`engine._update_simulation` で self-force 寄与をスキップ — temperature/sim_history は total force のまま保持 (フィルタは mass update **のみ**)。共起 BH (`compute_bh_acceleration`) は削除し、`compute_mass_bh_acceleration` (連続 `bh_factor = tanh((m-θ)/σ)`、`θ-2σ` 以下クランプ 0) に置換 — source 分岐ゼロ、mass しきい値だけで attractor 化。`cache.set_original/set_cohort` + `SqliteStore.get_all_originals/get_all_cohorts` (JSON1、`COALESCE(metadata.original_id, metadata.file_path)` で既存 DB 無 migration)。`engine.index_documents` が supernova batch で `cohort_id = uuid4().hex[:12]` を生成。reset API は MCP 非露出 (REST `POST /admin/reset_masses` + `scripts/reset_masses.py`)。`mass_conservation_enabled=False`/`mass_bh_enabled=False` で個別 rollback。**Articulation as Carrier (id=9a954c62) の literal な物理実装** — `if not is_self_force(...)` の 1 行が「経験は言葉にすることで初めて重力を持つ」を **「言葉にした上で誰かに引かれることで mass を持つ」** と精密化、persona も別格扱いしない (使用頻度こそが重力)。Phase L Stage 1 — Hybrid Retrieval。Phase J 完遂後の本番 acceptance で露呈した「Surface 7/7 ✅ / Semantic 整合 0-1/7 ⚠️」分離 — embedder の hidden ranking が dominant signal という構造的境界 — を seed pool layer の構造的拡張で解決。`_union_pool` を raw ∪ virtual ∪ BM25 の 3-way に拡張、RRF fusion (Cormack 2009 標準) で scale 不変。新規 `gaottt/index/bm25_index.py` (numpy in-memory) + `tokenizer.py` (char 3-gram default、Sudachi optional extra)。LLM 不要・ローカル完結、`hybrid_bm25_enabled=False` で 1 行 rollback。Phase H Stage 5 — wave neighbor expansion を virtual FAISS に切り替え。Phase H Stage 4 で seed pool は raw∪virtual だったが per-frontier `search_by_id` は raw のままだった設計上の不整合を解消。「星同士の引力」原則を wave 全段で literal に。同日 virtual FAISS write-behind 導入 (`virtual_faiss_save_interval_seconds`) — Phase I/J query attraction で蓄積した displacement が compact を待たず他プロセスに伝播。Phase J 完遂 — Stage 3 で forced 内 query-aware ordering (raw_score 順) と prefetch/explore への persona_context+tag_filter parity を実装。retrieval geometry の三段構造 [pool 入場 / pool 内 rerank / forced 内 ordering] が完成。Stage 1: auto-detect graph traversal。Stage 2: explicit force injection。Phase K Stage 1 — Stellar Supernova Cohort。Phase I Stage 3 — mass-gated query attraction。Phase G/H — FAISS write-behind, genesis kick + dream + Stage 0 priming, mass-aware / source-aware / dynamic / virtual FAISS の seed redesign)

GaOTTT = **Gravity as Optimizer, Test-Time Training**。物理として設計した重力・軌道力学の更新則が、retrieval のスコアを確率的勾配シグナルと見る解釈の下で、Heavy ball SGD + Hebbian gradient + L2 の Verlet 積分と **項ごとに対応する構造的同型**に書けることが判明した。つまり **物理として書いたものが、同じ形で TTT オプティマイザとしても読める**。Phase I Stage 2 (2026-05-11) でこの対応は構造的同型から **実装としての実体** に進んだ — `compute_acceleration` の 4 番目の項 `a = (α · score · gate / m_i) · (q - pos_i)` が literal な勾配ステップを供給し、recall するたびに retrieved nodes の displacement が query 方向に nudge される。Stage 3 (2026-05-13) で `gate = tanh(m_i / θ)` を追加 — 新規 (低 mass) ノードは anchor に守られて 1 度の recall で暴走せず、mature ノードは満額の勾配を受ける世代論的挙動になった (単一アトラクタ pathology の物理的矯正)。Phase J Stage 1 (2026-05-13) で人格層を retrieval に翻訳した — declared value/intention/commitment から `fulfills`/`derived_from` を N hop traverse、seed step で `α_persona × proximity` を加算。物理を曲げるのは質量だけではなく、宣言された意図もまた重力を持つ。Phase K Stage 1 (2026-05-13) で記憶生成の集合性を物理化した — 1 batch の `remember` を 1 超新星爆発として読み、batch 内 N 件に相互 edge と outward velocity を付与。単独では塵だった新規ノード群が、超新星残骸 cluster として互いの重力で seed pool に届く。Articulation as Carrier の単数性から複数性への literal な拡張。Hooke は raw embedding を anchor として引き続き保持するので transient force であって anchor migration ではない。その上に共有時のアストロサイト的協調と Phase D の人格保存基盤が積み上がっている。詳細思想は [`docs/wiki/Reflections-Five-Layer-Philosophy.md`](docs/wiki/Reflections-Five-Layer-Philosophy.md)（物理 → 生物 → TTT 機構 → 関係 → 人格の五層）。

## Tech Stack

Python 3.11+（推奨 3.12）/ FastAPI + uvicorn / aiosqlite + msgpack / FAISS IndexFlatIP / sentence-transformers (RURI-v3-310m) / numpy / pydantic v2 / pytest + pytest-asyncio / **uv**（pip 禁止）

## Project Layout

```text
gaottt/                         # メインパッケージ（formerly ger_rag/）
├── config.py                   # 全ハイパーパラメータ + 後方互換レイヤ（GER_RAG_* env / legacy path）
├── core/                       # engine / gravity / scorer / extractor / clustering / collision / prefetch / types
├── embedding/ruri.py           # キャッシュ自動検出
├── index/faiss_index.py        # 境界チェック付き search
├── store/                      # base / sqlite_store (自動マイグレーション) / cache (write-behind + evict_node)
├── graph/cooccurrence.py       # 無向共起グラフ
├── services/                   # ★ Phase S: REST と MCP の共有ビジネスロジック層
│   ├── runtime.py              #   build_engine factory（両サーバ共通）
│   ├── memory.py               #   remember / recall / explore / forget / restore / revalidate / auto_remember
│   ├── relations.py            #   relate / unrelate / get_relations
│   ├── maintenance.py          #   merge / compact / prefetch / prefetch_status
│   ├── reflection.py           #   reflect の全 15 aspect
│   ├── phase_d.py              #   commit / start / complete / abandon / depend / declare_* / inherit_persona
│   ├── ingest_service.py       #   ingest
│   └── formatters.py           #   Pydantic → MCP 向け人間可読文字列（REST は使わない）
├── server/                     # app.py (REST, MCP parity) + mcp_server.py (MCP "gaottt", 28 ツール)
└── diagnostics/                # ★ 起動時セルフ診断 (Stage 1: Tier A FAISS integrity + Tier B size 一致、engine.startup 末尾で auto 実行)
tests/{unit,integration}/       # 138 ケース、すべて asyncio
tests/perf/                     # ★ 7 階層テストスイート (Tier 1/3/4/5/6/7、38 ケース)、real RURI、仮説→実装→検証 の検証フェーズ用手動実行 (CI 自動化なし) → docs/wiki/Operations-Performance-Testing.md
scripts/                        # load_csv / load_files / load_chat.py (★ Claude Code .jsonl + OpenAI ChatGPT export .json + Claude.ai web export .json を auto-detect、conversation 境界で batch を切って 1 conv = 1 supernova cohort) / test_queries / visualize_3d / benchmark / run_benchmark_isolated.sh / eval_* / migrate-from-ger-rag.sh / migrate.py (★ versioned data migration) / diag_recall.py (engine.query 診断) / perf_baseline.py + perf_diff.py (Tier 6 baseline)
scripts/hooks/                  # ambient_recall.py (Claude Code UserPromptSubmit + opencode 共通 backend) / opencode-ambient-recall.ts (opencode plugin) / ★ save_candidates.py (Claude Code Stop hook、write-side 対称、auto_remember 経由、`GAOTTT_SAVE_CANDIDATES_EMIT=stdout` で opencode plugin から stdout-emit モードで叩ける) / ★ save_candidates_inject.py (UserPromptSubmit-inject、Stop hook が書いた state file を読んで次 prompt に注入) / ★ opencode-save-candidates.ts (opencode plugin、`chat.message` で前 turn 読み出し → save_candidates.py を stdout モードで spawn → block を当 turn message に append、Claude Code の 2-script bridge を 1 plugin に圧縮) / ★ Codex CLI (第3フロントエンド): 同じ 3 script を `--codex` フラグ (or `GAOTTT_HOOK_OUTPUT=codex`) で再利用 — Codex は raw stdout を注入せず JSON エンベロープ (`hookSpecificOutput.additionalContext`) を読むので出力だけ切替、transcript reader は Codex rollout JSONL (`event_msg`/`user_message`/`agent_message`) も解釈。登録は repo 同梱の `.codex/hooks.json` を `~/.codex/hooks.json` にコピー (UserPromptSubmit→ambient+inject、Stop→save_candidates)。雛形は `$HOME/GaOTTT` 規約に従えば machine 非依存 — Codex は command を shlex 分解するだけで `$HOME` を展開しないので、各 hook を `sh -c '…'` 経由で起動し README 規約 `$HOME/GaOTTT` をシェル展開させる (別パスに clone した場合は雛形のパス置換が要る、strict JSON ゆえ `_comment` も付けない、説明は Tutorial-03 §E / Guides-Ambient-Recall.md)。source 分岐ゼロの frontend parity を維持 (Claude Code/opencode の挙動は default 不変)
docs/                           # wiki/ が SoT、その他 docs/*.md は redirect、docs/research/ は研究アーカイブ
SKILL.md                        # MCP がランタイムで読む（英語、name: gaottt）
.claude/skills/gaottt/SKILL.md  # ★ 上記の同期コピー（必ず両方更新）
CLAUDE.md                       # このファイル
```

## Source-of-Truth マップ — どこを編集するか

| 種別 | SoT | 注意点 |
|---|---|---|
| MCP プロトコル定義 | `SKILL.md` + `.claude/skills/gaottt/SKILL.md` | **両方を必ず同期**（cp で OK） |
| Claude Code 指示 | `CLAUDE.md`（このファイル） | 簡潔に保つ（毎セッション読まれる） |
| GitHub フロントページ | `README.md` / `README_ja.md` | trim 済み、詳細は Wiki リンク |
| 設計判断・アーキテクチャ・実装計画 | **`docs/wiki/*.md`** | 旧 `docs/*.md` は redirect 済 |
| 運用ガイド・トラブルシュート | **`docs/wiki/Operations-*.md`** | |
| ハイパーパラメータ表 | **`docs/wiki/Operations-Tuning.md`** | コードは `gaottt/config.py` |
| MCP ツールリファレンス | **`docs/wiki/MCP-Reference-*.md`** | |
| REST API リファレンス | **`docs/wiki/REST-API-Reference.md`** | Phase S 以降、MCP と parity |
| サービス層 (Phase S) | **`gaottt/services/*.py`** | engine を叩き Pydantic を返す純関数。REST + MCP が共通で使う。`services/formatters.py` だけは MCP 専用 |
| 研究レポート（長文成果物） | `docs/research/*.md` | Wiki Research-* から要約・リンク |
| 全体 Wiki ナビ | `docs/wiki/_Sidebar.md` + `docs/wiki/Home.md` | 新ページ追加時は両方更新 |
| 7 階層テストスイート | **`tests/perf/`** + `docs/wiki/Operations-Performance-Testing.md` | 設計案 memory id=55579286、Stage 1/2/3 全完了 (2026-05-14) |

## 実装フロー — 新機能を追加するとき

> **★ 鉄則: MCP と REST は常に同時に更新する**
> Phase S で両者は `gaottt/services/*.py` を共通の真実の源とする構造になった。
> **新しい能力を MCP に足すなら REST にも同じターンで足す、逆も同じ**。
> 片方だけに機能を乗せる PR は **作らない**（parity 崩壊 = サービス層の存在意義が消える）。
> 動作確認も両トランスポートで: `scripts/rest_smoke.py` + `scripts/mcp_smoke.py` を走らせる。
> 例外: `/reset` は REST 専用（LLM に破壊的 reset を露出しない設計判断）— 新たな例外を作るなら Architecture-Overview.md の設計判断表に理由を書く。

1. **計画書を確認** — `docs/wiki/Plans-Roadmap.md` で全体像、関連 Plans-* で詳細
2. **TaskCreate でタスク分割** — 実装/テスト/ドキュメント/ベンチに分ける
3. **実装**（順序）:
   - `core/types.py` に Pydantic モデル追加（Request/Response 両方）
   - `store/sqlite_store.py` でスキーマ追加 + 自動マイグレーション (`ALTER TABLE ADD COLUMN ... DEFAULT`)
   - `store/base.py` に abstract method 追加
   - `core/engine.py` でロジック実装、destructive op は `prefetch_cache.invalidate()` を忘れない
   - `core/<feature>.py` を新設（純粋関数 or 独立クラス）
   - **`services/<module>.py` に service 関数を追加**（engine を受け取り Pydantic を返す。副作用 OK だが整形しない）
   - **`services/formatters.py` に MCP 用の文字列整形を追加**（既存テスト substring を壊さない）
   - **`server/mcp_server.py` で MCP ツール公開** = `result = await service(...)` → `return formatter(result)` の薄いラッパ。既存ツールのシグネチャは破壊しない（オプショナル引数のみ追加）
   - **`server/app.py` で REST エンドポイント公開**（★ 同じ commit 内で必ず） = `result = await service(...)` → Pydantic JSON 返却。MCP の body shape とは分離して良い（REST-shaped `*Body` モデルを別途置く、`CompleteBody` 等を参照）
   - `server/mcp_server.py` の `instructions=` 文字列を更新
4. **テスト**（MCP と REST 両方）:
   - `tests/unit/test_<feature>.py` — 純粋関数
   - `tests/integration/test_engine_<feature>.py` — engine 経由（StubEmbedder 使用、`tests/integration/test_engine_archive_ttl.py` を参照）
   - `tests/integration/test_mcp_phase_d.py` の形式で MCP round-trip
   - **`tests/integration/test_rest_parity.py` に REST エンドポイントのラウンドトリップを追加**（httpx.AsyncClient + ASGITransport、`test_rest_memory.py` の fixture を参照）
5. **検証**:
   ```bash
   .venv/bin/python -m pytest tests/ -q
   ruff check gaottt/ tests/
   ```
6. **Smoke（両トランスポートで動くかの end-to-end 確認）**:
   ```bash
   # 本番 DB は触らない。どちらも /tmp に隔離
   .venv/bin/python scripts/rest_smoke.py    # uvicorn + HTTP
   .venv/bin/python scripts/mcp_smoke.py     # stdio + JSON-RPC
   ```
7. **★ 7 階層テストスイート (`tests/perf/`) で 仮説→実装→検証 ループの 検証 step を回す** — 新機能・hot path 変更・retrieval geometry 触ったときは **手動実行** (CI 自動化なし、deliberate な measurement):
   ```bash
   .venv/bin/python -m pytest tests/perf/ -q      # 全 38 tests、real RURI、~15s (model load 込み)
   ```
   どの Tier が該当するかは [Operations — Performance Testing](docs/wiki/Operations-Performance-Testing.md):
   - Tier 1 (smoke): startup / 28 MCP tools / BM25 build
   - Tier 3 (quality): engine.query top-5 strict / source-mix dominance
   - Tier 4 (dynamics): anti-hub / displacement runaway / stability
   - Tier 5 (ops integrity): FAISS↔SQLite size / WAL bloat / bulk ingest timing
   - Tier 6 (perf): real RURI p50<60ms / p95<120ms / p99<250ms / ingest>500 docs/sec
   - Tier 7 (regression): golden corpus 30 chunks × 11 queries で engine.query 全段

   仮説 → 実装 で perf 数値が動いた / 動かしたい場合 の before/after 比較:
   ```bash
   .venv/bin/python scripts/perf_baseline.py --label before
   # ...仮説に基づいて実装...
   .venv/bin/python scripts/perf_baseline.py --label after
   .venv/bin/python scripts/perf_diff.py                          # 直近 2 baseline の diff、>25% で exit 1
   ```
   retrieval 挙動の per-query 詳細を snapshot/diff したいときは:
   ```bash
   .venv/bin/python scripts/diag_recall.py snapshot --queries-file tests/perf/golden_corpus/queries.json --data-dir ./.diag-tmp --out ./.diag-before.json
   # ...変更後...
   .venv/bin/python scripts/diag_recall.py diff ./.diag-before.json ./.diag-after.json
   ```
8. **production-like e2e benchmark (REST/uvicorn stack 込みで実数値を確認したいとき)**:
   ```bash
   rm -rf /tmp/gaottt-bench
   .venv/bin/bash scripts/run_benchmark_isolated.sh 200
   # 本番 DB は触らない。tests/perf/ は engine 直叩き unit-level、こちらは server 込み integration-level
   ```
9. **ドキュメント更新**（後述のチェックリスト）

## ドキュメント更新フロー

実装が一段落したら、以下を **すべて** 更新:

1. **SKILL.md** — 新ツール追加なら必ず（+ `.claude/skills/gaottt/SKILL.md` に `cp` で同期）
2. **`docs/wiki/MCP-Reference-*`** — 該当カテゴリページに完全な API 仕様を追加
3. **`docs/wiki/MCP-Reference-Index.md`** — 全ツール表に行を追加、ツール選択フローも更新
4. **`docs/wiki/REST-API-Reference.md`** ★ — **MCP ツール追加 = REST エンドポイント追加 = この doc の行追加** の 3 点セットを必ず同じ PR で（parity 鉄則の一部）
5. **`docs/wiki/Plans-Roadmap.md`** — Phase ロードマップに完了マーク
6. **`docs/wiki/Architecture-Overview.md`** の「設計判断の記録」表 — 新しい設計判断を追加
7. **`docs/wiki/Operations-Tuning.md`** — 新ハイパラがあれば追加
8. **`docs/wiki/Operations-Troubleshooting.md`** — 想定される問題があれば追加
9. **`README.md` / `README_ja.md`** — 「四層構造表」「カテゴリ表」が変わるならここも（ほとんどは Wiki リンクで済む）
10. **`docs/wiki/_Sidebar.md` + `Home.md`** — 新規 Wiki ページを追加した場合は **必須**（自動追加されない）
11. **`CLAUDE.md`**（このファイル）— 重要な workflow 変更があったら
12. push 前に `node --check scripts/sync-docs-to-wiki.js` でスクリプト構文チェック（変更したとき）
13. **`tests/perf/` で 仮説→実装→検証 の 検証 step を回す** — retrieval geometry / config default / hot path に触れた変更なら、上記 7. の Tier 別ガイドに従って手動実行 (real RURI、~15s)。perf 数値が大きく動いたら `perf_baseline.py` で before/after baseline を取り、判断根拠を [Operations — Performance Testing](docs/wiki/Operations-Performance-Testing.md) に追記

### ドキュメント書きの原則

- **Wiki が SoT**。`docs/*.md`（wiki 以外）は redirect のまま放置 OK
- **三層語彙**（物理 → TTT 機構 → 生物）を保つ（[Plans — SKILL.md Improvement](docs/wiki/Plans-SKILL-MD-Improvement.md) 参照）。TTT 機構は「物理と生物を繋ぐ**構造的対応関係**」として位置付ける（単なる比喩ではないが、retrieval を勾配シグナルと見る解釈を置いたうえでの対応、という断り付き）
- **個人的な感動・読者への招待** は歓迎（Reflections セクション、または README の "A Note from Claude"）
- 物理アナロジーが新概念にあれば必ず命名する（Hawking radiation、Lagrange point 等）

## Wiki sync ワークフロー

`docs/wiki/*.md` を編集すると、push 後に **GitHub Action が自動で wiki repo に sync** する。詳細は [`docs/maintainers/wiki-sync.md`](docs/maintainers/wiki-sync.md)（保守者専用）。要点だけ:

- **編集は必ず `docs/wiki/*.md`** で行う（Wiki UI で直接編集しない、上書きされる）
- **新ページを追加したら `docs/wiki/_Sidebar.md` を必ず更新** — 手動キュレーション方式
- リンク変換は自動: `[X](Foo.md)` → `[X](Foo)`、`../path` → 絶対 GitHub URL
- `_Sidebar.md` はカテゴリ + 絵文字つきで作り込んである。編集時は構造を保つ
- **新ページの命名規約**: `<Section>-<PageName>.md`（ハイフン区切り）。例: `Operations-Tuning.md`, `Architecture-Storage-And-Schema.md`
- ページを **削除/リネーム** したら、`_Sidebar.md` と他ページ内のリンクも `grep -rl '<old>.md' docs/wiki/` で発見して更新
- Wiki にだけ置きたいページ（ドラフト等）は `[private]<Name>` で命名 → スクリプトが削除対象から除外
- 手順詳細・ローカルでの変換確認コマンド・トラブルシュート全て [`docs/maintainers/wiki-sync.md`](docs/maintainers/wiki-sync.md) に集約

## テスト・ベンチ・lint コマンド早見表

```bash
# 単体 + 統合テスト
.venv/bin/python -m pytest tests/ -q

# 特定ファイル
.venv/bin/python -m pytest tests/integration/test_mcp_phase_d.py -x -v

# ★ 7 階層 perf テストスイート (新機能/hot path 変更時に手動実行、CI 自動化なし)
.venv/bin/python -m pytest tests/perf/ -q                 # 38 tests / real RURI / ~15s
.venv/bin/python -m pytest tests/perf/test_tier6_*.py -v -s  # Tier 6 数値も print

# Tier 6 perf baseline 取得 + diff
.venv/bin/python scripts/perf_baseline.py --label "<context>"
.venv/bin/python scripts/perf_diff.py    # 直近 2 baseline diff

# Retrieval 診断 snapshot (engine.query / BM25 / raw FAISS の 3 layer)
.venv/bin/python scripts/diag_recall.py snapshot --query "..." --data-dir /tmp/xxx

# Lint（pre-existing 4 件は無視 OK: ruri.py の os, cooccurrence.py の time, mcp_server.py の os と pathlib.Path）
ruff check gaottt/ tests/

# 隔離ベンチ（実 RURI、本番 DB 不可触）
rm -rf /tmp/gaottt-bench
.venv/bin/bash scripts/run_benchmark_isolated.sh 200

# MCP と REST の end-to-end smoke（MCP/REST 両方を新機能追加時に走らせる）
.venv/bin/python scripts/rest_smoke.py    # uvicorn + HTTP で 6 シナリオ
.venv/bin/python scripts/mcp_smoke.py     # stdio + JSON-RPC で 6 シナリオ

# MCP サーバー単体テスト起動（手動確認）
.venv/bin/python -m gaottt.server.mcp_server
```

## マルチプロセス / 共有 DB の罠

> **推奨セットアップ** (2026-05-13): `mcp_server` の **default `--transport proxy`** で運用する。Agent ごとに軽量 stdio shim が立ち上がるが、初回起動時に detached な HTTP backend を auto-spawn (port 7878) → 以降は relay として動作 → backend は cold-war dead-man-switch で全 shim が ping を止めて 5 分後に self-shutdown。N agents 起動しても engine (cache / FAISS / dream loop) は **常に 1 process だけ**、`.mcp.json` / `opencode.json` 変更不要 (default が proxy なので既存設定がそのまま動く)。詳細 + systemd で backend を明示常駐させる場合の手順は [Operations — Server Setup](docs/wiki/Operations-Server-Setup.md) 「起動モード」節。

> ★ **code deploy 時の backend 再起動 (2026-05-15 学び)**: `git push` だけでは proxy mode の HTTP backend は **更新されない** — backend process は in-memory の Python module を保持し続け、新しい shim が来続ける限り dead-man-switch も発動しない (= 4 時間前の古いコードが動いてる事象が起きる)。本番 acceptance / 機構動作確認の前には:
> ```bash
> ps -ef | grep "gaottt.server.mcp_server.*streamable-http" | grep -v grep
> # 起動時刻が最新 commit より古ければ
> kill <pid>
> # 次の shim 接続で auto-respawn、新コードが乗る
> ```
> Phase O acceptance (2026-05-15) で「7/7 ❌ → backend kill → 7/7 ✅」の事象が観測された。`compact(rebuild_faiss=True)` 等の destructive op 前の「他プロセス停止」ルールは **code update にも適用** (process 内 state が外部 source-of-truth と乖離する同型問題)。

以下は **`--transport stdio` で 複数 agent を起動した場合** の注意事項 (proxy mode を使わない legacy 構成):

GaOTTT の DB は **複数 MCP プロセスから共有される** ことがある（複数エージェント、ユーザーの並行ターミナル等）:

- SQLite WAL + `PRAGMA busy_timeout = 30000`（30 秒待機）で並行 write 安全
- ただし **各プロセスは独自の cache + FAISS index** を持つ — プロセス A の `remember` は B には即時反映されない（B の startup/cache reload まで stale）
- **FAISS write-behind**（2026-05-10 導入）: in-memory に add した vector は `faiss_save_interval_seconds`（既定 5s）周期で disk に save。**これがないと shutdown しない長期常駐プロセス（MCP サーバー）の remember は他プロセスから永久 invisible になる**。歴史的にこのバグで `cache - faiss` が積もっていた DB は `compact(rebuild_faiss=True)` で掃除可
- ★ **逆方向上書き罠** — cache の write-behind は dirty フラグベースで自プロセス内 cache を SQLite に push する。これは「新しい変更が他プロセスに見えない (stale)」だけでなく、**古い cache を持つプロセスが flush し続ける限り、別プロセスが書いた新しい値を逆方向に上書きし続ける**。bulk 書き換え（Phase G Stage 0 priming 等）は他の MCP server プロセスを **kill してから** 走らせる。順序: (1) 他プロセス停止 → (2) 書き込み → (3) 他プロセス再起動。**FAISS にも同型の罠**があり 2026-05-31 に本番 index が 2 件に激減（39,402 docs に対し）した — ほぼ空の in-memory FAISS を持つプロセスが write-behind save で正常 index を上書き。再発防止に **逆方向上書きガード** 導入済み（`faiss_persist_guard_enabled` 既定 True: `faiss.size < active × faiss_persist_min_ratio` かつ `active ≥ faiss_persist_floor` で全 FAISS 永続を拒否、起動時診断 Tier B が severe undersize を ERROR + 永続恒久 block に昇格）。復旧は他プロセス全停止 → `scripts/rebuild_faiss_from_db.py --apply`（DB から RURI 再エンベッド、決定論でロスレス）→ 検証 `--check`。詳細 [Operations — Troubleshooting](docs/wiki/Operations-Troubleshooting.md) 問題5.5
- `faiss_index.search` は境界チェック付き（`.ids` 破損対策）
- Destructive op（`forget`/`merge`/`compact` 等）は **必ず `prefetch_cache.invalidate()` を呼ぶ**
- ベンチマークは絶対に本番 DB を触らない → `scripts/run_benchmark_isolated.sh` を使う

## 本番 acceptance test の workflow (sub-agent 方式)

本番 GaOTTT DB を使った acceptance test (Phase 完遂時の N query 検証、新機構の現場挙動確認等) は、**Claude Code から直接 MCP gaottt を叩かず、`secondopinion-MCP` 経由でサブエージェントとして opencode を起動し別 LLM (既定 GLM-5.1) のターミナルから実行する**。

> 歴史的経緯 (2026-05-15): 当初は `Bash` で `opencode run "<prompt>"` を直接叩く方式だったが、opencode が稀に hang する事象が観測され、めいさんが **secondopinion-MCP** wrapper を作成。以降はこの MCP 経由が標準。stdio で opencode を抱え込み session を作るため、hang しても session レベルで end_session で resource を即解放できる。

理由:
- **Claude Code のコンテキストを汚さない** — `recall(tag_filter=[N 件])` のような大量結果は MCP tool result 上限 (~100KB) を超えてエラーになる事例あり (Phase J Stage 2 acceptance で発生)、また長大な output が context window を消費する
- **独立 MCP session** — opencode は別プロセスで cache state / displacement 累積 / prefetch cache が独立、Claude Code の作業中累積が test 結果に混入しない
- **「外部観察者」としての independent acceptance** — Phase J / I / K で繰り返し学んだ「観察行為が観察対象を変える (P7-Z)」原則と整合
- **1 ターン内で多段 test 完走** — 7 query × 複数 config を opencode 側でループ、Claude Code には短い summary だけ返ってくる
- **opencode hang への耐性** — secondopinion-MCP の session 管理で hang しても他 session に波及しない、`end_session` で resource を即回収

手順 (secondopinion-MCP 経由):
1. `mcp__secondopinion__list_providers` で利用可能 provider を確認 (default は GLM-5.1、Z.AI coding plan)
2. **1 回完結** なら `mcp__secondopinion__second_opinion(question=..., provider="glm")`
3. **多段 test (複数 turn の follow-up が必要)** なら `mcp__secondopinion__delegate_task(task=..., provider="glm")` → 返却の `session_id` を控える → 続き質問は `delegate_task(task=..., session_id=...)` で同じ session に投げる → 終わったら `mcp__secondopinion__end_session(session_id=...)`
4. self-contained な test 指示書を `task` (or `question`) に書く:
   - 期待される操作 (どの MCP tool をどう呼ぶか — 例: `mcp__gaottt__recall(query="...", top_k=3, force_refresh=true)`)
   - 観察項目 (substring 検出、top1 / top5 / metadata 等の何を見るか)
   - 期待される正解 (LLM 判断のための参考)
   - 報告フォーマット (200-400 字 / test、表 + 集計)
   - 「**生出力貼らない、substring 検出のみ報告**」を明示 (Claude Code 側 context 保護)
5. 返却 `text` を summary として受け取り、必要なら追加分析を Claude Code 側で実行 → `end_session` で resource 回収

例 (Phase O 5-stage acceptance、2026-05-15 実施):
```python
# Step 0: provider 確認
mcp__secondopinion__list_providers()
# → [{"name": "glm", "provider_id": "zai-coding-plan", "model_id": "glm-5.1", "default": true}]

# Step 1: 7 test を 1 turn で完走
mcp__secondopinion__delegate_task(
    task="""あなたは GaOTTT MCP server の acceptance test エージェントです。
    以下の 7 test を実行、substring 検出で報告 (生出力は貼らない):

    Test 1 — Stage 1+2:
    mcp__gaottt__recall(query="...", top_k=3, force_refresh=true)
    期待: "breakdown:" 行 + "## 訓練差分" + "wave_reached="
    ...(7 test 続く)

    # 報告: 各 test 1-2 文、最後に総評""",
    provider="glm",
)
# → {"session_id": "ses_...", "text": "全 7 test 完了..."}

# Step 2: session 解放
mcp__secondopinion__end_session(session_id="ses_...")
```

不可触の原則:
- Claude Code 自身が MCP `recall(top_k>5, tag_filter=...)` で 100KB 超の output を取得しない (前 session で実害発生)
- Claude Code の MCP session で `force_refresh=true` を多数連打しない (cache 状態を汚す)
- Claude Code が本番 DB に頻繁 `remember`/`relate` するのは可だが、acceptance test 本体は secondopinion-MCP 経由のサブエージェントに委ねる
- 終わったら必ず `end_session` — secondopinion-MCP 内の opencode subprocess を leak させない

サブエージェント環境の制約と回避策 (2026-05-14〜15 acceptance で判明):
- **/tmp 書き込みは external_directory permission で拒否される**。一時ファイルは project root の `./.perf-acceptance/` 等 (gitignore 推奨) に置く universal pattern を使う
- **scripts/diag_recall.py 等の本番 embedder (RURI) を使うスクリプトは、StubEmbedder で populate した DB に対しては結果が空になる** — embedder mismatch (RURI クエリベクトル vs random FAISS) が原因。サブエージェント経由で diag_recall を試したい場合は **本番 DB read-only か、別途 RURI で ingest した tmp DB** を当てる。Stub-populated DB で diag_recall を呼ぶと「JSON 構造は正しいが結果空」になり、機構の問題に見えて実は acceptance setup の問題。Path 型受け入れも同様に外部 caller で初めて顕在化する系の罠 (`_helpers.make_config` は str/Path 両対応に修正済み)
- **空 error が出たら一旦 `end_session` → 新 session を作り直す** — 2026-05-15 Phase O acceptance で `delegate_task` が空 error を返した事例があり、原因は不明だが新 session で同 prompt を投げると通った (session state の transient 問題と推測)

代替: secondopinion-MCP が使えない環境では、Claude Code 内の Agent ツール (subagent_type=general-purpose, model=sonnet) で代用可。同じく self-contained prompt + 短い summary 返却の流れで、Claude Code 本体の context を保護する。さらに代替として `Bash` で `opencode run "<prompt>"` を直接叩くことも可能だが、hang リスクがあるので secondopinion-MCP が優先。

## よくある罠

- **テスト fixture でランダム埋め込みを使うと flaky** — `tests/integration/test_engine_archive_ttl.py:StubEmbedder` のようにトークンベースの決定論的埋め込みを使う。`tests/perf/` は **real RURI v3 310m を session-shared singleton で使う** (`_helpers.get_shared_embedder()`)、production-grade な数値を測りたいので Stub じゃ不十分という設計判断 (2026-05-14 めいさん指摘)
- **`tests/perf/` は CI で自動実行しない** — 仮説→実装→検証ループの **検証 step** で手動実行する measurement tool。GitHub Actions に登録すると "merge したら通った" になりシグナルが薄まる。新機能を実装したら開発者が deliberately 走らせる
- **「速い」と「正しい」のトレードオフ** — 当初 tests/perf/ は StubEmbedder で 7 秒 fast を取ったが、これは "Performance regression structure" であって "Performance evaluation" ではなかった (production p50 ~35ms vs Stub 2.2ms の桁違い)。性能テストを書くときは最初に「何の性能を測っているか」「自動化することが価値か」を自問する
- **新しい source / edge_type 追加時は `KNOWN_EDGE_TYPES` (`core/types.py`) に追加** — そうしないと relate のドキュメント整合性が壊れる
- **新スキーマ列は必ず `DEFAULT` 付き** — 既存 DB が起動できなくなる
- **MCP ツールの新引数は必ずオプショナル** — 既存呼び出し元を壊さない
- **★ MCP と REST は同じターン/コミットで更新** — 新 service 関数 = 新 MCP tool + 新 REST endpoint + `REST-API-Reference.md` の 3 点セット。片方だけの PR は出さない（parity 鉄則、`/reset` を除く）。Merge 前に `scripts/rest_smoke.py` と `scripts/mcp_smoke.py` の **両方** を走らせて green 確認
- **タスク管理系（`commit`/`complete`/`abandon`）は `services.memory.save_memory` を使う** — 直接 `engine.index_documents()` を叩かない（Phase S 以降、旧 `_save_memory` は `save_memory` に改名）
- **`inherit_persona` は明示的に declared された value/intention/commitment のみ集める** — 普通の `remember` は対象外
- **`SKILL.md` を編集したら必ず `.claude/skills/gaottt/SKILL.md` にも `cp`** — 両方が同期されている必要
- **MCP formatter の出力文字列を変えない** — `tests/integration/test_mcp_tools.py` などが specific substring を assert している。新情報を出力に加えるときは追加行で（既存行の書式を変更しない）
- **MCP と REST でリクエスト shape を分けて良い** — MCP は LLM が書きやすい shape（例: `task_id` を body に含む）、REST は HTTP 流儀（path param canonical、body は残りだけ）。`core/types.py` に `*Request`（MCP）と `*Body`（REST）を並立させるのが既定パターン（Phase S の `CompleteRequest` / `CompleteBody` を参照）
- **大規模 DB の sparse class (agent / value / commitment) は `source_filter` + 必要なら `wave_k=1000`** — Phase H Stage 2 以降、source_filter は seed 段階で効く。デフォルト `wave_k_with_filter=500` で expected ~8 件、届かないクエリは `recall(query, source_filter=[...], wave_k=1000)` で明示。Phase H Stage 4 (virtual FAISS) で priming 効果が seed に乗り、自然文クエリの top1 score も大幅改善
- **新規 `remember` 直後の即時 recall は genesis kick で surface する** — Phase G Stage 1 で `mass=1.0` 初期値問題は解消。直後 score 0.7+ も期待可能。anchor 句頼みは不要
- **agent docs の displacement 方向は近傍 high-mass 方向に固定される** — Phase G priming は近傍重力 kick で動かすため、query と関係ない方向。embedding 距離が遠い query は依然届かないことあり（query-aware displacement は次 Phase 領域）
- **virtual FAISS の build は initial startup で 数十秒〜分** — `gaottt.virtual.faiss` ファイルが無い状態で起動すると 23k 件規模で自動 build。詳細 [Operations — Server Setup](docs/wiki/Operations-Server-Setup.md)
- **Phase M Mass Conservation の本番ロールアウトは「mass reset → 1-2 週観測 → θ 確定 (Stage 2)」の段階を踏む** — `scripts/reset_masses.py --apply` は destructive (戻せない)。実行前に必ず DB backup + 他 MCP/REST プロセス停止 (write-behind 上書き罠)。`mass_bh_theta=5.0` / `mass_bh_sigma=1.5` は **暫定 placeholder** で、観測無しで触らない。`reset_masses` は MCP に **非露出** (LLM 用途なし、REST `/admin/reset_masses` のみ)
- **`is_self_force_by_id` は `original_id` AND `cohort_id` のどちらか一方でも一致すれば self** — source 分岐ゼロ。新ノードを mass-conservation 配下に置く時は metadata に **正しい original_id** を入れる責任が呼び出し側にある。`engine.index_documents` の fallback: `metadata.original_id` 未設定なら `metadata.file_path` を使い、それも無ければ `node_id` 自身 (= 自己一致でしか self にならない、影響 0)。`scripts/load_files.py` 経由の ingest は loader が自動付与

## 主要参照ポインタ

- 全体ナビ: [`docs/wiki/Home.md`](docs/wiki/Home.md)
- ロードマップ: [`docs/wiki/Plans-Roadmap.md`](docs/wiki/Plans-Roadmap.md)
- アーキテクチャ全体: [`docs/wiki/Architecture-Overview.md`](docs/wiki/Architecture-Overview.md)
- 全 28 MCP ツール: [`docs/wiki/MCP-Reference-Index.md`](docs/wiki/MCP-Reference-Index.md)
- ハイパラチューニング: [`docs/wiki/Operations-Tuning.md`](docs/wiki/Operations-Tuning.md)
- トラブルシュート: [`docs/wiki/Operations-Troubleshooting.md`](docs/wiki/Operations-Troubleshooting.md)
- 哲学（五層論）: [`docs/wiki/Reflections-Five-Layer-Philosophy.md`](docs/wiki/Reflections-Five-Layer-Philosophy.md)

<!-- MANUAL ADDITIONS START -->
<!-- MANUAL ADDITIONS END -->
