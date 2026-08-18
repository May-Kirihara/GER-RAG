"""GaOTTT MCP Server — AI Agent Long-Term Memory (formerly GER-RAG)

Provides gravitational displacement-powered memory for AI agents.
Phase R4 will reframe the philosophy as "TTT framework that happens to look like RAG".

Usage:
    # stdio (legacy — every agent spawns its own subprocess and a full engine)
    python -m gaottt.server.mcp_server

    # streamable-HTTP (recommended — one long-lived process, N clients
    # connect over HTTP. Avoids the per-agent ×N RAM cost and the
    # bidirectional cache-overwrite trap)
    python -m gaottt.server.mcp_server --transport streamable-http --port 7878

    # SSE (older HTTP transport; supported but streamable-http is preferred)
    python -m gaottt.server.mcp_server --transport sse --port 7878

Clients (with --transport streamable-http) point at
``http://127.0.0.1:7878/mcp``. See Operations-Server-Setup.md for the
.mcp.json / opencode.json snippets and a systemd unit example.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from pathlib import Path

import numpy as np
from mcp.server.fastmcp import FastMCP

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.services import (
    formatters,
    ingest_service,
    maintenance as maintenance_service,
    memory as memory_service,
    phase_d as phase_d_service,
    reflection as reflection_service,
    relations as relations_service,
)
from gaottt.services.runtime import build_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Engine singleton ---

_engine: GaOTTTEngine | None = None
_engine_lock = asyncio.Lock()


async def get_engine() -> GaOTTTEngine:
    global _engine
    if _engine is not None:
        return _engine
    async with _engine_lock:
        if _engine is not None:
            return _engine
        config = GaOTTTConfig.from_config_file()
        logger.info("Initializing GaOTTT engine for MCP server...")
        engine = build_engine(config)
        await engine.startup()
        _engine = engine
        logger.info(
            "GaOTTT engine ready (%d nodes, %d vectors)",
            len(engine.cache.node_cache),
            engine.faiss_index.size,
        )
        return engine


# --- MCP Server ---

mcp = FastMCP(
    "gaottt",
    instructions=(
        "GaOTTT (formerly GER-RAG): Gravitational long-term memory for AI agents. "
        "Use 'remember' to store knowledge (source='hypothesis' or ttl_seconds "
        "for ephemeral, emotion/certainty for affective weighting), 'recall' to "
        "search with gravitational relevance (transparently consumes 'prefetch' "
        "cache; pass passive=true for a read-only recall that does not perturb "
        "the gravity field — for automatic/background use), 'ambient_recall' "
        "for a structured multi-slot context block (direct hits + "
        "gravitational-lensing pick + provenance — what the Claude Code hook "
        "injects each turn), 'prefetch' to "
        "pre-warm the gravity well around an anticipated "
        "query, 'prefetch_status' to inspect cache health, 'explore' for "
        "serendipitous discovery, 'reflect' to analyze memory state "
        "(aspect='duplicates' for collision candidates, 'relations' for "
        "typed-edge overview), 'auto_remember' to extract save candidates, "
        "'save_candidates' for the Stop-hook companion to ambient_recall "
        "(turn-end heuristic surface as a structured block — observation "
        "layer is automated, calling remember stays manual), "
        "'forget'/'restore' to prune (soft by default), 'merge' to collide "
        "near-duplicates, 'compact' for periodic maintenance, 'revalidate' to "
        "refresh certainty, 'get_node' to fetch a single node by ID "
        "(read-only, bypasses the gravity field entirely — use when "
        "reflect(hot_topics) showed an ID but recall cannot reach it), "
        "'relate'/'unrelate'/'get_relations' for typed "
        "directed edges (supersedes/derived_from/contradicts), Phase D persona "
        "& task layer ('commit'/'start'/'complete'/'abandon'/'depend' for "
        "tasks; 'declare_value'/'declare_intention'/'declare_commitment' for "
        "persona; 'inherit_persona' to wear past-self at session start; "
        "reflect aspects: tasks_todo/tasks_doing/tasks_completed/"
        "tasks_abandoned/commitments/values/intentions/relationships/persona), "
        "and 'ingest' to bulk-load files. "
        "Note: RURI embeddings are not cross-lingual — query in the same "
        "language as the target memories, and bridge a language gap with "
        "tag_filter. "
        "\nDecision tree: "
        "recall — explicit recall (training step, perturbs the field); "
        "recall(passive=true) — read-only recall (does not perturb; for "
        "automatic/repeated queries); "
        "ambient_recall — structured multi-slot block (for hooks; always "
        "passive); "
        "explore — serendipity / mode='dormant' to break fixation; "
        "prefetch — read-ahead warmup. "
        "remember=save, reflect=inspect state, Phase D=persona & task "
        "layer."
    ),
)


# -----------------------------------------------------------------------
# Tools
# -----------------------------------------------------------------------

@mcp.tool()
async def remember(
    content: str,
    source: str = "agent",
    tags: list[str] | None = None,
    context: str | None = None,
    ttl_seconds: float | None = None,
    emotion: float = 0.0,
    certainty: float = 1.0,
) -> str:
    """Store knowledge in long-term memory.

    Use this to save important insights, decisions, user preferences,
    or conversation summaries (compaction) for future recall.

    Args:
        content: Text to remember
        source: Origin — "agent" (your thoughts), "user" (user input),
                "system" (system info), "compaction" (context compression),
                "hypothesis" (ephemeral working memory; auto-expires)
        tags: Classification tags (optional)
        context: Brief description of when/why this was saved (optional)
        ttl_seconds: Override expiration in seconds. If omitted and
                source="hypothesis", uses default_hypothesis_ttl_seconds
                from config. Permanent for other sources unless set.
        emotion: Emotional weight in [-1.0, 1.0]. Negative = frustration/loss,
                positive = relief/success. Magnitude (not sign) boosts recall —
                both joyful successes and painful failures deserve to surface.
        certainty: Confidence in [0.0, 1.0]. Higher means recall ranks this
                memory higher; certainty decays over time unless re-verified
                via the `revalidate` tool.
    """
    engine = await get_engine()
    result = await memory_service.remember(
        engine, content=content, source=source, tags=tags, context=context,
        ttl_seconds=ttl_seconds, emotion=emotion, certainty=certainty,
    )
    return formatters.format_remember(result)


@mcp.tool()
async def revalidate(
    node_id: str,
    certainty: float | None = None,
    emotion: float | None = None,
) -> str:
    """Re-verify a memory: refresh its certainty timestamp and optionally adjust weights.

    Certainty decays over time (~30-day half life by default). Calling this
    on a memory you confirm is still true resets the decay clock and keeps
    the memory ranked highly in `recall`.

    Args:
        node_id: Memory ID to revalidate
        certainty: New certainty in [0.0, 1.0]. If omitted, the existing value
                is kept (timestamp is still refreshed).
        emotion: New emotion weight in [-1.0, 1.0]. If omitted, unchanged.
    """
    engine = await get_engine()
    result = await memory_service.revalidate(
        engine, node_id=node_id, certainty=certainty, emotion=emotion,
    )
    return formatters.format_revalidate(result)


@mcp.tool()
async def forget(
    node_ids: list[str],
    hard: bool = False,
) -> str:
    """Forget memories.

    By default this is a soft archive: nodes are excluded from recall,
    explore, and reflect, but remain in the store and can be restored
    with the same IDs. Pass hard=True to physically delete.

    Use this to prune dormant or no-longer-relevant memories. A typical
    flow is: reflect(aspect="dormant") → propose to user → forget(ids).

    Args:
        node_ids: Memory IDs to forget (returned by remember/recall)
        hard: If True, permanently delete from the store (default False)
    """
    engine = await get_engine()
    result = await memory_service.forget(engine, node_ids=node_ids, hard=hard)
    return formatters.format_forget(result)


@mcp.tool()
async def restore(node_ids: list[str]) -> str:
    """Restore previously archived memories back into active recall.

    Only works for soft-archived nodes (hard-deleted ones are gone).
    """
    engine = await get_engine()
    result = await memory_service.restore(engine, node_ids=node_ids)
    return formatters.format_restore(result)


@mcp.tool()
async def recall(
    query: str,
    top_k: int = 5,
    source_filter: list[str] | None = None,
    wave_depth: int | None = None,
    wave_k: int | None = None,
    force_refresh: bool = False,
    persona_context: list[str] | None = None,
    tag_filter: list[str] | None = None,
    output_mode: str = "full",
    auto_route: bool = True,
    mode: str = "detail",
    passive: bool = False,
) -> str:
    """Search long-term memory with gravitational wave propagation.

    Gravity waves propagate recursively through the knowledge space.
    High-mass memories attract more neighbors, creating wider gravitational fields.

    By default this transparently consumes any matching prefetch entry —
    if `prefetch(query, top_k)` was called recently, the result is returned
    instantly. Pass `force_refresh=True` to bypass the prefetch cache and
    re-run the full wave simulation.

    **Explicit pool injection** (`persona_context` /
    `tag_filter`): the seed step pulls FAISS top-K *and* unions in every
    node matching the caller's explicit filters, bypassing
    `source_filter` restrictions. Use this when you need to surface a
    memo whose embedding is far from the query — e.g., a tag-tied cohort
    in a different language or vocabulary from the query.

    **Embeddings are not cross-lingual.** RURI is a Japanese-specialized
    model: a query mostly retrieves memories written in the SAME language
    as the query — an English query surfaces English memories, a Japanese
    query surfaces Japanese ones; it does not bridge the two. Cosine stays
    in a narrow high band either way, so a language mismatch fails
    *silently* (no error, no low score). Write the query in the language
    of the memories you expect to find; when query and target differ in
    language, fall back to `tag_filter` / `source_filter` to inject the
    target explicitly.

    Args:
        query: Search query
        top_k: Number of results (default 5)
        source_filter: Restrictive — only ``metadata.source`` matches survive
                       the seed pool. Use for sparse class
                       carve-out like ``["agent"]``.
        wave_depth: Override recursion depth (default from config)
        wave_k: Override initial seed count (default from config)
        force_refresh: Bypass prefetch cache (default False)
        persona_context: Explicit list of declared value/intention/commitment
                         IDs. Overrides the auto-detect for proximity
                         calculation *and* additively injects these IDs into
                         the seed pool (explicit pool injection).
        tag_filter: Substring list (OR match) of ``metadata.tags`` entries.
                    Every matching node is additively injected into the seed
                    pool, even if it is distant in embedding space. Bypasses
                    ``source_filter`` (caller's explicit ask wins).
        output_mode: Controls how much content is returned per result.
                     "full" (default) — complete content, backward-compatible.
                     "compact" — content truncated at 300 chars; use when you
                     only need to scan/triage results before deciding which to
                     read in full. Saves significant tokens on large recalls.
                     "ids" — header line only (id, scores, tags), no content;
                     use when you only need to know which memories exist.
        auto_route: when True (default), the service
                    detects queries phrased as structured aspect questions
                    (e.g. "現在 active な commitment", "持っている value") and
                    runs the matching ``reflect`` aspect in parallel. The
                    summary is appended to the response so you do not have to
                    switch to ``reflect`` manually. Pass False to suppress for
                    this call (debugging, or you want pure free-form recall).
        mode: content economy.
              "detail" (default) — full content per result.
              "list" — content truncated to 80 chars (newline-stripped) so 20
              results fit in the budget one full result would consume. Pair
              with ``top_k=20, output_mode="full"`` for a scannable index;
              follow up with a targeted ``recall(...)`` on the id you care
              about for the full payload.
        passive: Ambient Recall — when True the search runs but the gravity
                 field is NOT perturbed: no mass update, no query-attraction
                 displacement, no co-occurrence edges. Use for automatic /
                 background recall (the Claude Code UserPromptSubmit hook
                 calls this) so ambient queries never become an uncontrolled
                 TTT signal. Default False keeps recall a training step.
    """
    engine = await get_engine()
    result = await memory_service.recall(
        engine, query=query, top_k=top_k, source_filter=source_filter,
        wave_depth=wave_depth, wave_k=wave_k, force_refresh=force_refresh,
        persona_context=persona_context, tag_filter=tag_filter,
        auto_route=auto_route, mode=mode, passive=passive,
    )
    verbose_modes = engine.config.recall_trailer_verbose_modes.split(",")
    verbose = (output_mode in verbose_modes) and (mode != "list")
    show_reason = output_mode in engine.config.recall_reason_line_modes.split(",")
    return formatters.format_recall(result, output_mode=output_mode, verbose=verbose, show_reason=show_reason)


@mcp.tool()
async def ambient_recall(
    query: str,
    direct_k: int = 2,
    min_score: float | None = None,
    exclude_tags: list[str] | None = None,
    expose_breakdown: bool = False,
    recently_surfaced: dict[str, int] | None = None,
) -> str:
    """Structured passive-recall injection — Ambient Recall Enrichment.

    Composes a multi-slot ``<gaottt-ambient-recall>`` block out of ONE passive
    (read-only, non-perturbing) recall:

      ▼ direct hits — top results by gravitational final_score
      ▼ gravitational lensing — a memory textually *far* from the query that
        the field's displacement has bent onto its path: an association the
        gravity field *learned*, which no plain embedding search would surface
      ▼ ⚠ contradiction — surfaced ``contradicts``-edge pairs
      ▼ persona — an active declared value/intention, for grounding

    Every entry carries provenance metadata (source · certainty · age) so the
    reader can weigh stale / low-certainty memories accordingly.

    This is what the Claude Code ``UserPromptSubmit`` hook calls every turn —
    it lets long-term memory surface *without* the model having to call
    ``recall`` explicitly. Always passive: it never perturbs the gravity
    field.

    Relevance gate: a word-level (Sudachi) BM25 "strong-match" gate decides
    whether to inject — only prompts that strongly match stored content fire.
    When nothing clears it the result is the sentinel ``(関連する記憶なし)``
    (no block), so ambient injection stays silent on off-topic / weak prompts.

    Args:
        query: The prompt / topic to pull ambient context for.
        direct_k: Number of direct-hit results (default 2).
        min_score: Threshold for the *fallback* virtual_score gate only — used
                   when the BM25 gate index is unavailable. The primary BM25
                   gate is tuned server-side (``config.ambient_bm25_min_score``).
        exclude_tags: Substrings; a memory whose tags contain ANY of them is
                   dropped from direct / lensing / persona candidates before
                   slot composition. Use to keep ``smoke-test`` and similar
                   test artifacts out of ambient injection without deleting
                   them from the corpus (production hook usually forwards
                   ``GAOTTT_AMBIENT_EXCLUDE_TAGS``).
        expose_breakdown: When True, append ``[raw=.. virt=.. bm25 mass=..]``
                   per slot row so the caller can see *why* each memory
                   surfaced (per-result ScoreBreakdown
                   at ambient granularity). Default off to preserve the
                   ambient block's token budget.
        recently_surfaced: Optional ``{node_id: count}`` map of memories
                   surfaced on recent ambient turns. Each slot's ranking score
                   is multiplied by ``config.ambient_novelty_decay ** count``
                   for matching ids, rotating recently-seen memos out of slot
                   1-2 turns (the "〇〇といえば〜
                   だったよな" controlled session-novelty channel). The
                   UserPromptSubmit hook builds this from past N turns of the
                   transcript; programmatic callers can pass {} or omit for
                   no decay (legacy behavior).
    """
    engine = await get_engine()
    result = await memory_service.ambient_recall(
        engine, query=query, direct_k=direct_k, min_score=min_score,
        exclude_tags=exclude_tags, expose_breakdown=expose_breakdown,
        recently_surfaced=recently_surfaced,
    )
    return formatters.format_ambient(result, config=engine.config)


@mcp.tool()
async def explore(
    query: str,
    diversity: float = 0.5,
    top_k: int = 10,
    persona_context: list[str] | None = None,
    tag_filter: list[str] | None = None,
    auto_route: bool = True,
    mode: str = "serendipity",
) -> str:
    """Explore memories serendipitously with increased randomness.

    Higher diversity increases wave depth and temperature noise,
    bringing unexpected cross-domain connections through deeper gravitational propagation.

    Parity with ``recall`` — explicit pool injection works
    here too. ``tag_filter`` forces the matched tagged memos into the result
    set even on a wide exploratory wave, useful for "explore within this
    intention's neighbourhood".

    Args:
        query: Starting point for exploration
        diversity: 0.0 = normal search, 1.0 = maximum exploration
        top_k: Number of results
        persona_context: Explicit persona ids (explicit pool injection semantics)
        tag_filter: Tag substring list (OR match) for additive injection
        auto_route: auto-attach a matching ``reflect``
                    summary when the query phrasing maps to a structured
                    aspect. Same semantics as ``recall.auto_route``.
        mode: exploration intent.
              "serendipity" (default) — diversity-amplified semantic explore.
              "dormant" — counter-importance sampling: returns random
              self-authored memos (agent/value/intention/commitment/note/
              reference) that are ≥ 7 days idle AND below the mass cut
              (percentile p10 by default). The wave is
              bypassed entirely; ``query`` is ignored. Use this when you
              suspect you have forgotten things worth pulling back; the field
              alone will not surface them (low mass + raw cosine alone never
              wins against a dense cluster).
    """
    engine = await get_engine()
    result = await memory_service.explore(
        engine, query=query, diversity=diversity, top_k=top_k,
        persona_context=persona_context, tag_filter=tag_filter,
        auto_route=auto_route, mode=mode,
    )
    return formatters.format_explore(result, mode=mode)


@mcp.tool()
async def reflect(
    aspect: str = "summary",
    limit: int = 10,
    bucket: str | None = None,
) -> str:
    """Analyze the state of your memory.

    Args:
        aspect: One of:
            - **summary** (overview)
            - **hot_topics** (high-mass memories)
            - **connections** (strong co-occurrence edges)
            - **dormant** (forgotten memories)
            - **duplicates** (near-duplicate clusters; pass to merge() to collide)
            - **relations** (directed typed edges)
            - Phase D — task layer:
              - **tasks_todo** (active tasks, deadline-sorted)
              - **tasks_doing** (recently `start()`-ed)
              - **tasks_completed** (completed-task chronology)
              - **tasks_abandoned** (shadow chronology)
              - **commitments** (active commitments, deadline-sorted)
            - Phase D — persona layer:
              - **values**, **intentions**, **relationships**
              - **persona** (composite self-introduction; same as `inherit_persona`)
        limit: Number of items to return
        bucket: (connections aspect only) Filter to one display bucket —
            "persona", "agent_user", or "ingest". None = all buckets
            (default). Ignored for every other aspect. Ignored when
            connections_grouped_by_source is False.
    """
    engine = await get_engine()
    return await _reflect_dispatch(engine, aspect, limit, bucket)


async def _reflect_dispatch(
    engine, aspect: str, limit: int, bucket: str | None = None,
) -> str:
    """Thin wrapper for backward-compat — delegates to the service dispatcher.

    The actual aspect → service-fn + formatter mapping lives in
    ``gaottt.services.reflection.dispatch_aspect`` so the recall auto-router
    can reuse the same table without re-importing the
    server layer.
    """
    return await reflection_service.dispatch_aspect(
        engine, aspect, limit=limit, bucket=bucket,
    )


@mcp.tool()
async def prefetch(
    query: str,
    top_k: int = 5,
    wave_depth: int | None = None,
    wave_k: int | None = None,
    persona_context: list[str] | None = None,
    tag_filter: list[str] | None = None,
) -> str:
    """Schedule a background recall to pre-load related memories.

    The astrocyte's true workload: while you reason in the foreground, this
    pre-warms the gravity well around `query` so a subsequent `recall` with
    the same arguments returns instantly. Useful at the start of a turn when
    you can predict what the user will probe next.

    Returns immediately; the actual work runs in a bounded background pool.
    Subsequent `recall(query, top_k)` calls within the cache TTL (default 90s)
    are served from the cache without re-running the wave simulation.

    ``persona_context`` / ``tag_filter`` are forwarded so
    the pre-warmed result matches what a subsequent ``recall`` with the
    same injection arguments would compute. Pre-fire the precise context
    you expect to recall.

    Args:
        query: search text to pre-warm
        top_k: number of results to cache (must match the eventual recall)
        wave_depth: optional override
        wave_k: optional override
        persona_context: Explicit persona ids (explicit pool injection semantics)
        tag_filter: Tag substring list (OR match) for additive injection
    """
    engine = await get_engine()
    result = maintenance_service.prefetch(
        engine, query=query, top_k=top_k, wave_depth=wave_depth, wave_k=wave_k,
        persona_context=persona_context, tag_filter=tag_filter,
    )
    return formatters.format_prefetch(result)


@mcp.tool()
async def prefetch_status() -> str:
    """Inspect the prefetch cache and async pool stats."""
    engine = await get_engine()
    result = maintenance_service.prefetch_status(engine)
    return formatters.format_prefetch_status(result)


@mcp.tool()
async def relate(
    src_id: str,
    dst_id: str,
    edge_type: str,
    weight: float = 1.0,
    metadata: dict | None = None,
) -> str:
    """Create a typed directed relation from src memory to dst memory.

    Reserved edge types:
      - "supersedes"   — src replaced/retracted dst (newer overrides older)
      - "derived_from" — src is an extension/derivation of dst
      - "contradicts"  — src disagrees with dst

    Custom edge_type strings are also allowed for experimentation.
    Used for "past-self dialogue" (Time-Delayed Echoes pattern): when you
    revise an earlier judgment, save the new conclusion and link it to the
    old one with edge_type="supersedes".

    Args:
        src_id: source memory ID (the newer / derived / contradicting one)
        dst_id: destination memory ID (the older / source / opposed one)
        edge_type: relation kind (see above)
        weight: optional strength of the relation (default 1.0)
        metadata: optional structured note (e.g. reason for retraction)
    """
    engine = await get_engine()
    result = await relations_service.relate(
        engine, src_id=src_id, dst_id=dst_id, edge_type=edge_type,
        weight=weight, metadata=metadata,
    )
    return formatters.format_relate(result)


@mcp.tool()
async def unrelate(
    src_id: str,
    dst_id: str,
    edge_type: str | None = None,
) -> str:
    """Remove a directed relation. If edge_type is omitted, removes all relations between the pair."""
    engine = await get_engine()
    result = await relations_service.unrelate(
        engine, src_id=src_id, dst_id=dst_id, edge_type=edge_type,
    )
    return formatters.format_unrelate(result)


@mcp.tool()
async def get_relations(
    node_id: str,
    edge_type: str | None = None,
    direction: str = "out",
) -> str:
    """List directed relations connected to a memory.

    Args:
        node_id: memory ID
        edge_type: filter by relation kind (optional)
        direction: "out" (relations from node), "in" (relations to node), or "both"
    """
    engine = await get_engine()
    result = await relations_service.get_relations(
        engine, node_id=node_id, edge_type=edge_type, direction=direction,
    )
    return formatters.format_relations(result)


# -----------------------------------------------------------------------
# Phase D — Persona & Task layer
# -----------------------------------------------------------------------

@mcp.tool()
async def commit(
    content: str,
    parent_id: str | None = None,
    deadline_seconds: float | None = None,
    certainty: float = 1.0,
) -> str:
    """Create a task. Tasks auto-expire (Hawking radiation) unless completed,
    abandoned, or revalidated.

    Optionally fulfills a parent commitment / intention via a `fulfills` edge,
    so you can trace a task back to "what is this for?".

    Args:
        content: Description of the task ("Fix the FAISS leak by Friday")
        parent_id: ID of a commitment or intention this task fulfills (optional)
        deadline_seconds: Override default 30-day TTL
        certainty: 1.0 = fully committed; lower for tentative tasks
    """
    engine = await get_engine()
    result = await phase_d_service.commit(
        engine, content=content, parent_id=parent_id,
        deadline_seconds=deadline_seconds, certainty=certainty,
    )
    return formatters.format_commit(result)


@mcp.tool()
async def start(task_id: str) -> str:
    """Mark a task as actively being worked on.

    Refreshes the task's certainty (resets the TTL decay clock) and bumps
    its emotion slightly positive — your attention is the energy.
    """
    engine = await get_engine()
    result = await phase_d_service.start(engine, task_id=task_id)
    return formatters.format_start(result)


@mcp.tool()
async def complete(
    task_id: str,
    outcome: str,
    emotion: float = 0.5,
) -> str:
    """Mark a task as completed.

    Saves the outcome as a new memory (`source="agent"`), draws a `completed`
    edge from outcome → task (so the task's gravity history records what it
    became), then archives the task so it stops surfacing in todo lists.

    Returns the outcome memory's ID. Use `recall` to find past completions.

    Args:
        task_id: Task to complete
        outcome: Free-form description of what got done / what was learned
        emotion: How it felt (default 0.5 = mild satisfaction). Failures that
                 got resolved deserve `emotion=0.7+` (relief is real).
    """
    engine = await get_engine()
    result = await phase_d_service.complete(
        engine, task_id=task_id, outcome=outcome, emotion=emotion,
    )
    return formatters.format_complete(result)


@mcp.tool()
async def abandon(task_id: str, reason: str) -> str:
    """Mark a task as deliberately abandoned (not failed, not forgotten — chosen).

    Saves the reason as a new memory and draws an `abandoned` edge from reason
    → task. Then archives the task. The pair persists as part of your "shadow
    chronology" — what you became by what you chose to release.

    Args:
        task_id: Task to abandon
        reason: Why you're letting this go ("priority dropped, will revisit Q3")
    """
    engine = await get_engine()
    result = await phase_d_service.abandon(
        engine, task_id=task_id, reason=reason,
    )
    return formatters.format_abandon(result)


@mcp.tool()
async def depend(
    task_id: str,
    depends_on_id: str,
    blocking: bool = False,
) -> str:
    """Declare that a task depends on another memory (typically another task).

    `blocking=True` uses the stronger `blocked_by` edge — the depender cannot
    progress until the blocker is resolved. Default `depends_on` is a softer
    "this comes after" relation.
    """
    engine = await get_engine()
    result = await phase_d_service.depend(
        engine, task_id=task_id, depends_on_id=depends_on_id, blocking=blocking,
    )
    return formatters.format_depend(result)


@mcp.tool()
async def declare_value(content: str, certainty: float = 1.0) -> str:
    """Declare a deeply-held belief / value. Permanent (no TTL).

    Values form the bedrock that intentions and commitments derive from.
    Use sparingly — these are the things that, if you forgot them, you'd
    no longer recognize yourself.
    """
    engine = await get_engine()
    result = await phase_d_service.declare_value(
        engine, content=content, certainty=certainty,
    )
    return formatters.format_declare_value(result)


@mcp.tool()
async def declare_intention(
    content: str,
    parent_value_id: str | None = None,
    certainty: float = 1.0,
) -> str:
    """Declare a long-term direction. Permanent unless explicitly revised.

    Intentions are larger than tasks but smaller than values. Optionally
    derived_from a value to show the "why" lineage.
    """
    engine = await get_engine()
    result = await phase_d_service.declare_intention(
        engine, content=content, parent_value_id=parent_value_id, certainty=certainty,
    )
    return formatters.format_declare_intention(result)


@mcp.tool()
async def declare_commitment(
    content: str,
    parent_intention_id: str,
    deadline_seconds: float | None = None,
    certainty: float = 1.0,
) -> str:
    """Declare a time-bounded commitment that fulfills an intention.

    Commitments auto-expire (default 14 days) unless revalidated. They
    represent active promises — the friction between intention and action.

    Args:
        content: What you're committing to
        parent_intention_id: REQUIRED — which intention does this serve?
        deadline_seconds: Override default 14-day TTL
        certainty: 1.0 = fully committed; lower for tentative
    """
    engine = await get_engine()
    result = await phase_d_service.declare_commitment(
        engine, content=content, parent_intention_id=parent_intention_id,
        deadline_seconds=deadline_seconds, certainty=certainty,
    )
    return formatters.format_declare_commitment(result)


@mcp.tool()
async def inherit_persona() -> str:
    """Generate a prose self-introduction from declared values, intentions,
    commitments, and recent activity.

    Call at session start to "wear" the persona accumulated across past
    sessions. Returns multi-paragraph text suitable for orienting a fresh
    Claude (or yourself, after a long break).
    """
    engine = await get_engine()
    result = await phase_d_service.inherit_persona(engine)
    return formatters.format_persona_snapshot(result)


@mcp.tool()
async def merge(
    node_ids: list[str],
    keep: str | None = None,
) -> str:
    """Gravitationally collide and merge memories into one survivor.

    Two or more nodes are absorbed into a single survivor: masses add (capped),
    velocities are momentum-weighted, displacements are mass-weighted, and
    co-occurrence edges are re-targeted. Absorbed nodes are soft-archived
    with merged_into pointing to the survivor — history is preserved.

    Use this after `reflect(aspect="duplicates")` surfaces near-duplicate
    clusters. Merging is irreversible; absorbed nodes can be restore()'d
    only as standalone (the merge is not unwound).

    Args:
        node_ids: 2 or more memory IDs to collide
        keep: Optional ID to force-survive. If omitted, the heaviest wins
              (ties broken by most recent access)
    """
    engine = await get_engine()
    result = await maintenance_service.merge(engine, node_ids=node_ids, keep=keep)
    return formatters.format_merge(result)


@mcp.tool()
async def compact(
    expire_ttl: bool = True,
    rebuild_faiss: bool = True,
    auto_merge: bool = False,
    merge_threshold: float = 0.95,
    merge_top_n: int = 500,
) -> str:
    """Periodic maintenance: expire TTL nodes, rebuild FAISS, optionally auto-merge.

    Run periodically (e.g. weekly) to:
      - mark TTL-expired nodes as archived (Hawking radiation)
      - drop archived/merged vectors from FAISS (system zero-point reset)
      - optionally collide near-duplicate clusters (gravitational merger)

    Auto-merge is irreversible and OFF by default. Enable explicitly when you
    want the physics to clean up duplicates without manual review.

    Args:
        expire_ttl: Run TTL expiration pass (default True)
        rebuild_faiss: Rebuild FAISS index dropping orphan vectors (default True)
        auto_merge: Run automatic collision-merge of duplicates (default False)
        merge_threshold: Cosine similarity threshold for auto-merge (default 0.95)
        merge_top_n: Limit auto-merge candidate pool to top-N by mass (default 500)
    """
    engine = await get_engine()
    result = await maintenance_service.compact(
        engine,
        expire_ttl=expire_ttl,
        rebuild_faiss=rebuild_faiss,
        auto_merge=auto_merge,
        merge_threshold=merge_threshold,
        merge_top_n=merge_top_n,
    )
    return formatters.format_compact(result)


@mcp.tool()
async def auto_remember(
    transcript: str,
    max_candidates: int = 5,
    include_reasons: bool = True,
) -> str:
    """Suggest memory candidates from a conversation transcript without saving.

    Heuristically extracts lines that look worth remembering (decisions,
    failures/successes, user preferences, lessons, metric-bearing notes).
    Does NOT save them — review the candidates and call `remember` for the
    ones you want to keep, optionally adjusting `source`, `tags`, or `content`.

    Args:
        transcript: Free-form text (typically the recent conversation segment)
        max_candidates: Max candidates to return (default 5)
        include_reasons: Include the heuristic reasons each line was picked
    """
    engine = await get_engine()
    result = await memory_service.auto_remember(
        engine, transcript=transcript,
        max_candidates=max_candidates, include_reasons=include_reasons,
    )
    return formatters.format_auto_remember(result)


@mcp.tool()
async def save_candidates(
    transcript: str,
    max_candidates: int = 3,
    include_reasons: bool = True,
    include_persona: bool = True,
) -> str:
    """Stop-hook companion to ``ambient_recall`` — write-side symmetric.

    Save filter (durable user preference, memory id ``93035d35``): save what
    *changes future decisions* — design rationale with WHY, retracted
    options, judgment-changing patterns, symmetric/structural insights,
    durable user constraints. Skip bug-existence itself (commit covers it),
    work-in-progress status (Phase D / TaskList covers it), and fact-only
    statements derivable from code. The heuristic over-surfaces
    "troubleshooting" tagged candidates; manual filter is expected.

    Returns the ``<gaottt-save-candidates>`` block (or the sentinel
    ``(保存候補なし)`` when nothing cleared the heuristic). Designed for the
    Stop / turn-end hook to call once per turn, with the resulting block
    injected into the *next* prompt — the model then decides whether to call
    ``remember`` on any candidate (observation layer is automated, the
    volitional mass-entry stays manual; see Observation vs Physics boundary).

    Reuses ``auto_remember`` for heuristic extraction and the ambient persona
    picker for the optional "▼ いま誰として" slot, so existing physics-invariant
    primitives back the surface.

    Args:
        transcript: Recent conversation segment (typically last 1-N turns).
        max_candidates: Max candidates to surface (default 3, ambient block
                        token-budget aware).
        include_reasons: Include the heuristic reasons each line was picked.
        include_persona: Include an active value/intention slot for grounding
                        (default on). Set False when persona context is
                        already injected by ambient_recall and you want to
                        avoid duplication.
    """
    engine = await get_engine()
    result = await memory_service.save_candidates(
        engine,
        transcript=transcript,
        max_candidates=max_candidates,
        include_reasons=include_reasons,
        include_persona=include_persona,
    )
    return formatters.format_save_candidates(result)


@mcp.tool()
async def ingest(
    path: str,
    source: str = "file",
    recursive: bool = False,
    pattern: str = "*.md,*.txt",
    chunk_size: int = 2000,
    include_tool_results: bool = False,
) -> str:
    """Bulk-load files or directories into memory.

    Supports Markdown (.md), plain text (.txt), CSV (.csv), and Claude Code
    transcript JSONL (.jsonl). For chat-history ingestion pass
    ``pattern="*.jsonl"`` and ``source="claude-code"``.

    Args:
        path: File or directory path
        source: Source label for metadata
        recursive: Recursively scan directories
        pattern: Glob patterns (comma-separated)
        chunk_size: Max characters per chunk for long documents
        include_tool_results: For .jsonl chat transcripts only — include
            raw tool stdout/stderr in the exchange body (default: off;
            usually noisy and inflates the DB).
    """
    engine = await get_engine()
    result = await ingest_service.ingest(
        engine, path=path, source=source, recursive=recursive,
        pattern=pattern, chunk_size=chunk_size,
        include_tool_results=include_tool_results,
    )
    return formatters.format_ingest(result)


@mcp.tool()
async def get_node(
    node_id: str,
) -> str:
    """Fetch a single node by ID — read-only, bypasses the gravity field.

    Retrieves content, provenance (source, tags, certainty, emotion), and
    physical state (mass, temperature, displacement) for a known node ID.
    This is the only retrieval path that **does not go through the seed
    pool** — it reads directly from the store and cache without perturbing
    mass, co-occurrence, displacement, or prefetch state. Use it when
    ``reflect(hot_topics)`` or another listing tool showed an ID but
    ``recall`` cannot surface it due to lexical/gravity capture by
    dominant neighbours.

    Returns "Node not found." for missing or archived IDs.
    """
    engine = await get_engine()
    result = await memory_service.get_node(engine, node_id)
    if result is None:
        return "Node not found."
    return formatters.format_node_detail(result)


# -----------------------------------------------------------------------
# Resources
# -----------------------------------------------------------------------

@mcp.resource("memory://stats")
async def memory_stats() -> str:
    """Overall memory statistics."""
    engine = await get_engine()
    cache = engine.cache
    nodes = cache.get_all_nodes()
    edges = cache.get_all_edges()
    displaced = sum(1 for nid in cache.displacement_cache
                   if np.linalg.norm(cache.displacement_cache[nid]) > 0.001)
    max_mass = max((n.mass for n in nodes), default=0)

    return json.dumps({
        "total_memories": len(nodes),
        "active_memories": sum(1 for n in nodes if n.mass > 1.01),
        "total_edges": len(edges),
        "displaced_nodes": displaced,
        "max_mass": round(max_mass, 2),
        "faiss_vectors": engine.faiss_index.size,
    }, ensure_ascii=False, indent=2)


@mcp.resource("memory://hot")
async def memory_hot() -> str:
    """Top 10 highest-mass (most frequently recalled) memories."""
    engine = await get_engine()
    nodes = sorted(engine.cache.get_all_nodes(), key=lambda n: n.mass, reverse=True)[:10]
    items = []
    for n in nodes:
        doc = await engine.store.get_document(n.id)
        items.append({
            "id": n.id,
            "mass": round(n.mass, 3),
            "temperature": round(n.temperature, 6),
            "content_preview": (doc.get("content", "")[:150] if doc else ""),
        })
    return json.dumps(items, ensure_ascii=False, indent=2)


# -----------------------------------------------------------------------
# Prompts
# -----------------------------------------------------------------------

@mcp.prompt()
async def context_recall(topic: str) -> str:
    """Recall memories relevant to the current topic and use them to inform your response."""
    engine = await get_engine()
    results = await engine.query(text=topic, top_k=5)

    memories = []
    for r in results:
        memories.append(f"- (relevance={r.final_score:.3f}) {r.content[:300]}")

    memory_text = "\n".join(memories) if memories else "(No relevant memories found)"

    return (
        f"The following long-term memories are relevant to \"{topic}\":\n\n"
        f"{memory_text}\n\n"
        f"Use these memories to inform your response. "
        f"They are ranked by GaOTTT's gravitational model based on past access patterns."
    )


@mcp.prompt()
async def save_context(summary: str) -> str:
    """Save important conversation context to long-term memory before it's compressed."""
    return (
        f"Please save the following summary to long-term memory using the 'remember' tool "
        f"with source='compaction':\n\n{summary}"
    )


@mcp.prompt()
async def explore_connections(topic_a: str, topic_b: str) -> str:
    """Explore unexpected connections between two topics."""
    return (
        f"Use the 'explore' tool with high diversity to find connections between "
        f"\"{topic_a}\" and \"{topic_b}\". Look for surprising, non-obvious relationships "
        f"in the long-term memory that bridge these two domains."
    )


# -----------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------

def _install_idle_watcher(idle_timeout: float) -> None:
    """Track last HTTP activity and gracefully shut down the backend when
    idle for longer than ``idle_timeout`` seconds.

    Implementation: monkey-patch ``mcp.streamable_http_app`` /
    ``mcp.sse_app`` so the Starlette app FastMCP returns gets an
    ``ActivityMiddleware`` injected. The middleware refreshes
    ``last_activity`` on every HTTP request (covering MCP tool calls,
    MCP protocol pings, session lifecycle, everything across the wire).
    A background asyncio task started on first activity periodically
    checks the idle window and SIGTERMs the process when exceeded,
    after flushing the engine cache.

    Earlier attempts at wrapping ``Server._handle_request`` /
    ``Server._handle_message`` directly on the server instance failed
    in the streamable-http path — anyio's task-group dispatch captures
    method references in a way that bypassed the instance overrides
    for those two methods. Starlette middleware sits at the HTTP layer
    above MCP, well clear of that issue.
    """
    import signal
    import time

    from starlette.middleware.base import BaseHTTPMiddleware

    state: dict[str, object] = {
        "last_activity": time.monotonic(),
        "watchdog_started": False,
        # H7 — number of HTTP requests currently executing. The watchdog
        # must never SIGTERM the process while a request is in flight: a
        # single `ingest` of a large directory or `compact(rebuild_faiss)`
        # on a 24k corpus can run far longer than idle_timeout with no
        # other traffic, and killing it mid-write (while the watchdog also
        # flushes the cache) races the destructive op against shutdown.
        "in_flight": 0,
        # Hold the task reference so it isn't GC'd mid-run (fire-and-forget
        # create_task can be collected) and so it is cancellable.
        "task": None,
    }

    async def watchdog() -> None:
        check_every = max(5.0, idle_timeout / 10.0)
        while True:
            await asyncio.sleep(check_every)
            # A request in flight (possibly a long ingest/compact) means
            # the process is NOT idle — defer; its exit refreshes
            # last_activity so the clock restarts cleanly.
            if state["in_flight"]:  # type: ignore[truthy-bool]
                continue
            idle_for = time.monotonic() - state["last_activity"]  # type: ignore[operator]
            if idle_for <= idle_timeout:
                continue
            logger.info(
                "Idle for %.1fs (> %ss timeout) — shutting down backend. "
                "Next client request will respawn it.",
                idle_for, int(idle_timeout),
            )
            # Graceful cache flush so dirty displacement / velocity /
            # mass updates don't vanish on idle shutdown.
            global _engine
            if _engine is not None:
                try:
                    await _engine.cache.flush_to_store(_engine.store)
                    logger.info("Final cache flush complete")
                except Exception:  # noqa: BLE001
                    logger.exception("Final cache flush failed; exiting anyway")
            # SIGTERM lets uvicorn run its shutdown hooks; OS reaps us
            # if they don't fire fast enough.
            os.kill(os.getpid(), signal.SIGTERM)
            return

    class ActivityMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if not state["watchdog_started"]:
                state["watchdog_started"] = True
                # H7 — get_running_loop()/create_task instead of the
                # deprecated get_event_loop(); keep the handle so the task
                # isn't GC'd and stays cancellable. `state` is a single
                # shared closure, so even if FastMCP rebuilds the Starlette
                # app the guard prevents a second watchdog.
                state["task"] = asyncio.create_task(watchdog())
                logger.info("Idle watchdog started (timeout=%ss)", int(idle_timeout))
            state["last_activity"] = time.monotonic()
            state["in_flight"] = state["in_flight"] + 1  # type: ignore[operator]
            try:
                return await call_next(request)
            finally:
                # Refresh on exit too: a long request must reset the idle
                # clock when it *finishes*, not only when it started.
                state["in_flight"] = state["in_flight"] - 1  # type: ignore[operator]
                state["last_activity"] = time.monotonic()

    # Wrap the app factory so the middleware is installed every time
    # FastMCP rebuilds the Starlette app (FastMCP caches, so this
    # normally runs once on the first transport.run call).
    original_streamable = mcp.streamable_http_app
    original_sse = mcp.sse_app

    def patched_streamable():
        app = original_streamable()
        app.add_middleware(ActivityMiddleware)
        return app

    def patched_sse(mount_path=None):
        app = original_sse(mount_path)
        app.add_middleware(ActivityMiddleware)
        return app

    mcp.streamable_http_app = patched_streamable  # type: ignore[method-assign]
    mcp.sse_app = patched_sse  # type: ignore[method-assign]
    logger.info("Idle watcher installed (timeout=%ss)", int(idle_timeout))


def build_token_middleware(expected_token: str):
    """Return a ``BaseHTTPMiddleware`` subclass that enforces
    ``Authorization: Bearer <expected_token>``.

    Extracted to module level (rather than closed over inside
    ``_install_token_middleware``) so the behaviour can be unit-tested
    against a minimal Starlette app without spinning up FastMCP or the
    engine. The comparison uses ``secrets.compare_digest`` to deny
    timing-based token discovery.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class TokenMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Split on whitespace so "Bearer" alone (no token), "Bearer "
            # (empty token), and "Bearer xxx yyy" (extra tokens) all collapse
            # to a malformed shape that is rejected before any comparison.
            parts = request.headers.get("authorization", "").split()
            if len(parts) != 2 or parts[0].lower() != "bearer":
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            # 401 returns *before* call_next, so an outer-brute-force attempt
            # never refreshes the idle watcher's activity clock.
            if not secrets.compare_digest(parts[1], expected_token):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            return await call_next(request)

    return TokenMiddleware


def _install_token_middleware() -> None:
    """Backend token auth — only active when ``GAOTTT_BACKEND_TOKEN`` is set.

    Unset / empty → no-op (default unchanged; the existing single 7878
    backend keeps its open-by-default posture). Set → install a
    ``TokenMiddleware`` on both ``mcp.streamable_http_app`` and
    ``mcp.sse_app`` using the same factory-wrap pattern as
    ``_install_idle_watcher``.

    Must be called *after* ``_install_idle_watcher`` so the token layer is
    added last and therefore sits as the outermost middleware: a rejected
    request short-circuits before ``call_next`` and never reaches the
    ActivityMiddleware, so brute-force attempts cannot keep an idle
    backend alive.
    """
    token = os.environ.get("GAOTTT_BACKEND_TOKEN", "")
    if not token:
        return

    TokenMiddleware = build_token_middleware(token)

    original_streamable = mcp.streamable_http_app
    original_sse = mcp.sse_app

    def patched_streamable():
        app = original_streamable()
        app.add_middleware(TokenMiddleware)
        return app

    def patched_sse(mount_path=None):
        app = original_sse(mount_path)
        app.add_middleware(TokenMiddleware)
        return app

    mcp.streamable_http_app = patched_streamable  # type: ignore[method-assign]
    mcp.sse_app = patched_sse  # type: ignore[method-assign]
    logger.info("Backend token middleware installed (GAOTTT_BACKEND_TOKEN set)")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog="gaottt.server.mcp_server",
        description=(
            "GaOTTT MCP server. Default `proxy` mode auto-spawns a shared "
            "HTTP backend (or connects to one) and relays stdio↔HTTP — "
            "any number of agents can configure stdio in their .mcp.json "
            "and they all share a single engine. Other transports: "
            "`stdio` (legacy single-process), `streamable-http`/`sse` "
            "(explicit backend, e.g. when running under systemd)."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=("proxy", "stdio", "streamable-http", "sse"),
        default="proxy",
        help=(
            "MCP transport. proxy (default) = stdio shim that auto-spawns "
            "+ relays to a shared HTTP backend. stdio = legacy, each agent "
            "loads its own engine. streamable-http / sse = HTTP backend "
            "only (use with a process manager). All four use the same "
            "tool surface."
        ),
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help=(
            "Bind address for HTTP backend (also used by proxy mode to "
            "find / spawn the backend). 127.0.0.1 = localhost only. Use "
            "0.0.0.0 only if you've configured your own auth — no auth "
            "is built in."
        ),
    )
    parser.add_argument(
        "--port", type=int, default=7878,
        help="Backend port. Default 7878 (mnemonic: NSNS).",
    )
    parser.add_argument(
        "--idle-timeout", type=float, default=300.0,
        help=(
            "Backend self-shutdown threshold in seconds — if no MCP "
            "request (tool call, ping, or any other) arrives for this "
            "long, the backend flushes cache and exits. Next client "
            "request will respawn it. Default 300 (5 minutes). "
            "Only used by the backend (streamable-http / sse / when "
            "spawned by proxy mode)."
        ),
    )
    parser.add_argument(
        "--ping-interval", type=float, default=60.0,
        help=(
            "Proxy mode: seconds between heartbeat pings sent to the "
            "backend. Must be comfortably below --idle-timeout so a "
            "single slow round-trip doesn't trip the watchdog. "
            "Default 60."
        ),
    )
    parser.add_argument(
        "--force-takeover", action="store_true", default=False,
        help=(
            "Force-take the owner lease even if another process holds a "
            "fresh one. Use only when you know the current owner is dead. "
            "Sets GAOTTT_LEASE_FORCE_TAKEOVER=true for the process (and "
            "any spawned backend in proxy mode)."
        ),
    )
    parser.add_argument(
        "--supervisor-url", default=None,
        help=(
            "Supervisor URL for multiverse routing (e.g. "
            "http://127.0.0.1:7880). When set, the proxy resolves the "
            "backend via POST /route instead of auto-spawning. Requires "
            "GAOTTT_API_KEY env. Default: unset (legacy auto-spawn mode)."
        ),
    )
    parser.add_argument(
        "--spawn-supervisor", action="store_true", default=False,
        help=(
            "Proxy supervisor mode: if the configured local supervisor is "
            "unreachable, start it as a detached subprocess and wait for "
            "readiness. Requires GAOTTT_MULTIVERSE_ROOT and "
            "GAOTTT_SUPERVISOR_ADMIN_KEY. Local http URLs only."
        ),
    )
    args = parser.parse_args()

    # MV2 — propagate --force-takeover into the GAOTTT_* env layer so the
    # generic override loop raises config.lease_force_takeover for both this
    # process's runtime config and any backend spawned by proxy mode (which
    # inherits the environment).
    if args.force_takeover:
        os.environ["GAOTTT_LEASE_FORCE_TAKEOVER"] = "true"

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    if args.transport == "proxy":
        from gaottt.server.mcp_proxy import run_proxy
        api_key = os.environ.get("GAOTTT_API_KEY", "")
        asyncio.run(run_proxy(
            host=args.host,
            port=args.port,
            idle_timeout=args.idle_timeout,
            ping_interval=args.ping_interval,
            supervisor_url=args.supervisor_url,
            api_key=api_key if args.supervisor_url else None,
            spawn_supervisor=args.spawn_supervisor,
        ))
        return

    # HTTP backend modes — set host/port + install idle watcher, then run.
    mcp.settings.host = args.host
    mcp.settings.port = args.port

    if args.idle_timeout > 0:
        _install_idle_watcher(args.idle_timeout)

    # Installed after the idle watcher so the token layer is the outermost
    # middleware: a 401 short-circuits before the ActivityMiddleware and
    # cannot refresh the idle clock. No-op unless GAOTTT_BACKEND_TOKEN is set.
    _install_token_middleware()

    if args.transport == "streamable-http":
        url = f"http://{args.host}:{args.port}{mcp.settings.streamable_http_path}"
        logger.info("Starting GaOTTT MCP backend (streamable-http) at %s", url)
        mcp.run(transport="streamable-http")
    else:  # sse
        url = f"http://{args.host}:{args.port}{mcp.settings.sse_path}"
        logger.info("Starting GaOTTT MCP backend (sse) at %s", url)
        mcp.run(transport="sse")


if __name__ == "__main__":
    main()
