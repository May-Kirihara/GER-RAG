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
        "Use 'remember' to store knowledge, 'recall' to search with gravitational relevance, "
        "'explore' for serendipitous discovery, 'reflect' to analyze your memory state, "
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
) -> str:
    """Store knowledge in long-term memory.

    Use this to save important insights, decisions, user preferences,
    or conversation summaries (compaction) for future recall.

    Args:
        content: Text to remember
        source: Origin — "agent" (your thoughts), "user" (user input),
                "system" (system info), "compaction" (context compression)
        tags: Classification tags (optional)
        context: Brief description of when/why this was saved (optional)
    """
    engine = await get_engine()
    metadata = {"source": source}
    if tags:
        metadata["tags"] = tags
    if context:
        metadata["context"] = context
    metadata["remembered_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    ids = await engine.index_documents([{"content": content, "metadata": metadata}])
    if not ids:
        return "Already exists in memory (duplicate content)."
    return f"Remembered. ID: {ids[0]}"


@mcp.tool()
async def recall(
    query: str,
    top_k: int = 5,
    source_filter: list[str] | None = None,
    wave_depth: int | None = None,
    wave_k: int | None = None,
) -> str:
    """Search long-term memory with gravitational wave propagation.

    Gravity waves propagate recursively through the knowledge space.
    High-mass memories attract more neighbors, creating wider gravitational fields.

    Args:
        query: Search query
        top_k: Number of results (default 5)
        source_filter: Filter by source, e.g. ["agent", "compaction"]
        wave_depth: Override recursion depth (default from config)
        wave_k: Override initial seed count (default from config)
    """
    engine = await get_engine()
    results = await engine.query(
        text=query, top_k=top_k * 2 if source_filter else top_k,
        wave_depth=wave_depth, wave_k=wave_k,
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
            f"[{i+1}] (score={r.final_score:.4f}, raw={r.raw_score:.4f}, "
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
                "connections" (strong co-occurrence edges), "dormant" (forgotten memories)
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
            lines.append(f"  mass={n.mass:.2f} temp={n.temperature:.6f} | {content}...")
        return "\n".join(lines)

    elif aspect == "connections":
        edges = sorted(cache.get_all_edges(), key=lambda e: e.weight, reverse=True)[:limit]
        lines = [f"Strongest connections ({len(edges)} shown):"]
        for e in edges:
            doc_s = await engine.store.get_document(e.src)
            doc_d = await engine.store.get_document(e.dst)
            s_text = (doc_s.get("content", "")[:50] if doc_s else "?").replace("\n", " ")
            d_text = (doc_d.get("content", "")[:50] if doc_d else "?").replace("\n", " ")
            lines.append(f"  weight={e.weight:.1f}: [{s_text}...] <-> [{d_text}...]")
        return "\n".join(lines)

    elif aspect == "dormant":
        nodes = sorted(cache.get_all_nodes(), key=lambda n: n.last_access)[:limit]
        lines = ["Dormant memories (longest since last access):"]
        for n in nodes:
            age_days = (now - n.last_access) / 86400
            doc = await engine.store.get_document(n.id)
            content = (doc.get("content", "")[:100] if doc else "?").replace("\n", " ")
            lines.append(f"  {age_days:.1f} days ago, mass={n.mass:.2f} | {content}...")
        return "\n".join(lines)

    return f"Unknown aspect: {aspect}"


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
