"""Phase I Stage 2 / Stage 3 — Implicit query-aware displacement kick (integration).

End-to-end through engine.query():
  1. After repeated `recall(q)`, retrieved nodes' displacement drifts toward
     `q`'s embedding (cos(displacement, q - raw) becomes positive and grows).
  2. The raw embedding stored in FAISS never changes — Stage 2 is *transient
     force*, not anchor migration.
  3. With query_kick_strength=0 the legacy behaviour is preserved (control).
  4. Stage 3 — mass_anchor_threshold > 0 dampens drift on low-mass (new) nodes
     compared to Stage 2 (threshold=0), end-to-end through the engine path.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.index.faiss_index import FaissIndex
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore


class StubEmbedder:
    """Deterministic embeddings with controlled wave connectivity.

    L-flaky fix (2026-05-18): the previous implementation used
    ``abs(hash(text))`` — Python's builtin ``hash`` is salted per process
    via ``PYTHONHASHSEED``, so the embedding geometry changed every run and
    ``test_query_kick_drifts_*`` / ``test_stage3_gate_dampens_*`` flapped
    (for some seeds the probe's gravity wave never reached the target, so
    the displacement assertion had nothing to assert on). Two changes make
    it robust *and* preserve the tests' physical intent:

      1. Seed from a stable cross-process hash (``hashlib.sha256``).
      2. Every vector = a shared base direction + a small per-text
         perturbation. High mutual cosine (~0.97) guarantees the wave
         reliably connects every doc to every probe, while distinct
         perturbations keep ``q - raw`` non-degenerate so the query-kick
         direction is well-defined. This is the connectivity the tests
         *assumed* but random near-orthogonal 768-d vectors didn't provide.
    """

    def __init__(self, dim: int = 768):
        self.dim = dim
        self._base = np.ones(dim, dtype=np.float32) / np.sqrt(dim)

    def encode_documents(self, contents):
        return np.array([self._embed(c) for c in contents], dtype=np.float32)

    def encode_query(self, text):
        return self._embed(text).reshape(1, -1).astype(np.float32)

    def encode_queries(self, texts):
        # Multi-Source Query batch path — one row per segment.
        return np.array([self._embed(t) for t in texts], dtype=np.float32)

    def _embed(self, text: str) -> np.ndarray:
        seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
        rng = np.random.default_rng(seed)
        perturb = rng.standard_normal(self.dim).astype(np.float32)
        perturb /= np.linalg.norm(perturb) + 1e-9
        v = self._base + 0.15 * perturb
        v /= np.linalg.norm(v) + 1e-9
        return v


def _make_engine(
    tmp_path,
    *,
    kick_strength: float,
    mass_anchor_threshold: float = 0.0,
    mass_anchor_extra_strength: float = 0.0,
    # Phase U WP-1 — mass-trajectory pins for the redesigned Stage 3 gate
    # test. Defaults are the GaOTTTConfig values, so every other test in
    # this file keeps its original physics bit-for-bit.
    eta: float = 0.05,
    gravity_G: float = 0.01,
    orbital_anchor_strength: float = 0.02,
):
    config = GaOTTTConfig(
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "test.db"),
        faiss_index_path=str(tmp_path / "test.faiss"),
        # Phase I Stage 2 knobs
        query_kick_strength=kick_strength,
        query_kick_enabled=True,
        # Phase I Stage 3 — default to 0 here so existing Stage 2 tests
        # keep their pure F=ma semantics. New Stage 3 tests pass θ=3.0.
        mass_anchor_threshold=mass_anchor_threshold,
        # Phase I Stage 4 — Mass-dependent Hooke (β, default 0 = legacy).
        mass_anchor_extra_strength=mass_anchor_extra_strength,
        # Phase Q2 governor is default-ON since 2026-05-31 but is an orthogonal,
        # later mechanism (it caps *neighbour gravity*, not the query kick). Its
        # |d|-dependent cap would confound this suite's isolation of the Phase I
        # query-attraction term, so pin it OFF here. The governor itself is
        # covered by tests/unit/test_phase_q2_governor.py.
        gravity_neighbor_governor_enabled=False,
        # Suppress unrelated noise so the kick is the dominant signal
        genesis_kick_enabled=False,
        dream_enabled=False,
        faiss_save_interval_seconds=0.0,
        flush_interval_seconds=0.05,
        eta=eta,
        gravity_G=gravity_G,
        orbital_anchor_strength=orbital_anchor_strength,
    )
    embedder = StubEmbedder(dim=config.embedding_dim)
    faiss_index = FaissIndex(dimension=config.embedding_dim)
    store = SqliteStore(db_path=config.db_path)
    cache = CacheLayer(
        flush_interval=config.flush_interval_seconds,
        flush_threshold=config.flush_threshold,
    )
    return GaOTTTEngine(
        config=config, embedder=embedder, faiss_index=faiss_index,
        cache=cache, store=store,
    )


@pytest.mark.asyncio
async def test_query_kick_drifts_displacement_toward_query(tmp_path):
    """Repeated recalls of the same query should pull the retrieved node's
    displacement toward the query embedding (positive and growing inner
    product with the kick direction)."""
    engine = _make_engine(tmp_path, kick_strength=0.05)  # exaggerated for test
    await engine.startup()
    try:
        # Seed a small cluster so the wave has ≥ 2 reached nodes (the
        # orbital update path requires this).
        await engine.index_documents([
            {"content": f"doc-{i}", "metadata": {"source": "agent"}}
            for i in range(5)
        ])

        # Pick one document as our probe target.
        target = "doc-0"
        target_id = (await engine.query(text=target, top_k=1))[0].id
        raw_emb_before = engine.faiss_index.get_vectors([target_id])[target_id].copy()

        # The kick direction is (query_anchor - virtual_pos). Since we're
        # using the same text as both the doc and the query, query == raw,
        # and the kick first goes toward -displacement (i.e., back to anchor).
        # That's degenerate. Use a *different* query that still retrieves
        # target via the wave so the kick has a non-zero (q - raw) direction.
        probe = "doc-1"  # distinct embedding, but a wave neighbor of doc-0
        q_emb = engine.embedder.encode_query(probe)[0]

        for _ in range(20):
            await engine.query(text=probe, top_k=5)

        disp_after = engine.cache.get_displacement(target_id)
        raw_emb_after = engine.faiss_index.get_vectors([target_id])[target_id]

        # 1. Raw embedding must be unchanged — Stage 2 is transient force,
        #    not anchor migration.
        assert np.allclose(raw_emb_before, raw_emb_after, atol=1e-7)

        # 2. Displacement, if non-zero, must have a positive component
        #    along (q - raw). With other forces present it may not be
        #    perfectly aligned, but the projection should be positive.
        if disp_after is not None and float(np.linalg.norm(disp_after)) > 1e-6:
            kick_dir = (q_emb - raw_emb_before)
            kick_dir = kick_dir / (float(np.linalg.norm(kick_dir)) + 1e-9)
            projection = float(np.dot(disp_after, kick_dir))
            assert projection > 0.0, (
                f"displacement projection onto (q - raw) was {projection:.4g}, "
                f"expected positive after 20 recalls with query_kick_strength=0.05"
            )
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_query_kick_zero_alpha_preserves_legacy(tmp_path):
    """With query_kick_strength=0, the recall path should not introduce any
    new displacement component beyond what the legacy 3-component physics
    produces. Direct regression guard against accidental enabling."""
    engine = _make_engine(tmp_path, kick_strength=0.0)
    await engine.startup()
    try:
        await engine.index_documents([
            {"content": f"ctrl-doc-{i}", "metadata": {"source": "agent"}}
            for i in range(5)
        ])

        target_id = (await engine.query(text="ctrl-doc-0", top_k=1))[0].id
        for _ in range(10):
            await engine.query(text="ctrl-doc-1", top_k=5)

        # With α=0 the only displacement source is neighbor gravity + Hooke;
        # we can't assert disp==0 (neighbor gravity will move it), only that
        # the test would distinguish from the kick=0.05 case via the
        # projection test above. Here we just guard against runtime errors
        # along the no-kick code path.
        disp = engine.cache.get_displacement(target_id)
        assert disp is None or float(np.linalg.norm(disp)) < 1.0
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_query_kick_does_not_migrate_raw_anchor(tmp_path):
    """Stronger version of property #1: even after many recalls the FAISS
    raw embedding for the target must equal its initial value bit-for-bit."""
    engine = _make_engine(tmp_path, kick_strength=0.05)
    await engine.startup()
    try:
        await engine.index_documents([
            {"content": f"anchor-doc-{i}", "metadata": {"source": "agent"}}
            for i in range(5)
        ])
        target_id = (await engine.query(text="anchor-doc-0", top_k=1))[0].id
        initial = engine.faiss_index.get_vectors([target_id])[target_id].copy()
        for _ in range(30):
            await engine.query(text="anchor-doc-2", top_k=5)
        final = engine.faiss_index.get_vectors([target_id])[target_id]
        assert np.array_equal(initial, final)
    finally:
        await engine.shutdown()


# ---------------------------------------------------------------------------
# Phase I Stage 3 — Mass-gated query attraction (integration)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stage3_gate_dampens_drift_for_new_nodes(tmp_path):
    """Stage 3 (mass-gated kick) dampens displacement drift on freshly-added
    nodes compared to Stage 2 (no gate), verifying the gate is actually
    applied through the full engine.query() pipeline — not just present in
    compute_acceleration.

    Setup: identical docs and probes, only mass_anchor_threshold differs.
      θ=0  → Stage 2 (gate=1.0): full F=ma kick toward q, larger projection
      θ=3  → Stage 3 (gate≈0.32 at mass=1): damped kick, smaller projection

    We measure the *projection* of displacement onto (q - raw) rather than
    total displacement norm because only the q-direction component isolates
    the query attraction term that Stage 3 gates.

    Phase U WP-1 redesign — mass-trajectory-independent contract comparison
    (plan approach (a)+(b): pin the mass, compare the kick-term effect
    directly). The original fixture compared two free-running 20-recall
    trajectories whose margins (3.6%) flipped sign once the promoted
    ttt_qualification confidence scaling made the two engines' mass
    accretion diverge by ~2.7% (calibration-round.md §2). Three pins make
    the comparison depend on the gate alone:
      - ``eta=0.0`` — no mass growth in either engine, so both trajectories
        stay bit-identical at the new-node mass 1.0 (exactly the regime the
        contract names: "dampens drift on a low-mass node").
      - ``gravity_G=0.0`` — neighbour N-body gravity off: all stub docs share
        a base direction, so neighbour pull has a large gate-independent
        projection onto (q - raw) (measured ratio 0.91 with it on — the
        common term swamps the kick difference).
      - ``orbital_anchor_strength=1.0`` — a stiff Hooke anchor keeps the
        Verlet dynamics in the linear regime where equilibrium displacement
        ∝ gate. With the soft default (0.02) the exaggerated kick α=0.05+
        saturates ``orbital_max_velocity`` (0.05) in *both* engines, and the
        rate limiter — not the gate — sets the drift (measured ratio 1.05,
        the very inversion this redesign eliminates).
    Measured outcome (deterministic stub): proj ratio ≈ 0.20 (gate is
    tanh(1/3) ≈ 0.32; relaxation nonlinearity pushes the ratio lower), so
    the ratio bound below has ~3x headroom.

    This is the engine-level acceptance test for the single-attractor
    pathology fix: Stage 3 prevents new nodes from being one-shot drifted
    into the "near every query" position by anchor (Hooke) protection.
    """
    async def measure_q_projection(subdir: str, threshold: float) -> float:
        path = tmp_path / subdir
        path.mkdir()
        # Bump kick strength so the gate's effect is measurable above
        # residual noise within 20 recall steps.
        engine = _make_engine(
            path, kick_strength=0.5, mass_anchor_threshold=threshold,
            # Phase U WP-1 mass-trajectory pins (see docstring)
            eta=0.0,
            gravity_G=0.0,
            orbital_anchor_strength=1.0,
        )
        await engine.startup()
        try:
            await engine.index_documents([
                {"content": f"drift-doc-{i}", "metadata": {"source": "agent"}}
                for i in range(5)
            ])
            # Phase U WP-4b — target は probe query の top-1 から選ぶ。
            # 旧 fixture は "drift-doc-0" (文書本文と同一) の top-1 を
            # target にしていたが、raw-top rescue の導入で top-1 は
            # exact-match doc (probe に対しては unqualified) に変わり、
            # Stage 4 learn-set が正しく kick を止めるため proj≈0 で
            # fixture が崩れた。kick を観測するには probe に対して
            # qualified な node (learn set に入る node) を測るべきなので、
            # 選択を probe 自身の top-1 に変更する (rescue ON では
            # raw-top qualified、OFF では qualified-first の head —
            # いずれも probe-qualified ことが構造上保証される)。
            target_id = (
                await engine.query(text="drift-probe-distinct", top_k=1)
            )[0].id
            # Mass must actually be pinned for the comparison to be
            # trajectory-independent — assert the fixture's own contract.
            assert engine.cache.get_node(target_id).mass == 1.0
            raw_emb = engine.faiss_index.get_vectors([target_id])[target_id].copy()
            q_emb = engine.embedder.encode_query("drift-probe-distinct")[0]
            kick_dir = q_emb - raw_emb
            kick_dir = kick_dir / (float(np.linalg.norm(kick_dir)) + 1e-9)
            for _ in range(20):
                await engine.query(text="drift-probe-distinct", top_k=5)
            assert engine.cache.get_node(target_id).mass == 1.0, (
                "fixture contract violated: mass accreted despite eta=0"
            )
            disp = engine.cache.get_displacement(target_id)
            if disp is None:
                return 0.0
            return float(np.dot(disp, kick_dir))
        finally:
            await engine.shutdown()

    proj_stage2 = await measure_q_projection("s2", 0.0)
    proj_stage3 = await measure_q_projection("s3", 3.0)

    # Both modes must produce positive drift toward q (kick is active)
    assert proj_stage2 > 0, f"Stage 2 should drift toward q, got proj={proj_stage2}"
    assert proj_stage3 > 0, f"Stage 3 should drift toward q, got proj={proj_stage3}"
    # Stage 3 gate must dampen the q-direction drift on a low-mass node.
    # With identical (pinned) mass trajectories the ratio is a direct
    # observable of the gate factor tanh(1/3) ≈ 0.32 — hold it well below
    # the ungated 1.0 so a gate regression cannot hide behind noise.
    assert proj_stage3 < proj_stage2, (
        f"Stage 3 gate should reduce q-direction drift on low-mass node — "
        f"proj_stage2={proj_stage2:.4f}, proj_stage3={proj_stage3:.4f}"
    )
    assert proj_stage3 < 0.6 * proj_stage2, (
        f"Stage 3 gate should dampen the drift substantially (gate "
        f"tanh(1/3)≈0.32), got ratio {proj_stage3 / proj_stage2:.3f} — "
        f"proj_stage2={proj_stage2:.4f}, proj_stage3={proj_stage3:.4f}"
    )


# ---------------------------------------------------------------------------
# Phase I Stage 4 — Mass-dependent Hooke (integration via update_orbital_state)
#
# The engine.query() path is timing-fragile (age_friction depends on real-time
# elapsed between recalls, varying with system load). Stage 4's Hooke
# amplification is also masked by the velocity cap when kick magnitude is
# large. We therefore test Stage 4 at the orchestration layer directly:
# update_orbital_state takes explicit dicts (no real-time, no FAISS, no
# wave), exercising compute_acceleration in the same shape the engine does.
# Determinism: same inputs → same outputs.
# ---------------------------------------------------------------------------

def test_stage4_amplified_hooke_shrinks_displacement_in_orbital_step():
    """One full update_orbital_state step with β > 0 produces smaller
    post-step displacement on a low-mass node than β = 0, given identical
    inputs and no query attraction. Direct verification that Stage 4's
    Hooke amplification reaches the orbital integrator."""
    from gaottt.core.gravity import update_orbital_state

    dim = 768
    node_ids = ["target", "neighbor"]
    raw = {
        "target": np.array([1.0] + [0.0] * (dim - 1), dtype=np.float32),
        "neighbor": np.array([0.0, 1.0] + [0.0] * (dim - 2), dtype=np.float32),
    }
    initial_disp = np.zeros(dim, dtype=np.float32)
    initial_disp[2] = 0.5  # large initial displacement so Hooke is the dominant force

    masses = {"target": 1.0, "neighbor": 1.0}  # low-mass: Stage 4 should engage
    last_accesses = {"target": 0.0, "neighbor": 0.0}

    def run_one_step(beta: float) -> np.ndarray:
        config = GaOTTTConfig(
            mass_anchor_extra_strength=beta,
            mass_anchor_threshold=3.0,
            query_kick_strength=0.0,
            query_kick_enabled=False,
            mass_bh_enabled=False,
            # Fix to a high cap so velocity isn't the limiter — we want
            # Hooke to be the dominant force shaping the next displacement.
            orbital_max_velocity=10.0,
            max_displacement_norm=1e6,
        )
        displacements = {
            "target": initial_disp.copy(),
            "neighbor": np.zeros(dim, dtype=np.float32),
        }
        velocities = {
            "target": np.zeros(dim, dtype=np.float32),
            "neighbor": np.zeros(dim, dtype=np.float32),
        }
        new_disps, _ = update_orbital_state(
            node_ids, raw, displacements, velocities,
            masses, last_accesses, now=1.0, config=config,
        )
        return new_disps["target"]

    legacy = run_one_step(beta=0.0)
    stage4 = run_one_step(beta=2.0)

    # The displacement-axis component (axis 2) should be smaller in magnitude
    # under Stage 4 because anchor pulls harder back toward 0.
    legacy_axis = float(legacy[2])
    stage4_axis = float(stage4[2])

    assert legacy_axis > 0, "test setup: initial disp on axis 2 should survive one step under β=0"
    assert 0 < stage4_axis < legacy_axis, (
        f"Stage 4 (β=2) should pull axis-2 disp back harder than β=0: "
        f"legacy={legacy_axis:.6f}, stage4={stage4_axis:.6f}"
    )


def test_stage4_beta_zero_matches_legacy_in_orbital_step():
    """β=0 with mass_i passed must produce bit-for-bit identical displacement
    to the constant-k Hooke baseline. This is the rollback guarantee — any
    drift between β=0 and the pre-Stage-4 codepath indicates the anchor_factor
    branch is leaking state.
    """
    from gaottt.core.gravity import update_orbital_state

    dim = 768
    node_ids = ["a", "b"]
    raw = {
        "a": np.array([1.0] + [0.0] * (dim - 1), dtype=np.float32),
        "b": np.array([0.0, 1.0] + [0.0] * (dim - 2), dtype=np.float32),
    }
    initial_disp = np.zeros(dim, dtype=np.float32)
    initial_disp[3] = 0.2
    masses = {"a": 1.0, "b": 1.0}
    last_accesses = {"a": 0.0, "b": 0.0}

    def run(beta: float, theta: float) -> np.ndarray:
        config = GaOTTTConfig(
            mass_anchor_extra_strength=beta,
            mass_anchor_threshold=theta,
            query_kick_strength=0.0,
            query_kick_enabled=False,
            mass_bh_enabled=False,
            orbital_max_velocity=10.0,
            max_displacement_norm=1e6,
        )
        displacements = {
            "a": initial_disp.copy(),
            "b": np.zeros(dim, dtype=np.float32),
        }
        velocities = {
            "a": np.zeros(dim, dtype=np.float32),
            "b": np.zeros(dim, dtype=np.float32),
        }
        new_disps, _ = update_orbital_state(
            node_ids, raw, displacements, velocities,
            masses, last_accesses, now=1.0, config=config,
        )
        return new_disps["a"]

    legacy = run(beta=0.0, theta=3.0)
    legacy_with_zero_theta = run(beta=0.0, theta=0.0)  # also force the early-out
    assert np.array_equal(legacy, legacy_with_zero_theta)
