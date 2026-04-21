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


# --- Service layer requests (Phase S2 and beyond) ---

class RememberRequest(BaseModel):
    content: str = Field(..., min_length=1)
    source: str = "agent"
    tags: list[str] | None = None
    context: str | None = None
    ttl_seconds: float | None = None
    emotion: float = Field(default=0.0, ge=-1.0, le=1.0)
    certainty: float = Field(default=1.0, ge=0.0, le=1.0)


class RecallRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=5, ge=1, le=100)
    source_filter: list[str] | None = None
    wave_depth: int | None = Field(default=None, ge=0, le=5)
    wave_k: int | None = Field(default=None, ge=1, le=20)
    force_refresh: bool = False


class ExploreRequest(BaseModel):
    query: str = Field(..., min_length=1)
    diversity: float = Field(default=0.5, ge=0.0, le=1.0)
    top_k: int = Field(default=10, ge=1, le=100)


class ForgetRequest(BaseModel):
    node_ids: list[str] = Field(..., min_length=1)
    hard: bool = False


class RestoreRequest(BaseModel):
    node_ids: list[str] = Field(..., min_length=1)


class RevalidateRequest(BaseModel):
    node_id: str = Field(..., min_length=1)
    certainty: float | None = Field(default=None, ge=0.0, le=1.0)
    emotion: float | None = Field(default=None, ge=-1.0, le=1.0)


# --- Service layer responses (Phase S1 and beyond) ---

class MemoryItem(BaseModel):
    """Recall / explore result item with denormalized fields for display."""
    id: str
    content: str
    metadata: dict[str, Any] | None = None
    raw_score: float
    final_score: float
    source: str = "unknown"
    tags: list[str] = Field(default_factory=list)
    displacement_norm: float = 0.0


class RememberResponse(BaseModel):
    """Outcome of a single remember call."""
    id: str | None = None
    duplicate: bool = False
    expires_at: str | None = None  # ISO 8601 local time, for display


class ForgetResponse(BaseModel):
    affected: int
    requested: int
    hard: bool = False


class RestoreResponse(BaseModel):
    affected: int
    requested: int


class RevalidateResponse(BaseModel):
    found: bool
    id: str | None = None
    certainty: float | None = None
    emotion_weight: float | None = None


class RecallResponse(BaseModel):
    items: list[MemoryItem] = Field(default_factory=list)
    count: int = 0


class ExploreResponse(BaseModel):
    items: list[MemoryItem] = Field(default_factory=list)
    count: int = 0
    diversity: float = 0.0


# --- Relations service ---

class RelateRequest(BaseModel):
    src_id: str = Field(..., min_length=1)
    dst_id: str = Field(..., min_length=1)
    edge_type: str = Field(..., min_length=1)
    weight: float = 1.0
    metadata: dict[str, Any] | None = None


class UnrelateRequest(BaseModel):
    src_id: str = Field(..., min_length=1)
    dst_id: str = Field(..., min_length=1)
    edge_type: str | None = None


class RelateResponse(BaseModel):
    edge: DirectedEdge


class UnrelateResponse(BaseModel):
    removed: int
    src_id: str
    dst_id: str


class RelationsResponse(BaseModel):
    node_id: str
    direction: str
    edges: list[DirectedEdge] = Field(default_factory=list)
    count: int = 0


# --- Maintenance service ---

class MergeOutcomeItem(BaseModel):
    absorbed_id: str
    survivor_id: str
    mass_before: float
    absorbed_mass: float
    mass_after: float


class MergeRequest(BaseModel):
    node_ids: list[str] = Field(..., min_length=2)
    keep: str | None = None


class MergeResponse(BaseModel):
    outcomes: list[MergeOutcomeItem] = Field(default_factory=list)
    count: int = 0


class CompactRequest(BaseModel):
    expire_ttl: bool = True
    rebuild_faiss: bool = True
    auto_merge: bool = False
    merge_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    merge_top_n: int = Field(default=500, ge=1)


class CompactResponse(BaseModel):
    expired: int
    merged_pairs: int
    faiss_rebuilt: bool
    vectors_before: int
    vectors_after: int


class PrefetchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=5, ge=1, le=100)
    wave_depth: int | None = Field(default=None, ge=0, le=5)
    wave_k: int | None = Field(default=None, ge=1, le=20)


class PrefetchResponse(BaseModel):
    scheduled: bool
    query: str
    top_k: int
    ttl_seconds: float


class PrefetchStatusResponse(BaseModel):
    cache: dict[str, Any]
    pool: dict[str, Any]


# --- Ingest service ---

class IngestRequest(BaseModel):
    path: str = Field(..., min_length=1)
    source: str = "file"
    recursive: bool = False
    pattern: str = "*.md,*.txt"
    chunk_size: int = Field(default=2000, ge=100)


class IngestResponse(BaseModel):
    path: str
    ingested: int
    skipped: int
    found: int


# --- Auto-remember service ---

class AutoRememberRequest(BaseModel):
    transcript: str = Field(..., min_length=1)
    max_candidates: int = Field(default=5, ge=1, le=50)
    include_reasons: bool = True


class AutoRememberCandidate(BaseModel):
    content: str
    score: float
    suggested_source: str
    suggested_tags: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class AutoRememberResponse(BaseModel):
    candidates: list[AutoRememberCandidate] = Field(default_factory=list)
    count: int = 0


# --- Reflection service ---

class ReflectSummaryResponse(BaseModel):
    total_memories: int
    active_memories: int
    displaced_nodes: int
    total_edges: int
    sources: dict[str, int] = Field(default_factory=dict)


class ReflectNodeItem(BaseModel):
    id: str
    mass: float
    temperature: float = 0.0
    content_preview: str = ""


class ReflectHotTopicsResponse(BaseModel):
    items: list[ReflectNodeItem] = Field(default_factory=list)


class ReflectConnectionItem(BaseModel):
    src: str
    dst: str
    weight: float
    src_preview: str = ""
    dst_preview: str = ""


class ReflectConnectionsResponse(BaseModel):
    items: list[ReflectConnectionItem] = Field(default_factory=list)
    total: int = 0


class ReflectDormantItem(BaseModel):
    id: str
    age_days: float
    mass: float
    content_preview: str = ""


class ReflectDormantResponse(BaseModel):
    items: list[ReflectDormantItem] = Field(default_factory=list)


class ReflectDuplicateMember(BaseModel):
    id: str
    mass: float
    content_preview: str = ""


class ReflectDuplicateCluster(BaseModel):
    ids: list[str]
    avg_pairwise_similarity: float
    members: list[ReflectDuplicateMember] = Field(default_factory=list)


class ReflectDuplicatesResponse(BaseModel):
    clusters: list[ReflectDuplicateCluster] = Field(default_factory=list)
    threshold: float = 0.95


class ReflectRelationEdgeItem(BaseModel):
    src: str
    dst: str
    edge_type: str
    weight: float


class ReflectRelationsOverviewResponse(BaseModel):
    total: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)
    recent: list[ReflectRelationEdgeItem] = Field(default_factory=list)


class TaskSurfaceItem(BaseModel):
    id: str
    content: str
    deadline: str | None = None  # ISO or "permanent"
    days_left: float | None = None  # None when permanent


class ReflectTasksTodoResponse(BaseModel):
    total: int = 0
    items: list[TaskSurfaceItem] = Field(default_factory=list)


class TaskDoingItem(BaseModel):
    id: str
    content: str
    minutes_since_last_verify: float


class ReflectTasksDoingResponse(BaseModel):
    items: list[TaskDoingItem] = Field(default_factory=list)


class TaskOutcomePair(BaseModel):
    task_id: str
    task_preview: str
    other_id: str      # outcome_id for completed, reason_id for abandoned
    other_preview: str
    timestamp: str     # formatted local time


class ReflectTasksCompletedResponse(BaseModel):
    total: int = 0
    items: list[TaskOutcomePair] = Field(default_factory=list)


class ReflectTasksAbandonedResponse(BaseModel):
    total: int = 0
    items: list[TaskOutcomePair] = Field(default_factory=list)


class ReflectCommitmentsResponse(BaseModel):
    total: int = 0
    items: list[TaskSurfaceItem] = Field(default_factory=list)


class PersonaItem(BaseModel):
    id: str
    content: str


class ReflectValuesResponse(BaseModel):
    total: int = 0
    items: list[PersonaItem] = Field(default_factory=list)


class ReflectIntentionsResponse(BaseModel):
    total: int = 0
    items: list[PersonaItem] = Field(default_factory=list)


class RelationshipMemory(BaseModel):
    id: str
    content: str


class RelationshipEntry(BaseModel):
    who: str
    memories: list[RelationshipMemory] = Field(default_factory=list)


class ReflectRelationshipsResponse(BaseModel):
    total_people: int = 0
    total_memories: int = 0
    people: list[RelationshipEntry] = Field(default_factory=list)


class PersonaCommitmentItem(BaseModel):
    id: str
    content: str
    deadline: str = "permanent"


class RelationshipSnapshot(BaseModel):
    id: str
    who: str
    content: str


class PersonaSnapshotResponse(BaseModel):
    values: list[PersonaItem] = Field(default_factory=list)
    intentions: list[PersonaItem] = Field(default_factory=list)
    commitments: list[PersonaCommitmentItem] = Field(default_factory=list)
    styles: list[PersonaItem] = Field(default_factory=list)
    relationships: list[RelationshipSnapshot] = Field(default_factory=list)


# --- Phase D requests ---

class CommitRequest(BaseModel):
    content: str = Field(..., min_length=1)
    parent_id: str | None = None
    deadline_seconds: float | None = None
    certainty: float = Field(default=1.0, ge=0.0, le=1.0)


class CompleteRequest(BaseModel):
    """MCP-shaped request — task_id is part of the body."""
    task_id: str = Field(..., min_length=1)
    outcome: str = Field(..., min_length=1)
    emotion: float = Field(default=0.5, ge=-1.0, le=1.0)


class AbandonRequest(BaseModel):
    """MCP-shaped request — task_id is part of the body."""
    task_id: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)


class DependRequest(BaseModel):
    """MCP-shaped request — task_id is part of the body."""
    task_id: str = Field(..., min_length=1)
    depends_on_id: str = Field(..., min_length=1)
    blocking: bool = False


# REST-shaped bodies for /tasks/{id}/* endpoints — task_id comes from the path
# so the body must NOT carry it (avoids redundant client-side duplication).

class CompleteBody(BaseModel):
    outcome: str = Field(..., min_length=1)
    emotion: float = Field(default=0.5, ge=-1.0, le=1.0)


class AbandonBody(BaseModel):
    reason: str = Field(..., min_length=1)


class DependBody(BaseModel):
    depends_on_id: str = Field(..., min_length=1)
    blocking: bool = False


class DeclareValueRequest(BaseModel):
    content: str = Field(..., min_length=1)
    certainty: float = Field(default=1.0, ge=0.0, le=1.0)


class DeclareIntentionRequest(BaseModel):
    content: str = Field(..., min_length=1)
    parent_value_id: str | None = None
    certainty: float = Field(default=1.0, ge=0.0, le=1.0)


class DeclareCommitmentRequest(BaseModel):
    content: str = Field(..., min_length=1)
    parent_intention_id: str = Field(..., min_length=1)
    deadline_seconds: float | None = None
    certainty: float = Field(default=1.0, ge=0.0, le=1.0)


# --- Phase D responses ---

class CommitResponse(BaseModel):
    id: str | None = None
    duplicate: bool = False
    expires_at: str | None = None  # ISO string or None (permanent)
    parent_id: str | None = None
    edge_error: str | None = None


class StartResponse(BaseModel):
    found: bool
    id: str
    emotion_weight: float | None = None


class CompleteResponse(BaseModel):
    outcome_id: str | None = None
    task_id: str
    duplicate: bool = False
    edge_error: str | None = None
    task_already_archived: bool = False


class AbandonResponse(BaseModel):
    reason_id: str | None = None
    task_id: str
    duplicate: bool = False
    edge_error: str | None = None


class DependResponse(BaseModel):
    task_id: str
    depends_on_id: str
    edge_type: str
    error: str | None = None


class DeclareValueResponse(BaseModel):
    id: str | None = None
    duplicate: bool = False


class DeclareIntentionResponse(BaseModel):
    id: str | None = None
    duplicate: bool = False
    parent_value_id: str | None = None
    edge_error: str | None = None


class DeclareCommitmentResponse(BaseModel):
    id: str | None = None
    duplicate: bool = False
    parent_intention_id: str
    expires_at: str | None = None
    edge_error: str | None = None
