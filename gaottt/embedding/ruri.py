from __future__ import annotations

import logging
import os

import numpy as np
from huggingface_hub import scan_cache_dir
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


def _is_model_cached(model_name: str) -> bool:
    """Check if model already exists in HuggingFace cache."""
    try:
        cache_info = scan_cache_dir()
        for repo in cache_info.repos:
            if repo.repo_id == model_name:
                return True
    except Exception:
        pass
    return False


class RuriEmbedder:
    QUERY_PREFIX = "検索クエリ: "
    DOCUMENT_PREFIX = "検索文書: "

    def __init__(self, model_name: str = "cl-nagoya/ruri-v3-310m", batch_size: int = 32):
        local_only = _is_model_cached(model_name)
        if local_only:
            logger.info("Loading %s from local cache (offline)", model_name)
        else:
            logger.info("Downloading %s from HuggingFace", model_name)
        self._model_name = model_name
        self._model = SentenceTransformer(model_name, local_files_only=local_only)
        self._batch_size = batch_size

    @property
    def dimension(self) -> int:
        return self._model.get_sentence_embedding_dimension()

    @property
    def embedder_id(self) -> str:
        # Manifest identity key: the model name (HF repo id) is the stable
        # handle. embedder_version (below) carries the snapshot drift.
        return self._model_name

    @property
    def embedder_version(self) -> str:
        # HF snapshot commit hash of the locally cached revision, most
        # recently modified first. Unpinned models (manual placement, cache
        # miss, scan failure) report "unpinned" — v1 treats this as a
        # warning, not a hard failure.
        try:
            cache_info = scan_cache_dir()
            for repo in cache_info.repos:
                if repo.repo_id != self._model_name:
                    continue
                revisions = sorted(
                    repo.revisions,
                    key=lambda r: r.last_modified,
                    reverse=True,
                )
                if revisions:
                    return revisions[0].commit_hash
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("embedder_version lookup failed: %s: %s", type(exc).__name__, exc)
        return "unpinned"

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        prefixed = [self.DOCUMENT_PREFIX + t for t in texts]
        embeddings = self._model.encode(
            prefixed,
            batch_size=self._batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return embeddings.astype(np.float32)

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        """Batch-embed multiple query segments in one model forward pass.

        Used by Multi-Source Query (docs/wiki/Plans-Query-Mass-Distribution.md):
        each prompt segment becomes a separate point-mass query vector. One
        ``encode`` call for all N keeps the multi-source path within the
        latency budget — N separate calls would be the regression. Returns
        shape ``(len(texts), dim)``; rows are L2-normalized.
        """
        prefixed = [self.QUERY_PREFIX + t for t in texts]
        embeddings = self._model.encode(
            prefixed,
            batch_size=self._batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return embeddings.astype(np.float32)

    def encode_query(self, text: str) -> np.ndarray:
        return self.encode_queries([text])
