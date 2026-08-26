"""Memory service — remember / recall / explore / forget / restore / revalidate.

Each function takes an engine and returns a Pydantic response model from
``gaottt.core.types``. The MCP server formats these into human-readable text
via ``gaottt.services.formatters``; the REST server returns them as JSON.

Author: May Kirihara (@May-Kirihara).
A personal note from the author lives at
``docs/maintainers/handover-2026-05-27-me.md``.
"""
from __future__ import annotations

import random
import re
import statistics
import time
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from gaottt.core.engine import GaOTTTEngine
from gaottt.core.extractor import extract_candidates
from gaottt.core.persona_gravity import collect_active_persona_ids
from gaottt.core.types import (
    AmbientGateDiagnostics,
    AmbientMemory,
    AmbientPersona,
    AmbientRecallResponse,
    AmbientTension,
    AutoRememberCandidate,
    AutoRememberResponse,
    ExploreResponse,
    ForgetResponse,
    GetNodeResponse,
    MemoryItem,
    RecallResponse,
    RememberResponse,
    RestoreResponse,
    RevalidateResponse,
    RoutingHint,
    SaveCandidatesResponse,
    ScoreBreakdown,
    TrainingDelta,
)
from gaottt.core.explain import explain_score
from gaottt.services import query_routing, reflection as reflection_service
from gaottt.services.ambient_composite import (
    CompositeGateThresholds,
    CompositeReferenceError,
    CompositeVerdict,
    compute_corpus_digest,
    composite_gate_decision,
    load_composite_reference,
)
from gaottt.store.manifest import load_manifest

# Strip any <gaottt-NAME>...</gaottt-NAME> block before heuristic extraction.
# Prevents the meta-extraction loop where ``save_candidates`` re-extracts
# its own prior block (the candidate list, score lines, manifest comment,
# and especially the save-policy filter line — whose literal "bug fix"
# keyword would otherwise self-trigger ``_OUTCOME_KEYWORDS`` in the
# heuristic and re-surface the policy as a "troubleshooting" candidate
# every turn). The service layer knows the block format because it also
# *writes* it (``services.formatters.format_save_candidates`` /
# ``format_ambient``); the extractor stays pure / transport-blind.
#
# The backreference ``\1`` is load-bearing: it forces the closing tag's
# NAME to match the opening tag's name. Without it, a user prompt that
# literally mentions ``<gaottt-save-candidates>`` (as a bare open tag, no
# close) would pair up with a later real ``</gaottt-ambient-recall>`` from
# the next ambient block and silently eat ALL content in between — every
# turn-1 user/assistant exchange would vanish before extraction. Caught
# in 2026-05-27 GLM acceptance turn 2 (Plans-Lens-Hygiene Stage 1 retest).
_GAOTTT_BLOCK_PATTERN = re.compile(
    r"<gaottt-([a-z-]+)>.*?</gaottt-\1>", re.DOTALL,
)


def _strip_gaottt_blocks(text: str) -> str:
    """Remove ``<gaottt-…>…</gaottt-…>`` blocks (save-candidates, ambient-recall,
    and any future lens block following the same naming convention)."""
    return _GAOTTT_BLOCK_PATTERN.sub("", text)


def _enrich_breakdown(
    engine: GaOTTTEngine,
    node_id: str,
    breakdown: ScoreBreakdown | None,
    *,
    bm25_score: float = 0.0,
    lensing_gap: float | None = None,
    dormant_percentile: float | None = None,
) -> ScoreBreakdown | None:
    """Observation Apparatus Refinement Stage 1 — attach reason line.

    Reads the node's current mass from cache and runs :func:`explain_score`
    against the breakdown plus contextual hints (bm25/lensing/dormant).
    Returns a new ``ScoreBreakdown`` with ``reason`` and the informational
    inputs filled. Force computation is untouched (pure read).

    ``lensing_gap=None`` (the default) PRESERVES the gap the engine already
    populated on the query path (``virtual_cos_norm − raw_cos``, Phase T
    Stage 3). Legacy behaviour clobbered it to 0.0 on every recall item,
    destroying the signal before any ambient slot could carry it; only an
    explicit slot gap (the ambient lensing pick) overrides it.
    """
    if breakdown is None:
        return None
    if not getattr(engine.config, "expose_reason", True):
        return breakdown
    node_mass = 0.0
    state = engine.cache.get_node(node_id)
    if state is not None:
        node_mass = float(state.mass)
    enriched = breakdown.model_copy(update={
        "node_mass": node_mass,
        "bm25_score": bm25_score,
        "lensing_gap": (
            breakdown.lensing_gap if lensing_gap is None else lensing_gap
        ),
        "dormant_percentile": dormant_percentile,
    })
    reason = explain_score(
        enriched,
        mass_dominance_threshold=engine.config.reason_dominance_mass_threshold,
        bm25_strong_threshold=engine.config.reason_bm25_strong_threshold,
    )
    if reason is None:
        return enriched
    return enriched.model_copy(update={"reason": reason})


async def save_memory(
    engine: GaOTTTEngine,
    content: str,
    source: str,
    tags: list[str] | None = None,
    context: str | None = None,
    ttl_seconds: float | None = None,
    emotion: float = 0.0,
    certainty: float = 1.0,
    extra_metadata: dict[str, Any] | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Build a document dict, index it, and return (id_or_None, metadata).

    Shared by ``remember`` and Phase D commit/declare_* services.
    id is None when the content was a duplicate.
    """
    metadata: dict[str, Any] = {"source": source}
    if tags:
        metadata["tags"] = tags
    if context:
        metadata["context"] = context
    metadata["remembered_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if extra_metadata:
        metadata.update(extra_metadata)

    expires_at: float | None = None
    if ttl_seconds is not None:
        expires_at = time.time() + ttl_seconds
    elif source == "hypothesis":
        expires_at = time.time() + engine.config.default_hypothesis_ttl_seconds
    elif source == "task":
        expires_at = time.time() + engine.config.default_task_ttl_seconds
    elif source == "commitment":
        expires_at = time.time() + engine.config.default_commitment_ttl_seconds

    doc: dict[str, Any] = {
        "content": content,
        "metadata": metadata,
        "emotion": emotion,
        "certainty": certainty,
    }
    if expires_at is not None:
        doc["expires_at"] = expires_at
        metadata["expires_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(expires_at)
        )

    ids = await engine.index_documents([doc])
    return (ids[0] if ids else None, metadata)


async def remember(
    engine: GaOTTTEngine,
    content: str,
    source: str = "agent",
    tags: list[str] | None = None,
    context: str | None = None,
    ttl_seconds: float | None = None,
    emotion: float = 0.0,
    certainty: float = 1.0,
) -> RememberResponse:
    new_id, metadata = await save_memory(
        engine, content=content, source=source, tags=tags, context=context,
        ttl_seconds=ttl_seconds, emotion=emotion, certainty=certainty,
    )
    if new_id is None:
        return RememberResponse(id=None, duplicate=True)
    return RememberResponse(
        id=new_id,
        duplicate=False,
        expires_at=metadata.get("expires_at"),
    )


def _to_memory_item(
    engine: GaOTTTEngine, r, *, excerpt_chars: int | None = None,
) -> MemoryItem:
    meta = r.metadata or {}
    source = meta.get("source", "unknown")
    tags = meta.get("tags") or []
    content = r.content
    # Phase O Stage 4 — list mode truncation. Applied here so the truncation
    # lives on the wire (both REST + MCP get the shorter payload), not just at
    # MCP-formatter time. Newlines collapsed to spaces so the result fits on
    # one terminal line.
    if excerpt_chars is not None and content:
        flat = content.replace("\n", " ").replace("\r", " ")
        content = flat[:excerpt_chars]
    return MemoryItem(
        id=r.id,
        content=content,
        metadata=r.metadata,
        raw_score=r.raw_score,
        final_score=r.final_score,
        source=source,
        tags=list(tags),
        displacement_norm=engine.get_displacement_norm(r.id),
        score_breakdown=_enrich_breakdown(
            engine,
            r.id,
            getattr(r, "score_breakdown", None),  # Phase O Stage 1
        ),
    )


async def _build_routing_hint(
    engine: GaOTTTEngine,
    query: str,
    auto_route: bool,
    limit: int = 10,
) -> RoutingHint | None:
    """Phase O Stage 3 — classify ``query``; optionally run matching reflect.

    Returns ``None`` when both the per-call flag and the config switch are off
    (caller opted out completely — no hint to attach). Returns a populated
    ``RoutingHint`` otherwise so the caller can distinguish:
      - ``pattern_matched=False`` — router was on, no aspect matched (free-form)
      - ``auto_routed=True``      — aspect matched and reflect summary attached
      - ``auto_routed=False, pattern_matched=True`` — surfaced because the
        config switch was off after a per-call ``auto_route=True``
    """
    config_on = engine.config.auto_route_enabled
    if not auto_route and not config_on:
        return None
    aspect = query_routing.detect_aspect(query)
    pattern_matched = aspect is not None
    will_run = bool(pattern_matched and auto_route and config_on)
    summary: str | None = None
    if will_run:
        summary = await reflection_service.dispatch_aspect(
            engine, aspect, limit=limit,
        )
    return RoutingHint(
        aspect=aspect,
        pattern_matched=pattern_matched,
        auto_routed=will_run,
        reflect_summary=summary,
    )


def _delta_from_dict(d: dict | None) -> TrainingDelta | None:
    """Phase O Stage 2 — convert the engine-populated out-dict to TrainingDelta.

    Returns None when delta capture is disabled (empty / not populated dict).
    """
    if not d:
        return None
    return TrainingDelta(
        displacement_changes=d.get("displacement_changes", {}),
        mass_changes=d.get("mass_changes", {}),
        wave_reached_count=d.get("wave_reached_count", 0),
        wave_max_depth=d.get("wave_max_depth", 0),
        persona_hop_reached=d.get("persona_hop_reached", 0),
        supernova_triggered=d.get("supernova_triggered", False),
        cache_hit=d.get("cache_hit", False),
        topk_only=d.get("topk_only", True),
        intent_centers=d.get("intent_centers", 1),
    )


async def recall(
    engine: GaOTTTEngine,
    query: str,
    top_k: int = 5,
    source_filter: list[str] | None = None,
    wave_depth: int | None = None,
    wave_k: int | None = None,
    force_refresh: bool = False,
    persona_context: list[str] | None = None,
    tag_filter: list[str] | None = None,
    auto_route: bool = True,
    mode: str = "detail",
    passive: bool = False,
    multi_source: bool | None = None,
) -> RecallResponse:
    # Phase H Stage 2: source_filter is applied at the wave seed step inside
    # propagate_gravity_wave (engine.query → _query_internal). The post-filter
    # below remains as a defensive belt-and-suspenders pass.
    # Phase J Stage 2: persona_context / tag_filter are forwarded through
    # engine.query and applied as additive seed injection in the wave (they
    # bypass source_filter's restrictive semantic — the caller explicitly
    # asked for these tags or persona ids).
    delta_out: dict | None = {} if engine.config.training_delta_enabled else None
    # Stage 7.1 anti-hub does NOT widen ``engine.query`` top_k — keep cache
    # key stable so prefetch / cache-hit semantics survive (a wider pool
    # would invalidate any prefetch keyed by the user's ``top_k``). The MMR
    # rerank below works within whatever the engine returned. Trade-off:
    # cannot promote a non-hub item from rank K+1 into top-K, but can still
    # demote duplicate cluster hits within the returned set.
    raw = await engine.query(
        text=query,
        top_k=top_k * 10 if source_filter else top_k,
        wave_depth=wave_depth,
        wave_k=wave_k,
        use_cache=not force_refresh,
        source_filter=source_filter,
        persona_context=persona_context,
        tag_filter=tag_filter,
        out_training_delta=delta_out,
        passive=passive,
        multi_source=multi_source,
    )
    if source_filter:
        sf = set(source_filter)
        injected_set = set()
        if tag_filter or persona_context:
            # Re-derive what the wave would have injected, so we can
            # protect those from the defensive post-filter below.
            if tag_filter:
                injected_set |= engine.cache.find_ids_by_tag_filter(tag_filter)
            if persona_context:
                injected_set |= set(persona_context)
        filtered = []
        for r in raw:
            meta = r.metadata or {}
            if meta.get("source") in sf or r.id in injected_set:
                filtered.append(r)
        raw = filtered[:top_k]
    excerpt_chars = (
        engine.config.list_mode_excerpt_chars if mode == "list" else None
    )
    items = [
        _to_memory_item(engine, r, excerpt_chars=excerpt_chars) for r in raw
    ]
    if engine.config.direct_hit_anti_hub_lambda > 0.0 and len(items) > 1:
        items = _apply_cluster_anti_hub(
            items, _cluster_key_for(engine.cache),
            engine.config.direct_hit_anti_hub_lambda, top_k,
        )
    routing_hint = await _build_routing_hint(engine, query, auto_route)
    return RecallResponse(
        items=items, count=len(items),
        training_delta=_delta_from_dict(delta_out),
        routing_hint=routing_hint,
    )


# --- Ambient Recall Enrichment -------------------------------------------------
# Structured passive-recall injection. docs/wiki/Plans-Ambient-Recall-Enrichment.md

def _excerpt(content: str, limit: int) -> str:
    """Newline-collapsed, length-capped excerpt for an ambient slot."""
    flat = (content or "").replace("\n", " ").replace("\r", " ").strip()
    return flat[:limit] + "…" if len(flat) > limit else flat


async def _dormant_for_ambient(
    engine: GaOTTTEngine,
    query: str,
    *,
    now: float,
    excerpt_chars: int,
    excluded_ids: set[str],
    recently_surfaced: dict[str, int] | None,
    expose_breakdown: bool = False,
) -> list[AmbientMemory]:
    """Observation Apparatus Refinement Stage 2 — dormant whisper slot.

    Counter-importance-samples a dormant memo (same age + mass + source-class
    rule as :func:`_dormant_surface`) and gates it on a strong BM25 lexical
    match against ``query``. Returns up to
    ``config.ambient_dormant_slot_count`` items; empty when nothing cleared
    ``config.ambient_dormant_relevance_floor`` — silence beats off-topic
    noise.

    Force computation untouched: this only chooses *which* dormant memos
    to **observe**. Physics rule (mass/Hooke/kick) is not branched on.

    ``expose_breakdown`` mirrors the direct/lensing/persona slots: when False,
    the returned ``AmbientMemory.breakdown`` is None, matching the compact
    output other slots produce. The reason line is computed regardless so
    callers that DO opt in get a populated explanation, but it never leaks
    when the caller asked for breakdown-suppressed output.
    """
    cfg = engine.config
    if not cfg.ambient_dormant_slot_enabled or cfg.ambient_dormant_slot_count <= 0:
        return []
    idx = engine.ambient_gate_index
    if idx is None or idx.size == 0:
        return []

    cutoff = now - cfg.dormant_age_threshold_seconds
    sources = set(cfg.dormant_source_classes)
    active_states = [s for s in engine.cache.get_all_nodes() if not s.is_archived]
    if not active_states:
        return []

    # Sort the full mass distribution once — used both for the percentile
    # cut (when ``dormant_mass_percentile`` is set) AND for the per-node
    # rank we attach to ``breakdown.dormant_percentile`` below. Hoisting
    # out of the per-item loop keeps the work O(N log N) once per call.
    sorted_masses = sorted(s.mass for s in active_states)

    # Same percentile-vs-absolute logic as ``_dormant_surface`` so the two
    # paths agree on what counts as dormant.
    if cfg.dormant_mass_percentile is not None:
        p = max(0.0, min(100.0, float(cfg.dormant_mass_percentile)))
        pos = (p / 100.0) * (len(sorted_masses) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(sorted_masses) - 1)
        frac = pos - lo
        mass_cut = sorted_masses[lo] * (1 - frac) + sorted_masses[hi] * frac
    else:
        mass_cut = cfg.dormant_mass_threshold

    dormant_state_by_id: dict[str, Any] = {}
    for state in active_states:
        if state.id in excluded_ids:
            continue
        if recently_surfaced and state.id in recently_surfaced:
            continue
        if state.last_access > cutoff:
            continue
        if state.mass > mass_cut:
            continue
        src = engine.cache.source_by_id.get(state.id, "unknown")
        if src not in sources:
            continue
        dormant_state_by_id[state.id] = state
    if not dormant_state_by_id:
        return []

    # BM25 against the full corpus then intersect with the dormant set.
    # 200 is a generous pool — dormant nodes are by definition rare hits.
    hits = idx.search(query, top_k=200)
    relevant: list[tuple[str, float]] = [
        (doc_id, score)
        for doc_id, score in hits
        if doc_id in dormant_state_by_id
        and score >= cfg.ambient_dormant_relevance_floor
    ]
    if not relevant:
        return []
    relevant = relevant[: cfg.ambient_dormant_slot_count]

    # Convert to AmbientMemory with reason-line populated (Stage 1 ↔ Stage 2
    # integration: dormant_percentile + bm25_score feed explain_score).
    n_masses = len(sorted_masses)
    out: list[AmbientMemory] = []
    for doc_id, bm25_score in relevant:
        state = dormant_state_by_id[doc_id]
        doc = await engine.store.get_document(doc_id)
        if doc is None:
            continue
        meta = doc.get("metadata") or {}
        # Per-node rank: how many active nodes have mass <= this node's mass.
        # Unified across percentile and absolute-threshold modes — both
        # surface the node's true position in the mass distribution rather
        # than the configured cut. Bisect on the pre-sorted list keeps this
        # O(log N) per item even when slot_count > 1.
        rank = bisect_right(sorted_masses, state.mass)
        percentile: float | None = (
            100.0 * rank / n_masses if n_masses > 0 else None
        )
        breakdown = ScoreBreakdown(
            bm25_score=float(bm25_score),
            bm25_contributed=True,
            dormant_percentile=percentile,
            node_mass=float(state.mass),
        )
        if cfg.expose_reason:
            reason = explain_score(
                breakdown,
                mass_dominance_threshold=cfg.reason_dominance_mass_threshold,
                bm25_strong_threshold=cfg.reason_bm25_strong_threshold,
            )
            if reason is not None:
                breakdown = breakdown.model_copy(update={"reason": reason})
        out.append(AmbientMemory(
            id=doc_id,
            content=_excerpt(doc.get("content") or "", excerpt_chars),
            source=meta.get("source", "unknown"),
            tags=list(meta.get("tags") or []),
            certainty=float(state.certainty) if state.certainty is not None else None,
            age_days=max(0.0, (now - state.last_access) / 86400.0),
            virtual_score=0.0,
            final_score=0.0,
            breakdown=breakdown if expose_breakdown else None,
        ))
    return out


def _to_ambient_memory(
    engine: GaOTTTEngine, item: MemoryItem, now: float, *,
    excerpt_chars: int, lensing_gap: float | None = None,
    expose_breakdown: bool = False,
) -> AmbientMemory:
    """``MemoryItem`` → ``AmbientMemory``, enriched with provenance metadata (③).

    ``certainty`` / ``age_days`` come from the node's ``NodeState`` — the recall
    that produced ``item`` just touched these nodes, so they are warm in cache.

    Refinement Stage 3: when ``expose_breakdown`` is True the recall's
    ``score_breakdown`` is attached, letting the caller see why this memory
    surfaced (raw vs virtual cosine, BM25 contribution, mass boost, ...).
    """
    state = engine.cache.get_node(item.id)
    certainty = float(state.certainty) if state is not None else None
    age_days = (
        max(0.0, (now - state.last_access) / 86400.0)
        if state is not None else None
    )
    return AmbientMemory(
        id=item.id,
        content=_excerpt(item.content, excerpt_chars),
        source=item.source,
        tags=list(item.tags),
        certainty=certainty,
        age_days=age_days,
        virtual_score=item.raw_score,
        final_score=item.final_score,
        lensing_gap=lensing_gap,
        breakdown=item.score_breakdown if expose_breakdown else None,
    )


def _lensing_resonance(
    lensing_id: str,
    direct_ids: list[str],
    engine: GaOTTTEngine,
    scale: float,
) -> float:
    """Lateral Association Stage 5 — cooccurrence-derived trust signal.

    Mode 5a: ``resonance = raw / (raw + scale)`` where
    ``raw = sum_{d in direct_ids} assoc(lensing_id, d)``.

    Measures "how often has the field pulled this lensing memo together with
    today's direct hits in past active recalls" — the cooccurrence graph
    captures associations built from real user recalls (passive recalls do
    not write cooccurrence, so the signal is uncontaminated by ambient
    background noise).

    Stage 8 (2026-06-02): ``assoc`` is the degree-normalized association
    strength (``cache.get_association_strength``) when
    ``config.cooccurrence_assoc_normalization != "none"``, otherwise the raw
    co-recall count (legacy, bit-exact). Normalization fixes the hub
    pathology — a promiscuous lensing pick that co-occurs with *everything*
    has high ``deg`` so its association to today's direct hits is divided
    down, dropping its resonance: the trust signal stops trusting hubs.
    **When enabling normalization, retune ``ambient_lensing_resonance_scale``**
    — normalized strengths live in a far smaller range than raw counts, so
    the count-tuned ``scale=10`` would crush every resonance toward 0.

    Saturating non-linearity bounds the output to ``[0, 1)`` regardless of
    the input scale. ``scale=10`` (default) hits 0.5 at raw=10 and 0.9 at
    raw=90 *in the raw-count regime*. ``scale=0`` short-circuits to 1.0 for
    any nonzero input (max-trust mode, not recommended).
    """
    cfg = engine.config
    mode = cfg.cooccurrence_assoc_normalization
    hub_cut = cfg.cooccurrence_hub_degree_percentile_cut
    # Synaptic Pruning: age the co-recall weights by half-life when enabled
    # (None ⇒ no decay). A stale one-off clique contributes less trust than a
    # repeatedly-reinforced organic association.
    decay_hl = (
        cfg.synaptic_pruning_half_life_seconds
        if cfg.synaptic_pruning_enabled else None
    )
    # mode="none" + hub_cut=None + no decay returns a copy of get_neighbors → legacy.
    neighbors = engine.cache.get_association_strength(
        lensing_id, mode=mode, hub_degree_cut=hub_cut, decay_half_life=decay_hl,
    )
    if scale <= 0.0:
        # Degenerate config — treat as "any cooccurrence is fully trustworthy".
        return 1.0 if any(neighbors.get(d, 0.0) > 0.0 for d in direct_ids) else 0.0
    raw = sum(float(neighbors.get(d, 0.0)) for d in direct_ids)
    if raw <= 0.0:
        return 0.0
    return raw / (raw + scale)


def _cluster_key_for(cache) -> Callable[[str], str | None]:
    """Stage 7.1 cluster identity = ``cohort_id`` OR ``original_id``.

    Two structural identifiers Phase M already maintains:
      - ``cohort_id`` (Phase K supernova batch — set when index_documents
        batch size ≥ supernova_min_cohort_size)
      - ``original_id`` (Phase M — defaults to ``file_path`` for chunked
        ingests, otherwise the node's own id for singletons)

    Production observation (2026-05-26, 26k corpus): cohort_id coverage is
    effectively 0% because most ingests happen one memo at a time via
    ``remember()`` (batch=1), but ``original_id`` covers 57.8% of active
    memos in multi-member clusters (largest = 638-chunk book). Falling
    back from cohort_id → original_id gives anti-hub coverage of the
    real cluster failure mode (book chunks crowding top-K).

    Singletons (no batch + no file_path) get ``original_id = doc_id`` —
    a unique value per memo, so they form length-1 clusters and never
    accumulate penalty against each other (correct: singletons are
    intrinsically diverse).

    Phase M 単一規則整合: both identifiers are structural (no source / tag
    branching). The fallback chain is a routing of *which* structural id
    we use, not a physics rule branch.
    """
    def _key(node_id: str) -> str | None:
        return cache.get_cohort(node_id) or cache.get_original(node_id)
    return _key


def _apply_cluster_anti_hub(
    items: list[MemoryItem],
    cluster_key_of: Callable[[str], str | None],
    lambda_val: float,
    target_k: int,
    *,
    score_map: dict[str, float] | None = None,
) -> list[MemoryItem]:
    """Lateral Association Stage 7.1 — direct-hit anti-hub.

    Greedy MMR-style reordering: for each subsequent slot, prefer candidates
    that don't share a cluster key (``cohort_id`` OR ``original_id``, see
    ``_cluster_key_for``) with already-picked items. Penalty per repeat:
    ``lambda_val × count_of_shared_cluster_in_selected``.

    ``cluster_key is None`` (no cohort + no original_id — typically
    pre-Phase-M memos) gets no penalty — they're treated as intrinsically
    diverse so they don't pile up against each other.

    ``score_map`` lets the caller pass already-decayed scores keyed by node
    id (e.g. novelty-decay output) so MMR ranks against the SAME score the
    upstream slot-pick was about to use. When absent, ``item.final_score``
    is used directly.

    Does NOT mutate ``items``' ``final_score`` — this is reordering only.
    The breakdown view downstream still sees the engine's original score so
    "why this surfaced" stays interpretable.

    No-op when ``lambda_val <= 0`` or fewer than 2 items.
    """
    if lambda_val <= 0.0 or len(items) <= 1:
        return list(items[:target_k])

    def _score_of(it: MemoryItem) -> float:
        if score_map is not None and it.id in score_map:
            return score_map[it.id]
        return it.final_score

    remaining = list(items)
    selected: list[MemoryItem] = []
    cluster_counts: dict[str, int] = {}
    while remaining and len(selected) < target_k:
        best_idx = 0
        best_adjusted = float("-inf")
        for idx, cand in enumerate(remaining):
            key = cluster_key_of(cand.id)
            penalty = (
                lambda_val * cluster_counts.get(key, 0)
                if key is not None else 0.0
            )
            adjusted = _score_of(cand) - penalty
            if adjusted > best_adjusted:
                best_adjusted = adjusted
                best_idx = idx
        pick = remaining.pop(best_idx)
        selected.append(pick)
        key = cluster_key_of(pick.id)
        if key is not None:
            cluster_counts[key] = cluster_counts.get(key, 0) + 1
    return selected


def _novelty_factor(
    node_id: str,
    recently_surfaced: dict[str, int] | None,
    decay: float,
) -> float:
    """Lateral Association Stage 1 — multiplicative session-novelty factor.

    ``decay ** count`` for nodes the caller has seen on recent ambient turns;
    ``1.0`` (no-op) for everything else, when ``recently_surfaced`` is unset,
    or when ``decay >= 1.0`` (rollback). The exponent ``count`` is the number
    of times this id appeared in the past N turns the hook scanned.

    Lives in this module (not on the engine) because it is a slot-pick
    concern, not a gravity-field property: ranking is bent here without
    perturbing any persistent state (passive principle).
    """
    if not recently_surfaced or decay >= 1.0:
        return 1.0
    count = recently_surfaced.get(node_id, 0)
    if count <= 0:
        return 1.0
    return float(decay) ** int(count)


def _conversational_source_factor(source: str, cfg: Any) -> float:
    """Observation Apparatus Round 2 Stage E1 — source damping for ambient
    slots. Returns ``ambient_conversational_source_factor`` when source is in
    ``ambient_conversational_sources``, else ``1.0``. Returns ``1.0`` early
    when factor >= 1.0 (OFF / rollback path)."""
    factor = cfg.ambient_conversational_source_factor
    if factor >= 1.0:
        return 1.0
    if source in cfg.ambient_conversational_sources:
        return factor
    return 1.0


# E2 — dump-shape symbol ratio. Characters that are NOT Japanese (hiragana,
# katakana, kanji), NOT ASCII letters, and NOT whitespace are counted as
# "symbols" (digits, punctuation, brackets, underscores, operators, etc.).
_NON_SYMBOL_RE = re.compile(
    r"[^\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\u3400-\u4DBF"
    r"a-zA-Z\s]",
)


def _dump_symbol_ratio(text: str, head: int = 400) -> float:
    """Observation Apparatus Round 2 Stage E2 — "dump score" of the first
    ``head`` characters: ``max(symbol ratio, long-ASCII-token ratio)``.

    Symbol ratio alone misses code / state-dict dumps because identifiers
    are letter-heavy (``residual_layer.0.fn.weight`` is ~80% letters). The
    second signal — the fraction of characters living in whitespace-split
    tokens that are ≥ 20 chars and ≥ 80% ASCII — catches exactly those
    machine-shaped tokens, while Japanese prose (no whitespace tokens) and
    natural English (short words) stay near 0. Returns ``0.0`` for empty
    text."""
    chunk = text[:head]
    if not chunk:
        return 0.0
    total = len(chunk)
    symbols = len(_NON_SYMBOL_RE.findall(chunk))
    long_ascii = 0
    for tok in chunk.split():
        if len(tok) >= 20 and sum(c.isascii() for c in tok) >= 0.8 * len(tok):
            long_ascii += len(tok)
    return max(symbols / total, long_ascii / total)


def _pick_lensing(
    engine: GaOTTTEngine, items: list[MemoryItem], exclude: set[str],
    recently_surfaced: dict[str, int] | None = None,
) -> list[tuple[MemoryItem, float]]:
    """② Gravitational lensing — the memories the field has bent onto the
    query's path. Memories textually far from the query (low ``raw_cosine``)
    that Phase I/J displacement has pulled near it (high ``virtual_cosine``)
    — associations the field *learned*, not ones the embedder sees.

    Gated by ``ambient_lensing_min_score`` (the pick must still be virtually
    relevant, not pure noise) and ``ambient_lensing_min_gap`` (the bend must
    be meaningful). Returns up to ``ambient_lensing_max_k`` ``(item, raw_gap)``
    tuples, ranked by decayed gap descending. Empty list = nothing cleared
    the gates.

    Lateral Association Stage 1 — when ``recently_surfaced`` is set, the
    ranking is against ``gap × novelty_factor(id)`` so a recently-surfaced
    lensing memo rotates out unless its bend dwarfs the decay. The reported
    gap is the *raw* gap (not decayed) so caller-visible numbers retain
    their physical meaning.

    Lateral Association Stage 3 (2026-05-25) — returns top-K instead of
    top-1. K = ``config.ambient_lensing_max_k`` (default 2). Each kept pick
    must independently clear both gates (no quota relaxation — the second
    best is only surfaced if it's still genuinely a bent association).
    """
    cfg = engine.config
    if not cfg.ambient_lensing_enabled:
        return []
    max_k = max(1, int(cfg.ambient_lensing_max_k))
    # (item, raw_gap, decayed_gap)
    ranked: list[tuple[MemoryItem, float, float]] = []
    for item in items:
        if item.id in exclude:
            continue
        b = item.score_breakdown
        if b is None:
            continue  # score breakdown disabled → cannot measure the bend
        if b.virtual_cosine < cfg.ambient_lensing_min_score:
            continue
        gap = b.virtual_cosine - b.raw_cosine
        if gap < cfg.ambient_lensing_min_gap:
            continue
        novelty = _novelty_factor(
            item.id, recently_surfaced, cfg.ambient_novelty_decay,
        )
        src_f = _conversational_source_factor(item.source, cfg)
        ranked.append((item, gap, gap * novelty * src_f))
    ranked.sort(key=lambda t: t[2], reverse=True)
    return [(it, raw_gap) for it, raw_gap, _ in ranked[:max_k]]


_REASON_EDGE_TYPES = ("derived_from", "supersedes")


async def _excerpt_of(
    engine: GaOTTTEngine, node_id: str, limit: int,
) -> str | None:
    """Fetch a node's content and return a compact excerpt (or None)."""
    doc = await engine.store.get_document(node_id)
    if not doc:
        return None
    return _excerpt(doc.get("content", ""), limit) or None


async def _attach_reasoning_and_tension(
    engine: GaOTTTEngine, memories: list[AmbientMemory], excerpt_chars: int,
) -> list[AmbientTension]:
    """Stage 2 — for each surfaced memory, attach the ``derived_from`` /
    ``supersedes`` parent excerpt as ``because`` (④, mutated in place), and
    collect ``contradicts``-edge pairs as tension cautions (⑤).

    Typed-edge lookups hit the indexed ``directed_edges`` table — a handful of
    small queries per ambient call, well within the ~0.5s budget.
    """
    cfg = engine.config
    tensions: list[AmbientTension] = []
    seen: set[tuple[str, str]] = set()
    for m in memories:
        out_edges = await engine.get_relations(m.id, direction="out")
        if cfg.ambient_reasoning_enabled and m.because is None:
            for e in out_edges:
                if e.edge_type in _REASON_EDGE_TYPES:
                    parent = await _excerpt_of(engine, e.dst, excerpt_chars)
                    if parent:
                        m.because = parent
                        break
        if cfg.ambient_tension_enabled:
            in_edges = await engine.get_relations(
                m.id, edge_type="contradicts", direction="in",
            )
            contradicts = [e for e in out_edges if e.edge_type == "contradicts"]
            for e in contradicts + in_edges:
                other = e.dst if e.src == m.id else e.src
                key = (m.id, other) if m.id < other else (other, m.id)
                if key in seen:
                    continue
                seen.add(key)
                other_excerpt = await _excerpt_of(engine, other, excerpt_chars)
                if other_excerpt:
                    tensions.append(AmbientTension(
                        memory_id=m.id,
                        memory_excerpt=m.content,
                        contradicts_id=other,
                        contradicts_excerpt=other_excerpt,
                    ))
    return tensions


async def _pick_persona(
    engine: GaOTTTEngine, query: str, excerpt_chars: int,
    excluded_ids: set[str] | None = None,
    expose_breakdown: bool = False,
    recently_surfaced: dict[str, int] | None = None,
) -> AmbientPersona | None:
    """⑥ Stage 3 + Refinement Stage 1 — query-conditioned persona pick.

    Reuses Phase J's ``collect_active_persona_ids``; commitments (task-shaped)
    are excluded — only value/intention ground "who I am working as".

    Refinement Stage 1 (2026-05-25, Plans-Ambient-Recall-Refinement.md):
    candidate value/intention nodes are re-ranked by
    ``(mass ** w) × cosine(query, node)`` — Phase J's persona-anchored
    geometry applied to slot selection — so the surfaced line is relevant
    to the current turn instead of an arbitrary mass/recency winner. The
    exponent ``w = config.ambient_persona_mass_weight`` (default ``0.3``,
    promoted 2026-07-02 to dampen Heavy Persona Dominance) lets ops swing
    between mass-dominated ranking and pure-cosine ranking. Returns ``None``
    when the best relevance is below
    ``config.ambient_persona_min_relevance`` (irrelevant persona is a worse
    context than no persona — literal failure observed during Phase A
    embedder comparison: an MCP-smoke ``intention`` surfaced in an
    embedder-discussion turn).
    """
    cfg = engine.config
    if not cfg.ambient_persona_enabled:
        return None
    ids = collect_active_persona_ids(engine.cache, cfg, time.time())
    candidates = [
        nid for nid in ids
        if engine.cache.source_by_id.get(nid) in ("value", "intention")
        and (excluded_ids is None or nid not in excluded_ids)
    ]
    if not candidates:
        return None

    # Pre-rank by mass to bound the candidate pool — encode + cosine is
    # cheap, but FAISS get_vectors and the per-candidate dot grow linearly
    # in pool size. ``ambient_persona_pool_size`` keeps it bounded.
    sorted_candidates: list[tuple[str, float]] = []
    for nid in candidates:
        state = engine.cache.get_node(nid)
        m = state.mass if state is not None else 1.0
        sorted_candidates.append((nid, m))
    sorted_candidates.sort(key=lambda t: t[1], reverse=True)
    pool = sorted_candidates[: cfg.ambient_persona_pool_size]
    pool_ids = [nid for nid, _ in pool]

    # One extra query embedding per ambient_recall when persona is on
    # (~30-50ms on RURI-310m; ~0ms on test stubs). Cosine is a dot product
    # because both vectors are L2-normalized.
    query_vec = engine.embedder.encode_query(query).reshape(-1)
    vecs = engine.faiss_index.get_vectors(pool_ids)

    # Refinement follow-up (b) — mass weight knob. ``weight=0.3`` (default,
    # promoted 2026-07-02) dampens a runaway-mass persona so cosine can still
    # decide; ``weight=1.0`` reproduces the Stage-1 ``mass × cos`` formula
    # exactly (rollback path); ``weight=0.0`` ranks purely by cosine (mass
    # ignored, the "relevance_dominant" mode). See
    # ``ambient_persona_mass_weight`` in config.py for context.
    # Lateral Association Stage 1 — additional ``× novelty`` factor so a
    # persona repeated across recent turns rotates out of slot.
    weight = cfg.ambient_persona_mass_weight
    best_id: str | None = None
    best_cos = 0.0
    best_score = float("-inf")
    for nid, mass in pool:
        v = vecs.get(nid)
        if v is None:
            continue
        cos = float(np.dot(query_vec, v))
        # ``max(mass, 0)`` guards the fractional-power branch from negative
        # mass (impossible in practice; defensive only). ``weight == 0``
        # collapses to ``mass ** 0 == 1``, yielding pure-cos ranking.
        mass_term = float(max(mass, 0.0)) ** weight if weight != 1.0 else float(mass)
        novelty = _novelty_factor(
            nid, recently_surfaced, cfg.ambient_novelty_decay,
        )
        score = mass_term * cos * novelty
        if score > best_score:
            best_score = score
            best_id = nid
            best_cos = cos

    if best_id is None:
        return None
    if best_cos < cfg.ambient_persona_min_relevance:
        # Onboarding period: when the persona corpus is too small / too
        # off-topic, an irrelevant pick is worse than no slot. The hook
        # simply omits the "▼ いま誰として" line for this turn.
        return None

    content = await _excerpt_of(engine, best_id, excerpt_chars)
    if not content:
        return None
    # Refinement Stage 3 — minimal breakdown: only the inputs the persona
    # pick actually computes (mass + raw cosine to query) are populated.
    persona_breakdown: ScoreBreakdown | None = None
    if expose_breakdown:
        best_state = engine.cache.get_node(best_id)
        best_mass = best_state.mass if best_state is not None else 1.0
        persona_breakdown = ScoreBreakdown(
            raw_cosine=best_cos,
            mass_boost=float(best_mass),
        )
    return AmbientPersona(
        id=best_id,
        kind=engine.cache.source_by_id.get(best_id, "value"),
        content=content,
        breakdown=persona_breakdown,
    )


def _bm25_gate_top(engine: GaOTTTEngine, query: str) -> float | None:
    """Top word-BM25 gate score for ``query`` — the numeric input behind
    :func:`_bm25_gate`. ``None`` when the gate is unusable (disabled /
    index absent / empty corpus); ``0.0`` when the index answered but
    nothing matched. Phase T Stage 5: ambient_recall derives both the gate
    verdict and ``AmbientGateDiagnostics.bm25_top_score`` from this single
    search instead of running it twice.
    """
    cfg = engine.config
    if not cfg.ambient_gate_use_bm25:
        return None
    idx = engine.ambient_gate_index
    if idx is None or idx.size == 0:
        return None
    hits = idx.search(query, 1)
    return hits[0][1] if hits else 0.0


def _bm25_gate(engine: GaOTTTEngine, query: str) -> bool | None:
    """Word-level BM25 strong-match gate — the primary ambient-recall gate.

    Scores the prompt against a dedicated word-tokenised (Sudachi) BM25 index
    over the corpus. A prompt that *strongly* matches stored content clears
    ``ambient_bm25_min_score``; weak / off-topic prompts stay below it. This
    is what dense-cosine ``virtual_score`` and char-trigram BM25 both failed
    to do — see the 4-round 2026-05-21 calibration in
    Plans-Ambient-Recall-Enrichment.md. Returns:

      ``True``  — top BM25 score clears the threshold → inject
      ``False``  — below threshold → suppress
      ``None``  — gate index unavailable (disabled / empty / bm25-sudachi
                  extra missing) → the caller falls back to virtual_score
    """
    top = _bm25_gate_top(engine, query)
    if top is None:
        return None
    return top >= engine.config.ambient_bm25_min_score


# --- Phase U WP-3 — ambient composite gate (runtime glue) ----------------------
# 純関数 (判定・percentile・digest・artifact loader) は services.ambient_composite。
# ここは engine を扱う runtime 部分: raw FAISS 軸の自前計算と、参照 artifact の
# lazy load + fingerprint 検証 + per-engine cache。

@dataclass
class _CompositeValidatedState:
    """参照 artifact の検証済み snapshot (engine attribute に cache)。

    一度検証を通ったら同一 engine 内では再 load しない (plan 契約:
    "lazily load + validate ... once per engine")。per-call の staleness
    check は ``reference_count`` に対する active count drift のみ (cheap)。
    """

    reference: list[float]
    reference_count: int
    corpus_digest: str


_COMPOSITE_STATE_ATTR = "_ambient_composite_state"


def _composite_raw_top1(engine: GaOTTTEngine, query: str) -> float | None:
    """Breakdown 非依存の raw cosine 軸 — engine 自身の raw FAISS 検索。

    Phase T の「raw 軸は breakdown populate 時のみ存在する」制約を回避する
    ため、composite gate は ``expose_score_breakdown`` とは無関係に自前で
    top-1 raw cosine を計算する (WP-3 契約)。空 index / 空ヒットは None
    (決定関数が reject する)。追加 1 回の ``encode_query`` は許容コスト
    (embedder 側 cache があればさらに安い)。
    """
    if engine.faiss_index.size == 0:
        return None
    vec = engine.embedder.encode_query(query)
    hits = engine.faiss_index.search(vec, 1)
    return hits[0][1] if hits else None


def _count_drift_exceeds(current: int, reference_count: int, max_drift: float) -> bool:
    if reference_count <= 0:
        return True
    return abs(current - reference_count) / reference_count > max_drift


async def _composite_reference_for(
    engine: GaOTTTEngine,
) -> tuple[list[float] | None, int | None, str | None]:
    """参照分布を返す — (values, reference_count, fail_detail)。

    fail_closed 契約 (Plans-Phase-U §4 WP-3): artifact 欠損・破損・
    fingerprint (embedder id/version, corpus digest) 不一致・count drift
    超過のいずれかで ``values=None`` を返し、呼び出し側は BM25 のみの
    accept 経路に落とす (empty_reason="composite_reference_unavailable")。
    "or" への open fallback はしない。検証失敗は cache しない — 修復後の
    再挑戦 (engine 再起動なし) を許すため。高コストな full digest scan は
    初回 composite 使用時のみで、その前に cheap check (load / identity /
    count drift) を済ませる。
    """
    cfg = engine.config
    state: _CompositeValidatedState | None = getattr(engine, _COMPOSITE_STATE_ATTR, None)
    if state is not None:
        if _count_drift_exceeds(
            len(engine.cache.node_cache), state.reference_count,
            cfg.ambient_composite_count_drift_max,
        ):
            return (
                None, state.reference_count,
                f"active count drift beyond "
                f"{cfg.ambient_composite_count_drift_max:.1%} of reference_count="
                f"{state.reference_count} (re-calibration required)",
            )
        return state.reference, state.reference_count, None

    path = Path(cfg.data_dir) / cfg.ambient_composite_reference_filename
    try:
        ref = load_composite_reference(path)
    except CompositeReferenceError as exc:
        return None, None, str(exc)

    # embedder identity — manifest が一次情報 (build_engine が検証に使うのと
    # 同一規約)。manifest 無しの直接構成 engine (tests 等) は embedder の
    # property に fallback。両方無ければ検証不能 → fail-closed。
    manifest = load_manifest(Path(cfg.data_dir))
    if manifest is not None:
        emb_id: str | None = manifest.embedder_id
        emb_ver: str | None = manifest.embedder_version
    else:
        emb_id = getattr(engine.embedder, "embedder_id", None)
        emb_ver = getattr(engine.embedder, "embedder_version", None)
    if not emb_id or emb_id != ref.embedder_id:
        return None, ref.active_count, (
            f"embedder_id mismatch: artifact={ref.embedder_id!r} runtime={emb_id!r}"
        )
    if emb_ver is not None and emb_ver != ref.embedder_version:
        return None, ref.active_count, (
            f"embedder_version mismatch: artifact={ref.embedder_version!r} "
            f"runtime={emb_ver!r}"
        )

    # cheap count-drift pre-check — corpus が既に drift 済みなら full digest
    # scan を無駄に払わない。
    active = len(engine.cache.node_cache)
    if _count_drift_exceeds(
        active, ref.active_count, cfg.ambient_composite_count_drift_max,
    ):
        return None, ref.active_count, (
            f"active count drift beyond "
            f"{cfg.ambient_composite_count_drift_max:.1%} of reference_count="
            f"{ref.active_count} (re-calibration required)"
        )

    # full corpus digest — active nodes の (id, sha256(content)) の sha256。
    contents = await engine.store.get_all_contents()
    digest, _active_with_content = compute_corpus_digest(
        contents, engine.cache.node_cache.keys(),
    )
    if digest != ref.corpus_digest:
        return None, ref.active_count, (
            "corpus digest mismatch — content changed since calibration "
            "(re-calibration required)"
        )

    state = _CompositeValidatedState(
        reference=list(ref.virt_top1_distribution),
        reference_count=ref.active_count,
        corpus_digest=digest,
    )
    setattr(engine, _COMPOSITE_STATE_ATTR, state)
    return state.reference, state.reference_count, None


async def _composite_gate_eval(
    engine: GaOTTTEngine,
    query: str,
    diag: AmbientGateDiagnostics,
    items: list[MemoryItem],
    virt_top1: float | None,
    bm25_top: float | None,
) -> CompositeVerdict:
    """ambient_recall の composite 分岐 (mode="composite" 専用)。

    axes は全て ``expose_score_breakdown`` 非依存: virtual 軸は
    ``item.raw_score`` (常に populate)、raw 軸は ``_composite_raw_top1``
    (自前 FAISS 検索)、参照分布は ``_composite_reference_for``。判定は
    gate decision (純関数) に委任し、結果の axes を diagnostics に書き戻す。
    """
    cfg = engine.config
    virt_median = (
        statistics.median([it.raw_score for it in items]) if items else None
    )
    raw_top1 = _composite_raw_top1(engine, query)
    reference, _ref_count, _detail = await _composite_reference_for(engine)
    verdict = composite_gate_decision(
        bm25_top=bm25_top,
        bm25_threshold=cfg.ambient_bm25_min_score,
        virt_top1=virt_top1,
        virt_median=virt_median,
        raw_top1=raw_top1,
        reference=reference,
        thresholds=CompositeGateThresholds(
            percentile_min=cfg.ambient_semantic_percentile_min,
            margin_min=cfg.ambient_margin_min,
            raw_floor=cfg.ambient_raw_floor_composite,
        ),
        pool_size=len(items),
    )
    diag.virt_percentile = verdict.virt_percentile
    diag.margin = verdict.margin
    diag.raw_top1 = verdict.raw_top1
    diag.composite_signal = verdict.signal
    return verdict


async def ambient_recall(
    engine: GaOTTTEngine,
    query: str,
    direct_k: int = 2,
    min_score: float | None = None,
    exclude_tags: list[str] | None = None,
    expose_breakdown: bool = False,
    recently_surfaced: dict[str, int] | None = None,
) -> AmbientRecallResponse:
    """Ambient Recall Enrichment — structured passive-recall injection.

    Composes a multi-slot block out of ONE passive recall:
      ① direct hits  — top ``direct_k`` by final_score
      ② lensing pick — largest virtual−raw gap (a field-learned association)
      ③ provenance   — source / certainty / age on every slot entry
    (Stage 2 adds ④ reasoning / ⑤ tension, Stage 3 adds ⑥ persona.)

    Relevance gate: if the best ``virtual_score`` among candidates is below
    ``min_score`` (default ``config.ambient_min_score``) the response is empty
    (``count == 0``) — ambient injection stays silent on off-topic prompts.

    Phase T Stage 5 — with ``config.ambient_gate_or_semantic`` a word-BM25
    reject no longer vetoes outright: the passive recall still runs and a
    semantic axis (best virtual_score ≥ ``min_score`` OR best raw cosine ≥
    ``config.ambient_semantic_raw_min``) can approve the injection. Every
    return carries ``gate_diagnostics`` (staged counts + gate inputs +
    discrete ``empty_reason``) for silence triage.

    ``passive=True`` throughout: ambient recall observes the gravity field
    without perturbing it (see Plans-Ambient-Recall-Enrichment.md).

    Refinement Stage 2 — ``exclude_tags`` substring-filters direct / lensing
    / persona candidates so test artifacts (``smoke-test`` etc.) stay out of
    ambient injection without being deleted from the corpus. See
    Plans-Ambient-Recall-Refinement.md.
    """
    cfg = engine.config
    threshold = cfg.ambient_min_score if min_score is None else min_score
    now = time.time()

    # Refinement Stage 2 — compute the exclusion set once. Substring
    # semantics mirror Phase J Stage 2's positive ``tag_filter``. Empty /
    # None ⇒ empty set ⇒ no-op.
    excluded_ids: set[str] = (
        engine.cache.find_ids_by_tag_filter(exclude_tags)
        if exclude_tags else set()
    )

    # Relevance gate — BM25 lexical (primary). Runs BEFORE the recall so an
    # off-topic prompt skips the recall cost entirely. `None` means BM25 is
    # unavailable → fall back to the virtual_score gate after the recall.
    # Phase T Stage 5 — OR gate. With ``ambient_gate_or_semantic`` a BM25
    # reject no longer vetoes outright: the recall still runs and a semantic
    # axis (best virtual_score ≥ min_score OR best raw cosine ≥
    # ``ambient_semantic_raw_min``) can approve the injection. Off-topic
    # suppression survives — BOTH axes must miss for the empty return.
    # Diagnostics (staged counts + gate inputs + empty_reason) ride on
    # every return, populated below.
    bm25_top = _bm25_gate_top(engine, query)
    bm25_ok = (
        None if bm25_top is None
        else bm25_top >= cfg.ambient_bm25_min_score
    )
    diag = AmbientGateDiagnostics(bm25_top_score=bm25_top, bm25_gate=bm25_ok)
    # Phase U WP-3 — mode="composite" は gate semantics を差し替える
    # (``ambient_gate_or_semantic`` より優先)。composite 判定は pool 統計を
    # 必要とするため BM25 reject による早期 veto は発動しない。
    composite_mode = cfg.ambient_gate_mode == "composite"
    if bm25_ok is False and not cfg.ambient_gate_or_semantic and not composite_mode:
        # Legacy veto — return before paying for the recall.
        diag.empty_reason = "bm25_veto"
        return AmbientRecallResponse(
            count=0, gate_diagnostics=diag, expose_breakdown=expose_breakdown,
        )

    # One passive recall — pool wide enough to host a lensing candidate.
    pool_k = max(direct_k * 5, 10)
    rr = await recall(
        engine, query, top_k=pool_k, passive=True, auto_route=False,
        multi_source=cfg.multi_source_ambient_enabled,
    )
    items = rr.items
    diag.candidates_generated = len(items)
    if excluded_ids:
        items = [it for it in items if it.id not in excluded_ids]
    diag.after_tag_exclusion = len(items)
    if not items:
        diag.empty_reason = (
            "no_candidates" if diag.candidates_generated == 0
            else "all_tag_excluded"
        )
        return AmbientRecallResponse(
            count=0, gate_diagnostics=diag, expose_breakdown=expose_breakdown,
        )
    # E2 — dump-shape gate. Drop items whose content is dominated by symbols
    # (state-dict keys, raw code). Applied before ranking so next-best
    # naturally fills. ``>= 1.0`` = OFF (bit-exact legacy).
    if cfg.ambient_dump_symbol_ratio < 1.0:
        ratio_threshold = cfg.ambient_dump_symbol_ratio
        items = [
            it for it in items
            if _dump_symbol_ratio(it.content) <= ratio_threshold
        ]
    diag.after_dump_filter = len(items)
    if not items:
        diag.empty_reason = "all_dump_filtered"
        return AmbientRecallResponse(
            count=0, gate_diagnostics=diag, expose_breakdown=expose_breakdown,
        )

    # Semantic-axis maxima over the surviving pool — diagnostics input AND
    # the OR-gate decision. ``raw_score`` is the virtual (gravity) score;
    # the raw-cosine axis exists only when breakdowns were populated
    # (absent axis → skipped, per contract).
    virt_max = max((it.raw_score for it in items), default=0.0)
    diag.semantic_max_virtual = virt_max
    raw_cosines = [
        it.score_breakdown.raw_cosine
        for it in items
        if it.score_breakdown is not None
    ]
    if raw_cosines:
        diag.semantic_max_raw = max(raw_cosines)
    diag.semantic_qualified = sum(
        1 for it in items
        if it.raw_score >= threshold
        or (
            it.score_breakdown is not None
            and it.score_breakdown.raw_cosine >= cfg.ambient_semantic_raw_min
        )
    )

    if composite_mode:
        # Phase U WP-3 — composite gate. accept = bm25_strong OR
        # (virt_percentile >= p_min AND margin >= m_min AND raw_top1 >=
        # raw_floor)。全軸が breakdown 非依存 (契約は
        # ``_composite_gate_eval`` docstring 参照)。reject は slot 構成の
        # 前に return — persona/lensing が direct reject を反転させない
        # 構造的 fence。
        verdict = await _composite_gate_eval(
            engine, query, diag, items, virt_max, bm25_top,
        )
        if not verdict.accepted:
            diag.empty_reason = verdict.reason
            return AmbientRecallResponse(
                count=0, gate_diagnostics=diag,
                expose_breakdown=expose_breakdown,
            )
    elif cfg.ambient_gate_or_semantic:
        # Stage 5 OR gate — a BM25 accept short-circuits (early accept,
        # legacy behaviour); a reject OR an unusable gate funnels into the
        # same semantic decision, unifying the legacy ``bm25_ok is None``
        # virtual fallback.
        if bm25_ok is not True:
            raw_ok = (
                diag.semantic_max_raw is not None
                and diag.semantic_max_raw >= cfg.ambient_semantic_raw_min
            )
            if not (virt_max >= threshold or raw_ok):
                diag.empty_reason = "bm25_and_semantic_below_threshold"
                return AmbientRecallResponse(
                    count=0, gate_diagnostics=diag,
                    expose_breakdown=expose_breakdown,
                )
    elif bm25_ok is None:
        # Legacy fallback — BM25 unavailable, virtual_score gate only (the
        # pool's max raw_score). Known weak on large corpora; BM25 is
        # preferred.
        if virt_max < threshold:
            diag.empty_reason = "bm25_and_semantic_below_threshold"
            return AmbientRecallResponse(
                count=0, gate_diagnostics=diag,
                expose_breakdown=expose_breakdown,
            )

    # Lateral Association Stage 1 — apply session novelty decay to direct
    # ranking *before* slicing top-K. ``items`` arrives sorted by
    # ``final_score`` (engine.query convention). When ``recently_surfaced``
    # is set, multiply each item's final_score by ``novelty_factor`` and
    # re-sort, so a memo seen on the last few turns rotates out unless its
    # margin survives the decay. No-op when ``recently_surfaced`` is empty
    # or ``ambient_novelty_decay >= 1.0`` (rollback path).
    # Observation Apparatus Round 2 E1 — source damping is folded into the
    # same combined multiplier: ``score = final_score × novelty × src_factor``.
    # Triggers re-rank when either novelty or source damping is active.
    score_map: dict[str, float] | None = None
    need_rerank = (
        (recently_surfaced and cfg.ambient_novelty_decay < 1.0)
        or cfg.ambient_conversational_source_factor < 1.0
    )
    if need_rerank:
        decayed: list[tuple[float, MemoryItem]] = []
        for it in items:
            nv = _novelty_factor(
                it.id, recently_surfaced, cfg.ambient_novelty_decay,
            )
            src_f = _conversational_source_factor(it.source, cfg)
            decayed.append((it.final_score * nv * src_f, it))
        decayed.sort(key=lambda t: t[0], reverse=True)
        items = [it for _, it in decayed]
        score_map = {it.id: s for s, it in decayed}

    # Lateral Association Stage 6.1 — direct-hit anti-hub. Greedy MMR on
    # ``cohort_id`` runs against the same score the upstream slot-pick
    # uses (novelty-decayed when Stage 1 fired, raw final_score otherwise).
    # ``lambda <= 0`` = behavior unchanged (default).
    if cfg.direct_hit_anti_hub_lambda > 0.0 and len(items) > 1:
        direct_items = _apply_cluster_anti_hub(
            items, _cluster_key_for(engine.cache),
            cfg.direct_hit_anti_hub_lambda, direct_k,
            score_map=score_map,
        )
    else:
        direct_items = items[:direct_k]
    direct_ids = {it.id for it in direct_items}
    direct = [
        _to_ambient_memory(
            engine, it, now, excerpt_chars=cfg.ambient_excerpt_chars,
            expose_breakdown=expose_breakdown,
        )
        for it in direct_items
    ]
    # Stage 3 — top-K lensing. ``picks`` is up to ``ambient_lensing_max_k``
    # tuples ranked by decayed gap; each kept ``lensing_gap`` is the raw
    # (pre-decay) gap so the caller sees physically meaningful numbers.
    picks = _pick_lensing(
        engine, items, exclude=direct_ids,
        recently_surfaced=recently_surfaced,
    )
    # Stage 5 — per-pick cooccurrence-derived resonance (trust signal).
    # Computed against today's direct picks. ``ambient_lensing_resonance_min``
    # > 0 drops low-resonance picks (no backfill — same no-quota-relaxation
    # principle as Stage 3's gap gate).
    direct_id_list = [it.id for it in direct_items]
    res_scale = cfg.ambient_lensing_resonance_scale
    res_min = cfg.ambient_lensing_resonance_min
    lensing: list[AmbientMemory] = []
    for l_item, l_gap in picks:
        resonance = _lensing_resonance(
            l_item.id, direct_id_list, engine, scale=res_scale,
        )
        if res_min > 0.0 and resonance < res_min:
            continue
        ambient_mem = _to_ambient_memory(
            engine, l_item, now,
            excerpt_chars=cfg.ambient_excerpt_chars, lensing_gap=l_gap,
            expose_breakdown=expose_breakdown,
        )
        ambient_mem.lensing_resonance = resonance
        lensing.append(ambient_mem)

    # Stage 2 — reasoning chain (④, mutates `because`) + tension flags (⑤).
    surfaced = direct + lensing
    tensions = await _attach_reasoning_and_tension(
        engine, surfaced, excerpt_chars=cfg.ambient_excerpt_chars,
    )

    # Stage 3 — persona grounding (⑥). Not counted toward `count`: it only
    # rides along an injection the relevance gate already approved.
    # Refinement Stage 1: the query is now used to re-rank persona candidates
    # by ``mass × cosine(query, node)`` (Phase J's persona-anchored geometry
    # applied to slot selection — see Plans-Ambient-Recall-Refinement.md).
    # Refinement Stage 2: ``excluded_ids`` also drops tagged persona nodes.
    persona = await _pick_persona(
        engine, query, cfg.ambient_excerpt_chars, excluded_ids=excluded_ids,
        expose_breakdown=expose_breakdown,
        recently_surfaced=recently_surfaced,
    )

    # Observation Apparatus Refinement Stage 2 — dormant whisper slot.
    # BM25-gated; runs only when the relevance gate already approved the
    # injection (no point whispering on off-topic prompts). Also excluded
    # are direct/lensing IDs to prevent duplicate surface in one block.
    # ``expose_breakdown`` is forwarded so the dormant slot follows the same
    # compact-vs-verbose contract as direct/lensing/persona.
    surfaced_ids = direct_ids | {m.id for m in lensing}
    dormant = await _dormant_for_ambient(
        engine, query,
        now=now,
        excerpt_chars=cfg.ambient_excerpt_chars,
        excluded_ids=excluded_ids | surfaced_ids,
        recently_surfaced=recently_surfaced,
        expose_breakdown=expose_breakdown,
    )

    # ``dormant`` rides along an injection the relevance gate already
    # approved (same as persona) — not counted toward ``count``, which the
    # caller reads as "how many primary memories surfaced".
    count = len(direct) + len(lensing)
    diag.direct_selected = len(direct)
    diag.lensing_selected = len(lensing)
    return AmbientRecallResponse(
        direct=direct, lensing=lensing, dormant=dormant, tensions=tensions,
        persona=persona, count=count,
        gate_diagnostics=diag, expose_breakdown=expose_breakdown,
    )


async def _dormant_surface(
    engine: GaOTTTEngine, top_k: int, diversity: float,
) -> ExploreResponse:
    """Phase O Stage 5 — random self-authored memo that the field has not
    pulled back in a long time.

    Bypasses the wave / FAISS entirely. The dormant condition is purely
    structural: age (last_access), mass (still below the mature gate), and
    source-class membership (the *kind* of memo I authored). No physics rule
    branches on the result — this is a different *operation* (counter-importance
    sampling), not a physics modifier.
    """
    cfg = engine.config
    cutoff = time.time() - cfg.dormant_age_threshold_seconds
    sources = set(cfg.dormant_source_classes)

    active_states = [s for s in engine.cache.get_all_nodes() if not s.is_archived]

    # Lateral Association Stage 6.2 — distribution-relative mass cut. When
    # ``dormant_mass_percentile`` is set, derive the cut from the active
    # corpus mass distribution; otherwise keep the legacy absolute floor.
    if cfg.dormant_mass_percentile is not None and active_states:
        sorted_masses = sorted(s.mass for s in active_states)
        p = max(0.0, min(100.0, float(cfg.dormant_mass_percentile)))
        pos = (p / 100.0) * (len(sorted_masses) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(sorted_masses) - 1)
        frac = pos - lo
        mass_cut = sorted_masses[lo] * (1 - frac) + sorted_masses[hi] * frac
    else:
        mass_cut = cfg.dormant_mass_threshold

    candidates: list[tuple[Any, dict[str, Any]]] = []
    for state in active_states:
        if state.last_access > cutoff:
            continue
        if state.mass > mass_cut:
            continue
        doc = await engine.store.get_document(state.id)
        if doc is None:
            continue
        meta = doc.get("metadata") or {}
        if meta.get("source", "") not in sources:
            continue
        candidates.append((state, doc))
    if not candidates:
        return ExploreResponse(items=[], count=0, diversity=diversity)
    # Random sample without replacement — the whole point is to surface a
    # *different* dormant memo each call (the field's saturation is
    # broken by counter-importance picking).
    picks = random.sample(candidates, k=min(top_k, len(candidates)))
    items: list[MemoryItem] = []
    for state, doc in picks:
        meta = doc.get("metadata") or {}
        items.append(MemoryItem(
            id=state.id,
            content=doc.get("content", ""),
            metadata=meta,
            raw_score=0.0,
            final_score=0.0,
            source=meta.get("source", "unknown"),
            tags=list(meta.get("tags") or []),
            displacement_norm=engine.get_displacement_norm(state.id),
            score_breakdown=None,
        ))
    return ExploreResponse(items=items, count=len(items), diversity=diversity)


async def explore(
    engine: GaOTTTEngine,
    query: str,
    diversity: float = 0.5,
    top_k: int = 10,
    persona_context: list[str] | None = None,
    tag_filter: list[str] | None = None,
    auto_route: bool = True,
    mode: str = "serendipity",
    passive: bool = False,
) -> ExploreResponse:
    """Phase O Stage 5 / Observation Apparatus Refinement Stage 3.

    ``passive=True`` (default False) routes through ``engine.query`` with
    ``passive=True``, mirroring ``recall(passive=True)``: no mass update, no
    displacement nudge, no co-occurrence write — read-only observation of the
    gravity field. Used by ``scripts/compare_retrieval.py`` so that running
    the diagnostic does not perturb the field it is observing.
    """
    if mode == "dormant":
        # Phase O Stage 5 — counter-importance sampling. Skips wave / routing
        # / training_delta entirely: this is a different operation, not a
        # query (no semantic intent to detect, no gradient step to record).
        # ``_dormant_surface`` is read-only by construction (no engine.query),
        # so ``passive`` is implied and ignored here.
        return await _dormant_surface(engine, top_k=top_k, diversity=diversity)

    config = engine.config
    # Hardening Stage 1 / C3 — do NOT monkey-patch the shared config.gamma.
    # engine.config is a single process-wide instance and engine.query
    # awaits (FAISS thread, store I/O); a concurrent recall/explore on the
    # shared event loop would read or "restore" the temporarily-inflated
    # gamma, permanently corrupting it. Pass the widened temperature scale
    # as a per-call gamma_override instead (thread-safe, like wave_depth/k).
    explore_gamma = config.gamma * (1.0 + diversity * 20.0)
    explore_depth = config.wave_max_depth + int(diversity * 2)
    explore_k = config.wave_initial_k + int(diversity * 4)

    # training_delta is meaningless when passive=True (no mutation to report).
    delta_out: dict | None = (
        {} if engine.config.training_delta_enabled and not passive else None
    )
    # Phase U WP-5 — wave propagation observability (explore header
    # ``wave: depth=… reached=…``). Populated unconditionally of passive
    # by the engine side channel; explore never hits the prefetch cache
    # (gamma_override below), so the stats are always fresh.
    wave_stats: dict = {}
    raw = await engine.query(
        text=query, top_k=top_k,
        wave_depth=explore_depth, wave_k=explore_k,
        persona_context=persona_context,
        tag_filter=tag_filter,
        out_training_delta=delta_out,
        gamma_override=explore_gamma,
        passive=passive,
        # Phase T Stage 6 — the presentation diversification runs inside
        # engine.query's Step 4 (service-layer post-processing would fire
        # presentation-derived updates on candidates the LLM never saw).
        # ``diversity <= 0`` or a disabled flag bypasses to the legacy
        # path inside the engine, so serendipity with diversity=0 is
        # plain recall plus the widened gamma/depth/k above.
        diversity=diversity,
        out_wave_stats=wave_stats,
    )

    items = [_to_memory_item(engine, r) for r in raw]
    routing_hint = await _build_routing_hint(engine, query, auto_route)
    return ExploreResponse(
        items=items, count=len(items), diversity=diversity,
        training_delta=_delta_from_dict(delta_out),
        routing_hint=routing_hint,
        wave_depth=wave_stats.get("depth"),
        wave_reached=wave_stats.get("reached"),
    )


async def forget(
    engine: GaOTTTEngine,
    node_ids: list[str],
    hard: bool = False,
) -> ForgetResponse:
    affected = await engine.forget(node_ids, hard=hard)
    return ForgetResponse(affected=affected, requested=len(node_ids), hard=hard)


async def restore(
    engine: GaOTTTEngine,
    node_ids: list[str],
) -> RestoreResponse:
    affected = await engine.restore(node_ids)
    return RestoreResponse(affected=affected, requested=len(node_ids))


async def revalidate(
    engine: GaOTTTEngine,
    node_id: str,
    certainty: float | None = None,
    emotion: float | None = None,
) -> RevalidateResponse:
    state = await engine.revalidate(node_id, certainty=certainty, emotion=emotion)
    if state is None:
        return RevalidateResponse(found=False, id=node_id)
    return RevalidateResponse(
        found=True,
        id=node_id,
        certainty=state.certainty,
        emotion_weight=state.emotion_weight,
    )


async def auto_remember(
    engine: GaOTTTEngine,
    transcript: str,
    max_candidates: int = 5,
    include_reasons: bool = True,
) -> AutoRememberResponse:
    cfg = engine.config
    if getattr(cfg, "auto_remember_strip_gaottt_blocks", True):
        transcript = _strip_gaottt_blocks(transcript)
    raw_candidates = extract_candidates(
        transcript,
        max_candidates=max_candidates,
        min_chars=cfg.auto_remember_min_chars,
        max_chars=cfg.auto_remember_max_chars,
    )
    candidates = [
        AutoRememberCandidate(
            content=c.content,
            score=c.score,
            suggested_source=c.suggested_source,
            suggested_tags=list(c.suggested_tags),
            reasons=list(c.reasons) if include_reasons else [],
        )
        for c in raw_candidates
    ]
    return AutoRememberResponse(candidates=candidates, count=len(candidates))


async def save_candidates(
    engine: GaOTTTEngine,
    transcript: str,
    max_candidates: int = 3,
    include_reasons: bool = True,
    include_persona: bool = True,
) -> SaveCandidatesResponse:
    """Stop-hook companion to ``ambient_recall`` (Plans-Save-Candidates-Hook.md).

    Reuses ``auto_remember`` for heuristic extraction and ``_pick_persona`` for
    the persona slot — both are existing physics-invariant primitives. The
    block this surfaces is *observation layer* only: it suggests what to save
    but never calls ``remember``. The agent's volitional act of articulating
    a save preserves the mass entry point that Articulation as Carrier
    (memory id 9a954c62) and the Phase M single-rule rely on.

    Returns ``count == 0`` when no candidate cleared the heuristic — the
    formatter then emits a sentinel and the hook stays silent.
    """
    auto_result = await auto_remember(
        engine,
        transcript=transcript,
        max_candidates=max_candidates,
        include_reasons=include_reasons,
    )
    persona: AmbientPersona | None = None
    if include_persona and engine.config.ambient_persona_enabled:
        # ``_pick_persona`` re-ranks active value/intention by
        # mass^w × cosine(transcript, node), so the persona slot reflects
        # who-I-am-as on the topic just discussed. Falls back to None when
        # no candidate clears ``ambient_persona_min_relevance`` — irrelevant
        # persona is worse than no persona (same lesson as ambient_recall).
        try:
            persona = await _pick_persona(
                engine,
                transcript,
                engine.config.ambient_excerpt_chars,
            )
        except Exception:
            # Persona pick is best-effort context; never block candidates.
            persona = None
    return SaveCandidatesResponse(
        candidates=list(auto_result.candidates),
        persona=persona,
        count=len(auto_result.candidates),
    )


async def get_node(engine: GaOTTTEngine, node_id: str) -> GetNodeResponse | None:
    """Observation Apparatus Round 2 Stage A — fetch-by-id, read-only.

    Retrieves a single node's content + provenance + physical state without
    touching mass, co-occurrence, displacement, or prefetch cache. Returns
    None for missing or archived nodes.
    """
    doc = await engine.store.get_document(node_id)
    if doc is None:
        return None
    state = engine.cache.get_node(node_id)
    if state is None or state.is_archived:
        return None
    meta = doc.get("metadata") or {}
    return GetNodeResponse(
        id=node_id,
        content=doc.get("content", ""),
        source=meta.get("source", ""),
        tags=meta.get("tags", []),
        metadata=meta,
        certainty=state.certainty,
        emotion=state.emotion_weight,
        mass=state.mass,
        temperature=state.temperature,
        last_access=state.last_access,
        displacement_norm=engine.get_displacement_norm(node_id),
    )
