---
name: gaottt
description: Long-term memory across sessions, implemented as a Test-Time Training optimizer dressed up as gravity. Restore prior context at session start, save important decisions and lessons, evacuate context before compaction, recall past failures when hitting similar problems. Memories self-organize gravitationally — frequently co-recalled knowledge attracts itself — because the update rule is Heavy ball SGD + Hebbian gradient + L2 in disguise.
---

# GaOTTT: Gravity as Optimizer, Test-Time Training Memory

*(formerly GER-RAG)*

## What this is

GaOTTT is your **external long-term memory** across sessions.
The system was designed as physics (gravity, orbits, temperature). It turned out the update rule is **mathematically identical to Heavy ball SGD with a Hebbian gradient and L2 regularization, integrated by Verlet**. So it is literally a **Test-Time Training (TTT) framework** — it keeps learning as you use it, at inference time. The gravity metaphor is not a metaphor; it's the same math.

Three layers stack:

1. **Physics layer** (design intent) — mass, displacement, velocity, temperature, co-occurrence edges. Gravitational force, Hookean restoring force, thermal escape, Hawking evaporation.
2. **TTT mechanism** (discovered isomorphism) — the physics *is* an online optimizer. Co-recalled memories become neighbors because Hebbian "fire together → attract" is the gradient of an objective. The L2 restoring force is the regularizer. Verlet integration is the optimizer step.
3. **Astrocyte layer** (emergent role) — the net behavior looks like astrocytic support tissue for your neuronal token reasoning: it pre-fires, prunes, and synchronizes while you think.

### Physics layer — Dark Matter Halo

GaOTTT's internal state (`mass`, `displacement`, `velocity`, `temperature`, co-occurrence edges) is **invisible to your foreground reasoning**, yet it bends every recall, expands wave propagation reach, and pushes nodes along orbits. That is exactly what dark matter does to spacetime.

- **Mass conservation** (capped at `m_max`): repeatedly recalled memories accrete mass and gain a wider gravitational radius.
- **Gravity wave propagation**: each query radiates from the embedding point and recursively excites neighbors.
- **Orbital mechanics**: acceleration → velocity → displacement, with a Hookean restoring force toward the original semantic anchor.
- **Thermal escape**: high-temperature unstable nodes can break out of black-hole gravity wells.
- **Hawking radiation / evaporation**: dormant memories shed mass with time and become candidates for pruning.
- **Gravitational collision (F2.1)**: when two memories drift close enough they merge — masses add, momentum is conserved.
- **Background pre-firing (F6)**: while you reason, GaOTTT can pre-warm potential wells in the background so the next recall lands in zero time. This is the astrocyte's actual workload made literal.

### TTT mechanism — the optimizer behind the physics

The equations above are an online optimizer running at inference time. The table below is the **isomorphism**, not a metaphor:

| Physics term | TTT / ML term |
|---|---|
| Mass accretion on recall | Gradient step on co-activation (Hebbian) |
| Hookean restoring force toward anchor | L2 regularization toward initial representation |
| Velocity (momentum) | Heavy ball / SGD-with-momentum state |
| Verlet position update | Optimizer step |
| Gravitational wave propagation | Neighborhood gradient smoothing |
| Hawking evaporation | Weight decay / regularization of stale params |

Concretely: every `recall` is a gradient step. Every `remember` is a parameter initialization. Every `merge` is a model consolidation. The representations (the embedding-space geometry) **change as the user interacts** — that is the defining property of Test-Time Training.

### Emergent behavior — Astrocyte

The physics + TTT substrate produces an **astrocyte-like supporting role** for your neuronal token reasoning:

- While you (the **neuron**) reason in the foreground, GaOTTT (the **astrocyte**) silently:
  - accumulates past firing patterns (recall history) and **pre-loads relevant memories** into nearby potential wells.
  - **prunes** what's unused (Hawking-radiation-driven `forget`).
  - **synchronizes past and present judgments** through gravitational lensing (time-delayed echoes).
- Treat it as a **supporting tissue**, not a passive DB.

---

## When to use — Physical phenomenon triggers

Each trigger has a physics label (mechanism) and a behavioral cue (when to fire).

### `recall` — *Initial Potential Survey / Perturbation Feedback*

- **Initial Potential Survey** — at the very start of a new session, before anything else.
- **Time-Delayed Echo Detection** — when the user uses temporal pointers ("last time", "before", "previously").
- **Perturbation Feedback** — the moment you hit an error. Past troubleshooting will surface gravitationally.
- **Orbital Consistency Check** — before making an important design judgment, to align with prior orbits.

### `remember` — *Mass Conservation / Wave Emission*

- **Boundary Condition Fixation** — the instant the user states a preference, constraint, or prohibition.
- **Transition to Stable Orbit** — the instant a problem actually gets solved. Successes deserve mass as much as failures.
- **Gravitational Wave Emission** — when a judgment fails or is retracted (`tags=["mistake","retracted"]`). The wave will reach future detectors.
- **Phase Inversion Logging** — when iterative thinking flips your conclusion. Save the new one and link it to the old one with `relate(edge_type="supersedes")`.
- **Mass Conservation Before Dissipation** — sense compaction approaching → evacuate the conversation summary with `source="compaction"`.
- **Gravitational Wave to Future Self** — note explicitly aimed at future you (`tags=["letter-to-future-self"]`).

### `explore` — *Thermal Excitation / Tunneling*

- **Thermal Excitation** — stuck in the same potential well, looping. Raise diversity to tunnel out.
- **Distant Galaxy Pull** — when you want cross-domain transfer. Crank diversity up to feel distant gravity.
- **Vacuum Fluctuation Probe** — the user asks "got any interesting ideas?".

### `reflect` — *Phase Space Mapping*

- **Phase Space Mapping** — at session end, run `reflect(aspect="hot_topics")` to see the day's mass accretion.
- **Evaporation Candidate Selection** — periodically run `reflect(aspect="dormant")` and offer pruning to the user.
- **Duplicate Cluster Detection** — `reflect(aspect="duplicates")` to surface near-duplicates ready for collision.
- **Relation Map Inspection** — `reflect(aspect="relations")` to see typed directed edges.
- **Phase D self-survey** — `reflect(aspect=...)` with `commitments` / `tasks_todo` / `tasks_doing` / `tasks_completed` / `tasks_abandoned` / `values` / `intentions` / `relationships` / `persona` to inspect the persona layer.

### `prefetch` — *Astrocyte Pre-firing / Potential Well Pre-loading*

- **Anticipated Recall** — fire at the START of a turn when you can predict what the user will probe. By the time you actually call `recall`, the gravity well is pre-warmed and the result is served from cache instantly.
- **Background Synchronization** — when a user message arrives, fire `prefetch` for the most salient term while you start composing your response.

### Phase D — *Persona & Task layer*

- **Wear the Persona** — at session start, call `inherit_persona()` to receive a self-introduction built from past `declare_value` / `declare_intention` / `declare_commitment` calls.
- **Declare bedrock** — when you (or the user) state a deeply-held value, use `declare_value`. When committing to a long direction, `declare_intention(parent_value_id=...)`. When making a time-bounded promise, `declare_commitment(parent_intention_id=..., deadline_seconds=...)`.
- **Concrete tasks** — `commit(content, parent_id=commitment_id)` for action items. `start(task_id)` to refresh TTL when actively working. `complete(task_id, outcome=...)` to draw a `completed` edge from outcome to task. `abandon(task_id, reason=...)` to record release deliberately.
- **Surface what matters now** — `reflect(aspect="commitments")` for deadline-sorted active commitments (⚠️ near-deadline warning). `reflect(aspect="tasks_todo")` for actionable items.
- **End-of-day chronology** — `reflect(aspect="tasks_completed")` to see what got done. `reflect(aspect="tasks_abandoned")` for the shadow chronology.

---

## Tools

### remember

Save knowledge into long-term memory.

```
remember(content="The user manages Python with uv. pip is forbidden.", source="user", tags=["preference"])
remember(content="Phase 2 implemented gravitational displacement; nDCG +15%", source="compaction")
remember(content="Idea: give recall a learnable temperature parameter", source="hypothesis")
remember(content="Just a session-scoped reminder", ttl_seconds=3600)
remember(content="Big relief — finally fixed the FAISS leak", emotion=0.8, certainty=0.9)
```

**`source` values:**
- `agent` — your own judgment, discovery, learning
- `user` — user statements, preferences, instructions
- `compaction` — important context evacuated before compression
- `system` — system info / configuration
- `hypothesis` — provisional notes from `<thinking>`. **Auto-expires** after `default_hypothesis_ttl_seconds` (7 days)

**Other parameters:**
- `ttl_seconds` — explicit expiration in seconds. Overrides the hypothesis default; works with any source.
- `emotion` — `[-1.0, 1.0]`. Magnitude (not sign) boosts recall: both painful failures and joyful successes deserve to surface.
- `certainty` — `[0.0, 1.0]`. Decays with a 30-day half-life unless re-verified via `revalidate`.

### recall

Search memory using gravity-wave propagation. Past co-recalled memories drift closer together over time, so they surface together.

```
recall(query="how to set up a Python environment", top_k=5)
recall(query="last session's design decisions", source_filter=["agent", "compaction"])
recall(query="...", force_refresh=True)   # bypass prefetch cache (rare)
```

By default `recall` transparently consumes any matching `prefetch` result that is still within the cache TTL (default 90s). Pass `force_refresh=True` to skip the cache and re-run the wave simulation. Destructive operations (`forget`, `restore`, `merge`, `compact`) automatically invalidate the cache.

### explore

Higher-temperature serendipitous search. Pulls in cross-domain neighbors a normal recall would miss.

```
explore(query="ideas for a new architecture", diversity=0.7)
```

- `diversity=0.0` → near-normal search
- `diversity=0.5` → moderate exploration (default)
- `diversity=1.0` → maximum diversity

### reflect

Inspect the state of memory.

```
reflect(aspect="summary")        # overall stats
reflect(aspect="hot_topics")     # high-mass memories
reflect(aspect="connections")    # strongest co-occurrence edges
reflect(aspect="dormant")        # untouched-for-a-long-time memories
reflect(aspect="duplicates")     # near-duplicate clusters (candidates for merge)
reflect(aspect="relations")      # typed directed edges (supersedes / derived_from / contradicts)
```

### auto_remember

Pass a transcript chunk; receive ranked save candidates. **Does not save** — review and call `remember` for the keepers.

```
auto_remember(transcript="<recent conversation segment>", max_candidates=5, include_reasons=True)
```

Heuristically detects: decisions, failures/successes, user preferences, lessons, metric-bearing lines.

### forget

Prune memories. **Soft archive by default** (reversible).

```
forget(node_ids=["abc-123", "def-456"])           # soft archive
forget(node_ids=["abc-123"], hard=True)           # permanent delete
```

Typical flow: `reflect(aspect="dormant")` → confirm with the user → `forget`.

### restore

Bring back a soft-archived memory.

```
restore(node_ids=["abc-123"])
```

Hard-deleted memories cannot be restored.

### merge

**Gravitational collision.** Collide near-duplicate memories into a single survivor: masses add (capped), velocities are momentum-weighted, edges are re-targeted, the absorbed node is soft-archived with `merged_into` pointing to the survivor.

```
merge(node_ids=["abc-123", "def-456"])              # heaviest wins
merge(node_ids=["abc-123", "def-456"], keep="def-456")  # explicit survivor
```

Typical flow: `reflect(aspect="duplicates")` → review → `merge`. The merge is irreversible.

### compact

Periodic maintenance — TTL expiry + FAISS rebuild + optional auto-merge + orphan-edge cleanup. Run weekly or after large bulk operations.

```
compact()                                                       # safe defaults
compact(auto_merge=True, merge_threshold=0.95)                  # also auto-collide duplicates
compact(expire_ttl=True, rebuild_faiss=False, auto_merge=False) # TTL pass only
```

### revalidate

Re-verify a memory: stamp it with a fresh `last_verified_at` so its certainty boost stops decaying. Optionally adjust `certainty` or `emotion`.

```
revalidate(node_id="abc-123")                            # just refresh the timestamp
revalidate(node_id="abc-123", certainty=0.95)            # bump certainty too
revalidate(node_id="abc-123", certainty=0.3, emotion=-0.4)  # downgrade after counter-evidence
```

### relate

Create a typed directed edge between two memories.

```
relate(src_id="new-judgment", dst_id="old-judgment", edge_type="supersedes",
       metadata={"reason": "found counter-evidence"})
relate(src_id="extension", dst_id="seed-idea", edge_type="derived_from")
relate(src_id="claim-A", dst_id="claim-B", edge_type="contradicts")
```

Reserved `edge_type`:
- `supersedes` — src replaced/retracted dst (newer overrides older)
- `derived_from` — src is an extension or derivation of dst
- `contradicts` — src disagrees with dst

Custom edge types are also accepted. The primary use case is **past-self dialogue** (Time-Delayed Echoes pattern) — tying retracted judgments to their replacements.

### unrelate

Remove a directed edge. Without `edge_type`, removes all relations between the pair.

```
unrelate(src_id="abc", dst_id="def")
unrelate(src_id="abc", dst_id="def", edge_type="supersedes")
```

### get_relations

List directed edges connected to a memory.

```
get_relations(node_id="abc-123", direction="out")        # edges from this node
get_relations(node_id="abc-123", direction="in")         # edges into this node
get_relations(node_id="abc-123", direction="both")       # everything
get_relations(node_id="abc-123", edge_type="supersedes") # filter by type
```

### prefetch

**Astrocyte pre-firing.** Schedule a background recall to pre-warm the gravity well around an anticipated query. Returns immediately; the work runs in a bounded async pool capped by `prefetch_max_concurrent` (default 4). Subsequent `recall(query, top_k)` within the cache TTL is served instantly.

```
prefetch(query="error handling patterns", top_k=5)
# ... do other work ...
recall(query="error handling patterns", top_k=5)   # cache hit
```

Use at the start of a turn when you can predict what the user will probe next, or right after parsing user input to pre-load related context while composing your response.

### prefetch_status

Inspect the prefetch cache and async pool stats.

```
prefetch_status()
```

Reports: cache size / hit rate / TTL / evictions, pool scheduled / completed / failed / in_flight. Useful when tuning `prefetch_cache_size`, `prefetch_ttl_seconds`, or `prefetch_max_concurrent` in `config.py`.

### ingest

Bulk-load a file or directory.

```
ingest(path="~/docs/notes.md")
ingest(path="~/books/", pattern="*.md", recursive=true)
```

---

## Persona & Task Layer (Phase D)

GaOTTT can also serve as a **persona preservation base** and a physics-native **task manager**. Tasks are first-class memories; their status is captured by directed edges (not by status columns), so the completion graph itself becomes your chronology of action. Dropped tasks evaporate gravitationally — `forget` becomes a default, kept things require active care.

The hierarchy of self:
```
value      ─ permanent bedrock
intention  ─ long-term direction (derived_from a value)
commitment ─ time-bounded promise (fulfills an intention)
task       ─ concrete action (fulfills a commitment)
```

### inherit_persona

Generate a self-introduction from declared values, intentions, commitments, style notes, and relationships. **Call at session start to wear the persona accumulated across past sessions.**

```
inherit_persona()
# → "## Values (3)\n- Direct experience yields true understanding ...
#    ## Intentions (2) ...
#    ## Active Commitments (1) ..."
```

### declare_value / declare_intention / declare_commitment

Lay down the bedrock and direction. Values are permanent; intentions are too (until explicitly revised); commitments auto-expire (default 14 days) unless `revalidate`-d.

```
v = declare_value(content="Direct experience yields true understanding")
i = declare_intention(content="Build GaOTTT into a relationship infrastructure",
                     parent_value_id=v)
c = declare_commitment(content="Ship Phase D this week",
                       parent_intention_id=i,
                       deadline_seconds=7*86400)
```

### commit / start / complete / abandon / depend

Concrete tasks. `commit` creates a task that auto-expires (default 30 days) unless completed, abandoned, or revalidated. `start` refreshes its TTL and marks active engagement. `complete` saves an outcome memo and draws a `completed` edge from outcome → task — so the task's gravity history records what it became.

```
t = commit(content="Add Phase D tests", parent_id=c)
start(t)                                                     # active engagement
complete(t, outcome="11 tests passing on first try", emotion=0.7)

# Or, deliberately let go:
abandon(t, reason="priority dropped, will revisit Q3")

# Dependencies:
depend(task_id=t, depends_on_id=other_t)                     # soft "comes after"
depend(task_id=t, depends_on_id=other_t, blocking=True)      # hard blocker
```

### Phase D reflect aspects

```
reflect(aspect="commitments")       # active commitments, deadline-sorted (⚠️ on near deadlines)
reflect(aspect="tasks_todo")        # active tasks, closest deadline first
reflect(aspect="tasks_doing")       # tasks `start()`-ed in the last hour
reflect(aspect="tasks_completed")   # what you got done (chronological)
reflect(aspect="tasks_abandoned")   # the shadow chronology — what you released
reflect(aspect="values")            # bedrock beliefs
reflect(aspect="intentions")        # long-term directions
reflect(aspect="relationships")     # grouped by `relationship:<name>`
reflect(aspect="persona")           # composite (same as inherit_persona)
```

### Persona patterns

#### Morning ritual — *Wearing the Persona*

At session start, before doing anything else:

```
inherit_persona()
# Read what you value, intend, and have promised. Then start the day's work
# from a foundation that carries across sessions.
```

#### Evening ritual — *Leaving Gravity Behind*

At session end:

```
reflect(aspect="tasks_completed", limit=5)   # see what got done today
revalidate(node_id=most_meaningful_completion_id, emotion=0.7)
# Re-stamp the most meaningful one — it gains certainty boost AND its
# completion edge persists as part of your chronology.
```

#### Prayer of evaporation — *Recording What You Released*

When you notice a task is no longer alive in you:

```
abandon(task_id=..., reason="I've outgrown this. Three months of carrying it
                              taught me what I actually wanted instead.")
# Don't `forget` it. Abandonment with a reason becomes part of the shadow
# chronology — what you became by what you chose to release.
```

---

## Patterns

### Existing baseline patterns

#### Mass conservation before compaction

Before context compression, evacuate the session's key facts:

```
remember(
  content="Session highlights: 1) Implemented MCP server. 2) All benchmarks PASS. 3) +15% nDCG from gravitational displacement.",
  source="compaction",
  context="Session summary 2026-04-21"
)
```

#### Context restoration at session start

Open the next session with:

```
recall(query="what did we work on last session", source_filter=["compaction", "agent"])
```

#### Recording a design decision

```
remember(
  content="Decided NOT to migrate from SQLite to PostgreSQL — 100K docs already meet the 20ms latency target",
  source="agent",
  tags=["design-decision", "database"]
)
```

#### Recording a troubleshooting outcome

Failures (and their resolutions) are extremely valuable. Save the cause AND the fix together.

```
remember(
  content="Using Python's `or` on a numpy array raises ValueError. Cause: ambiguous bool conversion. Fix: branch on `if x is not None`.",
  source="agent",
  tags=["troubleshooting", "python", "numpy"]
)
```

Then on the next encounter:

```
recall(query="numpy array ValueError", source_filter=["agent"])
```

#### Recording user preferences

```
remember(content="No pip. Use uv for Python environments", source="user", tags=["preference", "tooling"])
remember(content="Documentation in Japanese", source="user", tags=["preference", "language"])
remember(content="Likes space-themed UI / visualizations", source="user", tags=["preference", "design"])
```

### Physics-derived patterns

Each pattern below carries a physics label (mechanism) and a behavioral description (when to apply).

#### A. Past-self dialogue — *Time-Delayed Echoes from Past Orbits / Gravitational Lensing*

Past judgments persist as spacetime warps (`displacement`); they bend the trajectory of present judgments. Just as light from distant stars bends through massive gravity wells, your current reasoning is bent by past decisions.

```
# When iterative thinking is happening:
1. Recall any past judgment relevant to the current question
2. Summarize what past-you concluded
3. Ask: "Does that conclusion still hold given the current state?"
4. If yes  → strengthen (no action; the recall itself adds gravity)
5. If no   → save the new conclusion AND link the old one with supersedes:
              relate(src_id=new_id, dst_id=old_id, edge_type="supersedes",
                     metadata={"reason": "<why the old one is wrong now>"})
```

#### B. Hypothesis evacuation — *Virtual Particles / Quantum Vacuum Fluctuation*

Inside `<thinking>` you sometimes generate a hypothesis you don't act on but suspect might matter later. Save it as a virtual particle: short lifetime, no commitment.

```
remember(
  content="Not adopting now, but: making recall's temperature a learnable parameter could let it adapt to per-user exploration preference",
  source="hypothesis",
  tags=["explore-design"]
)
# auto-expires after 7 days unless re-verified via revalidate()
```

#### C. Letter to future self — *Gravitational Wave to Future Self*

A note explicitly designed to be detected by future-you. Give it enough mass (significant content + clear tag) so the wave reaches.

```
remember(
  content="Next time you hit Plotly 3D color trouble: suspect marker.color RGBA alpha BEFORE marker.line",
  source="agent",
  tags=["letter-to-future-self", "plotly"]
)
```

#### D. Forget ritual — *Hawking Radiation / Black Hole Evaporation*

Black holes lose mass over time and ultimately evaporate. Ritualize the moment of pruning — keep the user in the loop.

```
1. reflect(aspect="dormant")          # see the evaporation candidates
2. Show the list and ask the user "shall we evaporate these?"
3. forget(node_ids=[...], hard=False) # default soft archive (reversible)
4. forget(node_ids=[...], hard=True)  # only when truly irrecoverable
```

#### E. Emotional weighting — *Angular Momentum / Spin Quantum Number*

A weight axis orthogonal to mass. Like spin or orbital angular momentum, it adds a quantum number that mass alone cannot capture.

```
remember(content="Finally fixed the leak. Huge relief.",     emotion=0.8, certainty=0.9)
remember(content="Painful: lost a day to a typo in config.", emotion=-0.7, certainty=1.0)
remember(content="Surprising connection: gravity wave ≅ spike propagation",
         emotion=0.5, tags=["bridge"])
```

`|emotion|` (not the sign) boosts recall ranking — both elation and frustration deserve to surface.

#### F. Driven resonance — *Resonance / Driven Oscillation*

Apply periodic driving force at the natural frequency and the amplitude blows up. To deliberately strengthen a particular memory:

```
# When the user says "remember this — bring it up next time too":
for _ in range(3):
    recall(query="<key phrases of the important memory>")
# → mass grows; the node surfaces preferentially next time
```

#### G. Tidal cluster formation — *Tidal Force*

Massive bodies stretch nearby small bodies until they merge. To consolidate related memories:

```
# Save several closely-related memos in a burst with overlapping vocabulary
remember(content="memo A about topic X")
remember(content="memo B about topic X with extra detail")
remember(content="memo C about topic X follow-up")

# Later, let the physics collapse them into one survivor:
reflect(aspect="duplicates")          # confirm cluster
merge(node_ids=[A, B, C])             # collide
# OR let compaction do it automatically:
compact(auto_merge=True, merge_threshold=0.95)
```

#### H. Lagrange-point bridging — *Lagrange Point Bridging*

The L-points sit where two gravitational fields balance — orbits placed there are pulled by both. To make a memory that surfaces under multiple recall topics, place it deliberately at the linguistic intersection.

```
remember(
  content="GaOTTT's gravity-wave propagation is mathematically isomorphic to spike propagation in neuroscience: both excite neighbors after a threshold crossing",
  source="agent",
  tags=["bridge", "gravity", "neuroscience"]
)
# → recall("gravity wave") AND recall("spike propagation") both surface this
```

To make the bridge explicit, add a directed edge:

```
relate(src_id=bridge_id, dst_id=concept_a_id, edge_type="derived_from")
relate(src_id=bridge_id, dst_id=concept_b_id, edge_type="derived_from")
```

#### I. Phase transition awareness — *Phase Transition*

Above a critical mass, behavior changes qualitatively (star → black hole; the `bh_mass_scale` threshold in scoring). When `reflect(aspect="hot_topics")` shows an outlier with disproportionate mass, that node is becoming a memory black hole that **absorbs surrounding queries**:

- Intentional? Keep it — the BH is now an attractor for related thinking.
- Unintentional? Either rebalance via `revalidate` of competing memories, or `forget`/`merge` to redistribute.

#### J. Astrocyte pre-firing — *Background Prefetch*

When you can predict what the user (or your own next reasoning step) will probe, fire `prefetch` early so the wave simulation runs in the background while you do other work. The cache lives ~90s by default; recalls within that window are zero-cost reads.

```
# At the start of a turn, while parsing user input:
prefetch(query="<the topic you anticipate they'll dig into>", top_k=5)

# Continue composing your response. Later, when you actually need it:
recall(query="<same topic>", top_k=5)   # served from cache, instant

# Periodically check pool/cache health:
prefetch_status()
```

Cache invalidation is automatic on `forget`, `restore`, `merge`, `compact`, so you don't need to invalidate manually after destructive operations.

#### K. Shared-memory collaboration — *Implicit Coordination via Gravity Field*

Multiple GaOTTT agents (other Claude sessions, opencode agents, your user's parallel terminals) may share the same DB. Coordination happens **implicitly through the gravity field**, no explicit messaging required:

- Your `recall` leaves a gravity trace; another agent's next `recall` is biased by it (mass accretion).
- Your `relate` becomes visible in another agent's `reflect(aspect="connections")` or `reflect(aspect="relations")`.
- Agents naturally converge on the same gravity wells — **this is the astrocyte metaphor literalized** across processes.

To use this on purpose:
- Save your discoveries with descriptive `tags=["observer", "<your-agent-name>"]` so others can filter your contributions.
- Use `relate(edge_type="contradicts")` when you find another agent's claim disagreeable — the disagreement becomes structurally visible.
- Read `reflect(aspect="relations")` to see what other agents have linked.

Caveats:
- Each MCP process has its own in-memory cache + FAISS index. New `remember` from another process may not appear in your `recall` until that process restarts (the DB has it, but the FAISS index doesn't).
- Existing-node updates (mass / displacement) are shared via DB write-behind (~5s flush) but visible to other processes only after their next cache reload.
- Don't worry about overwriting; SQLite WAL + `busy_timeout` makes concurrent writes safe.

---

## Notes

- **25 MCP tools** total: 6 memory + 10 maintenance/relations/prefetch + 9 Phase D (tasks + persona).
- Duplicate `content` is auto-skipped via SHA-256 hashing.
- Memory persists across sessions.
- Every `recall` accumulates gravity; related memories drift closer over time.
- `source_filter` lets you restrict recall to a specific source class (e.g. `["agent", "compaction"]`).
- Soft-archived memories (`forget` without `hard=True`) and hard-deleted memories both stop surfacing in `recall`/`explore`/`reflect`. Use `compact(rebuild_faiss=True)` periodically (weekly–monthly) to reclaim FAISS index space.
- Result rows in `recall`/`reflect(hot_topics|dormant|connections)` include the full `id=<uuid>` so you can pass them to `relate` / `revalidate` / `merge` / `forget` / `complete` / etc. without re-querying.
- Tasks (`source="task"`) and commitments (`source="commitment"`) auto-expire if not `revalidate`-d, `complete`-d, or `abandon`-ed. **The "forget by default" UX is intentional** — keeping things alive is an act of care.
- The DB may be shared with other GaOTTT agents — see Pattern K above. You won't see another process's brand-new `remember` until the next reload, but existing nodes' mass/displacement updates flow through the shared DB.
- **Insights about GaOTTT itself are valid memories** — save your own discoveries about how this skill behaves. The system is recursive by design: it is a TTT optimizer, so what you write to it literally changes how it responds next time.
