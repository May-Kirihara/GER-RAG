"""Phase T Stage 3 — direct relevance qualification (integration, engine.query).

Deterministic StubEmbedder (md5-seeded token vectors, dim=64). Measured
cosines against QUERY="quantum gravity wave general relativity":

    A     +0.7900   (qualified via raw axis @0.75)
    C     +0.6208   (qualified via lexical axis @defaults: bm25 15.9, rel 0.54)
    B     +0.0411   (unqualified: cos<0.75, bm25 4.9 rel 0.165)
    LEX   +0.0180   (unqualified for QUERY; for LEX_QUERY bm25 39.1 rel 1.0)
    WEAK  -0.1412   (unqualified)

The mass-dominance pathology fixture: A backdated (semantic decayed to
~0 with 1-day half-life, floor 0) + unrelated B with mass pumped to 48
→ legacy order [B, A]; qualified-first order [A, B] with B demoted to an
explicit fallback (``qualified=False``).
"""
from __future__ import annotations

import hashlib
import time

import numpy as np
import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.core.gravity import compute_virtual_position
from gaottt.index.bm25_index import BM25Index
from gaottt.index.faiss_index import FaissIndex
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore


class StubEmbedder:
    """Deterministic embedder: keyword-overlap controls similarity.

    Each unique whitespace-separated token gets a stable unit basis vector
    (seeded by md5 of the token, so it is consistent across processes).
    A text's embedding is the L2-normalized sum of its token vectors.
    """

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
LEX_QUERY = "zeta protocol handshake"
DOC_A = "quantum gravity wave general relativity lecture notes extra"
DOC_B = "quantum cooking pasta recipe kitchen tomato basil olive"
DOC_C = "quantum gravity wave simulation update notes"
DOC_LEX = "zeta protocol handshake zeta protocol handshake review summary"
DOC_WEAK = "completely unrelated filler text about gardening tools"
# Fill-corpus docs share exactly one token ("zeta") with LEX_QUERY: mildly
# positive cosines (+0.14..+0.24, finals stay > 0) but far below every
# relevance axis — deterministic fallback filler regardless of corpus size.
DOC_BZ = "zeta cooking pasta recipe kitchen tomato basil olive"
DOC_AZ = "zeta gravity lecture notes draft appendix"
DOC_CZ = "zeta wave simulation update details"


def _make_config(tmp_path, **overrides) -> GaOTTTConfig:
    defaults = dict(
        embedding_dim=64,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "gaottt.db"),
        faiss_index_path=str(tmp_path / "gaottt.faiss"),
        flush_interval_seconds=999.0,   # disable background flush in tests
        faiss_save_interval_seconds=0.0,
        dream_enabled=False,
        genesis_kick_enabled=False,
        wave_initial_k=8,               # whole fixture corpus reaches the wave
        wave_max_depth=1,
        # Phase T Stage 3/4 under test
        direct_qualification_enabled=True,
        ttt_qualification_enabled=True,
        expose_score_breakdown=True,
    )
    defaults.update(overrides)
    return GaOTTTConfig(**defaults)


async def _make_engine(tmp_path, *, bm25: bool = True, **config_overrides) -> GaOTTTEngine:
    cfg = _make_config(tmp_path, **config_overrides)
    bm25_index = (
        BM25Index(k1=cfg.bm25_k1, b=cfg.bm25_b, tokenizer=cfg.bm25_tokenizer)
        if bm25
        else None
    )
    eng = GaOTTTEngine(
        config=cfg,
        embedder=StubEmbedder(dimension=64),
        faiss_index=FaissIndex(dimension=64),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=cfg.db_path),
        bm25_index=bm25_index,
    )
    await eng.startup()
    return eng


def _backdate(engine: GaOTTTEngine, node_id: str, age_seconds: float) -> None:
    state = engine.cache.get_node(node_id)
    assert state is not None
    state.last_access = time.time() - age_seconds
    engine.cache.set_node(state, dirty=True)


def _set_mass(engine: GaOTTTEngine, node_id: str, mass: float) -> None:
    state = engine.cache.get_node(node_id)
    assert state is not None
    state.mass = mass
    engine.cache.set_node(state, dirty=True)


async def _index_pathology_corpus(engine: GaOTTTEngine) -> dict[str, str]:
    """A (qualified, semantically decayed) + B (unqualified, high mass)."""
    ids = await engine.index_documents([
        {"content": DOC_A, "metadata": {"source": "agent"}},
        {"content": DOC_B, "metadata": {"source": "agent"}},
    ])
    ida, idb = ids[0], ids[1]
    _set_mass(engine, idb, 48.0)
    # 1-day half-life, floor 0 → decay(A) ≈ 0.5**10 after 10 days, so the
    # mass-boosted unrelated node outranks A on final_score (legacy order
    # [B, A]) even though A is the only semantically relevant node.
    _backdate(engine, ida, 10.0 * 86400.0)
    return {"A": ida, "B": idb}


PATHOLOGY_DECAY = dict(
    semantic_half_life_seconds=86400.0,
    semantic_floor=0.0,
)


async def test_qualified_first_ordering_and_fallback_tail(tmp_path):
    engine = await _make_engine(tmp_path, **PATHOLOGY_DECAY)
    try:
        ids = await _index_pathology_corpus(engine)

        results = await engine.query(text=QUERY, top_k=2)
        assert [r.id for r in results] == [ids["A"], ids["B"]]
        assert results[0].score_breakdown.qualified is True
        assert results[1].score_breakdown.qualified is False  # explicit fallback
    finally:
        await engine.shutdown()


async def test_qualified_first_top1_promotion(tmp_path):
    engine = await _make_engine(tmp_path, **PATHOLOGY_DECAY)
    try:
        ids = await _index_pathology_corpus(engine)
        results = await engine.query(text=QUERY, top_k=1)
        assert [r.id for r in results] == [ids["A"]]
    finally:
        await engine.shutdown()


async def test_flag_off_matches_legacy_order(tmp_path):
    """Both qualification flags OFF → legacy final_score order, and the
    breakdown reports qualified=None (qualification never computed)."""
    engine = await _make_engine(
        tmp_path,
        direct_qualification_enabled=False,
        ttt_qualification_enabled=False,
        **PATHOLOGY_DECAY,
    )
    try:
        ids = await _index_pathology_corpus(engine)
        results = await engine.query(text=QUERY, top_k=2)
        # legacy: mass-dominant B first
        assert [r.id for r in results] == [ids["B"], ids["A"]]
        for r in results:
            assert r.score_breakdown.qualified is None
            assert r.score_breakdown.direct_score is None
            assert r.score_breakdown.field_score is None
    finally:
        await engine.shutdown()


async def test_fallback_fill_preserves_result_count(tmp_path):
    """Qualified count below top_k still fills the result to the legacy
    count with fallback items (fill guarantee)."""
    async def run(sub, **overrides):
        engine = await _make_engine(sub, **overrides)
        try:
            all_ids = await engine.index_documents([
                {"content": DOC_LEX, "metadata": {"source": "agent"}},
                {"content": DOC_BZ, "metadata": {"source": "agent"}},
                {"content": DOC_AZ, "metadata": {"source": "agent"}},
                {"content": DOC_CZ, "metadata": {"source": "agent"}},
            ])
            results = await engine.query(text=LEX_QUERY, top_k=4)
            return all_ids, results
        finally:
            await engine.shutdown()

    sub = tmp_path / "qualified"
    sub.mkdir()
    all_ids, results = await run(sub)
    # For LEX_QUERY only DOC_LEX clears any relevance axis; everyone else
    # is a fallback — the result still fills every top_k slot.
    assert len(results) == 4
    assert {r.id for r in results} == set(all_ids)
    assert results[0].id == all_ids[0]
    assert results[0].score_breakdown.qualified is True
    assert all(r.score_breakdown.qualified is False for r in results[1:])

    # legacy run returns the same count (fill parity)
    sub = tmp_path / "legacy"
    sub.mkdir()
    _, legacy_results = await run(
        sub, direct_qualification_enabled=False, ttt_qualification_enabled=False,
    )
    assert len(legacy_results) == 4


async def test_qualification_independent_of_expose_flag(tmp_path):
    """Selection acts identically with expose_score_breakdown off —
    qualification must not depend on the observability flag."""
    for expose in (True, False):
        sub = tmp_path / f"eng-{expose}"
        sub.mkdir()
        engine = await _make_engine(
            sub, expose_score_breakdown=expose, **PATHOLOGY_DECAY,
        )
        try:
            ids = await _index_pathology_corpus(engine)
            results = await engine.query(text=QUERY, top_k=2)
            # same qualified-first ordering in both runs (asserted per
            # engine — node ids differ across engines)
            assert [r.id for r in results] == [ids["A"], ids["B"]]
            if not expose:
                for r in results:
                    assert r.score_breakdown is None
        finally:
            await engine.shutdown()


async def test_lexical_dual_condition(tmp_path):
    """The lexical axis needs BOTH the absolute (off-topic guard) and the
    relative (pool-ratio) condition. Cosine thresholds are set to 0.99 so
    only the lexical axis can qualify anyone."""
    async def qualified_flags(tmp_sub, **thresholds) -> dict[str, bool | None]:
        engine = await _make_engine(
            tmp_sub,
            direct_raw_cosine_min=0.99,
            direct_virtual_cosine_min=0.99,
            **thresholds,
        )
        try:
            ids = await engine.index_documents([
                {"content": DOC_LEX, "metadata": {"source": "agent"}},
                {"content": DOC_B, "metadata": {"source": "agent"}},
            ])
            results = await engine.query(text=LEX_QUERY, top_k=2)
            by_id = {r.id: r.score_breakdown.qualified for r in results}
            return {name: by_id.get(nid) for name, nid in zip(("LEX", "B"), ids)}
        finally:
            await engine.shutdown()

    # default thresholds: LEX has bm25≈39 (abs≥8) and rel=1.0 (≥0.4) → qualified
    sub = tmp_path / "defaults"
    sub.mkdir()
    flags = await qualified_flags(sub)
    assert flags["LEX"] is True
    assert flags["B"] is False

    # absolute arm required: relative 1.0 alone must NOT qualify (the
    # relative-only false positive rejection, end-to-end)
    sub = tmp_path / "abs-off"
    sub.mkdir()
    flags = await qualified_flags(sub, direct_bm25_absolute_min=1e9)
    assert flags["LEX"] is False
    assert flags["B"] is False

    # relative arm required: absolute trivially satisfied but only the
    # pool-top item (rel=1.0) clears rel=0.99
    sub = tmp_path / "rel-only"
    sub.mkdir()
    flags = await qualified_flags(
        sub, direct_bm25_absolute_min=0.0, direct_bm25_relative_min=0.99,
    )
    assert flags["LEX"] is True
    assert flags["B"] is False


async def test_forced_path_order_and_forced_only_not_trained(tmp_path):
    """tag_filter forces injected nodes under the Phase J Stage 3 rule
    (raw-cosine order inside the forced set) — unchanged by qualification.
    A forced-only (injected ∧ unqualified) node is presented but not
    trained (Stage 4 learn-set exclusion). raw threshold 0.5 makes the
    verdicts corpus-size independent (BM25 idf shifts with N)."""
    engine = await _make_engine(tmp_path, direct_raw_cosine_min=0.5)
    try:
        ids = await engine.index_documents([
            {"content": DOC_A, "metadata": {"source": "agent"}},
            {"content": DOC_B, "metadata": {"source": "agent", "tags": ["forced-set"]}},
            {"content": DOC_C, "metadata": {"source": "agent", "tags": ["forced-set"]}},
        ])
        ida, idb, idc = ids
        mass_b_before = engine.cache.get_node(idb).mass

        results = await engine.query(text=QUERY, top_k=3, tag_filter=["forced-set"])
        returned = [r.id for r in results]
        # forced items come first (both engines' rule), ordered inside the
        # forced set by raw cosine: C (+0.62) > B (+0.04).
        assert returned.index(idc) < returned.index(idb)
        assert ida in returned  # natural qualified item fills the rest
        for r in results:
            if r.id == idb:
                assert r.score_breakdown.forced_inclusion is True
                assert r.score_breakdown.qualified is False

        # forced-only unqualified node: presented (return_count bumped —
        # 0.99 after the habituation-recovery sweep) but excluded from the
        # learn set → mass unchanged.
        recovery = 1.0 - engine.config.habituation_recovery_rate
        assert engine.cache.get_node(idb).return_count == pytest.approx(recovery)
        assert engine.cache.get_node(idb).mass == mass_b_before
        # C is injected AND qualified (lexical axis) → stays in the learn set.
        assert engine.cache.get_node(idc).mass > 1.0
        # natural qualified item learns too
        assert engine.cache.get_node(ida).mass > 1.0
    finally:
        await engine.shutdown()


async def test_prefetch_parity_serves_qualified_order(tmp_path):
    engine = await _make_engine(tmp_path, **PATHOLOGY_DECAY)
    try:
        ids = await _index_pathology_corpus(engine)
        task = engine.prefetch(text=QUERY, top_k=2)
        prefetched = await task
        assert [r.id for r in prefetched] == [ids["A"], ids["B"]]

        cached = await engine.query(text=QUERY, top_k=2, use_cache=True)
        assert [r.id for r in cached] == [ids["A"], ids["B"]]
    finally:
        await engine.shutdown()


async def test_breakdown_identity(tmp_path):
    """direct_score / field_score / lensing_gap identities against an
    independent recomputation, plus expected_sum unchanged."""
    engine = await _make_engine(tmp_path)
    try:
        ids = await engine.index_documents([
            {"content": DOC_A, "metadata": {"source": "agent"}},
            {"content": DOC_B, "metadata": {"source": "agent"}},
        ])
        ida = ids[0]

        # passive → deterministic scoring on fresh state (temp 0, no
        # displacement) and no field perturbation.
        results = await engine.query(text=QUERY, top_k=2, passive=True)
        item = next(r for r in results if r.id == ida)
        bd = item.score_breakdown
        assert bd is not None

        # expected_sum contract unchanged by the new informational fields
        assert bd.expected_sum == pytest.approx(item.final_score, rel=1e-6, abs=1e-9)

        # independent recomputation of the normalized virtual cosine
        q_emb = engine.embedder.encode_query(QUERY)[0]
        raw_vec = engine.faiss_index.get_vectors([ida])[ida]
        disp = engine.cache.get_displacement(ida)
        state = engine.cache.get_node(ida)
        virtual = compute_virtual_position(raw_vec, disp, state.temperature)
        vcos_norm = float(np.dot(q_emb, virtual)) / (
            float(np.linalg.norm(q_emb)) * float(np.linalg.norm(virtual))
        )

        assert bd.direct_score == pytest.approx(
            vcos_norm * bd.decay_factor, abs=1e-6,
        )
        assert bd.field_score == pytest.approx(
            bd.wave_score + bd.mass_boost + bd.emotion_term + bd.certainty_term,
            abs=1e-9,
        )
        assert bd.lensing_gap == pytest.approx(vcos_norm - bd.raw_cosine, abs=1e-6)
        # fresh node: no displacement, no temperature → gap ≈ 0
        assert abs(bd.lensing_gap) < 1e-6
        assert bd.qualified is True
    finally:
        await engine.shutdown()
