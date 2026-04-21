"""Phase D service — task & persona layer.

Tasks: commit / start / complete / abandon / depend
Persona: declare_value / declare_intention / declare_commitment / inherit_persona
"""
from __future__ import annotations

import time

from gaottt.core.engine import GaOTTTEngine
from gaottt.core.types import (
    AbandonResponse,
    CommitResponse,
    CompleteResponse,
    DeclareCommitmentResponse,
    DeclareIntentionResponse,
    DeclareValueResponse,
    DependResponse,
    PersonaSnapshotResponse,
    StartResponse,
)
from gaottt.services.memory import save_memory
from gaottt.services.reflection import persona_snapshot as _persona_snapshot


async def commit(
    engine: GaOTTTEngine,
    content: str,
    parent_id: str | None = None,
    deadline_seconds: float | None = None,
    certainty: float = 1.0,
) -> CommitResponse:
    new_id, metadata = await save_memory(
        engine, content=content, source="task", tags=["todo"],
        ttl_seconds=deadline_seconds, certainty=certainty,
    )
    if new_id is None:
        return CommitResponse(duplicate=True)

    edge_error: str | None = None
    if parent_id:
        try:
            await engine.relate(
                src_id=new_id, dst_id=parent_id, edge_type="fulfills",
                metadata={"declared_at": metadata["remembered_at"]},
            )
        except ValueError as e:
            edge_error = str(e)

    return CommitResponse(
        id=new_id,
        duplicate=False,
        expires_at=metadata.get("expires_at"),
        parent_id=parent_id,
        edge_error=edge_error,
    )


async def start(engine: GaOTTTEngine, task_id: str) -> StartResponse:
    state = await engine.revalidate(task_id, certainty=1.0, emotion=0.4)
    if state is None:
        return StartResponse(found=False, id=task_id)
    return StartResponse(
        found=True, id=task_id, emotion_weight=state.emotion_weight,
    )


async def complete(
    engine: GaOTTTEngine,
    task_id: str,
    outcome: str,
    emotion: float = 0.5,
) -> CompleteResponse:
    outcome_id, _ = await save_memory(
        engine, content=outcome, source="agent", tags=["completed-task"],
        emotion=emotion, certainty=1.0,
    )
    if outcome_id is None:
        return CompleteResponse(task_id=task_id, duplicate=True)

    edge_error: str | None = None
    try:
        await engine.relate(
            src_id=outcome_id, dst_id=task_id, edge_type="completed",
            metadata={"completed_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
        )
    except ValueError as e:
        return CompleteResponse(
            outcome_id=outcome_id, task_id=task_id, edge_error=str(e),
        )

    archived = await engine.archive([task_id])
    return CompleteResponse(
        outcome_id=outcome_id,
        task_id=task_id,
        edge_error=edge_error,
        task_already_archived=not archived,
    )


async def abandon(
    engine: GaOTTTEngine,
    task_id: str,
    reason: str,
) -> AbandonResponse:
    reason_id, _ = await save_memory(
        engine, content=reason, source="agent", tags=["abandoned-task"],
        emotion=-0.2, certainty=1.0,
    )
    if reason_id is None:
        return AbandonResponse(task_id=task_id, duplicate=True)

    try:
        await engine.relate(
            src_id=reason_id, dst_id=task_id, edge_type="abandoned",
            metadata={"abandoned_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
        )
    except ValueError as e:
        return AbandonResponse(
            reason_id=reason_id, task_id=task_id, edge_error=str(e),
        )

    await engine.archive([task_id])
    return AbandonResponse(reason_id=reason_id, task_id=task_id)


async def depend(
    engine: GaOTTTEngine,
    task_id: str,
    depends_on_id: str,
    blocking: bool = False,
) -> DependResponse:
    edge_type = "blocked_by" if blocking else "depends_on"
    try:
        await engine.relate(
            src_id=task_id, dst_id=depends_on_id, edge_type=edge_type,
        )
    except ValueError as e:
        return DependResponse(
            task_id=task_id, depends_on_id=depends_on_id,
            edge_type=edge_type, error=str(e),
        )
    return DependResponse(
        task_id=task_id, depends_on_id=depends_on_id, edge_type=edge_type,
    )


async def declare_value(
    engine: GaOTTTEngine,
    content: str,
    certainty: float = 1.0,
) -> DeclareValueResponse:
    new_id, _ = await save_memory(
        engine, content=content, source="value", tags=["value"],
        emotion=0.6, certainty=certainty,
    )
    if new_id is None:
        return DeclareValueResponse(duplicate=True)
    return DeclareValueResponse(id=new_id, duplicate=False)


async def declare_intention(
    engine: GaOTTTEngine,
    content: str,
    parent_value_id: str | None = None,
    certainty: float = 1.0,
) -> DeclareIntentionResponse:
    new_id, _ = await save_memory(
        engine, content=content, source="intention", tags=["intention"],
        emotion=0.5, certainty=certainty,
    )
    if new_id is None:
        return DeclareIntentionResponse(duplicate=True)

    edge_error: str | None = None
    if parent_value_id:
        try:
            await engine.relate(
                src_id=new_id, dst_id=parent_value_id, edge_type="derived_from",
            )
        except ValueError as e:
            edge_error = str(e)
    return DeclareIntentionResponse(
        id=new_id,
        duplicate=False,
        parent_value_id=parent_value_id,
        edge_error=edge_error,
    )


async def declare_commitment(
    engine: GaOTTTEngine,
    content: str,
    parent_intention_id: str,
    deadline_seconds: float | None = None,
    certainty: float = 1.0,
) -> DeclareCommitmentResponse:
    new_id, metadata = await save_memory(
        engine, content=content, source="commitment", tags=["commitment"],
        ttl_seconds=deadline_seconds, emotion=0.5, certainty=certainty,
    )
    if new_id is None:
        return DeclareCommitmentResponse(
            duplicate=True, parent_intention_id=parent_intention_id,
        )

    edge_error: str | None = None
    try:
        await engine.relate(
            src_id=new_id, dst_id=parent_intention_id, edge_type="fulfills",
        )
    except ValueError as e:
        edge_error = str(e)
    return DeclareCommitmentResponse(
        id=new_id,
        duplicate=False,
        parent_intention_id=parent_intention_id,
        expires_at=metadata.get("expires_at"),
        edge_error=edge_error,
    )


async def inherit_persona(engine: GaOTTTEngine) -> PersonaSnapshotResponse:
    """Composite persona snapshot — same source as ``reflect(aspect='persona')``."""
    return await _persona_snapshot(engine)
