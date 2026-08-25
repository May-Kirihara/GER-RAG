# Operations — Troubleshooting

既知の問題と対処。

## 起動時に異常が出る — まずログを見る

GaOTTT は `engine.startup()` の最後で **Stage 1 セルフ診断** (`gaottt/diagnostics/startup.py`) を自動実行します。
3 つの代表的な事故 (FAISS 空 / 0 bytes / index ↔ SQLite ntotal 乖離) は起動直後の
`[diagnostics:tier_a_*]` / `[diagnostics:tier_b_*]` ログで即検知されます。

```
[diagnostics:tier_a_raw_zero_bytes] raw FAISS file at /.../gaottt.faiss is 0 bytes — corrupted save, triggering rebuild
[diagnostics:tier_a_raw_rebuilt] raw FAISS rebuilt: size=24050
[diagnostics:tier_b_faiss_size_drift] faiss.size=15 vs SQLite active=24050 (99.9% drift > 5%) — run compact(rebuild_faiss=True)
```

- **`tier_a_raw_zero_bytes` ERROR + `tier_a_raw_rebuilt` INFO** が連続で出ればその場で復旧済み (自動 lazy rebuild)。
- **`tier_b_*_size_drift` WARN** が出たら `compact(rebuild_faiss=True)` を一度走らせる。
- **`tier_a_tmp_residual_cleaned` INFO** は `.tmp` 残骸 (atomic save の中断痕跡) を掃除した記録。頻発するなら kill タイミングや disk full を疑う。

検知範囲は Tier A (FAISS integrity) + Tier B (FAISS↔SQLite + BM25 size 一致)。
Stage 2 候補 (WAL audit / physics dynamics drift / JSON endpoint) と Stage 3 (migration ledger / config sanity / CLI) は別 commitment。

## クエリスコアが初回だけ極端に低い

正常動作。初回クエリ時、`last_access` がインデックス時刻のため `decay = exp(-δ × 経過時間)` が非常に小さくなる。2 回目以降は decay ≈ 1.0。

**Phase T Stage 2 (2026-08-25、default ON) で契約が変わった**: semantic decay は秒 rate `delta` から **half-life + floor** (`floor + (1−floor)·0.5^(age/halflife)`、既定 7 日 + floor 0.35) に置き換えられ、経年記憶でも semantic 項が `semantic_floor` を下回らなくなった (旧契約では 10 分で factor≈0.0025 に沈み、mass/wave が順位を完全支配)。age=0 で factor=1.0 は legacy と同一。旧挙動に戻すのは `GAOTTT_SEMANTIC_HALFLIFE_ENABLED=false` (下記 warning 参照)。

## startup ログに semantic decay の deprecation warning が出る

**症状**: engine 起動時に以下の WARNING が出る:

```
semantic_halflife_enabled=False: using legacy compute_decay with config.delta=0.01 — delta is a per-SECOND rate (deprecated contract) that zeroes the semantic score term within minutes. See docs/wiki/Plans-Phase-T-Semantic-Requalification.md §3.
```

**原因**: Phase T Stage 2 で legacy `delta` (秒 rate) 契約が deprecated になり、`semantic_halflife_enabled=False` (env `GAOTTT_SEMANTIC_HALFLIFE_ENABLED=false`) で明示的に legacy 経路を選んだときに 1 回だけ出る。エラーではなく「意図的な rollback であること」の確認ログ。

**対処**: 意図的なら無視して OK。意図していないなら flag を外す (default `True` に戻す) — half-life + floor 契約のままだと経年記憶の semantic 項が消滅しない ([Tuning](Operations-Tuning.md) §Semantic Requalification)。

## 本番 acceptance test で新機能が一切検出されない (proxy mode backend が古いコードを保持)

**症状**: 直前に commit/push した Phase X 機能 (新しい trailer / response field / mode arg 等) が、`mcp__gaottt__recall` / `mcp__gaottt__explore` 経由の本番 acceptance で 1 件も検出されない。`scripts/rest_smoke.py` / `scripts/mcp_smoke.py` (各 smoke は毎回新 engine を立てる) では green、テスト suite (`pytest tests/`) も green。

**原因 (2026-05-15 発見)**: proxy mode の HTTP backend は **常駐 process** (`gaottt.server.mcp_server --transport streamable-http --port 7878`) で、Python module を in-memory に保持する。`git push` だけでは backend は **更新されない**。dead-man-switch は「全 shim ping が 5 分止まる」が条件だが、新しい opencode/Claude Code agent が来続ける限り発動しない → **古いコードが何時間も memory に居座る**。Phase O acceptance では PID 2788684 が commit 4 時間前から動いており、7 test 全て pre-Phase-O 出力を返した。

**対処**:
```bash
# 1. backend 起動時刻を確認
ps -ef | grep "gaottt.server.mcp_server.*streamable-http" | grep -v grep
# 出力例: misaki_+ 2788684 ... 19:25 ... gaottt.server.mcp_server --transport streamable-http --host 127.0.0.1 --port 7878

# 2. 起動時刻が直近 commit より古ければ kill
kill <pid>

# 3. 数秒待って消えたことを確認
sleep 3 && ps -ef | grep "gaottt.server.mcp_server.*streamable-http" | grep -v grep || echo "backend stopped"

# 4. 次に MCP shim (opencode/Claude Code) が接続したタイミングで新 backend が auto-respawn、新コードが乗る
```

**予防**: code 変更後の本番 acceptance ルーチンの **Step 0** として backend 起動時刻チェックを入れる。同じ pattern (process 内 state が外部 source-of-truth と乖離) は cache write-behind の「逆方向上書き罠」と同型 — CLAUDE.md の「bulk 書き換え時は他プロセス停止」ルールは **code update にも適用** する。memory: [[feedback-backend-kill-on-code-deploy]]。

## ambient recall フックが何も注入しない

**症状**: ambient recall フック / opencode プラグイン ([Guides — Ambient Recall](Guides-Ambient-Recall.md)) を登録したのに、プロンプトを送っても `<gaottt-ambient-recall>` ブロックが文脈に現れない。

**フックは fail-safe 設計** — backend ダウン・タイムアウト・プロトコルエラーいずれも無出力 exit 0 でユーザーのプロンプトを絶対ブロックしない。「何も起きない」が正常な失敗モードなので、原因は順に切り分ける:

1. **backend が `ambient_recall` ツールを知らない旧コード** (最頻) — proxy mode の 7878 backend が `ambient_recall` ツール追加前のコードを保持していると、未知ツールとして弾かれフックは無出力になる。上の「本番 acceptance test で新機能が一切検出されない」と同型。`ps -ef | grep streamable-http` で起動時刻を確認し、`ambient_recall` 追加 commit より古ければ `kill <pid>` → 次の MCP 接続で auto-respawn。
2. **手動で切り分ける** — フックスクリプトに直接プロンプトを流す:
   ```bash
   echo '{"prompt":"<関連しそうな長めのプロンプト>"}' | .venv/bin/python scripts/hooks/ambient_recall.py
   ```
   無出力なら下の 3〜5 を確認。
3. **relevance gate に弾かれている** — 主たる gate は語単位 (Sudachi) BM25 の「強一致」gate。top BM25 が `config.ambient_bm25_min_score` (既定 32、コーパス規模・クエリ長依存) 以上なら即 accept。**Phase T Stage 5 (default ON) では BM25 reject でも即空返しにならない** — passive recall が走り、`max(virtual_score) ≥ ambient_min_score` (0.70) OR `max(raw_cos) ≥ ambient_semantic_raw_min` (0.60) で accept、**両軸とも下回ったときだけ** 空返し (off-topic 抑制は維持)。`bm25-sudachi` extra 未導入 / `ambient_gate_use_bm25=False` だと `virtual_score` gate に自動フォールバック。しきい値の再校正は [Operations — Tuning](Operations-Tuning.md) の `ambient_*` 節（Guides の「しきい値の校正」も参照）。
4. **プロンプトが短すぎる** — `GAOTTT_AMBIENT_MIN_CHARS` (既定 12) 文字未満のプロンプトはスキップ。
5. **フックが無効化されている / 未登録** — `GAOTTT_AMBIENT_RECALL=0` が環境にある、または登録自体が無い。Claude Code は `.claude/settings.json` の `UserPromptSubmit` エントリ（`.claude/` は gitignore 対象なので clone では引き継がれない）、opencode は `~/.config/opencode/plugin/gaottt-ambient-recall.ts` の有無を確認。opencode プラグインは設計上 fail-safe で無言なので、`GAOTTT_AMBIENT_DEBUG=<path>` を設定すると各ステップの診断ログ（フック発火 / spawn の exit・stdout 長 / 注入有無）が出る。

**Phase T Stage 5 — `empty_reason` による空返しの段階切り分け**: gate が空返しを返した理由は `AmbientRecallResponse.gate_diagnostics.empty_reason` (離散一意) で機械的に判別できる。REST は response JSON、MCP は `expose_breakdown=True` のとき `gate: <reason|passed> (...)` 診断行で読む:

| `empty_reason` | 意味 | 次に見るもの |
|---|---|---|
| `bm25_veto` | legacy flag OFF (`ambient_gate_or_semantic=False`) で BM25 reject 即空 | rollback 意図なら正常。意図しなければ flag を戻す |
| `bm25_and_semantic_below_threshold` | 候補は存在した (`candidates_generated > 0`) が BM25 も semantic 両軸とも閾値未満 | `gate_diagnostics` の `bm25_top_score` / `semantic_max_virtual` / `semantic_max_raw` と各閾値 (`ambient_bm25_min_score` 32.0 / `ambient_min_score` 0.70 / `ambient_semantic_raw_min` 0.60) の比較 — 言い換え系 query で BM25 が 5-9 に沈むのは既知 (gate は語彙一致のみ)。semantic が 0.6 を切るなら本当に無関係の可能性大 |
| `no_candidates` | passive recall pool が 0 件 | corpus 側の問題 (DB 空 / FAISS 不整合)。下の「FAISS と SQLite のカウントが合わない」へ |
| `all_tag_excluded` | 候補はあったが `exclude_tags` で全滅 | `GAOTTT_AMBIENT_EXCLUDE_TAGS` の substring が広すぎないか |
| `all_dump_filtered` | 候補はあったが dump-shape gate (`ambient_dump_symbol_ratio`) で全滅 | corpus がコード / state-dump 中心でないか、`gate_diagnostics.after_dump_filter` と `after_tag_exclusion` の差分で |

なお passive recall は `last_access` を更新しないため、ambient フックでしか surface されない記憶は decay し続ける（意図的 — Guides ページ「既知の性質」）。relevance gate は decay 非依存（BM25 語彙一致、フォールバックの `virtual_score` も同様）なので ambient 注入自体は古い記憶でも効き続ける。

## ambient_recall が想定外の memo を surface する (composed query 不透明問題)

**症状**: 短いプロンプト (例: 「続けて」「ありがとう」「次のステップに進みましょう」) を送ったときに ambient block が前 turn と全然違う / 想定外の memo を surface し、なぜそれが出たのかが分からない。

**原因**: Refinement Stage 4 (`GAOTTT_AMBIENT_HISTORY_TURNS=2` 既定) で hook が **直前 N turn の user prompt を concatenate** して server に投げている (例: 「続けて」だけでなく「Phase L hybrid retrieval について\n続けて」が query になる)。BM25 gate と embedding search はこの concatenated query で動くので、結果が「現在の prompt 単体だと予測できない」ものになる。

**対処 — Lateral Association Stage 4 の debug knob**: 環境変数 `GAOTTT_AMBIENT_SHOW_COMPOSED_QUERY=1` を設定すると、ambient block 末尾に 1 行追加される:

```
<gaottt-ambient-recall>
... (slots) ...
<!-- ambient: composed query = "前 turn の prompt\n現 turn の prompt" -->
</gaottt-ambient-recall>
```

これで「**ambient 結果が変なのは query 自体が変なのか / recall が変なのか**」を即座に分離できる。composed query が想定通りで recall 結果が変 → server 側 (corpus / displacement / threshold) の問題。composed query 自体が想定外 → `GAOTTT_AMBIENT_HISTORY_TURNS` を 0 (現プロンプトのみ) か 1 (直前 1 turn のみ) に下げる、または対象 turn の prompt 自体を見直す。

**debug-only**: `composed == prompt` (連結が起きてない) のときは line 自体が省略される (debug 価値ゼロ)。token budget は composed query 長さに比例 (典型 50-200 字)、本番 hook では off 推奨。詳細: [Plans — Ambient Recall Lateral Association](Plans-Ambient-Recall-Lateral-Association.md) Stage 4。

## 別プロセスから新規 `remember` が見えない（FAISS stale）

**症状**: 別プロセスの MCP サーバー / opencode エージェント等で `remember` した直後、自プロセスの `recall` でその memory が一切 surface しない。`reflect(aspect="summary")` の `Total memories` は増えていることがある（SQLite は WAL で共有されるが FAISS index はプロセス毎独立）。

**原因（歴史的バグ、2026-05-10 修正済み）**: かつて `engine.shutdown()` でしか FAISS が disk に save されなかった。MCP サーバー等の長期常駐プロセスは shutdown しないため、新規 vector が永久に in-memory のまま、他プロセスからは invisible だった。

**修正**: `faiss_save_interval_seconds`（既定 5s）周期の write-behind loop を導入（[Architecture — Concurrency](Architecture-Concurrency.md) 参照）。

**virtual FAISS の同等問題（2026-05-13 修正済み）**: 上記は raw FAISS のみの修正で、virtual FAISS は依然 `compact(rebuild_faiss=True)` または起動時 (disk file 欠落時) のみ rebuild されていた。Phase I/J query attraction で蓄積した displacement が次の compact まで他プロセスの seed pool に反映されない問題があり、`virtual_faiss_save_interval_seconds`（既定 60s）周期の write-behind loop を追加。`cache.virtual_faiss_dirty` が立つと次 tick で full rebuild + disk save。長期常駐 MCP では非ゼロ必須。

**それでも見えない場合の対処**:
- 自プロセスを再起動（startup() で disk から最新 FAISS を load）
- 修正前の DB で長期間積もった「FAISS に無く SQLite/cache にのみ存在する」ノードがある場合、`engine.compact(rebuild_faiss=True)` で全 active から再構築すれば解消（diagnostics: `len(faiss._id_map - cache.node_cache.keys())` と逆向きを比較）
- `faiss_save_interval_seconds=0` に設定してしまっていないか確認（disable 設定）

## `MCP error 32600: Session terminated`（並列 recall でセッションが死ぬ）

**症状**: 1 つのエージェントターンで `recall` 等を **2 件以上並列**に呼んだ直後から、その MCP クライアントの **全 GaOTTT 呼び出し** が `MCP error 32600: Session terminated` を返し続ける。`/mcp` で gaottt を reconnect すると復旧。`ambient_recall` フック（毎ターン別接続で呼ばれる）は正常応答するので backend 自体は生きている。

**原因**: proxy mode では各エージェントの shim が単一の upstream `ClientSession`（streamable-http）で backend に繋がる。lowlevel `Server` は受信リクエストを並行ディスパッチするため、2 件の `call_tool` が同じ session へ同時 POST し、streamable-http の 1 セッションが同時 in-flight で壊れる。一度壊れると以後その session の全呼び出しが落ち、自動復旧しなかった。調査: [`handover-2026-06-01-concurrent-recall-session-termination.md`](https://github.com/May-Kirihara/GaOTTT/blob/main/docs/maintainers/handover-2026-06-01-concurrent-recall-session-termination.md)。

**修正（2026-06-01）**:
- **直列化**: proxy が全 upstream 呼び出し + ping を `asyncio.Lock` で 1 in-flight に直列化（`proxy_serialize_requests_enabled`、既定 ON）。並列呼びでも session が壊れない。
- **自己修復**: session 終了系の例外で upstream session を rebuild し 1 回 retry（`proxy_auto_reconnect_enabled`、既定 ON）。backend 死 / idle watchdog / cold-start でも自動再接続する（`/mcp` 手動 reconnect が不要に）。

**それでも出る場合**:
- 修正前の backend が動いている可能性 → backend を kill して新コードで respawn（[code deploy 時の backend 再起動](Operations-Server-Setup.md)）。
- 緊急 rollback は `GAOTTT_PROXY_SERIALIZE_REQUESTS_ENABLED=0`（ただし legacy の壊れる挙動に戻る）。
- 暫定の運用回避は従来どおり「GaOTTT 呼び出しを並列にしない（逐次化）」。

## メモリ使用量が大きい

- embedding モデル: ~1.5GB（GPU VRAM）
- FAISS インデックス: 768次元 × 4byte × ドキュメント数（100K 件で ~300MB）
- ノードキャッシュ: ドキュメント数に比例

## SQLite ロックエラー (`database is locked`)

複数 MCP サーバー（複数エージェント並行運用）で発生する。`PRAGMA busy_timeout = 30000` を設定済（最大 30 秒待機）が、それでも頻発するなら:

- write 頻度が高い → `flush_interval_seconds` を伸ばす
- ロック待ちが長い → MCP サーバープロセスを必要数だけに減らす

→ 詳細: [Architecture — Concurrency](Architecture-Concurrency.md)

## `recall` で `list index out of range`

`faiss_index._id_map` と FAISS の `ntotal` がズレた場合に発生していた問題。修正済（境界チェック追加）。

復元方法: `engine.compact(rebuild_faiss=True)` で FAISS を active ノードから再構築。

## archived ノードが大量に溜まった

`forget(hard=False)` の蓄積、または TTL hypothesis の自動 expire が積み重なると、FAISS に「使われないベクトル」が残り続ける。

**対処**: `compact(rebuild_faiss=True)` を週次〜月次で実行。

## 重力衝突合体 (merge) が暴走する

`compact(auto_merge=True, merge_threshold=...)` の閾値が低すぎると、似て非なる記憶を融合してしまう。

**対処**:
- `merge_threshold` を 0.95 以上に保つ
- `auto_merge` は default OFF。明示的に有効化したときのみ動く
- 心配な場合は手動で `reflect(aspect="duplicates")` → 中身を確認 → `merge(node_ids=[...])`

## 確信度が古いまま下がっていく（F7）

`certainty_half_life_seconds`（既定 30 日）を超えると certainty boost が指数減衰。`revalidate(node_id)` を呼ぶと last_verified_at が更新され、boost が回復。

## prefetch のヒット率が低い（F6）

`prefetch_status` で `hit_rate` が低い場合:
- クエリ文字列が完全一致しない → LLM 側で「prefetch と recall に渡す query を完全一致させる」プロトコル徹底
- TTL が短すぎる → `prefetch_ttl_seconds` を伸ばす
- destructive op が頻繁 → 設計上 invalidate される。頻発するなら戦略再考

## タスクが知らないうちに消える（Phase D）

`source="task"` は既定 30 日、`source="commitment"` は既定 14 日で auto-expire。

**対処**:
- `revalidate(node_id)` で意識的にコミットメントを生かし続ける
- `reflect(aspect="commitments")` を週次儀式に
- TTL を伸ばす（`config.py` の `default_*_ttl_seconds`）

## ambient_recall の「いま誰として」が query 横断で同じ persona に固定される

**症状**: `<gaottt-ambient-recall>` ブロックの「いま誰として」slot が、別の話題で連続して質問しても **毎回同じ persona** (value/intention) を表示する。例えば「embedder の話」「BM25 gate の話」「全く違うプロジェクトの話」のどれを聞いても `intention: harakiriworks-art-website ...` が出続ける。前後 query で文脈が変わったのに人格行だけ動かない。

**原因 (Heavy Persona Dominance、Plans-Ambient-Recall-Refinement.md follow-up (b))**: ranking 式は `score = (mass ** w) × cos(query, persona_vec)`。旧既定 `ambient_persona_mass_weight = 1.0` では、production で **1 つだけ mass が突出した persona** (例: `mass=2.82` vs 他 `mass=1.0` 付近) があると、mass 項が dominant になって cos 軸の差では決着しない (mass 比 10× × cos 比 1.5× → 常に heavy 側が勝つ)。**2026-07-02 に default を `w=0.3` + `min_relevance=0.65` へ同時昇格**し標準対策済み (env opt-in の w=0.3 単独では dense 日本語 embeddings の cos ≈ 0.52 が旧 0.5 floor を slip して症状が継続したため、両 knob の同時 default 化が必要だった)。本節は **default 適用後も残る residual case / さらなる tuning** 向け。Refinement Stage 1 の `mass × cos` re-rank ロジックは「正しく」動いており、production の質量分布が想定外に偏っているだけ。

**確認** (`expose_breakdown=true` で診断):
```python
mcp__gaottt__ambient_recall(query="...", direct_k=2, expose_breakdown=true)
```
複数の異なる query に対して persona slot の breakdown を見る。`mass=2.5` 以上の同じ persona が毎回 picked されているなら Heavy Persona Dominance 確定。

**対処** (default `w=0.3` / `min_relevance=0.65` 適用後も residual な場合、`config.py` の値変更 → backend 再起動が必要):
1. **measurement first** — `tests/perf/test_tier3_ambient_quality.py` で **before baseline** を取る (現状の persona pick 分布を記録)。事後 baseline の形でも、default 昇格の効果を数値で分離するのに使う
2. 現 default `w=0.3` からさらに `ambient_persona_mass_weight=0.5` に *上げて* `sqrt(mass) × cos` (穏やかすぎる場合は戻す方向) 、または `0.0` (cos のみ) に *下げて* 強抑制 → 再起動 → 同じ test で after baseline 比較。critical exponent の予測式: `w* = log(cos_ratio) / log(mass_ratio)` (例: mass_ratio=2.82, 目当ての cos_ratio=1.3 → `w* ≈ 0.25`)
3. 過剰に下がって cos noise で別の不適切 persona が surface するなら `ambient_persona_min_relevance` を現 default `0.65` からさらに上げてガード (逆に on-topic persona まで落ちは上げすぎの兆候)

**rollback**: `ambient_persona_mass_weight=1.0` (旧既定値、`w=1.0` で Stage 1 bit-identical — 累乗を skip する分岐があり数値も完全一致)。backend 再起動が必要 ([feedback: backend kill on code deploy](#)、`ps -ef | grep streamable-http` で起動時刻 → `kill <pid>` → 次の MCP 接続で auto-respawn)。

**関連**:
- 詳細設計: [Plans — Ambient Recall Refinement](Plans-Ambient-Recall-Refinement.md) 「follow-up (b)」節
- knob 詳細: [Operations — Tuning](Operations-Tuning.md) `ambient_persona_mass_weight` 行
- なぜ Stage 1 の `mass × cos` を「壊れていない」と言えるかの literal な test fixture: `tests/integration/test_engine_ambient_recall.py::test_ambient_persona_mass_weight_*` (3 ケース)

## inherit_persona の出力が薄い

新セッションで `inherit_persona()` を呼んだのに「No values declared」しか返ってこない場合:
- value/intention/commitment を実際に declare していない
- agent ソースの記憶が混ざっている → `inherit_persona` は明示的に source 指定が必要
- 数が多すぎて切り詰められている → `reflect(aspect="values", limit=20)` で全件確認可能

## 異常終了後の起動

フラッシュされていない dirty 状態は消失するが、ドキュメントと embedding は保全される。動的状態（mass, temperature）はクエリを繰り返すことで自然に再構築される。

→ 関連: [Architecture — Concurrency](Architecture-Concurrency.md), [Compact & Backup](Operations-Compact-And-Backup.md)

## `tag_filter` / `persona_context` で注入した node が recall 結果に出ない

**症状**: `recall(query, tag_filter=["foo"])` を呼んだのに、タグ "foo" を持つ node が結果に表示されない。`reflect` で確認すると node 自体は存在する。

**原因（2026-05-12 修正済み）**: Phase J Stage 2 の `injected_ids` が seed pool の `initial_k` 上限（既定 ~3 程度）を超えると、溢れた node が wave propagation の `reached` dict に入らず、Step 3 の `original_emb = faiss_index.get_vectors(reached_ids)` で `None` になり results から除外されていた。FAISS にベクトルが存在していても surface しないという非直感的な挙動。

**修正内容** (`gaottt/core/gravity.py`): wave 終了後に `injected_ids` の欠落 node を `reached[nid] = 1.0`（direct seed と同等の force）で強制追加するパスを追加。これにより injected node 数が `initial_k` を超えても全件が scoring に参加する。

**修正前の回避策**（旧バージョン対応時）:
- `top_k` を小さくして `injected_ids` が `initial_k` を超えないようにする
- 注入対象を 1 件に絞って `persona_context=[specific_id]` を使う

## 英語クエリで日本語の記憶がヒットしない（埋め込みが cross-lingual でない）

**症状**: 英語で `recall(query="...")` を呼ぶと、日本語で書かれた記憶（ツイート / note / 日本語ファイル）がほとんど surface しない。`cos` スコアは 0.7〜0.9 と高いままなので一見うまく動いているように見えるが、内容は無関係。逆方向（日本語クエリ → 英語記憶）でも同じ。

**原因**: 埋め込みモデル RURI v3 は日本語特化モデルで、**cross-lingual ではない**。英語クエリのベクトルと日本語文書のベクトルが共有意味空間で揃わないため、検索は実質「クエリと同じ言語で書かれた記憶」しか引けない。RURI は EN→EN / JA→JA のモノリンガル検索はこなすが、EN↔JA を橋渡ししない。BM25 ハイブリッド層（char 3-gram）も言語をまたげない（`競艇` と `boat race` は 3-gram をひとつも共有しない）。

**実測（2026-05-21、本番 DB）**: 同一概念を英日ペアで `recall`（`passive=true`）したところ、検索の勝敗は「クエリの言語」ではなく「ターゲット文書の言語」で決まった。日本語の正解ツイートは日本語クエリが #1 で一発ヒット（英語クエリは考古学の参考文献リストを誤爆）、英語で書かれた開発ログは英語クエリが最高スコア（`cos=0.885`）でヒット。`cos` は当たり外れに関わらず 0.74〜0.89 の狭い帯に入るため、スコアからは判別できない。

**対処**:
- 探したい記憶の言語に合わせてクエリを書く（日本語中心の DB なら日本語で訊く）
- 言語ギャップを越えたいときは `tag_filter` / `source_filter` でターゲットを明示注入する（語彙・言語が違っても seed pool に強制投入される）
- 英語コーパスを本格運用する / 英語で横断検索したい場合は multilingual モデル（multilingual-e5-large, BGE-M3 等）への移行が必要。移行は FAISS index 全再構築（`compact(rebuild_faiss=True)`）を伴い、displacement 蓄積もリセットされる破壊的操作。異なる embedder のベクトルを同一 index に混在させると比較不能なので「日本語 RURI のまま」か「多言語移行」かは二者択一。

## 問題5.5: FAISS が2件などに激減（逆方向上書き罠）

**症状**: `recall` がほぼ空、`scripts/visualize_3d.py` が「2 stars」で UMAP が
`zero-size array to reduction operation maximum` で落ちる。`gaottt.faiss` が
数KB（正常時は数十〜百MB）。DB (`gaottt.db`) は通常サイズのまま。

**原因**: 「逆方向上書き罠」。stdio で多数の MCP プロセスが並走しているとき、
ほぼ空の in-memory FAISS を持つプロセスが write-behind save ループ（既定5秒）で
ディスク上の**正常なインデックスを空のもので上書き**し続ける。DB は無傷なので
完全復旧できる（RURI は決定論的、`documents.content` から再エンベッドすれば raw
ベクトルはビット単位で元通り。mass/displacement/velocity は SQLite に保持）。

> **注意**: 本番データは XDG パス `~/.local/share/gaottt/` に置かれる
> （リポジトリ内の `./data` ではない）。解決先の確認は
> `scripts/rebuild_faiss_from_db.py --check`。

**診断（read-only）**:
```bash
.venv/bin/python scripts/rebuild_faiss_from_db.py --check
# raw FAISS vectors が SQLite documents より桁違いに少なければ desync
```

**復旧**（順序が重要 — プロセスを止めてから rebuild）:
```bash
# 1. バックアップ
cp ~/.local/share/gaottt/gaottt.faiss ~/.local/share/gaottt/gaottt.faiss.broken-$(date +%Y%m%d-%H%M%S)
cp ~/.local/share/gaottt/gaottt.db    ~/.local/share/gaottt/gaottt.db.before-rebuild-$(date +%Y%m%d-%H%M%S)
# 2. 全 gaottt プロセス停止（これをやらないと逆方向上書きが続く）
ps -ef | grep 'gaottt.server.mcp_server' | grep -v grep
pkill -f 'gaottt.server.mcp_server'   # :7878 backend も含む
# 3. DB から再構築（RURI で再エンベッド、規模により数分〜十数分）
.venv/bin/python scripts/rebuild_faiss_from_db.py --apply
# 4. 検証
.venv/bin/python scripts/rebuild_faiss_from_db.py --check
.venv/bin/python scripts/verify_faiss_recovery.py
```

**再発防止（自動）**: 2026-05-31 に **逆方向上書きガード** を追加。`faiss.size`
が SQLite active ノード数の `faiss_persist_min_ratio`（既定 0.5）未満で
`active >= faiss_persist_floor`（既定 100）のとき、全 FAISS 永続経路（save
ループ + shutdown 最終 save）が**書き込みを拒否**する。起動時診断（Tier B）は
severe undersize を WARN→ERROR に昇格し、そのプロセスの永続を恒久 block + 復旧
手順をログ出力する（rebuild storm 回避のため自動 rebuild はしない）。正当な大量
`forget`/`compact` は cache active 数も同時に減るので誤発動しない。
`GAOTTT_FAISS_PERSIST_GUARD_ENABLED=0` で無効化可。詳細パラメータは
[Operations — Tuning](Operations-Tuning.md)。

**根本対策**: stdio での複数 agent 同時起動が構造的原因。ガードは*上書き*を
止めるが、複数 stdio engine の並走自体は止めない。proxy mode への統一が運用上の
follow-up（[Operations — Server Setup](Operations-Server-Setup.md)）。

## FAISS と SQLite のカウントが合わない

**症状**: `recall` で存在するはずの node が surface しない、または `compact(rebuild_faiss=True)` を実行しても FAISS count が SQLite count より少ないまま。

**診断**: `scripts/verify_faiss_recovery.py` を実行:
```bash
.venv/bin/python scripts/verify_faiss_recovery.py [node_id_prefix ...]
```
`Gap > 0` ならば SQLite にはあるが FAISS にない node が存在する。特定 ID を引数に渡すと IN FAISS / MISSING を確認できる。

**原因 A — write-behind フラッシュ前のプロセス終了**: MCP サーバーが `faiss_save_interval_seconds`（既定 5s）周期のフラッシュ前に異常終了した場合、その session の `remember` が SQLite には保存されているが FAISS disk には反映されない。次回起動時に FAISS を disk から load するため欠落が続く。

**原因 B — `_rebuild_faiss_index` の旧バグ（2026-05-12 修正済み）**: `compact(rebuild_faiss=True)` が FAISS に既存のベクトルのみ再構築し、SQLite/cache にあるが FAISS に載っていない node を再埋め込みしなかった。

**修正内容** (`gaottt/core/engine.py`): `_rebuild_faiss_index` が `vecs = faiss_index.get_vectors(active_ids)` で返らなかった `missing_ids` を `store.get_document()` で content 取得 → `embedder.encode_documents()` で再埋め込み → FAISS 追加するパスを追加。これにより `compact(rebuild_faiss=True)` が SQLite 全 active node を確実に FAISS に収録する。

**対処手順**:
1. `scripts/verify_faiss_recovery.py` でギャップを確認
2. MCP サーバーを再起動（修正済みコードを読み込む）
3. `compact(rebuild_faiss=True)` を実行
4. 再度 `verify_faiss_recovery.py` で `Gap: 0` を確認

**起動時 Tier B 診断 `tier_b_faiss_snapshot_mismatch` (ERROR)**: SQLite が restore / rollback されて FAISS file が新しい corpus snapshot のまま残っていると、FAISS が返す ID が SQLite に存在せず recall が空リストに劣化する (件数だけでは同規模 snapshot を弁別できないため、ID set の overlap 比で判定)。同診断は severe undersize (`tier_b_faiss_severe_undersize`) と同じく persist guard を latch し、この process が broken index を disk に書き戻すのを恒久的に block する。**対処**: 全 gaottt process 停止 → `scripts/rebuild_faiss_from_db.py --apply` (DB から再 embed、決定論でロスレス) → `--check` で検証 → 再起動。なお本診断は 2026-08-25 の semantic search 障害復旧 session に由来する **既存の未コミット変更** の一部として追加されたもの (ID-set 比較の強化。本項は事実の記載のみ)。

## 特定の memory が無関係なクエリでも上位に出続ける（重力井戸）

**症状**: Phase I Stage 2/3 の query attraction や Phase J の累積 recall によって特定ノードの `displacement` が蓄積し、embedding 距離の遠いクエリでも wave の引力で浮上し続ける（重力井戸状態）。`recall` 結果の `displacement_norm` 値が 0.5 を超えている場合に疑う。

**診断**: `scripts/reset_displacements.py`（引数なし）で全ノードの displacement 統計を表示:
```bash
.venv/bin/python scripts/reset_displacements.py
# 出力例:
# displacement 統計 (全 23695 件)
#   min=0.0006  p50=0.0013  p90=0.3042  max=0.6005
#   |d| > 1.0: 0 件
```

p90 > 1.0 や特定 tag に集中した高 displacement が見られたら要対処。

**対処手順** (edges は保持、displacement のみリセット):
```bash
# 1. サーバーを停止
pkill -f gaottt.server.mcp_server
pkill -f gaottt.server.app

# 2. 対象を確認 (dry-run)
.venv/bin/python scripts/reset_displacements.py --tag <tag-name> --min-displacement 1.0
# または特定 ID: --ids <id-prefix>
# または全件: --all

# 3. 実際にリセット
.venv/bin/python scripts/reset_displacements.py --tag <tag-name> --min-displacement 1.0 --apply

# 4. priming で Hooke 均衡に再収束 (省略可、効果を加速したい場合)
.venv/bin/python scripts/prime_gravity.py --apply

# 5. virtual FAISS を再構築してサーバー再起動
# (MCP サーバー起動時に compact が自動実行される)
```

**注意**: `--all --apply` は全ノードの累積 recall 履歴をリセットするため不可逆。対象を `--tag` や `--min-displacement` で絞るか、`scripts/migrate.py --apply` で自動バックアップ後に実行することを推奨。

## ファイルで登録した文書が recall に出てこない

**症状**: `scripts/load_files.py` 等で `source="file"` として登録したはずの書籍 / ノート / ドキュメントが、明らかにヒットするはずの自然文 query でも `recall(source_filter=["file"])` の top-K に出てこない。直接 SQL で確認すると documents/nodes table にはあり、内容も合っている。

**典型ケース** (2026-05-14 観測):
- query: 「あの航空機事故はこうして起きた」
- 期待: 同名書籍の chunks が top に
- 実際: 京都大学入試、会社四季報、無修正でも合法本など **無関係なファイル chunk** が top を占め、書籍は top-10 圏外
- 書籍 chunks の cosine sim は raw FAISS 直接検索だと 0.92 と十分高い

**原因 — Phase L Stage 1 (RRF) と Phase H Stage 1 (seed mass boost) の score scale 不整合**:

Phase L Stage 1 で BM25 RRF fusion を導入したとき、`_seed_boost(raw + α × log(1+mass))` の式は更新されなかった。RRF score は ~0.018–0.033 範囲、`α × log(1+mass)` は cosine scale (~0.9 max) 想定 → α=0.02 でも mass=22 の chunk で boost 0.062 = RRF max の 2 倍。**mass の重い無関係 chunk が semantic 距離を完全に上書き**する。

**診断スクリプト** (read-only、副作用なし):

`/tmp/diag_seed_pool.py` のように、`_union_pool` と `_seed_boost` を直接呼んで stage 別に target chunks の位置を追う。コードは `gaottt.core.gravity._union_pool` / `_seed_boost` をそのまま使う:

```python
from gaottt.core.gravity import _union_pool, _seed_boost
# ... load components (RuriEmbedder, SqliteStore, FaissIndex, BM25Index, CacheLayer) ...
qv = embedder.encode_query("<problem query>").reshape(-1).astype(np.float32)
pool = _union_pool(qv, raw_faiss, virt_faiss, 1000,
                   query_text="<query>", bm25_index=bm25, ...)
# Stage 5: source_filter
filtered = [(nid, s) for nid, s in pool if cache.get_source(nid) in {"file"}]
# Stage 6: _seed_boost — observe whether targets fall here
rescored = sorted(((nid, _seed_boost(nid, raw, cache, config, None), raw)
                   for nid, raw in filtered),
                  key=lambda t: t[1], reverse=True)
# Print top 15 with mass / boost / raw
```

target chunk が **Stage 4-5 (RRF union + source filter) で top に居る** が **Stage 6 (`_seed_boost`) で陥落** していれば scale 不整合バグ。

**対処**: `gaottt/config.py:wave_seed_mass_alpha` を `0.0` に固定(2026-05-14 以降 default)。RRF fusion が既に raw + virtual + BM25 を scale-invariant に組み合わせているため、seed boost で更に mass を加える必要はない。

**Phase N tuning target**(未着手): RRF-mode を検出して mass term を score scale に正規化するか、rank-based boost に切り替える。詳細: [Plans — Roadmap](Plans-Roadmap.md)。

## `LeaseHeldError` / `LeaseLostError` が出る（MV2 owner lease）

**症状**: engine 起動時に `LeaseHeldError: owner.lock is held by another active process`、または運用中に mutating 操作（`remember` / `forget` / `relate` 等）が `LeaseLostError: Engine is read-only: the write lease was lost to another process` で失敗する。

**原因**: MV2 owner lease（`owner_lease_enabled=True` または `manifest.managed=True` の宇宙）では、同じ `data_dir` に対して書き込めるプロセスは常に 1 つ（1 宇宙 1 書き込みオーナー）。2 つ目のプロセスが `startup()` すると `LeaseHeldError`。オーナーの heartbeat が `lease_stale_seconds`（既定 60s）以上止まると別プロセスが takeover 可能になり、元オーナーは次 heartbeat で `LeaseLostError`（read-only 遷移）を受ける。

**対処 — `LeaseHeldError`（起動時）**:
- 保持しているプロセスが生きているなら、それを使う（2 つ目を開かない）
- 保持プロセスがクラッシュした確証があるなら `--force-takeover`（または `GAOTTT_LEASE_FORCE_TAKEOVER=true`）で起動 → stale lease を奪取
- **standalone 構成で不要なら**: `owner_lease_enabled=False`（default）のままならそもそも発動しない。managed 宇宙（supervisor 管理下）は `manifest.json` の `managed` を `false` に書き換えることで回避可能だが、事故防御を外す操作なので runbook でのみ案内

**対処 — `LeaseLostError`（運用中）**:
- 別プロセス（supervisor 経由の respawn 等）が takeover した。現在のオーナー経由で再接続する
- read 系（`recall(passive=True)` / `get_node` / `reflect`）は引き続き成功する。mutating 操作のみ拒否されている
- engine の shutdown は安全（stale write しない、他者 lock を消さない）

**注意**: lease 機構は **local filesystem の POSIX semantics**（`O_EXCL` / `fcntl.flock` / `os.replace`）に依存する。NFS / CIFS 等 network filesystem 上の `data_dir` では信頼できない — v1 は local FS のみサポート。

→ 関連: [Architecture — Concurrency](Architecture-Concurrency.md)「構造的解 (2): owner lease」、[Operations — Tuning](Operations-Tuning.md)「Multiverse owner lease」節

## importer 実行時に `database is locked` が出る（MV3 supervisor 起動中）

**症状**: `scripts/import_universe.py` を実行すると `aiosqlite.OperationalError: database is locked` で失敗する。supervisor が起動中で `registry.db` の書き込みを争奪している場合に発生する。

**原因**: importer は supervisor が起動中でも停止中でも動く設計ですが、両者が同時に `registry.allocate_port` や `registry.create_universe` を呼ぶと SQLite の lock 争奪が起きます。`PRAGMA busy_timeout = 30000`（最大 30 秒待機）が効きますが、高頻度な操作が重なると timeout します。

**対処**:
- **推奨**: importer 実行時に supervisor を停止する（race を構造的に回避）。importer は supervisor がいなくても registry.db を直接触って完結します
- supervisor 起動中に実行する場合は WAL + busy_timeout で最終的に整合しますが、頻発するなら supervisor 停止を推奨します
- importer 側は max 3 回 retry で `IntegrityError`（port race）を救済しますが、`OperationalError`（lock timeout）は retry 対象外です

→ 詳細: [Operations — Multiverse Import Universe](Operations-Multiverse-Import-Universe.md)「トラブルシューティング」、[Architecture — Concurrency](Architecture-Concurrency.md)

## importer 後に backend が起動しない（embedder identity mismatch）

**症状**: importer で取り込んだ宇宙に supervisor が backend を spawn するが、`engine.startup()` で `verify_embedder_identity` が失敗して backend が起動しない、または即座に crash する。

**原因**: source の manifest に記録された `embedder_id` / `embedding_dim` と、supervisor が spawn する backend が使う embedder service（`GAOTTT_EMBEDDER_ENDPOINT`）の `/info` が一致しない。典型的には:

- source を embed した model と現 embedder service の model が違う（model swap の残滓）
- source に manifest.json が無く、importer が config fallback で `cl-nagoya/ruri-v3-310m` を記録したが、実際の vector は別 model で生成されていた（semantic drift、importer は falsify 不可）

**対処**:
- **`/info` で現 embedder service の `model_name` / `dimension` を確認** し、manifest と一致しているか確認
- **source を再 embed する**（別 model で生成されていた場合）: `scripts/rebuild_faiss_from_db.py --apply`（同一 model で再構築、決定論的）または別 model で再構築（破壊的、displacement は reset）
- **`--embedder-id` / `--embedder-version` で override**: importer 実行時に manifest の identity を明示的に指定（`scripts/import_universe.py --source ... --embedder-id cl-nagoya/ruri-v3-310m --embedder-version <rev>`）
- **緊急 escape**: `GAOTTT_MANIFEST_CHECK_ENABLED=false` で manifest 整合 check を無効化（[Tuning](Operations-Tuning.md) 参照）。ただし別 model の vector を混在させる危険があるので、恒久対応ではなく一時的な回避策としてのみ

**semantic drift は検出不可**: importer は次元チェック（`config.embedding_dim`）までしかできず、過去に別 model で embed された vector の意味的 drift は falsify できません。実質的な検証は [runbook の典型的利用フロー step 4](Operations-Multiverse-Import-Universe.md#4-target-で-engine-を直接起動して-recall-の-round-trip-を確認推奨) の `recall` round-trip で行ってください。

→ 詳細: [Operations — Multiverse Import Universe](Operations-Multiverse-Import-Universe.md)「既知の制約と今後の改善点」、[Operations — Backup & DR](Operations-Backup-Multiverse.md)「embedder artifact pinning」

## importer 実行時に WAL が大きすぎる WARNING / hard reject が出る

**症状**: importer 実行時に `source WAL is N bytes (> 67108864)`（WARNING、64MB）または `source WAL is N bytes (> 268435456)`（hard reject、256MB → exit 5）が出る。

**原因**: source backend が正常に shutdown せず、WAL に未 checkpoint の変更が積もっている。SQLite は WAL を checkpoint して本体に merge しないと、copy した DB の整合性が保証できません（unflushed 変更が失われる / torn write の可能性）。

**対処**:
- **source backend を正常 shutdown して WAL checkpoint を実行** してください:

  ```bash
  # source を触る全プロセスを正常停止（SIGTERM で shutdown handler が走り checkpoint される）
  pkill -TERM -f 'gaottt.server.mcp_server'
  pkill -TERM -f 'gaottt.server.app'

  # プロセス停止後、WAL size を確認（数十 MB 以下が正常）
  ls -lh ~/.local/share/gaottt/gaottt.db-wal 2>/dev/null || echo "no WAL (clean checkpoint)"
  ```

- `> 64MB` の WARNING は `--yes` で続行できますが、可能なら checkpoint させることを推奨します
- `> 256MB` の hard reject は迂回できません。必ず source を正常 shutdown してから再実行してください
- `--force` は **WAL size check は迂回しません**（source backend 停止確認のみ迂回）。WAL が大きい場合は `--force` でも exit 5 します

> **設計理由**: SQLite WAL は通常数十 MB 以下。256MB 超は明らかに異常（他プロセスが生きている / SIGKILL された / disk I/O 異常等）なので、安全側として hard reject します。`--force` を付けても integrity_check が走るので corruption は検出されますが、WAL size 異常は事前の段的に弾く設計です。

→ 詳細: [Operations — Multiverse Import Universe](Operations-Multiverse-Import-Universe.md)「CLI リファレンス」の exit code 5

## importer 後に recall 結果が変わる場合

**症状**: importer で取り込んだ宇宙で `recall` を実行すると、元の standalone 環境と **結果の順位や内容が変わる**（特定の memory が surface しなくなる、または別の memory が上位に来る）。

**正常な挙動**: importer は `gaottt.db` の `nodes` テーブル（`displacement` / `velocity` BLOB 列を含む）および `gaottt.virtual.faiss` を copy 対象とします。target の `engine.startup` は disk から load し、`virtual.faiss` が空（size==0）の時のみ rebuild します。したがって mass / temperature / displacement / virtual 位置が全て preserve され、**import 後も同じクエリに対して同じ recall 結果を返すのが正常** です。

**結果が変わる要因**:

- **embedder service の切り替え**: standalone は in-process RuriEmbedder、multiverse は RemoteEmbedder です。同一 model・同一重みでも、batching の timing で encode 数値に微小差が出る可能性があります（bit-exact ではなく `np.allclose` 級の一致）。この場合は同点付近の順位入れ替え程度で、内容が大きく変わることはありません
- **semantic drift / corruption**: 過去に別 model で embed された vector が残っている、copy 中の torn write、`virtual.faiss` と `gaottt.db` の整合性崩れ等があると、結果が大きく変わります（特定の memory が永久に surface しない、全く無関係な memory ばかり上位に来る等）

**対処**: 結果が大きく変わる場合は `scripts/diag_recall.py snapshot` で source / target を比較してください（同一 query set を両環境に投げて raw FAISS / virtual / final の差分を取る）。torn write が疑われる場合は copy 元 backend が停止していたか確認し、必要なら `scripts/rebuild_faiss_from_db.py` で DB から再 embed してください。embedder identity mismatch が疑われる場合は [上記の embedder identity mismatch](#importer-後に-backend-が起動しないembedder-identity-mismatch) を参照してください。

→ 詳細: [Operations — Multiverse Import Universe](Operations-Multiverse-Import-Universe.md)「既知の制約と今後の改善点」の semantic drift 検出不可

## supervisor が lazy spawn した embedder が `owned_terminating` に固まる

**症状**: 開発機（systemd 無し・`supervisor_spawn_embedder=True`）で supervisor が lazy spawn した embedding service が、`/route` が `503` を返す・`create_universe` が embedder 検証失敗で 400 になる等で使えなくなる。supervisor log に `embedder pid=NNNN ... state stays owned_terminating, manual recovery required` の ERROR が出続ける。

**原因**: supervisor が spawn した embedder の termination path で、(a) 他 uid が所有する pid に SIGTERM を送って `PermissionError`、(b) SIGKILL 後も 5s 待って pid が survive した、のいずれかが起きた状態。安全側として state を `unowned` に**消さず** `owned_terminating` を保持する設計（[Tuning §MV3 follow-on](Operations-Tuning.md#multiverse-supervisor--embedder-lazy-spawn-mv3-follow-on2026-07-06)）。次回 `ensure_embedder_up()` は `_embedder_state` が `unowned` / `owned_idle` でないため `EmbedderValidationError` で失敗し、`/route` handler が 503 に mapping する（auto-respawn しない安全側）。

**対処（手動 recovery 手順）**:

```bash
# 1. supervisor log で該当 pid を特定
journalctl --user -u gaottt-supervisor -n 200 | grep "owned_terminating"
# 例: embedder pid=12345 PermissionError on SIGTERM ... state stays owned_terminating

# 2. 該当 pid を手動で cleanup（他 uid 所有なら sudo / 該当ユーザーで）
ps -p 12345 -o pid,user,cmd        # 誰が持っているか確認
kill -9 12345                      # 必要なら sudo / su

# 3. supervisor を再起動（state を `unowned` に戻す一番簡単な方法）
systemctl restart gaottt-supervisor  # または該当起動スクリプト
# 再起動後は lifespan で state が `unowned` に初期化、次回 route で lazy spawn が走る
```

**よくあるトリガー**:
- **他 uid 所有**: supervisor を user A で動かしているのに、user B が手動で `python -m gaottt.embedding.service` を port 7879 で立てた（race で user B 側が勝ち、supervisor は `unowned` に倒れるべきだが、timing によっては中途半端に所有権を主張してしまう）
- **SIGKILL 後 survive**: カーネルが zombie を reaped していない・defunct な pid。`waitpid` を待つか手動で `kill -9` する
- **NFS 上の multiverse_root**: `fcntl.flock` が POSIX semantics を保証しないので race が起きる。local FS のみサポート（[MV3 制限事項](Operations-Multiverse-Setup.md#制限事項-v1)）

**予防**:
- lazy spawn は開発機向け（systemd 1 本運用）。**本番では `deploy/gaottt-embedder.service` で systemd 常駐させることを推奨**（`/healthz` で検知して supervisor は所有権を主張しない・完全不変）
- supervisor と同じ uid で手動 embedder を立てない
- multiverse_root は local FS のみ

→ 詳細: [Tuning](Operations-Tuning.md) §Multiverse supervisor — embedder lazy spawn、[Architecture — Overview](Architecture-Overview.md) 設計判断表「Supervisor による embedder lazy spawn」

## 診断ツール一覧 (read-only)

retrieval / mass / displacement の挙動を読み解くための副作用なしスクリプト群。本番 DB に対して安全に走らせて良い (write しない、cache を汚さない)。

| スクリプト | 用途 | 主な出力 |
|---|---|---|
| `scripts/diag_recall.py` | `engine.query` の per-query snapshot (raw FAISS / BM25 / virtual / final) を取って 2 snapshot の diff を取る | `snapshot --queries-file ... --out before.json` / `diff before.json after.json`、retrieval geometry の前後比較に |
| `scripts/diag_dormant.py` | active mass 分布の percentile 表示 (Stage 7.2 `dormant_mass_percentile` のチューニング基準値を取る) | `mass` の p10/p25/p50/p75/p90 と dormant 候補数の推定 |
| `scripts/diag_pressure.py` | Phase P (Λ / Langevin) の **dry-run projection** — knob を弄った時の mass / displacement 分布変化を本番 opt-in 前に予測 | `--enable lambda --h <値>` / `--enable langevin --t0 <値>` / `--enable both`、Phase N β でも dry-run が一致 99.9% の実績 |
| `scripts/compare_retrieval.py` | `recall` / `explore(serendipity)` / `explore(dormant)` / `ambient_recall` を **同 query で横並びに表示**、Observation Apparatus Refinement Stage 3 の観測 wrapper | 同じ問いに対する 4 経路の応答差分、どの slot に何が surface しているかを 1 view で |
| `scripts/diag_dynamics.py` | mass / displacement / velocity の集計と時系列 hint | 全 node の `(mass, |d|, |v|)` 分布と簡易統計 |
| `scripts/diag_cluster_coverage.py` | cluster (`cohort_id` / `original_id`) coverage 統計 — Stage 7.1 anti-hub の effective scope 確認 | cluster_key 保有率、cluster サイズ分布 |
| `scripts/verify_faiss_recovery.py` | FAISS index と SQLite の整合性確認 (Tier B 自己診断の手動版) | ギャップ node ID 一覧、再 embed 要否 |
| `scripts/score_baseline.py` | **Phase T Stage 1** の観測 baseline — golden corpus から隔離 DB を build し、score 項別寄与率 (semantic/wave/mass/…) / Stage 3 qualification 率 / ambient gate 診断 / recall vs explore Jaccard を測定 | `--out <json>` + 人間可読 summary、`--synthetic-age-seconds <n>` で経年シミュレーション。read-only (passive のみ)、Phase T knob の before/after 比較に |

> **使い方の原則**: いずれも `--data-dir <path>` で本番 DB に向ける場合は他 MCP / REST プロセスを一旦停止 (read-only でも SQLite WAL のロック争奪は起こり得る、`cache - faiss` 整合性の write-behind 罠を避ける)。一時 DB で実験するなら `--data-dir ./.diag-tmp` のように project root 配下に置く (`/tmp` は外部 directory permission で拒否される環境あり)。
