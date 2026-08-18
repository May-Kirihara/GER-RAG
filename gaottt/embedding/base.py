"""Embedder protocol (MV0).

Structural protocol describing the embedder surface ``GaOTTTEngine`` and
``build_engine`` consume. ``RuriEmbedder`` is the in-process reference
implementation; MV1 adds ``RemoteEmbedder`` behind the same protocol so the
engine layer is agnostic to whether vectors come from a local model or a
shared embedding service.

The protocol is a *narrow* projection of the runtime contract — it pins
only the members the engine relies on. It is intentionally not a base
class: existing embedders (``RuriEmbedder``, the test ``StubEmbedder``)
keep duck-typing and need not inherit. ``runtime_checkable`` lets
``isinstance`` succeed for structural conformance where a check is wanted.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class EmbedderProtocol(Protocol):
    @property
    def dimension(self) -> int:
        ...

    @property
    def embedder_id(self) -> str:
        ...

    @property
    def embedder_version(self) -> str:
        ...

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        ...

    def encode_query(self, text: str) -> np.ndarray:
        ...
