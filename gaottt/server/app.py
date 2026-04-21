from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.core.types import (
    AbandonBody,
    AbandonResponse,
    AutoRememberRequest,
    AutoRememberResponse,
    CommitRequest,
    CommitResponse,
    CompactRequest,
    CompactResponse,
    CompleteBody,
    CompleteResponse,
    DeclareCommitmentRequest,
    DeclareCommitmentResponse,
    DeclareIntentionRequest,
    DeclareIntentionResponse,
    DeclareValueRequest,
    DeclareValueResponse,
    DependBody,
    DependResponse,
    ExploreRequest,
    ExploreResponse,
    ForgetRequest,
    ForgetResponse,
    GraphResponse,
    IndexedDoc,
    IndexRequest,
    IndexResponse,
    IngestRequest,
    IngestResponse,
    MergeRequest,
    MergeResponse,
    NodeResponse,
    PersonaSnapshotResponse,
    PrefetchRequest,
    PrefetchResponse,
    PrefetchStatusResponse,
    QueryRequest,
    QueryResponse,
    QueryResultItem,
    RecallRequest,
    RecallResponse,
    ReflectCommitmentsResponse,
    ReflectConnectionsResponse,
    ReflectDormantResponse,
    ReflectDuplicatesResponse,
    ReflectHotTopicsResponse,
    ReflectIntentionsResponse,
    ReflectRelationsOverviewResponse,
    ReflectRelationshipsResponse,
    ReflectSummaryResponse,
    ReflectTasksAbandonedResponse,
    ReflectTasksCompletedResponse,
    ReflectTasksDoingResponse,
    ReflectTasksTodoResponse,
    ReflectValuesResponse,
    RelateRequest,
    RelateResponse,
    RelationsResponse,
    RememberRequest,
    RememberResponse,
    ResetResponse,
    RestoreRequest,
    RestoreResponse,
    RevalidateRequest,
    RevalidateResponse,
    StartResponse,
    UnrelateResponse,
)
from gaottt.services import (
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = GaOTTTConfig.from_config_file()

    logger.info("Loading embedding model: %s", config.model_name)
    engine = build_engine(config)
    await engine.startup()
    app.state.engine = engine

    logger.info("GaOTTT server ready")
    yield

    logger.info("Shutting down GaOTTT server")
    await engine.shutdown()


app = FastAPI(title="GaOTTT", version="0.1.0", lifespan=lifespan)


def _get_engine() -> GaOTTTEngine:
    return app.state.engine


# --- POST /index ---

@app.post("/index", response_model=IndexResponse)
async def index_documents(request: IndexRequest):
    engine = _get_engine()
    docs = [{"content": d.content, "metadata": d.metadata} for d in request.documents]
    total = len(docs)
    ids = await engine.index_documents(docs)
    return IndexResponse(
        indexed=[IndexedDoc(id=doc_id) for doc_id in ids],
        count=len(ids),
        skipped=total - len(ids),
    )


# --- POST /query ---

@app.post("/query", response_model=QueryResponse)
async def query_documents(request: QueryRequest):
    """Legacy Phase A query endpoint. Prefer POST /recall for new clients —
    it exposes source/tags/displacement and the prefetch cache."""
    engine = _get_engine()
    result = await memory_service.recall(
        engine, query=request.text, top_k=request.top_k,
        wave_depth=request.wave_depth, wave_k=request.wave_k,
        force_refresh=True,  # legacy /query never consulted cache
    )
    legacy_items = [
        QueryResultItem(
            id=item.id,
            content=item.content,
            metadata=item.metadata,
            raw_score=item.raw_score,
            final_score=item.final_score,
        )
        for item in result.items
    ]
    return QueryResponse(results=legacy_items, count=result.count)


# --- POST /remember ---

@app.post("/remember", response_model=RememberResponse)
async def remember_memory(request: RememberRequest):
    engine = _get_engine()
    return await memory_service.remember(
        engine,
        content=request.content,
        source=request.source,
        tags=request.tags,
        context=request.context,
        ttl_seconds=request.ttl_seconds,
        emotion=request.emotion,
        certainty=request.certainty,
    )


# --- POST /recall ---

@app.post("/recall", response_model=RecallResponse)
async def recall_memory(request: RecallRequest):
    engine = _get_engine()
    return await memory_service.recall(
        engine,
        query=request.query,
        top_k=request.top_k,
        source_filter=request.source_filter,
        wave_depth=request.wave_depth,
        wave_k=request.wave_k,
        force_refresh=request.force_refresh,
    )


# --- POST /explore ---

@app.post("/explore", response_model=ExploreResponse)
async def explore_memory(request: ExploreRequest):
    engine = _get_engine()
    return await memory_service.explore(
        engine,
        query=request.query,
        diversity=request.diversity,
        top_k=request.top_k,
    )


# --- POST /forget ---

@app.post("/forget", response_model=ForgetResponse)
async def forget_memory(request: ForgetRequest):
    engine = _get_engine()
    return await memory_service.forget(
        engine, node_ids=request.node_ids, hard=request.hard,
    )


# --- POST /restore ---

@app.post("/restore", response_model=RestoreResponse)
async def restore_memory(request: RestoreRequest):
    engine = _get_engine()
    return await memory_service.restore(engine, node_ids=request.node_ids)


# --- POST /revalidate ---

@app.post("/revalidate", response_model=RevalidateResponse)
async def revalidate_memory(request: RevalidateRequest):
    engine = _get_engine()
    result = await memory_service.revalidate(
        engine,
        node_id=request.node_id,
        certainty=request.certainty,
        emotion=request.emotion,
    )
    if not result.found:
        raise HTTPException(status_code=404, detail=f"Node {request.node_id} not found or archived")
    return result


# --- GET /node/{node_id} ---

@app.get("/node/{node_id}", response_model=NodeResponse)
async def get_node(node_id: str):
    engine = _get_engine()
    state = await engine.get_node_state(node_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Node not found")
    return NodeResponse(
        id=state.id,
        mass=state.mass,
        temperature=state.temperature,
        last_access=state.last_access,
        sim_history=state.sim_history,
        displacement_norm=engine.get_displacement_norm(node_id),
    )


# --- GET /graph ---

@app.get("/graph", response_model=GraphResponse)
async def get_graph(min_weight: float = 0.0, node_id: str | None = None):
    engine = _get_engine()
    edges = engine.get_graph(min_weight=min_weight, node_id=node_id)
    return GraphResponse(edges=edges, count=len(edges))


# --- POST /reset ---

@app.post("/reset", response_model=ResetResponse)
async def reset_state():
    engine = _get_engine()
    nodes_reset, edges_removed = await engine.reset()
    return ResetResponse(nodes_reset=nodes_reset, edges_removed=edges_removed)


# -----------------------------------------------------------------------
# Relations
# -----------------------------------------------------------------------

@app.post("/relations", response_model=RelateResponse)
async def create_relation(request: RelateRequest):
    engine = _get_engine()
    try:
        return await relations_service.relate(
            engine,
            src_id=request.src_id,
            dst_id=request.dst_id,
            edge_type=request.edge_type,
            weight=request.weight,
            metadata=request.metadata,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.delete("/relations", response_model=UnrelateResponse)
async def delete_relation(
    src_id: str, dst_id: str, edge_type: str | None = None,
):
    engine = _get_engine()
    return await relations_service.unrelate(
        engine, src_id=src_id, dst_id=dst_id, edge_type=edge_type,
    )


@app.get("/relations/{node_id}", response_model=RelationsResponse)
async def list_relations(
    node_id: str,
    edge_type: str | None = None,
    direction: str = "out",
):
    engine = _get_engine()
    return await relations_service.get_relations(
        engine, node_id=node_id, edge_type=edge_type, direction=direction,
    )


# -----------------------------------------------------------------------
# Maintenance
# -----------------------------------------------------------------------

@app.post("/merge", response_model=MergeResponse)
async def merge_memories(request: MergeRequest):
    engine = _get_engine()
    return await maintenance_service.merge(
        engine, node_ids=request.node_ids, keep=request.keep,
    )


@app.post("/compact", response_model=CompactResponse)
async def compact_memory(request: CompactRequest):
    engine = _get_engine()
    return await maintenance_service.compact(
        engine,
        expire_ttl=request.expire_ttl,
        rebuild_faiss=request.rebuild_faiss,
        auto_merge=request.auto_merge,
        merge_threshold=request.merge_threshold,
        merge_top_n=request.merge_top_n,
    )


@app.post("/prefetch", response_model=PrefetchResponse)
async def schedule_prefetch(request: PrefetchRequest):
    engine = _get_engine()
    return maintenance_service.prefetch(
        engine,
        query=request.query,
        top_k=request.top_k,
        wave_depth=request.wave_depth,
        wave_k=request.wave_k,
    )


@app.get("/prefetch/status", response_model=PrefetchStatusResponse)
async def prefetch_status_endpoint():
    engine = _get_engine()
    return maintenance_service.prefetch_status(engine)


# -----------------------------------------------------------------------
# Ingest
# -----------------------------------------------------------------------

@app.post("/ingest", response_model=IngestResponse)
async def ingest_files(request: IngestRequest):
    engine = _get_engine()
    return await ingest_service.ingest(
        engine,
        path=request.path,
        source=request.source,
        recursive=request.recursive,
        pattern=request.pattern,
        chunk_size=request.chunk_size,
    )


# -----------------------------------------------------------------------
# Auto-remember
# -----------------------------------------------------------------------

@app.post("/auto_remember", response_model=AutoRememberResponse)
async def auto_remember_endpoint(request: AutoRememberRequest):
    engine = _get_engine()
    return await memory_service.auto_remember(
        engine,
        transcript=request.transcript,
        max_candidates=request.max_candidates,
        include_reasons=request.include_reasons,
    )


# -----------------------------------------------------------------------
# Reflection — one typed endpoint per aspect
# -----------------------------------------------------------------------

@app.post("/reflect/summary", response_model=ReflectSummaryResponse)
async def reflect_summary():
    return await reflection_service.summary(_get_engine())


@app.post("/reflect/hot_topics", response_model=ReflectHotTopicsResponse)
async def reflect_hot_topics(limit: int = 10):
    return await reflection_service.hot_topics(_get_engine(), limit=limit)


@app.post("/reflect/connections", response_model=ReflectConnectionsResponse)
async def reflect_connections(limit: int = 10):
    return await reflection_service.connections(_get_engine(), limit=limit)


@app.post("/reflect/dormant", response_model=ReflectDormantResponse)
async def reflect_dormant(limit: int = 10):
    return await reflection_service.dormant(_get_engine(), limit=limit)


@app.post("/reflect/duplicates", response_model=ReflectDuplicatesResponse)
async def reflect_duplicates(
    limit: int = 10, threshold: float = 0.95, top_n_by_mass: int = 500,
):
    return await reflection_service.duplicates(
        _get_engine(), limit=limit, threshold=threshold, top_n_by_mass=top_n_by_mass,
    )


@app.post("/reflect/relations", response_model=ReflectRelationsOverviewResponse)
async def reflect_relations_overview(limit: int = 10):
    return await reflection_service.relations_overview(_get_engine(), limit=limit)


@app.post("/reflect/tasks_todo", response_model=ReflectTasksTodoResponse)
async def reflect_tasks_todo(limit: int = 10):
    return await reflection_service.tasks_todo(_get_engine(), limit=limit)


@app.post("/reflect/tasks_doing", response_model=ReflectTasksDoingResponse)
async def reflect_tasks_doing(limit: int = 10):
    return await reflection_service.tasks_doing(_get_engine(), limit=limit)


@app.post("/reflect/tasks_completed", response_model=ReflectTasksCompletedResponse)
async def reflect_tasks_completed(limit: int = 10):
    return await reflection_service.tasks_completed(_get_engine(), limit=limit)


@app.post("/reflect/tasks_abandoned", response_model=ReflectTasksAbandonedResponse)
async def reflect_tasks_abandoned(limit: int = 10):
    return await reflection_service.tasks_abandoned(_get_engine(), limit=limit)


@app.post("/reflect/commitments", response_model=ReflectCommitmentsResponse)
async def reflect_commitments(limit: int = 10):
    return await reflection_service.commitments(_get_engine(), limit=limit)


@app.post("/reflect/intentions", response_model=ReflectIntentionsResponse)
async def reflect_intentions(limit: int = 10):
    return await reflection_service.intentions(_get_engine(), limit=limit)


@app.post("/reflect/values", response_model=ReflectValuesResponse)
async def reflect_values(limit: int = 10):
    return await reflection_service.values_(_get_engine(), limit=limit)


@app.post("/reflect/relationships", response_model=ReflectRelationshipsResponse)
async def reflect_relationships(limit: int = 10):
    return await reflection_service.relationships(_get_engine(), limit=limit)


@app.post("/reflect/persona", response_model=PersonaSnapshotResponse)
async def reflect_persona():
    return await reflection_service.persona_snapshot(_get_engine())


# -----------------------------------------------------------------------
# Phase D — Tasks
# -----------------------------------------------------------------------

@app.post("/tasks", response_model=CommitResponse)
async def create_task(request: CommitRequest):
    engine = _get_engine()
    return await phase_d_service.commit(
        engine,
        content=request.content,
        parent_id=request.parent_id,
        deadline_seconds=request.deadline_seconds,
        certainty=request.certainty,
    )


@app.post("/tasks/{task_id}/start", response_model=StartResponse)
async def start_task(task_id: str):
    engine = _get_engine()
    result = await phase_d_service.start(engine, task_id=task_id)
    if not result.found:
        raise HTTPException(
            status_code=404, detail=f"Task {task_id} not found or archived",
        )
    return result


@app.post("/tasks/{task_id}/complete", response_model=CompleteResponse)
async def complete_task(task_id: str, request: CompleteBody):
    engine = _get_engine()
    return await phase_d_service.complete(
        engine, task_id=task_id, outcome=request.outcome, emotion=request.emotion,
    )


@app.post("/tasks/{task_id}/abandon", response_model=AbandonResponse)
async def abandon_task(task_id: str, request: AbandonBody):
    engine = _get_engine()
    return await phase_d_service.abandon(
        engine, task_id=task_id, reason=request.reason,
    )


@app.post("/tasks/{task_id}/depend", response_model=DependResponse)
async def add_task_dependency(task_id: str, request: DependBody):
    engine = _get_engine()
    return await phase_d_service.depend(
        engine,
        task_id=task_id,
        depends_on_id=request.depends_on_id,
        blocking=request.blocking,
    )


# -----------------------------------------------------------------------
# Phase D — Persona
# -----------------------------------------------------------------------

@app.post("/persona/values", response_model=DeclareValueResponse)
async def declare_value_endpoint(request: DeclareValueRequest):
    engine = _get_engine()
    return await phase_d_service.declare_value(
        engine, content=request.content, certainty=request.certainty,
    )


@app.post("/persona/intentions", response_model=DeclareIntentionResponse)
async def declare_intention_endpoint(request: DeclareIntentionRequest):
    engine = _get_engine()
    return await phase_d_service.declare_intention(
        engine,
        content=request.content,
        parent_value_id=request.parent_value_id,
        certainty=request.certainty,
    )


@app.post("/persona/commitments", response_model=DeclareCommitmentResponse)
async def declare_commitment_endpoint(request: DeclareCommitmentRequest):
    engine = _get_engine()
    return await phase_d_service.declare_commitment(
        engine,
        content=request.content,
        parent_intention_id=request.parent_intention_id,
        deadline_seconds=request.deadline_seconds,
        certainty=request.certainty,
    )


@app.get("/persona", response_model=PersonaSnapshotResponse)
async def inherit_persona_endpoint():
    engine = _get_engine()
    return await phase_d_service.inherit_persona(engine)
