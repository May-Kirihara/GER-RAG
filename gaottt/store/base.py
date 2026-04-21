from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import numpy as np

from gaottt.core.types import CooccurrenceEdge, DirectedEdge, NodeState


class StoreBase(ABC):
    @abstractmethod
    async def save_documents(
        self, docs: list[dict[str, Any]]
    ) -> None:
        """Save documents (id, content, metadata)."""
        ...

    @abstractmethod
    async def get_document(self, doc_id: str) -> dict[str, Any] | None:
        """Get a document by ID."""
        ...

    @abstractmethod
    async def save_node_states(self, states: list[NodeState]) -> None:
        """Batch upsert node states."""
        ...

    @abstractmethod
    async def get_node_states(self, ids: list[str]) -> dict[str, NodeState]:
        """Get node states by IDs."""
        ...

    @abstractmethod
    async def get_all_node_states(self) -> list[NodeState]:
        """Get all node states."""
        ...

    @abstractmethod
    async def save_edges(self, edges: list[CooccurrenceEdge]) -> None:
        """Batch upsert co-occurrence edges."""
        ...

    @abstractmethod
    async def get_edges_for_node(self, node_id: str) -> list[CooccurrenceEdge]:
        """Get all edges connected to a node."""
        ...

    @abstractmethod
    async def get_all_edges(self) -> list[CooccurrenceEdge]:
        """Get all edges."""
        ...

    @abstractmethod
    async def save_displacements(self, displacements: dict[str, np.ndarray]) -> None:
        """Batch save displacement vectors."""
        ...

    @abstractmethod
    async def load_displacements(
        self, ids: list[str] | None = None,
    ) -> dict[str, np.ndarray]:
        """Load displacement vectors. If `ids` is given, only those nodes are
        fetched; otherwise all displacements are returned."""
        ...

    @abstractmethod
    async def save_velocities(self, velocities: dict[str, np.ndarray]) -> None:
        """Batch save velocity vectors."""
        ...

    @abstractmethod
    async def load_velocities(
        self, ids: list[str] | None = None,
    ) -> dict[str, np.ndarray]:
        """Load velocity vectors. If `ids` is given, only those nodes are
        fetched; otherwise all velocities are returned."""
        ...

    @abstractmethod
    async def reset_dynamic_state(self) -> tuple[int, int]:
        """Reset all dynamic state. Returns (nodes_reset, edges_removed)."""
        ...

    @abstractmethod
    async def set_archived(self, node_ids: list[str], archived: bool) -> int:
        """Soft-delete (or restore) nodes by flipping is_archived. Returns affected count."""
        ...

    @abstractmethod
    async def hard_delete_nodes(self, node_ids: list[str]) -> int:
        """Physically remove nodes, their documents, and connected edges. Returns deleted count."""
        ...

    @abstractmethod
    async def expire_due_nodes(self, now: float) -> int:
        """Mark expired nodes as archived. Returns count of newly archived nodes."""
        ...

    @abstractmethod
    async def upsert_directed_edge(self, edge: DirectedEdge) -> None:
        """Create or replace a typed directed edge."""
        ...

    @abstractmethod
    async def delete_directed_edge(
        self, src: str, dst: str, edge_type: str | None = None,
    ) -> int:
        """Remove one or all directed edges between src and dst."""
        ...

    @abstractmethod
    async def get_directed_edges(
        self,
        node_id: str | None = None,
        edge_type: str | None = None,
        direction: str = "out",
    ) -> list[DirectedEdge]:
        """List directed edges with optional node/type/direction filters."""
        ...

    @abstractmethod
    async def delete_directed_edges_for_node(self, node_id: str) -> int:
        """Drop every directed edge touching the given node."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close storage connections."""
        ...
