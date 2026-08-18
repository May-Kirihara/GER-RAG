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

import logging
from pathlib import Path

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.embedding.ruri import RuriEmbedder
from gaottt.index.bm25_index import BM25Index
from gaottt.index.faiss_index import FaissIndex
from gaottt.store.cache import CacheLayer
from gaottt.store.manifest import ensure_manifest, verify_embedder_identity
from gaottt.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)


def build_engine(config: GaOTTTConfig) -> GaOTTTEngine:
    """Construct a GaOTTTEngine with default component wiring.

    Caller is responsible for ``await engine.startup()`` and
    ``await engine.shutdown()``.
    """
    # MV1 — when an embedding service endpoint is configured, wire a
    # RemoteEmbedder (one shared model load) instead of a local RuriEmbedder.
    # RemoteEmbedder.__init__ GETs /info, raising ConnectionError at once if
    # the service is down (§MV1-3 "接続不能なら ConnectionError を即 raise").
    if config.embedder_endpoint:
        from gaottt.embedding.remote import RemoteEmbedder

        embedder = RemoteEmbedder(
            config.embedder_endpoint,
            timeout=config.embedder_request_timeout_seconds,
        )
    else:
        embedder = RuriEmbedder(
            model_name=config.model_name, batch_size=config.batch_size
        )
    # MV0 — manifest identity verification at factory time. ``ensure_manifest``
    # auto-generates from config for existing DBs; ``verify_embedder_identity``
    # checks FAISS dim (always raises), manifest dim / embedder_id (raise /
    # warn per ``manifest_check_enabled``), and embedder_version (warn only).
    # Direct engine construction in tests bypasses this factory, so legacy
    # stub embedders without ``embedder_id`` stay unaffected.
    manifest = ensure_manifest(Path(config.data_dir), config)
    verify_embedder_identity(manifest, embedder, config)
    faiss_index = FaissIndex(
        dimension=config.embedding_dim,
        lock_enabled=config.faiss_index_lock_enabled,
    )
    virtual_faiss_index = (
        FaissIndex(
            dimension=config.embedding_dim,
            lock_enabled=config.faiss_index_lock_enabled,
        )
        if config.virtual_faiss_enabled else None
    )
    # Phase L Stage 1: wire the BM25 lexical index when enabled. The flag
    # default is True; set ``hybrid_bm25_enabled=False`` to fall back to
    # the Phase H Stage 4 raw+virtual-only seed pool.
    bm25_index = (
        BM25Index(
            k1=config.bm25_k1,
            b=config.bm25_b,
            tokenizer=config.bm25_tokenizer,
        )
        if config.hybrid_bm25_enabled else None
    )
    # Ambient Recall Enrichment: a dedicated word-level (Sudachi) BM25 index
    # for the relevance gate — kept separate from the Phase L hybrid-retrieval
    # index above so the gate's tokenizer choice does not touch retrieval.
    # If the bm25-sudachi extra is missing, construction raises ImportError —
    # the gate then falls back to the virtual_score gate.
    ambient_gate_index = None
    if config.ambient_gate_use_bm25:
        try:
            ambient_gate_index = BM25Index(tokenizer=config.ambient_gate_tokenizer)
        except ImportError as exc:
            logger.warning(
                "ambient gate index unavailable (%s) — ambient_recall falls "
                "back to the virtual_score gate. Install the extra with "
                "`uv pip install -e '.[bm25-sudachi]'`.", exc,
            )
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
        virtual_faiss_index=virtual_faiss_index,
        bm25_index=bm25_index,
        ambient_gate_index=ambient_gate_index,
    )
