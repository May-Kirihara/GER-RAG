"""Relations service — relate / unrelate / get_relations."""
from __future__ import annotations

from gaottt.core.engine import GaOTTTEngine
from gaottt.core.types import (
    RelateResponse,
    RelationsResponse,
    UnrelateResponse,
)


async def relate(
    engine: GaOTTTEngine,
    src_id: str,
    dst_id: str,
    edge_type: str,
    weight: float = 1.0,
    metadata: dict | None = None,
) -> RelateResponse:
    edge = await engine.relate(
        src_id=src_id, dst_id=dst_id, edge_type=edge_type,
        weight=weight, metadata=metadata,
    )
    return RelateResponse(edge=edge)


async def unrelate(
    engine: GaOTTTEngine,
    src_id: str,
    dst_id: str,
    edge_type: str | None = None,
) -> UnrelateResponse:
    removed = await engine.unrelate(src_id, dst_id, edge_type)
    return UnrelateResponse(removed=removed, src_id=src_id, dst_id=dst_id)


async def get_relations(
    engine: GaOTTTEngine,
    node_id: str,
    edge_type: str | None = None,
    direction: str = "out",
) -> RelationsResponse:
    edges = await engine.get_relations(node_id, edge_type=edge_type, direction=direction)
    return RelationsResponse(
        node_id=node_id, direction=direction, edges=edges, count=len(edges),
    )
