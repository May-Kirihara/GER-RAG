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
    return_count: float = 0.0
    expires_at: float | None = None
    is_archived: bool = False
    merged_into: str | None = None
    merge_count: int = 0
    merged_at: float | None = None
    emotion_weight: float = 0.0
    certainty: float = 1.0
    last_verified_at: float | None = None


class CooccurrenceEdge(BaseModel):
    src: str
    dst: str
    weight: float = 0.0
    last_update: float = Field(default_factory=time.time)


class DirectedEdge(BaseModel):
    """Typed directed relation between two memories (F3).

    edge_type:
      - "supersedes"   — src replaced/retracted dst (newer overrides older)
      - "derived_from" — src is an extension/derivation of dst
      - "contradicts"  — src disagrees with dst (mutual exclusion candidate)
    """
    src: str
    dst: str
    edge_type: str
    weight: float = 1.0
    created_at: float = Field(default_factory=time.time)
    metadata: dict[str, Any] | None = None


KNOWN_EDGE_TYPES: tuple[str, ...] = (
    # Phase B (F3)
    "supersedes", "derived_from", "contradicts",
    # Phase D — task & persona layer
    "completed",    # outcome → task
    "abandoned",    # reason → task
    "depends_on",   # task → task
    "blocked_by",   # task → blocker (specialised depends_on)
    "working_on",   # session_marker → task (active engagement)
    "fulfills",     # task → commitment, commitment → intention, intention → value
)


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
