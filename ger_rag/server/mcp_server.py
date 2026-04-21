"""GER-RAG MCP Server — AI Agent Long-Term Memory

Provides gravitational displacement-powered memory for AI agents.

Usage:
    # stdio (Claude Code / Claude Desktop)
    python -m ger_rag.server.mcp_server

    # SSE (remote clients)
    python -m ger_rag.server.mcp_server --transport sse --port 8001
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

from ger_rag.config import GERConfig
from ger_rag.core.engine import GEREngine
from ger_rag.core.extractor import extract_candidates
from ger_rag.embedding.ruri import RuriEmbedder
from ger_rag.index.faiss_index import FaissIndex
from ger_rag.ingest.loader import ingest_path
from ger_rag.store.cache import CacheLayer
from ger_rag.store.sqlite_store import SqliteStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Engine singleton ---

_engine: GEREngine | None = None
_engine_lock = asyncio.Lock()


async def get_engine() -> GEREngine:
    global _engine
    if _engine is not None:
        return _engine
    async with _engine_lock:
        if _engine is not None:
            return _engine
        config = GERConfig.from_config_file()
        logger.info("Initializing GER-RAG engine for MCP server...")
        embedder = RuriEmbedder(model_name=config.model_name, batch_size=config.batch_size)
        faiss_index = FaissIndex(dimension=config.embedding_dim)
        store = SqliteStore(db_path=config.db_path)
        cache = CacheLayer(
            flush_interval=config.flush_interval_seconds,
            flush_threshold=config.flush_threshold,
        )
        engine = GEREngine(
            config=config, embedder=embedder, faiss_index=faiss_index,
            cache=cache, store=store,
        )
        await engine.startup()
        _engine = engine
        logger.info("GER-RAG engine ready (%d nodes, %d vectors)", len(cache.node_cache), faiss_index.size)
        return engine


# --- MCP Server ---

mcp = FastMCP(
    "ger-rag-memory",
    instructions=(
        "GER-RAG: Gravitational long-term memory for AI agents. "
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
        "directed edges (supersedes/derived_from/contradicts), and 'ingest' "
        "to bulk-load files."
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
    metadata = {"source": source}
    if tags:
        metadata["tags"] = tags
    if context:
        metadata["context"] = context
    metadata["remembered_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    expires_at: float | None = None
    if ttl_seconds is not None:
        expires_at = time.time() + ttl_seconds
    elif source == "hypothesis":
        expires_at = time.time() + engine.config.default_hypothesis_ttl_seconds

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
    if not ids:
        return "Already exists in memory (duplicate content)."
    suffix = f" (expires {metadata['expires_at']})" if expires_at else ""
    return f"Remembered. ID: {ids[0]}{suffix}"


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
        aspect: "summary" (overview), "hot_topics" (high-mass memories),
                "connections" (strong co-occurrence edges), "dormant" (forgotten memories),
                "duplicates" (near-duplicate clusters; pass to merge() to collide),
                "relations" (directed typed edges: supersedes / derived_from / contradicts)
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
        f"They are ranked by GER-RAG's gravitational model based on past access patterns."
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
