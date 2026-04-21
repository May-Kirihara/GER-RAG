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
import time
from pathlib import Path

import numpy as np
from mcp.server.fastmcp import FastMCP

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.core.extractor import extract_candidates
from gaottt.embedding.ruri import RuriEmbedder
from gaottt.index.faiss_index import FaissIndex
from gaottt.ingest.loader import ingest_path
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore

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
        embedder = RuriEmbedder(model_name=config.model_name, batch_size=config.batch_size)
        faiss_index = FaissIndex(dimension=config.embedding_dim)
        store = SqliteStore(db_path=config.db_path)
        cache = CacheLayer(
            flush_interval=config.flush_interval_seconds,
            flush_threshold=config.flush_threshold,
        )
        engine = GaOTTTEngine(
            config=config, embedder=embedder, faiss_index=faiss_index,
            cache=cache, store=store,
        )
        await engine.startup()
        _engine = engine
        logger.info("GaOTTT engine ready (%d nodes, %d vectors)", len(cache.node_cache), faiss_index.size)
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
# Internal helpers (shared by remember + Phase D commit/declare_* tools)
# -----------------------------------------------------------------------

async def _save_memory(
    engine,
    content: str,
    source: str,
    tags: list[str] | None = None,
    context: str | None = None,
    ttl_seconds: float | None = None,
    emotion: float = 0.0,
    certainty: float = 1.0,
    extra_metadata: dict | None = None,
) -> tuple[str | None, dict]:
    """Build the document dict and call engine.index_documents.

    Returns (id_or_None, metadata). id is None when the content was a duplicate.
    """
    metadata = {"source": source}
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

    doc: dict = {
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
    new_id, metadata = await _save_memory(
        engine, content=content, source=source, tags=tags, context=context,
        ttl_seconds=ttl_seconds, emotion=emotion, certainty=certainty,
    )
    if new_id is None:
        return "Already exists in memory (duplicate content)."
    suffix = f" (expires {metadata['expires_at']})" if "expires_at" in metadata else ""
    return f"Remembered. ID: {new_id}{suffix}"


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
    state = await engine.revalidate(node_id, certainty=certainty, emotion=emotion)
    if state is None:
        return f"Node {node_id} not found or archived."
    return (
        f"Revalidated {node_id[:8]}.. "
        f"certainty={state.certainty:.2f}, emotion={state.emotion_weight:+.2f}"
    )


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
    affected = await engine.forget(node_ids, hard=hard)
    verb = "Hard-deleted" if hard else "Archived"
    return f"{verb} {affected} of {len(node_ids)} requested memories."


@mcp.tool()
async def restore(node_ids: list[str]) -> str:
    """Restore previously archived memories back into active recall.

    Only works for soft-archived nodes (hard-deleted ones are gone).
    """
    engine = await get_engine()
    affected = await engine.restore(node_ids)
    return f"Restored {affected} of {len(node_ids)} requested memories."


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
    results = await engine.query(
        text=query, top_k=top_k * 2 if source_filter else top_k,
        wave_depth=wave_depth, wave_k=wave_k,
        use_cache=not force_refresh,
    )

    if source_filter:
        filtered = []
        for r in results:
            meta = r.metadata or {}
            if meta.get("source") in source_filter:
                filtered.append(r)
        results = filtered[:top_k]

    if not results:
        return "No memories found."

    lines = []
    for i, r in enumerate(results):
        meta = r.metadata or {}
        source = meta.get("source", "unknown")
        tags = meta.get("tags", [])
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        disp = engine.get_displacement_norm(r.id)
        lines.append(
            f"[{i+1}] id={r.id} (score={r.final_score:.4f}, raw={r.raw_score:.4f}, "
            f"source={source}{tag_str}, displacement={disp:.4f})\n"
            f"{r.content}"
        )

    return "\n\n---\n\n".join(lines)


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
    config = engine.config

    # Temporarily boost temperature for exploration
    original_gamma = config.gamma
    config.gamma = config.gamma * (1.0 + diversity * 20.0)

    # Diversity controls wave depth and initial k
    explore_depth = config.wave_max_depth + int(diversity * 2)  # +0 to +2 extra depth
    explore_k = config.wave_initial_k + int(diversity * 4)      # +0 to +4 extra seeds

    try:
        results = await engine.query(
            text=query, top_k=top_k,
            wave_depth=explore_depth, wave_k=explore_k,
        )
    finally:
        config.gamma = original_gamma

    if not results:
        return "No memories found for exploration."

    lines = [f"Exploration (diversity={diversity:.1f}):"]
    for i, r in enumerate(results):
        meta = r.metadata or {}
        source = meta.get("source", "unknown")
        lines.append(
            f"[{i+1}] (score={r.final_score:.4f}, source={source})\n"
            f"{r.content[:200]}"
        )

    return "\n\n---\n\n".join(lines)


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
    cache = engine.cache
    now = time.time()

    if aspect == "summary":
        nodes = cache.get_all_nodes()
        edges = cache.get_all_edges()
        active = sum(1 for n in nodes if n.mass > 1.01)
        displaced = sum(1 for nid in cache.displacement_cache
                       if np.linalg.norm(cache.displacement_cache[nid]) > 0.001)
        sources: dict[str, int] = {}
        for n in nodes:
            doc = await engine.store.get_document(n.id)
            if doc:
                s = (doc.get("metadata") or {}).get("source", "unknown")
                sources[s] = sources.get(s, 0) + 1

        return (
            f"Memory Summary:\n"
            f"  Total memories: {len(nodes)}\n"
            f"  Active (mass > 1): {active}\n"
            f"  Displaced by gravity: {displaced}\n"
            f"  Co-occurrence edges: {len(edges)}\n"
            f"  Sources: {json.dumps(sources, ensure_ascii=False)}"
        )

    elif aspect == "hot_topics":
        nodes = sorted(cache.get_all_nodes(), key=lambda n: n.mass, reverse=True)[:limit]
        lines = ["High-mass memories (frequently recalled):"]
        for n in nodes:
            doc = await engine.store.get_document(n.id)
            content = (doc.get("content", "")[:100] if doc else "?").replace("\n", " ")
            lines.append(f"  id={n.id} mass={n.mass:.2f} temp={n.temperature:.6f} | {content}...")
        return "\n".join(lines)

    elif aspect == "connections":
        edges = sorted(cache.get_all_edges(), key=lambda e: e.weight, reverse=True)[:limit]
        lines = [f"Strongest connections ({len(edges)} shown):"]
        for e in edges:
            doc_s = await engine.store.get_document(e.src)
            doc_d = await engine.store.get_document(e.dst)
            s_text = (doc_s.get("content", "")[:50] if doc_s else "?").replace("\n", " ")
            d_text = (doc_d.get("content", "")[:50] if doc_d else "?").replace("\n", " ")
            lines.append(f"  weight={e.weight:.1f}: {e.src}↔{e.dst} | [{s_text}...] <-> [{d_text}...]")
        return "\n".join(lines)

    elif aspect == "dormant":
        nodes = sorted(cache.get_all_nodes(), key=lambda n: n.last_access)[:limit]
        lines = ["Dormant memories (longest since last access):"]
        for n in nodes:
            age_days = (now - n.last_access) / 86400
            doc = await engine.store.get_document(n.id)
            content = (doc.get("content", "")[:100] if doc else "?").replace("\n", " ")
            lines.append(f"  id={n.id} {age_days:.1f} days ago, mass={n.mass:.2f} | {content}...")
        return "\n".join(lines)

    elif aspect == "relations":
        edges = await engine.store.get_directed_edges()
        if not edges:
            return "No directed relations recorded yet."
        by_type: dict[str, int] = {}
        for e in edges:
            by_type[e.edge_type] = by_type.get(e.edge_type, 0) + 1
        lines = [f"Directed relations ({len(edges)} total):"]
        for t, c in sorted(by_type.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {t}: {c}")
        recent = sorted(edges, key=lambda e: e.created_at, reverse=True)[:limit]
        lines.append(f"\nMost recent {len(recent)}:")
        for e in recent:
            lines.append(
                f"  {e.src[:8]}.. --[{e.edge_type}]--> {e.dst[:8]}.. "
                f"(weight={e.weight:.2f})"
            )
        return "\n".join(lines)

    elif aspect == "duplicates":
        clusters = engine.find_duplicates(threshold=0.95, top_n_by_mass=500)
        if not clusters:
            return "No near-duplicate clusters found (threshold 0.95)."
        lines = [f"Near-duplicate clusters ({len(clusters)} found, top {limit}):"]
        for i, c in enumerate(clusters[:limit], start=1):
            lines.append(
                f"\n[Cluster {i}] {len(c.ids)} nodes, "
                f"avg_pairwise_sim={c.avg_pairwise_similarity:.3f}"
            )
            for nid in c.ids:
                doc = await engine.store.get_document(nid)
                content = (doc.get("content", "")[:80] if doc else "?").replace("\n", " ")
                lines.append(f"  - {nid[:8]}.. mass={cache.get_node(nid).mass:.2f} | {content}")
            lines.append(
                f"  → To merge: merge(node_ids={list(c.ids)})"
            )
        return "\n".join(lines)

    elif aspect in (
        "tasks_todo", "tasks_doing", "tasks_completed", "tasks_abandoned",
        "commitments", "intentions", "values", "persona", "relationships",
    ):
        return await _reflect_phase_d(engine, aspect, limit, now)

    return f"Unknown aspect: {aspect}"


async def _reflect_phase_d(engine, aspect: str, limit: int, now: float) -> str:
    """Phase D aspects — task & persona surfaces.

    Implementation note: source matching scans cache.get_all_nodes() which is
    O(N) but in-memory. For tasks_completed/abandoned, we query directed_edges
    by edge_type since those tasks are typically archived (cache-evicted).
    """
    cache = engine.cache
    store = engine.store

    async def _content_of(node_id: str, max_len: int = 120) -> str:
        doc = await store.get_document(node_id)
        if doc is None:
            return "?"
        return (doc.get("content", "")[:max_len]).replace("\n", " ")

    async def _gather_by_source(prefix_match: bool = False, *sources: str) -> list[tuple[str, str, dict]]:
        """Return (node_id, content, metadata) for nodes whose source matches."""
        out: list[tuple[str, str, dict]] = []
        for state in cache.get_all_nodes():
            doc = await store.get_document(state.id)
            if doc is None:
                continue
            meta = doc.get("metadata") or {}
            src = meta.get("source", "")
            if prefix_match:
                if not any(src.startswith(s) for s in sources):
                    continue
            else:
                if src not in sources:
                    continue
            out.append((state.id, doc.get("content", ""), meta))
        return out

    # ----- Task aspects -----

    if aspect == "tasks_todo":
        # Active tasks: source=task, not archived (= still in cache), no completed/abandoned edge
        tasks = await _gather_by_source(False, "task")
        # Filter out tasks that have any completed or abandoned incoming edge
        eligible: list[tuple[str, str, dict, float]] = []
        for tid, content, meta in tasks:
            inc = await store.get_directed_edges(node_id=tid, direction="in")
            if any(e.edge_type in ("completed", "abandoned") for e in inc):
                continue
            state = cache.get_node(tid)
            deadline = state.expires_at if state and state.expires_at else float("inf")
            eligible.append((tid, content, meta, deadline))
        eligible.sort(key=lambda t: t[3])  # closest deadline first
        if not eligible:
            return "No active tasks. Use `commit(...)` to start one."
        lines = [f"Active tasks ({len(eligible)} total, showing top {limit} by deadline):"]
        for tid, content, meta, dl in eligible[:limit]:
            dl_str = meta.get("expires_at", "permanent")
            days_left = (dl - now) / 86400 if dl != float("inf") else None
            days_note = f" ({days_left:+.1f}d)" if days_left is not None else ""
            lines.append(f"  id={tid} deadline={dl_str}{days_note} | {content[:120]}")
        return "\n".join(lines)

    if aspect == "tasks_doing":
        # Recently revalidated tasks (last 1 hour by last_verified_at)
        threshold = now - 3600
        tasks = await _gather_by_source(False, "task")
        active = []
        for tid, content, _meta in tasks:
            state = cache.get_node(tid)
            if state and state.last_verified_at and state.last_verified_at >= threshold:
                active.append((tid, content, state.last_verified_at))
        active.sort(key=lambda t: t[2], reverse=True)
        if not active:
            return "No tasks actively in progress (no `start()` in the last hour)."
        lines = [f"In-progress tasks ({len(active)}):"]
        for tid, content, lva in active[:limit]:
            mins_ago = (now - lva) / 60
            lines.append(f"  id={tid} ({mins_ago:.0f}m ago) | {content[:120]}")
        return "\n".join(lines)

    if aspect == "tasks_completed":
        edges = await store.get_directed_edges(edge_type="completed")
        edges.sort(key=lambda e: e.created_at, reverse=True)
        if not edges:
            return "No completed tasks yet."
        lines = [f"Completed tasks ({len(edges)} total, showing top {limit}):"]
        for e in edges[:limit]:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.created_at))
            task_content = await _content_of(e.dst, max_len=80)
            outcome_content = await _content_of(e.src, max_len=80)
            lines.append(f"  {ts}  task={e.dst[:8]}.. | {task_content}")
            lines.append(f"        outcome={e.src[:8]}.. | {outcome_content}")
        return "\n".join(lines)

    if aspect == "tasks_abandoned":
        edges = await store.get_directed_edges(edge_type="abandoned")
        edges.sort(key=lambda e: e.created_at, reverse=True)
        if not edges:
            return "No abandoned tasks (yet — that's OK)."
        lines = [f"Abandoned tasks (shadow chronology, {len(edges)} total, top {limit}):"]
        for e in edges[:limit]:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.created_at))
            task_content = await _content_of(e.dst, max_len=80)
            reason_content = await _content_of(e.src, max_len=120)
            lines.append(f"  {ts}  task={e.dst[:8]}.. | {task_content}")
            lines.append(f"        reason | {reason_content}")
        return "\n".join(lines)

    # ----- Persona aspects -----

    if aspect == "commitments":
        commitments = await _gather_by_source(False, "commitment")
        # Sort by closest deadline
        annotated: list[tuple[str, str, dict, float]] = []
        for cid, content, meta in commitments:
            state = cache.get_node(cid)
            deadline = state.expires_at if state and state.expires_at else float("inf")
            annotated.append((cid, content, meta, deadline))
        annotated.sort(key=lambda t: t[3])
        if not annotated:
            return "No active commitments. Use `declare_commitment(...)`."
        lines = [f"Active commitments ({len(annotated)} total, showing top {limit}):"]
        for cid, content, meta, dl in annotated[:limit]:
            dl_str = meta.get("expires_at", "permanent")
            days_left = (dl - now) / 86400 if dl != float("inf") else None
            days_note = f" ({days_left:+.1f}d)" if days_left is not None else ""
            warn = " ⚠️" if days_left is not None and days_left < 2 else ""
            lines.append(f"  id={cid} deadline={dl_str}{days_note}{warn} | {content[:120]}")
        return "\n".join(lines)

    if aspect == "intentions":
        intentions = await _gather_by_source(False, "intention")
        if not intentions:
            return "No intentions declared. Use `declare_intention(...)`."
        lines = [f"Intentions ({len(intentions)} total, showing top {limit}):"]
        for iid, content, _meta in intentions[:limit]:
            lines.append(f"  id={iid} | {content[:160]}")
        return "\n".join(lines)

    if aspect == "values":
        values = await _gather_by_source(False, "value")
        if not values:
            return "No values declared. Use `declare_value(...)`."
        lines = [f"Values ({len(values)} total, showing top {limit}):"]
        for vid, content, _meta in values[:limit]:
            lines.append(f"  id={vid} | {content[:160]}")
        return "\n".join(lines)

    if aspect == "relationships":
        rels = await _gather_by_source(True, "relationship:")
        if not rels:
            return "No relationships recorded. Use `remember(source=\"relationship:<name>\", ...)`."
        # Group by who
        by_who: dict[str, list[tuple[str, str]]] = {}
        for rid, content, meta in rels:
            who = meta.get("source", "relationship:?").split(":", 1)[1] or "?"
            by_who.setdefault(who, []).append((rid, content))
        lines = [f"Relationships ({len(by_who)} people, {len(rels)} memories):"]
        for who, items in sorted(by_who.items(), key=lambda kv: -len(kv[1]))[:limit]:
            lines.append(f"\n## {who}  ({len(items)} memories)")
            for rid, content in items[:3]:
                lines.append(f"  id={rid[:8]}.. | {content[:120]}")
        return "\n".join(lines)

    if aspect == "persona":
        # Composite snapshot — same content as inherit_persona but invoked via reflect
        return await inherit_persona()

    return f"Unknown Phase D aspect: {aspect}"


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
    engine.prefetch(text=query, top_k=top_k, wave_depth=wave_depth, wave_k=wave_k)
    return (
        f"Scheduled prefetch for '{query[:60]}...' (top_k={top_k}). "
        f"Subsequent recall within {engine.config.prefetch_ttl_seconds:.0f}s "
        f"will be served from cache."
    )


@mcp.tool()
async def prefetch_status() -> str:
    """Inspect the prefetch cache and async pool stats."""
    engine = await get_engine()
    status = engine.prefetch_status()
    cache = status["cache"]
    pool = status["pool"]
    return (
        "Prefetch cache:\n"
        f"  size:      {cache['size']}/{cache['max_size']}  (active: {cache['active']})\n"
        f"  hit/miss:  {cache['hits']} / {cache['misses']}  "
        f"(hit_rate: {cache['hit_rate']:.2%})\n"
        f"  evictions: {cache['evictions']}\n"
        f"  ttl:       {cache['ttl_seconds']:.0f}s\n"
        "Prefetch pool:\n"
        f"  scheduled: {pool['scheduled']}\n"
        f"  completed: {pool['completed']}\n"
        f"  failed:    {pool['failed']}\n"
        f"  in_flight: {pool['in_flight']}/{pool['max_concurrent']}"
    )


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
    edge = await engine.relate(
        src_id=src_id, dst_id=dst_id, edge_type=edge_type,
        weight=weight, metadata=metadata,
    )
    return (
        f"Related {edge.src[:8]}.. --[{edge.edge_type}]--> {edge.dst[:8]}.. "
        f"(weight={edge.weight:.2f})"
    )


@mcp.tool()
async def unrelate(
    src_id: str,
    dst_id: str,
    edge_type: str | None = None,
) -> str:
    """Remove a directed relation. If edge_type is omitted, removes all relations between the pair."""
    engine = await get_engine()
    n = await engine.unrelate(src_id, dst_id, edge_type)
    return f"Removed {n} directed edge(s) between {src_id[:8]}.. and {dst_id[:8]}.."


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
    edges = await engine.get_relations(node_id, edge_type=edge_type, direction=direction)
    if not edges:
        return f"No directed relations found for {node_id[:8]}.. (direction={direction})."
    lines = [f"Relations for {node_id[:8]}.. (direction={direction}, {len(edges)} found):"]
    for e in edges:
        meta = f" meta={e.metadata}" if e.metadata else ""
        lines.append(
            f"  {e.src[:8]}.. --[{e.edge_type}]--> {e.dst[:8]}.. "
            f"weight={e.weight:.2f}{meta}"
        )
    return "\n".join(lines)


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
    new_id, metadata = await _save_memory(
        engine, content=content, source="task", tags=["todo"],
        ttl_seconds=deadline_seconds, certainty=certainty,
    )
    if new_id is None:
        return "Task already exists (duplicate content)."
    if parent_id:
        try:
            await engine.relate(
                src_id=new_id, dst_id=parent_id, edge_type="fulfills",
                metadata={"declared_at": metadata["remembered_at"]},
            )
        except ValueError as e:
            return f"Task created (id={new_id}) but fulfills edge failed: {e}"
    expires_at = metadata.get("expires_at", "permanent")
    parent_note = f", fulfills {parent_id[:8]}..." if parent_id else ""
    return f"Task committed. ID: {new_id} (deadline {expires_at}{parent_note})"


@mcp.tool()
async def start(task_id: str) -> str:
    """Mark a task as actively being worked on.

    Refreshes the task's certainty (resets the TTL decay clock) and bumps
    its emotion slightly positive — your attention is the energy.
    """
    engine = await get_engine()
    state = await engine.revalidate(task_id, certainty=1.0, emotion=0.4)
    if state is None:
        return f"Task {task_id} not found or archived."
    return f"Started {task_id[:8]}.. (TTL refreshed; emotion={state.emotion_weight:+.2f})"


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
    outcome_id, _ = await _save_memory(
        engine, content=outcome, source="agent", tags=["completed-task"],
        emotion=emotion, certainty=1.0,
    )
    if outcome_id is None:
        return "Outcome content already exists; could not record completion."
    try:
        await engine.relate(
            src_id=outcome_id, dst_id=task_id, edge_type="completed",
            metadata={"completed_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
        )
    except ValueError as e:
        return f"Outcome saved (id={outcome_id}) but completed edge failed: {e}"
    archived = await engine.archive([task_id])
    note = "" if archived else " (task already archived)"
    return f"Completed. outcome={outcome_id} → task={task_id[:8]}..{note}"


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
    reason_id, _ = await _save_memory(
        engine, content=reason, source="agent", tags=["abandoned-task"],
        emotion=-0.2, certainty=1.0,
    )
    if reason_id is None:
        return "Reason content already exists; could not record abandonment."
    try:
        await engine.relate(
            src_id=reason_id, dst_id=task_id, edge_type="abandoned",
            metadata={"abandoned_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
        )
    except ValueError as e:
        return f"Reason saved (id={reason_id}) but abandoned edge failed: {e}"
    await engine.archive([task_id])
    return f"Abandoned. reason={reason_id} → task={task_id[:8]}.."


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
    edge_type = "blocked_by" if blocking else "depends_on"
    try:
        edge = await engine.relate(
            src_id=task_id, dst_id=depends_on_id, edge_type=edge_type,
        )
    except ValueError as e:
        return f"Dependency could not be created: {e}"
    return f"{task_id[:8]}.. --[{edge.edge_type}]--> {depends_on_id[:8]}.."


@mcp.tool()
async def declare_value(content: str, certainty: float = 1.0) -> str:
    """Declare a deeply-held belief / value. Permanent (no TTL).

    Values form the bedrock that intentions and commitments derive from.
    Use sparingly — these are the things that, if you forgot them, you'd
    no longer recognize yourself.
    """
    engine = await get_engine()
    new_id, _ = await _save_memory(
        engine, content=content, source="value", tags=["value"],
        emotion=0.6, certainty=certainty,
    )
    if new_id is None:
        return "Value already declared (duplicate content)."
    return f"Value declared. ID: {new_id} (permanent)"


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
    new_id, _ = await _save_memory(
        engine, content=content, source="intention", tags=["intention"],
        emotion=0.5, certainty=certainty,
    )
    if new_id is None:
        return "Intention already declared (duplicate content)."
    if parent_value_id:
        try:
            await engine.relate(
                src_id=new_id, dst_id=parent_value_id, edge_type="derived_from",
            )
        except ValueError as e:
            return f"Intention created (id={new_id}) but derived_from edge failed: {e}"
    note = f", derived_from {parent_value_id[:8]}.." if parent_value_id else ""
    return f"Intention declared. ID: {new_id} (permanent{note})"


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
    new_id, metadata = await _save_memory(
        engine, content=content, source="commitment", tags=["commitment"],
        ttl_seconds=deadline_seconds, emotion=0.5, certainty=certainty,
    )
    if new_id is None:
        return "Commitment already declared (duplicate content)."
    try:
        await engine.relate(
            src_id=new_id, dst_id=parent_intention_id, edge_type="fulfills",
        )
    except ValueError as e:
        return f"Commitment created (id={new_id}) but fulfills edge failed: {e}"
    expires_at = metadata.get("expires_at", "permanent")
    return f"Commitment declared. ID: {new_id} (deadline {expires_at}, fulfills {parent_intention_id[:8]}..)"


@mcp.tool()
async def inherit_persona() -> str:
    """Generate a prose self-introduction from declared values, intentions,
    commitments, and recent activity.

    Call at session start to "wear" the persona accumulated across past
    sessions. Returns multi-paragraph text suitable for orienting a fresh
    Claude (or yourself, after a long break).
    """
    engine = await get_engine()
    cache = engine.cache

    values: list[tuple[str, str]] = []     # (id, content)
    intentions: list[tuple[str, str]] = []
    commitments: list[tuple[str, str, str]] = []  # (id, content, deadline)
    styles: list[tuple[str, str]] = []
    relationships: list[tuple[str, str, str]] = []  # (id, who, content)

    for state in cache.get_all_nodes():
        doc = await engine.store.get_document(state.id)
        if doc is None:
            continue
        meta = doc.get("metadata") or {}
        source = meta.get("source", "")
        content = doc.get("content", "")[:200].replace("\n", " ")
        if source == "value":
            values.append((state.id, content))
        elif source == "intention":
            intentions.append((state.id, content))
        elif source == "commitment":
            deadline = meta.get("expires_at", "permanent")
            commitments.append((state.id, content, deadline))
        elif source == "style":
            styles.append((state.id, content))
        elif source.startswith("relationship:"):
            who = source.split(":", 1)[1] or "?"
            relationships.append((state.id, who, content))

    parts: list[str] = ["# Persona inheritance\n"]

    if values:
        parts.append(f"## Values ({len(values)})")
        for vid, c in values[:8]:
            parts.append(f"- {c}  _(id={vid[:8]}..)_")
    else:
        parts.append("## Values\n_No values declared yet. `declare_value(...)` to seed the bedrock._")

    if intentions:
        parts.append(f"\n## Intentions ({len(intentions)})")
        for iid, c in intentions[:8]:
            parts.append(f"- {c}  _(id={iid[:8]}..)_")
    else:
        parts.append("\n## Intentions\n_No long-term direction declared yet._")

    if commitments:
        parts.append(f"\n## Active Commitments ({len(commitments)})")
        for cid, c, deadline in commitments[:8]:
            parts.append(f"- {c}  _(id={cid[:8]}.., deadline {deadline})_")

    if styles:
        parts.append(f"\n## Style ({len(styles)})")
        for sid, c in styles[:5]:
            parts.append(f"- {c}")

    if relationships:
        parts.append(f"\n## Relationships ({len(relationships)})")
        for rid, who, c in relationships[:8]:
            parts.append(f"- **{who}**: {c}")

    parts.append(
        "\n---\n_To add to this persona: `declare_value` / `declare_intention` "
        "/ `declare_commitment`, or `remember(source=\"style\", ...)` and "
        "`remember(source=\"relationship:<name>\", ...)`._"
    )
    return "\n".join(parts)


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
    outcomes = await engine.merge(node_ids, keep=keep)
    if not outcomes:
        return "Nothing to merge (need ≥2 active nodes from the given IDs)."
    lines = [f"Merged {len(outcomes)} node(s) into a survivor:"]
    for o in outcomes:
        lines.append(
            f"  {o.absorbed_id[:8]}.. → {o.survivor_id[:8]}.. "
            f"(mass {o.mass_before:.3f} + {o.absorbed_mass:.3f} = {o.mass_after:.3f})"
        )
    return "\n".join(lines)


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
    report = await engine.compact(
        expire_ttl=expire_ttl,
        rebuild_faiss=rebuild_faiss,
        auto_merge=auto_merge,
        merge_threshold=merge_threshold,
        merge_top_n=merge_top_n,
    )
    return (
        f"Compaction complete:\n"
        f"  TTL-expired:    {report['expired']}\n"
        f"  Auto-merged:    {report['merged_pairs']} pairs\n"
        f"  FAISS rebuilt:  {report['faiss_rebuilt']}\n"
        f"  FAISS vectors:  {report['vectors_before']} → {report['vectors_after']}"
    )


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
    cfg = engine.config
    candidates = extract_candidates(
        transcript,
        max_candidates=max_candidates,
        min_chars=cfg.auto_remember_min_chars,
        max_chars=cfg.auto_remember_max_chars,
    )
    if not candidates:
        return "No save-worthy candidates extracted from the transcript."

    lines = [f"Extracted {len(candidates)} candidate(s):"]
    for i, c in enumerate(candidates, start=1):
        tags = f", tags={list(c.suggested_tags)}" if c.suggested_tags else ""
        header = f"[{i}] score={c.score} source={c.suggested_source}{tags}"
        body = c.content
        block = f"{header}\n{body}"
        if include_reasons and c.reasons:
            block += f"\n  reasons: {', '.join(c.reasons)}"
        lines.append(block)
    lines.append(
        "\nReview and call `remember` for the ones you want to keep."
    )
    return "\n\n".join(lines)


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
    documents = ingest_path(path, source=source, recursive=recursive,
                            pattern=pattern, chunk_size=chunk_size)

    if not documents:
        return f"No documents found at {path}"

    ids = await engine.index_documents(documents)
    skipped = len(documents) - len(ids)
    return f"Ingested {len(ids)} documents from {path} (skipped {skipped} duplicates)"


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
