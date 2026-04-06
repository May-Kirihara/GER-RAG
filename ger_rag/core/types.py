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


class CooccurrenceEdge(BaseModel):
    src: str
    dst: str
    weight: float = 0.0
    last_update: float = Field(default_factory=time.time)


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


class QueryResultItem(BaseModel):
    id: str
    content: str
    metadata: dict[str, Any] | None
    raw_score: float
    final_score: float


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


class GraphResponse(BaseModel):
    edges: list[CooccurrenceEdge]
    count: int


class ResetResponse(BaseModel):
    reset: bool = True
    nodes_reset: int
    edges_removed: int
