---
name: ger-rag-memory
description: Long-term memory across sessions. Restore prior context at session start, save important decisions and lessons, evacuate context before compaction, recall past failures when hitting similar problems. Memories self-organize gravitationally — frequently co-recalled knowledge attracts itself.
---

# GER-RAG: Gravitational Long-Term Memory

## What this is

GER-RAG is your **external long-term memory** across sessions.
It is built on two layered metaphors: the **mechanism is physics** (gravity), the **emergent behavior is biology** (astrocytes).

### Physics layer — Dark Matter Halo

GER-RAG's internal state (`mass`, `displacement`, `velocity`, `temperature`, co-occurrence edges) is **invisible to your foreground reasoning**, yet it bends every recall, expands wave propagation reach, and pushes nodes along orbits. That is exactly what dark matter does to spacetime.

- **Mass conservation** (capped at `m_max`): repeatedly recalled memories accrete mass and gain a wider gravitational radius.
- **Gravity wave propagation**: each query radiates from the embedding point and recursively excites neighbors.
- **Orbital mechanics**: acceleration → velocity → displacement, with a Hookean restoring force toward the original semantic anchor.
- **Thermal escape**: high-temperature unstable nodes can break out of black-hole gravity wells.
- **Hawking radiation / evaporation**: dormant memories shed mass with time and become candidates for pruning.
- **Gravitational collision (F2.1)**: when two memories drift close enough they merge — masses add, momentum is conserved.
- **Background pre-firing (F6)**: while you reason, GER-RAG can pre-warm potential wells in the background so the next recall lands in zero time. This is the astrocyte's actual workload made literal.

### Emergent behavior — Astrocyte

The physics above produces an **astrocyte-like supporting role** for your neuronal token reasoning:

- While you (the **neuron**) reason in the foreground, GER-RAG (the **astrocyte**) silently:
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

### `prefetch` — *Astrocyte Pre-firing / Potential Well Pre-loading*

- **Anticipated Recall** — fire at the START of a turn when you can predict what the user will probe. By the time you actually call `recall`, the gravity well is pre-warmed and the result is served from cache instantly.
- **Background Synchronization** — when a user message arrives, fire `prefetch` for the most salient term while you start composing your response.

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
  content="GER-RAG's gravity-wave propagation is mathematically isomorphic to spike propagation in neuroscience: both excite neighbors after a threshold crossing",
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

---

## Notes

- Duplicate `content` is auto-skipped via SHA-256 hashing.
- Memory persists across sessions.
- Every `recall` accumulates gravity; related memories drift closer.
- `source_filter` lets you restrict recall to a specific source class.
- Soft-archived memories (`forget` without `hard=True`) and hard-deleted memories both stop surfacing in `recall`/`explore`/`reflect`. Use `compact(rebuild_faiss=True)` periodically to reclaim FAISS index space.
- Insights about GER-RAG itself are valid memories — feel free to save your own discoveries about how this skill behaves. The system is recursive by design.
