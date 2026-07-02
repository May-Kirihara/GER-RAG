import json
import logging
import math
import os
import sys
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path

logger = logging.getLogger(__name__)

# Config file locations (checked in order):
#   1. GAOTTT_CONFIG env var (legacy GER_RAG_CONFIG accepted with deprecation warning)
#   2. ~/.config/gaottt/config.json  (Linux/macOS)
#      %APPDATA%/gaottt/config.json  (Windows)
#   3. Legacy ~/.config/ger-rag/config.json (fallback for migrating users)
_CONFIG_FILE_PATHS = []

_env_config = os.environ.get("GAOTTT_CONFIG") or os.environ.get("GER_RAG_CONFIG")
if os.environ.get("GER_RAG_CONFIG") and not os.environ.get("GAOTTT_CONFIG"):
    logger.warning(
        "GER_RAG_CONFIG is deprecated; use GAOTTT_CONFIG. "
        "Continuing with the legacy variable."
    )
if _env_config:
    _CONFIG_FILE_PATHS.append(Path(_env_config))

if sys.platform == "win32":
    _appdata = os.environ.get("APPDATA", "")
    if _appdata:
        _CONFIG_FILE_PATHS.append(Path(_appdata) / "gaottt" / "config.json")
        _CONFIG_FILE_PATHS.append(Path(_appdata) / "ger-rag" / "config.json")
else:
    _xdg = os.environ.get("XDG_CONFIG_HOME", "")
    _config_base = Path(_xdg) if _xdg else Path.home() / ".config"
    _CONFIG_FILE_PATHS.append(_config_base / "gaottt" / "config.json")
    _CONFIG_FILE_PATHS.append(_config_base / "ger-rag" / "config.json")


def _load_config_file() -> dict:
    """Load config overrides from JSON config file."""
    for path in _CONFIG_FILE_PATHS:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                logger.info("Loaded config from %s", path)
                return data
            except (json.JSONDecodeError, OSError):
                pass
    return {}


def _legacy_data_dir_default() -> Path:
    """Where the old GER-RAG default data directory would have been."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        return Path(local) / "ger-rag" if local else Path.home() / "ger-rag"
    xdg = os.environ.get("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "ger-rag"


def _default_data_dir() -> str:
    """Resolve data directory.

    Priority:
      1. GAOTTT_DATA_DIR env var (legacy GER_RAG_DATA_DIR accepted with warning)
      2. "data_dir" in config file
      3. Platform default:
         - Linux/macOS: ~/.local/share/gaottt/
         - Windows:     %LOCALAPPDATA%/gaottt/

    Backward compatibility: if no env/config override is set and the
    new platform default is empty, but a legacy ger-rag/ directory exists
    with a real ger_rag.db, prefer the legacy path so existing users do
    not lose access to their memory. Run scripts/migrate-from-ger-rag.sh
    to move the data permanently.
    """
    env = os.environ.get("GAOTTT_DATA_DIR") or os.environ.get("GER_RAG_DATA_DIR")
    if os.environ.get("GER_RAG_DATA_DIR") and not os.environ.get("GAOTTT_DATA_DIR"):
        logger.warning(
            "GER_RAG_DATA_DIR is deprecated; use GAOTTT_DATA_DIR. "
            "Continuing with the legacy variable."
        )
    if env:
        p = Path(env)
    else:
        file_conf = _load_config_file()
        if "data_dir" in file_conf:
            p = Path(file_conf["data_dir"])
        elif sys.platform == "win32":
            local = os.environ.get("LOCALAPPDATA", "")
            p = Path(local) / "gaottt" if local else Path.home() / "gaottt"
        else:
            xdg = os.environ.get("XDG_DATA_HOME", "")
            base = Path(xdg) if xdg else Path.home() / ".local" / "share"
            p = base / "gaottt"

        legacy = _legacy_data_dir_default()
        if not (p / "gaottt.db").exists() and (legacy / "ger_rag.db").exists():
            logger.warning(
                "Detected legacy GER-RAG data at %s. "
                "Run scripts/migrate-from-ger-rag.sh to migrate to %s, "
                "or set GAOTTT_DATA_DIR explicitly.",
                legacy, p,
            )
            p = legacy

    p.mkdir(parents=True, exist_ok=True)
    return str(p)


@dataclass
class GaOTTTConfig:
    # Embedding
    model_name: str = "cl-nagoya/ruri-v3-310m"
    embedding_dim: int = 768
    batch_size: int = 32

    # Multiverse embedding (MV1)
    # When ``embedder_endpoint`` is set (non-empty), ``build_engine`` wires a
    # ``RemoteEmbedder`` against it instead of an in-process ``RuriEmbedder``.
    # Empty string = default (in-process RuriEmbedder). Env:
    #   GAOTTT_EMBEDDER_ENDPOINT=http://127.0.0.1:7879
    # We use a sentinel empty-string default (not ``Optional[str] = None``)
    # so the generic GAOTTT_* env-override loop picks it up — MV3 supervisor
    # relies on passing the endpoint via env at spawn time.
    # ``embedder_request_timeout_seconds`` is a float default and is
    # env-settable via GAOTTT_EMBEDDER_REQUEST_TIMEOUT_SECONDS.
    embedder_endpoint: str = ""
    embedder_request_timeout_seconds: float = 30.0

    # Multiverse manifest
    # MV0 universe manifest gate. When True, a manifest embedding_dim or
    # embedder_id mismatch against config / runtime embedder is a hard
    # RuntimeError at startup / build_engine; when False it degrades to a
    # warning (escape hatch for migrations). The FAISS dimension guard
    # (embedder.dimension != config.embedding_dim) is manifest-independent
    # and always raises — it protects index integrity, not identity.
    manifest_check_enabled: bool = True

    # ---- Owner lease (single-owner coordination, WP-2) -----------------------
    # Ops / coordination layer, NOT physics. When ``owner_lease_enabled`` the
    # engine (WP-4) wraps its startup in an ``OwnerLease`` that coordinates
    # exclusive ownership of the data directory across processes via
    # ``owner.lock`` + ``owner.lock.guard`` (flock). The lease implementation
    # lives in ``gaottt/store/lease.py``; these knobs only configure it.
    # Defaults keep the feature OFF so existing deployments are bit-exact until
    # an operator opts in. Env via the generic loop: GAOTTT_<UPPER>.
    owner_lease_enabled: bool = False
    # Acquire supersedes an otherwise-active owner without staleness checks —
    # escape hatch for a wedged owner whose heartbeat is still fresh.
    lease_force_takeover: bool = False
    # heartbeat_loop write cadence (seconds). Must stay comfortably below
    # lease_stale_seconds so a live owner is never misjudged stale.
    lease_heartbeat_seconds: float = 10.0
    # A lock whose heartbeat is older than this (strict ``>``) is takeable.
    lease_stale_seconds: float = 60.0

    # Retrieval
    top_k: int = 5              # Results returned to LLM (presentation layer)

    # Scoring
    alpha: float = 0.05       # Mass boost scaling
    delta: float = 0.01       # Temporal decay rate
    gamma: float = 0.5        # Temperature scaling

    # Mass update
    eta: float = 0.05         # Mass growth rate
    m_max: float = 50.0       # Mass saturation limit

    # Co-occurrence graph
    edge_threshold: int = 5   # Co-occurrence count to form edge
    edge_decay: float = 0.97  # Edge weight decay factor
    prune_threshold: float = 0.5  # Edge removal threshold
    max_degree: int = 20      # Per-node edge cap

    # Gravitational displacement
    gravity_G: float = 0.01            # Gravitational constant
    gravity_eta: float = 0.005         # Displacement learning rate
    gravity_epsilon: float = 1e-6      # Zero-division guard
    displacement_decay: float = 0.995  # Per-step displacement decay
    displacement_age_delta: float = 0.005  # Access-age based decay rate
    max_displacement_norm: float = 1e6 # L2 norm cap on displacement. Phase I (2026-05-11): default raised from 0.3 → 1e6 (effectively ∞). Hooke (orbital_anchor_strength) + displacement_decay + orbital_max_velocity provide physical equilibrium around d ≈ (G·m/k)^(1/3) ≈ 0.8–3.0 without a hard cap. Set to a small value only as an emergency knob.

    # Phase I Stage 2 — Query-aware displacement (implicit kick)
    # Adds a 4th term to compute_acceleration: F_query = α · score · (q - pos);
    # a_query = F_query / m. Acts as the Hebbian gradient term in the TTT reading.
    # Hooke (orbital_anchor_strength) continues to pull back toward the raw
    # embedding anchor — query attraction is a transient force, not anchor
    # migration. Set to 0.0 as a clean roll-back.
    query_kick_strength: float = 0.01  # α — start small. Per-step accel is bounded by orbital_max_velocity (0.05), so α larger than ~0.05 saturates immediately. d=0.01 means ~10 recalls of the same query → ~0.1 drift toward query (Hooke equilibrium ~0.8).
    query_kick_enabled: bool = True    # Global off-switch; if False, skip the 4th term entirely.

    # Phase I Stage 3 — Mass-gated query attraction:
    # Adds a gate to the Stage 2 query-attraction term so that brand-new
    # (low-mass) nodes are protected by anchor (Hooke) until co-occurrence
    # structure forms. Without this gate, m_i ≈ 1.0 lets a single recall
    # trigger a near-full-α drift, producing the single-attractor pathology
    # (one node becoming top1 for many queries via positive feedback).
    #   gate = tanh(m_i / mass_anchor_threshold)
    #     mass=1, θ=3   → 0.32  (newly-added: heavily damped)
    #     mass=3, θ=3   → 0.76  (gate's characteristic point)
    #     mass=10, θ=3  → 0.997 (mature: nearly full)
    # Set θ=0.0 for clean rollback to Stage 2 behaviour (gate forced to 1.0).
    mass_anchor_threshold: float = 3.0

    # Phase I Stage 4 — Mass-dependent Hooke (symmetric form of Stage 3).
    # Stage 3 gated the query-attraction kick by mass so brand-new nodes are
    # protected from one-shot drift. Stage 4 is the dual move: the Hooke
    # restoring force itself is amplified for low-mass nodes — the anchor is
    # stronger when the node hasn't yet earned a gravity well of its own.
    #   k_eff(m) = k · (1 + β · (1 - tanh(m / θ)))
    #     mass=1, θ=3, β=1   → 1 + 0.68  → 1.68× anchor   (newborn)
    #     mass=3, θ=3, β=1   → 1 + 0.24  → 1.24× anchor   (gate point)
    #     mass=10, θ=3, β=1  → 1 + 0.003 → ~1.00×        (mature)
    #     mass=50, θ=3, β=1  → 1 + 1e-15 → 1.00×        (BH)
    # β shares θ with Stage 3 so the two halves keep a single notion of
    # "newborn" — light nodes get strong anchor *and* damped kick; mature
    # nodes get base anchor *and* full kick. The asymmetric pair was the
    # learning of Phase I Stage 3 (protection short-fall = same homogenization
    # symptom as over-driving); Stage 4 closes the symmetry.
    #
    # Default β=0.0 — opt-in. Unlike Stage 3 (which fixed a directly observed
    # single-attractor pathology), Stage 4 is a prophylactic refinement.
    # Activate after 1-2 weeks of running with Stage 3 alone to confirm the
    # added anchor doesn't over-restrict mature drift. Set β=0.0 for a
    # bit-for-bit rollback to Stage 1-3 behaviour.
    mass_anchor_extra_strength: float = 0.0

    # ---- Phase P-α: Cosmological Λ (long-range repulsion) -------------------
    # Hubble-flow-shaped repulsion proportional to inter-node distance, added
    # to ``compute_acceleration`` alongside the four existing terms (neighbor
    # gravity / Hooke / mass-BH / query attraction). For every (pos_i, pos_j)
    # pair in the wave neighbor scope:
    #     a_Λ(i) += +H · (pos_i - pos_j)
    # i.e. each node is pushed AWAY from each neighbor with magnitude
    # proportional to their separation — the literal Hubble flow on a
    # non-relativistic embedding space. Local bound (dense gravity well) is
    # preserved because Newton's 1/r² dominates near zero; the Λ term wins
    # at long range. In retrieval terms: "every node is a galaxy in a single
    # supercluster" is forbidden — the cluster geometry stays a network of
    # bound groups separated by expanding voids, not a single black-hole sink.
    #
    # ``H`` is the (synthetic) Hubble constant. Default 0.001 = 1/10 of
    # ``gravity_G=0.01``, chosen so Λ at typical displacement (||Δ||~0.5) is
    # ~10 % of the gravity acceleration at the same scale (a_Λ ~ 5e-4 vs
    # a_grav ~ 5e-3). Tune via ``scripts/diag_pressure.py`` after Phase N β
    # observation. Default OFF.
    #
    # Self-force handling: ``neighbors`` passed to ``compute_acceleration``
    # is generated by ``propagate_gravity_wave`` (or the equivalent caller).
    # Whatever filter that caller applies to the neighbor set is shared by
    # Λ and the legacy gravity loop — Λ does NOT re-filter (Plan §3.1: "Λ is
    # filter-shared with gravity"). When the caller hands in a full active
    # set (e.g. ``update_orbital_state``), Λ acts on all pairs, matching
    # gravity's behavior in the same context.
    #
    # Plans: docs/wiki/Plans-Phase-P-Pressure-Terms.md
    cosmological_lambda_enabled: bool = False
    cosmological_lambda_h: float = 0.001    # Hubble-flow rate H

    # ---- Phase P-β: Langevin Temperature (thermal pressure) -----------------
    # Stochastic thermal kick added to the position-update step:
    #     new_disp += √(2 · T · dt) · ξ,    ξ ~ N(0, I),   dt = 1.0
    # This is the SGLD (Welling-Teh 2011) noise term in TTT reading:
    # gradient SGD + Gaussian kick of magnitude √(2η). Physically, it is a
    # Brownian/thermal-bath term that lets a particle escape sharp deep
    # wells with non-zero probability while broad shallow wells remain
    # stable. In retrieval terms: a singleton high-mass attractor (the
    # Stage 7.1 limitation — ffe48a30 / 24a0bf39 etc.) is *probabilistically*
    # escaped, while healthy diverse clusters (broad shallow wells) keep
    # their structure. Default OFF — opt-in env / config after Phase M
    # Stage 2 mass reset has settled and Phase N β evaporation is running.
    #
    # Distinct from the legacy per-node ``state.temperature`` field which
    # adds *read-time* noise inside ``compute_virtual_position`` (= measurement
    # disturbance, observation-side). Langevin is *write-time* noise on the
    # position update step (= dynamics-side, Brownian kick). The two are
    # orthogonal noise channels and coexist without interaction.
    #
    # Plans: docs/wiki/Plans-Phase-P-Pressure-Terms.md
    langevin_temperature_enabled: bool = False
    # Global temperature constant T₀. With dt=1, ``σ = √(2·T₀) ≈ 0.045`` for
    # T₀=0.001 → per-step L2 random walk ~0.045, which is ~5 % of the Hooke
    # equilibrium ``(G·m/k)^(1/3) ≈ 0.8`` — small enough to not dominate
    # gravity but large enough to lift a stuck singleton over a finite well.
    # Calibrate via ``scripts/diag_pressure.py`` snapshot + 1-2 week
    # production observation before raising.
    langevin_temperature_t0: float = 0.001

    # Phase J Stage 1 — Persona-anchored retrieval (graph traversal seed boost).
    # Boosts seed-pool ranking for nodes within N hops of an actively-declared
    # value / intention / commitment, via fulfills / derived_from edges.
    # Translates the Five-Layer persona layer into retrieval geometry — what
    # the user has declared as identity bends the gravity field at recall time.
    #   boosted_seed_score = raw_cosine + α_mass × log(1+mass)
    #                                   + α_persona × proximity
    #   proximity(node) = persona_hop_decay ** min_hop_distance(node, persona)
    #                     0.0 beyond persona_max_hop
    # Set persona_boost_enabled=False for clean rollback (boost path skipped).
    persona_boost_enabled: bool = True
    persona_boost_alpha: float = 0.5           # weight of proximity in seed rerank (5× wave_seed_mass_alpha — "context dominates mass" prior)
    persona_max_hop: int = 2                    # graph traversal limit (1=fulfills only, 2=+derived_from chain, 3+=indirect mixing risk)
    persona_hop_decay: float = 0.5              # per-hop decay (0.5: 1 hop=0.5, 2 hop=0.25)
    persona_active_ttl_seconds: float = 14 * 86400.0  # commitment TTL synchronized; intention/value are always active unless archived

    # Phase K Stage 1 — Stellar supernova cohort.
    # When `index_documents` receives a batch of size ≥ supernova_min_cohort_size,
    # the new nodes are treated as a single supernova event:
    #   (1) all pairs in the batch get a co-occurrence edge of weight
    #       supernova_initial_weight (event-driven, independent of Phase B's
    #       recall-based edge_threshold accumulation)
    #   (2) each node receives an outward initial velocity = α × (embedding - centroid),
    #       clamped to orbital_max_velocity
    # Applied AFTER Phase G genesis_kick (Phase G: existing-system binding;
    # Phase K: cohort-internal binding + explosion energy). Velocities are
    # added, not replaced — both physics co-exist.
    # Rationale: Phase J Stage 1 acceptance revealed that newly-remembered
    # cohorts have no mutual gravity, so they can't compete with mature
    # past-session clusters for FAISS top-K entry. Phase K fixes the physics
    # of memory creation rather than reranking after the fact.
    supernova_enabled: bool = True
    supernova_min_cohort_size: int = 2          # 1 件だけの remember は単独彗星 (Phase G で足りる)、2 件以上で発火
    supernova_initial_weight: float = 1.0       # 相互 edge の初期 weight (wave_seed_mass_alpha × log(1+w) で boost が効く)
    supernova_velocity_alpha: float = 0.03      # 爆発の運動量 α (orbital_max_velocity=0.05 以下に収まる)

    # Phase L Stage 1 — Hybrid retrieval (BM25 union seed).
    # Adds a third metric (BM25 lexical) alongside raw FAISS (semantic) and
    # virtual FAISS (semantic+history) to the seed pool. Lexical and semantic
    # are independent metric tensors — surface-form matches BM25 catches that
    # embedder cosine misses ("Eleventy Pipeline" → exact .eleventy.js match).
    # RRF fusion combines ranks scale-invariantly across the 3 indexes.
    #
    # Decision log (めいさん 2026-05-14):
    #   D1. bm25_score_mode default = "rrf" (Reciprocal Rank Fusion, k=60)
    #   D2. in-memory only in Stage 1 — startup rebuild from SQLite content;
    #       disk persistence is a future stage
    #   D3. tokenizer default = "trigram"; "sudachi" available as optional
    #       extra (uv pip install -e ".[bm25-sudachi]")
    #   D4. wave_neighbor 中の BM25 拡張なし — Stage 1 は seed pool 入場権のみ
    # Set hybrid_bm25_enabled=False for clean rollback (BM25 skipped entirely).
    hybrid_bm25_enabled: bool = True
    bm25_seed_k: int = 50                       # BM25 top-N drawn into the union pool
    bm25_k1: float = 1.5                        # Robertson-Sparck-Jones k1 (term-saturation)
    bm25_b: float = 0.75                        # length-normalization b
    bm25_score_mode: str = "rrf"                # "rrf" (default) | "weighted_sum"
    bm25_score_alpha: float = 0.5               # weighted_sum: BM25 normalized share; ignored for "rrf"
    rrf_k: int = 60                             # RRF rank-fusion constant (Cormack 2009 standard)
    bm25_tokenizer: str = "trigram"             # "trigram" (default) | "sudachi" (optional extra)

    # Query as Mass Distribution — Multi-Source Query.
    # A compound prompt pooled into one embedding is a centroid, and the
    # centroid is dragged toward whichever sub-topic is lexically densest in
    # the corpus (a meta-instruction naming a heavily-recorded entity can
    # drown the actual task). When enabled, the prompt is segmented into
    # clauses, each embedded as a separate point mass, and the wave seeds from
    # the RRF-superposed per-segment pools — one wave, not N. Gravity
    # superposes fields; it does not average masses. The pooled ``query_vec``
    # still anchors scoring and the TTT query-attraction term, so no physics
    # rule changes (this is why the feature consumes no Phase letter).
    # ``multi_source_enabled`` gates recall / explore; the separate
    # ``multi_source_ambient_enabled`` gates the every-turn ambient_recall
    # path (kept separate for perf isolation — the ambient hook fires on
    # each prompt). Both default True (2026-05-21): a real-RURI check put a
    # compound-query recall at ~2× single-source but p95 ~40ms — far under
    # the Tier 6 gate (120ms) and the ambient hook budget (~500ms). Simple
    # (non-compound) prompts do not segment, so they pay nothing. Set either
    # to False for a one-line rollback. See
    # docs/wiki/Plans-Query-Mass-Distribution.md.
    multi_source_enabled: bool = True
    multi_source_ambient_enabled: bool = True
    multi_source_max_segments: int = 4          # cap N — the longest N segments are kept
    multi_source_min_segment_chars: int = 12    # fragments below this merge into a neighbor

    # Gravity wave propagation
    wave_initial_k: int = 3            # Initial FAISS top-k for seed nodes
    # Phase M follow-up (2026-05-13): depth 2 → 3 to widen the displacement
    # update scope per recall (from ~20-50 nodes to ~60-150). Wave force
    # attenuation (`wave_attenuation`) still bounds the reach — depth 3
    # third-frontier nodes receive ~0.25 of seed force, deeper nodes drop
    # below the 0.001 force floor and are filtered automatically. Per-recall
    # latency cost +20-30% (mostly the extra per-frontier neighbor lookup).
    # Set to 2 to restore Phase L Stage 1 behaviour.
    wave_max_depth: int = 3            # Maximum recursion depth
    wave_base_k: int = 2              # Minimum neighbors per node
    wave_mass_scale: float = 2.0       # Mass-to-top-k scaling factor
    wave_max_node_k: int = 10          # Maximum neighbors per node
    wave_attenuation: float = 0.3      # Force decay per depth level
    wave_mass_attenuation_factor: float = 0.5  # Mass-based attenuation reduction
    wave_boost_weight: float = 0.05    # Wave force weight in final score

    # Gravitational radius — derived from a = G * m / r²
    wave_gravity_a_min: float = 0.1         # Minimum gravitational acceleration threshold

    # Co-occurrence black hole (Phase M: deprecated — replaced by mass-based BH).
    # Kept as fields so legacy configs and visualize_3d.py don't error; the
    # values are no longer consulted at runtime once
    # mass_bh_enabled=True. Removed in Phase M Stage 2.
    bh_mass_scale: float = 0.5             # deprecated; see mass_bh_*
    bh_gravity_G: float = 0.0              # deprecated; see mass_bh_*

    # Phase M Stage 1 — Mass conservation + mass-based BH.
    # (1) Mass conservation: when mass_conservation_enabled=True, the mass
    #     update in _update_simulation only counts force contributions from
    #     parent nodes that are NOT in the same original document
    #     (original_id) or supernova cohort (cohort_id). "Internal trade"
    #     between chunks of the same book no longer inflates mass —
    #     Articulation as Carrier (id=9a954c62) made literal.
    # (2) Mass-based BH: bh_factor(mass, θ, σ) = tanh((mass - θ) / σ),
    #     clamped to 0 below θ - 2σ. Heavy nodes become attractors
    #     gradually; no source-class branching (single rule for all).
    # θ/σ default to a placeholder; tuned after 1-2 weeks of observation
    # under the new rule (Phase M Stage 2). Set mass_bh_enabled=False to
    # disable the new attractor (force=0 from the mass-BH term).
    mass_conservation_enabled: bool = True
    mass_bh_enabled: bool = True
    mass_bh_theta: float = 5.0             # mass threshold for BH attractor onset
    mass_bh_sigma: float = 1.5             # tanh transition width

    # Orbital mechanics — velocity-based physics
    orbital_friction: float = 0.05          # Constant velocity friction per step
    orbital_friction_age_factor: float = 0.1  # Additional friction for unaccessed nodes
    orbital_max_velocity: float = 0.05      # Max L2 norm of velocity vector
    orbital_anchor_strength: float = 0.02   # Restoring force toward original position (Hooke's law)

    # ---- Phase Q2: anchor-referenced neighbour-gravity governor --------------
    # Density-adaptive cap on the *attractive* neighbour force (neighbour gravity
    # + mass-BH) so it stays a perturbation rather than dominating the anchor.
    # Phase Q rollout + Phase Q2 pass-5 (2026-05-30, isolated copy of the 41K
    # field): in RURI's narrow high-cosine space the neighbour-gravity vectors
    # sum *coherently* (coherence ~0.8-1.0, net ∝ N), so the net pull runs
    # ~10⁴-10⁵× the anchor restoring force (~0.005) and has a heavy tail
    # (ratio p90 ~10⁵). A single small global gravity_G cannot tame that tail,
    # so we cap per node: scale the attractive force by
    #   g_i = min(1, α · k_eff(m_i) · max(|d_i|, d_floor) / |acc_neigh|)
    # which holds neighbour gravity at ≤ α × the anchor force scale, adapting to
    # each node's local density (dense → smaller g_i; sparse → g_i=1, untouched).
    # The direction is preserved; anchor / query-attraction / Λ are NOT capped.
    # Source-blind (mass + geometry only). Set False for bit-exact pre-Q2 legacy.
    # Default ON since 2026-05-31 — promoted from opt-in after the Phase Q2
    # rollout (段階4 acceptance: per-query ranking-neutral; production live +
    # healthy, velocity field bounded). See Plans-Phase-Q2-Gravitational-Scale.md §4.
    gravity_neighbor_governor_enabled: bool = True
    gravity_neighbor_governor_alpha: float = 0.2    # target net ≈ α × anchor force
    gravity_neighbor_governor_disp_floor: float = 0.1  # |d| floor so d≈0 isn't fully suppressed

    # ---- Phase Q: Orbital Mechanics (rosette orbits around own anchor) -------
    # The Hooke anchor (orbital_anchor_strength) is F = -k·d toward a node's
    # own original embedding x₀. By Bertrand's theorem this isotropic-harmonic
    # central force already produces CLOSED orbits — what was missing is
    # angular momentum. Phase Q seeds a *tangential* velocity component at
    # genesis / supernova so a node, instead of falling straight in and
    # oscillating on a line through x₀, traces a closed ellipse AROUND x₀
    # (a rosette once neighbor gravity perturbs it). The orbit centre stays
    # the node's own anchor → zero anchor migration (Phase M single rule).
    # See docs/wiki/Plans-Phase-Q-Orbital-Mechanics.md.
    #
    # ``orbital_tangential_alpha`` = magnitude of the seeded tangential
    # velocity as a fraction of the radial (gravity-induced) kick. The seed
    # makes displacement (∥ gravity) and velocity (gravity + tangential)
    # non-collinear, so angular momentum L = d × v ≠ 0 and an ellipse forms.
    # ``0.0`` = legacy radial-only kick (bit-exact rollback, L=0 line
    # oscillation). ``~1.0`` ≈ near-circular; smaller → more eccentric.
    orbital_tangential_alpha: float = 0.0
    # Integration scheme for ``update_orbital_state``. ``"euler"`` = the
    # legacy semi-implicit (symplectic) Euler (``v+=a; x+=v``), bit-exact to
    # pre-Phase-Q. ``"verlet"`` = velocity-Verlet (two force passes per step,
    # O(dt²) and time-reversible) which removes the O(ω·dt) artificial
    # precession Euler adds, leaving only the physical (neighbor-perturbation)
    # precession of the rosette. Verlet doubles per-step force cost; default
    # euler keeps production untouched.
    orbital_integrator: str = "euler"

    # ---- Phase Q Stage 2: continuous orbital tick ---------------------------
    # When enabled, the dream loop runs a lightweight ``_orbital_tick`` each
    # cycle that advances the orbital state (displacement + velocity) of the
    # *lively* nodes — those with ``|v| > orbital_lively_v_min`` — WITHOUT a
    # recall (no FAISS query, no mass/temperature/last_access/co-occurrence
    # update). This is what makes the cosmos move on its own clock instead of
    # only when a node is recalled: recall = energy injection, tick = free
    # orbital integration. The lively set is self-limiting — constant friction
    # (orbital_friction) bleeds energy so a node drops below v_min ~100 ticks
    # after its last kick — which is exactly what bounds the per-tick cost.
    # Age-based friction is suppressed inside the tick (it keys on last_access,
    # which is stale for an orbiting-but-not-recalled node and would otherwise
    # kill the orbit in a few ticks); only constant friction applies. Default
    # OFF. See docs/wiki/Plans-Phase-Q-Orbital-Mechanics.md §3.3.
    orbital_tick_enabled: bool = False
    orbital_lively_v_min: float = 0.001     # |v| below this → node is "cold", skipped
    orbital_tick_max_nodes: int = 256       # per-tick cap (cost bound); excess deferred + logged
    # Whether the continuous tick applies *mutual neighbor gravity* among the
    # lively set. Default OFF → the tick runs a pure self-anchor Hooke orbit
    # (each node orbits its own embedding x₀; β still applies). Rollout finding
    # (2026-05-30, isolated copy of the 41K production field): the implemented
    # tick passes the lively set itself as the N-body neighbours (NOT each
    # node's true FAISS neighbours, cf. plan §3.3). In RURI's narrow high-cosine
    # space those neighbour-gravity vectors point alike and sum *coherently* —
    # measured net |a| ≈ 10 (p50) … 640 (max) at gravity_G=0.01 vs the anchor's
    # ~0.005 — so neighbour gravity ceases to be the "mild precession
    # perturbation" the plan assumed and instead dominates ~1000×, slamming
    # displacement onto the max_displacement_norm clamp and killing the friction
    # self-limiting. Pure self-anchor (this flag OFF) was measured bounded and
    # self-limiting (displacement relaxes, lively set drains). Set True only
    # with a tamed neighbour scheme (true per-node FAISS neighbours or a much
    # smaller tick gravity); rosette precession is deferred future work. See
    # docs/wiki/Plans-Phase-Q-Orbital-Mechanics.md §4 (rollout findings).
    orbital_tick_neighbor_gravity_enabled: bool = False

    # Habituation & thermal escape
    saturation_rate: float = 0.2            # How fast nodes saturate (higher = faster)
    habituation_recovery_rate: float = 0.01 # Recovery from saturation per step
    thermal_escape_scale: float = 5000.0    # Temperature-based BH escape scaling

    # Phase O Stage 1 — Score breakdown observability.
    # When True, each QueryResultItem carries an additive/multiplicative
    # decomposition of final_score (raw_cosine, virtual_cosine, decay,
    # wave_score, mass_boost, emotion/certainty, saturation, plus informational
    # persona_proximity / bm25_contributed / forced_inclusion). default ON —
    # TTT-aware caller can read why a result scored what it scored without
    # workaround. set False for legacy clients or to shave a few bytes.
    expose_score_breakdown: bool = True

    # Observation Apparatus Refinement Stage 1 — reason line.
    # When True, ScoreBreakdown.reason carries a 1-line human-readable
    # explanation of which factors dominated this node's score
    # (e.g. "high mass persona proximity — possible dominance artifact",
    # "bm25 strong lexical match", "lensing pick", "dormant surface").
    # Force computation / mass update / acceleration are NOT touched —
    # this is pure observation layer (Phase M single-rule preserved).
    # Set False to disable the string generation (saves a few µs per result).
    expose_reason: bool = True
    # Mass threshold above which a node's mass is flagged as "dominance candidate"
    # in the reason line. Calibrated against production observation
    # (harakiriworks intention mass=2.82 was the canonical dominance case).
    reason_dominance_mass_threshold: float = 2.0
    # BM25 score threshold above which the reason line labels the surface
    # as "strong lexical match". RRF-normalized scores typically sit in
    # 0.01-0.05 range; raw BM25 in 0-10+ range — this threshold expects raw.
    reason_bm25_strong_threshold: float = 0.5

    # Observation Apparatus Refinement Stage 2 — dormant whisper slot in
    # ambient_recall. Mixes a counter-importance-sampled dormant memo into
    # the ambient block when its BM25 score against the query clears
    # ``ambient_dormant_relevance_floor``. Force computation untouched —
    # this is a surface-candidate-set extension, not a physics modifier.
    # Set False to suppress the slot entirely (legacy ambient block).
    ambient_dormant_slot_enabled: bool = True
    # Maximum dormant memos to whisper per ambient block. 1 is the calibrated
    # default — more risks crowding out direct / lensing.
    ambient_dormant_slot_count: int = 1
    # BM25 floor a dormant candidate must clear to qualify for the slot.
    # Below this we leave the slot empty (no random hit — silence beats
    # off-topic noise). NOTE: this is the floor for the *intersection* of
    # (BM25 top-200) ∩ (dormant set), evaluated AFTER the upstream
    # ``ambient_bm25_min_score`` (corpus-calibrated at ~32) has already
    # approved the injection. So this floor can be looser than the upstream
    # gate without producing noise — the candidate pool is already narrow.
    # In production, tune in tandem with ``ambient_bm25_min_score`` (a
    # 1:50-ish ratio is typical when the upstream gate is 32 — see
    # docs/wiki/Operations-Tuning.md). Calibration is measurement-first.
    ambient_dormant_relevance_floor: float = 0.5

    # Observation Apparatus Refinement Stage 4 — source-aware connections
    # display. When True, ``reflect(aspect="connections")`` groups co-occurrence
    # edges into persona / agent_user / ingest buckets so file-ingest
    # artifacts (chunks of one file co-occurring among themselves) stop
    # crowding out the rare cross-domain associations a reader actually wants
    # to see. The grouping is **display layer only** — edge weight and
    # co-occurrence count are unchanged. Set False for legacy flat output.
    connections_grouped_by_source: bool = True

    # Phase O Stage 2 — Training delta trailer (TTT update visibility).
    # When True, recall/explore responses carry a ``training_delta`` field
    # exposing displacement/mass changes induced by this recall + wave reach
    # counts. ``training_delta_topk_only`` (default True) limits the per-node
    # delta dicts to the top-K returned nodes for context economy; set False
    # for full reached-node coverage (debug mode). Cache hits emit a
    # ``cache_hit=True`` delta with empty dicts (no simulation ran).
    training_delta_enabled: bool = True
    training_delta_topk_only: bool = True

    # Phase O Stage 4 — List mode (recall(mode='list')) excerpt size.
    # When ``mode='list'`` is requested on recall, ``services.memory.recall``
    # truncates each result's content to this many chars and replaces newlines
    # with spaces. Default 80 — fits one terminal line, ~20× smaller than a
    # typical agent memo. caller-side opt-in (default mode is 'detail').
    list_mode_excerpt_chars: int = 80

    # Ambient Recall Enrichment — structured passive-recall injection.
    # ``services.memory.ambient_recall`` composes a multi-slot block out of one
    # passive recall: direct hits + a gravitational-lensing pick + provenance
    # metadata (+ Stage 2/3 reasoning/tension/persona). See
    # docs/wiki/Plans-Ambient-Recall-Enrichment.md.
    # Relevance gate. A dedicated word-level (Sudachi) BM25 index over the
    # corpus answers "does the prompt strongly match stored content" — the
    # strong-match gate. 2026-05-21 calibration over 4 rounds: dense-cosine
    # virtual_score cannot separate (on/off-topic both ~0.6, drowned in
    # temperature noise); char-3gram BM25 cannot either (common Japanese
    # morphology accumulates — a long off-topic prompt outscores a terse
    # on-topic one). Sudachi *word* tokens bound off-topic (a rare word like
    # "卵焼き" is one absent/near-absent unit, not smeared into common
    # 3-grams), giving a clean high-precision gate: strong topical matches
    # clear ~34+, everything else (incl. off-topic) stays ≤~29.
    ambient_gate_use_bm25: bool = True       # gate mode: word-BM25 (True) | virtual_score (False)
    ambient_gate_tokenizer: str = "sudachi"  # gate index tokenizer; needs the bm25-sudachi extra
    ambient_bm25_min_score: float = 32.0     # word-BM25 strong-match threshold (corpus-calibrated)
    ambient_min_score: float = 0.70          # fallback virtual_score gate threshold (gate index unavailable)
    ambient_excerpt_chars: int = 240         # per-slot content excerpt length
    ambient_lensing_enabled: bool = True     # ② gravitational-lensing slot on/off
    ambient_lensing_min_score: float = 0.5   # lensing pick noise floor (virtual_cosine)
    ambient_lensing_min_gap: float = 0.05    # lensing pick must clear this virtual−raw gap
    # Lateral Association Stage 3 (2026-05-25,
    # Plans-Ambient-Recall-Lateral-Association.md) — lensing slot is the
    # mechanism closest to "〇〇といえば〜だったよな" (the field-learned
    # association). Top-1 only is a "one lateral hit per turn" cap that
    # contradicts natural human associative chains ("X といえば Y で、Y と
    # いえば Z だから..."). Allow top-K lensing picks (ranked by decayed gap
    # desc; same exclude-set against direct as before), giving multiple
    # lateral channels per turn.
    #   max_k=1 → Stage 1/2 behaviour exactly (single lensing memo).
    #   max_k=2 (default, controlled increase) → +1 ambient row, ~+30% block.
    #   max_k=3 → maximum recommended (token-budget concern); each row is
    #             still bounded by ``ambient_excerpt_chars`` (240 chars).
    # Each kept pick must independently clear ``ambient_lensing_min_score``
    # and ``ambient_lensing_min_gap`` (no quota relaxation — second-best is
    # only surfaced if it's still genuinely a "bent" association).
    ambient_lensing_max_k: int = 2
    # Stage 3 dynamic-K mode (opt-in). When True, K floats in
    # [1, ambient_lensing_max_k] driven by query abstraction (= the spread
    # of pure raw_cosine across reached nodes). Reserved for a follow-up
    # tuning step — Stage 3 ships the static knob first to keep behaviour
    # predictable while measuring lateral hit rate (Stage 6a corpus).
    ambient_lensing_dynamic_k: bool = False
    # Lateral Association Stage 5 (2026-05-25,
    # Plans-Ambient-Recall-Lateral-Association.md) — lensing resonance signal.
    # ``gap`` measures the strength of the bend (virtual − raw), but not
    # whether the bent association is "trustworthy" or "noise". Stage 3's
    # top-K extension increases the risk of false-positive lateral picks —
    # so Stage 5 adds a per-pick ``resonance`` score derived from the
    # cooccurrence graph (mode 5a in the plan): for each lensing pick,
    # ``resonance = raw / (raw + scale)`` where
    # ``raw = sum_{d in direct} cache.get_neighbors(lensing)[d]``. Saturating
    # non-linearity bounds resonance to ``[0, 1)`` regardless of raw count
    # scale. The semantic: "how often has the field pulled this lensing
    # memo together with today's direct hits in past *active* recalls" — a
    # field-learned trust signal, not a per-call topical-match signal.
    #
    # Why 5a (cooccurrence) over 5b (mass × cos) or 5c (cos to direct):
    #   5b conflates "important memo" with "appropriate for this turn"
    #     (the Heavy Persona Dominance failure mode, re-introduced for
    #     lensing — see [[project-ambient-persona-mass-dominance]]).
    #   5c uses raw embedding cosine to direct, but lensing is *defined*
    #     as "embedding-far from query but bent close" — cos(lensing,
    #     direct) is not a clean trust signal because both interpretations
    #     ("near-miss of direct, not really lateral" and "genuinely
    #     cross-topic via field bend") map to the same number.
    #   5a directly measures "the field has learned to associate these"
    #     which is what lensing is *supposed* to surface.
    #
    # ``scale=10.0`` → resonance hits 0.5 at raw count 10 (10 prior
    # co-recalls), 0.9 at raw=90. Saturating, never reaches 1.0.
    ambient_lensing_resonance_scale: float = 10.0
    # Optional drop gate. When > 0, lensing picks with resonance below
    # this threshold are dropped from the slot (no backfill — same
    # principle as Stage 3 no-quota-relaxation). Default 0.0 = no
    # filtering, just surface the resonance signal for the agent to weigh.
    # Production tuning should observe natural resonance distribution
    # before raising; typical values ~0.1-0.3 to drop pure noise picks.
    ambient_lensing_resonance_min: float = 0.0
    ambient_reasoning_enabled: bool = True   # ④ derived_from/supersedes "because" chain
    ambient_tension_enabled: bool = True     # ⑤ contradicts caution pairs
    ambient_persona_enabled: bool = True     # ⑥ active declared value/intention line
    # Refinement Stage 1 (Plans-Ambient-Recall-Refinement.md): query-conditioned
    # persona pick. The top-N candidates (by mass) are re-ranked by
    # ``(mass ** w) × cosine(query, persona_vec)`` (default ``w = 0.3``,
    # see follow-up (b) below); the best is surfaced only when its cosine
    # clears ``ambient_persona_min_relevance``. Below the threshold the slot
    # is silently omitted — irrelevant persona is worse context than no
    # persona (Phase A literal failure: MCP-smoke intention in embedder turn).
    ambient_persona_pool_size: int = 10      # mass-top-N pool size for cosine re-rank
    ambient_persona_min_relevance: float = 0.65  # cosine floor for surfacing the slot
    # Refinement follow-up (b) — Heavy Persona Dominance knob. Production
    # observation 2026-05-25: when one persona has runaway mass (e.g.
    # ``harakiriworks intention mass=2.82`` vs others at ~1.0), the
    # ``mass × cos`` formula's mass term dominates and the persona slot
    # surfaces the same node for every query. Knob to dampen mass:
    # ``score = (mass ** ambient_persona_mass_weight) × cos``.
    #   weight = 0.0 — pure cos ranking (mass ignored), the
    #                 ``relevance_dominant`` mode as a degenerate case
    #   weight = 0.3 (default, promoted 2026-07-02) — mass^0.3 × cos, dampens
    #                 a runaway-mass persona so cosine can still decide
    #   weight = 0.5 — sqrt(mass) × cos, log-scale-ish dampening
    #   weight = 1.0 — Stage 1 bit-identical, full mass attribution (rollback path;
    #                 the original default before the production promotion)
    # The 2026-07-02 promotion moved both ``min_relevance`` (0.5 → 0.65) and
    # this knob (1.0 → 0.3) to code defaults at once: env-set w=0.3 alone
    # still let a mass-inflated cohort through because dense Japanese
    # embeddings give cos ≈ 0.52 to a query that merely shares the keyword,
    # clearing the old 0.5 floor. The two knobs are complementary, not
    # redundant. Tune with ``test_tier3_ambient_quality.py`` before/after
    # baseline to separate "performance improvement" from "feature preference".
    ambient_persona_mass_weight: float = 0.3

    # Lateral Association Stage 1 sub-step 1 (2026-05-25,
    # Plans-Ambient-Recall-Lateral-Association.md) — session-aware novelty
    # decay. When the caller (the UserPromptSubmit hook) passes
    # ``recently_surfaced: dict[node_id, count]`` to ``ambient_recall``,
    # candidates appearing in that map have their ranking score multiplied by
    # ``ambient_novelty_decay ** count`` *before* the slot pick:
    #   persona: ``(mass ** w) × cos × novelty``
    #   direct:  ``final_score × novelty`` (re-sort, then take direct_k)
    #   lensing: ``gap × novelty``         (gap-largest pick uses decayed gap)
    # ``decay = 1.0`` (no-op) preserves the pre-Stage-1 behavior. ``decay = 0.7``
    # (default) drops a same-id re-surface to 70% on the first repeat, 49% on
    # the second — pressure to rotate without outright suppression. The node's
    # mass / displacement are not touched (passive principle): only the
    # session-scope ranking is bent. See
    # docs/wiki/Plans-Ambient-Recall-Lateral-Association.md Stage 1.
    ambient_novelty_decay: float = 0.7

    # Instruction Surface Hygiene Stage 2a (C2) — per-slot content budget for
    # the MCP formatter's rendered ambient block. The service layer already
    # truncates at ``ambient_excerpt_chars`` (240) when building AmbientMemory,
    # but even 240 chars per slot × 4 slots = ~960 chars of ambient noise per
    # turn. These knobs let the formatter trim further without touching the
    # Pydantic model (REST output stays full). 0 = unlimited (legacy).
    ambient_direct_max_chars: int = 0       # 0 = unlimited (backward-compatible default)
    ambient_lensing_max_chars: int = 300    # lensing / dormant (ささやき) slot budget

    # Instruction Surface Hygiene Stage 2b (C3) — recall trailer suppression.
    # The breakdown line and 訓練差分 trailer are only emitted when the
    # effective verbosity matches one of these modes. Stored as a comma-
    # separated string so the generic env-override loop picks it up; callers
    # split on "," to get the set. Default "detail,full" = no change from
    # pre-Stage-2 behaviour (Phase O observability preserved in default path).
    recall_trailer_verbose_modes: str = "detail,full"

    # Observation Apparatus Round 2 (Stage B) — reason line display modes.
    # reason line は breakdown/trailer と独立に表示 mode を制御。
    # default 全 mode = triage 中でも dominance artifact 警告が見える。
    recall_reason_line_modes: str = "detail,full,compact,ids,list"

    # Lateral Association Stage 7.1 (2026-05-26) — direct-hit anti-hub.
    # Greedy MMR-style penalty on top-k composition: for each subsequent slot,
    # candidate ``final_score`` is reduced by
    # ``direct_hit_anti_hub_lambda × count_of_shared_cluster_in_already_selected``.
    # Cluster identity = ``cohort_id`` OR ``original_id`` (see
    # ``services.memory._cluster_key_for``). Both are Phase M structural
    # identifiers (no source / tag branching). Fallback to ``original_id``
    # added after production dogfooding (2026-05-26) found ``cohort_id``
    # coverage = 0% in 26k corpus (one-at-a-time ``remember()`` calls give
    # batch=1, supernova doesn't fire), while ``original_id`` covers 57.8%
    # of active memos in multi-member clusters (largest = 638-chunk book).
    # ``cluster_key is None`` (no cohort + no original_id, pre-Phase-M
    # memos) gets no penalty — intrinsically diverse.
    # Default ``0.4`` — promoted from OFF to provisional active 2026-05-26
    # after Stage 7.1 acceptance: internal test corpus avg_unique_cohorts
    # 2.67→4.00, avg_max_dominance 2.33→2.00, target_hit_rate 3/3;
    # production GLM acceptance literal verified 米国会社四季報 (638-chunk
    # book) capped to 1/5 of top-5. ``0.0`` for full rollback. Applied to:
    #   ambient_recall.direct slot  — before the ``items[:direct_k]`` slice
    #   recall top-k composition    — engine returns a larger pool, then MMR
    # See docs/wiki/Plans-Ambient-Recall-Lateral-Association.md (Stage 7).
    direct_hit_anti_hub_lambda: float = 0.4

    # Observation Apparatus Round 2 — E1: conversational source damping
    # (Plans-Observation-Apparatus-Round-2.md Stage E1). Multiplies the
    # ambient-slot ranking score of items whose source matches one of
    # ``ambient_conversational_sources`` by this factor, before the slot-pick
    # slice. Applied to direct and lensing slots only (persona slot is not
    # affected — conversational sources are not declared values/intentions).
    # 観測・注入層のみの lens — recall/force/mass には不適用。
    # Phase M 単一規則は不変 (precedent: exclude_tags / novelty decay /
    # persona min relevance). ``1.0`` = OFF (bit-exact legacy).
    # Default ``0.5`` baked in 2026-06-12 after a dogfooding measurement round
    # (id=fa1d03a8) confirmed conversational sources were dominating ambient
    # slots; set to ``1.0`` to restore legacy behaviour.
    ambient_conversational_source_factor: float = 0.5
    ambient_conversational_sources: tuple[str, ...] = (
        "openai", "claude-web", "claude-code",
    )

    # Observation Apparatus Round 2 — E2: dump-shape gate.
    # When set, the first N characters of each ambient candidate's content are
    # checked for the ratio of "symbol" characters (non-Japanese, non-ASCII-
    # letter, non-whitespace). Items exceeding the ratio are dropped from both
    # direct and lensing ambient slots (next-best naturally fills). Targets
    # state-dict key dumps, raw code fragments, and similar low-S/N ingest
    # artifacts. ``>= 1.0`` = OFF (bit-exact legacy; same convention as E1 —
    # a float default keeps the field GAOTTT_* env-settable, which a
    # ``None`` default would not be).
    # Default ``0.45`` baked in 2026-06-12 (id=fa1d03a8): calibrated against
    # production dumps — true dumps (state-dict / base64 / hex) score 0.97-1.0,
    # readable code ≤0.40, prose <0.05, so 0.45 sits in a clean gap. Set to
    # ``1.0`` to restore legacy behaviour.
    ambient_dump_symbol_ratio: float = 0.45

    # Phase O Stage 5 — Dormant surface (explore(mode='dormant')).
    # ``explore(mode='dormant')`` returns random self-authored memos that have
    # been quietly forgotten — older than ``dormant_age_threshold_seconds``
    # since last_access, mass ≤ ``dormant_mass_threshold`` (raw cosine alone
    # cannot pull them back), and source ∈ ``dormant_source_classes``. The
    # source list is a **structural identifier** for "memories I authored"
    # (Phase D persona / agent / note classes) — it is a *filter* on caller
    # intent, not a gate on physics rule, so Phase M's source-branching-zero
    # principle stays intact (see Plans-Phase-O §Stage 5 "設計判断").
    dormant_age_threshold_seconds: float = 7 * 86400.0  # 7 days
    dormant_mass_threshold: float = 2.0                  # mature gate point — below means "the field didn't claim it"
    # Lateral Association Stage 7.2 (2026-05-26) — distribution-relative
    # dormant mass cut. When set (e.g. ``10.0``), the actual cut becomes the
    # ``p`` percentile of active-corpus mass instead of the fixed
    # ``dormant_mass_threshold``. Production observation showed the absolute
    # 2.0 cut returns 0 candidates on a 26k-memo corpus because the mass
    # distribution has shifted upward — see
    # ``project_phase_o_stage_5_production_observation``. Default ``10.0`` —
    # promoted from None to provisional active 2026-05-26 after production
    # acceptance: ``diag_dormant.py`` with age=7d showed p10 yields 23
    # candidates (vs absolute 2.0 floods at 89.6%). ``None`` for legacy
    # absolute-threshold rollback. **Note**: percentile needs a matching age
    # gate — the percentile cut surfaces 0 dormant nodes if
    # ``dormant_age_threshold_seconds`` excludes every node. This is why the
    # age default was lowered 30d → 7d on 2026-06-01: the proxy-mode shared
    # backend does not reliably inherit the per-frontend opt-in env (a Codex
    # shim spawned it with no GAOTTT_* set; see
    # ``project_proxy_backend_env_not_delivered``), so the intended 7d env
    # override never reached the engine and dormant returned 0 on a 41k corpus
    # (GLM acceptance handover-2026-06-01 §4.3). Promoting to a code default
    # makes it env-independent, mirroring ``direct_hit_anti_hub_lambda`` and
    # ``dormant_mass_percentile``. Physics-invariant: dormant surfacing is an
    # observation-layer filter, not a force/mass rule.
    dormant_mass_percentile: float | None = 10.0
    # 2026-06-12 dogfooding で exploration-report (self-authored 化石レポート層)
    # と compaction (context compression 層) が dormant 経路から構造的に出ない
    # ことが判明。dormant surfacing is an observation-layer filter, not a
    # force/mass rule なので追加は physics 不変。
    dormant_source_classes: tuple[str, ...] = (
        "agent", "value", "intention", "commitment", "note", "reference",
        "exploration-report", "compaction",
    )

    # Lateral Association Stage 8 (2026-06-02) — degree-normalized
    # co-occurrence association strength (graph-layer anti-hub). The raw
    # ``cache.get_neighbors`` weight is a co-recall *count*; promiscuous
    # hubs (nodes that surfaced in many recall sessions, e.g. a bulk
    # self-knowledge recording batch) carry high weight to *every* anchor
    # regardless of semantic relevance, so any consumer that ranks/pulls by
    # raw weight amplifies hubs (measured 2026-06-02 via
    # ``scripts/diag_assoc_halo.py``: one w=16 node surfaced as a top novel-
    # far halo member across unrelated queries). This knob transforms the
    # raw weight into an association strength that discounts promiscuity:
    #   "none"   — raw co-recall count (legacy, bit-exact default)
    #   "cosine" — ``w(a,b) / sqrt(deg(a)·deg(b))``  (co-occurrence cosine)
    #   "pmi"    — ``max(0, log(w·W / (deg(a)·deg(b))))`` (positive PMI)
    # where ``deg(x) = Σ_n w(x,n)`` and ``W`` = total unique-edge weight.
    # A high-degree hub has its association to everyone divided down; a node
    # that co-occurs only with ``a`` (rare, specific) keeps a high score —
    # exactly the "〇〇といえば〜" signal Lateral Association is after.
    # Consumed by Stage 5 lensing resonance now; the future Accretion Recall
    # gather layer reuses it (Plans-Accretion-Recall.md P2). Touches no
    # physics (force/mass): a transform on an observation/ranking signal.
    # Default "none" keeps every consumer bit-exact.
    cooccurrence_assoc_normalization: str = "none"
    # Optional explicit hub cut: drop neighbors whose degree exceeds this
    # percentile of the active-corpus degree distribution before returning
    # association strengths (degree-axis sibling of Stage 7.2's mass
    # percentile cut). ``None`` = no cut. JSON-only (``| None`` default is
    # not env-coercible); pair with the normalization knob above.
    cooccurrence_hub_degree_percentile_cut: float | None = None

    # Synaptic Pruning (2026-06-02, default OFF) — co-occurrence edge decay.
    # The edge-layer twin of Phase N mass evaporation: an association weight is
    # a leaky integrator that decays by half-life since its last reinforcement
    # (co-recall). A one-off bulk-recording session-clique is reinforced once
    # then never again → it fades; an organic association is re-traversed
    # repeatedly → it persists. This thins the bulk-content "degree wall" that
    # capped Stage 8 (Plans-Synaptic-Pruning.md, the P3 lever). Biology: LTD /
    # synaptic pruning, the twin of "fire together wire together". Physics: a
    # filament's radioactive half-life. Touches no physics (force/mass) —
    # co-occurrence weight feeds only the observation/ranking layer (Stage 5
    # resonance, reflect, future Accretion). Lazy mode: weights are decayed at
    # read time (get_association_strength / degree); the graph is not mutated,
    # so off ⇒ bit-exact and rollback is a no-op. (Eager pruning — actually
    # removing faded edges — is a future ops step, Fork A.) The decay clock is
    # the edge's last_update, which the store already records as the last-
    # reinforcement time, so decay applies retroactively to stale cliques.
    synaptic_pruning_enabled: bool = False
    synaptic_pruning_half_life_seconds: float = 30 * 86400.0  # 30 days

    # Phase O Stage 3 — Query routing (recall + reflect auto-merge).
    # When True, ``recall`` / ``explore`` heuristically classify the query
    # surface form (e.g. "現在 active な commitment", "持っている value") and run
    # the corresponding ``reflect`` aspect in parallel, attaching the summary to
    # ``routing_hint``. caller can ask free-form questions about declared
    # persona / task state without remembering to switch tools. Pattern-based
    # (query syntax) — *not* source-based — so it stays compatible with Phase M
    # "source 分岐ゼロの単一規則". Disable to suppress for all calls; per-call
    # opt-out via ``auto_route=False`` on the recall request.
    auto_route_enabled: bool = True

    # Phase N candidate β — Mass Evaporation (Hawking radiation 類比、出力側).
    # Single rule: ``mass -= ε · max(mass - floor, 0)^β · (t_idle / τ_idle)^γ``
    # applied when ``mass > floor AND t_idle > τ_grace``. Source-branching-zero
    # (Phase M 単一規則と整合): same formula for every node, only structural
    # identifiers (mass, last_access) matter. Floor-protected (新規ノードは
    # 永久不変)、grace-protected (recent recall は即時 decay されない).
    #
    # D2=C hybrid evaluation:
    #   - lazy: applied inside ``_update_simulation`` at touch time (no extra I/O).
    #   - startup sweep: ``engine.startup`` walks all active nodes once when
    #     enabled — settles "cold-start mass debt" from any offline period.
    #
    # Default OFF — Stage 1 merges with no observable behaviour change. Enable
    # via opt-in PR (Stage 1.5) after Phase M Stage 2 (θ confirmation) lands.
    # Stage 2 will add ``training_delta.evaporation_changes`` visibility +
    # optional eager cron via ``mass_evaporation_eager_cron_seconds``.
    mass_evaporation_enabled: bool = False
    mass_evaporation_floor: float = 1.0                          # M_floor — initial mass、below this no decay
    mass_evaporation_grace_seconds: float = 7 * 86400.0          # τ_grace (7d) — recall 直後の即時 decay 抑止
    mass_evaporation_idle_normalize_seconds: float = 30 * 86400.0  # τ_idle (30d) — t_idle/τ_idle 比の正規化
    mass_evaporation_rate: float = 0.01                          # ε — fraction-of-excess^β eroded per τ_idle period
    mass_evaporation_mass_exponent: float = 1.5                  # β — mass-amplification (heavier loses more)
    mass_evaporation_time_exponent: float = 1.0                  # γ — time-amplification (linear default)
    mass_evaporation_eager_cron_seconds: float = 0.0             # Stage 2: >0 → spawn background cron sweep loop

    # Similarity history
    sim_buffer_size: int = 20  # Ring buffer size

    # Storage — resolved to fixed data directory
    data_dir: str = field(default_factory=_default_data_dir)
    db_path: str = ""
    faiss_index_path: str = ""

    # Write-behind (cache → SQLite + FAISS index → disk)
    flush_interval_seconds: float = 5.0
    flush_threshold: int = 100
    # Periodic FAISS save: if non-zero, the engine spawns a background task
    # that calls faiss_index.save() every N seconds when dirty. Critical for
    # multi-process visibility — without this, brand-new `remember` lives
    # only in the writing process's in-memory FAISS until shutdown(), so
    # other processes' recall() never sees it. Set to 0 to disable.
    faiss_save_interval_seconds: float = 5.0
    # Upper bound (seconds) on the final synchronous FAISS save during
    # engine.shutdown(). The save runs in a worker thread (asyncio.to_thread);
    # without a bound, a wedged write (executor saturation or FaissIndex-lock
    # contention with a cancelled-but-still-running periodic save) hung
    # shutdown indefinitely. On timeout the save is skipped and logged — the
    # startup diagnostic rebuilds from the store, so durability is preserved.
    # 30s is ample for a real write_index + fsync; raise only for very large
    # indexes on slow storage.
    faiss_final_save_timeout_seconds: float = 30.0

    # F4: TTL for ephemeral memory (source="hypothesis")
    default_hypothesis_ttl_seconds: float = 7 * 86400.0  # 7 days

    # F1: auto_remember heuristics
    auto_remember_default_max: int = 5
    auto_remember_min_chars: int = 12
    auto_remember_max_chars: int = 400
    # Strip ``<gaottt-…>…</gaottt-…>`` blocks (save-candidates, ambient-recall)
    # from the transcript before heuristic extraction. Default ON closes the
    # meta-extraction loop where save_candidates would re-extract its own prior
    # block content — including the save-policy filter line whose literal
    # "bug fix" keyword self-triggers _OUTCOME_KEYWORDS. Env opt-out via
    # GAOTTT_AUTO_REMEMBER_STRIP_GAOTTT_BLOCKS=0 for safety-net rollback.
    auto_remember_strip_gaottt_blocks: bool = True

    # F7: emotional weight & certainty
    emotion_alpha: float = 0.04                       # Score weight per |emotion|
    certainty_alpha: float = 0.02                     # Score weight per certainty
    certainty_half_life_seconds: float = 30 * 86400.0 # 30 days half-life

    # F6: background prefetch
    prefetch_cache_size: int = 64                     # Max cached query results
    prefetch_ttl_seconds: float = 90.0                # Cache entry lifetime
    prefetch_max_concurrent: int = 4                  # Bounded async pool size

    # Source-filtered recall — seed pool expansion
    # When `source_filter` is set on `recall`, the default `wave_initial_k=3`
    # seeds from the densest cluster only, so sparse classes (agent / value /
    # intention / commitment / compaction) get squeezed out of the seed
    # pool entirely on corpus-heavy DBs (~10k+ entries). Boosting wave_k
    # for filtered queries oversamples seeds so the requested sources have
    # a real chance of being reached. Raised to 1000 (2026-05-12) after
    # production 23k-DB acceptance showed multiple queries reachable at 1000
    # that missed at 500; latency impact is negligible since source_filter
    # is only used for explicit sparse-class carve-out, not on hot paths.
    wave_k_with_filter: int = 1000

    # Phase H — Wave seed redesign (H.3 Mass-aware seed boosting):
    # FAISS raw cosine top-K seed alone misses heavy nodes that sit
    # slightly outside top cosine. We pull a wider pool, rerank by
    # `raw + α * log(1+mass)`, then take the top initial_k. With α=0
    # the behaviour is identical to legacy raw cosine top-K seeding,
    # so existing setups can opt out by setting it to 0.
    #
    # 2026-05-14 — set to 0.0 (Phase H Stage 1 disabled in seed step).
    # Diagnostic discovery: when Phase L Stage 1 hybrid retrieval is on
    # (hybrid_bm25_enabled=True, bm25_score_mode="rrf"), the `raw` value
    # passed to `_seed_boost` is an RRF score (~0.02-0.05 range), not a
    # raw cosine (0.0-1.0). The mass term `α × log(1+mass)` was tuned
    # for cosine scale, so in RRF mode mass dominates: e.g. heavy chunk
    # (RRF 0.018, mass 22) boost = 0.018+0.02·log(23) = 0.080, vs
    # book chunk (RRF 0.033, mass 1.4) boost = 0.033+0.02·log(2.4) =
    # 0.055 — semantically wrong chunk wins. Disabling the mass term
    # restores RRF as the seed-ranking signal (RRF already combines
    # raw cosine + virtual cosine + BM25 in a scale-invariant way).
    # The Phase H Stage 1 intent (heavy-node lift in seed) needs a
    # proper rescaling for RRF mode — tracked as Phase N tuning target.
    wave_seed_mass_alpha: float = 0.0
    wave_seed_pool_size: int = 50

    # Phase H Stage 3 — Density-aware dynamic wave_k:
    # Look at top_N raw cosine scores and decide if the query landed in
    # a "dense" cluster (top scores all close together) or a "sparse"
    # region (top-1 alone, then sharp dropoff). For sparse regions we
    # expand the effective seed pool up to `wave_initial_k_max` so that
    # the wave can reach further before mass / source rerank narrows it.
    # Set wave_dynamic_k_enabled=False to fall back to fixed initial_k.
    wave_dynamic_k_enabled: bool = True
    wave_density_window: int = 10            # how many top-N to inspect
    wave_density_threshold: float = 0.95     # tail/top ratio above this = "dense"
    wave_initial_k_max: int = 50             # cap for sparse-region expansion

    # Phase H Stage 4 — Virtual FAISS:
    # A second FAISS index built on virtual_pos (= raw embedding +
    # cached displacement, normalized). Phase G priming moves
    # displacement on every active node, but raw FAISS does not see
    # those updates — wave seeding therefore can't benefit from priming.
    # Virtual FAISS does. propagate_gravity_wave unions seeds from raw
    # and virtual indexes; if a node moved closer to the query through
    # priming, it can enter the seed pool via the virtual index even
    # when its raw cosine is far. Rebuilt at startup (if disk file is
    # missing) and on compact(rebuild_faiss=True). Saved on shutdown.
    virtual_faiss_enabled: bool = True
    virtual_faiss_index_path: str = ""
    # Phase H Stage 4 (cont., 2026-05-13) — Virtual FAISS write-behind:
    # Without this, virtual FAISS only refreshes at compact(rebuild_faiss=
    # True) or startup-when-missing, so Phase I/J query attraction and
    # genesis kicks accumulate in cache.displacement without ever being
    # visible to the next recall's seed pool. The loop checks
    # cache.virtual_faiss_dirty on a fixed cadence and triggers a full
    # rebuild + disk save when dirty. Default is 60s — slower than raw
    # FAISS's 5s because rebuild is O(N) over all active nodes; tune
    # down on small DBs, up on large ones. Set to 0 to disable.
    virtual_faiss_save_interval_seconds: float = 60.0

    # ── Reverse-overwrite guard ─────────────────────────────────────
    # A process whose in-memory FAISS is far smaller than the SQLite active
    # node count is almost certainly running on a corrupt/empty index. Its
    # write-behind save loop would then overwrite a *good* on-disk index with
    # its broken one — the "reverse overwrite trap" (CLAUDE.md). When this
    # guard is enabled, every FAISS persist path (save loops + final shutdown
    # save) refuses to write whenever ``faiss.size < active *
    # faiss_persist_min_ratio`` and ``active >= faiss_persist_floor``.
    # Legitimate bulk shrink (forget/compact) evicts from cache too, so active
    # falls in lockstep and the guard never misfires. source-branch-free I/O
    # safety layer; physics untouched. Env-settable via the generic
    # GAOTTT_<FIELD> loop (GAOTTT_FAISS_PERSIST_GUARD_ENABLED=0 to disable).
    faiss_persist_guard_enabled: bool = True
    # Below this active-node count the guard is inert — a small or freshly
    # reset DB legitimately has few vectors and there is no good index to
    # protect yet.
    faiss_persist_floor: int = 100
    # Persist only if in-memory faiss.size >= active * this ratio.
    faiss_persist_min_ratio: float = 0.5
    # --- Concurrency hardening (2026-06-01) ----------------------------------
    # Handover: docs/maintainers/handover-2026-06-01-concurrent-recall-session-termination.md
    # Proxy transport serializes every upstream request on the single shared
    # streamable-http ClientSession (one in-flight at a time). Two concurrent
    # in-flight POSTs break that session → "MCP error 32600: Session
    # terminated" for every later call until reconnect. ``False`` = legacy
    # no-lock pass-through (also disables auto-reconnect, which needs the lock
    # for exclusive rebuild). Env: GAOTTT_PROXY_SERIALIZE_REQUESTS_ENABLED=0.
    proxy_serialize_requests_enabled: bool = True
    # Proxy transport rebuilds the upstream session and retries once when it
    # reports terminated/closed (also self-heals backend death / idle-watchdog
    # shutdown / cold-start), instead of erroring every call until the client
    # manually reconnects. Only effective when serialization is on (rebuild
    # must hold the lock). Env: GAOTTT_PROXY_AUTO_RECONNECT_ENABLED=0.
    proxy_auto_reconnect_enabled: bool = True
    # FaissIndex guards add/save/search/reset/get_vectors with a threading.Lock
    # so the background ``to_thread`` save (a real worker thread) cannot race a
    # synchronous ``add()`` on the event loop — an asyncio lock cannot cover a
    # cross-thread race. Cost is a µs-scale acquire on the recall hot path.
    # Env: GAOTTT_FAISS_INDEX_LOCK_ENABLED=0 to disable.
    faiss_index_lock_enabled: bool = True
    # Phase H Stage 5 — Wave neighbor uses virtual FAISS:
    # Previously the seed pool unioned raw+virtual, but the wave's per-
    # frontier `search_by_id` only queried raw FAISS. That breaks the
    # "stars attract stars" design — the star is the virtual position
    # (raw + cached displacement), not the raw embedding. With this on,
    # neighbor expansion uses `virtual_faiss_index.search_by_id` (so a
    # node's virtual position drives who it pulls in), falling back to
    # raw when virtual_faiss_index is None or empty. Set to False to
    # restore the legacy raw-only neighbor search.
    wave_neighbor_use_virtual: bool = True

    # Phase D: persona & task TTL defaults
    default_task_ttl_seconds: float = 30 * 86400.0       # 30 日 (要 revalidate / complete / abandon)
    default_commitment_ttl_seconds: float = 14 * 86400.0  # 14 日

    # Phase G — Genesis kick: brand-new nodes receive a one-step gravitational
    # interaction with their top-K heavy neighbors at index time. Without this,
    # a fresh `remember` lands with mass=1.0 and zero displacement/velocity,
    # losing recall ranking to established clusters even when semantically
    # close. The kick gives the new node initial orbital state derived from
    # the same Newtonian formula the rest of the engine uses (no special-case
    # physics — just one step of update_orbital_state's force calculation).
    genesis_kick_enabled: bool = True
    genesis_kick_neighbor_k: int = 5      # heaviest K to include in the kick
    genesis_kick_pool_size: int = 50       # FAISS top-N pool to mass-rank from
    genesis_mass_boost_alpha: float = 0.5  # initial mass boost = α * |kick force|
    # Mass boost cap per kick — raw |acc| can spike for nodes near dense
    # cluster centers (observed max ~71 on the 23k production DB before
    # capping). Without a cap a single step could push mass close to m_max
    # in one go, which violates the "gradual accretion" feel of the model.
    genesis_mass_boost_cap: float = 1.0

    # Phase G — Dream consolidation (Stage 2): a background loop that picks
    # quiet (low-mass, idle) nodes and runs synthetic recalls against them
    # so they accumulate co-occurrence edges and gravity field updates over
    # time. Hippocampal-replay analog. Distinct from genesis kick — kick is
    # the single bound-to-orbit moment, dream is gradual tidal capture
    # spread across idle wall-clock.
    dream_enabled: bool = True
    # Phase M follow-up (2026-05-13): broaden the dream loop's coverage but
    # keep per-tick load small enough that foreground MCP / REST recalls
    # don't get starved. First-cut bump to 50 / 10s saturated the event
    # loop on a 24k DB (2.5s contiguous CPU burst per tick, foreground
    # MCP calls timed out), so we dial back to 10 / 30s and add an explicit
    # ``await asyncio.sleep(0)`` between candidates in ``_dream_loop`` to
    # yield to other tasks. Net: 20 source recalls/min × ~60-150 wave-
    # reached → ~24k full-corpus coverage in 15-30 minutes of background
    # work, ~2% sustained CPU, foreground recalls stay responsive.
    # Set interval=60s + batch=5 to restore Phase L Stage 1 cadence.
    dream_interval_seconds: float = 30.0   # tick cadence; 0 disables loop
    dream_batch_size: int = 10             # quiet nodes revisited per tick
    dream_mass_ceiling: float = 1.5        # only nodes with mass below this
    dream_min_idle_seconds: float = 300.0  # only nodes idle this long
    dream_top_k: int = 10                  # top_k for the synthetic recall

    def __post_init__(self):
        if not self.db_path:
            new_db = os.path.join(self.data_dir, "gaottt.db")
            legacy_db = os.path.join(self.data_dir, "ger_rag.db")
            if not os.path.exists(new_db) and os.path.exists(legacy_db):
                logger.warning(
                    "Using legacy ger_rag.db at %s; run scripts/migrate-from-ger-rag.sh to rename.",
                    legacy_db,
                )
                self.db_path = legacy_db
            else:
                self.db_path = new_db
        if not self.faiss_index_path:
            new_faiss = os.path.join(self.data_dir, "gaottt.faiss")
            legacy_faiss = os.path.join(self.data_dir, "ger_rag.faiss")
            if not os.path.exists(new_faiss) and os.path.exists(legacy_faiss):
                self.faiss_index_path = legacy_faiss
            else:
                self.faiss_index_path = new_faiss
        if not self.virtual_faiss_index_path:
            self.virtual_faiss_index_path = os.path.join(
                self.data_dir, "gaottt.virtual.faiss"
            )

        # M6 — friction multipliers must live in [0, 1] so the runtime
        # ``(1 - friction)`` factor in ``update_velocity`` stays in [0, 1]
        # and cannot invert / amplify velocity. ``update_velocity`` itself
        # also clamps defensively (M6), but warning at startup gives the
        # operator a fast signal that their config is suspect.
        for name in ("orbital_friction", "orbital_friction_age_factor"):
            val = getattr(self, name)
            if not (0.0 <= val <= 1.0):
                logger.warning(
                    "GaOTTTConfig.%s=%r is outside [0, 1]; runtime will "
                    "clamp the velocity-keep factor to that range "
                    "(see gravity.update_velocity M6 guard).",
                    name, val,
                )

        # Phase Q — orbit mode needs a finite displacement clamp. The
        # relaxation regime left ``max_displacement_norm`` effectively infinite
        # (1e6) because Hooke + friction settle to a natural equilibrium. But
        # with the continuous orbital tick, tangential seeding + neighbor
        # gravity 1/r² close encounters can drive a net outward drift the
        # velocity clamp does NOT stop (Stage 3 finding — displacement reached
        # |d|≈26 over 500 steps in test). The only backstop in that regime is
        # ``max_displacement_norm`` itself, so warn if it is left ~infinite
        # while the tick is on. (Warning, not error — a deliberately large cap
        # is a valid choice for someone who knows what they are doing.)
        if self.orbital_tick_enabled and self.max_displacement_norm > 100.0:
            logger.warning(
                "GaOTTTConfig: orbital_tick_enabled=True with "
                "max_displacement_norm=%r (effectively unbounded). Orbit mode "
                "can drift without a finite displacement clamp; set "
                "max_displacement_norm to a finite value (e.g. 2.0). See "
                "docs/wiki/Plans-Phase-Q-Orbital-Mechanics.md Stage 3.",
                self.max_displacement_norm,
            )

    @staticmethod
    def _coerce_env(raw: str, target: type):
        """Coerce an env-var string to a scalar field's type.

        The bool branch is deliberate: ``bool("false")`` is ``True`` in
        Python, so a naive ``target(raw)`` would make
        ``GAOTTT_DREAM_ENABLED=false`` evaluate truthy. Only explicit
        truthy tokens yield True; everything else (incl. "false", "0", "")
        is False.
        """
        if target is bool:
            return raw.strip().lower() in ("1", "true", "yes", "on")
        if target is int:
            return int(raw)
        if target is float:
            return float(raw)
        return raw  # str

    @classmethod
    def from_config_file(cls) -> "GaOTTTConfig":
        """Create GaOTTTConfig with overrides applied in precedence order.

        H5 — precedence (highest wins): ``GAOTTT_<FIELD>`` env var >
        ``config.json`` > dataclass default. The env layer lets operators
        flip a single knob (e.g. the Phase M Stage 2 θ tuning,
        ``GAOTTT_MASS_BH_THETA=6.0``) without editing the JSON file. Only
        scalar fields (bool / int / float / str) are env-settable;
        collection / factory fields (e.g. ``data_dir``, which has its own
        ``GAOTTT_DATA_DIR`` resolution) are JSON-only. An unparseable
        override is logged and ignored rather than crashing startup.
        """
        file_conf = _load_config_file()
        field_objs = {f.name: f for f in fields(cls)}
        overrides = {k: v for k, v in file_conf.items() if k in field_objs}

        for name, f in field_objs.items():
            # Only plain scalar fields with a concrete default are
            # env-settable; field(default_factory=...) (data_dir, lists)
            # is JSON / dedicated-resolver only.
            if f.default is MISSING:
                continue
            target = type(f.default)
            if target not in (bool, int, float, str):
                continue
            env_name = f"GAOTTT_{name.upper()}"
            raw = os.environ.get(env_name)
            if raw is None:
                legacy = os.environ.get(f"GER_RAG_{name.upper()}")
                if legacy is not None:
                    logger.warning(
                        "GER_RAG_%s is deprecated; use %s. "
                        "Continuing with the legacy variable.",
                        name.upper(), env_name,
                    )
                    raw = legacy
            if raw is None:
                continue
            try:
                overrides[name] = cls._coerce_env(raw, target)
            except (ValueError, TypeError):
                logger.warning(
                    "Ignoring invalid env override %s=%r (expected %s)",
                    env_name, raw, target.__name__,
                )
        return cls(**overrides)

    def compute_node_top_k(self, mass: float) -> int:
        """Compute per-node top-k based on mass."""
        k = self.wave_base_k + int(self.wave_mass_scale * math.log(1.0 + mass))
        return min(k, self.wave_max_node_k)

    def compute_effective_attenuation(self, mass: float) -> float:
        """Compute effective attenuation (high mass = slower decay = farther reach)."""
        mass_factor = math.log(1.0 + mass) / math.log(1.0 + self.m_max)
        return self.wave_attenuation * (1.0 - self.wave_mass_attenuation_factor * mass_factor)

    def compute_gravity_radius(self, mass: float) -> float:
        """Compute minimum cosine similarity for a node's gravity field.

        Derived from real gravitational physics:
          Gravitational acceleration: a = G * m / r²
          Gravity radius (where a drops below a_min): r = sqrt(G * m / a_min)
          Convert to cosine similarity: min_sim = 1 - G * m / (2 * a_min)

        Higher mass = lower threshold = wider gravitational reach.

        With G=0.01, a_min=0.1:
          mass=1   → min_sim=0.95 (dwarf — very close neighbors only)
          mass=10  → min_sim=0.50 (giant — moderate reach)
          mass=50  → min_sim=0.05 (supergiant — vast gravitational field)
        """
        r_squared = self.gravity_G * mass / self.wave_gravity_a_min
        min_sim = 1.0 - r_squared / 2.0
        return max(0.05, min(0.95, min_sim))
