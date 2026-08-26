---
name: gaottt
description: Long-term memory across sessions. Recall is biased by a gravity field, so frequently co-recalled memories drift together over time. Use to restore prior context at session start, save important decisions / lessons, evacuate context before compaction, and surface past failures when hitting similar problems.
---

# GaOTTT: long-term memory

External long-term memory across sessions. Backed by embeddings + a gravity-simulation that biases recall toward co-occurring and recently-touched memories. The full design rationale (physics ↔ TTT ↔ astrocyte correspondence, Phase A–L history) lives in [`docs/wiki/`](docs/wiki/Home.md) — this file is the operational reference.

## When to use

### `recall` — retrieve past memories
- **Session start.** Load the prior session's summary with `source_filter=["compaction","agent"]`.
- **User uses temporal pointers** ("last time", "before", "we already…").
- **You hit an error.** Past troubleshooting will surface.
- **Before an important design judgment** — align with prior decisions.

### `remember` — save knowledge
- **User states a preference / constraint / prohibition** → `source="user"`.
- **A problem actually gets solved** → save the cause AND fix together, `source="agent"`.
- **A judgment fails or is retracted** → `tags=["mistake","retracted"]`.
- **Iterative thinking flips your conclusion** → save the new one and `relate(edge_type="supersedes")` the old.
- **Sense compaction approaching** → evacuate session summary with `source="compaction"`.
- **Note explicitly for future you** → `tags=["letter-to-future-self"]`.

### `explore` — serendipitous discovery
- Looping in the same potential well.
- Cross-domain transfer wanted.
- User asks "got any interesting ideas?".
- **Break fixation with `mode="dormant"`**: pulls low-mass, idle-for-≥7d self-authored memos the gravity field has *not* been pulling back. Use when `recall` keeps returning the same handful of high-mass nodes ("Heavy Persona Dominance") and you want to widen the lens. `query` is ignored in this mode.

### Reading retrieval results
- Each `recall`/`ambient_recall` result now carries a one-line **`reason:`** explanation: `high mass persona proximity (mass=2.82) — possible dominance artifact` / `gravity pick (below relevance floor)` / `bm25 strong lexical match (0.71)` / `lensing pick (gap=+0.07)` / `dormant surface (percentile=8)`. When you see *"possible dominance artifact"*, the field is leaning hard on a familiar high-mass node — consider `mode="dormant"` or different phrasing. `gravity pick` (Phase T Stage 3, appears only when direct qualification is enabled) means the memo failed every relevance axis and the field — not semantics — carried it into the presentation (a fallback pick); its TTT updates are also gated when Stage 4 (`ttt_qualification_enabled`) is on.
- `ambient_recall` blocks now include a **▼ ささやき** slot when a dormant memo also lexically matches the prompt — these are the "〇〇といえば〜だったよな" picks. Silence is correct when nothing dormant matches lexically (no random hits).
- `reflect(aspect="connections")` groups results into **persona / agent_user / ingest** display buckets. Pass `bucket="persona"` (or `"agent_user"` / `"ingest"`) to filter to a single bucket — the filter applies *before* the weight-sorted top-N, so low-weight persona edges surface even when high-weight same-file chunk co-occurrence would otherwise dominate.

### Debugging retrieval geometry
- `scripts/compare_retrieval.py "<query>"` runs the same query through `recall` / `explore(diversity=0.9)` / `explore(mode="dormant")` / `ambient_recall` side-by-side. Read-only — does not perturb the field. Use this when retrieval feels off, when you want to see what each mode returns differently, or to compare before/after a config change (`--json` for diff-driven regression).

### `reflect` — inspect memory state
- **Session end**: `aspect="hot_topics"` to see the day's mass accretion.
- **Periodic pruning**: `aspect="dormant"` → confirm with user → `forget`.
- **Duplicate cleanup**: `aspect="duplicates"` → `merge`.

### `prefetch` — pre-warm recall
- Start of a turn when you can predict what the user will probe.
- Right after parsing user input, while you compose your response.

### Phase D — persona + tasks
- **Session start**: `inherit_persona()` to wear past values / intentions / commitments.
- Deeply-held belief → `declare_value`. Long-term direction → `declare_intention`. Time-bounded promise → `declare_commitment`.
- Action items → `commit` → `start` → `complete` / `abandon`.

---

## Tools

### remember

```
remember(content, source="agent", tags=None, context=None,
        ttl_seconds=None, emotion=0.0, certainty=1.0)
```

- `source`: `agent` (your own), `user`, `compaction`, `system`, `hypothesis` (auto-expires 7 days).
- `emotion ∈ [-1.0, 1.0]`: magnitude (not sign) boosts recall — both elation and frustration deserve to surface.
- `certainty ∈ [0.0, 1.0]`: 30-day half-life decay unless `revalidate`-d.

```
remember(content="No pip; use uv", source="user", tags=["preference"])
remember(content="Phase L Stage 1 完了 (BM25 union seed)", source="compaction")
remember(content="Idea: learnable temperature for explore", source="hypothesis")
remember(content="Finally fixed the FAISS leak", emotion=0.8, certainty=0.9)
```

### recall

```
recall(query, top_k=5, source_filter=None, wave_depth=None, wave_k=None,
       force_refresh=False, persona_context=None, tag_filter=None,
       output_mode="full", auto_route=True, mode="detail", passive=False)
```

**RURI embeddings are not cross-lingual** — query in the language of the memories you expect to find; bridge a language gap with `tag_filter` / `source_filter` (see the `recall` tool docstring for details).

- `output_mode` — `"compact"` (content truncated at 300 chars; **prefer this for triage**), `"ids"` (header only — id, scores, tags), `"full"` (complete content, default). For real token economy, `"ids"`/`"compact"` (and `mode="list"`) now also **suppress the per-result `breakdown:` line and the `## 訓練差分` trailer** — they ride along only in the verbose `"full"`/`"detail"` path (config `recall_trailer_verbose_modes`), so a lightweight triage call stays lightweight. The 1-line `reason:` diagnostic (Observation Apparatus Stage 1) is **not suppressed** — it is shown in all output modes by default (`recall_reason_line_modes`), so dominance-artifact warnings stay visible during triage.
- `passive=True` — **read-only recall**. Runs the search but does not perturb the gravity field: no mass update, no query-attraction displacement, no co-occurrence edges. The result is identical, only the side effects are suppressed. Use for automatic / background recall (the Claude Code ambient-recall hook calls this) so noise queries never become an uncontrolled TTT signal. Default `False` keeps recall a training step.
- `source_filter` — restrict to one or more source classes (e.g. `["agent","compaction"]`). Effective at the seed step. For sparse classes on a large DB, pass `wave_k=1000` to widen the seed pool.
- `persona_context` — list of declared value / intention / commitment ids. Force-injects them into both the seed pool AND the final top-K, bypassing `source_filter`.
- `tag_filter` — list of tag substrings; force-injects matching nodes into both seed and final top-K, bypassing `source_filter`. Use when query and target memo live in different vocabularies.
- `force_refresh=True` — bypass the prefetch cache (rare; cache is auto-invalidated on destructive ops).
- `auto_route=False` — disable auto-routing for this call (otherwise queries phrased as structured aspect questions auto-attach a matching `reflect` summary; see "Auto-routed reflect" below).
- `mode="list"` — service-level content economy: truncates each result's content to 80 chars (newline-stripped). Pair with `top_k=20` for a scannable index; follow up with `get_node(node_id=...)` on the id you care about for the full payload. Affects REST too — the truncation lives on the wire.

```
recall(query="design decisions", top_k=5, output_mode="compact")
recall(query="last session work", source_filter=["compaction","agent"])
recall(query="harakiriworks Eleventy", tag_filter=["harakiriworks-self-knowledge"])
recall(query="any past notes on X", top_k=10, output_mode="ids")     # existence check
```

**Score breakdown:** every result carries a one-line additive decomposition of `final_score` — `cos=` (raw cosine, no displacement), `vcos=` (virtual cosine, with displacement) `·decay=`, `+wave=` `+mass=` `+emo=` `+cert=` `×sat=` (habituation), plus `persona_prox=` and flags `[bm25, forced]`. Use it to judge **why** a memo scored what it did:
- `cos` near 0 with high `mass` / `wave` → memo wins by gravity, not by semantic match (treat with suspicion).
- `[forced]` → memo was force-injected by your `tag_filter` / `persona_context`, not earned.
- `[bm25]` → BM25 surface-form match contributed to the seed ranking.
- Phase T (Stage 3, when direct qualification is enabled) appends `q=+`/`q=-` (qualification verdict; `q=-` = fallback pick below the relevance floor) plus a pre-saturation split `d=` (semantic-direct: `virtual_cos_norm × decay`) / `f=` (field: wave+mass+emo+cert) / `gap=` (`virtual_cos_norm − raw_cos`, the lensing curvature). `d` dominant = earned by meaning; `f` dominant = carried by the field.

**Training delta:** every recall ends with a `## 訓練差分` trailer showing what your recall *changed* in the gravity field — `wave_reached`, `depth`, `persona_hop`, then top movers by `Δmass` and `Δ|disp|`. You're not just reading memory; you're training it. Two practical implications:
- A memo you keep recalling accumulates mass (`Δmass +0.003, +0.002, +0.001…`) — deliberate rehearsal works.
- `cache hit` trailer means **no simulation ran** — useful to distinguish "I touched the field" from "I got a free read".

**Auto-routed reflect (default on):** when your query phrasing matches a structured persona / task aspect — e.g. "現在 active な commitment は?", "持っている value", "今やってる task", "my intentions" — `recall` runs the matching `reflect` aspect in parallel and appends a `## 関連 reflect サマリ (auto-routed)` section. You don't need to switch tools manually. Pattern-based on the query surface form (not source class), so it never gates physics — it only routes which aspect summary rides along. Pass `auto_route=False` to suppress for one call (debugging, or you want pure free-form output).

### get_node

```
get_node(node_id)
```

Fetch a single node by ID — **read-only, bypasses the gravity field entirely**. Returns content, provenance (source, tags, certainty, emotion), and physical state (mass, temperature, displacement) without touching mass, co-occurrence, or displacement. This is the **only retrieval path that does not go through the seed pool** — use it when `reflect(hot_topics)` or another listing tool showed an ID but `recall` cannot surface it (lexical/gravity capture by dominant neighbours). Returns `"Node not found."` for missing or archived IDs.

**When to use which:** if you already know the ID (from `reflect`, `recall(output_mode="ids")`, `get_relations`, etc.), use `get_node`. If you're searching by meaning, use `recall`.

### ambient_recall

```
ambient_recall(query, direct_k=2, min_score=None, exclude_tags=None,
               expose_breakdown=False, recently_surfaced=None)
```

Structured **passive** (read-only, non-perturbing) recall — composes one `<gaottt-ambient-recall>` block out of a single recall:
- **▼ direct hits** — top results by gravitational `final_score`.
- **▼ gravitational lensing** — up to `config.ambient_lensing_max_k` (default 2) memories textually *far* from the query that the field's displacement has bent onto its path: associations the gravity field *learned*, which a plain embedding search would miss. Each pick independently clears `min_score`/`min_gap` (no quota relaxation — second-best is only surfaced if it's still genuinely a bent association). Multiple lateral hits per turn fire the natural "X といえば Y で、Y といえば Z" chain. Each pick now carries a `resonance` score (`[0, 1)`, cooccurrence-derived trust signal) alongside `gap`: `resonance` measures *how often the field has pulled this memo together with today's direct hits in past active recalls* — high gap + high resonance = trustworthy lateral; high gap + low resonance = bent by noise, weigh with caution.
- **▼ ⚠ contradiction** — surfaced `contradicts`-edge pairs.
- **▼ persona** — the active declared value/intention most relevant to the current query (ranked by `mass^config.ambient_persona_mass_weight × cos(query, persona_vec)` (default w=0.3, mitigates Heavy Persona Dominance — see Operations-Troubleshooting) and silently omitted below `config.ambient_persona_min_relevance`).

Every entry carries provenance metadata (`source · certainty · age`). This is what the **Claude Code `UserPromptSubmit` hook** (and the **opencode `chat.message` plugin**) calls every turn — long-term memory surfaces automatically, without you having to call `recall`. You can also call it directly for a fast structured context pull. Always passive (never perturbs the field).

- **Relevance gate (Phase T Stage 5 — OR gate, default on)** — a word-level (Sudachi) BM25 *strong-match* gate is still the primary axis, but a BM25 reject is no longer a veto: the passive recall runs anyway and a strong `virtual_score` (≥ `min_score` / `config.ambient_min_score`) **OR** a raw-cosine match (≥ `config.ambient_semantic_raw_min`, default 0.60) approves the injection. Only when both axes miss is the sentinel `(関連する記憶なし)` returned — off-topic suppression is preserved. `min_score` is a fallback knob for the legacy virtual_score gate (used only when the BM25 gate index is unavailable). Rollback: `GAOTTT_AMBIENT_GATE_OR_SEMANTIC=false` restores the legacy BM25 veto.
- **Gate diagnostics** — every return carries `gate_diagnostics` on the structured response (staged counts `candidates_generated` / `after_tag_exclusion` / `after_dump_filter` / `semantic_qualified`, gate inputs `bm25_top_score` / `bm25_gate` / `semantic_max_virtual` / `semantic_max_raw` / `virt_top1` / `bm25_top` / `composite_signal` (composite mode), and a discrete `empty_reason`: `bm25_veto` / `bm25_and_semantic_below_threshold` / `no_candidates` / `all_tag_excluded` / `all_dump_filtered`, plus composite-mode-only values `composite_reject` / `composite_pool_too_small` / `composite_reference_unavailable` (when `ambient_gate_mode="composite"` — default is `"or"`)). With `expose_breakdown=True` the formatted block appends a one-line `gate: <reason|passed> (...)` triage after the sentinel; the default output stays byte-identical.
- **`exclude_tags`** — substring-filters direct / lensing / persona candidates so test artifacts (`smoke-test`, etc.) stay out of injection without being deleted. Production hooks pass `GAOTTT_AMBIENT_EXCLUDE_TAGS=smoke-test,test` by default.
- **`expose_breakdown`** — appends a terse `[raw=.. virt=.. bm25 mass=..]` to each slot row so you can see *why* each memory surfaced (the `ScoreBreakdown` at ambient granularity). Default off (token-budget safe); toggle via `GAOTTT_AMBIENT_EXPOSE_BREAKDOWN=1`.
- **`recently_surfaced`** — `{node_id: count}` map of memos seen on recent ambient turns. Each slot's ranking score is multiplied by `config.ambient_novelty_decay ** count` (default `0.7`), so recently-seen memos rotate out of slot 1-2 turns (the controlled "〇〇といえば〜だったよな" novelty channel). The hook builds this from the past `GAOTTT_AMBIENT_NOVELTY_TURNS` (default 5) `<!-- ambient-ids ... -->` manifest comments at the bottom of each emitted block; programmatic callers can pass `None`/`{}` for no decay. Formatter emits the manifest line at the bottom of every non-empty block.

See [Guides — Ambient Recall](docs/wiki/Guides-Ambient-Recall.md).

### explore

```
explore(query, top_k=5, diversity=0.5, source_filter=None,
        persona_context=None, tag_filter=None, auto_route=True,
        mode="serendipity")
```

**Dormant surface**: pass `mode="dormant"` to bypass the wave entirely and pull random *self-authored* memos (`agent` / `value` / `intention` / `commitment` / `note` / `reference`) that have been idle ≥ 7 days **and** below the mass cut (percentile p10 by default) — the "low-mass, the field never claimed it" cohort. `query` is ignored in this mode (pass any placeholder). Use it when you suspect you've forgotten something the field alone won't surface. `training_delta` / `routing_hint` are `None` in this mode (no wave ran, no aspect intent to detect).

Higher-temperature search; pulls in cross-domain neighbors a normal recall would miss. `diversity`: `0.0` (near-normal) → `0.5` (default) → `1.0` (maximum).

**Diversified presentation (Phase T Stage 6, promoted to default on in Phase U — `GAOTTT_EXPLORE_DIVERSIFIED_PRESENTATION_ENABLED=0` to disable):** with the flag on, `diversity > 0` additionally re-selects the natural slots at the engine layer via MMR — a `top_k × explore_diversity_pool_multiplier` candidate slice, an `explore_min_semantic` raw-cosine floor for laterals, and a cohort penalty so one file/cohort cluster cannot fill the list. Forced (`tag_filter`/`persona_context`) items keep their legacy ordering. `diversity=0.0` / `None` is bit-for-bit legacy even with the flag on. The explore response header carries `wave: depth=<N> reached=<M>` (Phase U selection trace), and breakdown lines gain trailing `c=`/`src=`/`learn=` segments when populated.

### reflect

```
reflect(aspect="summary", limit=10)
reflect(aspect="connections", bucket="persona")  # filter to one bucket
```

Aspects:
- **Memory**: `summary`, `hot_topics`, `connections`, `dormant`, `duplicates`, `relations`.
- **Phase D — tasks**: `tasks_todo`, `tasks_doing`, `tasks_completed`, `tasks_abandoned`, `commitments`.
- **Phase D — persona**: `values`, `intentions`, `relationships`, `persona` (composite; same as `inherit_persona`).

`bucket` (connections only): `"persona"`, `"agent_user"`, or `"ingest"`. Filters edges to a single display bucket *before* weight-sorted top-N — use `"persona"` when you want declared value↔intention relationships that high-weight ingest edges would otherwise crowd out. Ignored for other aspects.

### auto_remember

```
auto_remember(transcript, max_candidates=5, include_reasons=True)
```

Pass a transcript chunk; returns ranked save candidates (decisions, failures/successes, preferences, lessons, metric-bearing lines). **Does not save** — review and call `remember` for the keepers.

### save_candidates

```
save_candidates(transcript, max_candidates=3, include_reasons=True, include_persona=True)
```

Stop-hook companion to `ambient_recall` — write-side symmetric. Wraps `auto_remember` in a `<gaottt-save-candidates>` block formatter so a turn-end hook can surface candidates in the *next* prompt (option A in `docs/wiki/Plans-Save-Candidates-Hook.md`). Returns the sentinel `(保存候補なし)` when no candidate clears the heuristic — the hook keys on the leading block tag to stay silent. Observation layer is automated; calling `remember` to actually save stays the agent's volitional decision (preserves Articulation as Carrier + Phase M single-rule).

### forget / restore

```
forget(node_ids=["abc-123"])              # soft archive (reversible)
forget(node_ids=["abc-123"], hard=True)   # permanent delete
restore(node_ids=["abc-123"])             # bring back from soft archive
```

Hard-deleted memories cannot be restored.

### merge

Collide near-duplicate memories into a single survivor. Masses add (capped), edges re-target, the absorbed node is soft-archived with `merged_into` pointing to the survivor. **Irreversible.** Typical flow: `reflect(aspect="duplicates")` → review → `merge`.

```
merge(node_ids=["abc-123","def-456"])                    # heaviest wins
merge(node_ids=["abc-123","def-456"], keep="def-456")    # explicit survivor
```

### compact

Periodic maintenance — TTL expiry + FAISS rebuild + optional auto-merge + orphan-edge cleanup. Run weekly or after large bulk operations.

```
compact()                                              # safe defaults
compact(auto_merge=True, merge_threshold=0.95)         # also collide duplicates
compact(expire_ttl=True, rebuild_faiss=False)          # TTL pass only
```

### revalidate

Refresh `last_verified_at` (resets certainty decay). Optionally adjust `certainty` or `emotion`.

```
revalidate(node_id="abc-123")
revalidate(node_id="abc-123", certainty=0.95)              # bump certainty
revalidate(node_id="abc-123", certainty=0.3, emotion=-0.4) # downgrade after counter-evidence
```

### relate / unrelate / get_relations

Typed directed edges between memories.

```
relate(src_id="new", dst_id="old", edge_type="supersedes",
       metadata={"reason": "<why old is wrong now>"})
relate(src_id="ext", dst_id="seed", edge_type="derived_from")
relate(src_id="A", dst_id="B", edge_type="contradicts")

unrelate(src_id="a", dst_id="b")                            # remove all edges
unrelate(src_id="a", dst_id="b", edge_type="supersedes")    # one type

get_relations(node_id="abc", direction="out")               # "out" | "in" | "both"
get_relations(node_id="abc", edge_type="supersedes")        # filter by type
```

Reserved `edge_type`: `supersedes`, `derived_from`, `contradicts`. Custom types accepted. Primary use: past-self dialogue (link a retracted judgment to its replacement).

### prefetch / prefetch_status

```
prefetch(query, top_k=5, source_filter=None,
         persona_context=None, tag_filter=None)
prefetch_status()
```

Schedule a background recall to pre-warm the gravity well; returns immediately. Cache TTL ~90s. A matching `recall(query, top_k)` within the window is served from cache instantly. Auto-invalidated on `forget` / `restore` / `merge` / `compact`. `prefetch_status` reports cache size, hit rate, pool stats.

### ingest

```
ingest(path="~/docs/notes.md")
ingest(path="~/books/", pattern="*.md", recursive=true)
```

Bulk-load a file or directory.

---

## Phase D — persona & tasks

Hierarchy:
```
value      ─ permanent bedrock
intention  ─ long-term direction       (derived_from a value)
commitment ─ time-bounded promise      (fulfills an intention)
task       ─ concrete action           (fulfills a commitment)
```

### inherit_persona

```
inherit_persona()
```

Self-introduction built from declared `value` / `intention` / `commitment` (plus relationships). **Call at session start to wear the persona accumulated across past sessions.**

### declare_value / declare_intention / declare_commitment

```
v = declare_value(content="Direct experience yields true understanding")
i = declare_intention(content="Build GaOTTT as relationship infrastructure",
                     parent_value_id=v)
c = declare_commitment(content="Ship Phase D this week",
                       parent_intention_id=i, deadline_seconds=7*86400)
```

Values & intentions are permanent (until explicitly revised). Commitments auto-expire (default 14 days) unless `revalidate`-d.

### commit / start / complete / abandon / depend

```
t = commit(content="Add Phase D tests", parent_id=c)
start(t)                                                 # active engagement; refreshes TTL
complete(t, outcome="11 tests pass on first try", emotion=0.7)
abandon(t, reason="priority dropped, will revisit Q3")
depend(task_id=t, depends_on_id=other_t)                 # soft "comes after"
depend(task_id=t, depends_on_id=other_t, blocking=True)  # hard blocker
```

Tasks auto-expire (default 30 days) unless completed, abandoned, or `revalidate`-d. `complete` draws a `completed` edge from outcome → task — the chronology becomes the gravity history.

---

## Patterns

### Compaction evacuation
Before context compression, evacuate the session's key facts:
```
remember(content="Session highlights: 1) ... 2) ... 3) ...",
         source="compaction", context="Session 2026-05-14")
```

### Session restoration
Open the next session with:
```
inherit_persona()
recall(query="last session work", source_filter=["compaction","agent"])
```

### Past-self dialogue
1. `recall` the past judgment relevant to the current question.
2. Summarize what past-you concluded.
3. Ask: "Does that still hold given the current state?"
4. If no → save the new judgment AND link the old one with `supersedes`:
   ```
   relate(src_id=new, dst_id=old, edge_type="supersedes",
          metadata={"reason": "<why the old one is wrong now>"})
   ```

### Troubleshooting record
Save cause AND fix together:
```
remember(
  content="Using Python `or` on a numpy array raises ValueError. "
          "Cause: ambiguous bool conversion. Fix: branch on `if x is not None`.",
  source="agent", tags=["troubleshooting", "python", "numpy"]
)
```
Next encounter: `recall(query="numpy ValueError", source_filter=["agent"])`.

### Letter to future self
```
remember(
  content="Next time you hit Plotly 3D color trouble: suspect marker.color RGBA alpha BEFORE marker.line",
  source="agent", tags=["letter-to-future-self", "plotly"]
)
```

### Prefetch warmup
```
prefetch(query="<what you anticipate>", top_k=5)
# ... compose your response ...
recall(query="<same>", top_k=5)        # served from cache, instant
```

### Forget ritual
```
1. reflect(aspect="dormant")
2. Show the list and confirm with the user
3. forget(node_ids=[...], hard=False)  # default soft archive (reversible)
4. forget(node_ids=[...], hard=True)   # only when truly irrecoverable
```

### Driven resonance (strengthen a key memory)
When the user says "remember this — bring it up next time too":
```
for _ in range(3):
    recall(query="<key phrases of the important memory>")
# → mass grows; the node surfaces preferentially next time
```

---

## Notes

- **28 MCP tools**: 9 memory (remember/recall/get_node/ambient_recall/explore/reflect/auto_remember/save_candidates/ingest) + 10 maintenance / relations / prefetch + 9 Phase D (commit/start/complete/abandon/depend/declare_value/declare_intention/declare_commitment/inherit_persona).
- **Duplicate `content` is auto-skipped** via SHA-256 hashing.
- **Memory persists across sessions.** Every `recall` accumulates gravity — co-recalled memories drift closer over time.
- **Cache is auto-invalidated** on `forget` / `restore` / `merge` / `compact`. Manual `force_refresh=True` is rarely needed.
- **Result rows in `recall` / `reflect`** include the full `id=<uuid>` — pass directly to `relate` / `revalidate` / `merge` / `forget` / `complete` / etc. without re-querying.
- **Fresh `remember()` is immediately findable** by `recall()` — genesis kick gives non-zero displacement / velocity / mass at index time, no warm-up needed.
- **`recall(source_filter=["agent"])` works at the seed step.** On corpus-heavy DBs, sparse classes (`agent`, `value`, `commitment`, `compaction`) may need `wave_k=1000` to reach the wave reliably.
- **Tasks (`source="task"`) and commitments (`source="commitment"`) auto-expire** unless `revalidate`-d, `complete`-d, or `abandon`-ed. The "forget by default" UX is intentional — keeping things alive is an act of care.
- **Multi-process DB sharing**: each MCP process has its own in-memory cache + FAISS index. A new `remember` from another process becomes visible after FAISS write-behind (~5s) + the reader's cache reload. **Bidirectional cache overwrite risk** for bulk re-writes — kill other MCP processes before Phase G-style priming or similar.
- **Insights about GaOTTT itself are valid memories** — save your own discoveries about how this skill behaves. The system is recursive; what you write to it literally changes how it responds next time.
- **Mechanism details** (Phase G genesis kick / Phase H source-filter + virtual FAISS / Phase I free-star + query attraction / Phase J persona-anchored retrieval / Phase K supernova cohort / Phase L BM25 hybrid retrieval): see [`docs/wiki/Plans-Roadmap.md`](docs/wiki/Plans-Roadmap.md) and [`Architecture-Overview.md`](docs/wiki/Architecture-Overview.md). Current behavior is captured in the tool signatures above.
