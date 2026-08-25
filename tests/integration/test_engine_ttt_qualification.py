"""Phase T Stage 4 — TTT update qualification (integration, engine.query).

Update-category split (plan §3 Stage 4):

- all reached (maintenance): last_access refresh, lazy evaporation,
  sim_history / temperature, orbital N-body participation
- learn set only (query-conditioned): mass growth (× confidence),
  query kick
- presented ∩ learn set: cooccurrence edges
- all presented: return_count

The learn set is the Stage 3 qualification. Unqualified nodes may still
be *presented* (fallback fill) but must not train — the bad-gradient
self-reinforcement loop (low-relevance high-mass node recalled → grows
→ outranks relevant nodes) is severed.

Deterministic StubEmbedder (dim=64, md5 token seeds). Measured cosines
vs QUERY="quantum gravity wave general relativity": A +0.790 (qualified
via raw @0.75), C +0.621 (qualified via lexical @defaults), B +0.041
(unqualified), WEAK -0.141 (unqualified).
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
QUERY2 = "quantum gravity wave simulation"
DOC_A = "quantum gravity wave general relativity lecture notes extra"
DOC_B = "quantum cooking pasta recipe kitchen tomato basil olive"
DOC_C = "quantum gravity wave simulation update notes"
DOC_WEAK = "completely unrelated filler text about gardening tools"


# Isolate the query kick as the only orbital force so displacement is a
# direct observable of the kick gate: gravity_G=0 (no neighbour pull),
# orbital_anchor_strength=0 (no Hooke), no BH, no Langevin, no Λ.
KICK_ISOLATION = dict(
    gravity_G=0.0,
    orbital_anchor_strength=0.0,
    mass_anchor_threshold=0.0,       # kick gate = 1.0 (Stage 3 legacy mode)
    mass_anchor_extra_strength=0.0,
    mass_bh_enabled=False,
    query_kick_enabled=True,
    query_kick_strength=0.5,         # exaggerated for observability
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
        supernova_enabled=False,       # keep initial velocities out of the
                                       # displacement observables
        wave_initial_k=8,
        wave_max_depth=1,
        direct_qualification_enabled=True,
        ttt_qualification_enabled=True,
        expose_score_breakdown=True,
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


async def _index_ab(engine: GaOTTTEngine) -> dict[str, str]:
    ids = await engine.index_documents([
        {"content": DOC_A, "metadata": {"source": "agent"}},
        {"content": DOC_B, "metadata": {"source": "agent"}},
    ])
    return {"A": ids[0], "B": ids[1]}


async def test_passive_purity_all_fields_unchanged(tmp_path):
    engine = await _make_engine(tmp_path)
    try:
        ids = await _index_ab(engine)
        _set_mass(engine, ids["B"], 5.0)
        before = {
            nid: (
                engine.cache.get_node(nid).mass,
                engine.cache.get_node(nid).return_count,
                engine.cache.get_node(nid).last_access,
                _disp_norm(engine, nid),
            )
            for nid in ids.values()
        }
        edges_before = engine.get_graph()

        await engine.query(text=QUERY, top_k=2, passive=True)

        for nid, (mass, rc, la, dn) in before.items():
            state = engine.cache.get_node(nid)
            assert state.mass == mass
            assert state.return_count == rc
            assert state.last_access == la
            assert _disp_norm(engine, nid) == dn
        assert engine.get_graph() == edges_before
    finally:
        await engine.shutdown()


async def test_unqualified_presented_node_not_trained(tmp_path):
    """B is presented (fallback fill) — return_count records the
    presentation fact — but mass growth and the query kick skip it. A
    (qualified) receives both. Kick-isolation config makes displacement
    a direct kick observable."""
    engine = await _make_engine(tmp_path, **KICK_ISOLATION)
    try:
        ids = await _index_ab(engine)
        results = await engine.query(text=QUERY, top_k=2)
        assert {r.id for r in results} == set(ids.values())

        # presentation bookkeeping applies to ALL presented nodes.
        # return_count lands at 0.99, not 1.0: Step 5 bumps it to 1.0 and
        # the habituation-recovery sweep (rate 0.01) then scales every
        # reached node down once.
        recovery = 1.0 - engine.config.habituation_recovery_rate
        assert engine.cache.get_node(ids["B"]).return_count == pytest.approx(recovery)
        assert engine.cache.get_node(ids["A"]).return_count == pytest.approx(recovery)

        # mass growth: learn set only
        assert engine.cache.get_node(ids["A"]).mass > 1.0
        assert engine.cache.get_node(ids["B"]).mass == 1.0

        # query kick: learn set only (no other orbital forces configured)
        assert _disp_norm(engine, ids["A"]) > 1e-6
        assert _disp_norm(engine, ids["B"]) == 0.0

        # the kick really moved A toward the query anchor
        q_emb = engine.embedder.encode_query(QUERY)[0]
        raw_a = engine.faiss_index.get_vectors([ids["A"]])[ids["A"]]
        disp_a = engine.cache.get_displacement(ids["A"])
        kick_dir = q_emb - raw_a
        kick_dir = kick_dir / (np.linalg.norm(kick_dir) + 1e-9)
        assert float(np.dot(disp_a, kick_dir)) > 0.0

        # control: same setup without the Stage 4 gate kicks B too — the
        # per-node gate (not the fixture) is what zeroes B's displacement
        sub = tmp_path / "ungated"
        sub.mkdir()
        control = await _make_engine(
            sub, ttt_qualification_enabled=False, **KICK_ISOLATION,
        )
        try:
            cids = await _index_ab(control)
            await control.query(text=QUERY, top_k=2)
            assert _disp_norm(control, cids["B"]) > 1e-6
        finally:
            await control.shutdown()
    finally:
        await engine.shutdown()


async def test_maintenance_applies_to_all_reached(tmp_path):
    """Unqualified reached nodes still get last_access refresh,
    sim_history/temperature updates and lazy evaporation."""
    engine = await _make_engine(
        tmp_path,
        mass_evaporation_enabled=True,
        mass_evaporation_rate=0.05,
    )
    try:
        ids = await _index_ab(engine)
        _set_mass(engine, ids["B"], 30.0)
        _backdate(engine, ids["B"], 20.0 * 86400.0)  # past the 7d grace

        # two distinct queries → sim_history grows for the unqualified
        # node too (the seed force is query-independent here, so var — and
        # with it temperature — stays 0; the append is the maintenance
        # evidence)
        await engine.query(text=QUERY, top_k=2)
        await engine.query(text=QUERY2, top_k=2)

        state_b = engine.cache.get_node(ids["B"])
        assert state_b.mass < 29.0, "evaporation must run for unqualified nodes"
        assert state_b.mass > engine.config.mass_evaporation_floor
        assert state_b.last_access > time.time() - 10.0
        assert len(state_b.sim_history) == 2, (
            "sim_history updates must run for unqualified nodes"
        )
    finally:
        await engine.shutdown()


async def test_qualified_node_learns(tmp_path):
    engine = await _make_engine(tmp_path)
    try:
        ids = await _index_ab(engine)
        delta: dict = {}
        await engine.query(text=QUERY, top_k=2, out_training_delta=delta)
        assert delta["mass_changes"][ids["A"]] > 0.0
        assert engine.cache.get_node(ids["A"]).mass > 1.0
    finally:
        await engine.shutdown()


async def test_confidence_monotonicity(tmp_path):
    """At raw threshold 0.5 both A (+0.79) and C (+0.62) qualify with
    confidences 0.58 / 0.24; the higher-margin node accrues more mass
    (seed force also favours A, so the direction is robust)."""
    engine = await _make_engine(tmp_path, direct_raw_cosine_min=0.5)
    try:
        ids_list = await engine.index_documents([
            {"content": DOC_A, "metadata": {"source": "agent"}},
            {"content": DOC_C, "metadata": {"source": "agent"}},
            {"content": DOC_B, "metadata": {"source": "agent"}},
        ])
        ida, idc, idb = ids_list
        delta: dict = {}
        await engine.query(text=QUERY, top_k=3, out_training_delta=delta)
        assert delta["mass_changes"][ida] > delta["mass_changes"][idc] > 0.0
        assert delta["mass_changes"][idb] == 0.0
    finally:
        await engine.shutdown()


async def test_false_candidate_self_reinforcement_regression(tmp_path):
    """The bad-gradient loop: an unrelated high-mass node B repeatedly
    surfacing as a fallback must NOT grow or climb ranks. With the gate
    off (twin engine) the same loop does grow B — demonstrating the
    blocked pathology."""
    async def run(sub, ttt_on: bool):
        engine = await _make_engine(
            sub, ttt_qualification_enabled=ttt_on,
            direct_qualification_enabled=True,
        )
        try:
            ids = await _index_ab(engine)
            _set_mass(engine, ids["B"], 30.0)
            mass_b = engine.cache.get_node(ids["B"]).mass
            top_ids = []
            for _ in range(10):
                results = await engine.query(text=QUERY, top_k=2)
                top_ids.append([r.id for r in results])
                # B never trains under the gate
                if ttt_on:
                    assert engine.cache.get_node(ids["B"]).mass == mass_b
            final_b = engine.cache.get_node(ids["B"]).mass
            return ids, top_ids, final_b
        finally:
            await engine.shutdown()

    gated_dir = tmp_path / "gated"
    gated_dir.mkdir()
    ungated_dir = tmp_path / "ungated"
    ungated_dir.mkdir()
    ids_g, tops_g, mass_b_gated = await run(gated_dir, True)
    ids_u, tops_u, mass_b_ungated = await run(ungated_dir, False)

    # gated: qualified A leads every round; B's mass is bit-identical
    assert all(t[0] == ids_g["A"] for t in tops_g)
    assert mass_b_gated == 30.0
    # ungated: the same recall loop pumps the unrelated node
    assert mass_b_ungated > 30.0


async def test_single_qualified_among_many_orbital_not_degenerate(tmp_path):
    """learn set of 1 with several unqualified reached nodes: the orbital
    N-body update still runs over all reached (≥2 participants) and moves
    the qualified node."""
    engine = await _make_engine(tmp_path, **KICK_ISOLATION)
    try:
        ids_list = await engine.index_documents([
            {"content": DOC_A, "metadata": {"source": "agent"}},
            {"content": DOC_B, "metadata": {"source": "agent"}},
            {"content": DOC_WEAK, "metadata": {"source": "agent"}},
        ])
        ida = ids_list[0]
        await engine.query(text=QUERY, top_k=3)
        assert _disp_norm(engine, ida) > 1e-6
        for nid in ids_list[1:]:
            assert _disp_norm(engine, nid) == 0.0
    finally:
        await engine.shutdown()


async def test_training_delta_zero_for_unqualified_topk(tmp_path):
    engine = await _make_engine(tmp_path)
    try:
        ids = await _index_ab(engine)
        _set_mass(engine, ids["B"], 10.0)
        delta: dict = {}
        results = await engine.query(text=QUERY, top_k=2, out_training_delta=delta)
        assert {r.id for r in results} == set(ids.values())
        assert delta["mass_changes"][ids["B"]] == 0.0
        assert delta["mass_changes"][ids["A"]] > 0.0
    finally:
        await engine.shutdown()


async def test_flag_combinations(tmp_path):
    """(S3 off, S4 on): legacy ordering, learning still gated.
    (S3 on, S4 off): qualified-first ordering, learning ungated."""
    # A decayed + B mass-boosted → legacy final_score order is [B, A].
    decay = dict(semantic_half_life_seconds=86400.0, semantic_floor=0.0)

    async def setup(engine):
        ids = await _index_ab(engine)
        _set_mass(engine, ids["B"], 48.0)
        _backdate(engine, ids["A"], 10.0 * 86400.0)
        return ids

    # (S3 off, S4 on)
    sub = tmp_path / "s3off"
    sub.mkdir()
    engine = await _make_engine(
        sub, direct_qualification_enabled=False, ttt_qualification_enabled=True,
        **decay,
    )
    try:
        ids = await setup(engine)
        results = await engine.query(text=QUERY, top_k=2)
        assert [r.id for r in results] == [ids["B"], ids["A"]]  # legacy order
        # qualification still computed for the learning gate
        assert results[0].score_breakdown.qualified is False
        assert engine.cache.get_node(ids["B"]).mass == 48.0
        assert engine.cache.get_node(ids["A"]).mass > 1.0
    finally:
        await engine.shutdown()

    # (S3 on, S4 off)
    sub = tmp_path / "s4off"
    sub.mkdir()
    engine = await _make_engine(
        sub, direct_qualification_enabled=True, ttt_qualification_enabled=False,
        **decay,
    )
    try:
        ids = await setup(engine)
        for _ in range(5):
            results = await engine.query(text=QUERY, top_k=2)
        assert [r.id for r in results] == [ids["A"], ids["B"]]  # qualified-first
        # learning ungated: even the unqualified fallback node accretes
        assert engine.cache.get_node(ids["B"]).mass > 48.0
    finally:
        await engine.shutdown()
