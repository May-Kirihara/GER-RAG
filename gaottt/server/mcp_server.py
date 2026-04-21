"""GaOTTT MCP Server — AI Agent Long-Term Memory (formerly GER-RAG)

Provides gravitational displacement-powered memory for AI agents.
Phase R4 will reframe the philosophy as "TTT framework that happens to look like RAG".

Usage:
    # stdio (Claude Code / Claude Desktop)
    python -m gaottt.server.mcp_server

    # SSE (remote clients)
    python -m gaottt.server.mcp_server --transport sse --port 8001
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
        "cache), 'prefetch' to pre-warm the gravity well around an anticipated "
        "query, 'prefetch_status' to inspect cache health, 'explore' for "
        "serendipitous discovery, 'reflect' to analyze memory state "
        "(aspect='duplicates' for collision candidates, 'relations' for "
        "typed-edge overview), 'auto_remember' to extract save candidates, "
        "'forget'/'restore' to prune (soft by default), 'merge' to collide "
        "near-duplicates, 'compact' for periodic maintenance, 'revalidate' to "
        "refresh certainty, 'relate'/'unrelate'/'get_relations' for typed "
        "directed edges (supersedes/derived_from/contradicts), Phase D persona "
        "& task layer ('commit'/'start'/'complete'/'abandon'/'depend' for "
        "tasks; 'declare_value'/'declare_intention'/'declare_commitment' for "
        "persona; 'inherit_persona' to wear past-self at session start; "
        "reflect aspects: tasks_todo/tasks_doing/tasks_completed/"
        "tasks_abandoned/commitments/values/intentions/relationships/persona), "
        "and 'ingest' to bulk-load files."
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
) -> str:
    """Search long-term memory with gravitational wave propagation.

    Gravity waves propagate recursively through the knowledge space.
    High-mass memories attract more neighbors, creating wider gravitational fields.

    By default this transparently consumes any matching prefetch entry —
    if `prefetch(query, top_k)` was called recently, the result is returned
    instantly. Pass `force_refresh=True` to bypass the prefetch cache and
    re-run the full wave simulation.

    Args:
        query: Search query
        top_k: Number of results (default 5)
        source_filter: Filter by source, e.g. ["agent", "compaction"]
        wave_depth: Override recursion depth (default from config)
        wave_k: Override initial seed count (default from config)
        force_refresh: Bypass prefetch cache (default False)
    """
    engine = await get_engine()
    result = await memory_service.recall(
        engine, query=query, top_k=top_k, source_filter=source_filter,
        wave_depth=wave_depth, wave_k=wave_k, force_refresh=force_refresh,
    )
    return formatters.format_recall(result)


@mcp.tool()
async def explore(
    query: str,
    diversity: float = 0.5,
    top_k: int = 10,
) -> str:
    """Explore memories serendipitously with increased randomness.

    Higher diversity increases wave depth and temperature noise,
    bringing unexpected cross-domain connections through deeper gravitational propagation.

    Args:
        query: Starting point for exploration
        diversity: 0.0 = normal search, 1.0 = maximum exploration
        top_k: Number of results
    """
    engine = await get_engine()
    result = await memory_service.explore(
        engine, query=query, diversity=diversity, top_k=top_k,
    )
    return formatters.format_explore(result)


@mcp.tool()
async def reflect(
    aspect: str = "summary",
    limit: int = 10,
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
    """
    engine = await get_engine()
    return await _reflect_dispatch(engine, aspect, limit)


async def _reflect_dispatch(engine, aspect: str, limit: int) -> str:
    """Dispatch a reflect aspect through the service + formatter layers."""
    if aspect == "summary":
        r = await reflection_service.summary(engine)
        return formatters.format_reflect_summary(r)
    if aspect == "hot_topics":
        r = await reflection_service.hot_topics(engine, limit=limit)
        return formatters.format_reflect_hot_topics(r)
    if aspect == "connections":
        r = await reflection_service.connections(engine, limit=limit)
        return formatters.format_reflect_connections(r)
    if aspect == "dormant":
        r = await reflection_service.dormant(engine, limit=limit)
        return formatters.format_reflect_dormant(r)
    if aspect == "duplicates":
        r = await reflection_service.duplicates(engine, limit=limit)
        return formatters.format_reflect_duplicates(r, limit=limit)
    if aspect == "relations":
        r = await reflection_service.relations_overview(engine, limit=limit)
        return formatters.format_reflect_relations_overview(r)
    if aspect == "tasks_todo":
        r = await reflection_service.tasks_todo(engine, limit=limit)
        return formatters.format_reflect_tasks_todo(r, limit=limit)
    if aspect == "tasks_doing":
        r = await reflection_service.tasks_doing(engine, limit=limit)
        return formatters.format_reflect_tasks_doing(r)
    if aspect == "tasks_completed":
        r = await reflection_service.tasks_completed(engine, limit=limit)
        return formatters.format_reflect_tasks_completed(r, limit=limit)
    if aspect == "tasks_abandoned":
        r = await reflection_service.tasks_abandoned(engine, limit=limit)
        return formatters.format_reflect_tasks_abandoned(r, limit=limit)
    if aspect == "commitments":
        r = await reflection_service.commitments(engine, limit=limit)
        return formatters.format_reflect_commitments(r, limit=limit)
    if aspect == "intentions":
        r = await reflection_service.intentions(engine, limit=limit)
        return formatters.format_reflect_intentions(r, limit=limit)
    if aspect == "values":
        r = await reflection_service.values_(engine, limit=limit)
        return formatters.format_reflect_values(r, limit=limit)
    if aspect == "relationships":
        r = await reflection_service.relationships(engine, limit=limit)
        return formatters.format_reflect_relationships(r)
    if aspect == "persona":
        r = await reflection_service.persona_snapshot(engine)
        return formatters.format_persona_snapshot(r)
    return f"Unknown aspect: {aspect}"


@mcp.tool()
async def prefetch(
    query: str,
    top_k: int = 5,
    wave_depth: int | None = None,
    wave_k: int | None = None,
) -> str:
    """Schedule a background recall to pre-load related memories.

    The astrocyte's true workload: while you reason in the foreground, this
    pre-warms the gravity well around `query` so a subsequent `recall` with
    the same arguments returns instantly. Useful at the start of a turn when
    you can predict what the user will probe next.

    Returns immediately; the actual work runs in a bounded background pool.
    Subsequent `recall(query, top_k)` calls within the cache TTL (default 90s)
    are served from the cache without re-running the wave simulation.

    Args:
        query: search text to pre-warm
        top_k: number of results to cache (must match the eventual recall)
        wave_depth: optional override
        wave_k: optional override
    """
    engine = await get_engine()
    result = maintenance_service.prefetch(
        engine, query=query, top_k=top_k, wave_depth=wave_depth, wave_k=wave_k,
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
async def ingest(
    path: str,
    source: str = "file",
    recursive: bool = False,
    pattern: str = "*.md,*.txt",
    chunk_size: int = 2000,
) -> str:
    """Bulk-load files or directories into memory.

    Supports Markdown (.md), plain text (.txt), and CSV (.csv).

    Args:
        path: File or directory path
        source: Source label for metadata
        recursive: Recursively scan directories
        pattern: Glob patterns (comma-separated)
        chunk_size: Max characters per chunk for long documents
    """
    engine = await get_engine()
    result = await ingest_service.ingest(
        engine, path=path, source=source, recursive=recursive,
        pattern=pattern, chunk_size=chunk_size,
    )
    return formatters.format_ingest(result)


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

def main():
    import sys
    transport = "stdio"
    port = 8001
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--transport" and i + 1 < len(args):
            transport = args[i + 1]
            i += 2
        elif args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        else:
            i += 1

    if transport == "sse":
        mcp.run(transport="sse", port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
