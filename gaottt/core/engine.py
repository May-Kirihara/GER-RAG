from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import uuid

import numpy as np

from gaottt.config import GaOTTTConfig
from gaottt.core.clustering import Cluster, cluster_by_similarity, find_merge_candidates
from gaottt.core.collision import MergeOutcome, merge_pair, pick_survivor
from gaottt.core.gravity import (
    SEED_PARENT_ID,
    compute_gravity_kick,
    compute_virtual_position,
    evaporate_mass,
    is_self_force_by_id,
    propagate_gravity_wave,
    update_orbital_state,
)
from gaottt.core.persona_gravity import (
    collect_active_persona_ids,
    compute_persona_proximities,
)
from gaottt.core.prefetch import PrefetchCache, PrefetchPool
from gaottt.core.segmentation import segment_query
from gaottt.core.scorer import (
    compute_certainty_boost,
    compute_decay,
    compute_emotion_boost,
    compute_mass_boost,
)
from gaottt.core.types import (
    CooccurrenceEdge,
    DirectedEdge,
    NodeState,
    QueryResultItem,
    ScoreBreakdown,
)
from gaottt.embedding.base import EmbedderProtocol
from gaottt.graph.cooccurrence import CooccurrenceGraph
from gaottt.index.bm25_index import BM25Index
from gaottt.index.faiss_index import FaissIndex
from gaottt.store.cache import CacheLayer
from gaottt.store.lease import LeaseLostError, OwnerLease
from gaottt.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)


def _rrf_forced_key(
    nid: str,
    cosine_rank: dict[str, int],
    bm25_rank: dict[str, int],
    rrf_k: int,
) -> float:
    """RRF-combined rank score for forced ordering (Phase L Stage 1).

    ``cosine_rank`` / ``bm25_rank`` map node id → 1-based rank (precomputed).
    Absent ids contribute 0 for that metric.
    """
    score = 0.0
    cr = cosine_rank.get(nid)
    if cr is not None:
        score += 1.0 / (rrf_k + cr)
    br = bm25_rank.get(nid)
    if br is not None:
        score += 1.0 / (rrf_k + br)
    return score


class GaOTTTEngine:
    def __init__(
        self,
        config: GaOTTTConfig,
        embedder: EmbedderProtocol,
        faiss_index: FaissIndex,
        cache: CacheLayer,
        store: SqliteStore,
        virtual_faiss_index: FaissIndex | None = None,
        bm25_index: BM25Index | None = None,
        ambient_gate_index: BM25Index | None = None,
    ):
        self.config = config
        self.embedder = embedder
        self.faiss_index = faiss_index
        self.virtual_faiss_index = virtual_faiss_index
        # Phase L Stage 1: optional BM25 lexical index. When ``None``, the
        # engine behaves exactly as before Phase L (raw + virtual FAISS only).
        # Production should wire this up in build_engine; tests get the
        # legacy behaviour for free.
        self.bm25_index = bm25_index
        # Ambient Recall Enrichment: dedicated word-level BM25 index for the
        # relevance gate (see services.memory._bm25_gate). Separate from
        # ``bm25_index`` so the gate's tokenizer is independent of Phase L.
        self.ambient_gate_index = ambient_gate_index
        self.cache = cache
        self.store = store
        self.graph = CooccurrenceGraph(config, cache)
        self.prefetch_cache = PrefetchCache(
            max_size=config.prefetch_cache_size,
            ttl_seconds=config.prefetch_ttl_seconds,
        )
        self.prefetch_pool = PrefetchPool(
            max_concurrent=config.prefetch_max_concurrent,
        )
        # FAISS write-behind state. New vectors enter only the in-memory
        # FAISS index; without periodic save, other processes' startup()
        # would load a stale index and never see them until this process
        # called shutdown(). The background loop below saves the index on
        # a fixed cadence whenever `_faiss_dirty` is set.
        self._faiss_dirty: bool = False
        self._faiss_save_task: asyncio.Task | None = None
        self._faiss_save_stop: asyncio.Event | None = None
        # Reverse-overwrite guard. Latched True by startup diagnostics when
        # this process loaded a FAISS index severely undersized vs the SQLite
        # active-node count: the index is untrustworthy, so this process must
        # never persist it back to disk (it would clobber a good index written
        # by a healthy sibling). Stays latched for the life of the process —
        # recover by running scripts/rebuild_faiss_from_db.py with all other
        # gaottt processes stopped, then restart.
        self._faiss_persist_blocked: bool = False
        # One-shot latch so the persist-skip path logs at most once per
        # process instead of every save tick.
        self._faiss_persist_guard_warned: bool = False
        # Virtual FAISS write-behind. Same multi-process visibility
        # problem as raw FAISS but driven by cache.displacement edits
        # (Phase I/J query attraction, genesis kicks, dream loop). The
        # dirty signal is `cache.virtual_faiss_dirty`; the loop reads it,
        # rebuilds the full virtual index, saves, and clears.
        self._virtual_faiss_save_task: asyncio.Task | None = None
        self._virtual_faiss_save_stop: asyncio.Event | None = None
        # Phase G — Dream loop: revisits quiet nodes on a slow cadence with
        # synthetic recalls so co-occurrence and gravity state build up even
        # without user query (hippocampal-replay analog). Disabled when
        # dream_enabled=False or dream_interval_seconds<=0.
        self._dream_task: asyncio.Task | None = None
        self._dream_stop: asyncio.Event | None = None
        # MV2 — engine-wide persist block. Latched True when the lease
        # heartbeat detects the owner changed (another process took over).
        # Gates cache flush + FAISS save + mutating method entry (read-only
        # transition). Mirrored onto ``cache.persist_blocked`` so the
        # write-behind / virtual-FAISS loops pick it up through their
        # existing flush / safe-to-persist paths. Default OFF: when
        # ``owner_lease_enabled`` and ``manifest.managed`` are both False
        # (the standalone default) no lease is ever acquired and this latch
        # stays False forever.
        self._persist_blocked: bool = False
        self._lease: OwnerLease | None = None
        self._lease_task: asyncio.Task | None = None
        self._lease_stop: asyncio.Event | None = None
        self._lease_lost_warned: bool = False

    async def startup(self) -> None:
        # MV0 — universe manifest hard gate. Runs before any store / FAISS
        # touch: ``ensure_manifest`` auto-generates from config for existing
        # DBs (backward-compat), then the manifest ``embedding_dim`` is
        # checked against ``config.embedding_dim``. The diagnostics block
        # below swallows exceptions, so this gate must sit above it. The
        # runtime embedder identity (embedder_id / version) is verified in
        # ``build_engine``; here we only guard the manifest-vs-config dim.
        from pathlib import Path
        from gaottt.store.manifest import ensure_manifest

        manifest = ensure_manifest(Path(self.config.data_dir), self.config)
        if manifest.embedding_dim != self.config.embedding_dim:
            msg = (
                f"Manifest embedding_dim mismatch: manifest="
                f"{manifest.embedding_dim}, config="
                f"{self.config.embedding_dim}. Switching embedder "
                f"requires re-embedding via scripts/rebuild_faiss_from_db.py "
                f"and a manifest update. "
                f"escape: GAOTTT_MANIFEST_CHECK_ENABLED=false"
            )
            if self.config.manifest_check_enabled:
                raise RuntimeError(msg)
            logger.warning(
                "Manifest embedding_dim mismatch (check disabled, "
                "continuing): manifest=%s, config=%s",
                manifest.embedding_dim,
                self.config.embedding_dim,
            )
        # MV2 — owner lease. Fires when owner_lease_enabled OR manifest.managed.
        # Both False (standalone default) → skip entirely: no owner.lock, no
        # heartbeat task, no release on shutdown. This is the "default 不変"
        # gate — existing deployments that never set the flag are bit-exact.
        # LeaseHeldError from acquire() propagates unmasked (the caller's
        # startup fails loudly, signalling "another process owns this dir").
        if self.config.owner_lease_enabled or manifest.managed:
            self._lease = OwnerLease(
                Path(self.config.data_dir), self.config,
            )
            self._lease.acquire(force=self.config.lease_force_takeover)
        await self.store.initialize()
        expired = await self.store.expire_due_nodes(time.time())
        if expired:
            logger.info("Auto-expired %d nodes past their TTL", expired)
        await self.cache.load_from_store(self.store)
        self.faiss_index.load(self.config.faiss_index_path)
        if self.virtual_faiss_index is not None:
            virtual_path = self.config.virtual_faiss_index_path
            self.virtual_faiss_index.load(virtual_path)
            if self.virtual_faiss_index.size == 0 and self.faiss_index.size > 0:
                # No persisted virtual index yet — rebuild from raw + cache.
                logger.info(
                    "Virtual FAISS missing; building from raw + displacement"
                )
                await self._rebuild_virtual_faiss_index()
        # Phase L Stage 1: build BM25 index from active document content.
        # D2: in-memory only — no disk persistence in Stage 1, so we always
        # rebuild from SQLite content at startup. Also builds the ambient
        # gate index (word-level BM25) when wired.
        if self.bm25_index is not None or self.ambient_gate_index is not None:
            await self._build_bm25_from_store()
        self.cache.start_write_behind(self.store)
        if self.config.faiss_save_interval_seconds > 0:
            self._faiss_save_stop = asyncio.Event()
            self._faiss_save_task = asyncio.create_task(self._faiss_save_loop())
        if (
            self.virtual_faiss_index is not None
            and self.config.virtual_faiss_save_interval_seconds > 0
        ):
            self._virtual_faiss_save_stop = asyncio.Event()
            self._virtual_faiss_save_task = asyncio.create_task(
                self._virtual_faiss_save_loop()
            )
        # The dream loop hosts both the synthetic-recall replay (dream_enabled)
        # and the Phase Q Stage 2 continuous orbital tick (orbital_tick_enabled).
        # Start it if either feature is on; each is independently gated inside.
        if (
            (self.config.dream_enabled or self.config.orbital_tick_enabled)
            and self.config.dream_interval_seconds > 0
        ):
            self._dream_stop = asyncio.Event()
            self._dream_task = asyncio.create_task(self._dream_loop())
        # MV2 — lease heartbeat: refresh owner.lock on a fixed cadence so a
        # foreign takeover (stale/force) is detected and the engine latches
        # read-only. Only started when a lease was acquired and the cadence
        # is positive.
        if self._lease is not None and self.config.lease_heartbeat_seconds > 0:
            self._lease_stop = asyncio.Event()
            self._lease_task = asyncio.create_task(self._lease_heartbeat_loop())
        logger.info(
            "Engine started: %d nodes cached, %d vectors indexed, %d displacements",
            len(self.cache.node_cache),
            self.faiss_index.size,
            len(self.cache.displacement_cache),
        )

        # Stage 1 startup self-diagnostics (commitment id=aaa6e7cc).
        # Imported lazily so test fixtures that construct engines without
        # the diagnostics module on the path don't break. Failures of
        # individual checks are captured in the report, not raised.
        try:
            from gaottt.diagnostics import run_startup_checks
            await run_startup_checks(self, self.config)
        except Exception as e:
            logger.warning(
                "Startup diagnostics raised — engine remains operational: %s: %s",
                type(e).__name__, e,
            )

        # Phase N candidate β Stage 1 — cold-start mass evaporation sweep.
        # If the engine was offline for longer than τ_grace, no recall path
        # has touched these nodes since shutdown, so the lazy hook never
        # fires for them. Apply ``evaporate_mass`` once to every active
        # node here so the field starts from a fully-settled state. Idempotent:
        # uses ``state.last_access`` as the only time reference, so re-running
        # this on the same shutdown→startup gap produces the same result.
        # No-op when ``mass_evaporation_enabled=False`` (per-call guard inside
        # ``evaporate_mass``), so the loop cost is only paid post-rollout.
        if self.config.mass_evaporation_enabled:
            now_sweep = time.time()
            swept = 0
            for state in self.cache.get_all_nodes():
                if state.is_archived:
                    continue
                new_mass = evaporate_mass(
                    state.mass, state.last_access, now_sweep, self.config,
                )
                if new_mass != state.mass:
                    state.mass = new_mass
                    self.cache.set_node(state, dirty=True)
                    swept += 1
            if swept:
                logger.info(
                    "Phase N β cold-start sweep: %d nodes settled mass debt",
                    swept,
                )

    async def shutdown(self) -> None:
        await self.prefetch_pool.drain(timeout=5.0)
        if self._dream_stop is not None:
            self._dream_stop.set()
        if self._dream_task is not None:
            try:
                await asyncio.wait_for(self._dream_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._dream_task.cancel()
        # MV2 — stop the lease heartbeat before touching FAISS: the order is
        # dream → lease heartbeat → FAISS save. Signalling stop lets the
        # loop exit cleanly; the task then awaits like the others.
        if self._lease_stop is not None:
            self._lease_stop.set()
        if self._lease_task is not None:
            try:
                await asyncio.wait_for(self._lease_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._lease_task.cancel()
        if self._faiss_save_stop is not None:
            self._faiss_save_stop.set()
        if self._faiss_save_task is not None:
            try:
                await asyncio.wait_for(self._faiss_save_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._faiss_save_task.cancel()
        if self._virtual_faiss_save_stop is not None:
            self._virtual_faiss_save_stop.set()
        if self._virtual_faiss_save_task is not None:
            try:
                await asyncio.wait_for(
                    self._virtual_faiss_save_task, timeout=10.0,
                )
            except asyncio.TimeoutError:
                self._virtual_faiss_save_task.cancel()
        await self.cache.stop_write_behind()
        # MV2 — revalidate ownership AFTER write-behind is fully stopped, so
        # no background flush can race with the latch. The heartbeat loop
        # stopped above, but write-behind kept running until the line just
        # above; with it now cancelled+joined, a final read-back of
        # ``is_active`` is the single remaining place a takeover (between
        # heartbeat-stop and here) can be detected. ``is_active`` re-reads
        # owner.lock without bumping heartbeat_at, so it is safe
        # mid-shutdown. If the owner changed, ``_on_lease_lost`` latches
        # ``_persist_blocked`` (and the cache's flag), making the manual
        # final flush below and the FAISS save further down both no-ops.
        if self._lease is not None and not self._lease.is_active:
            self._on_lease_lost()
        await self.cache.flush_to_store(self.store)
        # Final synchronous save guarantees durability even if the loop
        # was disabled or skipped a final tick — but still honour the
        # reverse-overwrite guard so a broken-index process doesn't clobber a
        # good on-disk index on its way out.
        ok, reason = self._faiss_safe_to_persist()
        if ok:
            # Bound the final saves so shutdown cannot hang indefinitely. The
            # per-task awaits above use wait_for(10s); these to_thread saves
            # previously had no timeout, so a wedged save (executor saturation,
            # or — with the FaissIndex lock — a cancelled-but-still-running
            # periodic save holding the lock while this one waits for it) would
            # block shutdown forever. On timeout we log and move on: the
            # on-disk index may be stale, but the startup diagnostic rebuilds
            # from the store, so durability is not lost. (wait_for cancels the
            # await but cannot interrupt the worker thread; this unblocks
            # shutdown, not a genuinely deadlocked write_index — the deeper fix
            # there is to not hold the lock across the whole write.)
            timeout = self.config.faiss_final_save_timeout_seconds
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        self.faiss_index.save, self.config.faiss_index_path,
                    ),
                    timeout=timeout,
                )
                if self.virtual_faiss_index is not None:
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            self.virtual_faiss_index.save,
                            self.config.virtual_faiss_index_path,
                        ),
                        timeout=timeout,
                    )
            except asyncio.TimeoutError:
                logger.warning(
                    "Final FAISS save exceeded %.0fs during shutdown — "
                    "skipping to avoid a hang. On-disk index may be stale; "
                    "the startup diagnostic will rebuild from the store.",
                    timeout,
                )
        else:
            self._log_persist_skip(reason)
        # MV2 — release the owner lease after the final flush + final FAISS
        # save so the very last writes land before ownership is relinquished.
        # ``release()`` is owner_id-guarded: if the lease was lost (persist
        # blocked), the final flush/save were no-ops and release() leaves the
        # foreign owner's lock intact. A standalone engine (no lease) skips.
        if self._lease is not None:
            self._lease.release()
        self._faiss_dirty = False
        await self.store.close()
        logger.info("Engine shut down, state persisted")

    def _faiss_safe_to_persist(self) -> tuple[bool, str]:
        """Reverse-overwrite guard: may this process write FAISS to disk?

        Returns ``(ok, reason)``. ``ok=False`` means the in-memory index is
        untrustworthy and persisting it would risk clobbering a good on-disk
        index written by a healthy sibling process.

        Two gates:
          * ``_faiss_persist_blocked`` — a hard latch set by startup
            diagnostics when the loaded index was severely undersized.
          * dynamic ratio — ``faiss.size`` has fallen far below the SQLite
            active-node count *right now*. Inert below ``faiss_persist_floor``
            (small/reset DBs) and when the guard is disabled by config.

        Legitimate bulk shrink (forget/compact) evicts from cache too, so the
        active count falls in lockstep and ``size/active`` stays healthy — the
        guard does not misfire on intentional deletion.
        """
        if self._persist_blocked:
            return False, "persist blocked (lease lost — engine is read-only)"
        if self._faiss_persist_blocked:
            return False, "persist blocked (startup severe-undersize latch)"
        if not self.config.faiss_persist_guard_enabled:
            return True, ""
        active = sum(
            1 for s in self.cache.get_all_nodes() if not s.is_archived
        )
        if active < self.config.faiss_persist_floor:
            return True, ""
        size = self.faiss_index.size
        if size < active * self.config.faiss_persist_min_ratio:
            return (
                False,
                f"faiss.size={size} << SQLite active={active} "
                f"(ratio<{self.config.faiss_persist_min_ratio}); refusing to "
                f"overwrite a healthy on-disk index",
            )
        return True, ""

    def _log_persist_skip(self, reason: str) -> None:
        """Log a guard-triggered persist skip at most once per process."""
        if not self._faiss_persist_guard_warned:
            self._faiss_persist_guard_warned = True
            logger.error(
                "FAISS persist BLOCKED — %s. This process will not write FAISS "
                "to disk. Recover: stop all gaottt processes, run "
                "`scripts/rebuild_faiss_from_db.py --apply`, then restart.",
                reason,
            )

    async def _faiss_save_loop(self) -> None:
        """Background FAISS save: persists in-memory FAISS additions on a
        fixed cadence. Crucial for multi-process visibility.
        """
        assert self._faiss_save_stop is not None
        interval = self.config.faiss_save_interval_seconds
        path = self.config.faiss_index_path
        while not self._faiss_save_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._faiss_save_stop.wait(), timeout=interval,
                )
                break  # stop signalled
            except asyncio.TimeoutError:
                pass  # interval elapsed, try a save tick
            if self._faiss_dirty:
                ok, reason = self._faiss_safe_to_persist()
                if not ok:
                    # Keep dirty=True so a later healthy state still flushes,
                    # but never overwrite a good on-disk index from here.
                    self._log_persist_skip(reason)
                    continue
                # Claim before save so any add() during the save itself
                # leaves dirty=True for the next tick to handle.
                self._faiss_dirty = False
                try:
                    await asyncio.to_thread(self.faiss_index.save, path)
                except Exception:  # noqa: BLE001
                    self._faiss_dirty = True
                    logger.exception("Periodic FAISS save failed; will retry")

    async def _virtual_faiss_save_loop(self) -> None:
        """Background virtual FAISS rebuild + save: refreshes the
        displacement-aware seed index on a fixed cadence whenever the
        cache marks itself dirty. Without this, displacement edits from
        recall (Phase I/J query attraction), genesis kicks, and the dream
        loop never reach the seed pool of subsequent recalls — virtual
        FAISS would only refresh at compact(rebuild_faiss=True).

        Rebuild is O(N) over active nodes. The default 60s cadence keeps
        the work amortized; tune via virtual_faiss_save_interval_seconds.
        """
        assert self._virtual_faiss_save_stop is not None
        assert self.virtual_faiss_index is not None
        interval = self.config.virtual_faiss_save_interval_seconds
        path = self.config.virtual_faiss_index_path
        while not self._virtual_faiss_save_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._virtual_faiss_save_stop.wait(), timeout=interval,
                )
                break  # stop signalled
            except asyncio.TimeoutError:
                pass  # interval elapsed, try a rebuild tick
            if self.cache.virtual_faiss_dirty:
                # The virtual index is derived from the raw index; if the raw
                # index is untrustworthy (reverse-overwrite guard tripped),
                # the virtual one is too — do not persist either.
                ok, reason = self._faiss_safe_to_persist()
                if not ok:
                    self._log_persist_skip(reason)
                    continue
                # Claim before rebuild so any set_displacement during the
                # rebuild itself leaves dirty=True for the next tick.
                self.cache.virtual_faiss_dirty = False
                try:
                    await self._rebuild_virtual_faiss_index()
                    await asyncio.to_thread(
                        self.virtual_faiss_index.save, path,
                    )
                except Exception:  # noqa: BLE001
                    self.cache.virtual_faiss_dirty = True
                    logger.exception(
                        "Periodic virtual FAISS rebuild failed; will retry"
                    )

    def _pick_dream_candidates(self, limit: int) -> list[str]:
        """Quiet nodes worth revisiting in a dream tick.

        Picks non-archived nodes whose mass is still below
        ``dream_mass_ceiling`` and whose ``last_access`` is older than
        ``dream_min_idle_seconds``. Sorted by oldest-access-first so the
        coldest memories get revived earliest.
        """
        now = time.time()
        ceiling = self.config.dream_mass_ceiling
        min_idle = self.config.dream_min_idle_seconds
        quiet = [
            s for s in self.cache.get_all_nodes()
            if not s.is_archived
            and s.mass < ceiling
            and (now - s.last_access) > min_idle
        ]
        quiet.sort(key=lambda s: s.last_access)
        return [s.id for s in quiet[:limit]]

    async def _dream_loop(self) -> None:
        """Hippocampal-replay analog. While the user is silent, revisit
        quiet nodes via synthetic recall so they accumulate co-occurrence
        and gravity field updates without ever being shown to the LLM.
        """
        assert self._dream_stop is not None
        interval = self.config.dream_interval_seconds
        while not self._dream_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._dream_stop.wait(), timeout=interval,
                )
                break  # stop signalled
            except asyncio.TimeoutError:
                pass

            try:
                # Phase Q Stage 2: advance free orbital motion for the lively
                # set (no recall, no mass/temp update). Runs first so the
                # cosmos keeps moving even when synthetic replay is disabled.
                if self.config.orbital_tick_enabled:
                    self._orbital_tick()
                    await asyncio.sleep(0)

                # Hippocampal replay: synthetic recalls of quiet nodes.
                if self.config.dream_enabled:
                    candidates = self._pick_dream_candidates(
                        limit=self.config.dream_batch_size,
                    )
                    for nid in candidates:
                        if self._dream_stop.is_set():
                            break
                        doc = await self.store.get_document(nid)
                        if not doc:
                            continue
                        await self._query_internal(
                            text=doc["content"],
                            top_k=self.config.dream_top_k,
                            wave_depth=None,
                            wave_k=None,
                            _is_synthetic=True,
                        )
                        # Phase M follow-up (2026-05-13): yield to the event
                        # loop between candidates so foreground MCP / REST
                        # recalls aren't starved during a dream tick.
                        # ``_query_internal`` is dominated by numpy / FAISS work
                        # that doesn't release the GIL, so without this explicit
                        # yield a batch of N candidates runs as a single
                        # contiguous CPU burst and makes interactive recalls
                        # time out.
                        await asyncio.sleep(0)
            except Exception:  # noqa: BLE001
                # A bad tick should not kill the loop. Log and try again.
                logger.exception("Dream tick failed; will retry next cycle")

    def _orbital_tick(self) -> None:
        """Phase Q Stage 2 — one continuous orbital integration step.

        Advances the orbital state (displacement + velocity) of the *lively*
        nodes — those whose cached velocity exceeds
        ``orbital_lively_v_min`` — by reusing ``update_orbital_state`` with the
        lively set as the active body. The dominant force is each node's own
        Hooke anchor (``F = -k·d`` toward its raw embedding), which by
        Bertrand's theorem is a closed-orbit central force; mutual gravity
        among the lively set perturbs the ellipses into rosettes. The node
        orbits its *own* anchor → zero anchor migration.

        Unlike a recall, this touches **only** displacement and velocity:
        mass, temperature, last_access, and co-occurrence are left untouched
        (recall = energy injection; tick = free evolution). Age-based friction
        is suppressed for the tick — it keys on ``last_access``, which is stale
        for an orbiting-but-unrecalled node and would otherwise damp the orbit
        to zero within a few ticks; only the small constant friction applies,
        giving the slow thermodynamic decay back into the well.

        Cost is O(L²) over the lively set L (mutual gravity in
        ``update_orbital_state``). ``L`` is self-limiting because constant
        friction returns kicked nodes to "cold" ~100 ticks after their last
        recall; ``orbital_tick_max_nodes`` is a hard backstop and logs when it
        truncates so a coverage cap is never silent.
        """
        if not self.config.orbital_tick_enabled:
            return
        if self._persist_blocked:
            return  # read-only: lease lost, skip mass/displacement writes

        v_min = self.config.orbital_lively_v_min
        lively: list[tuple[str, float]] = []
        for state in self.cache.get_all_nodes():
            if state.is_archived:
                continue
            vel = self.cache.get_velocity(state.id)
            if vel is None:
                continue
            speed = float(np.linalg.norm(vel))
            if speed > v_min:
                lively.append((state.id, speed))

        if len(lively) < 2:
            # update_orbital_state needs >= 2 bodies; a lone lively node has
            # no mutual gravity to integrate against here (its anchor-only
            # motion is picked up on the next recall path). Skip cleanly.
            return

        cap = self.config.orbital_tick_max_nodes
        if len(lively) > cap:
            # Process the fastest movers first; defer the rest to later ticks.
            lively.sort(key=lambda t: t[1], reverse=True)
            logger.info(
                "orbital_tick: %d lively nodes > cap %d — integrating top %d "
                "by speed, deferring %d to the next tick",
                len(lively), cap, cap, len(lively) - cap,
            )
            lively = lively[:cap]

        ids = [nid for nid, _ in lively]
        original_embs = self.faiss_index.get_vectors(ids)
        active = [nid for nid in ids if nid in original_embs]
        if len(active) < 2:
            return

        dim = self.config.embedding_dim
        displacements: dict[str, np.ndarray] = {}
        velocities: dict[str, np.ndarray] = {}
        masses: dict[str, float] = {}
        last_accesses: dict[str, float] = {}
        now = time.time()
        for nid in active:
            d = self.cache.get_displacement(nid)
            displacements[nid] = d if d is not None else np.zeros(dim, dtype=np.float32)
            v = self.cache.get_velocity(nid)
            velocities[nid] = v if v is not None else np.zeros(dim, dtype=np.float32)
            st = self.cache.get_node(nid)
            masses[nid] = st.mass if st is not None else 1.0
            last_accesses[nid] = st.last_access if st is not None else now

        # Suppress age friction for the free-evolution tick (constant friction
        # only — see docstring). dataclasses.replace keeps every other knob,
        # including orbital_integrator (Verlet) and the mass-dependent anchor β.
        #
        # Phase Q rollout finding (2026-05-30): the lively set handed to
        # update_orbital_state is a scattered set of the fastest movers, not a
        # true local neighbourhood. Treating it as mutual N-body neighbours
        # makes neighbour gravity sum *coherently* in RURI's narrow high-cosine
        # space (measured net |a|~10–640 vs the anchor's ~0.005), dominating the
        # Hooke restoring force and slamming displacement onto the
        # max_displacement_norm clamp instead of perturbing the orbit. So by
        # default the tick zeroes gravity_G (→ neighbour gravity AND the
        # G-scaled mass-BH term vanish), leaving a pure self-anchor Hooke orbit:
        # each node orbits its own embedding x₀ — exactly Phase Q's
        # zero-anchor-migration core, measured bounded + self-limiting.
        # orbital_tick_neighbor_gravity_enabled=True restores the coupled
        # behaviour for experimentation. The recall path is unaffected.
        from dataclasses import replace
        tick_overrides = {"orbital_friction_age_factor": 0.0}
        if not self.config.orbital_tick_neighbor_gravity_enabled:
            tick_overrides["gravity_G"] = 0.0
        tick_config = replace(self.config, **tick_overrides)

        new_disps, new_vels = update_orbital_state(
            active, original_embs,
            displacements, velocities,
            masses, last_accesses, now, tick_config,
            cache=self.cache,
            query_anchor=None,   # no query-attraction kick during free evolution
            query_scores=None,
        )

        for nid in new_disps:
            self.cache.set_displacement(nid, new_disps[nid])
            self.cache.set_velocity(nid, new_vels[nid])

    async def _lease_heartbeat_loop(self) -> None:
        """MV2 — refresh owner.lock heartbeat until stop or owner loss.

        On each interval the lease re-reads its lock under the guard and
        bumps ``heartbeat_at`` iff it still owns it. A foreign owner read
        back (another process took over via stale/force) flips the engine
        to read-only via ``_on_lease_lost`` and ends the loop.
        """
        assert self._lease_stop is not None
        assert self._lease is not None
        interval = self.config.lease_heartbeat_seconds
        while not self._lease_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._lease_stop.wait(), timeout=interval,
                )
                break  # stop signalled
            except asyncio.TimeoutError:
                pass  # interval elapsed, try a heartbeat tick
            if not self._lease._refresh_heartbeat():
                # Owner_id mismatch read back under the guard — we were
                # displaced. Transition to read-only and stop refreshing
                # (further writes would clobber the new owner).
                self._on_lease_lost()
                break

    def _on_lease_lost(self) -> None:
        """Latch the read-only transition after the heartbeat detects loss.

        Sets both the engine-wide ``_persist_blocked`` and the cache's
        ``persist_blocked`` so every persistence route (write-behind flush,
        FAISS save loop, virtual-FAISS save loop, shutdown final save /
        flush, mutating method entry) gates on the same signal. The ERROR
        log fires once per process (``_lease_lost_warned`` latch).
        """
        self._persist_blocked = True
        self.cache.persist_blocked = True
        if not self._lease_lost_warned:
            self._lease_lost_warned = True
            logger.error(
                "Owner lease lost — heartbeat detected owner_id mismatch. "
                "Engine transitioning to read-only. Mutating operations "
                "will raise LeaseLostError. Reads still work."
            )

    # --- US1: Document Indexing ---

    async def index_documents(
        self, documents: list[dict],
    ) -> list[str]:
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        hashes = [
            hashlib.sha256(d["content"].encode("utf-8")).hexdigest()
            for d in documents
        ]
        existing = await self.store.find_existing_hashes(hashes)
        filtered = [
            (d, h) for d, h in zip(documents, hashes) if h not in existing
        ]
        if not filtered:
            logger.info("All %d documents already exist, skipping", len(documents))
            return []

        docs_to_index = [d for d, _ in filtered]
        skipped = len(documents) - len(docs_to_index)
        if skipped:
            logger.info("Skipping %d duplicate documents", skipped)

        contents = [d["content"] for d in docs_to_index]
        metadatas = [d.get("metadata") for d in docs_to_index]
        ids = [str(uuid.uuid4()) for _ in docs_to_index]

        # Phase M Stage 1 — stamp structural identifiers on metadata before
        # we hand it to the store, so the self-force filter has something
        # to inspect on every node.
        #   * ``original_id``: defaults to the node's own id (single
        #     remember acts as its own "document"). If the caller already
        #     supplied one — or provided ``file_path`` from a chunking
        #     ingest path — we honour that, so all chunks of the same
        #     file share the same original_id and stop inflating each
        #     other's mass.
        #   * ``cohort_id``: assigned only when this batch is going to
        #     trigger a Phase K supernova (cohort size ≥ threshold). All
        #     nodes in the cohort share the same id; singleton remembers
        #     stay absent so they never self-cancel by accident.
        cohort_id: str | None = None
        if (
            self.config.supernova_enabled
            and len(ids) >= self.config.supernova_min_cohort_size
        ):
            cohort_id = uuid.uuid4().hex[:12]

        for i, doc_id in enumerate(ids):
            meta = metadatas[i] or {}
            if "original_id" not in meta:
                # H8: only group by file_path when it is an UNAMBIGUOUS
                # absolute path. A bare basename / relative path (e.g.
                # "README.md") is not a global identity — two unrelated
                # ingests that happen to share it would be treated as the
                # same document and have their genuine external-referral
                # mass suppressed as "internal trade" (false self-force,
                # corrupting Mass Conservation). Falling back to the node's
                # own id is the safe direction: a node only ever
                # self-matches itself, so a *missed* grouping merely costs
                # a little mass conservation for that ingest, whereas a
                # *false* grouping actively corrupts the gravity field.
                # Loaders that want chunk-grouping must pass an absolute
                # file_path (scripts/load_files.py does) or set
                # original_id explicitly.
                fp = meta.get("file_path")
                if isinstance(fp, str) and os.path.isabs(fp):
                    meta["original_id"] = fp
                else:
                    meta["original_id"] = doc_id
            if cohort_id is not None:
                meta["cohort_id"] = cohort_id
            metadatas[i] = meta

        vectors = self.embedder.encode_documents(contents)
        self.faiss_index.add(vectors, ids)
        if self.virtual_faiss_index is not None:
            # Fresh nodes have displacement=0, so virtual_pos == raw.
            # The genesis kick below mutates cache.displacement; for now,
            # raw and virtual stay aligned, and a later compact (or the
            # next kick step) will pull virtual_pos away from raw if
            # needed.
            self.virtual_faiss_index.add(vectors, ids)
        # Phase L Stage 1: feed BM25 with the document text so lexical
        # matches on the new docs are findable immediately, without waiting
        # for the next compact/startup rebuild.
        if self.bm25_index is not None:
            self.bm25_index.add(ids, contents)
        if self.ambient_gate_index is not None:
            self.ambient_gate_index.add(ids, contents)

        now = time.time()
        docs_for_store = []
        for i, doc_id in enumerate(ids):
            doc_in = docs_to_index[i]
            state = NodeState(
                id=doc_id,
                last_access=now,
                expires_at=doc_in.get("expires_at"),
                emotion_weight=float(doc_in.get("emotion", 0.0)),
                certainty=float(doc_in.get("certainty", 1.0)),
                last_verified_at=now if "certainty" in doc_in else None,
            )
            self.cache.set_node(state, dirty=True)
            meta = metadatas[i] or {}
            src = meta.get("source")
            if src:
                self.cache.set_source(doc_id, src)
            # Phase J Stage 2: mirror tags so tag_filter injection sees
            # them without waiting for a cache reload.
            tags = meta.get("tags")
            if isinstance(tags, list):
                self.cache.set_tags(doc_id, [t for t in tags if isinstance(t, str)])
            # Phase M Stage 1: mirror structural identifiers — the
            # self-force filter in the wave-driven mass update consults
            # the cache, not the store, so we have to populate it now
            # instead of waiting for the next restart.
            original_id = meta.get("original_id")
            if isinstance(original_id, str) and original_id:
                self.cache.set_original(doc_id, original_id)
            cohort_meta = meta.get("cohort_id")
            if isinstance(cohort_meta, str) and cohort_meta:
                self.cache.set_cohort(doc_id, cohort_meta)
            docs_for_store.append({
                "id": doc_id,
                "content": contents[i],
                "metadata": metadatas[i],
            })

        await self.store.save_documents(docs_for_store)

        # Phase G — Genesis kick: apply one-step Newtonian gravity so the
        # new nodes enter the field with non-zero orbital state instead of
        # competing against established clusters from a "naked" mass=1,
        # displacement=0 starting point. See Plans-Phase-G-Memory-Genesis.md.
        if self.config.genesis_kick_enabled:
            self._apply_genesis_kick(ids, vectors)

        # Phase K — Stellar supernova cohort: when the batch is large enough,
        # form mutual co-occurrence edges + outward initial velocity so the
        # newly-born cohort has internal gravity from birth. See
        # Plans-Phase-K-Stellar-Supernova-Cohort.md. Applied after Phase G so
        # cohort-internal coupling stacks on top of existing-system binding.
        if self.config.supernova_enabled:
            self._apply_supernova_cohort(ids, vectors)

        await self.cache.flush_to_store(self.store)
        self._faiss_dirty = True

        logger.info("Indexed %d documents", len(ids))
        return ids

    def _top_k_heavy_neighbors(
        self,
        vec: np.ndarray,
        k: int,
        pool_size: int = 50,
    ) -> list[tuple[np.ndarray, float]]:
        """Pull a wide FAISS top-N pool, rerank by cached mass, return the
        top-k as (embedding, mass) pairs. Used by the genesis kick to find
        the heavy bodies whose gravity will bend the new node's orbit."""
        pool = self.faiss_index.search(vec.reshape(1, -1), pool_size)
        if not pool:
            return []
        candidates: list[tuple[str, float]] = []
        for nid, _cos in pool:
            state = self.cache.get_node(nid)
            if state is None or state.is_archived:
                continue
            candidates.append((nid, state.mass))
        if not candidates:
            return []
        candidates.sort(key=lambda t: t[1], reverse=True)
        candidates = candidates[:k]
        ids_only = [nid for nid, _ in candidates]
        vec_map = self.faiss_index.get_vectors(ids_only)
        out: list[tuple[np.ndarray, float]] = []
        for nid, mass in candidates:
            v = vec_map.get(nid)
            if v is not None:
                out.append((v, mass))
        return out

    def _apply_genesis_kick(
        self, new_ids: list[str], new_vecs: np.ndarray,
    ) -> None:
        """Run one Verlet step of neighbor gravity on each freshly-indexed
        node, seeding cache displacement/velocity and bumping mass.
        Skips nodes with no qualifying neighbors (an empty DB or a region
        with no heavy bodies)."""
        for i, new_id in enumerate(new_ids):
            new_vec = new_vecs[i]
            neighbors = self._top_k_heavy_neighbors(
                new_vec,
                k=self.config.genesis_kick_neighbor_k,
                pool_size=self.config.genesis_kick_pool_size,
            )
            if not neighbors:
                continue
            disp, vel, m_boost = compute_gravity_kick(
                new_vec, neighbors, self.config,
            )
            disp_norm = float(np.linalg.norm(disp))
            if disp_norm <= 1e-9 and m_boost <= 0.0:
                continue
            self.cache.set_displacement(new_id, disp)
            self.cache.set_velocity(new_id, vel)
            state = self.cache.get_node(new_id)
            if state is not None and m_boost > 0.0:
                state.mass = max(state.mass, 1.0 + m_boost)
                self.cache.set_node(state, dirty=True)

    def _apply_supernova_cohort(
        self, new_ids: list[str], new_vecs: np.ndarray,
    ) -> None:
        """Form mutual co-occurrence edges + outward initial velocity for
        the supernova cohort.

        Velocity is *added* to whatever Phase G genesis kick already put
        in cache (typically Phase G writes a velocity toward existing
        heavy bodies; Phase K adds an outward push from the batch
        centroid; the two compose). Edges are written via
        ``cache.set_edge`` which mirrors both directions of the
        undirected graph and marks them dirty for write-behind flush.
        """
        from gaottt.core.supernova import (
            compute_supernova_velocities,
            form_supernova_edges,
        )

        # Mutual co-occurrence edges
        edges = form_supernova_edges(new_ids, self.config)
        for src, dst, weight in edges:
            self.cache.set_edge(src, dst, weight, dirty=True)

        # Outward initial velocity (added to any Phase G velocity)
        velocities = compute_supernova_velocities(new_ids, new_vecs, self.config)
        for nid, v_supernova in velocities.items():
            existing = self.cache.get_velocity(nid)
            if existing is not None:
                combined = existing + v_supernova
            else:
                combined = v_supernova
            from gaottt.core.gravity import clamp_vector
            combined = clamp_vector(combined, self.config.orbital_max_velocity)
            self.cache.set_velocity(nid, combined.astype(np.float32))

    # --- US2: Query (Gravity Wave Propagation) ---

    async def query(
        self,
        text: str,
        top_k: int | None = None,
        wave_depth: int | None = None,
        wave_k: int | None = None,
        use_cache: bool = False,
        source_filter: list[str] | None = None,
        persona_context: list[str] | None = None,
        tag_filter: list[str] | None = None,
        out_training_delta: dict | None = None,
        gamma_override: float | None = None,
        passive: bool = False,
        multi_source: bool | None = None,
    ) -> list[QueryResultItem]:
        """Run a recall query.

        ``use_cache=True`` consults the prefetch cache first; on hit the
        cached results are returned without re-running embedding/wave/scoring
        (and crucially, without re-applying simulation updates — the prefetch
        already paid that cost). Cache hits are bounded by
        ``config.prefetch_ttl_seconds``.

        ``source_filter`` (Phase H Stage 2) lets the seed step trim the
        FAISS pool to nodes whose ``metadata.source`` matches. Source
        filtering is not part of the prefetch cache key, so any call with
        ``source_filter`` set bypasses the cache.

        ``persona_context`` (Phase J Stage 2) — explicit list of declared
        value/intention/commitment IDs overriding the Stage 1 auto-detect,
        plus additive seed injection of those IDs.

        ``tag_filter`` (Phase J Stage 2) — substring list (OR match) for
        additive seed injection of every node whose ``metadata.tags`` list
        contains any substring. Bypasses ``source_filter``.

        ``gamma_override`` (Hardening Stage 1 / C3) — per-call temperature
        scale used instead of ``config.gamma`` for this recall only. Lets
        ``explore`` widen the thermal noise without monkey-patching the
        shared config across an await (which corrupted concurrent recalls).
        A non-default gamma must never read or write the shared (text, k)
        prefetch cache, so it also bypasses the cache.

        ``passive`` (Ambient Recall) — read-only recall. The search runs in
        full, but the gravity field is not perturbed afterward: no mass
        update, no query-attraction displacement, no co-occurrence edges.
        A passive recall still *reads* the prefetch cache (a cache hit is
        side-effect-free anyway), but it never *writes* it — a passive
        result must not poison a later active recall into skipping its TTT
        update. Used by automatic / background recall (Claude Code hook).

        Either explicit argument bypasses the prefetch cache.
        """
        # MV2 — read-only transition: when the lease was lost, a query still
        # returns results but must NOT perturb the gravity field. Forcing
        # ``passive=True`` routes through the existing passive gates (mass /
        # displacement / co-occurrence / return_count updates all skip on a
        # passive recall), so the field is observed, never mutated.
        if self._persist_blocked:
            passive = True
        k = top_k or self.config.top_k
        if source_filter or persona_context or tag_filter or gamma_override is not None:
            use_cache = False
        if use_cache:
            cached = self.prefetch_cache.get(text, k, wave_depth, wave_k)
            if cached is not None:
                if out_training_delta is not None:
                    # Phase O Stage 2 — cache hit means no simulation ran.
                    # Signal that explicitly so the caller can distinguish
                    # "TTT update was suppressed" from "no nodes were touched".
                    out_training_delta["cache_hit"] = True
                return cached
        results = await self._query_internal(
            text=text, top_k=k, wave_depth=wave_depth, wave_k=wave_k,
            source_filter=source_filter,
            persona_context=persona_context,
            tag_filter=tag_filter,
            out_training_delta=out_training_delta,
            gamma_override=gamma_override,
            passive=passive,
            multi_source=multi_source,
        )
        # A passive recall never writes the shared prefetch cache: a cached
        # passive result would let a subsequent active recall hit the cache
        # and silently skip its simulation update.
        if use_cache and not passive:
            self.prefetch_cache.put(text, k, results, wave_depth, wave_k)
        return results

    async def _query_internal(
        self,
        text: str,
        top_k: int,
        wave_depth: int | None,
        wave_k: int | None,
        _is_synthetic: bool = False,
        source_filter: list[str] | None = None,
        persona_context: list[str] | None = None,
        tag_filter: list[str] | None = None,
        out_training_delta: dict | None = None,
        gamma_override: float | None = None,
        passive: bool = False,
        multi_source: bool | None = None,
    ) -> list[QueryResultItem]:
        # MV2 — when the lease was lost, force passive so mass / displacement
        # / co-occurrence / return_count updates are all skipped. Putting this
        # guard here (not just in query()) covers ALL callers: query(),
        # prefetch(), and the dream loop. The query()-level guard above is
        # now belt-and-suspenders and kept for readability.
        if self._persist_blocked:
            passive = True
        k = top_k
        query_vec = self.embedder.encode_query(text)

        # Multi-Source Query — when enabled, segment the prompt into clauses
        # and batch-embed each as a separate point mass. The wave then seeds
        # from the superposed per-segment pools instead of the pooled
        # centroid; ``query_vec`` (the whole-prompt embedding) stays the
        # scoring / TTT anchor. ``multi_source`` overrides the config flag
        # (the ambient path passes ``multi_source_ambient_enabled``); None
        # falls back to ``config.multi_source_enabled``. See
        # docs/wiki/Plans-Query-Mass-Distribution.md.
        segment_vecs: np.ndarray | None = None
        n_intent_centers = 1
        ms_on = (
            self.config.multi_source_enabled if multi_source is None
            else multi_source
        )
        if ms_on:
            segments = segment_query(text, self.config)
            if len(segments) > 1:
                # ``encode_queries`` is the batched fast path (RuriEmbedder);
                # fall back to per-segment ``encode_query`` for embedders that
                # only implement the single-query method (e.g. test stubs).
                encode_many = getattr(self.embedder, "encode_queries", None)
                if encode_many is not None:
                    segment_vecs = encode_many(segments)
                else:
                    segment_vecs = np.vstack(
                        [self.embedder.encode_query(s) for s in segments]
                    )
                n_intent_centers = len(segments)

        # Phase J Stage 1 / Stage 2: compute persona proximities once per
        # recall. Stage 2 explicit `persona_context` takes precedence over
        # the Stage 1 auto-detected active set.
        persona_proximities: dict[str, float] | None = None
        if self.config.persona_boost_enabled and self.config.persona_boost_alpha > 0.0:
            if persona_context:
                persona_ids: set[str] = set(persona_context)
            else:
                persona_ids = collect_active_persona_ids(
                    self.cache, self.config, time.time(),
                )
            if persona_ids:
                persona_proximities = compute_persona_proximities(
                    persona_ids, self.cache, self.config,
                )

        # Phase J Stage 2: build the additive injection set — explicit
        # persona_context ids plus every node matching the tag_filter
        # substring(s).
        injected_ids: set[str] | None = None
        if persona_context or tag_filter:
            injected_ids = set()
            if persona_context:
                injected_ids |= set(persona_context)
            if tag_filter:
                injected_ids |= self.cache.find_ids_by_tag_filter(tag_filter)
            if not injected_ids:
                injected_ids = None

        # Step 1: Gravity wave propagation — recursive neighbor expansion.
        # Phase M Stage 1: capture per-parent force attribution so the
        # mass-update path can filter same-document / same-cohort
        # "internal trade" contributions (Mass Conservation rule).
        wave_attribution: dict[str, dict[str, float]] = {}
        reached = propagate_gravity_wave(
            query_vec, self.faiss_index, self.cache, self.config,
            wave_k=wave_k, wave_depth=wave_depth,
            source_filter=source_filter,
            virtual_faiss_index=self.virtual_faiss_index,
            persona_proximities=persona_proximities,
            injected_ids=injected_ids,
            query_text=text,
            bm25_index=self.bm25_index,
            out_attribution=wave_attribution,
            segment_vectors=segment_vecs,
        )

        if not reached:
            return []

        # Step 2: Get original embeddings for all reached nodes
        reached_ids = list(reached.keys())
        original_embs = self.faiss_index.get_vectors(reached_ids)

        # Step 3: Score all reached nodes with virtual coordinates + wave boost
        now = time.time()
        query_vec_flat = query_vec[0] if query_vec.ndim == 2 else query_vec
        q_norm = float(np.linalg.norm(query_vec_flat)) + 1e-12
        results: list[QueryResultItem] = []
        # Coordinate naming:
        #   gravity_sim  = query_raw · virtual_pos  (stored as QueryResultItem.raw_score,
        #                  labelled "virtual_score" in MCP output).  Carries displacement
        #                  and temperature noise — reflects how far the node has drifted
        #                  toward frequently co-recalled queries.
        #   pure_raw_cosines = query_raw · node_raw  (no displacement).  Used only for
        #                  Phase J Stage 3 forced-set ordering where "closest to this
        #                  query's vocabulary" must win over "most-touched memo".
        #   QueryResultItem.raw_score keeps the field name for REST backward compat;
        #   formatters.format_recall labels it "virtual_score" in MCP output (2026-05-12).
        pure_raw_cosines: dict[str, float] = {}

        # Phase O Stage 1 — informational: precompute which reached nodes the
        # BM25 index hit for this query. Used only for the breakdown flag
        # (bm25_contributed) since BM25's actual additive contribution is
        # already folded into wave_score via _seed_boost RRF fusion.
        bm25_hit_ids: set[str] = set()
        if (
            self.config.expose_score_breakdown
            and self.config.hybrid_bm25_enabled
            and self.bm25_index is not None
            and self.bm25_index.size > 0
            and text
        ):
            try:
                bm25_hits = self.bm25_index.search(text, max(len(reached_ids), 50))
                bm25_hit_ids = {nid for nid, _ in bm25_hits}
            except Exception:
                bm25_hit_ids = set()

        for node_id in reached_ids:
            state = self.cache.get_node(node_id)
            if state is None:
                states = await self.store.get_node_states([node_id])
                state = states.get(node_id)
            if state is None or state.is_archived:
                continue
            if state.expires_at is not None and state.expires_at <= now:
                continue

            original_emb = original_embs.get(node_id)
            if original_emb is None:
                continue

            displacement = self.cache.get_displacement(node_id)
            virtual_pos = compute_virtual_position(
                original_emb, displacement, state.temperature
            )

            gravity_sim = float(np.dot(query_vec_flat, virtual_pos))
            # Pure raw cosine — no displacement, no temperature noise.
            emb_norm = float(np.linalg.norm(original_emb)) + 1e-12
            pure_raw_cosines[node_id] = (
                float(np.dot(query_vec_flat, original_emb)) / (q_norm * emb_norm)
            )

            mass_boost = compute_mass_boost(state.mass, self.config.alpha)
            decay = compute_decay(state.last_access, now, self.config.delta)
            wave_boost = self.config.wave_boost_weight * reached[node_id]
            emotion_boost = compute_emotion_boost(
                state.emotion_weight, self.config.emotion_alpha,
            )
            certainty_boost = compute_certainty_boost(
                state.certainty, state.last_verified_at, now,
                self.config.certainty_alpha, self.config.certainty_half_life_seconds,
            )

            # Presentation saturation: frequently returned nodes get lower scores
            saturation = 1.0 / (1.0 + state.return_count * self.config.saturation_rate)

            final = (
                gravity_sim * decay + mass_boost + wave_boost
                + emotion_boost + certainty_boost
            ) * saturation

            if final <= 0.0:
                continue

            doc = await self.store.get_document(node_id)
            if doc is None:
                continue

            breakdown: ScoreBreakdown | None = None
            if self.config.expose_score_breakdown:
                persona_prox = 0.0
                if persona_proximities is not None:
                    persona_prox = float(persona_proximities.get(node_id, 0.0))
                breakdown = ScoreBreakdown(
                    raw_cosine=pure_raw_cosines[node_id],
                    virtual_cosine=gravity_sim,
                    decay_factor=decay,
                    wave_score=wave_boost,
                    mass_boost=mass_boost,
                    emotion_term=emotion_boost,
                    certainty_term=certainty_boost,
                    saturation=saturation,
                    persona_proximity=persona_prox,
                    bm25_contributed=node_id in bm25_hit_ids,
                    forced_inclusion=bool(injected_ids and node_id in injected_ids),
                )

            results.append(
                QueryResultItem(
                    id=node_id,
                    content=doc["content"],
                    metadata=doc.get("metadata"),
                    raw_score=gravity_sim,
                    final_score=final,
                    score_breakdown=breakdown,
                )
            )

        # Step 4: Sort and take top-K for presentation to LLM.
        # Phase J Stage 2: when explicit injection is requested, force the
        # injected ids into the top-K result. Seed-pool injection alone
        # isn't enough — once the target is a seed, its own wave neighbours
        # can outrank it by sheer cluster mass. The caller's explicit ask
        # has to survive the final cut, not just the entry gate.
        #
        # Critical: when ``len(injected_ids) > k`` (e.g. ``tag_filter``
        # matching 112 nodes with ``top_k=5``), we still respect the
        # caller's ``top_k`` budget — pick the top-K *of the injected
        # set itself* — but rank the forced set by ``raw_score`` rather
        # than ``final_score`` (Phase J Stage 3). Final score is dominated
        # by mass / wave / emotion / certainty, which makes "frequently
        # touched memos win" regardless of query semantic. Inside a
        # caller-injected set, the right ordering is "which of these
        # tagged memos is closest to the query" — i.e. raw cosine.
        # Non-injected results still rank by final_score.
        #
        # Phase L Stage 1 supplement: when BM25 is active, compute a
        # per-node lexical score and combine it with pure raw cosine
        # via RRF so that surface-form matches ("Eleventy Pipeline" →
        # .eleventy.js) can outrank pure-embedding similarity.
        bm25_forced_scores: dict[str, float] = {}
        if (
            injected_ids
            and self.bm25_index is not None
            and self.bm25_index.size > 0
            and text
        ):
            bm25_pool = self.bm25_index.search(
                text, max(len(injected_ids), 50),
            )
            bm25_forced_scores = {nid: sc for nid, sc in bm25_pool}

        if injected_ids:
            forced = [r for r in results if r.id in injected_ids]
            others = [r for r in results if r.id not in injected_ids]

            if bm25_forced_scores:
                forced_cosine_rank: dict[str, int] = {
                    r.id: rank
                    for rank, r in enumerate(
                        sorted(
                            forced,
                            key=lambda r: pure_raw_cosines.get(r.id, 0.0),
                            reverse=True,
                        ),
                        start=1,
                    )
                }
                forced_bm25_rank: dict[str, int] = {}
                bm25_sorted = sorted(
                    bm25_forced_scores.items(),
                    key=lambda t: t[1],
                    reverse=True,
                )
                for rank, (nid, _) in enumerate(bm25_sorted, start=1):
                    if nid in injected_ids:
                        forced_bm25_rank[nid] = rank
                forced.sort(
                    key=lambda r: _rrf_forced_key(
                        r.id, forced_cosine_rank, forced_bm25_rank,
                        self.config.rrf_k,
                    ),
                    reverse=True,
                )
            else:
                forced.sort(
                    key=lambda r: pure_raw_cosines.get(r.id, 0.0),
                    reverse=True,
                )
            others.sort(key=lambda r: r.final_score, reverse=True)
            if len(forced) >= k:
                results = forced[:k]
            else:
                results = forced + others[: k - len(forced)]
        else:
            results.sort(key=lambda r: r.final_score, reverse=True)
            results = results[:k]

        # Step 5: Update return_count for presented nodes + habituation recovery for all.
        # Synthetic recalls (Phase G dream loop) skip return_count so that
        # background revisits don't trip presentation saturation — the user
        # never saw these results, so habituation must not punish them.
        # Lateral Association Stage 1 sub-step 0 (2026-05-25): passive recall
        # is also gated. ``passive=True`` means the ambient hook is observing
        # the field without perturbing it; saturation is field state that
        # drives next-call ranking, so silently mutating it via every ambient
        # turn breaks the "no perturbation" contract (the same way mass /
        # displacement / co-occurrence updates are already gated below). See
        # Plans-Ambient-Recall-Lateral-Association.md Stage 1.
        result_ids = [r.id for r in results]
        all_reached_ids = list(reached.keys())
        if not _is_synthetic and not passive:
            for node_id in result_ids:
                state = self.cache.get_node(node_id)
                if state:
                    state.return_count += 1.0
                    self.cache.set_node(state, dirty=True)

        # Habituation recovery: all reached nodes slowly recover freshness.
        # Synthetic dream-loop recalls still recover (background heal). Passive
        # ambient recalls do NOT — recovery is also field perturbation.
        if not passive:
            for node_id in all_reached_ids:
                state = self.cache.get_node(node_id)
                if state and state.return_count > 0:
                    state.return_count *= (1.0 - self.config.habituation_recovery_rate)
                    self.cache.set_node(state, dirty=True)

        # Phase O Stage 2 — snapshot displacement / mass for delta computation.
        # ``topk_only=True`` (default) limits coverage to top-K returned nodes
        # for context economy; ``False`` covers every reached node (debug).
        pre_disp_norms: dict[str, float] = {}
        pre_masses: dict[str, float] = {}
        delta_active = (
            out_training_delta is not None
            and self.config.training_delta_enabled
        )
        if delta_active:
            topk_only = self.config.training_delta_topk_only
            delta_target_ids = result_ids if topk_only else all_reached_ids
            for nid in delta_target_ids:
                disp = self.cache.get_displacement(nid)
                pre_disp_norms[nid] = float(np.linalg.norm(disp)) if disp is not None else 0.0
                state = self.cache.get_node(nid)
                pre_masses[nid] = float(state.mass) if state is not None else 0.0

        # Step 6: Simulation update — ALL reached nodes.
        # Phase I Stage 2: pass the query vector + wave scores so the orbital
        # step can apply the query-attraction term to reached nodes.
        # Phase M Stage 1: pass per-parent attribution so the mass update can
        # apply the self-force (Mass Conservation) filter.
        # Ambient Recall: a passive recall observes the field without
        # perturbing it — skip mass update, query-attraction displacement and
        # co-occurrence so automatic / background queries never become an
        # uncontrolled TTT signal. The delta block below then reports zeros,
        # which is the honest answer (nothing moved).
        if not passive:
            self._update_simulation(
                all_reached_ids, reached, original_embs, now,
                query_anchor=query_vec_flat,
                wave_attribution=wave_attribution,
                gamma_override=gamma_override,
            )
            self._update_cooccurrence(result_ids)

        if delta_active:
            disp_changes: dict[str, float] = {}
            mass_changes: dict[str, float] = {}
            for nid in delta_target_ids:
                disp = self.cache.get_displacement(nid)
                post_d = float(np.linalg.norm(disp)) if disp is not None else 0.0
                disp_changes[nid] = post_d - pre_disp_norms.get(nid, 0.0)
                state = self.cache.get_node(nid)
                post_m = float(state.mass) if state is not None else 0.0
                mass_changes[nid] = post_m - pre_masses.get(nid, 0.0)
            persona_hops = 0
            if persona_proximities:
                for nid in all_reached_ids:
                    if persona_proximities.get(nid, 0.0) > 0.0:
                        persona_hops += 1
            out_training_delta["displacement_changes"] = disp_changes
            out_training_delta["mass_changes"] = mass_changes
            out_training_delta["wave_reached_count"] = len(reached)
            out_training_delta["wave_max_depth"] = (
                wave_depth if wave_depth is not None else self.config.wave_max_depth
            )
            out_training_delta["persona_hop_reached"] = persona_hops
            out_training_delta["supernova_triggered"] = False  # recall path
            out_training_delta["topk_only"] = self.config.training_delta_topk_only
            out_training_delta["intent_centers"] = n_intent_centers

        return results

    def _update_simulation(
        self,
        all_reached_ids: list[str],
        reached: dict[str, float],
        original_embs: dict[str, np.ndarray],
        now: float,
        query_anchor: np.ndarray | None = None,
        wave_attribution: dict[str, dict[str, float]] | None = None,
        gamma_override: float | None = None,
    ) -> None:
        """Update gravity simulation for ALL wave-reached nodes.

        This is the simulation layer: every node the wave touched gets
        mass/temperature updates and orbital mechanics (acceleration → velocity → position).
        Like dark matter, these invisible updates reshape the gravitational field.
        """
        dim = self.config.embedding_dim
        masses = {}
        last_accesses = {}

        for node_id in all_reached_ids:
            state = self.cache.get_node(node_id)
            if state is None:
                continue

            force = reached.get(node_id, 0.0)

            # Phase M Stage 1 — Mass conservation filter. Sum only the
            # parent contributions that came from *outside* this node's
            # source document and supernova cohort. Same-original /
            # same-cohort co-occurrence is "internal trade" — Articulation
            # as Carrier (id=9a954c62) requires an external referrer to
            # generate mass. ``SEED_PARENT_ID`` (the query itself) and
            # absent attribution (legacy callers, no wave_attribution
            # passed) fall back to full ``force``.
            if (
                self.config.mass_conservation_enabled
                and wave_attribution is not None
            ):
                contributions = wave_attribution.get(node_id, {})
                if contributions:
                    mass_force = 0.0
                    for parent_id, contrib in contributions.items():
                        if parent_id == SEED_PARENT_ID:
                            mass_force += contrib
                        elif not is_self_force_by_id(self.cache, node_id, parent_id):
                            mass_force += contrib
                else:
                    mass_force = force
            else:
                mass_force = force

            # Phase N candidate β Stage 1 — lazy mass evaporation.
            # Apply the t_idle-accumulated decay *before* this recall's
            # Hebbian growth, so a heavily-touched-then-idle node first
            # repays its evaporation debt and then receives new mass on
            # top. No-op when disabled / below floor / inside grace window.
            state.mass = evaporate_mass(
                state.mass, state.last_access, now, self.config,
            )

            # Mass update scaled by external (non-self) force only.
            state.mass += self.config.eta * mass_force * (1.0 - state.mass / self.config.m_max)

            # Sim history ring buffer
            state.sim_history.append(force)
            if len(state.sim_history) > self.config.sim_buffer_size:
                state.sim_history = state.sim_history[-self.config.sim_buffer_size:]

            # Temperature
            if len(state.sim_history) >= 2:
                arr = np.array(state.sim_history)
                gamma = gamma_override if gamma_override is not None else self.config.gamma
                state.temperature = gamma * float(np.var(arr))
            else:
                state.temperature = 0.0

            last_accesses[node_id] = state.last_access
            state.last_access = now
            self.cache.set_node(state, dirty=True)
            masses[node_id] = state.mass

        # Orbital mechanics: acceleration → velocity → displacement
        active_ids = [nid for nid in all_reached_ids if nid in original_embs]
        if len(active_ids) >= 2:
            current_displacements = {}
            current_velocities = {}
            for nid in active_ids:
                cached_d = self.cache.get_displacement(nid)
                current_displacements[nid] = cached_d if cached_d is not None else np.zeros(dim, dtype=np.float32)
                cached_v = self.cache.get_velocity(nid)
                current_velocities[nid] = cached_v if cached_v is not None else np.zeros(dim, dtype=np.float32)

            new_disps, new_vels = update_orbital_state(
                active_ids, original_embs,
                current_displacements, current_velocities,
                masses, last_accesses, now, self.config,
                cache=self.cache,
                query_anchor=query_anchor,
                query_scores=reached if query_anchor is not None else None,
            )

            for nid in new_disps:
                self.cache.set_displacement(nid, new_disps[nid])
                self.cache.set_velocity(nid, new_vels[nid])

    def _update_cooccurrence(self, result_ids: list[str]) -> None:
        """Update co-occurrence graph for LLM-returned results only.

        Co-occurrence is based on what the user/LLM actually "sees" together,
        not the full simulation reach.
        """
        if result_ids:
            self.graph.update_cooccurrence(result_ids)

    # --- US3: Node State Inspection ---

    async def get_node_state(self, node_id: str) -> NodeState | None:
        state = self.cache.get_node(node_id)
        if state is not None:
            return state
        states = await self.store.get_node_states([node_id])
        return states.get(node_id)

    def get_displacement_norm(self, node_id: str) -> float:
        disp = self.cache.get_displacement(node_id)
        if disp is None:
            return 0.0
        return float(np.linalg.norm(disp))

    # --- US4: Graph Inspection ---

    def get_graph(
        self,
        min_weight: float = 0.0,
        node_id: str | None = None,
    ) -> list[CooccurrenceEdge]:
        all_edges = self.cache.get_all_edges()
        filtered = []
        for edge in all_edges:
            if edge.weight < min_weight:
                continue
            if node_id is not None and node_id not in (edge.src, edge.dst):
                continue
            filtered.append(edge)
        return filtered

    # --- F5: Forget / Archive ---

    async def archive(self, node_ids: list[str]) -> int:
        """Soft-delete: mark nodes as archived. They are evicted from cache
        and excluded from recall/explore/reflect, but remain in the store
        and can be restored.
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        if not node_ids:
            return 0
        await self.cache.flush_to_store(self.store)
        affected = await self.store.set_archived(node_ids, archived=True)
        for nid in node_ids:
            self.cache.evict_node(nid)
        # Phase L Stage 1: drop archived ids from BM25 so search excludes
        # them immediately (the postings remain until compact/rebuild).
        if self.bm25_index is not None and affected:
            self.bm25_index.remove(node_ids)
        if affected:
            self.prefetch_cache.invalidate()
        logger.info("Archived %d nodes", affected)
        return affected

    async def restore(self, node_ids: list[str]) -> int:
        """Un-archive nodes and reload them into the cache."""
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        if not node_ids:
            return 0
        affected = await self.store.set_archived(node_ids, archived=False)
        if affected:
            states = await self.store.get_node_states(node_ids)
            for state in states.values():
                state.is_archived = False
                self.cache.set_node(state, dirty=False)
            disps = await self.store.load_displacements(ids=node_ids)
            vels = await self.store.load_velocities(ids=node_ids)
            for nid, disp in disps.items():
                self.cache.set_displacement(nid, disp)
            for nid, vel in vels.items():
                self.cache.set_velocity(nid, vel)
            # Phase L Stage 1: BM25 also surfaces the restored docs again.
            # Calling restore is cheap (just flips the soft-remove flag);
            # if the postings were already compacted away, this is a no-op
            # and the next startup rebuild picks them up.
            if self.bm25_index is not None and affected:
                self.bm25_index.restore(node_ids)
            self.prefetch_cache.invalidate()
        logger.info("Restored %d nodes", affected)
        return affected

    async def forget(self, node_ids: list[str], hard: bool = False) -> int:
        """Forget nodes. hard=False archives them (reversible); hard=True
        physically removes them from the store. Vectors in the FAISS index
        are not removed (rebuild on next reset), but archived nodes are
        filtered out at query time.
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        if not node_ids:
            return 0
        if not hard:
            return await self.archive(node_ids)
        await self.cache.flush_to_store(self.store)
        for nid in node_ids:
            self.cache.evict_node(nid)
        deleted = await self.store.hard_delete_nodes(node_ids)
        # Phase L Stage 1: BM25 postings are reclaimed on the next compact;
        # for now just drop them from active statistics so search excludes
        # them.
        if self.bm25_index is not None and deleted:
            self.bm25_index.remove(node_ids)
        if deleted:
            self.prefetch_cache.invalidate()
        logger.info("Hard-deleted %d nodes", deleted)
        return deleted

    # --- F6: Background prefetch ---

    def prefetch(
        self,
        text: str,
        top_k: int | None = None,
        wave_depth: int | None = None,
        wave_k: int | None = None,
        persona_context: list[str] | None = None,
        tag_filter: list[str] | None = None,
    ) -> object:
        """Schedule a background recall and cache its result.

        Returns the asyncio.Task handle (mostly opaque to callers; tests can
        ``await`` it for determinism). The next ``query(text, top_k,
        use_cache=True)`` within ``prefetch_ttl_seconds`` will be served from
        the cache without re-running the simulation.

        Phase J Stage 3: `persona_context` / `tag_filter` are forwarded so
        the prefetched result matches what an explicit `recall(...)` with
        the same arguments would return. H6: the cache key is
        `(text, top_k, wave_depth, wave_k)`, so a prefetch only serves a
        recall issued with the *same* wave reach — a shallow prefetch can
        no longer poison a deep recall (or vice versa). `persona_context` /
        `tag_filter` still bypass the cache entirely on the read side, so
        they need not be in the key.
        """
        k = top_k or self.config.top_k

        async def _run() -> list[QueryResultItem]:
            results = await self._query_internal(
                text=text, top_k=k, wave_depth=wave_depth, wave_k=wave_k,
                persona_context=persona_context, tag_filter=tag_filter,
            )
            self.prefetch_cache.put(text, k, results, wave_depth, wave_k)
            return results

        return self.prefetch_pool.schedule(_run)

    def prefetch_status(self) -> dict:
        return {
            "cache": self.prefetch_cache.stats(),
            "pool": self.prefetch_pool.stats(),
        }

    # --- F3: Directed (typed) relations ---

    async def relate(
        self,
        src_id: str,
        dst_id: str,
        edge_type: str,
        weight: float = 1.0,
        metadata: dict | None = None,
    ) -> DirectedEdge:
        """Create (or replace) a directed typed edge from src to dst.

        Reserved edge types are documented in ``KNOWN_EDGE_TYPES``; the API
        does not enforce them so callers can experiment with new relations.
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        if src_id == dst_id:
            raise ValueError("Self-relations are not allowed")
        edge = DirectedEdge(
            src=src_id, dst=dst_id, edge_type=edge_type,
            weight=weight, created_at=time.time(), metadata=metadata,
        )
        await self.store.upsert_directed_edge(edge)
        # Phase J Stage 1: mirror into the in-memory cache so persona
        # traversal in the next recall sees the new edge without waiting
        # for a cache reload.
        self.cache.set_directed_edge(src_id, dst_id, edge_type)
        return edge

    async def unrelate(
        self, src_id: str, dst_id: str, edge_type: str | None = None,
    ) -> int:
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        deleted = await self.store.delete_directed_edge(src_id, dst_id, edge_type)
        if deleted > 0:
            self.cache.remove_directed_edge(src_id, dst_id, edge_type)
        return deleted

    async def get_relations(
        self,
        node_id: str,
        edge_type: str | None = None,
        direction: str = "out",
    ) -> list[DirectedEdge]:
        return await self.store.get_directed_edges(
            node_id=node_id, edge_type=edge_type, direction=direction,
        )

    # --- F7: Emotional weight & certainty ---

    async def revalidate(
        self,
        node_id: str,
        certainty: float | None = None,
        emotion: float | None = None,
    ) -> NodeState | None:
        """Stamp a node with fresh certainty/emotion (re-verification ritual).

        ``certainty`` updates last_verified_at; pass without value to just
        refresh the timestamp at the existing certainty level.
        Returns the updated state or None if the node doesn't exist.
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        state = self.cache.get_node(node_id)
        if state is None:
            states = await self.store.get_node_states([node_id])
            state = states.get(node_id)
        if state is None or state.is_archived:
            return None
        if certainty is not None:
            state.certainty = max(0.0, min(1.0, certainty))
        if emotion is not None:
            state.emotion_weight = max(-1.0, min(1.0, emotion))
        state.last_verified_at = time.time()
        self.cache.set_node(state, dirty=True)
        return state

    # --- F2 / F2.1: Clustering, Collision, Compaction ---

    def _virtual_position_for(self, node_id: str) -> np.ndarray | None:
        """Best-effort virtual position (original embedding + displacement)."""
        embs = self.faiss_index.get_vectors([node_id])
        original = embs.get(node_id)
        if original is None:
            return None
        state = self.cache.get_node(node_id)
        temperature = state.temperature if state else 0.0
        displacement = self.cache.get_displacement(node_id)
        return compute_virtual_position(original, displacement, temperature)

    def _active_virtual_positions(
        self, *, top_n_by_mass: int | None = None,
    ) -> dict[str, np.ndarray]:
        """Collect virtual positions for non-archived nodes.

        ``top_n_by_mass`` restricts to the heaviest N nodes (cheaper for large
        memories; matches the "hot topic neighborhood" heuristic in the plan).
        """
        nodes = [n for n in self.cache.get_all_nodes() if not n.is_archived]
        if top_n_by_mass is not None and len(nodes) > top_n_by_mass:
            nodes = sorted(nodes, key=lambda s: s.mass, reverse=True)[:top_n_by_mass]
        out: dict[str, np.ndarray] = {}
        for state in nodes:
            pos = self._virtual_position_for(state.id)
            if pos is not None:
                out[state.id] = pos
        return out

    def find_duplicates(
        self, *, threshold: float = 0.95, top_n_by_mass: int | None = 500,
    ) -> list[Cluster]:
        """Detect near-duplicate clusters among active memories."""
        positions = self._active_virtual_positions(top_n_by_mass=top_n_by_mass)
        return cluster_by_similarity(positions, threshold=threshold)

    async def merge(self, node_ids: list[str], keep: str | None = None) -> list[MergeOutcome]:
        """Manual collision: collapse the given IDs into one survivor.

        If ``keep`` is given, that ID survives. Otherwise the heaviest among
        ``node_ids`` is chosen (ties broken by recency).
        Returns one ``MergeOutcome`` per absorbed node.
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        unique_ids = list(dict.fromkeys(node_ids))
        if len(unique_ids) < 2:
            return []

        states_by_id: dict[str, NodeState] = {}
        for nid in unique_ids:
            state = self.cache.get_node(nid)
            if state is None:
                states = await self.store.get_node_states([nid])
                state = states.get(nid)
            if state is None or state.is_archived:
                continue
            states_by_id[nid] = state

        if len(states_by_id) < 2:
            return []

        if keep is not None and keep in states_by_id:
            survivor = states_by_id.pop(keep)
            keep_explicit = True
        else:
            ordered = sorted(
                states_by_id.values(),
                key=lambda s: (s.mass, s.last_access),
                reverse=True,
            )
            survivor = ordered[0]
            states_by_id.pop(survivor.id)
            keep_explicit = False

        outcomes: list[MergeOutcome] = []
        now = time.time()
        for absorbed in states_by_id.values():
            if not keep_explicit:
                # Auto-pick the heavier body so mass conservation feels right.
                # When the caller explicitly passed keep=, that intent wins —
                # otherwise Phase-G mass perturbations could silently override
                # the user's choice.
                survivor, _ = pick_survivor(survivor, absorbed)
                if survivor.id == absorbed.id:
                    survivor, absorbed = absorbed, survivor
            outcome = merge_pair(survivor, absorbed, self.cache, self.config, now=now)
            outcomes.append(outcome)
            # Evict absorbed from cache after marking dirty so flush persists state
            await self.cache.flush_to_store(self.store)
            self.cache.evict_node(absorbed.id)
            # Phase L Stage 1: drop absorbed from BM25 so the survivor wins
            # all lexical searches (the absorbed content is now redundant).
            if self.bm25_index is not None:
                self.bm25_index.remove([absorbed.id])
        if outcomes:
            self.prefetch_cache.invalidate()
        return outcomes

    async def compact(
        self,
        *,
        expire_ttl: bool = True,
        rebuild_faiss: bool = True,
        auto_merge: bool = False,
        merge_threshold: float = 0.95,
        merge_top_n: int = 500,
    ) -> dict:
        """Periodic maintenance: TTL expiry, FAISS rebuild, optional auto-merge.

        Returns a dict report of what changed:
            {
              "expired": int,
              "merged_pairs": int,
              "faiss_rebuilt": bool,
              "vectors_before": int,
              "vectors_after": int,
            }
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        report = {
            "expired": 0,
            "merged_pairs": 0,
            "faiss_rebuilt": False,
            "vectors_before": self.faiss_index.size,
            "vectors_after": self.faiss_index.size,
        }

        if expire_ttl:
            now = time.time()
            n = await self.store.expire_due_nodes(now)
            expired_ids: list[str] = []
            for state in list(self.cache.get_all_nodes()):
                if state.expires_at is not None and state.expires_at <= now:
                    expired_ids.append(state.id)
                    self.cache.evict_node(state.id)
            # Phase L Stage 1: drop expired ids from BM25 active stats.
            if self.bm25_index is not None and expired_ids:
                self.bm25_index.remove(expired_ids)
            report["expired"] = n

        if auto_merge:
            positions = self._active_virtual_positions(top_n_by_mass=merge_top_n)
            candidates = find_merge_candidates(positions, threshold=merge_threshold)
            done_pairs = 0
            absorbed_ids: set[str] = set()
            for a, b, _sim in candidates:
                if a in absorbed_ids or b in absorbed_ids:
                    continue
                outcomes = await self.merge([a, b])
                if outcomes:
                    done_pairs += len(outcomes)
                    for o in outcomes:
                        absorbed_ids.add(o.absorbed_id)
            report["merged_pairs"] = done_pairs

        if rebuild_faiss:
            await self._rebuild_faiss_index()
            report["faiss_rebuilt"] = True
            # Phase L Stage 1: rebuild BM25 together with FAISS so the
            # lexical index also reclaims postings from forget/merge/expire
            # and re-syncs with the SQLite content (covers any drift from
            # other processes that wrote to the DB while this one was idle).
            if self.bm25_index is not None:
                await self._rebuild_bm25_from_store()
        report["vectors_after"] = self.faiss_index.size

        # Drop orphan directed edges (endpoints hard-deleted by the user)
        all_relations = await self.store.get_directed_edges()
        valid_ids = {state.id for state in self.cache.get_all_nodes()}
        orphan_count = 0
        for edge in all_relations:
            if edge.src not in valid_ids and edge.dst not in valid_ids:
                await self.store.delete_directed_edge(edge.src, edge.dst, edge.edge_type)
                orphan_count += 1
        report["orphan_relations_removed"] = orphan_count

        await self.cache.flush_to_store(self.store)
        # Compaction may have changed scoring — invalidate prefetch cache
        self.prefetch_cache.invalidate()
        return report

    async def _rebuild_faiss_index(self) -> None:
        """Rebuild FAISS from all active cache nodes.

        Nodes already in FAISS have their vectors extracted directly.
        Nodes in cache but absent from FAISS (write-behind gap from a previous
        session that ended before the flush fired) are re-embedded from store
        so they are no longer invisible to recall.
        """
        active_ids = [
            state.id for state in self.cache.get_all_nodes() if not state.is_archived
        ]
        if not active_ids:
            self.faiss_index.reset()
            self._faiss_dirty = True
            return
        vecs = self.faiss_index.get_vectors(active_ids)
        present = [(nid, vecs[nid]) for nid in active_ids if nid in vecs]

        # Re-embed nodes that exist in cache/store but are absent from FAISS.
        missing_ids = [nid for nid in active_ids if nid not in vecs]
        recovered: list[tuple[str, np.ndarray]] = []
        if missing_ids:
            logger.info(
                "_rebuild_faiss_index: re-embedding %d nodes missing from FAISS",
                len(missing_ids),
            )
            contents: list[str] = []
            valid_ids: list[str] = []
            for nid in missing_ids:
                doc = await self.store.get_document(nid)
                if doc is not None:
                    contents.append(doc["content"])
                    valid_ids.append(nid)
            if contents:
                re_vecs = self.embedder.encode_documents(contents)
                for nid, vec in zip(valid_ids, re_vecs):
                    recovered.append((nid, vec))
            logger.info(
                "_rebuild_faiss_index: recovered %d/%d missing nodes",
                len(recovered),
                len(missing_ids),
            )

        all_pairs = present + recovered
        if not all_pairs:
            return
        matrix = np.stack([v for _, v in all_pairs]).astype(np.float32)
        # H1: build a fresh index, then swap the reference in a single
        # atomic assignment. The previous reset()+add() left a window where
        # self.faiss_index.ntotal == 0; a concurrent recall landing in that
        # window got an empty seed pool — a silent degraded result during a
        # routine compact. A lone attribute store is atomic under the GIL,
        # so no searcher observes a partial index: in-flight searches that
        # already captured the old reference finish against the old,
        # fully-valid index, and subsequent ones see the complete new one.
        new_index = FaissIndex(
            dimension=self.config.embedding_dim,
            lock_enabled=self.config.faiss_index_lock_enabled,
        )
        new_index.add(matrix, [nid for nid, _ in all_pairs])
        self.faiss_index = new_index
        self._faiss_dirty = True
        if self.virtual_faiss_index is not None:
            await self._rebuild_virtual_faiss_index()

    async def _rebuild_virtual_faiss_index(self) -> None:
        """Build the virtual FAISS index from raw embeddings + cached
        displacement (Phase H Stage 4).

        Uses ``compute_virtual_position`` for each active node so that
        Phase G priming (which moves displacement on every active node)
        becomes seedable. Without this index, raw FAISS top-K never sees
        priming-induced cluster shifts."""
        if self.virtual_faiss_index is None:
            return
        active_ids = [
            s.id for s in self.cache.get_all_nodes() if not s.is_archived
        ]
        if not active_ids:
            self.virtual_faiss_index.reset()
            return
        raw_vecs = self.faiss_index.get_vectors(active_ids)
        virtual_vectors: list[np.ndarray] = []
        virtual_ids: list[str] = []
        for nid in active_ids:
            original = raw_vecs.get(nid)
            if original is None:
                continue
            displacement = self.cache.get_displacement(nid)
            state = self.cache.get_node(nid)
            temperature = state.temperature if state is not None else 0.0
            virtual_pos = compute_virtual_position(
                original, displacement, temperature,
            )
            virtual_vectors.append(virtual_pos)
            virtual_ids.append(nid)
        if not virtual_vectors:
            self.virtual_faiss_index.reset()
            return
        matrix = np.stack(virtual_vectors).astype(np.float32)
        # H1: atomic swap, same rationale as _rebuild_faiss_index — no
        # concurrent seed step ever sees an empty virtual index mid-compact.
        new_virtual = FaissIndex(
            dimension=self.config.embedding_dim,
            lock_enabled=self.config.faiss_index_lock_enabled,
        )
        new_virtual.add(matrix, virtual_ids)
        self.virtual_faiss_index = new_virtual
        logger.info(
            "Virtual FAISS rebuilt: %d active vectors", len(virtual_ids),
        )

    async def _build_bm25_from_store(self) -> None:
        """Phase L Stage 1: Initial BM25 build at startup.

        Loads every document content from SQLite and adds the active ones
        (those present in ``cache.node_cache`` — archived/expired ids are
        skipped) to the in-memory BM25 index. Decision D2 dictates that
        Stage 1 has no disk persistence, so this rebuild happens on every
        startup. The cost is proportional to total content length; at 24k
        documents it completes in a few seconds.
        """
        if self.bm25_index is None and self.ambient_gate_index is None:
            return
        contents = await self.store.get_all_contents()
        active_ids: list[str] = []
        active_texts: list[str] = []
        for nid, text in contents.items():
            if nid in self.cache.node_cache and text:
                active_ids.append(nid)
                active_texts.append(text)
        if not active_ids:
            return
        if self.bm25_index is not None:
            self.bm25_index.add(active_ids, active_texts)
        # Ambient Recall Enrichment: the word-level gate index is built from
        # the same content scan. Sudachi tokenisation is slower than the
        # char-trigram default, so this adds to startup time on a large corpus.
        if self.ambient_gate_index is not None:
            self.ambient_gate_index.add(active_ids, active_texts)
        logger.info(
            "BM25 index built: %d active docs (skipped %d archived/missing)",
            len(active_ids), len(contents) - len(active_ids),
        )

    async def _rebuild_bm25_from_store(self) -> None:
        """Phase L Stage 1: Full BM25 rebuild during compact.

        Drops in-memory state and rebuilds from SQLite — reclaims postings
        from forget/merge/expire and re-syncs with content written by other
        processes (the multi-process visibility caveat in CLAUDE.md).
        """
        if self.bm25_index is None and self.ambient_gate_index is None:
            return
        if self.bm25_index is not None:
            self.bm25_index.reset()
        if self.ambient_gate_index is not None:
            self.ambient_gate_index.reset()
        await self._build_bm25_from_store()

    # --- Phase M Stage 1: orbital-state reset (legacy BH residue cleanup) ---

    async def reset_orbital_state(self) -> int:
        """Clear displacement + velocity for every node — wipes the
        runtime residue of the legacy co-occurrence BH (which pulled
        nodes toward neighbor centroids before Phase M replaced it with
        the mass-threshold BH). Mass is **not** touched (see
        ``reset_masses`` for that).

        Destructive: also loses Phase G genesis kicks and Phase I/J
        query-attraction accumulation. The maintainer migration path for
        rolling Phase M out on a DB that ran under the old physics; not
        a runtime operation.

        Flushes any pending cache writes first, performs the SQL update,
        then clears the in-memory caches and invalidates the prefetch
        cache (displacement change invalidates every cached recall) plus
        marks virtual FAISS dirty so the next save loop rebuilds it from
        the now-zero displacement state.
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        await self.cache.flush_to_store(self.store)
        affected = await self.store.reset_orbital_state()
        self.cache.displacement_cache.clear()
        self.cache.velocity_cache.clear()
        self.cache.dirty_displacements.clear()
        self.cache.dirty_velocities.clear()
        self.cache.virtual_faiss_dirty = True
        self.prefetch_cache.invalidate()
        logger.info(
            "Orbital state reset: %d nodes cleared (displacement + velocity)",
            affected,
        )
        return affected

    # --- Phase Q2: velocity-only cooldown ---

    async def reset_velocities(self) -> int:
        """Clear every node's velocity, keeping displacement intact.

        Phase Q2 one-time cooldown of the degenerate, clamp-saturated momentum
        field (the pre-Q2 over-scaled neighbour gravity pushed ~99% of nodes'
        velocity to the clamp during recalls; that momentum lost its EMA
        meaning). Displacement — the learned positions / query-attraction
        integral — is preserved, unlike ``reset_orbital_state`` which wipes
        both. Velocity is a regenerable derivative of the field, so the
        (rescaled) gravity re-derives a correct, small velocity the next time
        a node participates in dynamics.

        Does NOT invalidate the prefetch cache or mark virtual FAISS dirty:
        velocity affects only future mutation, never the current ranking
        (which reads displacement) or the virtual-FAISS positions.

        Flushes pending cache writes first, performs the SQL update, then
        clears the in-memory velocity cache + dirty set.
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        await self.cache.flush_to_store(self.store)
        affected = await self.store.reset_velocities()
        self.cache.velocity_cache.clear()
        self.cache.dirty_velocities.clear()
        logger.info("Velocity cooldown: %d nodes cleared (velocity only)", affected)
        return affected

    # --- Phase M Stage 1: Mass-only reset ---

    async def reset_masses(self, value: float = 1.0) -> int:
        """Reset every node's mass to ``value`` (default 1.0), keeping
        displacement / velocity / edges / cohort_id / source intact.

        This is the maintainer hook for rolling out Phase M Stage 1 on a
        live database that accumulated mass under the old "internal trade"
        rule: switch the flag on, kill other connected processes, run
        ``reset_masses()``, restart. The new rule then accretes mass from
        a clean baseline.

        Flushes any pending cache writes first so they don't clobber the
        reset, performs the SQL update, then mirrors the new value into the
        in-memory cache and invalidates the prefetch cache (mass change
        invalidates every cached recall ranking).
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        await self.cache.flush_to_store(self.store)
        affected = await self.store.reset_masses(value)
        for state in self.cache.node_cache.values():
            state.mass = value
        self.prefetch_cache.invalidate()
        logger.info("Mass reset: %d nodes set to mass=%s", affected, value)
        return affected

    # --- Phase M follow-up: warm displacement from velocity ---

    async def warm_displacement(
        self, overwrite: bool = False,
    ) -> dict[str, int]:
        """Seed ``displacement = velocity`` (one orbital timestep) on every
        active node that has velocity but no meaningful displacement.

        Why this exists: M004 (corpus-scale cosmic-bang) writes velocity to
        every active node but leaves displacement NULL by design — the
        dream loop and natural recall events were supposed to fill it in
        over time. In practice that takes ~20 hours of continuous uptime
        for a 24k-node corpus and stalls across server restarts, so most
        nodes sit at ``velocity ≠ 0`` / ``displacement = NULL`` for days
        and visualisations show "velocity arrows but the position never
        moves". This one-shot pass takes the same step the dream loop
        would have taken on its first visit (``new_disp = old_disp +
        new_vel`` with ``old_disp = 0``) and applies it everywhere at
        once.

        Default (``overwrite=False``) leaves nodes that already have a
        non-zero displacement alone, so naturally-accumulated history
        (Phase G genesis kicks, Phase I/J query attraction, dream loop
        ticks since M004) is preserved. ``overwrite=True`` forces
        ``displacement = velocity`` on every active node with velocity
        — useful immediately after a fresh M002/M004 cycle.

        Returns a dict ``{seeded, skipped_no_velocity, skipped_already_displaced,
        active_total}`` so callers can verify how the corpus shifted.
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        tol = 1e-6
        seeded = 0
        skipped_no_velocity = 0
        skipped_already_displaced = 0
        active_total = 0
        for state in self.cache.get_all_nodes():
            if state.is_archived:
                continue
            active_total += 1
            v = self.cache.get_velocity(state.id)
            if v is None or float(np.linalg.norm(v)) < tol:
                skipped_no_velocity += 1
                continue
            if not overwrite:
                d = self.cache.get_displacement(state.id)
                if d is not None and float(np.linalg.norm(d)) >= tol:
                    skipped_already_displaced += 1
                    continue
            self.cache.set_displacement(state.id, v.astype(np.float32).copy())
            seeded += 1

        if seeded > 0:
            await self.cache.flush_to_store(self.store)
            self.prefetch_cache.invalidate()
            logger.info(
                "Warm displacement: seeded %d / %d active nodes "
                "(skipped %d no-velocity, %d already-displaced)",
                seeded, active_total,
                skipped_no_velocity, skipped_already_displaced,
            )
        return {
            "seeded": seeded,
            "skipped_no_velocity": skipped_no_velocity,
            "skipped_already_displaced": skipped_already_displaced,
            "active_total": active_total,
        }

    # --- US5: State Reset ---

    async def reset(self) -> tuple[int, int]:
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        nodes_count = len(self.cache.node_cache)
        edges_count = len(self.cache.get_all_edges())

        self.cache.reset()
        self.graph.reset()
        nodes_reset, edges_removed = await self.store.reset_dynamic_state()

        # Hardening Stage 1 / C4 — every other destructive op invalidates
        # the prefetch cache (forget/merge/compact/reset_masses/
        # warm_displacement); reset() was the sole omission. Without this a
        # `recall` matching a cached (text, k) key keeps returning the
        # pre-reset ranked list for up to prefetch_ttl_seconds, so the wipe
        # silently appears not to have taken effect. Also mark the virtual
        # FAISS dirty for parity with reset_orbital_state — the displacement
        # field that fed it is now gone.
        self.prefetch_cache.invalidate()
        self.cache.virtual_faiss_dirty = True

        logger.info("Reset: %d nodes, %d edges removed", nodes_reset, edges_removed)
        return nodes_count, edges_count
