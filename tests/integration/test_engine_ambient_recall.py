"""Ambient Recall Enrichment — ambient_recall() service (integration).

``services.memory.ambient_recall`` composes a structured multi-slot block out
of ONE passive recall: ① direct hits + ② gravitational-lensing pick +
③ provenance metadata, ④ derived_from/supersedes reasoning, ⑤ contradicts
tension, ⑥ persona grounding. Verified end to end through a real engine
(deterministic StubEmbedder).
"""
from __future__ import annotations

import hashlib

import numpy as np
import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.index.bm25_index import BM25Index
from gaottt.index.faiss_index import FaissIndex
from gaottt.services import memory as memory_service
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore
from tests.integration.test_engine_query_kick import StubEmbedder


class TokenEmbedder:
    """Token-bag deterministic embedder — cosine reflects token overlap.

    Each token hashes to a stable unit vector; the text vector is the
    L2-normalized sum of its tokens. Two strings with shared tokens land at
    a noticeably higher cosine than disjoint pairs, which the base-direction
    + tiny-perturbation StubEmbedder cannot demonstrate (it sits at uniform
    cosine ~0.97). Used by the Refinement Stage 1 persona-ranking tests
    where the point IS to distinguish a query-relevant persona from an
    equal-mass irrelevant one.
    """

    def __init__(self, dim: int = 64):
        self.dim = dim
        self._cache: dict[str, np.ndarray] = {}

    def _tok_vec(self, tok: str) -> np.ndarray:
        v = self._cache.get(tok)
        if v is not None:
            return v
        seed = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:8], "big")
        rng = np.random.default_rng(seed)
        x = rng.standard_normal(self.dim).astype(np.float32)
        x /= np.linalg.norm(x) + 1e-9
        self._cache[tok] = x
        return x

    def _embed(self, text: str) -> np.ndarray:
        toks = [t.lower() for t in text.split() if t.strip()]
        if not toks:
            return np.zeros(self.dim, dtype=np.float32)
        v = sum(self._tok_vec(t) for t in toks)
        n = float(np.linalg.norm(v))
        return (v / n).astype(np.float32) if n > 0 else v.astype(np.float32)

    def encode_documents(self, contents):
        return np.array([self._embed(c) for c in contents], dtype=np.float32)

    def encode_query(self, text):
        return self._embed(text).reshape(1, -1).astype(np.float32)

    def encode_queries(self, texts):
        return np.array([self._embed(t) for t in texts], dtype=np.float32)


def _make_engine(tmp_path, *, bm25: bool = False, embedder=None, **overrides):
    """Build a Stub-backed engine. ``bm25=True`` wires the word-BM25 ambient
    gate index and lowers ``ambient_bm25_min_score`` so the gate is
    exercisable on a tiny corpus (the 32.0 default is calibrated for a
    ~32k-doc corpus). The gate index here uses the default trigram tokenizer
    — the test drives the gate *wiring*, not the Sudachi tokenizer. With
    ``bm25=False`` there is no gate index, so ``ambient_recall`` falls back to
    the virtual_score gate — that path is what the other tests drive."""
    base_kwargs: dict = dict(
        embedding_dim=64,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "test.db"),
        faiss_index_path=str(tmp_path / "test.faiss"),
        genesis_kick_enabled=False,
        dream_enabled=False,
        faiss_save_interval_seconds=0.0,
        flush_interval_seconds=0.05,
        ambient_bm25_min_score=0.01,
    )
    base_kwargs.update(overrides)
    config = GaOTTTConfig(**base_kwargs)
    if embedder is None:
        embedder = StubEmbedder(dim=config.embedding_dim)
    return GaOTTTEngine(
        config=config,
        embedder=embedder,
        faiss_index=FaissIndex(dimension=config.embedding_dim),
        cache=CacheLayer(
            flush_interval=config.flush_interval_seconds,
            flush_threshold=config.flush_threshold,
        ),
        store=SqliteStore(db_path=config.db_path),
        ambient_gate_index=BM25Index() if bm25 else None,
    )


@pytest.mark.asyncio
async def test_ambient_recall_returns_structured_block_with_provenance(tmp_path):
    """① direct hits + ③ provenance metadata (source / certainty / age)."""
    engine = _make_engine(tmp_path)
    await engine.startup()
    try:
        for i in range(5):
            await memory_service.remember(
                engine, content=f"ambient probe doc {i}", source="agent",
            )
        resp = await memory_service.ambient_recall(
            engine, "ambient probe", direct_k=2,
        )
        assert resp.count >= 1
        assert 1 <= len(resp.direct) <= 2
        m = resp.direct[0]
        assert m.source == "agent"
        assert m.certainty is not None          # ③ from NodeState
        assert m.age_days is not None and m.age_days >= 0.0
        assert m.virtual_score > 0.0
        assert m.content                        # excerpt non-empty
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_recall_virtual_score_gate_fallback(tmp_path):
    """Fallback gate — with no BM25 index, ambient_recall gates on
    virtual_score; min_score above any achievable score → empty."""
    # explicit legacy ambient gate — OR default (WP-D) would let the raw
    # cosine axis (~0.97 with StubEmbedder) approve past min_score=0.999.
    engine = _make_engine(tmp_path, ambient_gate_or_semantic=False)
    await engine.startup()
    try:
        for i in range(3):
            await memory_service.remember(
                engine, content=f"gate doc {i}", source="agent",
            )
        resp = await memory_service.ambient_recall(
            engine, "gate doc", direct_k=2, min_score=0.999,
        )
        assert resp.count == 0
        assert resp.direct == []
        assert resp.lensing == []
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_recall_bm25_lexical_gate(tmp_path):
    """BM25 lexical gate — a prompt sharing terms with the corpus passes; a
    prompt with no lexical overlap (disjoint vocabulary → BM25 0) is
    suppressed even though dense recall would still return its 3 nearest."""
    # explicit legacy ambient gate — OR default (WP-D) would let the dense
    # axis (StubEmbedder uniform cosine ~0.97) surface the disjoint prompt.
    engine = _make_engine(tmp_path, bm25=True, ambient_gate_or_semantic=False)
    await engine.startup()
    try:
        for i in range(4):
            await memory_service.remember(
                engine,
                content=f"gravitational wave propagation seed pool {i}",
                source="agent",
            )
        on = await memory_service.ambient_recall(
            engine, "gravitational wave propagation", direct_k=2,
        )
        assert on.count >= 1, "lexically-overlapping prompt should inject"
        off = await memory_service.ambient_recall(
            engine, "りんごジュースの値段はいくらですか", direct_k=2,
        )
        assert off.count == 0, "disjoint-vocabulary prompt should be gated out"
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_recall_reasoning_chain(tmp_path):
    """Stage 2 ④ — a derived_from edge surfaces as `because` on the child."""
    engine = _make_engine(tmp_path)
    await engine.startup()
    try:
        ids = []
        for i in range(4):
            r = await memory_service.remember(
                engine, content=f"reasoning node {i}", source="agent",
            )
            ids.append(r.id)
        await engine.relate(
            src_id=ids[0], dst_id=ids[1], edge_type="derived_from",
        )
        resp = await memory_service.ambient_recall(
            engine, "reasoning node", direct_k=4,
        )
        because = next(
            (m.because for m in resp.direct if m.id == ids[0]), None,
        )
        assert because is not None, "derived_from parent should populate `because`"
        assert "reasoning node 1" in because
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_recall_tension_flag(tmp_path):
    """Stage 2 ⑤ — a contradicts edge surfaces as a tension caution."""
    engine = _make_engine(tmp_path)
    await engine.startup()
    try:
        ids = []
        for i in range(4):
            r = await memory_service.remember(
                engine, content=f"tension node {i}", source="agent",
            )
            ids.append(r.id)
        await engine.relate(
            src_id=ids[0], dst_id=ids[1], edge_type="contradicts",
        )
        resp = await memory_service.ambient_recall(
            engine, "tension node", direct_k=4,
        )
        assert resp.tensions, "contradicts edge should surface a tension"
        pair = {resp.tensions[0].memory_id, resp.tensions[0].contradicts_id}
        assert pair == {ids[0], ids[1]}
        # contradicts is bidirectional but the pair is de-duplicated
        assert len(resp.tensions) == 1
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_recall_persona_slot(tmp_path):
    """Stage 3 ⑥ — a declared value surfaces in the persona slot."""
    engine = _make_engine(tmp_path)
    await engine.startup()
    try:
        await memory_service.remember(
            engine, content="最も literal な解を選ぶ", source="value",
        )
        for i in range(3):
            await memory_service.remember(
                engine, content=f"persona probe {i}", source="agent",
            )
        resp = await memory_service.ambient_recall(
            engine, "persona probe", direct_k=2,
        )
        assert resp.persona is not None
        assert resp.persona.kind == "value"
        assert "literal" in resp.persona.content
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_recall_passive_does_not_perturb_field(tmp_path):
    """ambient_recall is passive end to end — mass is not moved (it reuses the
    passive recall path)."""
    engine = _make_engine(tmp_path)
    await engine.startup()
    try:
        for i in range(5):
            await memory_service.remember(
                engine, content=f"passive ambient {i}", source="agent",
            )
        ids = [
            r.id for r in (await engine.query(text="passive ambient", top_k=5))
        ]
        mass_before = {
            nid: float(engine.cache.get_node(nid).mass) for nid in ids
        }
        for _ in range(5):
            await memory_service.ambient_recall(engine, "passive ambient")
        mass_after = {
            nid: float(engine.cache.get_node(nid).mass) for nid in ids
        }
        assert mass_after == mass_before, "ambient_recall must not perturb mass"
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_persona_query_conditioned_pick(tmp_path):
    """Refinement Stage 1 — among multiple equal-mass value/intention nodes
    the one sharing tokens with the query is preferred over an irrelevant
    one. Reproduces the Phase A literal failure (an MCP-smoke ``intention``
    surfaced in an embedder-discussion turn) at fixture level: a
    ``smoke-test`` intention coexists with an on-topic value, and the
    persona slot must pick the on-topic one."""
    engine = _make_engine(
        tmp_path,
        embedder=TokenEmbedder(dim=64),
        # The TokenEmbedder gives query-relevant docs cosine ~0.5-0.9 and
        # disjoint docs ~0; lower the fallback virtual_score gate so the
        # recall pool isn't blocked just because the BM25 gate is absent.
        ambient_min_score=0.0,
    )
    await engine.startup()
    try:
        r_off = await memory_service.remember(
            engine, content="smoke test intention dummy artifact",
            source="intention",
        )
        r_on = await memory_service.remember(
            engine, content="embedder comparison careful methodology",
            source="value",
        )
        for i in range(3):
            await memory_service.remember(
                engine, content=f"persona padding doc {i}", source="agent",
            )
        resp = await memory_service.ambient_recall(
            engine, "embedder comparison methodology", direct_k=2,
        )
        assert resp.persona is not None
        assert resp.persona.id == r_on.id, (
            "query-shared-token persona should outrank the smoke intention"
        )
        assert resp.persona.id != r_off.id
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_persona_returns_none_below_relevance_floor(tmp_path):
    """Refinement Stage 1 — when no candidate clears
    ``ambient_persona_min_relevance`` the slot is silently omitted rather
    than surfacing an irrelevant persona. Driven by forcing the floor
    above any achievable cosine (1.5 > 1.0)."""
    engine = _make_engine(
        tmp_path,
        ambient_persona_min_relevance=1.5,  # impossible to clear
    )
    await engine.startup()
    try:
        await memory_service.remember(
            engine, content="some value statement", source="value",
        )
        for i in range(3):
            await memory_service.remember(
                engine, content=f"persona probe {i}", source="agent",
            )
        resp = await memory_service.ambient_recall(
            engine, "persona probe", direct_k=2,
        )
        assert resp.persona is None, (
            "cosine cannot exceed 1.0; min_relevance=1.5 must suppress the slot"
        )
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_recall_exclude_tags_drops_direct_and_persona(tmp_path):
    """Refinement Stage 2 — ``exclude_tags`` substring-filters the direct
    pool AND the persona candidate set. A ``smoke-test`` tagged memory is
    invisible to ambient_recall while still living in the corpus."""
    engine = _make_engine(tmp_path)
    await engine.startup()
    try:
        # A "smoke" persona that would otherwise dominate the persona slot
        # (StubEmbedder cosine is uniform ~0.97 so mass × cos ties → first
        # candidate wins; the tag must keep it out).
        r_smoke_value = await memory_service.remember(
            engine, content="smoke test ambient sentinel value",
            source="value", tags=["smoke-test"],
        )
        r_real_value = await memory_service.remember(
            engine, content="real grounding value", source="value",
        )
        # A "smoke" tagged agent doc that would otherwise be a direct hit.
        r_smoke_doc = await memory_service.remember(
            engine, content="smoke test agent doc payload",
            source="agent", tags=["smoke-test"],
        )
        for i in range(3):
            await memory_service.remember(
                engine, content=f"plain agent doc {i}", source="agent",
            )

        # Baseline (no exclude): the smoke doc CAN appear (Stub uniform
        # cosine), and a persona surfaces.
        baseline = await memory_service.ambient_recall(
            engine, "smoke test payload", direct_k=4,
        )
        assert baseline.persona is not None
        assert baseline.count >= 1

        # With exclude_tags: smoke entries are gone from every slot.
        filtered = await memory_service.ambient_recall(
            engine, "smoke test payload", direct_k=4,
            exclude_tags=["smoke-test"],
        )
        direct_ids = {m.id for m in filtered.direct}
        assert r_smoke_doc.id not in direct_ids, (
            "smoke-tagged agent doc must be excluded from direct hits"
        )
        for lens in filtered.lensing:
            assert lens.id != r_smoke_doc.id
        if filtered.persona is not None:
            assert filtered.persona.id != r_smoke_value.id, (
                "smoke-tagged value must not occupy the persona slot"
            )
            # Only the non-smoke value remains; it should be the pick.
            assert filtered.persona.id == r_real_value.id
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_recall_exclude_tags_none_is_no_op(tmp_path):
    """Refinement Stage 2 — ``exclude_tags=None`` and ``[]`` are no-ops
    (back-compat with all callers that don't pass the new arg)."""
    engine = _make_engine(tmp_path)
    await engine.startup()
    try:
        for i in range(4):
            await memory_service.remember(
                engine, content=f"noop ambient doc {i}", source="agent",
                tags=["smoke-test"] if i == 0 else None,
            )
        a = await memory_service.ambient_recall(
            engine, "noop ambient doc", direct_k=4,
        )
        b = await memory_service.ambient_recall(
            engine, "noop ambient doc", direct_k=4, exclude_tags=None,
        )
        c = await memory_service.ambient_recall(
            engine, "noop ambient doc", direct_k=4, exclude_tags=[],
        )
        assert a.count == b.count == c.count, (
            "None / [] / omitted must yield the same response shape"
        )
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_recall_expose_breakdown_default_off(tmp_path):
    """Refinement Stage 3 — without ``expose_breakdown`` every slot's
    ``breakdown`` is None (back-compat / token-budget safe)."""
    engine = _make_engine(tmp_path)
    await engine.startup()
    try:
        await memory_service.remember(
            engine, content="literal value statement", source="value",
        )
        for i in range(3):
            await memory_service.remember(
                engine, content=f"breakdown off probe {i}", source="agent",
            )
        resp = await memory_service.ambient_recall(
            engine, "breakdown off probe", direct_k=2,
        )
        for m in resp.direct:
            assert m.breakdown is None
        for lens in resp.lensing:
            assert lens.breakdown is None
        if resp.persona is not None:
            assert resp.persona.breakdown is None
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_recall_expose_breakdown_attaches(tmp_path):
    """Refinement Stage 3 — with ``expose_breakdown=True`` direct items
    carry a ScoreBreakdown (sourced from the recall path's Phase O Stage 1
    machinery), and the persona slot gets a minimal mass+raw breakdown."""
    engine = _make_engine(tmp_path)
    await engine.startup()
    try:
        await memory_service.remember(
            engine, content="literal grounding value", source="value",
        )
        for i in range(3):
            await memory_service.remember(
                engine, content=f"breakdown on probe {i}", source="agent",
            )
        resp = await memory_service.ambient_recall(
            engine, "breakdown on probe", direct_k=2,
            expose_breakdown=True,
        )
        assert resp.direct, "direct must be non-empty for this assert chain"
        for m in resp.direct:
            assert m.breakdown is not None, (
                "expose_breakdown=True must attach breakdown to direct"
            )
        if resp.persona is not None:
            assert resp.persona.breakdown is not None
            # Persona breakdown: only raw_cosine + mass_boost are populated.
            assert resp.persona.breakdown.raw_cosine != 0.0
            assert resp.persona.breakdown.mass_boost > 0.0
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_persona_pool_size_caps_candidates(tmp_path):
    """Refinement Stage 1 — ``ambient_persona_pool_size`` caps the cosine
    re-rank set to the top-N by mass. A persona node ranked outside the
    pool by mass cannot win the slot even when its cosine is higher."""
    engine = _make_engine(
        tmp_path,
        embedder=TokenEmbedder(dim=64),
        ambient_min_score=0.0,
        ambient_persona_pool_size=1,        # only the heaviest is considered
    )
    await engine.startup()
    try:
        # Heaviest is the off-topic one (we bump its mass directly below).
        r_off = await memory_service.remember(
            engine, content="unrelated heavy persona statement",
            source="value",
        )
        # _r_on exists only as a corpus distractor; it must NOT be picked.
        # The assertion ``resp.persona is None`` proves the pool cap kept it
        # out of the cosine re-rank entirely.
        _r_on = await memory_service.remember(
            engine, content="embedder comparison methodology focus",
            source="value",
        )
        # Direct mass mutation — simulating accumulated gravitational mass.
        engine.cache.get_node(r_off.id).mass = 10.0
        for i in range(3):
            await memory_service.remember(
                engine, content=f"pool cap padding doc {i}", source="agent",
            )
        resp = await memory_service.ambient_recall(
            engine, "embedder comparison methodology", direct_k=2,
        )
        # With pool_size=1, only r_off (heaviest) enters the cosine re-rank.
        # Its cosine to the query is ~0 → below the 0.5 floor → slot empty.
        # r_on never gets considered despite higher cosine.
        assert resp.persona is None, (
            "pool cap should exclude the on-topic persona; off-topic fails the floor"
        )
    finally:
        await engine.shutdown()


# --- Refinement follow-up (b) — ambient_persona_mass_weight ------------------
# Heavy Persona Dominance fix: ``score = (mass ** w) × cos`` where w =
# config.ambient_persona_mass_weight. Production observation 2026-05-25 saw a
# single intention with mass=2.82 capturing the persona slot across every
# query because Stage 1's bare ``mass × cos`` let the mass term dominate.
# These three tests pin down each knob regime on a calibrated fixture
# (TokenEmbedder, deterministic token-bag cosine):
#   - heavy "embedder" alone: cos ≈ 0.523, mass bumped to 10.0
#   - light "embedder comparison methodology" (= query exactly): cos = 1.0
#   Critical exponent w* = log(1.0/0.523)/log(10) ≈ 0.281 — below which the
#   cosine gap flips the winner.

@pytest.mark.asyncio
async def test_ambient_persona_mass_weight_default_preserves_heavy_winner(tmp_path):
    """Refinement follow-up (b) — with the production-promoted default
    ``ambient_persona_mass_weight=0.3`` a heavy off-topic persona still wins
    the slot over a lighter on-topic one: heavy_score = 10^0.3 × 0.523 ≈
    1.044 > light_score = 1^0.3 × 1.0 = 1.0 (a thin ~4.4% margin, but mass
    still overcomes the cosine gap at the default). Regression guard for the
    knob default — flipping it to a relevance-dominant value would change the
    winner, and that flip is covered by the w=0.0 / w=0.2 tests below."""
    engine = _make_engine(
        tmp_path,
        embedder=TokenEmbedder(dim=64),
        ambient_min_score=0.0,
        ambient_persona_min_relevance=0.3,   # both candidates must clear
        # ambient_persona_mass_weight defaults to 0.3
    )
    await engine.startup()
    try:
        r_heavy = await memory_service.remember(
            engine, content="embedder", source="intention",
        )
        r_light = await memory_service.remember(
            engine, content="embedder comparison methodology",
            source="value",
        )
        engine.cache.get_node(r_heavy.id).mass = 10.0
        for i in range(3):
            await memory_service.remember(
                engine, content=f"weight padding doc {i}", source="agent",
            )
        resp = await memory_service.ambient_recall(
            engine, "embedder comparison methodology", direct_k=2,
        )
        assert resp.persona is not None
        assert resp.persona.id == r_heavy.id, (
            "default weight=0.3 must still let heavy mass (10x) overcome the "
            "cosine gap — heavy_score=10^0.3×0.523≈1.044 > light_score=1.0 "
            "(~4.4% margin)"
        )
        assert resp.persona.id != r_light.id
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_persona_mass_weight_zero_yields_pure_cos_ranking(tmp_path):
    """Refinement follow-up (b) — ``ambient_persona_mass_weight=0.0`` is the
    ``relevance_dominant`` degenerate case: ``mass ** 0 == 1`` collapses the
    mass term to a constant and ranking falls back to pure cosine. The
    on-topic light persona must win despite a 10× mass deficit."""
    engine = _make_engine(
        tmp_path,
        embedder=TokenEmbedder(dim=64),
        ambient_min_score=0.0,
        ambient_persona_min_relevance=0.3,
        ambient_persona_mass_weight=0.0,
    )
    await engine.startup()
    try:
        r_heavy = await memory_service.remember(
            engine, content="embedder", source="intention",
        )
        r_light = await memory_service.remember(
            engine, content="embedder comparison methodology",
            source="value",
        )
        engine.cache.get_node(r_heavy.id).mass = 10.0
        for i in range(3):
            await memory_service.remember(
                engine, content=f"weight padding doc {i}", source="agent",
            )
        resp = await memory_service.ambient_recall(
            engine, "embedder comparison methodology", direct_k=2,
        )
        assert resp.persona is not None
        assert resp.persona.id == r_light.id, (
            "weight=0.0 must collapse mass; on-topic light persona wins on cos"
        )
        assert resp.persona.id != r_heavy.id
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_persona_mass_weight_intermediate_dampens_heavy(tmp_path):
    """Refinement follow-up (b) — an intermediate
    ``ambient_persona_mass_weight=0.2`` dampens a 10× mass gap to
    ``10**0.2 ≈ 1.585×``, which the calibrated cosine advantage now
    overcomes. The production-tuning sweet-spot regime: mass still matters
    a little but no longer monopolises the slot."""
    engine = _make_engine(
        tmp_path,
        embedder=TokenEmbedder(dim=64),
        ambient_min_score=0.0,
        ambient_persona_min_relevance=0.3,
        ambient_persona_mass_weight=0.2,
    )
    await engine.startup()
    try:
        r_heavy = await memory_service.remember(
            engine, content="embedder", source="intention",
        )
        r_light = await memory_service.remember(
            engine, content="embedder comparison methodology",
            source="value",
        )
        engine.cache.get_node(r_heavy.id).mass = 10.0
        for i in range(3):
            await memory_service.remember(
                engine, content=f"weight padding doc {i}", source="agent",
            )
        resp = await memory_service.ambient_recall(
            engine, "embedder comparison methodology", direct_k=2,
        )
        assert resp.persona is not None
        assert resp.persona.id == r_light.id, (
            "weight=0.2 must dampen the 10x mass enough that cos (1.0 vs "
            "~0.523) flips the winner — heavy_score=1.585*0.523≈0.830 < "
            "light_score=1.0*1.0=1.0"
        )
    finally:
        await engine.shutdown()


# --- Default promotion (2026-07-02, Heavy Persona Dominance mitigation) -------
# Production observation: even with env-set mass_weight=0.3, a single
# mass-inflated supernova cohort (harakiriworks intention) kept capturing the
# persona slot across every query because min_relevance=0.5 let the cos-baseline
# of dense Japanese embeddings (cos ≈ 0.52) through. The env value alone was
# insufficient; both knobs had to move to code defaults. See
# docs/wiki/Operations-Troubleshooting.md (Heavy Persona Dominance).

@pytest.mark.asyncio
async def test_ambient_persona_default_mass_weight_is_03(tmp_path):
    """Default ``ambient_persona_mass_weight`` is 0.3 (Heavy Persona Dominance
    mitigation, promoted from env opt-in to code default 2026-07-02 after
    production observation: harakiriworks-art-website intention with
    mass-inflated supernova cohort captured the persona slot across every
    query even with env-set w=0.3, because min_relevance=0.5 was too low
    to suppress the cos-baseline of dense Japanese embeddings).

    Pinning test: a freshly constructed engine without overrides should
    have ambient_persona_mass_weight == 0.3.
    """
    engine = _make_engine(tmp_path)
    try:
        assert engine.config.ambient_persona_mass_weight == 0.3
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_persona_default_min_relevance_065_omits_offtopic(tmp_path):
    """Default ``ambient_persona_min_relevance=0.65`` suppresses off-topic
    persona surfacing. With a persona whose cosine to the query is below
    0.65 (TokenEmbedder produces cos ≈ 0.523 for "embedder" vs
    "embedder comparison methodology"), the slot is silently omitted.

    This test is RED before the default change (0.5 lets cos=0.523 through)
    and GREEN after (0.65 drops it)."""
    engine = _make_engine(
        tmp_path,
        embedder=TokenEmbedder(dim=64),
        ambient_min_score=0.0,
        # ambient_persona_min_relevance defaults to 0.65 (post-change)
        # ambient_persona_mass_weight defaults to 0.3 (post-change)
    )
    await engine.startup()
    try:
        await memory_service.remember(
            engine, content="embedder", source="intention",
        )
        # No on-topic persona — "embedder comparison methodology" has no
        # matching value/intention. The only candidate is "embedder" intention
        # with cos ≈ 0.523 < 0.65 → must be omitted.
        resp = await memory_service.ambient_recall(
            engine, "embedder comparison methodology", direct_k=2,
        )
        assert resp.persona is None, (
            "default min_relevance=0.65 must drop off-topic persona with "
            "cos ≈ 0.523 (TokenEmbedder 'embedder' vs 'embedder comparison methodology')"
        )
    finally:
        await engine.shutdown()


# --- Lateral Association Stage 1 — session-aware novelty decay ---------------
# ``services.memory.ambient_recall(recently_surfaced=...)`` multiplies each
# slot's ranking score by ``ambient_novelty_decay ** count`` for matching ids.
# These tests pin the three slot interactions (direct / lensing / persona)
# independently so a regression in any one is visible.

@pytest.mark.asyncio
async def test_ambient_novelty_decay_rotates_direct_slot(tmp_path):
    """A node listed in ``recently_surfaced`` must drop out of the direct slot
    when its margin over the next candidate is smaller than the decay factor.

    Fixture: two ambient calls back-to-back. Call 1 has no history → top-1
    direct surfaces normally. Call 2 receives ``{top1_id: 1}`` → the decay
    drops top1's score below #2's; the surfaces should swap."""
    engine = _make_engine(
        tmp_path,
        embedder=TokenEmbedder(dim=64),
        ambient_min_score=0.0,
        ambient_novelty_decay=0.5,  # aggressive for a tight fixture
    )
    await engine.startup()
    try:
        await memory_service.remember(
            engine, content="ambient probe alpha beta", source="agent",
        )
        await memory_service.remember(
            engine, content="ambient probe alpha", source="agent",
        )
        await memory_service.remember(
            engine, content="ambient probe gamma", source="agent",
        )

        first = await memory_service.ambient_recall(
            engine, "ambient probe alpha", direct_k=1,
        )
        assert first.direct, "fixture: first call must surface at least 1 direct"
        top1 = first.direct[0].id

        # Now decay top1 — second call should rotate to a different id.
        second = await memory_service.ambient_recall(
            engine, "ambient probe alpha", direct_k=1,
            recently_surfaced={top1: 1},
        )
        assert second.direct, "novelty decay should not drop the slot entirely"
        assert second.direct[0].id != top1, (
            f"recently_surfaced did not rotate the direct slot: "
            f"first={top1[:8]}… second={second.direct[0].id[:8]}… "
            f"(decay 0.5 should overcome the gap)"
        )
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_novelty_decay_no_op_when_unset(tmp_path):
    """``recently_surfaced=None`` / empty dict must produce byte-identical
    results to two consecutive calls without it — the no-op path."""
    engine = _make_engine(
        tmp_path,
        embedder=TokenEmbedder(dim=64),
        ambient_min_score=0.0,
        ambient_novelty_decay=0.3,  # aggressive — would visibly change ranking
    )
    await engine.startup()
    try:
        for content in (
            "ambient probe alpha beta gamma",
            "ambient probe alpha beta",
            "ambient probe gamma",
        ):
            await memory_service.remember(
                engine, content=content, source="agent",
            )
        baseline = await memory_service.ambient_recall(
            engine, "ambient probe", direct_k=2,
        )
        with_none = await memory_service.ambient_recall(
            engine, "ambient probe", direct_k=2, recently_surfaced=None,
        )
        with_empty = await memory_service.ambient_recall(
            engine, "ambient probe", direct_k=2, recently_surfaced={},
        )
        assert [m.id for m in baseline.direct] == [m.id for m in with_none.direct]
        assert [m.id for m in baseline.direct] == [m.id for m in with_empty.direct]
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_novelty_decay_rolled_back_at_unity(tmp_path):
    """``ambient_novelty_decay=1.0`` is the rollback path: even with a
    populated ``recently_surfaced``, no decay should apply (1.0 ** any = 1)."""
    engine = _make_engine(
        tmp_path,
        embedder=TokenEmbedder(dim=64),
        ambient_min_score=0.0,
        ambient_novelty_decay=1.0,  # explicit no-op
    )
    await engine.startup()
    try:
        for content in (
            "ambient probe alpha beta",
            "ambient probe alpha",
            "ambient probe gamma",
        ):
            await memory_service.remember(
                engine, content=content, source="agent",
            )
        first = await memory_service.ambient_recall(
            engine, "ambient probe alpha", direct_k=1,
        )
        assert first.direct
        top1 = first.direct[0].id
        # Even with extreme history weight, ranking is unchanged at decay=1.0.
        second = await memory_service.ambient_recall(
            engine, "ambient probe alpha", direct_k=1,
            recently_surfaced={top1: 99},
        )
        assert second.direct
        assert second.direct[0].id == top1, (
            "decay=1.0 must be a literal no-op even with high recency counts"
        )
    finally:
        await engine.shutdown()


# --- Lateral Association Stage 3 — lensing top-K ----------------------------
# Stage 3 lifts ``ambient_lensing_max_k`` from 1 to a configurable cap so
# multiple field-bent associations fire in one turn — the natural "X といえば
# Y で、Y といえば Z" chain that top-1-only suppressed.

@pytest.mark.asyncio
async def test_ambient_lensing_top_k_returns_multiple_picks(tmp_path):
    """``ambient_lensing_max_k=3`` returns up to 3 lensing picks ranked by
    gap descending. Each kept pick still independently clears
    ``ambient_lensing_min_score`` + ``ambient_lensing_min_gap``."""
    engine = _make_engine(
        tmp_path,
        embedder=TokenEmbedder(dim=64),
        ambient_min_score=0.0,
        # Loosen the gates so the small fixture can host multiple picks;
        # the test exercises the K-cap logic, not the noise thresholds.
        ambient_lensing_min_score=-1.0,
        ambient_lensing_min_gap=-1.0,
        ambient_lensing_max_k=3,
        # Force breakdown so _pick_lensing can read virtual − raw.
        expose_score_breakdown=True,
    )
    await engine.startup()
    try:
        # Direct hit (excluded from lensing).
        await memory_service.remember(
            engine, content="lensing probe alpha", source="agent",
        )
        # Several distant docs that will become lensing candidates after the
        # query → top-K filtering picks at most 3.
        for tok in ("beta", "gamma", "delta", "epsilon", "zeta"):
            await memory_service.remember(
                engine, content=f"lensing probe {tok}", source="agent",
            )
        resp = await memory_service.ambient_recall(
            engine, "lensing probe alpha", direct_k=1, expose_breakdown=True,
        )
        assert 1 <= len(resp.lensing) <= 3, (
            f"Stage 3 cap: got {len(resp.lensing)} lensing picks, expected 1-3"
        )
        # No lensing pick should overlap a direct pick (exclude set).
        direct_ids = {m.id for m in resp.direct}
        for lens in resp.lensing:
            assert lens.id not in direct_ids, (
                "lensing pick overlapped direct slot — exclude set broken"
            )
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_lensing_max_k_one_is_legacy_behavior(tmp_path):
    """``ambient_lensing_max_k=1`` reproduces Stage 1/2 exactly (at most one
    lensing pick). Regression guard so a future default bump doesn't break
    rollback."""
    engine = _make_engine(
        tmp_path,
        embedder=TokenEmbedder(dim=64),
        ambient_min_score=0.0,
        ambient_lensing_min_score=-1.0,
        ambient_lensing_min_gap=-1.0,
        ambient_lensing_max_k=1,
        expose_score_breakdown=True,
    )
    await engine.startup()
    try:
        for tok in ("alpha", "beta", "gamma", "delta"):
            await memory_service.remember(
                engine, content=f"lensing k1 probe {tok}", source="agent",
            )
        resp = await memory_service.ambient_recall(
            engine, "lensing k1 probe alpha", direct_k=1,
            expose_breakdown=True,
        )
        assert len(resp.lensing) <= 1, (
            f"max_k=1 must yield at most 1 pick (got {len(resp.lensing)})"
        )
    finally:
        await engine.shutdown()


# --- Lateral Association Stage 5 — lensing resonance signal -----------------

@pytest.mark.asyncio
async def test_ambient_lensing_resonance_populated_for_each_pick(tmp_path):
    """Every lensing pick gets ``lensing_resonance`` populated (None means
    "not computed" / non-lensing slot). With no cooccurrence history the
    value is 0.0 — verifies the signal is *always* present so the agent can
    distinguish "no co-recall history" from "Stage 5 disabled"."""
    engine = _make_engine(
        tmp_path,
        embedder=TokenEmbedder(dim=64),
        ambient_min_score=0.0,
        ambient_lensing_min_score=-1.0,
        ambient_lensing_min_gap=-1.0,
        ambient_lensing_max_k=2,
        expose_score_breakdown=True,
    )
    await engine.startup()
    try:
        for tok in ("alpha", "beta", "gamma", "delta"):
            await memory_service.remember(
                engine, content=f"resonance probe {tok}", source="agent",
            )
        resp = await memory_service.ambient_recall(
            engine, "resonance probe alpha", direct_k=1,
            expose_breakdown=True,
        )
        assert resp.lensing, "fixture: at least one lensing pick expected"
        for lens in resp.lensing:
            assert lens.lensing_resonance is not None, (
                f"lensing pick {lens.id[:8]}… missing resonance — "
                "Stage 5 should always populate"
            )
            assert 0.0 <= lens.lensing_resonance < 1.0, (
                f"resonance must be in [0,1): {lens.lensing_resonance}"
            )
        # Direct slot never carries resonance (lensing-only concept).
        for d in resp.direct:
            assert d.lensing_resonance is None
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_lensing_resonance_reflects_cooccurrence(tmp_path):
    """When the cooccurrence graph has an edge between the lensing pick and
    a direct id, resonance must be > 0. Manually seed an edge and confirm.
    """
    engine = _make_engine(
        tmp_path,
        embedder=TokenEmbedder(dim=64),
        ambient_min_score=0.0,
        ambient_lensing_min_score=-1.0,
        ambient_lensing_min_gap=-1.0,
        ambient_lensing_max_k=2,
        expose_score_breakdown=True,
        ambient_lensing_resonance_scale=5.0,
    )
    await engine.startup()
    try:
        r_alpha = await memory_service.remember(
            engine, content="resonance edge probe alpha", source="agent",
        )
        r_beta = await memory_service.remember(
            engine, content="resonance edge probe beta", source="agent",
        )
        for tok in ("gamma", "delta"):
            await memory_service.remember(
                engine, content=f"resonance edge probe {tok}", source="agent",
            )
        # Manually seed a cooccurrence edge (weight 5) between alpha and beta.
        # In production this would happen organically through past active
        # recalls; here we plant the literal signal to verify resonance reads
        # it correctly.
        engine.cache.set_edge(r_alpha.id, r_beta.id, weight=5.0)

        resp = await memory_service.ambient_recall(
            engine, "resonance edge probe alpha", direct_k=1,
            expose_breakdown=True,
        )
        # If alpha is the direct and beta lands in lensing (or vice versa),
        # the cooccurrence pair contributes 5.0 → resonance = 5/(5+5) = 0.5.
        # If neither alpha nor beta is picked we cannot assert; widen direct_k.
        direct_ids = {m.id for m in resp.direct}
        lensing_with_edge = [
            lens for lens in resp.lensing
            if (lens.id == r_beta.id and r_alpha.id in direct_ids)
            or (lens.id == r_alpha.id and r_beta.id in direct_ids)
        ]
        if not lensing_with_edge:
            pytest.skip("fixture: the seeded pair did not split into direct + lensing")
        edge_pick = lensing_with_edge[0]
        assert edge_pick.lensing_resonance is not None
        assert edge_pick.lensing_resonance > 0.45, (
            f"seeded weight=5, scale=5 → expected ~0.5 resonance, "
            f"got {edge_pick.lensing_resonance}"
        )
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_lensing_resonance_min_drops_low_resonance(tmp_path):
    """``ambient_lensing_resonance_min`` > 0 drops lensing picks whose
    resonance is below threshold. No backfill (Stage 3 + Stage 5 share the
    no-quota-relaxation principle)."""
    engine = _make_engine(
        tmp_path,
        embedder=TokenEmbedder(dim=64),
        ambient_min_score=0.0,
        ambient_lensing_min_score=-1.0,
        ambient_lensing_min_gap=-1.0,
        ambient_lensing_max_k=3,
        expose_score_breakdown=True,
        # With no cooccurrence edges the resonance is always 0.0; setting
        # min above 0 must drop *everything*.
        ambient_lensing_resonance_min=0.01,
    )
    await engine.startup()
    try:
        for tok in ("alpha", "beta", "gamma", "delta", "epsilon"):
            await memory_service.remember(
                engine, content=f"drop probe {tok}", source="agent",
            )
        resp = await memory_service.ambient_recall(
            engine, "drop probe alpha", direct_k=1,
        )
        assert resp.lensing == [], (
            "all picks should be dropped (no cooccurrence → resonance=0 "
            "→ below min=0.01)"
        )
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_ambient_novelty_decay_rotates_persona_slot(tmp_path):
    """A heavy persona that would normally win ``mass × cos`` rotates out
    when listed in ``recently_surfaced`` — the lateral-novelty channel for
    Heavy Persona Dominance."""
    engine = _make_engine(
        tmp_path,
        embedder=TokenEmbedder(dim=64),
        ambient_min_score=0.0,
        ambient_persona_min_relevance=0.3,
        ambient_novelty_decay=0.3,  # strong enough to overcome 2x mass
    )
    await engine.startup()
    try:
        r_heavy = await memory_service.remember(
            engine, content="ambient probe alpha", source="intention",
        )
        r_other = await memory_service.remember(
            engine, content="ambient probe alpha beta",
            source="value",
        )
        engine.cache.get_node(r_heavy.id).mass = 2.0
        for i in range(3):
            await memory_service.remember(
                engine, content=f"padding {i}", source="agent",
            )
        # Without history, heavy wins.
        baseline = await memory_service.ambient_recall(
            engine, "ambient probe alpha", direct_k=1,
        )
        assert baseline.persona is not None
        assert baseline.persona.id == r_heavy.id, (
            "fixture sanity: heavy mass should win the slot without history"
        )

        # With heavy listed as recently-surfaced, the other persona takes the
        # slot.
        rotated = await memory_service.ambient_recall(
            engine, "ambient probe alpha", direct_k=1,
            recently_surfaced={r_heavy.id: 1},
        )
        assert rotated.persona is not None
        assert rotated.persona.id == r_other.id, (
            f"novelty decay did not rotate persona: "
            f"baseline={baseline.persona.id[:8]}… "
            f"rotated={rotated.persona.id[:8]}…"
        )
    finally:
        await engine.shutdown()
