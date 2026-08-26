from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field


class NodeState(BaseModel):
    id: str
    mass: float = 1.0
    temperature: float = 0.0
    last_access: float = Field(default_factory=time.time)
    sim_history: list[float] = Field(default_factory=list)
    return_count: float = 0.0
    expires_at: float | None = None
    is_archived: bool = False
    merged_into: str | None = None
    merge_count: int = 0
    merged_at: float | None = None
    emotion_weight: float = 0.0
    certainty: float = 1.0
    last_verified_at: float | None = None
    # H2 — monotonic per-node revision, bumped by cache.set_node on every
    # mutation. Persisted and reloaded so it survives across processes.
    # save_node_states upserts conditionally on it (excluded.rev >=
    # nodes.rev), so a process holding a STALE node can no longer silently
    # overwrite a value another process advanced further (the documented
    # "逆方向上書き罠"). Default 0 keeps old DBs / old rows valid.
    rev: int = 0


class CooccurrenceEdge(BaseModel):
    src: str
    dst: str
    weight: float = 0.0
    last_update: float = Field(default_factory=time.time)


class DirectedEdge(BaseModel):
    """Typed directed relation between two memories (F3).

    edge_type:
      - "supersedes"   — src replaced/retracted dst (newer overrides older)
      - "derived_from" — src is an extension/derivation of dst
      - "contradicts"  — src disagrees with dst (mutual exclusion candidate)
    """
    src: str
    dst: str
    edge_type: str
    weight: float = 1.0
    created_at: float = Field(default_factory=time.time)
    metadata: dict[str, Any] | None = None


KNOWN_EDGE_TYPES: tuple[str, ...] = (
    # Phase B (F3)
    "supersedes", "derived_from", "contradicts",
    # Phase D — task & persona layer
    "completed",    # outcome → task
    "abandoned",    # reason → task
    "depends_on",   # task → task
    "blocked_by",   # task → blocker (specialised depends_on)
    "working_on",   # session_marker → task (active engagement)
    "fulfills",     # task → commitment, commitment → intention, intention → value
)


# --- Request / Response models ---

class DocumentInput(BaseModel):
    content: str = Field(..., min_length=1)
    metadata: dict[str, Any] | None = None


class IndexRequest(BaseModel):
    documents: list[DocumentInput] = Field(..., min_length=1)


class IndexedDoc(BaseModel):
    id: str


class IndexResponse(BaseModel):
    indexed: list[IndexedDoc]
    count: int
    skipped: int = 0


class QueryRequest(BaseModel):
    text: str = Field(..., min_length=1)
    top_k: int = Field(default=10, ge=1, le=100)
    wave_depth: int | None = Field(default=None, ge=0, le=5, description="Override wave recursion depth")
    wave_k: int | None = Field(default=None, ge=1, le=20, description="Override wave initial top-k")


class ScoreBreakdown(BaseModel):
    """Phase O Stage 1 — additive/multiplicative decomposition of final_score.

    Lets a TTT-aware caller see *why* a node scored what it scored:
    final = (virtual_cosine * decay_factor + wave_score + mass_boost
            + emotion_term + certainty_term) * saturation

    ``raw_cosine`` is informational only (no displacement applied). ``persona_proximity``
    and ``bm25_contributed`` reflect contributions that are *folded into* ``wave_score``
    via the seed-boost path (Phase J / Phase L), so they are exposed as informational
    fields rather than double-counted additive terms.
    """
    raw_cosine: float = 0.0          # query · original_emb (no displacement) — informational
    virtual_cosine: float = 0.0      # query · virtual_pos (= gravity_sim, what enters final)
    decay_factor: float = 1.0        # multiplicative recency decay applied to virtual_cosine
    wave_score: float = 0.0          # additive wave_boost (gravity propagation reach)
    mass_boost: float = 0.0          # additive α · log(1+mass)
    emotion_term: float = 0.0        # additive |emotion| · α_emotion
    certainty_term: float = 0.0      # additive certainty-weighted boost
    saturation: float = 1.0          # multiplicative habituation (1/(1+return_count·rate))
    persona_proximity: float = 0.0   # informational: persona-graph proximity (already in wave_score)
    bm25_contributed: bool = False   # informational: did BM25 affect seed ranking
    forced_inclusion: bool = False   # informational: was node in injected_ids (tag/persona_context)
    # Observation Apparatus Refinement Stage 1 — informational inputs for explain_score()
    node_mass: float = 0.0           # informational: the node's current mass (for dominance detection)
    bm25_score: float = 0.0          # informational: raw BM25 score if BM25 was used, else 0
    lensing_gap: float = 0.0         # informational: ambient lensing-pick gap signal (0 if not lensing slot)
    dormant_percentile: float | None = None  # informational: percentile rank if dormant surface, None otherwise
    reason: str | None = None        # Stage 1: 1-line human-readable explanation of why this scored
    # Phase T Stage 3 — direct relevance qualification (informational, does
    # not enter final_score / expected_sum). Populated whenever at least one
    # qualification flag is on, independent of expose_score_breakdown
    # reporting. In the engine query path ``lensing_gap`` above is populated
    # as ``virtual_cos_norm - raw_cos`` for the same nodes.
    qualified: bool | None = None    # None = both qualification flags OFF (legacy); False = fallback pick
    direct_score: float | None = None  # pre-saturation virtual_cos_norm * decay_factor
    field_score: float | None = None   # pre-saturation wave + mass + emotion + certainty
    # Phase U WP-5 — selection trace (observability only; never a scoring
    # or ordering input). ``cohort`` is the Stage 7.1 structural cluster
    # key (cohort_id OR original_id) the item belongs to;
    # ``provenance`` records which candidate generation produced the item
    # ("raw" FAISS seed pool / "virtual" FAISS — virtual seed pool or
    # Phase H Stage 5 virtual neighbor expansion — / "forced" tag or
    # persona injection); ``in_learn_set`` is the Stage 4 TTT learn-set
    # membership of the presented item. None on passive / synthetic /
    # ttt-OFF recalls — the learn set is all-reached there, so a True
    # would carry no information (WP-5 contract: None, not True).
    cohort: str | None = None        # informational: cluster key (cohort_id OR original_id)
    provenance: str | None = None    # informational: "raw" | "virtual" | "forced"
    in_learn_set: bool | None = None # informational: Stage 4 learn-set membership (None when the gate didn't run)

    @property
    def expected_sum(self) -> float:
        """Reproduce final_score from breakdown — within FP tolerance."""
        return (
            self.virtual_cosine * self.decay_factor
            + self.wave_score
            + self.mass_boost
            + self.emotion_term
            + self.certainty_term
        ) * self.saturation


class QueryResultItem(BaseModel):
    id: str
    content: str
    metadata: dict[str, Any] | None
    raw_score: float   # query_raw · virtual_pos (= gravity_sim). Labelled "virtual_score" in MCP output.
    final_score: float
    score_breakdown: ScoreBreakdown | None = None  # Phase O Stage 1 — None for legacy/disabled paths


class QueryResponse(BaseModel):
    results: list[QueryResultItem]
    count: int


class NodeResponse(BaseModel):
    id: str
    mass: float
    temperature: float
    last_access: float
    sim_history: list[float]
    displacement_norm: float = 0.0


class GetNodeResponse(BaseModel):
    """Observation Apparatus Round 2 Stage A — fetch-by-id (read-only).

    Combines document content/provenance with physical state.
    """
    id: str
    content: str
    source: str = ""
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    certainty: float = 1.0
    emotion: float = 0.0
    mass: float = 1.0
    temperature: float = 0.0
    last_access: float = 0.0
    displacement_norm: float = 0.0


class GraphResponse(BaseModel):
    edges: list[CooccurrenceEdge]
    count: int


class ResetResponse(BaseModel):
    reset: bool = True
    nodes_reset: int
    edges_removed: int


# --- Service layer requests (Phase S2 and beyond) ---

class RememberRequest(BaseModel):
    content: str = Field(..., min_length=1)
    source: str = "agent"
    tags: list[str] | None = None
    context: str | None = None
    ttl_seconds: float | None = None
    emotion: float = Field(default=0.0, ge=-1.0, le=1.0)
    certainty: float = Field(default=1.0, ge=0.0, le=1.0)


class RecallRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=5, ge=1, le=100)
    source_filter: list[str] | None = None
    wave_depth: int | None = Field(default=None, ge=0, le=5)
    wave_k: int | None = Field(default=None, ge=1, le=20)
    force_refresh: bool = False
    # Phase J Stage 2: explicit pool injection.
    persona_context: list[str] | None = None  # node ids of declared value/intention/commitment; None → auto-detect (Stage 1)
    tag_filter: list[str] | None = None       # additive injection — substrings (OR match) of metadata.tags entries
    # Phase O Stage 3: when True, the service classifies the query surface form
    # and runs the matching ``reflect`` aspect in parallel, attaching summary
    # to ``RecallResponse.routing_hint``. Default True (engine-side off via
    # config.auto_route_enabled). Pass False to suppress for a single call.
    auto_route: bool = True
    # Phase O Stage 4 — content economy mode.
    #   "detail" (default) — full content (legacy).
    #   "list"            — content truncated to config.list_mode_excerpt_chars
    #                       and newline-stripped, fits one line per result.
    mode: str = "detail"
    # Ambient Recall — passive (read-only) recall. When True the search runs
    # but the gravity field is NOT perturbed: no mass update, no query
    # attraction displacement, no co-occurrence edges. For automatic /
    # background recall (e.g. the Claude Code UserPromptSubmit hook) so that
    # ambient queries never become an uncontrolled TTT signal. Default False
    # preserves the legacy training-on-recall behaviour.
    passive: bool = False


class ExploreRequest(BaseModel):
    query: str = Field(..., min_length=1)
    diversity: float = Field(default=0.5, ge=0.0, le=1.0)
    top_k: int = Field(default=10, ge=1, le=100)
    # Phase J Stage 3: parity with recall — explicit pool injection for explore.
    persona_context: list[str] | None = None
    tag_filter: list[str] | None = None
    # Phase O Stage 3: parity with recall — auto-route to reflect when surface
    # form matches a structured aspect.
    auto_route: bool = True
    # Phase O Stage 5 — exploration intent mode.
    #   "serendipity" (default) — diversity-amplified semantic explore (legacy).
    #   "dormant"               — random self-authored memo older than
    #                             dormant_age_threshold_seconds AND mass ≤
    #                             dormant_mass_threshold AND source ∈
    #                             dormant_source_classes. Bypasses the wave
    #                             entirely; intentionally counter-importance.
    mode: str = "serendipity"


class ForgetRequest(BaseModel):
    node_ids: list[str] = Field(..., min_length=1)
    hard: bool = False


class RestoreRequest(BaseModel):
    node_ids: list[str] = Field(..., min_length=1)


class RevalidateRequest(BaseModel):
    node_id: str = Field(..., min_length=1)
    certainty: float | None = Field(default=None, ge=0.0, le=1.0)
    emotion: float | None = Field(default=None, ge=-1.0, le=1.0)


# --- Service layer responses (Phase S1 and beyond) ---

class MemoryItem(BaseModel):
    """Recall / explore result item with denormalized fields for display."""
    id: str
    content: str
    metadata: dict[str, Any] | None = None
    raw_score: float
    final_score: float
    source: str = "unknown"
    tags: list[str] = Field(default_factory=list)
    displacement_norm: float = 0.0
    score_breakdown: ScoreBreakdown | None = None  # Phase O Stage 1


class RememberResponse(BaseModel):
    """Outcome of a single remember call."""
    id: str | None = None
    duplicate: bool = False
    expires_at: str | None = None  # ISO 8601 local time, for display


class ForgetResponse(BaseModel):
    affected: int
    requested: int
    hard: bool = False


class RestoreResponse(BaseModel):
    affected: int
    requested: int


class RevalidateResponse(BaseModel):
    found: bool
    id: str | None = None
    certainty: float | None = None
    emotion_weight: float | None = None


class TrainingDelta(BaseModel):
    """Phase O Stage 2 — TTT update visibility for the caller.

    State changes induced by this recall — the *backward pass* of the
    forward-pass that ScoreBreakdown describes (Phase I Stage 2's
    ``a = (α · score / m_i) · (q - pos_i)`` term lands here as signed
    displacement_changes).

    - ``displacement_changes`` — node_id → Δ|displacement| (post − pre, signed).
      Positive means the node drifted *away* from its original embedding by
      more after this recall; negative means closer.
    - ``mass_changes`` — node_id → Δmass (Phase M self-force filter applied).
    - ``wave_reached_count`` — total reached nodes (informational).
    - ``wave_max_depth`` — requested / configured wave depth (not actual reach).
    - ``persona_hop_reached`` — count of reached nodes with persona_proximity > 0
      (Phase J graph traversal landed there).
    - ``supernova_triggered`` — always ``False`` for recall path; kept for
      parity with ingest path (where batch ``remember`` can trigger
      Phase K cohort supernova).
    - ``cache_hit`` — when ``True``, no simulation ran (prefetch cache served
      the result), so all delta dicts/counts are zero by definition.
    - ``topk_only`` — ``displacement_changes`` / ``mass_changes`` cover only the
      top-K returned nodes (default ``True`` for context economy). ``False``
      means full reached-node coverage (debug / observability mode).
    - ``intent_centers`` — Multi-Source Query: how many clause segments the
      prompt was split into (1 = single-source / disabled). >1 means the
      wave seeded from that many superposed point masses. See
      docs/wiki/Plans-Query-Mass-Distribution.md.
    """
    displacement_changes: dict[str, float] = Field(default_factory=dict)
    mass_changes: dict[str, float] = Field(default_factory=dict)
    wave_reached_count: int = 0
    wave_max_depth: int = 0
    persona_hop_reached: int = 0
    supernova_triggered: bool = False
    cache_hit: bool = False
    topk_only: bool = True
    intent_centers: int = 1


class RoutingHint(BaseModel):
    """Phase O Stage 3 — auto-routed reflect summary attached to recall/explore.

    Set when the query surface form matched a structured persona/task aspect
    (e.g. "現在 active な commitment" → ``aspect="commitments"``) and the
    service ran the matching ``reflect`` aspect in parallel. The free-form
    recall result is still returned in ``items``; the reflect summary lives
    here so the caller sees both layers without having to switch tools.

    ``reflect_summary`` is the same human-readable string that ``reflect``
    would produce for the matched aspect (via ``services.formatters``).
    ``auto_routed=False`` means the caller passed ``auto_route=False`` or the
    config switch was off — the field is set so the caller can tell the
    difference between "router was off" and "router didn't match".
    """
    aspect: str | None = None              # matched aspect name (e.g. "commitments"), or None when no match
    pattern_matched: bool = False          # True iff detect_aspect returned non-None
    auto_routed: bool = False              # True iff service actually ran the reflect call
    reflect_summary: str | None = None     # formatted aspect output, or None if no run


class RecallResponse(BaseModel):
    items: list[MemoryItem] = Field(default_factory=list)
    count: int = 0
    training_delta: TrainingDelta | None = None  # Phase O Stage 2
    routing_hint: RoutingHint | None = None      # Phase O Stage 3


class ExploreResponse(BaseModel):
    items: list[MemoryItem] = Field(default_factory=list)
    count: int = 0
    diversity: float = 0.0
    training_delta: TrainingDelta | None = None  # Phase O Stage 2
    routing_hint: RoutingHint | None = None      # Phase O Stage 3
    # Phase U WP-5 — selection trace: wave propagation observability for
    # the explore header (``wave: depth=… reached=…``). ``depth`` is the
    # effective (explore-widened) wave depth actually used, ``reached``
    # the wave reach (same value TrainingDelta.wave_reached_count
    # reports when the delta block is enabled). None on the dormant
    # mode (the wave is bypassed there).
    wave_depth: int | None = None
    wave_reached: int | None = None


# --- Ambient Recall Enrichment (structured passive-recall injection) ---
# docs/wiki/Plans-Ambient-Recall-Enrichment.md

class AmbientMemory(BaseModel):
    """A memory as it appears in a slot of the <gaottt-ambient-recall> block —
    denormalized, and carrying the provenance metadata the LLM needs to weigh
    it (source / certainty / age)."""
    id: str
    content: str                       # excerpt, newline-collapsed
    source: str = "unknown"
    tags: list[str] = Field(default_factory=list)
    certainty: float | None = None     # F7 certainty (None if node state unavailable)
    age_days: float | None = None      # (now − last_access) / 86400 — staleness signal
    virtual_score: float = 0.0         # query · virtual_pos — physics-modulated relevance
    final_score: float = 0.0
    lensing_gap: float | None = None   # virtual_cosine − raw_cosine (lensing slot only)
    # Lateral Association Stage 5 (Plans-Ambient-Recall-Lateral-Association.md):
    # cooccurrence-derived trust signal for a lensing pick.
    # ``resonance = raw / (raw + scale)`` where
    # ``raw = sum_{d in direct} cache.get_neighbors(lensing)[d]``. Bounded to
    # ``[0, 1)``. None = not computed (= not a lensing pick or Stage 5 disabled).
    lensing_resonance: float | None = None
    because: str | None = None         # Stage 2 — derived_from/supersedes parent excerpt
    # Refinement Stage 3 (Plans-Ambient-Recall-Refinement.md) — Phase O
    # Stage 1's ``ScoreBreakdown`` exposed at ambient slot granularity so the
    # caller can see *why* a memory was surfaced (raw vs virtual cosine,
    # BM25 contribution, mass boost, ...). Opt-in via the ``expose_breakdown``
    # arg on ``ambient_recall`` (default off — token budget preserved).
    breakdown: ScoreBreakdown | None = None


class AmbientTension(BaseModel):
    """Stage 2 — a ``contradicts``-edge pair surfaced as a caution."""
    memory_id: str
    memory_excerpt: str
    contradicts_id: str
    contradicts_excerpt: str


class AmbientPersona(BaseModel):
    """Stage 3 — an active declared value/intention surfaced for grounding."""
    id: str
    kind: str                          # "value" | "intention"
    content: str
    # Refinement Stage 3 — minimal breakdown for the persona slot. Only the
    # fields the persona pick actually computes (mass + cos(query, persona))
    # are populated; the rest stay at defaults.
    breakdown: ScoreBreakdown | None = None


class AmbientRecallRequest(BaseModel):
    """Ambient Recall Enrichment — structured passive-recall injection.
    Shared by the MCP tool and the REST endpoint."""
    query: str = Field(..., min_length=1)
    direct_k: int = Field(default=2, ge=1, le=10)        # ① direct-hit count
    # Relevance gate — the response is empty unless the best virtual_score
    # among candidates clears this. None → config.ambient_min_score.
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    # Refinement Stage 2 (Plans-Ambient-Recall-Refinement.md) — substring
    # tag exclusion applied to direct / lensing / persona slots. Mirrors the
    # positive ``tag_filter`` substring semantics of Phase J Stage 2: an item
    # whose tags contain any of these substrings is dropped from candidate
    # pools before slot composition. Default None / empty list = no filter.
    # Production hooks pass e.g. ``["smoke-test"]`` to keep MCP/REST smoke
    # memories out of ambient injection without deleting them.
    exclude_tags: list[str] | None = None
    # Refinement Stage 3 — opt-in ``ScoreBreakdown`` per slot. Default off
    # so the ambient block stays compact (token budget). Hook env
    # ``GAOTTT_AMBIENT_EXPOSE_BREAKDOWN=1`` toggles for debug sessions.
    expose_breakdown: bool = False
    # Lateral Association Stage 1 sub-step 1
    # (Plans-Ambient-Recall-Lateral-Association.md) — caller-supplied
    # "node_id → recent surface count" map. When set, candidates in the map
    # have their slot ranking score multiplied by
    # ``config.ambient_novelty_decay ** count`` before slot composition,
    # rotating recently-surfaced memos out of slots 1-2 turns. Provided by
    # the UserPromptSubmit hook from past N turns of transcript parsing
    # (``GAOTTT_AMBIENT_NOVELTY_TURNS``). None / empty = no decay (legacy).
    recently_surfaced: dict[str, int] | None = None


class AmbientGateDiagnostics(BaseModel):
    """Phase T Stage 5 — staged ambient-gate diagnostics.

    Populated by ``ambient_recall`` on EVERY return (empty or not) so a
    caller can triage silence: was the pool empty, did tag/dump exclusion
    empty it, or did the relevance gate reject candidates that existed?
    Counts are staged in pipeline order; the gate inputs follow.
    """
    # staged candidate counts (pipeline order)
    candidates_generated: int = 0     # passive recall pool size (pre-exclusion)
    after_tag_exclusion: int = 0
    after_dump_filter: int = 0
    semantic_qualified: int = 0       # pool items with virtual ≥ threshold OR raw ≥ raw_min
    direct_selected: int = 0
    lensing_selected: int = 0
    # gate inputs / verdicts (None = axis not computed: gate index
    # unavailable for bm25, no breakdown for raw cosine)
    bm25_top_score: float | None = None
    bm25_gate: bool | None = None     # True/False/None (None = gate index unusable)
    semantic_max_virtual: float | None = None
    semantic_max_raw: float | None = None
    # Phase U WP-3 — composite gate axes (mode="composite" のみ populated)。
    # raw 軸は ``expose_score_breakdown`` に非依存 (ambient_recall が自前の
    # raw FAISS 検索で算出) — breakdown が off でもこれらは埋まる。
    # ``virt_percentile`` は参照分布 (較正 artifact) に対する [0,100] の
    # empirical percentile、``margin`` は pool top-1 virtual − pool virtual
    # median、``raw_top1`` は engine 自身の raw FAISS top-1 cosine。
    # ``composite_signal`` は accept ("bm25_strong" / "semantic_composite")
    # または拒否理由 ("composite_reject" / "composite_pool_too_small" /
    # "composite_reference_unavailable")。mode="or" では全て None のまま。
    virt_percentile: float | None = None
    margin: float | None = None
    raw_top1: float | None = None
    composite_signal: str | None = None
    # discrete, unique empty reason — None means the response is not empty.
    # "bm25_veto" (legacy flag off, bm25 reject → immediate empty) /
    # "bm25_and_semantic_below_threshold" / "no_candidates" (recall 0 件) /
    # "all_tag_excluded" / "all_dump_filtered" /
    # Phase U WP-3 (mode="composite"): "composite_reject" (semantic 軸 未達) /
    # "composite_pool_too_small" (pool < 2 で margin 未定義) /
    # "composite_reference_unavailable" (参照 artifact 欠損・破損・
    # fingerprint 不一致・count drift 超過 — fail-closed)。
    empty_reason: str | None = None


class AmbientRecallResponse(BaseModel):
    """Structured ambient-recall block. ``count == 0`` means the relevance
    gate suppressed injection (nothing relevant enough surfaced) — check
    ``gate_diagnostics.empty_reason`` for which stage produced the silence.

    Lateral Association Stage 3 (2026-05-25) — ``lensing`` is now a list to
    host top-K picks (capped by ``config.ambient_lensing_max_k``, default 2).
    Empty list means no candidate cleared ``min_score`` + ``min_gap``.
    """
    direct: list[AmbientMemory] = Field(default_factory=list)     # ① top final_score hits
    lensing: list[AmbientMemory] = Field(default_factory=list)    # ② top-K gravitational-lensing picks (Stage 3)
    # Observation Apparatus Refinement Stage 2 — dormant whisper slot.
    # Counter-importance-sampled dormant memos that *also* satisfy a strong
    # BM25 lexical match against the query. Surfaces "〇〇といえば〜だったよな"
    # picks the gravity field has neglected. Empty when no dormant candidate
    # cleared ``ambient_dormant_relevance_floor``. Not counted toward
    # ``count`` (it rides along an injection the gate already approved).
    dormant: list[AmbientMemory] = Field(default_factory=list)    # ③ dormant whispers
    tensions: list[AmbientTension] = Field(default_factory=list)  # ⑤ contradicts pairs (Stage 2)
    persona: AmbientPersona | None = None                         # ⑥ persona grounding (Stage 3)
    count: int = 0                                                # total memories surfaced
    # Phase T Stage 5 — always populated by ``ambient_recall`` (even when
    # count > 0; the cost is a handful of ints) so callers can triage empty
    # returns and observe gate inputs on successful ones.
    gate_diagnostics: AmbientGateDiagnostics | None = None
    # Echo of the request flag: transport formatters receive only this
    # response, and the diagnostic line is opt-in (token budget) — the
    # default-off wire format stays byte-identical.
    expose_breakdown: bool = False


# --- Relations service ---

class RelateRequest(BaseModel):
    src_id: str = Field(..., min_length=1)
    dst_id: str = Field(..., min_length=1)
    edge_type: str = Field(..., min_length=1)
    weight: float = 1.0
    metadata: dict[str, Any] | None = None


class UnrelateRequest(BaseModel):
    src_id: str = Field(..., min_length=1)
    dst_id: str = Field(..., min_length=1)
    edge_type: str | None = None


class RelateResponse(BaseModel):
    edge: DirectedEdge


class UnrelateResponse(BaseModel):
    removed: int
    src_id: str
    dst_id: str


class RelationsResponse(BaseModel):
    node_id: str
    direction: str
    edges: list[DirectedEdge] = Field(default_factory=list)
    count: int = 0


# --- Maintenance service ---

class MergeOutcomeItem(BaseModel):
    absorbed_id: str
    survivor_id: str
    mass_before: float
    absorbed_mass: float
    mass_after: float


class MergeRequest(BaseModel):
    node_ids: list[str] = Field(..., min_length=2)
    keep: str | None = None


class MergeResponse(BaseModel):
    outcomes: list[MergeOutcomeItem] = Field(default_factory=list)
    count: int = 0


class CompactRequest(BaseModel):
    expire_ttl: bool = True
    rebuild_faiss: bool = True
    auto_merge: bool = False
    merge_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    merge_top_n: int = Field(default=500, ge=1)


class CompactResponse(BaseModel):
    expired: int
    merged_pairs: int
    faiss_rebuilt: bool
    vectors_before: int
    vectors_after: int


class PrefetchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=5, ge=1, le=100)
    wave_depth: int | None = Field(default=None, ge=0, le=5)
    wave_k: int | None = Field(default=None, ge=1, le=20)
    # Phase J Stage 3: parity with recall.
    persona_context: list[str] | None = None
    tag_filter: list[str] | None = None


class PrefetchResponse(BaseModel):
    scheduled: bool
    query: str
    top_k: int
    ttl_seconds: float


class PrefetchStatusResponse(BaseModel):
    cache: dict[str, Any]
    pool: dict[str, Any]


class ResetMassesRequest(BaseModel):
    value: float = Field(default=1.0, ge=0.0)


class ResetMassesResponse(BaseModel):
    nodes_reset: int
    value: float


class WarmDisplacementRequest(BaseModel):
    overwrite: bool = Field(
        default=False,
        description=(
            "If True, also seed nodes that already have a non-zero "
            "displacement (replacing it with velocity). Default False — "
            "only NULL / near-zero displacements are touched."
        ),
    )


class WarmDisplacementResponse(BaseModel):
    seeded: int
    skipped_no_velocity: int
    skipped_already_displaced: int
    active_total: int


# --- Ingest service ---

class IngestRequest(BaseModel):
    path: str = Field(..., min_length=1)
    source: str = "file"
    recursive: bool = False
    pattern: str = "*.md,*.txt"
    chunk_size: int = Field(default=2000, ge=100)
    include_tool_results: bool = False


class IngestResponse(BaseModel):
    path: str
    ingested: int
    skipped: int
    found: int


# --- Auto-remember service ---

class AutoRememberRequest(BaseModel):
    transcript: str = Field(..., min_length=1)
    max_candidates: int = Field(default=5, ge=1, le=50)
    include_reasons: bool = True


class AutoRememberCandidate(BaseModel):
    content: str
    score: float
    suggested_source: str
    suggested_tags: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class AutoRememberResponse(BaseModel):
    candidates: list[AutoRememberCandidate] = Field(default_factory=list)
    count: int = 0


# --- Save-Candidates service (Plans-Save-Candidates-Hook.md) ---
#
# Write-side symmetric to Ambient Recall Enrichment: a Stop hook (turn-end)
# surfaces auto_remember candidates as a structured block in the *next*
# prompt. The model decides whether to call ``remember`` — the lens is
# automated, the volitional moment (mass entry) stays manual. See
# Observation vs Physics boundary (memory id 701e7822) for the design rule.

class SaveCandidatesRequest(BaseModel):
    """MCP shape — body is a transcript blob plus optional knobs."""
    transcript: str = Field(..., min_length=1)
    max_candidates: int = Field(default=3, ge=1, le=20)
    include_reasons: bool = True
    include_persona: bool = True


class SaveCandidatesBody(BaseModel):
    """REST shape — same fields. Kept separate per the CLAUDE.md REST/MCP
    body-shape parity convention (``CompleteRequest`` / ``CompleteBody``)."""
    transcript: str = Field(..., min_length=1)
    max_candidates: int = Field(default=3, ge=1, le=20)
    include_reasons: bool = True
    include_persona: bool = True


class SaveCandidatesResponse(BaseModel):
    """Structured save-candidates block. ``count == 0`` means no candidate
    cleared the heuristic — formatter returns a sentinel and the hook stays
    silent (mirrors the ``AmbientRecallResponse.count == 0`` contract)."""
    candidates: list[AutoRememberCandidate] = Field(default_factory=list)
    persona: AmbientPersona | None = None
    count: int = 0


# --- Reflection service ---

class ReflectSummaryResponse(BaseModel):
    total_memories: int
    active_memories: int
    displaced_nodes: int
    total_edges: int
    sources: dict[str, int] = Field(default_factory=dict)


class ReflectNodeItem(BaseModel):
    id: str
    mass: float
    temperature: float = 0.0
    content_preview: str = ""


class ReflectHotTopicsResponse(BaseModel):
    items: list[ReflectNodeItem] = Field(default_factory=list)


class ReflectConnectionItem(BaseModel):
    src: str
    dst: str
    weight: float
    src_preview: str = ""
    dst_preview: str = ""
    # Observation Apparatus Refinement Stage 4 — display-layer bucket.
    # ``"persona"`` (value/intention/commitment ↔ same), ``"agent_user"``
    # (agent/user/hypothesis/note/... pairs), or ``"ingest"`` (any pair
    # involving file/tweet/csv/document — likely a same-batch chunk
    # artifact). None when source classification is unavailable. This is
    # informational only — edge weight and count are unchanged.
    bucket: str | None = None
    # Convenience: source labels for src and dst, populated alongside
    # ``bucket`` when classification succeeded.
    src_source: str | None = None
    dst_source: str | None = None


class ReflectConnectionsResponse(BaseModel):
    items: list[ReflectConnectionItem] = Field(default_factory=list)
    total: int = 0
    # Set only when a bucket filter was actually applied (caller passed
    # ``bucket=...`` AND ``connections_grouped_by_source`` is True). None
    # otherwise, so the formatter never shows a misleading filter banner.
    filter_bucket: str | None = None
    # Total edge count in the filtered pool *before* the top-N slice —
    # observability for "how many persona edges exist overall?". None when
    # no filter was applied.
    filtered_total: int | None = None


class ReflectDormantItem(BaseModel):
    id: str
    age_days: float
    mass: float
    content_preview: str = ""


class ReflectDormantResponse(BaseModel):
    items: list[ReflectDormantItem] = Field(default_factory=list)


class ReflectDuplicateMember(BaseModel):
    id: str
    mass: float
    content_preview: str = ""


class ReflectDuplicateCluster(BaseModel):
    ids: list[str]
    avg_pairwise_similarity: float
    members: list[ReflectDuplicateMember] = Field(default_factory=list)


class ReflectDuplicatesResponse(BaseModel):
    clusters: list[ReflectDuplicateCluster] = Field(default_factory=list)
    threshold: float = 0.95


class ReflectRelationEdgeItem(BaseModel):
    src: str
    dst: str
    edge_type: str
    weight: float


class ReflectRelationsOverviewResponse(BaseModel):
    total: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)
    recent: list[ReflectRelationEdgeItem] = Field(default_factory=list)


class TaskSurfaceItem(BaseModel):
    id: str
    content: str
    deadline: str | None = None  # ISO or "permanent"
    days_left: float | None = None  # None when permanent


class ReflectTasksTodoResponse(BaseModel):
    total: int = 0
    items: list[TaskSurfaceItem] = Field(default_factory=list)


class TaskDoingItem(BaseModel):
    id: str
    content: str
    minutes_since_last_verify: float


class ReflectTasksDoingResponse(BaseModel):
    items: list[TaskDoingItem] = Field(default_factory=list)


class TaskOutcomePair(BaseModel):
    task_id: str
    task_preview: str
    other_id: str      # outcome_id for completed, reason_id for abandoned
    other_preview: str
    timestamp: str     # formatted local time


class ReflectTasksCompletedResponse(BaseModel):
    total: int = 0
    items: list[TaskOutcomePair] = Field(default_factory=list)


class ReflectTasksAbandonedResponse(BaseModel):
    total: int = 0
    items: list[TaskOutcomePair] = Field(default_factory=list)


class ReflectCommitmentsResponse(BaseModel):
    total: int = 0
    items: list[TaskSurfaceItem] = Field(default_factory=list)


class PersonaItem(BaseModel):
    id: str
    content: str


class ReflectValuesResponse(BaseModel):
    total: int = 0
    items: list[PersonaItem] = Field(default_factory=list)


class ReflectIntentionsResponse(BaseModel):
    total: int = 0
    items: list[PersonaItem] = Field(default_factory=list)


class RelationshipMemory(BaseModel):
    id: str
    content: str


class RelationshipEntry(BaseModel):
    who: str
    memories: list[RelationshipMemory] = Field(default_factory=list)


class ReflectRelationshipsResponse(BaseModel):
    total_people: int = 0
    total_memories: int = 0
    people: list[RelationshipEntry] = Field(default_factory=list)


class PersonaCommitmentItem(BaseModel):
    id: str
    content: str
    deadline: str = "permanent"


class RelationshipSnapshot(BaseModel):
    id: str
    who: str
    content: str


class PersonaSnapshotResponse(BaseModel):
    values: list[PersonaItem] = Field(default_factory=list)
    intentions: list[PersonaItem] = Field(default_factory=list)
    commitments: list[PersonaCommitmentItem] = Field(default_factory=list)
    styles: list[PersonaItem] = Field(default_factory=list)
    relationships: list[RelationshipSnapshot] = Field(default_factory=list)


# --- Phase D requests ---

class CommitRequest(BaseModel):
    content: str = Field(..., min_length=1)
    parent_id: str | None = None
    deadline_seconds: float | None = None
    certainty: float = Field(default=1.0, ge=0.0, le=1.0)


class CompleteRequest(BaseModel):
    """MCP-shaped request — task_id is part of the body."""
    task_id: str = Field(..., min_length=1)
    outcome: str = Field(..., min_length=1)
    emotion: float = Field(default=0.5, ge=-1.0, le=1.0)


class AbandonRequest(BaseModel):
    """MCP-shaped request — task_id is part of the body."""
    task_id: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)


class DependRequest(BaseModel):
    """MCP-shaped request — task_id is part of the body."""
    task_id: str = Field(..., min_length=1)
    depends_on_id: str = Field(..., min_length=1)
    blocking: bool = False


# REST-shaped bodies for /tasks/{id}/* endpoints — task_id comes from the path
# so the body must NOT carry it (avoids redundant client-side duplication).

class CompleteBody(BaseModel):
    outcome: str = Field(..., min_length=1)
    emotion: float = Field(default=0.5, ge=-1.0, le=1.0)


class AbandonBody(BaseModel):
    reason: str = Field(..., min_length=1)


class DependBody(BaseModel):
    depends_on_id: str = Field(..., min_length=1)
    blocking: bool = False


class DeclareValueRequest(BaseModel):
    content: str = Field(..., min_length=1)
    certainty: float = Field(default=1.0, ge=0.0, le=1.0)


class DeclareIntentionRequest(BaseModel):
    content: str = Field(..., min_length=1)
    parent_value_id: str | None = None
    certainty: float = Field(default=1.0, ge=0.0, le=1.0)


class DeclareCommitmentRequest(BaseModel):
    content: str = Field(..., min_length=1)
    parent_intention_id: str = Field(..., min_length=1)
    deadline_seconds: float | None = None
    certainty: float = Field(default=1.0, ge=0.0, le=1.0)


# --- Phase D responses ---

class CommitResponse(BaseModel):
    id: str | None = None
    duplicate: bool = False
    expires_at: str | None = None  # ISO string or None (permanent)
    parent_id: str | None = None
    edge_error: str | None = None


class StartResponse(BaseModel):
    found: bool
    id: str
    emotion_weight: float | None = None


class CompleteResponse(BaseModel):
    outcome_id: str | None = None
    task_id: str
    duplicate: bool = False
    edge_error: str | None = None
    task_already_archived: bool = False


class AbandonResponse(BaseModel):
    reason_id: str | None = None
    task_id: str
    duplicate: bool = False
    edge_error: str | None = None


class DependResponse(BaseModel):
    task_id: str
    depends_on_id: str
    edge_type: str
    error: str | None = None


class DeclareValueResponse(BaseModel):
    id: str | None = None
    duplicate: bool = False


class DeclareIntentionResponse(BaseModel):
    id: str | None = None
    duplicate: bool = False
    parent_value_id: str | None = None
    edge_error: str | None = None


class DeclareCommitmentResponse(BaseModel):
    id: str | None = None
    duplicate: bool = False
    parent_intention_id: str
    expires_at: str | None = None
    edge_error: str | None = None
