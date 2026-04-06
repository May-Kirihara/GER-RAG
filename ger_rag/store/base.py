from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import numpy as np

from ger_rag.core.types import CooccurrenceEdge, NodeState


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
    async def load_displacements(self) -> dict[str, np.ndarray]:
        """Load all displacement vectors."""
        ...

    @abstractmethod
    async def save_velocities(self, velocities: dict[str, np.ndarray]) -> None:
        """Batch save velocity vectors."""
        ...

    @abstractmethod
    async def load_velocities(self) -> dict[str, np.ndarray]:
        """Load all velocity vectors."""
        ...

    @abstractmethod
    async def reset_dynamic_state(self) -> tuple[int, int]:
        """Reset all dynamic state. Returns (nodes_reset, edges_removed)."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close storage connections."""
        ...
