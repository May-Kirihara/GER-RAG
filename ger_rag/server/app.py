from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from ger_rag.config import GERConfig
from ger_rag.core.engine import GEREngine
from ger_rag.core.types import (
    GraphResponse,
    IndexedDoc,
    IndexRequest,
    IndexResponse,
    NodeResponse,
    QueryRequest,
    QueryResponse,
    ResetResponse,
)
from ger_rag.embedding.ruri import RuriEmbedder
from ger_rag.index.faiss_index import FaissIndex
from ger_rag.store.cache import CacheLayer
from ger_rag.store.sqlite_store import SqliteStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = GERConfig.from_config_file()

    logger.info("Loading embedding model: %s", config.model_name)
    embedder = RuriEmbedder(model_name=config.model_name, batch_size=config.batch_size)

    faiss_index = FaissIndex(dimension=config.embedding_dim)
    store = SqliteStore(db_path=config.db_path)
    cache = CacheLayer(
        flush_interval=config.flush_interval_seconds,
        flush_threshold=config.flush_threshold,
    )

    engine = GEREngine(
        config=config,
        embedder=embedder,
        faiss_index=faiss_index,
        cache=cache,
        store=store,
    )
    await engine.startup()
    app.state.engine = engine

    logger.info("GER-RAG server ready")
    yield

    logger.info("Shutting down GER-RAG server")
    await engine.shutdown()


app = FastAPI(title="GER-RAG", version="0.1.0", lifespan=lifespan)


def _get_engine() -> GEREngine:
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
    engine = _get_engine()
    results = await engine.query(
        text=request.text, top_k=request.top_k,
        wave_depth=request.wave_depth, wave_k=request.wave_k,
    )
    return QueryResponse(results=results, count=len(results))


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
