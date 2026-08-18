"""Tier 6 — RemoteEmbedder numerical equivalence (REAL RURI required).

This module carries a module-level conditional skip marker (active only when
``GAOTTT_EMBEDDER_ENDPOINT`` is unset). It is part of the
仮説 -> 実装 -> 検証 manual verification loop (see
``docs/wiki/Operations-Performance-Testing.md``) and is deliberately **not**
run in CI. Run it manually with a running embedding service::

    .venv/bin/python -m gaottt.embedding.service --port 7879 &
    GAOTTT_EMBEDDER_ENDPOINT=http://127.0.0.1:7879 \\
    .venv/bin/python -m pytest tests/perf/test_tier6_remote_embedder.py -v -s

Numerical equivalence between the in-process ``RuriEmbedder`` and a
``RemoteEmbedder`` pointed at the service is verified in three stages
(implementation plan §MV1-4):

    1. ``np.allclose(in_process, remote, atol=1e-5)``
    2. cosine difference < 1e-6
    3. golden queries (``tests/perf/golden_corpus/queries.json``) top-K
       agreement via ``engine.query``

Bit-exact equality is NOT required — service-side batching can change the
batch shape without changing the per-vector result.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

from gaottt.embedding.remote import RemoteEmbedder
from gaottt.services.runtime import build_engine
from tests.perf._helpers import get_shared_embedder, make_config, make_engine

# Module-level conditional skip: the bodies need a live embedding service.
# CI leaves the env unset so collection succeeds and every test is reported
# as skipped; setting GAOTTT_EMBEDDER_ENDPOINT opts in to manual verification.
pytestmark = pytest.mark.skipif(
    not os.environ.get("GAOTTT_EMBEDDER_ENDPOINT"),
    reason="set GAOTTT_EMBEDDER_ENDPOINT to a running embedding service; "
    "manual verification per Operations-Performance-Testing",
)

GOLDEN_DIR = Path(__file__).parent / "golden_corpus"
CHUNKS_PATH = GOLDEN_DIR / "synthetic_chunks.jsonl"
QUERIES_PATH = GOLDEN_DIR / "queries.json"

TOP_K = 5

# RURI is Japanese-specialised, so the equivalence texts mix JP/EN to
# exercise the model on the vocabulary the golden corpus actually uses.
# Shared between test 1 and test 2 so the "cosine < 1e-6" assertion is
# literally a tighter restatement of the same comparison, not a new one.
_EQUIVALENCE_TEXTS = [
    "Eleventy 静的サイトジェネレーター",
    "シチリア島の海軍上陸 1943",
    "guanciale pecorino eggs パスタ",
    "Hebbian learning 重み付き勾配",
    "Reciprocal Rank Fusion ハイブリッド検索",
]


def _load_chunks() -> list[dict]:
    if not CHUNKS_PATH.exists():
        pytest.skip(f"Golden corpus missing: {CHUNKS_PATH}")
    chunks: list[dict] = []
    with CHUNKS_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            chunks.append(json.loads(line))
    return chunks


def _load_queries() -> list[dict]:
    if not QUERIES_PATH.exists():
        pytest.skip(f"Golden queries missing: {QUERIES_PATH}")
    with QUERIES_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def test_remote_encode_documents_allclose():
    """np.allclose(in_process, remote, atol=1e-5) for encode_documents."""
    texts = _EQUIVALENCE_TEXTS
    local = get_shared_embedder().encode_documents(texts)
    remote = RemoteEmbedder(os.environ["GAOTTT_EMBEDDER_ENDPOINT"]).encode_documents(texts)
    assert local.shape == remote.shape == (len(texts), 768)
    assert np.allclose(local, remote, atol=1e-5)


def test_remote_encode_documents_cosine_diff():
    """cosine difference < 1e-6 for encode_documents.

    Rows are L2-normalized by both embedders, so cosine similarity equals
    the dot product and the max abs elementwise diff bounds the angular
    drift. A tighter tolerance than test 1's allclose.
    """
    texts = _EQUIVALENCE_TEXTS
    local = get_shared_embedder().encode_documents(texts)
    remote = RemoteEmbedder(os.environ["GAOTTT_EMBEDDER_ENDPOINT"]).encode_documents(texts)
    assert local.shape == remote.shape == (len(texts), 768)
    assert np.max(np.abs(local - remote)) < 1e-6


@pytest.mark.asyncio
async def test_remote_topk_agreement_golden_queries(tmp_path):
    """engine.query top-K agreement between in-process and remote on the
    golden queries in tests/perf/golden_corpus/queries.json."""
    chunks = _load_chunks()
    queries = _load_queries()
    endpoint = os.environ["GAOTTT_EMBEDDER_ENDPOINT"]

    documents = [
        {
            "content": c["content"],
            "metadata": {
                "source": c.get("source", "synthetic"),
                "tags": c.get("tags", []),
                "golden_fixture_id": c["id"],
            },
        }
        for c in chunks
    ]

    # Local engine reuses the shared in-process RURI embedder (manifest
    # verification bypassed by direct construction). Remote engine goes
    # through the build_engine factory so manifest identity is checked
    # against the service's /info. Both use identical retrieval params via
    # make_config, isolating the embedder as the only varying input.
    local_eng = make_engine(tmp_path / "local")
    await local_eng.startup()
    try:
        remote_eng = build_engine(
            make_config(tmp_path / "remote", embedder_endpoint=endpoint)
        )
        await remote_eng.startup()
        try:
            await local_eng.index_documents(documents)
            await remote_eng.index_documents(documents)

            failures: list[str] = []
            for q in queries:
                local_results = await local_eng.query(text=q["query"], top_k=TOP_K)
                remote_results = await remote_eng.query(text=q["query"], top_k=TOP_K)
                local_ids = [r.metadata.get("golden_fixture_id") for r in local_results]
                remote_ids = [r.metadata.get("golden_fixture_id") for r in remote_results]
                if local_ids != remote_ids:
                    failures.append(
                        f"query {q['query']!r}: local top-{TOP_K} {local_ids} "
                        f"!= remote {remote_ids}"
                    )
            assert not failures, (
                "top-K golden_fixture_id disagreement between in-process and "
                "remote embedders:\n  " + "\n  ".join(failures)
            )
        finally:
            await remote_eng.shutdown()
    finally:
        await local_eng.shutdown()
