"""Phase T Stage 6 — explore diversified presentation (integration, engine.query).

Deterministic StubEmbedder (md5-seeded token vectors, dim=512 to keep
unrelated-token cross-noise small). Measured pure raw cosines for the
calibration query QUERY (locked from the fixture, see module constants):

    Y2 0.8093  Y1 0.7856  X2 0.7628  X1 0.7568  X3 0.7557
    Z1 0.6969  W1 0.5825  U2 0.5034  V2 0.4943  V1 0.4896  U1 0.4730

X1..X3 share original_id "book-x" (a 3-chunk cluster), Y1/Y2 share
"book-y"; the rest are singletons (their own id). With
``wave_boost_weight=0`` the final_score ordering of fresh nodes is the
raw-cosine ordering (uniform mass/decay/certainty offsets only), so the
MMR selection is exercised against fully deterministic inputs.

Locked outcomes (engine reference == spec MMR):
  plain   top-5: [Y2, Y1, X2, X1, X3]   (book-x run of 3)
  d=0.8   top-5: [Y2, X2, Y1, Z1, X1]   (book-x run of 1, set changes)
  d=1.0   top-5: [Y2, X2, Y1, Z1, W1]
"""
from __future__ import annotations

import hashlib
import time

import numpy as np
import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.diversity import cluster_key_from_cache
from gaottt.core.engine import GaOTTTEngine
from gaottt.index.bm25_index import BM25Index
from gaottt.index.faiss_index import FaissIndex
from gaottt.services import memory as memory_service
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore


class StubEmbedder:
    """Deterministic embedder: keyword-overlap controls similarity.

    Each unique whitespace-separated token gets a stable unit basis
    vector (seeded by md5 of the token, consistent across processes and
    across copies of this class). A text's embedding is the
    L2-normalized sum of its token vectors.
    """

    def __init__(self, dimension: int = 512):
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


QUERY = "alpha beta gamma delta epsilon"
QUERIES = [
    "alpha beta gamma delta epsilon",
    "alpha beta gamma delta",
    "gamma delta epsilon",
    "alpha gamma epsilon",
    "beta delta gamma",
]

# (key, content, original_id, tags)
CORPUS = [
    ("X1", "alpha beta gamma delta epsilon ax bx cx dx", "book-x", []),
    ("X2", "alpha beta gamma delta epsilon ax bx cx ex", "book-x", []),
    ("X3", "alpha beta gamma delta epsilon ax bx cx fx", "book-x", []),
    ("Y1", "alpha beta gamma delta y1", "book-y", []),
    ("Y2", "alpha beta gamma delta y2", "book-y", []),
    ("Z1", "alpha beta gamma z1", None, []),
    ("W1", "alpha epsilon w1", None, []),
    ("V1", "alpha beta v1", None, []),
    ("V2", "gamma delta v2", None, []),
    ("U1", "beta epsilon u1", None, []),
    ("U2", "alpha delta u2", None, []),
]

PLAIN_TOP5 = ["Y2", "Y1", "X2", "X1", "X3"]
D08_TOP5 = ["Y2", "X2", "Y1", "Z1", "X1"]
D10_TOP5 = ["Y2", "X2", "Y1", "Z1", "W1"]


def _make_config(tmp_path, **overrides) -> GaOTTTConfig:
    defaults = dict(
        embedding_dim=512,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "gaottt.db"),
        faiss_index_path=str(tmp_path / "gaottt.faiss"),
        flush_interval_seconds=999.0,   # disable background flush in tests
        faiss_save_interval_seconds=0.0,
        dream_enabled=False,
        genesis_kick_enabled=False,
        wave_initial_k=16,              # whole fixture corpus reaches the wave
        wave_max_depth=1,
        # Determinism: zero wave boost so fresh-node final_score ordering is
        # exactly the raw-cosine ordering (uniform mass/decay/certainty
        # offsets cancel in the min-max relevance normalization).
        wave_boost_weight=0.0,
        # Phase T Stage 6 under test
        explore_diversified_presentation_enabled=True,
    )
    defaults.update(overrides)
    return GaOTTTConfig(**defaults)


async def _make_engine(tmp_path, **config_overrides) -> GaOTTTEngine:
    cfg = _make_config(tmp_path, **config_overrides)
    eng = GaOTTTEngine(
        config=cfg,
        embedder=StubEmbedder(dimension=512),
        faiss_index=FaissIndex(dimension=512),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=cfg.db_path),
        bm25_index=BM25Index(k1=cfg.bm25_k1, b=cfg.bm25_b, tokenizer=cfg.bm25_tokenizer),
    )
    await eng.startup()
    return eng


async def _index_corpus(
    engine: GaOTTTEngine,
    corpus: list[tuple[str, str, str | None, list[str]]] = CORPUS,
) -> dict[str, str]:
    """Index one document per call (batch=1: no supernova cohort, no
    supernova edges — cluster identity comes from explicit original_id
    metadata only, keeping co-occurrence observations test-clean)."""
    key_of: dict[str, str] = {}
    for key, content, orig, tags in corpus:
        meta: dict = {"source": "agent"}
        if orig:
            meta["original_id"] = orig
        if tags:
            meta["tags"] = tags
        ids = await engine.index_documents(
            [{"content": content, "metadata": meta}],
        )
        key_of[key] = ids[0]
    return key_of


def _keys(engine: GaOTTTEngine, results, key_of: dict[str, str]) -> list[str]:
    id_of = {nid: k for k, nid in key_of.items()}
    return [id_of[r.id] for r in results]


def _longest_cluster_run(keys: list[str], key_of: dict[str, str]) -> int:
    """Longest run of consecutive results sharing an original_id cluster."""
    cluster_of = {k: c for k, _n, c, _t in CORPUS if c}
    best = run = 0
    prev = None
    for k in keys:
        cur = cluster_of.get(k)
        if cur is not None and cur == prev:
            run += 1
        else:
            run = 1 if cur is not None else 0
        prev = cur
        best = max(best, run)
    return best


# --- 1. diversity=0.0 / flag OFF bypass: strict legacy identity ------------


async def test_diversity_zero_and_flag_off_bypass_bit_for_bit(tmp_path):
    """flag ON + diversity=0.0 must be the identical legacy path as
    diversity=None; flag OFF + diversity=0.8 bypasses too. All four
    calls return the same id sequence."""
    sub_on = tmp_path / "flag-on"
    sub_on.mkdir()
    sub_off = tmp_path / "flag-off"
    sub_off.mkdir()

    eng_on = await _make_engine(sub_on)
    eng_off = await _make_engine(
        sub_off, explore_diversified_presentation_enabled=False,
    )
    try:
        keys_on = await _index_corpus(eng_on)
        keys_off = await _index_corpus(eng_off)

        plain_on = await eng_on.query(text=QUERY, top_k=5, passive=True)
        zero_on = await eng_on.query(text=QUERY, top_k=5, diversity=0.0, passive=True)
        # same engine, both bypass routes → literally identical id lists
        assert [r.id for r in plain_on] == [r.id for r in zero_on]
        assert _keys(eng_on, plain_on, keys_on) == PLAIN_TOP5

        plain_off = await eng_off.query(text=QUERY, top_k=5, passive=True)
        diverse_off = await eng_off.query(
            text=QUERY, top_k=5, diversity=0.8, passive=True,
        )
        assert [r.id for r in plain_off] == [r.id for r in diverse_off]
        # flag ON (diversity=None) and flag OFF agree on the legacy order
        assert _keys(eng_off, plain_off, keys_off) == PLAIN_TOP5
    finally:
        await eng_on.shutdown()
        await eng_off.shutdown()


# --- 2. diversity>0 changes presentation and suppresses cohort runs --------


async def test_diversity_changes_order_and_suppresses_cohort_runs(tmp_path):
    engine = await _make_engine(tmp_path)
    try:
        key_of = await _index_corpus(engine)

        plain = await engine.query(text=QUERY, top_k=5, passive=True)
        d08 = await engine.query(text=QUERY, top_k=5, diversity=0.8, passive=True)
        d10 = await engine.query(text=QUERY, top_k=5, diversity=1.0, passive=True)

        plain_keys = _keys(engine, plain, key_of)
        d08_keys = _keys(engine, d08, key_of)
        d10_keys = _keys(engine, d10, key_of)

        assert plain_keys == PLAIN_TOP5
        assert d08_keys == D08_TOP5
        assert d10_keys == D10_TOP5

        # presentation is no longer a plain-recall clone
        assert d08_keys != plain_keys
        # cohort duplicate suppression: the book-x run collapses 3 → 1
        assert _longest_cluster_run(plain_keys, key_of) == 3
        assert _longest_cluster_run(d08_keys, key_of) == 1
        # and at diversity=1.0 the cluster loses slots outright (3 → 1:
        # only X2 survives — W1 takes the fifth slot)
        assert sum(1 for k in d10_keys if k.startswith("X")) == 1
        # result count is still the requested top_k
        assert len(d08) == 5 and len(d10) == 5
    finally:
        await engine.shutdown()


# --- 3. relevance floor -----------------------------------------------------


async def test_relevance_floor_excludes_low_semantic_candidates(tmp_path):
    # Phase U WP-1: promoted direct qualification reorders the plain path
    # qualified-first, which demotes the mass=4000 OFF node before the
    # "mass dominates the plain path" premise can be observed. This test
    # isolates the Stage 6 relevance floor, so pin Stage 3 OFF here
    # (mechanism isolation; assertion unchanged).
    engine = await _make_engine(
        tmp_path, alpha=0.5, direct_qualification_enabled=False,
    )
    try:
        corpus = CORPUS + [("OFF", "zzz qqq www vvv", None, [])]
        key_of = await _index_corpus(engine, corpus)
        off_id = key_of["OFF"]

        # massive mass makes the semantically-unrelated node the plain
        # top-1 (the classic field-dominance pathology)…
        state = engine.cache.get_node(off_id)
        assert state is not None
        state.mass = 4000.0
        engine.cache.set_node(state, dirty=True)

        # fixture honesty: the node is genuinely below the floor
        q_vec = engine.embedder.encode_query(QUERY)[0]
        raw_vec = engine.faiss_index.get_vectors([off_id])[off_id]
        raw_cos = float(np.dot(q_vec, raw_vec)) / (
            float(np.linalg.norm(q_vec)) * float(np.linalg.norm(raw_vec))
        )
        assert raw_cos < engine.config.explore_min_semantic

        plain = await engine.query(text=QUERY, top_k=5, passive=True)
        plain_keys = _keys(engine, plain, key_of)
        assert plain_keys[0] == "OFF"  # mass dominates the plain path

        for d in (0.5, 0.8, 1.0):
            diverse = await engine.query(
                text=QUERY, top_k=5, diversity=d, passive=True,
            )
            diverse_keys = _keys(engine, diverse, key_of)
            assert "OFF" not in diverse_keys
            # every presented item cleared the floor
            for r in diverse:
                v = engine.faiss_index.get_vectors([r.id])[r.id]
                cos = float(np.dot(q_vec, v)) / (
                    float(np.linalg.norm(q_vec)) * float(np.linalg.norm(v))
                )
                assert cos >= engine.config.explore_min_semantic
    finally:
        await engine.shutdown()


# --- 4. forced injection is exempt from MMR --------------------------------


async def test_forced_items_keep_legacy_order_under_diversity(tmp_path):
    """tag_filter forces Y1+Z1 into the presentation. Forced items keep
    the legacy raw-cosine order and are never MMR-reordered; the
    remaining slots are MMR-selected naturals."""
    corpus = [
        (k, c, o, (["forced-set"] if k in ("Y1", "Z1") else t))
        for k, c, o, t in CORPUS
    ]
    engine = await _make_engine(tmp_path)
    try:
        key_of = await _index_corpus(engine, corpus)

        results = await engine.query(
            text=QUERY, top_k=5, tag_filter=["forced-set"], diversity=1.0,
            passive=True,
        )
        keys = _keys(engine, results, key_of)
        # forced set comes first, in the legacy raw-cosine order
        # (Y1 0.7856 > Z1 0.6969)
        assert keys[:2] == ["Y1", "Z1"]
        # naturals fill the remaining slots via MMR (book-x capped at 2)
        naturals = keys[2:]
        assert len(naturals) == 3
        assert sum(1 for k in naturals if k.startswith("X")) <= 2
        assert set(keys) == {"Y1", "Z1"} | set(naturals)
    finally:
        await engine.shutdown()


# --- 5. presentation-derived updates touch presented ids only ---------------


async def test_presented_only_updates_pool_members_untouched(tmp_path):
    """Active diversity query: return_count / co-occurrence only move for
    the MMR-presented ids; last_access still updates for every reached
    node (Stage 4 all-reached maintenance contract).

    Phase U WP-1 rework — under the promoted defaults (ttt qualification
    ON, unpinned) co-occurrence is gated to ``presented ∩ learn set``.
    The fixture corpus's shared-token docs (X/Y share 4-5 of QUERY's 5
    tokens → raw cosine 0.75-0.81 ≥ direct_raw_cosine_min) keep the learn
    set non-empty, while the lateral Z1 ("alpha beta gamma z1", raw cos
    0.697 < 0.75 and below every other axis) stays unqualified — so the
    promoted combination is exercised on both sides of the intersection:
    Z1 is *presented* (return_count records the presentation fact, MMR
    lateral slot) yet trains nothing (no co-occurrence edges), while the
    qualified presented docs co-present pairwise.
    """
    engine = await _make_engine(tmp_path, edge_threshold=1)
    try:
        key_of = await _index_corpus(engine)

        # uniform saturation state so ordering stays the calibrated one
        old_access = time.time() - 3600.0
        for nid in key_of.values():
            state = engine.cache.get_node(nid)
            assert state is not None
            state.return_count = 10.0
            state.last_access = old_access
            engine.cache.set_node(state, dirty=True)

        results = await engine.query(text=QUERY, top_k=5, diversity=0.8)
        presented_keys = _keys(engine, results, key_of)
        assert presented_keys == D08_TOP5

        presented_ids = {key_of[k] for k in presented_keys}
        all_ids = set(key_of.values())
        unselected = all_ids - presented_ids
        assert len(unselected) == 6  # pool beyond the presented cut

        # fixture honesty (promoted contract): the verdicts the assertions
        # below split on are the engine's own qualification verdicts.
        verdict = {
            r.id: r.score_breakdown.qualified
            for r in results
            if r.score_breakdown is not None
        }
        assert verdict[key_of["Z1"]] is False, (
            "fixture premise: Z1 must be the presented-but-unqualified lateral"
        )
        qualified_presented = {
            key_of[k] for k in presented_keys if k != "Z1"
        }
        assert all(verdict[nid] is True for nid in qualified_presented), (
            "fixture premise: X/Y docs must clear the 0.75 raw-cosine axis"
        )

        recovery = 1.0 - engine.config.habituation_recovery_rate
        for nid in presented_ids:
            # 10 → +1 (presented) → ×recovery — ALL presented nodes record
            # the presentation fact, qualified or not.
            assert engine.cache.get_node(nid).return_count == pytest.approx(
                11.0 * recovery,
            )
        for nid in unselected:
            state = engine.cache.get_node(nid)
            # no presentation bump: habituation recovery only (all-reached)
            assert state.return_count == pytest.approx(10.0 * recovery)
            # maintenance contract: last_access refreshed for all reached
            assert state.last_access > old_access

        # co-occurrence edges exist only within presented ∩ learn set
        for nid in unselected:
            assert engine.cache.get_neighbors(nid) == {}
        for nid in qualified_presented:
            neighbors = set(engine.cache.get_neighbors(nid))
            assert neighbors  # the qualified presented set is pairwise-co-presented
            assert neighbors <= presented_ids
        # the presented-but-unqualified lateral trains nothing: the ∩ of
        # the presentation with the learn set excludes it.
        assert engine.cache.get_neighbors(key_of["Z1"]) == {}
    finally:
        await engine.shutdown()


# --- 6. aggregate Jaccard trend ---------------------------------------------


async def test_jaccard_trend_median_non_increasing(tmp_path):
    engine = await _make_engine(tmp_path)
    try:
        key_of = await _index_corpus(engine)

        def jaccard(a: list[str], b: list[str]) -> float:
            sa, sb = set(a), set(b)
            if not sa and not sb:
                return 1.0
            return len(sa & sb) / len(sa | sb)

        def median(xs: list[float]) -> float:
            xs = sorted(xs)
            n = len(xs)
            return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2

        medians: dict[float, float] = {}
        order_changes: dict[float, int] = {}
        for d in (0.0, 0.5, 0.8, 1.0):
            jaccards = []
            changes = 0
            for q in QUERIES:
                plain = await engine.query(text=q, top_k=5, passive=True)
                diverse = await engine.query(
                    text=q, top_k=5, diversity=d, passive=True,
                )
                plain_keys = _keys(engine, plain, key_of)
                diverse_keys = _keys(engine, diverse, key_of)
                jaccards.append(jaccard(plain_keys, diverse_keys))
                if diverse_keys != plain_keys:
                    changes += 1
            medians[d] = median(jaccards)
            order_changes[d] = changes

        # diversity=0.0 is the strict-identity bypass
        assert medians[0.0] == 1.0
        # non-increasing across the sweep (equality allowed — per-query
        # monotonicity is NOT required by the plan)
        assert medians[0.5] <= medians[0.0]
        assert medians[0.8] <= medians[0.5]
        assert medians[1.0] <= medians[0.8]
        assert medians[1.0] < 1.0
        # the change actually manifests for most queries at 0.8 (the
        # test-sized analogue of the acceptance "8割変化")
        assert order_changes[0.8] >= 3
    finally:
        await engine.shutdown()


# --- 7. passive explore leaves the field untouched ---------------------------


async def test_explore_passive_leaves_field_unchanged(tmp_path):
    engine = await _make_engine(tmp_path)
    try:
        key_of = await _index_corpus(engine)

        def snapshot() -> dict:
            snap = {}
            for nid in key_of.values():
                state = engine.cache.get_node(nid)
                snap[nid] = (
                    state.mass,
                    state.return_count,
                    state.last_access,
                    state.temperature,
                    engine.get_displacement_norm(nid),
                )
            return snap

        before = snapshot()
        response = await memory_service.explore(
            engine, query=QUERY, diversity=0.8, top_k=5,
            auto_route=False, passive=True,
        )
        assert response.diversity == 0.8
        assert response.count == 5
        assert snapshot() == before
    finally:
        await engine.shutdown()


# --- 8. service wiring drives the engine diversity path ----------------------


async def test_service_explore_hits_engine_diversity_path(tmp_path):
    engine = await _make_engine(tmp_path)
    try:
        key_of = await _index_corpus(engine)

        plain = await engine.query(text=QUERY, top_k=5, passive=True)
        response = await memory_service.explore(
            engine, query=QUERY, diversity=0.8, top_k=5,
            auto_route=False, passive=True,
        )
        plain_keys = _keys(engine, plain, key_of)
        explore_keys = [
            next(k for k, nid in key_of.items() if nid == m.id)
            for m in response.items
        ]
        assert plain_keys == PLAIN_TOP5
        assert explore_keys == D08_TOP5
        assert explore_keys != plain_keys
    finally:
        await engine.shutdown()


# --- 9. Phase U WP-5 — promoted-default acceptance ----------------------------


async def test_promoted_default_explore_acceptance(tmp_path):
    """All Phase U promoted defaults ON (the fixture pin equals the code
    default; the guard below fences the combination) on the shared-token
    corpus:

    (a) Jaccard@5(explore d=0.8, recall top-5) < 1.0 — the diversified
        presentation is not a plain-recall clone under the promoted
        combination
    (b) ≥1 lateral result whose cluster key (cohort_id OR original_id —
        Stage 7.1 semantics) differs from top-1's, clearing
        explore_min_semantic
    (c) a below-floor pool candidate is excluded (the pool covers the
        whole corpus, so only the floor can exclude it)
    (d) deterministic across two identical runs (passive explore —
        fresh fixture nodes carry temperature 0, so no rng seam fires)
    """
    engine = await _make_engine(tmp_path)
    try:
        cfg = engine.config
        assert cfg.direct_qualification_enabled is True
        assert cfg.ttt_qualification_enabled is True
        assert cfg.explore_diversified_presentation_enabled is True

        corpus = CORPUS + [("OFF", "zzz qqq www vvv", None, [])]
        key_of = await _index_corpus(engine, corpus)
        off_id = key_of["OFF"]

        q_vec = engine.embedder.encode_query(QUERY)[0]

        def raw_cos(node_id: str) -> float:
            v = engine.faiss_index.get_vectors([node_id])[node_id]
            return float(np.dot(q_vec, v)) / (
                float(np.linalg.norm(q_vec)) * float(np.linalg.norm(v))
            )

        # fixture honesty (c): OFF is below the floor, and every active
        # doc sits inside the widened pool (top_k × multiplier ≥ corpus
        # size), so only the floor — not the pool cut — can exclude it.
        assert raw_cos(off_id) < cfg.explore_min_semantic
        assert 5 * cfg.explore_diversity_pool_multiplier >= len(corpus)

        plain = await engine.query(text=QUERY, top_k=5, passive=True)
        explore_resp = await memory_service.explore(
            engine, query=QUERY, diversity=0.8, top_k=5,
            auto_route=False, passive=True,
        )
        plain_ids = [r.id for r in plain]
        explore_ids = [m.id for m in explore_resp.items]

        # (a) Jaccard@5 < 1.0
        jaccard = len(set(plain_ids) & set(explore_ids)) / len(
            set(plain_ids) | set(explore_ids)
        )
        assert jaccard < 1.0

        # (b) lateral beyond top-1's cluster, above the relevance floor
        def cluster_of(nid: str) -> str | None:
            return cluster_key_from_cache(engine.cache, nid)

        laterals = [
            nid for nid in explore_ids[1:]
            if cluster_of(nid) != cluster_of(explore_ids[0])
        ]
        assert laterals, "no lateral (different cluster) result surfaced"
        assert all(
            raw_cos(nid) >= cfg.explore_min_semantic for nid in laterals
        )

        # (c) the below-floor candidate never presents; every presented
        # item cleared the floor
        assert off_id not in explore_ids
        assert all(
            raw_cos(nid) >= cfg.explore_min_semantic for nid in explore_ids
        )

        # (d) deterministic across two identical runs
        second = await memory_service.explore(
            engine, query=QUERY, diversity=0.8, top_k=5,
            auto_route=False, passive=True,
        )
        assert [m.id for m in second.items] == explore_ids
    finally:
        await engine.shutdown()


# --- 10. Phase U WP-5 — selection trace fields --------------------------------


async def test_selection_trace_fields_active_and_passive(tmp_path):
    """ScoreBreakdown carries the WP-5 selection trace:

    - ``cohort`` — the Stage 7.1 structural cluster key (cohort_id OR
      original_id); every corpus doc carries original_id (explicit or
      the engine's own-id fallback) so it is populated for all
    - ``provenance`` — raw / virtual / forced; forced-injected items are
      labelled "forced"
    - ``in_learn_set`` — Stage 4 learn-set membership on active recalls
      (True for qualified presented, False for the presented-but-
      unqualified lateral), None on passive recalls (no learn set —
      all-reached would make the field carry no information)
    """
    engine = await _make_engine(tmp_path)
    try:
        key_of = await _index_corpus(engine)
        results = await engine.query(text=QUERY, top_k=5, diversity=0.8)
        assert _keys(engine, results, key_of) == D08_TOP5

        by_id = {r.id: r for r in results}
        for r in results:
            b = r.score_breakdown
            assert b is not None
            assert b.cohort == cluster_key_from_cache(engine.cache, r.id)
            assert b.cohort is not None
            assert b.provenance in {"raw", "virtual", "forced"}
            assert isinstance(b.in_learn_set, bool)

        # Z1 is the calibrated presented-but-unqualified lateral (raw cos
        # 0.697 < 0.75 and below every other axis) → trains nothing
        assert by_id[key_of["Z1"]].score_breakdown.in_learn_set is False
        qualified_presented = [r for r in results if r.id != key_of["Z1"]]
        assert all(
            r.score_breakdown.in_learn_set is True
            for r in qualified_presented
        )

        # passive recall: no learn set → in_learn_set None; cohort /
        # provenance remain observable (read-only trace)
        passive = await engine.query(text=QUERY, top_k=5, passive=True)
        assert passive
        for r in passive:
            b = r.score_breakdown
            assert b is not None
            assert b.in_learn_set is None
            assert b.cohort is not None
            assert b.provenance in {"raw", "virtual", "forced"}
    finally:
        await engine.shutdown()


async def test_selection_trace_provenance_forced_injection(tmp_path):
    """tag_filter-forced items carry provenance="forced"; the MMR natural
    slots carry raw/virtual (raw here — the fixture wires no virtual
    FAISS index, so every non-forced entry is raw-index-sourced)."""
    sub = tmp_path / "forced"
    sub.mkdir()
    engine = await _make_engine(sub)
    try:
        corpus = [
            (k, c, o, (["forced-set"] if k in ("Y1", "Z1") else t))
            for k, c, o, t in CORPUS
        ]
        key_of = await _index_corpus(engine, corpus)
        results = await engine.query(
            text=QUERY, top_k=5, tag_filter=["forced-set"], diversity=0.8,
        )
        forced_ids = {key_of["Y1"], key_of["Z1"]}
        assert {r.id for r in results[:2]} == forced_ids
        for r in results[:2]:
            assert r.score_breakdown.provenance == "forced"
        for r in results:
            assert r.score_breakdown.provenance in {"raw", "virtual", "forced"}
    finally:
        await engine.shutdown()


# --- 11. Phase U WP-5 — formatter contract: strictly additive trace segments --


async def test_formatter_trace_segments_additive(tmp_path):
    """MCP formatter emits the WP-5 trace as strictly TRAILING segments
    (`` c=`` / `` src=`` / `` learn=``) after the legacy breakdown line,
    and explore gains the ``wave:`` header line between the existing
    header and the item list — existing segment formats and order
    untouched (CLAUDE.md formatter rule)."""
    from gaottt.services import formatters

    engine = await _make_engine(tmp_path)
    try:
        await _index_corpus(engine)
        recall_resp = await memory_service.recall(
            engine, query=QUERY, top_k=3, auto_route=False,
        )
        out = formatters.format_recall(recall_resp)
        assert "breakdown:" in out
        # legacy segments keep their exact format
        assert "cos=" in out and "vcos=" in out and "sat=" in out
        # Phase T Stage 3 segments precede the WP-5 trace
        assert " q=+" in out or " q=-" in out
        # WP-5 trace segments present
        assert " c=" in out
        assert " src=" in out
        assert " learn=+" in out or " learn=-" in out
        # trace comes after the legacy segments (trailing additivity)
        line = next(ln for ln in out.splitlines() if "breakdown:" in ln)
        assert line.index("persona_prox=") < line.index(" c=")
        if " gap=" in line:
            assert line.index(" gap=") < line.index(" c=")

        explore_resp = await memory_service.explore(
            engine, query=QUERY, diversity=0.8, top_k=5,
            auto_route=False, passive=True,
        )
        exp_out = formatters.format_explore(explore_resp)
        assert exp_out.startswith("Exploration (diversity=0.8):")
        lines = exp_out.splitlines()
        # the wave line sits between the existing header and the items
        assert lines[1].startswith("wave: depth=")
        assert " reached=" in lines[1]
        # depth is the explore-widened wave depth; reached covers ≥ count
        assert explore_resp.wave_depth == (
            engine.config.wave_max_depth + int(0.8 * 2)
        )
        assert explore_resp.wave_reached >= explore_resp.count
    finally:
        await engine.shutdown()
