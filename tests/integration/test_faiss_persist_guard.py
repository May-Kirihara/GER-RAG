"""Reverse-overwrite guard tests (deterministic stub embedder, no model).

A process whose in-memory FAISS is far smaller than the SQLite active-node
count is running on a corrupt/truncated index. Its write-behind save loop and
final shutdown save must NOT overwrite a good on-disk index (the "reverse
overwrite trap", CLAUDE.md). 2026-05-31 incident: production FAISS collapsed to
2 vectors vs 39,402 docs. These tests pin:

  * the dynamic ``_faiss_safe_to_persist`` decision,
  * the startup-diagnostics latch escalating severe undersize to ERROR,
  * the end-to-end invariant: a guarded shutdown leaves the on-disk index
    intact.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.diagnostics import DiagnosticLevel, run_startup_checks
from gaottt.index.faiss_index import FaissIndex
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore


class StubEmbedder:
    """Deterministic embedder: token-overlap controls similarity (no model).

    Mirrors tests/integration/test_engine_archive_ttl.py so the engine's
    ``encode_documents`` / ``encode_query`` contract is satisfied exactly.
    """

    def __init__(self, dimension: int = 32):
        self._dimension = dimension
        self._token_cache: dict[str, np.ndarray] = {}

    @property
    def dimension(self) -> int:
        return self._dimension

    def _token_vec(self, token: str) -> np.ndarray:
        cached = self._token_cache.get(token)
        if cached is not None:
            return cached
        seed = int.from_bytes(hashlib.md5(token.encode()).digest()[:8], "big")
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(self._dimension).astype(np.float32)
        v /= np.linalg.norm(v) + 1e-9
        self._token_cache[token] = v
        return v

    def _embed(self, text: str) -> np.ndarray:
        tokens = [t.lower() for t in text.split() if t.strip()]
        if not tokens:
            return np.zeros(self._dimension, dtype=np.float32)
        v = sum(self._token_vec(t) for t in tokens)
        norm = np.linalg.norm(v)
        return (v / norm).astype(np.float32) if norm > 0 else v.astype(np.float32)

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._embed(t) for t in texts])

    def encode_query(self, text: str) -> np.ndarray:
        return self._embed(text).reshape(1, -1)


def _make_engine(tmp_path, **cfg_overrides) -> GaOTTTEngine:
    kw = dict(
        embedding_dim=32,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "g.db"),
        faiss_index_path=str(tmp_path / "g.faiss"),
        virtual_faiss_index_path=str(tmp_path / "g.virtual.faiss"),
        flush_interval_seconds=999.0,           # no background flush in tests
        faiss_save_interval_seconds=0,          # no background raw-save loop
        virtual_faiss_save_interval_seconds=0,  # no background virtual loop
        dream_enabled=False,
        # Low floor so a handful of stub docs clears it (default 100 is for
        # production-scale DBs).
        faiss_persist_floor=3,
    )
    kw.update(cfg_overrides)
    cfg = GaOTTTConfig(**kw)
    return GaOTTTEngine(
        config=cfg,
        embedder=StubEmbedder(dimension=32),
        faiss_index=FaissIndex(dimension=32),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=cfg.db_path),
        virtual_faiss_index=FaissIndex(dimension=32),
    )


async def _index_n(engine: GaOTTTEngine, n: int) -> list[str]:
    return await engine.index_documents([
        {"content": f"document number {i} about topic {i}",
         "metadata": {"source": "user"}}
        for i in range(n)
    ])


# --- dynamic _faiss_safe_to_persist decision ------------------------------

@pytest.mark.asyncio
async def test_guard_allows_save_when_healthy(tmp_path):
    engine = _make_engine(tmp_path)
    await engine.startup()
    await _index_n(engine, 5)
    ok, reason = engine._faiss_safe_to_persist()
    assert ok is True, reason
    await engine.shutdown()


@pytest.mark.asyncio
async def test_guard_blocks_save_when_severely_undersized(tmp_path):
    engine = _make_engine(tmp_path)
    await engine.startup()
    await _index_n(engine, 5)
    # Simulate in-memory divergence: index collapses to 1 vector while the
    # cache still holds 5 active nodes.
    engine.faiss_index.reset()
    engine.faiss_index.add(np.zeros((1, 32), dtype=np.float32), ["dummy"])
    ok, reason = engine._faiss_safe_to_persist()
    assert ok is False
    assert "active=5" in reason
    await engine.shutdown()


@pytest.mark.asyncio
async def test_guard_inert_below_floor(tmp_path):
    # Production-scale floor: a tiny DB legitimately has few vectors and there
    # is no good on-disk index worth protecting yet.
    engine = _make_engine(tmp_path, faiss_persist_floor=100)
    await engine.startup()
    await _index_n(engine, 5)
    engine.faiss_index.reset()  # 0 vectors, but active=5 < floor=100
    ok, _reason = engine._faiss_safe_to_persist()
    assert ok is True
    await engine.shutdown()


@pytest.mark.asyncio
async def test_guard_disabled_by_config(tmp_path):
    engine = _make_engine(tmp_path, faiss_persist_guard_enabled=False)
    await engine.startup()
    await _index_n(engine, 5)
    engine.faiss_index.reset()  # severely undersized, but guard off
    ok, _reason = engine._faiss_safe_to_persist()
    assert ok is True
    await engine.shutdown()


# --- startup diagnostics latch --------------------------------------------

@pytest.mark.asyncio
async def test_startup_diagnostics_latches_persist_block(tmp_path):
    # Build a good on-disk index (5 vectors), then truncate it to 1 vector so
    # the next process loads a severely-undersized index against a 5-node DB.
    e1 = _make_engine(tmp_path)
    await e1.startup()
    await _index_n(e1, 5)
    await e1.shutdown()  # on-disk faiss now has 5 vectors

    bad = FaissIndex(dimension=32)
    bad.add(np.zeros((1, 32), dtype=np.float32), ["dummy"])
    bad.save(e1.config.faiss_index_path)

    e2 = _make_engine(tmp_path)
    await e2.startup()  # loads corrupt index; startup runs diagnostics
    report = await run_startup_checks(e2, e2.config)
    names_levels = {(r.name, r.level) for r in report.results}
    assert ("tier_b_faiss_severe_undersize", DiagnosticLevel.ERROR) in names_levels
    assert e2._faiss_persist_blocked is True
    await e2.shutdown()


@pytest.mark.asyncio
async def test_startup_diagnostics_detects_oversized_stale_snapshot(tmp_path):
    """A rolled-back DB must not silently query a newer FAISS snapshot."""
    e1 = _make_engine(tmp_path)
    await e1.startup()
    await _index_n(e1, 5)
    await e1.shutdown()

    stale = FaissIndex(dimension=32)
    stale.add(
        np.zeros((20, 32), dtype=np.float32),
        [f"stale-snapshot-{i}" for i in range(20)],
    )
    stale.save(e1.config.faiss_index_path)

    e2 = _make_engine(tmp_path)
    await e2.startup()
    report = await run_startup_checks(e2, e2.config)
    names_levels = {(r.name, r.level) for r in report.results}
    assert (
        "tier_b_faiss_snapshot_mismatch", DiagnosticLevel.ERROR,
    ) in names_levels
    assert e2._faiss_persist_blocked is True
    await e2.shutdown()


# --- end-to-end: guard protects the good on-disk index --------------------

@pytest.mark.asyncio
async def test_blocked_shutdown_does_not_clobber_good_index(tmp_path):
    e1 = _make_engine(tmp_path)
    await e1.startup()
    await _index_n(e1, 5)
    await e1.shutdown()

    on_disk = FaissIndex(dimension=32)
    on_disk.load(e1.config.faiss_index_path)
    assert on_disk.size == 5  # good index established

    # A second process loads the good index, then its in-memory FAISS diverges
    # to a near-empty state (the trap). Its shutdown must NOT overwrite disk.
    e2 = _make_engine(tmp_path)
    await e2.startup()
    assert e2.faiss_index.size == 5  # loaded healthy
    e2.faiss_index.reset()
    e2.faiss_index.add(np.zeros((1, 32), dtype=np.float32), ["dummy"])
    assert e2._faiss_safe_to_persist()[0] is False
    await e2.shutdown()

    after = FaissIndex(dimension=32)
    after.load(e1.config.faiss_index_path)
    assert after.size == 5, "guard failed: on-disk good index was clobbered"
