"""Shared engine construction for MCP and REST servers.

Both transports need the same embedder + FAISS + store + cache wiring.
This module hands back a fully-wired ``GaOTTTEngine`` (before startup) so
each transport can own its own lifecycle (lifespan vs lazy singleton).

Test injection contract
-----------------------
``gaottt.server.mcp_server`` keeps its own ``_engine`` module attribute so
existing tests (``tests/integration/test_mcp_tools.py`` et al.) can continue
to ``monkeypatch.setattr(srv, "_engine", eng)``. This module deliberately
does **not** maintain a separate singleton; it is a factory only.
"""
from __future__ import annotations

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.embedding.ruri import RuriEmbedder
from gaottt.index.faiss_index import FaissIndex
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore


def build_engine(config: GaOTTTConfig) -> GaOTTTEngine:
    """Construct a GaOTTTEngine with default component wiring.

    Caller is responsible for ``await engine.startup()`` and
    ``await engine.shutdown()``.
    """
    embedder = RuriEmbedder(model_name=config.model_name, batch_size=config.batch_size)
    faiss_index = FaissIndex(dimension=config.embedding_dim)
    store = SqliteStore(db_path=config.db_path)
    cache = CacheLayer(
        flush_interval=config.flush_interval_seconds,
        flush_threshold=config.flush_threshold,
    )
    return GaOTTTEngine(
        config=config,
        embedder=embedder,
        faiss_index=faiss_index,
        cache=cache,
        store=store,
    )
