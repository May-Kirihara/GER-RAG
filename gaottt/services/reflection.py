"""Reflection service — analyze memory state across many aspects.

Each aspect is a standalone function returning a typed Pydantic response.
The MCP ``reflect`` tool dispatches to the right function and formatter; the
REST server exposes each aspect as its own endpoint.
"""
from __future__ import annotations

import time
from typing import Any

import numpy as np

from gaottt.core.engine import GaOTTTEngine
from gaottt.core.types import (
    PersonaCommitmentItem,
    PersonaItem,
    PersonaSnapshotResponse,
    RelationshipEntry,
    RelationshipMemory,
    RelationshipSnapshot,
    ReflectCommitmentsResponse,
    ReflectConnectionItem,
    ReflectConnectionsResponse,
    ReflectDormantItem,
    ReflectDormantResponse,
    ReflectDuplicateCluster,
    ReflectDuplicateMember,
    ReflectDuplicatesResponse,
    ReflectHotTopicsResponse,
    ReflectIntentionsResponse,
    ReflectNodeItem,
    ReflectRelationEdgeItem,
    ReflectRelationsOverviewResponse,
    ReflectRelationshipsResponse,
    ReflectSummaryResponse,
    ReflectTasksAbandonedResponse,
    ReflectTasksCompletedResponse,
    ReflectTasksDoingResponse,
    ReflectTasksTodoResponse,
    ReflectValuesResponse,
    TaskDoingItem,
    TaskOutcomePair,
    TaskSurfaceItem,
)


async def _content_of(engine: GaOTTTEngine, node_id: str, max_len: int = 120) -> str:
    doc = await engine.store.get_document(node_id)
    if doc is None:
        return "?"
    return (doc.get("content", "")[:max_len]).replace("\n", " ")


async def _gather_by_source(
    engine: GaOTTTEngine,
    sources: tuple[str, ...],
    prefix_match: bool = False,
) -> list[tuple[str, str, dict[str, Any]]]:
    """Return (node_id, content, metadata) for cached nodes whose source matches."""
    out: list[tuple[str, str, dict[str, Any]]] = []
    for state in engine.cache.get_all_nodes():
        doc = await engine.store.get_document(state.id)
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


# ----- Simple aspects -----

async def summary(engine: GaOTTTEngine) -> ReflectSummaryResponse:
    cache = engine.cache
    nodes = cache.get_all_nodes()
    edges = cache.get_all_edges()
    active = sum(1 for n in nodes if n.mass > 1.01)
    displaced = sum(
        1 for nid in cache.displacement_cache
        if np.linalg.norm(cache.displacement_cache[nid]) > 0.001
    )
    sources: dict[str, int] = {}
    for n in nodes:
        doc = await engine.store.get_document(n.id)
        if doc:
            s = (doc.get("metadata") or {}).get("source", "unknown")
            sources[s] = sources.get(s, 0) + 1
    return ReflectSummaryResponse(
        total_memories=len(nodes),
        active_memories=active,
        displaced_nodes=displaced,
        total_edges=len(edges),
        sources=sources,
    )


async def hot_topics(engine: GaOTTTEngine, limit: int = 10) -> ReflectHotTopicsResponse:
    nodes = sorted(engine.cache.get_all_nodes(), key=lambda n: n.mass, reverse=True)[:limit]
    items = []
    for n in nodes:
        doc = await engine.store.get_document(n.id)
        content = (doc.get("content", "")[:100] if doc else "?").replace("\n", " ")
        items.append(ReflectNodeItem(
            id=n.id, mass=n.mass, temperature=n.temperature, content_preview=content,
        ))
    return ReflectHotTopicsResponse(items=items)


async def connections(engine: GaOTTTEngine, limit: int = 10) -> ReflectConnectionsResponse:
    all_edges = engine.cache.get_all_edges()
    edges = sorted(all_edges, key=lambda e: e.weight, reverse=True)[:limit]
    items = []
    for e in edges:
        doc_s = await engine.store.get_document(e.src)
        doc_d = await engine.store.get_document(e.dst)
        s_text = (doc_s.get("content", "")[:50] if doc_s else "?").replace("\n", " ")
        d_text = (doc_d.get("content", "")[:50] if doc_d else "?").replace("\n", " ")
        items.append(ReflectConnectionItem(
            src=e.src, dst=e.dst, weight=e.weight,
            src_preview=s_text, dst_preview=d_text,
        ))
    return ReflectConnectionsResponse(items=items, total=len(all_edges))


async def dormant(engine: GaOTTTEngine, limit: int = 10) -> ReflectDormantResponse:
    now = time.time()
    nodes = sorted(engine.cache.get_all_nodes(), key=lambda n: n.last_access)[:limit]
    items = []
    for n in nodes:
        age_days = (now - n.last_access) / 86400
        doc = await engine.store.get_document(n.id)
        content = (doc.get("content", "")[:100] if doc else "?").replace("\n", " ")
        items.append(ReflectDormantItem(
            id=n.id, age_days=age_days, mass=n.mass, content_preview=content,
        ))
    return ReflectDormantResponse(items=items)


async def duplicates(
    engine: GaOTTTEngine,
    limit: int = 10,
    threshold: float = 0.95,
    top_n_by_mass: int = 500,
) -> ReflectDuplicatesResponse:
    clusters = engine.find_duplicates(threshold=threshold, top_n_by_mass=top_n_by_mass)
    out: list[ReflectDuplicateCluster] = []
    for c in clusters[:limit]:
        members = []
        for nid in c.ids:
            doc = await engine.store.get_document(nid)
            preview = (doc.get("content", "")[:80] if doc else "?").replace("\n", " ")
            state = engine.cache.get_node(nid)
            mass = state.mass if state else 0.0
            members.append(ReflectDuplicateMember(
                id=nid, mass=mass, content_preview=preview,
            ))
        out.append(ReflectDuplicateCluster(
            ids=list(c.ids),
            avg_pairwise_similarity=c.avg_pairwise_similarity,
            members=members,
        ))
    return ReflectDuplicatesResponse(clusters=out, threshold=threshold)


async def relations_overview(
    engine: GaOTTTEngine, limit: int = 10,
) -> ReflectRelationsOverviewResponse:
    edges = await engine.store.get_directed_edges()
    by_type: dict[str, int] = {}
    for e in edges:
        by_type[e.edge_type] = by_type.get(e.edge_type, 0) + 1
    recent_edges = sorted(edges, key=lambda e: e.created_at, reverse=True)[:limit]
    recent = [
        ReflectRelationEdgeItem(
            src=e.src, dst=e.dst, edge_type=e.edge_type, weight=e.weight,
        )
        for e in recent_edges
    ]
    return ReflectRelationsOverviewResponse(
        total=len(edges), by_type=by_type, recent=recent,
    )


# ----- Phase D: task aspects -----

async def tasks_todo(engine: GaOTTTEngine, limit: int = 10) -> ReflectTasksTodoResponse:
    now = time.time()
    cache = engine.cache
    store = engine.store
    tasks = await _gather_by_source(engine, ("task",))
    eligible: list[tuple[str, str, dict, float]] = []
    for tid, content, meta in tasks:
        inc = await store.get_directed_edges(node_id=tid, direction="in")
        if any(e.edge_type in ("completed", "abandoned") for e in inc):
            continue
        state = cache.get_node(tid)
        deadline = state.expires_at if state and state.expires_at else float("inf")
        eligible.append((tid, content, meta, deadline))
    eligible.sort(key=lambda t: t[3])
    items: list[TaskSurfaceItem] = []
    for tid, content, meta, dl in eligible[:limit]:
        dl_str = meta.get("expires_at", "permanent")
        days_left = (dl - now) / 86400 if dl != float("inf") else None
        items.append(TaskSurfaceItem(
            id=tid, content=content, deadline=dl_str, days_left=days_left,
        ))
    return ReflectTasksTodoResponse(total=len(eligible), items=items)


async def tasks_doing(engine: GaOTTTEngine, limit: int = 10) -> ReflectTasksDoingResponse:
    now = time.time()
    threshold = now - 3600
    cache = engine.cache
    tasks = await _gather_by_source(engine, ("task",))
    active: list[tuple[str, str, float]] = []
    for tid, content, _meta in tasks:
        state = cache.get_node(tid)
        if state and state.last_verified_at and state.last_verified_at >= threshold:
            active.append((tid, content, state.last_verified_at))
    active.sort(key=lambda t: t[2], reverse=True)
    items = [
        TaskDoingItem(
            id=tid,
            content=content,
            minutes_since_last_verify=(now - lva) / 60,
        )
        for tid, content, lva in active[:limit]
    ]
    return ReflectTasksDoingResponse(items=items)


async def _task_outcome_pairs(
    engine: GaOTTTEngine, edge_type: str, limit: int,
) -> tuple[int, list[TaskOutcomePair]]:
    edges = await engine.store.get_directed_edges(edge_type=edge_type)
    edges.sort(key=lambda e: e.created_at, reverse=True)
    items: list[TaskOutcomePair] = []
    for e in edges[:limit]:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.created_at))
        task_preview = await _content_of(engine, e.dst, max_len=80)
        other_preview = await _content_of(
            engine, e.src, max_len=80 if edge_type == "completed" else 120,
        )
        items.append(TaskOutcomePair(
            task_id=e.dst, task_preview=task_preview,
            other_id=e.src, other_preview=other_preview,
            timestamp=ts,
        ))
    return len(edges), items


async def tasks_completed(
    engine: GaOTTTEngine, limit: int = 10,
) -> ReflectTasksCompletedResponse:
    total, items = await _task_outcome_pairs(engine, "completed", limit)
    return ReflectTasksCompletedResponse(total=total, items=items)


async def tasks_abandoned(
    engine: GaOTTTEngine, limit: int = 10,
) -> ReflectTasksAbandonedResponse:
    total, items = await _task_outcome_pairs(engine, "abandoned", limit)
    return ReflectTasksAbandonedResponse(total=total, items=items)


# ----- Phase D: persona aspects -----

async def commitments(
    engine: GaOTTTEngine, limit: int = 10,
) -> ReflectCommitmentsResponse:
    now = time.time()
    cache = engine.cache
    rows = await _gather_by_source(engine, ("commitment",))
    annotated: list[tuple[str, str, dict, float]] = []
    for cid, content, meta in rows:
        state = cache.get_node(cid)
        deadline = state.expires_at if state and state.expires_at else float("inf")
        annotated.append((cid, content, meta, deadline))
    annotated.sort(key=lambda t: t[3])
    items: list[TaskSurfaceItem] = []
    for cid, content, meta, dl in annotated[:limit]:
        dl_str = meta.get("expires_at", "permanent")
        days_left = (dl - now) / 86400 if dl != float("inf") else None
        items.append(TaskSurfaceItem(
            id=cid, content=content, deadline=dl_str, days_left=days_left,
        ))
    return ReflectCommitmentsResponse(total=len(annotated), items=items)


async def intentions(
    engine: GaOTTTEngine, limit: int = 10,
) -> ReflectIntentionsResponse:
    rows = await _gather_by_source(engine, ("intention",))
    total = len(rows)
    items = [PersonaItem(id=rid, content=content) for rid, content, _m in rows[:limit]]
    return ReflectIntentionsResponse(total=total, items=items)


async def values_(engine: GaOTTTEngine, limit: int = 10) -> ReflectValuesResponse:
    rows = await _gather_by_source(engine, ("value",))
    total = len(rows)
    items = [PersonaItem(id=vid, content=content) for vid, content, _m in rows[:limit]]
    return ReflectValuesResponse(total=total, items=items)


async def relationships(
    engine: GaOTTTEngine, limit: int = 10,
) -> ReflectRelationshipsResponse:
    rows = await _gather_by_source(engine, ("relationship:",), prefix_match=True)
    by_who: dict[str, list[RelationshipMemory]] = {}
    for rid, content, meta in rows:
        who = (meta.get("source", "relationship:?").split(":", 1)[1] or "?")
        by_who.setdefault(who, []).append(RelationshipMemory(id=rid, content=content))
    ordered = sorted(by_who.items(), key=lambda kv: -len(kv[1]))[:limit]
    people = [
        RelationshipEntry(who=who, memories=memories)
        for who, memories in ordered
    ]
    return ReflectRelationshipsResponse(
        total_people=len(by_who),
        total_memories=len(rows),
        people=people,
    )


async def persona_snapshot(engine: GaOTTTEngine) -> PersonaSnapshotResponse:
    """Composite self-introduction — same source as ``inherit_persona``."""
    cache = engine.cache
    values: list[PersonaItem] = []
    intents: list[PersonaItem] = []
    commits: list[PersonaCommitmentItem] = []
    styles: list[PersonaItem] = []
    rels: list[RelationshipSnapshot] = []

    for state in cache.get_all_nodes():
        doc = await engine.store.get_document(state.id)
        if doc is None:
            continue
        meta = doc.get("metadata") or {}
        source = meta.get("source", "")
        content = doc.get("content", "")[:200].replace("\n", " ")
        if source == "value":
            values.append(PersonaItem(id=state.id, content=content))
        elif source == "intention":
            intents.append(PersonaItem(id=state.id, content=content))
        elif source == "commitment":
            deadline = meta.get("expires_at", "permanent")
            commits.append(PersonaCommitmentItem(
                id=state.id, content=content, deadline=deadline,
            ))
        elif source == "style":
            styles.append(PersonaItem(id=state.id, content=content))
        elif source.startswith("relationship:"):
            who = source.split(":", 1)[1] or "?"
            rels.append(RelationshipSnapshot(id=state.id, who=who, content=content))

    return PersonaSnapshotResponse(
        values=values,
        intentions=intents,
        commitments=commits,
        styles=styles,
        relationships=rels,
    )
