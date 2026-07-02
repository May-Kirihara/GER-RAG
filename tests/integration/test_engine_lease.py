"""WP-3 / WP-4 contract — engine-side owner-lease integration (test-first / RED).

These tests pin the engine wiring of the owner-lease read-only transition that
WP-4 implements. Until WP-4 lands, every lease-dependent test here is RED with a
precise, attributable signal (``_persist_blocked`` latch absent, the persist
loops still flush/save, mutators do not raise ``LeaseLostError``, startup does
not acquire the lease, the CLI lacks ``--force-takeover``). When WP-4 wires the
contract below, each test turns GREEN with no further edit.

Following the WP-1 learning recorded in ``tests/unit/test_owner_lease.py`` (a
module-top import of an unimplemented symbol aborts collection for the whole
file), the only unimplemented symbol — ``LeaseLostError`` — is imported **inside
the single test that needs it**. Every module-top import names a symbol that
already exists, so ``pytest`` collects the file and reports each section's RED
individually.

Pinned contract (what these tests assert):

  CacheLayer (``gaottt/store/cache.py``):
    - new flag ``cache.persist_blocked: bool = False``
    - ``flush_to_store()`` entry: ``if self.persist_blocked: return`` (no-op)

  Engine (``gaottt/core/engine.py``):
    - new latch ``engine._persist_blocked: bool = False``
    - lease loss sets both ``engine._persist_blocked = True`` and
      ``engine.cache.persist_blocked = True``
    - FAISS save loop / virtual FAISS save loop / shutdown final save all skip
      when ``_persist_blocked`` OR ``_faiss_persist_blocked`` is set
    - 14 mutating methods raise ``LeaseLostError`` at entry:
        index_documents, archive, restore, forget, relate, unrelate,
        revalidate, merge, compact, reset_orbital_state, reset_velocities,
        reset_masses, warm_displacement, reset
    - ``query`` falls back to passive (returns results, does not perturb mass /
      displacement) when ``_persist_blocked`` is set
    - ``_orbital_tick`` is a no-op when ``_persist_blocked`` is set
    - startup acquires the lease when ``owner_lease_enabled`` OR
      ``manifest.managed``; otherwise the whole lease layer is skipped (no
      ``owner.lock``, no heartbeat task)
    - shutdown flushes *then* calls ``lease.release()``

  Exception (``gaottt/store/lease.py``):
    - new ``LeaseLostError(Exception)`` raised by the 14 mutators above
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import socket
import time
from pathlib import Path

import numpy as np
import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.core.types import NodeState
from gaottt.index.faiss_index import FaissIndex
from gaottt.store.cache import CacheLayer
from gaottt.store.lease import LeaseHeldError
from gaottt.store.manifest import UniverseManifest, write_manifest
from gaottt.store.sqlite_store import SqliteStore

OWNER_LOCK_FILENAME = "owner.lock"
OWNER_GUARD_FILENAME = "owner.lock.guard"
FOREIGN_OWNER_ID = "foreign-usurper"


# ---------------------------------------------------------------------------
# deterministic embedder (mirrors tests/integration/test_engine_archive_ttl.py)
# ---------------------------------------------------------------------------

class StubEmbedder:
    """Deterministic embedder: keyword-overlap controls similarity.

    Each unique whitespace-separated token gets a stable unit basis vector
    (seeded by md5 of the token, so it is consistent across processes). A
    text's embedding is the L2-normalized sum of its token vectors.
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


# ---------------------------------------------------------------------------
# config / engine factories
# ---------------------------------------------------------------------------

def _make_config(data_dir, **overrides) -> GaOTTTConfig:
    """Build a config pinned to ``data_dir`` with all background loops off.

    Lease is OFF by default; lease-dependent tests pass ``owner_lease_enabled=
    True`` plus short heartbeat/stale timers. Loop tests re-enable the
    write-behind / FAISS / virtual-FAISS save cadences they need.
    """
    base: dict = dict(
        data_dir=str(data_dir),
        embedding_dim=32,
        model_name="stub-deterministic",
        flush_interval_seconds=999.0,        # no background cache flush
        faiss_save_interval_seconds=0.0,     # no background FAISS save
        virtual_faiss_save_interval_seconds=0.0,
        dream_enabled=False,
        dream_interval_seconds=0.0,
        orbital_tick_enabled=False,
        genesis_kick_enabled=False,
        supernova_enabled=False,
        mass_conservation_enabled=False,
        mass_bh_enabled=False,
        persona_boost_enabled=False,
        owner_lease_enabled=False,
    )
    base.update(overrides)
    return GaOTTTConfig(**base)


def _make_engine_from_config(config: GaOTTTConfig) -> GaOTTTEngine:
    embedder = StubEmbedder(dimension=config.embedding_dim)
    faiss_index = FaissIndex(dimension=config.embedding_dim)
    virtual = (
        FaissIndex(dimension=config.embedding_dim)
        if config.virtual_faiss_enabled else None
    )
    store = SqliteStore(db_path=config.db_path)
    cache = CacheLayer(
        flush_interval=config.flush_interval_seconds,
        flush_threshold=config.flush_threshold,
    )
    return GaOTTTEngine(
        config=config, embedder=embedder, faiss_index=faiss_index,
        cache=cache, store=store, virtual_faiss_index=virtual,
    )


def _make_engine(tmp_path, **overrides) -> GaOTTTEngine:
    return _make_engine_from_config(_make_config(tmp_path, **overrides))


@contextlib.asynccontextmanager
async def _engine(tmp_path, **overrides):
    eng = _make_engine(tmp_path, **overrides)
    await eng.startup()
    try:
        yield eng
    finally:
        await eng.shutdown()


# ---------------------------------------------------------------------------
# owner.lock helpers (deterministic — never rely on wall-clock ageing)
# ---------------------------------------------------------------------------

def _read_lock(data_dir) -> dict:
    return json.loads((Path(data_dir) / OWNER_LOCK_FILENAME).read_text("utf-8"))


def _write_full_lock(
    data_dir, *, owner_id: str, heartbeat_at: float,
    started_at: float | None = None, takeover_count: int = 0,
) -> None:
    """Write a complete owner.lock JSON from scratch (no prior acquire needed).

    Mirrors tests/unit/test_owner_lease._write_full_lock: staging a foreign or
    stale lock without going through OwnerLease.acquire keeps the test
    deterministic and avoids contending for the guard.
    """
    payload = {
        "owner_id": owner_id,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": started_at if started_at is not None else time.time(),
        "heartbeat_at": heartbeat_at,
        "takeover_count": takeover_count,
    }
    (Path(data_dir) / OWNER_LOCK_FILENAME).write_text(json.dumps(payload), "utf-8")


def _install_foreign_owner(data_dir) -> None:
    """Overwrite (or create) owner.lock with a foreign owner id.

    Simulates an external takeover so the heartbeat can detect the mismatch
    without the test having to run a second process.
    """
    _write_full_lock(data_dir, owner_id=FOREIGN_OWNER_ID, heartbeat_at=time.time())


# ---------------------------------------------------------------------------
# FAISS observability helpers
# ---------------------------------------------------------------------------

def _ids_line_count(ids_path) -> int:
    p = Path(ids_path)
    if not p.exists():
        return 0
    return sum(1 for line in p.read_text().splitlines() if line.strip())


def _file_signature(path) -> str | None:
    """sha256 of a file's bytes (None if absent) — resolution-independent
    'did the file change' signal for the virtual-FAISS rebuild path, where the
    ``.ids`` line count stays constant across rebuilds of the same node set."""
    p = Path(path)
    if not p.exists():
        return None
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


async def _wait_for_faiss_save(eng, count: int, timeout: float = 3.0) -> bool:
    ids_path = eng.config.faiss_index_path + ".ids"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _ids_line_count(ids_path) >= count:
            return True
        await asyncio.sleep(0.05)
    return False


async def _wait_for_virtual_save(eng, timeout: float = 3.0) -> bool:
    vpath = Path(eng.config.virtual_faiss_index_path)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if vpath.exists() and vpath.stat().st_size > 0:
            return True
        await asyncio.sleep(0.05)
    return False


def _mark_persist_blocked(eng) -> None:
    """Flip both latches, exactly as WP-4's lease-loss handler will."""
    eng._persist_blocked = True
    eng.cache.persist_blocked = True


# ===========================================================================
# Section A — 4-path persist block (the four persistence routes each skip)
# ===========================================================================

@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_write_behind_loop_skipped(tmp_path):
    """A1: cache.flush_to_store() honours persist_blocked → dirty cache never
    reaches SQLite across several write-behind ticks."""
    async with _engine(tmp_path, flush_interval_seconds=0.05) as eng:
        _mark_persist_blocked(eng)
        eng.cache.set_node(NodeState(id="a1-blocked", mass=2.0), dirty=True)
        # immediate: not yet flushed
        assert "a1-blocked" not in await eng.store.get_node_states(["a1-blocked"])
        await asyncio.sleep(0.4)  # ~8 write-behind ticks
        assert "a1-blocked" not in await eng.store.get_node_states(["a1-blocked"])


@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_shutdown_final_flush_skipped(tmp_path):
    """A2: shutdown's final flush_to_store honours persist_blocked → dirty cache
    is not written even on the way out."""
    eng = _make_engine(tmp_path, flush_interval_seconds=999.0)
    await eng.startup()
    _mark_persist_blocked(eng)
    eng.cache.set_node(NodeState(id="a2-blocked", mass=2.0), dirty=True)
    await eng.shutdown()
    fresh = SqliteStore(db_path=eng.config.db_path)
    await fresh.initialize()
    try:
        assert "a2-blocked" not in await fresh.get_node_states(["a2-blocked"])
    finally:
        await fresh.close()


@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_faiss_save_loop_skipped(tmp_path):
    """A3: the periodic FAISS save loop honours persist_blocked → an in-memory
    index grown after the block is never persisted to disk."""
    async with _engine(tmp_path, faiss_save_interval_seconds=0.1) as eng:
        await eng.index_documents([
            {"content": "a3 first doc", "metadata": {"source": "user"}},
        ])
        assert await _wait_for_faiss_save(eng, count=1), "baseline save missing"
        ids_path = eng.config.faiss_index_path + ".ids"
        assert _ids_line_count(ids_path) == 1

        _mark_persist_blocked(eng)
        # grow the in-memory index past the persisted baseline + flag dirty
        eng.faiss_index.add(
            eng.embedder.encode_query("a3 second doc"), ["a3-second"],
        )
        eng._faiss_dirty = True
        await asyncio.sleep(0.5)  # ~5 save ticks
        assert _ids_line_count(ids_path) == 1, "FAISS saved despite persist block"


@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_virtual_faiss_save_loop_skipped(tmp_path):
    """A4: the virtual-FAISS rebuild+save loop honours persist_blocked → a
    displacement change after the block never rewrites the virtual index."""
    async with _engine(tmp_path, virtual_faiss_save_interval_seconds=0.1) as eng:
        ids = await eng.index_documents([
            {"content": "a4 doc", "metadata": {"source": "user"}},
        ])
        dim = eng.config.embedding_dim
        eng.cache.set_displacement(ids[0], np.zeros(dim, dtype=np.float32))
        assert await _wait_for_virtual_save(eng), "baseline virtual save missing"
        vpath = eng.config.virtual_faiss_index_path
        baseline = _file_signature(vpath)

        _mark_persist_blocked(eng)
        eng.cache.set_displacement(ids[0], np.ones(dim, dtype=np.float32))
        await asyncio.sleep(0.5)  # ~5 rebuild ticks
        assert _file_signature(vpath) == baseline, (
            "virtual FAISS rebuilt despite persist block"
        )


# ===========================================================================
# Section B — read-only transition (14 mutators + query + orbital + reads)
# ===========================================================================

async def _m_index_documents(e):
    return await e.index_documents([
        {"content": "third lease guard", "metadata": {"source": "user"}},
    ])


async def _m_archive(e):
    return await e.archive([next(iter(e.cache.node_cache))])


async def _m_restore(e):
    return await e.restore([next(iter(e.cache.node_cache))])


async def _m_forget(e):
    return await e.forget([next(iter(e.cache.node_cache))])


async def _m_relate(e):
    a, b = list(e.cache.node_cache)[:2]
    return await e.relate(a, b, "supersedes")


async def _m_unrelate(e):
    a, b = list(e.cache.node_cache)[:2]
    return await e.unrelate(a, b, "supersedes")


async def _m_revalidate(e):
    return await e.revalidate(next(iter(e.cache.node_cache)))


async def _m_merge(e):
    return await e.merge(list(e.cache.node_cache)[:2])


async def _m_compact(e):
    return await e.compact()


async def _m_reset_orbital_state(e):
    return await e.reset_orbital_state()


async def _m_reset_velocities(e):
    return await e.reset_velocities()


async def _m_reset_masses(e):
    return await e.reset_masses()


async def _m_warm_displacement(e):
    return await e.warm_displacement()


async def _m_reset(e):
    return await e.reset()


# name -> minimal-valid-args caller. Each derives any ids it needs from the
# engine's own cache so the entry-check raises before any DB work in GREEN and
# simply runs through (no LeaseLostError) in RED.
_MUTATORS = {
    "index_documents": _m_index_documents,
    "archive": _m_archive,
    "restore": _m_restore,
    "forget": _m_forget,
    "relate": _m_relate,
    "unrelate": _m_unrelate,
    "revalidate": _m_revalidate,
    "merge": _m_merge,
    "compact": _m_compact,
    "reset_orbital_state": _m_reset_orbital_state,
    "reset_velocities": _m_reset_velocities,
    "reset_masses": _m_reset_masses,
    "warm_displacement": _m_warm_displacement,
    "reset": _m_reset,
}


@pytest.mark.parametrize("name", list(_MUTATORS))
@pytest.mark.asyncio
async def test_mutating_methods_raise_lease_lost(name, tmp_path):
    """B1: every mutating method raises LeaseLostError at entry once the engine
    is in the read-only (persist-blocked) state. RED until WP-4 adds
    LeaseLostError and the per-method entry guard."""
    from gaottt.store.lease import LeaseLostError  # RED: ImportError until WP-4

    async with _engine(tmp_path) as eng:
        await eng.index_documents([
            {"content": "alpha lease guard", "metadata": {"source": "user"}},
            {"content": "beta lease guard", "metadata": {"source": "user"}},
        ])
        _mark_persist_blocked(eng)
        with pytest.raises(LeaseLostError):
            await _MUTATORS[name](eng)


@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_query_passive_fallback(tmp_path):
    """B2: query() stays callable when blocked — it returns results but does
    NOT perturb the gravity field (passive fallback, not LeaseLostError)."""
    async with _engine(tmp_path) as eng:
        ids = await eng.index_documents([
            {"content": "recall me lease guard", "metadata": {"source": "user"}},
        ])
        nid = ids[0]
        before_mass = eng.cache.get_node(nid).mass
        before_disp_none = eng.cache.get_displacement(nid) is None

        _mark_persist_blocked(eng)
        results = await eng.query("recall")  # must not raise
        assert isinstance(results, list)

        after = eng.cache.get_node(nid)
        assert after.mass == before_mass, "query perturbed mass despite block"
        assert (eng.cache.get_displacement(nid) is None) == before_disp_none, (
            "query perturbed displacement despite block"
        )


@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_orbital_tick_skipped(tmp_path):
    """B3: _orbital_tick() is a no-op when blocked — lively nodes' displacement
    is left untouched (free evolution suspended during read-only mode)."""
    overrides = dict(orbital_tick_enabled=True, dream_interval_seconds=0.0)
    async with _engine(tmp_path, **overrides) as eng:
        ids = await eng.index_documents([
            {"content": "orbital node one", "metadata": {"source": "user"}},
            {"content": "orbital node two", "metadata": {"source": "user"}},
        ])
        dim = eng.config.embedding_dim
        vel = np.ones(dim, dtype=np.float32) * 0.1  # well above lively_v_min
        baseline = {}
        for nid in ids:
            eng.cache.set_velocity(nid, vel)
            eng.cache.set_displacement(nid, np.zeros(dim, dtype=np.float32))
            baseline[nid] = eng.cache.get_displacement(nid).copy()

        _mark_persist_blocked(eng)
        eng._orbital_tick()  # would integrate displacement if not skipped

        for nid in ids:
            assert np.array_equal(
                eng.cache.get_displacement(nid), baseline[nid],
            ), "orbital tick integrated displacement despite block"


@pytest.mark.asyncio
async def test_reads_succeed_when_blocked(tmp_path):
    """B4: read-only operations stay available when blocked — they must NOT
    raise LeaseLostError. (Engine-layer analogues of recall/get_node/reflect:
    query is the recall read path, cache.get_node the node fetch, get_relations
    the typed-edge read. recall/reflect themselves are service-layer
    compositions over these.) This is a regression guard: it passes today and
    must keep passing once WP-4 lands."""
    async with _engine(tmp_path) as eng:
        ids = await eng.index_documents([
            {"content": "read me lease guard", "metadata": {"source": "user"}},
        ])
        nid = ids[0]
        _mark_persist_blocked(eng)

        assert isinstance(await eng.query("read"), list)
        assert eng.cache.get_node(nid) is not None
        assert isinstance(await eng.get_relations(nid), list)


# ===========================================================================
# Section C — lease-loss race (heartbeat detects a foreign owner)
# ===========================================================================

@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_lease_loss_transitions_to_readonly(tmp_path, caplog):
    """C1: rewriting owner.lock to a foreign owner id, then waiting one
    heartbeat, flips both persist latches and logs the loss."""
    overrides = dict(
        owner_lease_enabled=True,
        lease_heartbeat_seconds=0.1,
        lease_stale_seconds=10.0,
    )
    async with _engine(tmp_path, **overrides) as eng:
        with caplog.at_level(logging.ERROR):
            _install_foreign_owner(tmp_path)
            for _ in range(20):
                await asyncio.sleep(0.05)
                if getattr(eng, "_persist_blocked", False):
                    break
        assert getattr(eng, "_persist_blocked", False) is True
        assert getattr(eng.cache, "persist_blocked", False) is True
        assert any(
            "lease" in rec.message.lower() or "owner" in rec.message.lower()
            for rec in caplog.records
        )


@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_dirty_cache_not_flushed_after_lease_loss(tmp_path):
    """C2: a node dirtied *after* the loss is detected is never flushed — the
    read-only latch gates the write-behind loop, not just the mutators."""
    overrides = dict(
        owner_lease_enabled=True,
        lease_heartbeat_seconds=0.1,
        lease_stale_seconds=10.0,
        flush_interval_seconds=0.05,
    )
    async with _engine(tmp_path, **overrides) as eng:
        _install_foreign_owner(tmp_path)
        # wait for detection first, THEN dirty a node (avoids a pre-loss flush)
        for _ in range(20):
            await asyncio.sleep(0.05)
            if getattr(eng, "_persist_blocked", False):
                break
        eng.cache.set_node(NodeState(id="c2-post-loss", mass=2.5), dirty=True)
        await asyncio.sleep(0.3)  # several flush ticks
        assert "c2-post-loss" not in await eng.store.get_node_states(["c2-post-loss"])


@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_shutdown_after_lease_loss_no_flush_no_delete(tmp_path):
    """C3: after a detected loss, shutdown skips the final cache flush + FAISS
    save, and release() leaves the foreign owner's lock intact."""
    overrides = dict(
        owner_lease_enabled=True,
        lease_heartbeat_seconds=0.1,
        lease_stale_seconds=10.0,
        flush_interval_seconds=0.05,
        faiss_save_interval_seconds=0.1,
    )
    eng = _make_engine(tmp_path, **overrides)
    await eng.startup()
    faiss_sig_before = None
    try:
        await eng.index_documents([
            {"content": "c3 persist guard doc", "metadata": {"source": "user"}},
        ])
        assert await _wait_for_faiss_save(eng, count=1), "baseline save missing"
        faiss_sig_before = _file_signature(eng.config.faiss_index_path)

        _install_foreign_owner(tmp_path)
        for _ in range(20):
            await asyncio.sleep(0.05)
            if getattr(eng, "_persist_blocked", False):
                break
        # dirty cache + faiss after the loss; loops should skip in GREEN
        eng.cache.set_node(NodeState(id="c3-post-loss", mass=3.0), dirty=True)
        eng.faiss_index.add(
            eng.embedder.encode_query("c3 second vector"), ["c3-second"],
        )
        eng._faiss_dirty = True
        await asyncio.sleep(0.3)
    finally:
        await eng.shutdown()

    fresh = SqliteStore(db_path=eng.config.db_path)
    await fresh.initialize()
    try:
        assert "c3-post-loss" not in await fresh.get_node_states(["c3-post-loss"])
    finally:
        await fresh.close()
    assert _file_signature(eng.config.faiss_index_path) == faiss_sig_before, (
        "final FAISS save wrote despite block"
    )
    # release() must not delete a foreign owner's lock
    assert _read_lock(tmp_path)["owner_id"] == FOREIGN_OWNER_ID


# ===========================================================================
# Section D — lifecycle (acquire / reject / release / stale takeover)
# ===========================================================================

@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_second_engine_rejected(tmp_path):
    """D1: a second engine on the same data_dir cannot start while the first
    holds the lease — startup raises LeaseHeldError."""
    lease = dict(owner_lease_enabled=True, lease_heartbeat_seconds=10.0,
                 lease_stale_seconds=60.0)
    eng_a = _make_engine(tmp_path, **lease)
    await eng_a.startup()
    try:
        eng_b = _make_engine(
            tmp_path, **lease,
            flush_interval_seconds=999.0, faiss_save_interval_seconds=0.0,
        )
        started = False
        try:
            with pytest.raises(LeaseHeldError):
                await eng_b.startup()
                started = True  # only reached in RED (startup did not raise)
        finally:
            if started:
                await eng_b.shutdown()
    finally:
        await eng_a.shutdown()


@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_engine_acquires_after_release(tmp_path):
    """D2: once engine A shuts down (releasing the lease), engine B acquires it
    and owns owner.lock."""
    lease = dict(owner_lease_enabled=True, lease_heartbeat_seconds=10.0,
                 lease_stale_seconds=60.0)
    eng_a = _make_engine(tmp_path, **lease)
    await eng_a.startup()
    await eng_a.shutdown()

    eng_b = _make_engine(tmp_path, **lease)
    await eng_b.startup()
    try:
        lock_path = Path(tmp_path) / OWNER_LOCK_FILENAME
        assert lock_path.exists(), "owner.lock missing after B acquired"
        lock = _read_lock(tmp_path)
        b_owner = getattr(getattr(eng_b, "_lease", None), "owner_id", None)
        assert lock["owner_id"] == b_owner
    finally:
        await eng_b.shutdown()


@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_stale_takeover_on_startup(tmp_path, caplog):
    """D3: a pre-existing owner.lock whose heartbeat is past lease_stale_seconds
    is taken over on startup (new owner, takeover_count incremented, WARNING)."""
    stale = time.time() - 120.0  # well past lease_stale_seconds below
    _write_full_lock(
        tmp_path, owner_id="dead-owner", heartbeat_at=stale, takeover_count=0,
    )
    overrides = dict(
        owner_lease_enabled=True,
        lease_stale_seconds=10.0,
        lease_heartbeat_seconds=10.0,
    )
    eng = _make_engine(tmp_path, **overrides)
    with caplog.at_level(logging.WARNING):
        await eng.startup()
    try:
        lock = _read_lock(tmp_path)
        assert lock["owner_id"] != "dead-owner"
        assert lock["takeover_count"] >= 1
    finally:
        await eng.shutdown()
    assert any(
        "stale" in rec.message.lower() and "takeover" in rec.message.lower()
        for rec in caplog.records
    )


# ===========================================================================
# Section E — managed forces lease + default-OFF creates no lock
# ===========================================================================

@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_managed_forces_lease(tmp_path):
    """E1: manifest.managed=True forces lease acquisition even when
    owner_lease_enabled=False — startup creates owner.lock."""
    config = _make_config(tmp_path, owner_lease_enabled=False)
    write_manifest(tmp_path, UniverseManifest(
        universe_id="default",
        embedder_id=config.model_name,
        embedder_version="unpinned",
        embedding_dim=config.embedding_dim,
        created_at=time.time(),
        managed=True,
    ))
    eng = _make_engine_from_config(config)
    await eng.startup()
    try:
        assert (Path(tmp_path) / OWNER_LOCK_FILENAME).exists()
    finally:
        await eng.shutdown()


@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_default_off_creates_no_lock(tmp_path):
    """E2: with the lease off and the manifest unmanaged (defaults), startup
    touches no lease artefact — the 'default 不変' proof. This is a regression
    guard: it passes today and must keep passing once WP-4 lands."""
    async with _engine(tmp_path, owner_lease_enabled=False) as eng:
        assert not (Path(tmp_path) / OWNER_LOCK_FILENAME).exists()
        assert not (Path(tmp_path) / OWNER_GUARD_FILENAME).exists()
        assert getattr(eng, "_lease", None) is None


# ===========================================================================
# Section F — CLI (force-takeover flag + proxy spawn env propagation)
# ===========================================================================

def test_force_takeover_flag_sets_config(monkeypatch):
    """F1: ``--force-takeover`` is accepted by mcp_server.main's parser and
    propagates to config.lease_force_takeover via the GAOTTT_* env layer (so the
    spawned backend + this process's config both pick it up)."""
    import sys

    monkeypatch.setattr(sys, "argv", ["mcp_server", "--force-takeover"])
    monkeypatch.delenv("GAOTTT_LEASE_FORCE_TAKEOVER", raising=False)

    async def _noop_proxy(*args, **kwargs):
        return None

    # Neutralise the server tail so main() returns after parsing.
    monkeypatch.setattr(
        "gaottt.server.mcp_proxy.run_proxy", _noop_proxy, raising=False,
    )

    from gaottt.server import mcp_server
    mcp_server.main()  # RED: argparse rejects --force-takeover -> SystemExit

    assert os.environ.get("GAOTTT_LEASE_FORCE_TAKEOVER"), (
        "--force-takeover did not propagate to GAOTTT_LEASE_FORCE_TAKEOVER"
    )
    assert GaOTTTConfig.from_config_file().lease_force_takeover is True


def test_proxy_spawn_env_propagates(monkeypatch, tmp_path):
    """F2: when the proxy spawns the backend, the force-takeover flag is
    forwarded — either as a CLI arg on the spawned command or as an explicit
    env entry. No real process is started."""
    monkeypatch.setenv("GAOTTT_LEASE_FORCE_TAKEOVER", "1")
    from gaottt.server import mcp_proxy

    captured: dict = {}

    class _FakeProc:
        pid = 4242

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs.get("env")
        stdout = kwargs.get("stdout")
        if stdout is not None:
            with contextlib.suppress(Exception):
                stdout.close()
        return _FakeProc()

    monkeypatch.setattr(mcp_proxy.subprocess, "Popen", _fake_popen)
    mcp_proxy._spawn_backend_detached(
        "127.0.0.1", 7878, 300.0, tmp_path / "spawn.log",
    )

    cmd = captured.get("cmd", [])
    env = captured.get("env")
    propagated = (
        "--force-takeover" in cmd
        or (isinstance(env, dict) and env.get("GAOTTT_LEASE_FORCE_TAKEOVER"))
    )
    assert propagated, (
        f"force-takeover not forwarded to spawned backend: cmd={cmd} env={env}"
    )


# ===========================================================================
# Section G — MV2 hardening (Codex final review 2 blocking issues)
#   B1: shutdown closes the lease-loss blind window between heartbeat-stop
#       and final flush with a final ownership revalidation.
#   B2: prefetch / dream loop honour the read-only latch because the
#       ``passive`` guard lives inside ``_query_internal``, not just ``query``.
# ===========================================================================

@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_shutdown_lease_loss_window_no_stale_write(tmp_path):
    """B1 regression — the blind window between heartbeat-stop and final-flush
    is closed.

    Sequence that only the heartbeat could previously see: the heartbeat loop
    is STOPPED, THEN a foreign owner takes over. With the heartbeat gone no
    tick will ever detect the mismatch, so without a shutdown-time
    revalidation the final cache flush + FAISS save would run with
    ``_persist_blocked`` still False — a stale write that clobbers the new
    owner. The fix re-reads ownership once during shutdown and latches the
    block so both final writes no-op and release() leaves the foreign lock.
    """
    overrides = dict(
        owner_lease_enabled=True,
        lease_heartbeat_seconds=10.0,   # long: no spontaneous heartbeat tick
        lease_stale_seconds=60.0,
        flush_interval_seconds=0.05,   # write-behind live during shutdown
        faiss_save_interval_seconds=0.0,
        virtual_faiss_save_interval_seconds=0.0,
    )
    eng = _make_engine(tmp_path, **overrides)
    await eng.startup()
    try:
        await eng.index_documents([
            {"content": "b1 blind-window baseline doc", "metadata": {"source": "user"}},
        ])
        # The final flush's stale write target: a dirty cache node that has
        # not reached SQLite yet. (index_documents persists its own nodes
        # synchronously, so we dirty an independent node to assert the FLUSH
        # path specifically — mirroring the C3 pattern.)
        eng.cache.set_node(NodeState(id="b1-stale-target", mass=2.0), dirty=True)

        # Open the blind window: stop the heartbeat and await its exit so no
        # later tick can detect the foreign owner first.
        assert eng._lease_stop is not None and eng._lease_task is not None
        eng._lease_stop.set()
        await eng._lease_task
        assert eng._persist_blocked is False, "precondition: block not yet latched"
        # Takeover lands inside the blind window.
        _install_foreign_owner(tmp_path)
    finally:
        await eng.shutdown()

    fresh = SqliteStore(db_path=eng.config.db_path)
    await fresh.initialize()
    try:
        assert "b1-stale-target" not in await fresh.get_node_states(
            ["b1-stale-target"],
        ), "final flush wrote despite takeover inside the blind window"
    finally:
        await fresh.close()
    # release() is owner_id-guarded: the foreign owner's lock survives.
    assert _read_lock(tmp_path)["owner_id"] == FOREIGN_OWNER_ID


@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_prefetch_passive_when_blocked(tmp_path):
    """B2 regression — prefetch() (and the dream loop) route through
    ``_query_internal`` directly, bypassing ``query()``'s ``_persist_blocked →
    passive`` guard. The guard must therefore live inside
    ``_query_internal`` so EVERY caller honours the read-only latch. A blocked
    prefetch still returns results, but mass / displacement / return_count are
    left untouched.
    """
    async with _engine(tmp_path) as eng:
        ids = await eng.index_documents([
            {"content": "b2 alpha corpus", "metadata": {"source": "user"}},
            {"content": "b2 beta corpus", "metadata": {"source": "user"}},
        ])
        assert len(ids) == 2
        # Snapshot mass + displacement magnitude before the blocked prefetch.
        pre = {}
        for nid in ids:
            st = eng.cache.get_node(nid)
            disp = eng.cache.get_displacement(nid)
            pre[nid] = (
                float(st.mass) if st else None,
                float(np.linalg.norm(disp)) if disp is not None else 0.0,
            )
            assert st is not None and st.return_count == 0.0

        # Flip the read-only latch exactly as lease loss would.
        _mark_persist_blocked(eng)

        task = eng.prefetch("b2 alpha corpus", top_k=2)
        results = await task  # synchronous schedule() returns an awaitable Task

        # Prefetch completed without raising and surfaced the corpus.
        assert isinstance(results, list)
        assert len(results) >= 1
        # Field untouched: passive contract holds for the prefetch path too.
        for nid in ids:
            st = eng.cache.get_node(nid)
            disp = eng.cache.get_displacement(nid)
            post_mass = float(st.mass) if st else None
            post_disp = float(np.linalg.norm(disp)) if disp is not None else 0.0
            assert post_mass == pre[nid][0], f"mass mutated by blocked prefetch: {nid}"
            assert post_disp == pre[nid][1], (
                f"displacement mutated by blocked prefetch: {nid}"
            )
            assert st.return_count == 0.0, (
                f"return_count mutated by blocked prefetch: {nid}"
            )
