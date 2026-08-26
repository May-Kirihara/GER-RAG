"""Phase U WP-1 — promoted-combination tests for the Stage 3/4 default-ON
promotion (``direct_qualification_enabled`` / ``ttt_qualification_enabled``).

Coverage required by docs/wiki/Plans-Phase-U-Review-Hardening.md §4 WP-1:

- default pin: both flags default True on a bare GaOTTTConfig (fence).
- promoted combination (direct + ttt + explore all ON): the Phase T
  Stage 4 update-category contract, enumerated precisely —
  * gated (unqualified candidates must NOT train): mass growth
    (confidence-scaled), query kick, co-occurrence edge creation
  * ungated (must keep updating for everyone): last_access,
    evaporation inputs, temperature/sim_history, orbital N-body
    displacement, return_count for presented nodes
  * dream/synthetic recalls keep the exemption (learn set unrestricted).
- config matrix: direct × ttt 4 combinations asserting the documented
  behavior differences (qualified-first ordering only with direct ON;
  learn-set restriction only with ttt ON).
- MCP formatter: recall breakdown shows the ``q/d/f/gap`` segments on a
  default-config engine (no flag pins — the promoted default itself).

Deterministic StubEmbedder (md5-seeded token vectors, dim=64), same
convention as test_engine_ttt_qualification.py. Measured raw cosines vs
QUERY="quantum gravity wave general relativity": DOC_A1 +0.79,
DOC_A2 (5 shared query tokens + 1 extra) ≈ +0.79, DOC_B +0.041,
DOC_WEAK -0.141 — A1/A2 qualified via the raw axis @0.75, B/WEAK fail
every axis.
"""
from __future__ import annotations

import hashlib
import time

import numpy as np
import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.index.bm25_index import BM25Index
from gaottt.index.faiss_index import FaissIndex
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore


class StubEmbedder:
    """Deterministic embedder: keyword-overlap controls similarity."""

    def __init__(self, dimension: int = 64):
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


QUERY = "quantum gravity wave general relativity"
DOC_A1 = "quantum gravity wave general relativity lecture notes extra"
DOC_A2 = "quantum gravity wave general relativity tutorial"
DOC_B = "quantum cooking pasta recipe kitchen tomato basil olive"
DOC_WEAK = "completely unrelated filler text about gardening tools"
# N-body pair: mutual token overlap ~6/7 (raw cosine ≈ 0.95) but zero
# query-token overlap — gravity neighbours of each other, unqualified
# against QUERY on every axis.
DOC_N1 = "orchard apple harvest cider press autumn"
DOC_N2 = "orchard apple harvest cider press vintage"

# The promoted production combination under test — all three Phase T
# presentation/update flags explicitly ON (mirrors the code defaults for
# direct/ttt after WP-1; explore follows once WP-5 flips it).
PROMOTED = dict(
    direct_qualification_enabled=True,
    ttt_qualification_enabled=True,
    explore_diversified_presentation_enabled=True,
)

# Kick isolation (same seam as test_engine_ttt_qualification.KICK_ISOLATION):
# gravity_G=0 and anchor=0 make displacement a direct observable of the
# query-kick gate alone.
KICK_ISOLATION = dict(
    gravity_G=0.0,
    orbital_anchor_strength=0.0,
    mass_anchor_threshold=0.0,
    mass_anchor_extra_strength=0.0,
    mass_bh_enabled=False,
    query_kick_enabled=True,
    query_kick_strength=0.5,
)


def _make_config(tmp_path, **overrides) -> GaOTTTConfig:
    defaults = dict(
        embedding_dim=64,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "gaottt.db"),
        faiss_index_path=str(tmp_path / "gaottt.faiss"),
        flush_interval_seconds=999.0,
        faiss_save_interval_seconds=0.0,
        dream_enabled=False,
        genesis_kick_enabled=False,
        supernova_enabled=False,
        wave_initial_k=8,
        wave_max_depth=1,
        expose_score_breakdown=True,
        **PROMOTED,
    )
    defaults.update(overrides)
    return GaOTTTConfig(**defaults)


async def _make_engine(tmp_path, **config_overrides) -> GaOTTTEngine:
    cfg = _make_config(tmp_path, **config_overrides)
    eng = GaOTTTEngine(
        config=cfg,
        embedder=StubEmbedder(dimension=64),
        faiss_index=FaissIndex(dimension=64),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=cfg.db_path),
        bm25_index=BM25Index(k1=cfg.bm25_k1, b=cfg.bm25_b, tokenizer=cfg.bm25_tokenizer),
    )
    await eng.startup()
    return eng


def _set_mass(engine: GaOTTTEngine, node_id: str, mass: float) -> None:
    state = engine.cache.get_node(node_id)
    assert state is not None
    state.mass = mass
    engine.cache.set_node(state, dirty=True)


def _backdate(engine: GaOTTTEngine, node_id: str, age_seconds: float) -> None:
    state = engine.cache.get_node(node_id)
    assert state is not None
    state.last_access = time.time() - age_seconds
    engine.cache.set_node(state, dirty=True)


def _disp_norm(engine: GaOTTTEngine, node_id: str) -> float:
    disp = engine.cache.get_displacement(node_id)
    return float(np.linalg.norm(disp)) if disp is not None else 0.0


# --- 0. default pin (regression fence) --------------------------------------


def test_promoted_default_flags(tmp_path):
    """A bare GaOTTTConfig must carry both qualifications flags ON — the
    Phase U WP-1 promotion. Any future flip back to opt-in breaks this
    fence and must be a deliberate, reviewed change."""
    cfg = GaOTTTConfig(data_dir=str(tmp_path))
    assert cfg.direct_qualification_enabled is True
    assert cfg.ttt_qualification_enabled is True


# --- 1. update-category contract: gated vs maintenance -----------------------


async def test_promoted_gated_updates_skip_unqualified(tmp_path):
    """Gated categories (mass growth / query kick) skip the unqualified
    candidate under the promoted combination. The ttt-OFF twin engine is
    the positive control: the same recall does train it, proving the gate
    (not the fixture) is what blocks the update."""
    async def run(sub, ttt_on: bool):
        engine = await _make_engine(sub, ttt_qualification_enabled=ttt_on, **KICK_ISOLATION)
        try:
            ids = await engine.index_documents([
                {"content": DOC_A1, "metadata": {"source": "agent"}},
                {"content": DOC_B, "metadata": {"source": "agent"}},
            ])
            results = await engine.query(text=QUERY, top_k=2)
            assert {r.id for r in results} == set(ids)  # B presented as fallback
            verdict = {r.id: r.score_breakdown.qualified for r in results}
            assert verdict[ids[0]] is True and verdict[ids[1]] is False
            return ids, {
                "mass_b": engine.cache.get_node(ids[1]).mass,
                "disp_b": _disp_norm(engine, ids[1]),
                "disp_a": _disp_norm(engine, ids[0]),
            }
        finally:
            await engine.shutdown()

    gated_dir = tmp_path / "gated"
    gated_dir.mkdir()
    control_dir = tmp_path / "control"
    control_dir.mkdir()

    ids_g, gated = await run(gated_dir, True)
    _ids_c, control = await run(control_dir, False)

    # promoted (gated): unqualified B trains nothing
    assert gated["mass_b"] == 1.0
    assert gated["disp_b"] == 0.0
    # qualified A does train (mass growth + kick) — the gate is selective
    assert gated["disp_a"] > 1e-6
    # control (ttt OFF): same fixture pumps the unqualified node
    assert control["mass_b"] > 1.0
    assert control["disp_b"] > 1e-6


async def test_promoted_maintenance_updates_all_reached(tmp_path):
    """Non-gated categories still update the unqualified candidate under
    the promoted combination: last_access, lazy evaporation inputs,
    sim_history/temperature, orbital N-body displacement, and return_count
    for presented nodes (presentation fact)."""
    engine = await _make_engine(
        tmp_path,
        mass_evaporation_enabled=True,
        mass_evaporation_rate=0.05,
    )
    try:
        ids = await engine.index_documents([
            {"content": DOC_A1, "metadata": {"source": "agent"}},
            {"content": DOC_A2, "metadata": {"source": "agent"}},
            {"content": DOC_B, "metadata": {"source": "agent"}},
            {"content": DOC_N1, "metadata": {"source": "agent"}},
            {"content": DOC_N2, "metadata": {"source": "agent"}},
        ])
        a1, a2, b, n1, n2 = ids
        # B: backdated high mass → evaporation must run (unqualified or not)
        _set_mass(engine, b, 30.0)
        _backdate(engine, b, 20.0 * 86400.0)
        # N1/N2: unqualified gravity-neighbour pair with enough mass to
        # reach each other's field (radius(mass=10) covers cos 0.95) —
        # N-body displacement must move them even though they never train.
        _set_mass(engine, n1, 10.0)
        _set_mass(engine, n2, 10.0)

        results = await engine.query(text=QUERY, top_k=5)
        presented = {r.id for r in results}
        assert {b, n1, n2} <= presented  # fallback fill presents them

        recovery = 1.0 - engine.config.habituation_recovery_rate
        state_b = engine.cache.get_node(b)
        # evaporation ran (mass fell), growth did not restore it
        assert state_b.mass < 29.0, "evaporation must run for unqualified nodes"
        assert state_b.mass > engine.config.mass_evaporation_floor
        # last_access refreshed
        assert state_b.last_access > time.time() - 10.0
        # sim_history appended (temperature stays 0 here — the seed force
        # is query-independent across the two recalls below)
        await engine.query(text=QUERY, top_k=5)
        assert len(engine.cache.get_node(b).sim_history) == 2
        # return_count: presentation fact recorded for unqualified too.
        # Two presented recalls: (0→1, ×r) then (+1, ×r) → r·(1+r).
        assert engine.cache.get_node(b).return_count == pytest.approx(
            recovery * (1.0 + recovery),
        )
        # orbital N-body displacement: the unqualified pair moved each other
        assert _disp_norm(engine, n1) > 0.0, (
            "N-body participation must survive the qualification gate"
        )
        assert _disp_norm(engine, n2) > 0.0
        # while neither grew (10.0 pinned — fresh, inside evaporation grace)
        assert engine.cache.get_node(n1).mass == 10.0
        assert engine.cache.get_node(n2).mass == 10.0
        # qualified docs did grow (selectivity of the gate)
        assert engine.cache.get_node(a1).mass > 1.0
        assert engine.cache.get_node(a2).mass > 1.0
    finally:
        await engine.shutdown()


async def test_promoted_cooccurrence_presented_intersect_learn(tmp_path):
    """Co-occurrence is gated to presented ∩ learn set: the qualified
    presented pair gets edges; the presented-but-unqualified node gets
    none, while its return_count still records the presentation."""
    engine = await _make_engine(tmp_path, edge_threshold=1)
    try:
        ids = await engine.index_documents([
            {"content": DOC_A1, "metadata": {"source": "agent"}},
            {"content": DOC_A2, "metadata": {"source": "agent"}},
            {"content": DOC_B, "metadata": {"source": "agent"}},
        ])
        a1, a2, b = ids
        results = await engine.query(text=QUERY, top_k=3)
        assert {r.id for r in results} == {a1, a2, b}

        assert set(engine.cache.get_neighbors(a1)) == {a2}
        assert set(engine.cache.get_neighbors(a2)) == {a1}
        assert engine.cache.get_neighbors(b) == {}
        recovery = 1.0 - engine.config.habituation_recovery_rate
        assert engine.cache.get_node(b).return_count == pytest.approx(recovery)
    finally:
        await engine.shutdown()


async def test_promoted_synthetic_recall_keeps_exemption(tmp_path):
    """Dream/synthetic recalls are exempt from the learn-set restriction
    (plan §3 Stage 4): an unqualified node that an active recall refuses
    to train DOES train under ``_is_synthetic=True`` — self-directed
    maintenance rehearsal, not a user-query gradient path."""
    engine = await _make_engine(tmp_path, edge_threshold=1, **KICK_ISOLATION)
    try:
        ids = await engine.index_documents([
            {"content": DOC_A1, "metadata": {"source": "agent"}},
            {"content": DOC_B, "metadata": {"source": "agent"}},
        ])
        a, b = ids

        # Active recall: the gate holds.
        await engine.query(text=QUERY, top_k=2)
        assert engine.cache.get_node(b).mass == 1.0
        assert engine.cache.get_neighbors(b) == {}

        # Synthetic recall: learn set unrestricted — B grows and edges form.
        await engine._query_internal(
            text=QUERY, top_k=2, wave_depth=None, wave_k=None,
            _is_synthetic=True,
        )
        assert engine.cache.get_node(b).mass > 1.0, (
            "dream exemption broken: synthetic recall left the "
            "unqualified node untrained"
        )
        assert a in engine.cache.get_neighbors(b), (
            "dream exemption broken: synthetic recall built no edges"
        )
    finally:
        await engine.shutdown()


# --- 2. config matrix: direct × ttt ------------------------------------------


async def test_config_matrix_direct_ttt(tmp_path):
    """4-combination matrix on the mass-dominance pathology fixture
    (A qualified + semantically decayed, B unqualified + mass 48):
      - qualified-first ordering appears ONLY with direct ON
        (legacy [B, A] order with direct OFF, regardless of ttt)
      - learn-set restriction appears ONLY with ttt ON
        (B's mass grows with ttt OFF, stays pinned with ttt ON)
    """
    decay = dict(semantic_half_life_seconds=86400.0, semantic_floor=0.0)

    async def run(sub, direct_on: bool, ttt_on: bool):
        engine = await _make_engine(
            sub,
            direct_qualification_enabled=direct_on,
            ttt_qualification_enabled=ttt_on,
            **decay,
        )
        try:
            ids = await engine.index_documents([
                {"content": DOC_A1, "metadata": {"source": "agent"}},
                {"content": DOC_B, "metadata": {"source": "agent"}},
            ])
            a, b = ids
            _set_mass(engine, b, 48.0)
            _backdate(engine, a, 10.0 * 86400.0)
            # The "A decayed below B" premise only holds on the FIRST
            # recall — every recall refreshes last_access, so A's decay
            # factor returns to 1.0 from recall 2 on. Capture the order
            # from recall 1 (the legacy-order observable), then repeat to
            # make the mass-growth difference measurable.
            order = None
            for _ in range(5):
                results = await engine.query(text=QUERY, top_k=2)
                if order is None:
                    order = [r.id for r in results]
            return a, b, order, engine.cache.get_node(b).mass
        finally:
            await engine.shutdown()

    cases = [
        # (direct, ttt) → (expected order, B trains?)
        (False, False, "legacy", True),
        (False, True, "legacy", False),
        (True, False, "qualified_first", True),
        (True, True, "qualified_first", False),
    ]
    for direct_on, ttt_on, order_kind, b_trains in cases:
        sub = tmp_path / f"d{int(direct_on)}t{int(ttt_on)}"
        sub.mkdir()
        a, b, order, mass_b = await run(sub, direct_on, ttt_on)
        if order_kind == "legacy":
            assert order == [b, a], (direct_on, ttt_on, order)
        else:
            assert order == [a, b], (direct_on, ttt_on, order)
        if b_trains:
            assert mass_b > 48.0, (direct_on, ttt_on, mass_b)
        else:
            assert mass_b == 48.0, (direct_on, ttt_on, mass_b)


# --- 3. MCP formatter: q/d/f/gap on the default config -----------------------


async def test_promoted_breakdown_segments_mcp(tmp_path, monkeypatch):
    """MCP recall output shows the Phase T Stage 3 breakdown segments
    (``q=±`` / ``d=`` / ``f=`` / ``gap=``) on a DEFAULT-config engine —
    no flag pins, so the promoted defaults themselves are what renders
    the segments (the Phase U R1 acceptance)."""
    from gaottt.server import mcp_server as srv

    cfg = GaOTTTConfig(
        embedding_dim=32,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "gaottt.db"),
        faiss_index_path=str(tmp_path / "gaottt.faiss"),
        flush_interval_seconds=999.0,
    )
    # fixture guard: this test is only meaningful with the promoted
    # defaults in force (an accidental future flag pin here would
    # silently stop testing the default config).
    assert cfg.direct_qualification_enabled is True
    assert cfg.ttt_qualification_enabled is True

    eng = GaOTTTEngine(
        config=cfg,
        embedder=StubEmbedder(dimension=32),
        faiss_index=FaissIndex(dimension=32),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=cfg.db_path),
    )
    await eng.startup()
    monkeypatch.setattr(srv, "_engine", eng)
    try:
        await srv.remember(
            content="qual segment probe alpha beta gamma", source="user",
        )
        await srv.remember(
            content="unrelated cooking notes kitchen pantry", source="user",
        )
        out = await srv.recall(
            query="qual segment probe alpha beta", top_k=3,
        )
        assert "breakdown:" in out
        # qualification verdict + pre-saturation decomposition + gap
        assert " q=+" in out, f"qualified verdict segment missing: {out!r}"
        assert " q=-" in out, f"fallback verdict segment missing: {out!r}"
        assert " d=" in out and " f=" in out and " gap=" in out
    finally:
        monkeypatch.setattr(srv, "_engine", None)
        await eng.shutdown()
