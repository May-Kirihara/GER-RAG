from __future__ import annotations

import hashlib
import logging
import time
import uuid

import numpy as np

from ger_rag.config import GERConfig
from ger_rag.core.clustering import Cluster, cluster_by_similarity, find_merge_candidates
from ger_rag.core.collision import MergeOutcome, merge_pair, pick_survivor
from ger_rag.core.gravity import (
    compute_virtual_position,
    propagate_gravity_wave,
    update_orbital_state,
)
from ger_rag.core.prefetch import PrefetchCache, PrefetchPool
from ger_rag.core.scorer import (
    compute_certainty_boost,
    compute_decay,
    compute_emotion_boost,
    compute_mass_boost,
)
from ger_rag.core.types import (
    CooccurrenceEdge,
    DirectedEdge,
    NodeState,
    QueryResultItem,
)
from ger_rag.embedding.ruri import RuriEmbedder
from ger_rag.graph.cooccurrence import CooccurrenceGraph
from ger_rag.index.faiss_index import FaissIndex
from ger_rag.store.cache import CacheLayer
from ger_rag.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)


class GEREngine:
    def __init__(
        self,
        config: GERConfig,
        embedder: RuriEmbedder,
        faiss_index: FaissIndex,
        cache: CacheLayer,
        store: SqliteStore,
    ):
        self.config = config
        self.embedder = embedder
        self.faiss_index = faiss_index
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

    async def startup(self) -> None:
        await self.store.initialize()
        expired = await self.store.expire_due_nodes(time.time())
        if expired:
            logger.info("Auto-expired %d nodes past their TTL", expired)
        await self.cache.load_from_store(self.store)
        self.faiss_index.load(self.config.faiss_index_path)
        self.cache.start_write_behind(self.store)
        logger.info(
            "Engine started: %d nodes cached, %d vectors indexed, %d displacements",
            len(self.cache.node_cache),
            self.faiss_index.size,
            len(self.cache.displacement_cache),
        )

    async def shutdown(self) -> None:
        await self.prefetch_pool.drain(timeout=5.0)
        await self.cache.stop_write_behind()
        await self.cache.flush_to_store(self.store)
        self.faiss_index.save(self.config.faiss_index_path)
        await self.store.close()
        logger.info("Engine shut down, state persisted")

    # --- US1: Document Indexing ---

    async def index_documents(
        self, documents: list[dict],
    ) -> list[str]:
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

        vectors = self.embedder.encode_documents(contents)
        self.faiss_index.add(vectors, ids)

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
            docs_for_store.append({
                "id": doc_id,
                "content": contents[i],
                "metadata": metadatas[i],
            })

        await self.store.save_documents(docs_for_store)
        await self.cache.flush_to_store(self.store)

        logger.info("Indexed %d documents", len(ids))
        return ids

    # --- US2: Query (Gravity Wave Propagation) ---

    async def query(
        self,
        text: str,
        top_k: int | None = None,
        wave_depth: int | None = None,
        wave_k: int | None = None,
        use_cache: bool = False,
    ) -> list[QueryResultItem]:
        """Run a recall query.

        ``use_cache=True`` consults the prefetch cache first; on hit the
        cached results are returned without re-running embedding/wave/scoring
        (and crucially, without re-applying simulation updates — the prefetch
        already paid that cost). Cache hits are bounded by
        ``config.prefetch_ttl_seconds``.
        """
        k = top_k or self.config.top_k
        if use_cache:
            cached = self.prefetch_cache.get(text, k)
            if cached is not None:
                return cached
        results = await self._query_internal(
            text=text, top_k=k, wave_depth=wave_depth, wave_k=wave_k,
        )
        if use_cache:
            self.prefetch_cache.put(text, k, results)
        return results

    async def _query_internal(
        self,
        text: str,
        top_k: int,
        wave_depth: int | None,
        wave_k: int | None,
    ) -> list[QueryResultItem]:
        k = top_k
        query_vec = self.embedder.encode_query(text)

        # Step 1: Gravity wave propagation — recursive neighbor expansion
        reached = propagate_gravity_wave(
            query_vec, self.faiss_index, self.cache, self.config,
            wave_k=wave_k, wave_depth=wave_depth,
        )

        if not reached:
            return []

        # Step 2: Get original embeddings for all reached nodes
        reached_ids = list(reached.keys())
        original_embs = self.faiss_index.get_vectors(reached_ids)

        # Step 3: Score all reached nodes with virtual coordinates + wave boost
        now = time.time()
        query_vec_flat = query_vec[0] if query_vec.ndim == 2 else query_vec
        results: list[QueryResultItem] = []

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

            results.append(
                QueryResultItem(
                    id=node_id,
                    content=doc["content"],
                    metadata=doc.get("metadata"),
                    raw_score=gravity_sim,
                    final_score=final,
                )
            )

        # Step 4: Sort and take top-K for presentation to LLM
        results.sort(key=lambda r: r.final_score, reverse=True)
        results = results[:k]

        # Step 5: Update return_count for presented nodes + habituation recovery for all
        result_ids = [r.id for r in results]
        for node_id in result_ids:
            state = self.cache.get_node(node_id)
            if state:
                state.return_count += 1.0
                self.cache.set_node(state, dirty=True)

        # Habituation recovery: all reached nodes slowly recover freshness
        all_reached_ids = list(reached.keys())
        for node_id in all_reached_ids:
            state = self.cache.get_node(node_id)
            if state and state.return_count > 0:
                state.return_count *= (1.0 - self.config.habituation_recovery_rate)
                self.cache.set_node(state, dirty=True)

        # Step 6: Simulation update — ALL reached nodes
        self._update_simulation(all_reached_ids, reached, original_embs, now)
        self._update_cooccurrence(result_ids)

        return results

    def _update_simulation(
        self,
        all_reached_ids: list[str],
        reached: dict[str, float],
        original_embs: dict[str, np.ndarray],
        now: float,
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

            # Mass update scaled by force
            state.mass += self.config.eta * force * (1.0 - state.mass / self.config.m_max)

            # Sim history ring buffer
            state.sim_history.append(force)
            if len(state.sim_history) > self.config.sim_buffer_size:
                state.sim_history = state.sim_history[-self.config.sim_buffer_size:]

            # Temperature
            if len(state.sim_history) >= 2:
                arr = np.array(state.sim_history)
                state.temperature = self.config.gamma * float(np.var(arr))
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
        if not node_ids:
            return 0
        await self.cache.flush_to_store(self.store)
        affected = await self.store.set_archived(node_ids, archived=True)
        for nid in node_ids:
            self.cache.evict_node(nid)
        if affected:
            self.prefetch_cache.invalidate()
        logger.info("Archived %d nodes", affected)
        return affected

    async def restore(self, node_ids: list[str]) -> int:
        """Un-archive nodes and reload them into the cache."""
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
                self.cache.displacement_cache[nid] = disp
            for nid, vel in vels.items():
                self.cache.velocity_cache[nid] = vel
            self.prefetch_cache.invalidate()
        logger.info("Restored %d nodes", affected)
        return affected

    async def forget(self, node_ids: list[str], hard: bool = False) -> int:
        """Forget nodes. hard=False archives them (reversible); hard=True
        physically removes them from the store. Vectors in the FAISS index
        are not removed (rebuild on next reset), but archived nodes are
        filtered out at query time.
        """
        if not node_ids:
            return 0
        if not hard:
            return await self.archive(node_ids)
        await self.cache.flush_to_store(self.store)
        for nid in node_ids:
            self.cache.evict_node(nid)
        deleted = await self.store.hard_delete_nodes(node_ids)
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
    ) -> object:
        """Schedule a background recall and cache its result.

        Returns the asyncio.Task handle (mostly opaque to callers; tests can
        ``await`` it for determinism). The next ``query(text, top_k,
        use_cache=True)`` within ``prefetch_ttl_seconds`` will be served from
        the cache without re-running the simulation.
        """
        k = top_k or self.config.top_k

        async def _run() -> list[QueryResultItem]:
            results = await self._query_internal(
                text=text, top_k=k, wave_depth=wave_depth, wave_k=wave_k,
            )
            self.prefetch_cache.put(text, k, results)
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
        if src_id == dst_id:
            raise ValueError("Self-relations are not allowed")
        edge = DirectedEdge(
            src=src_id, dst=dst_id, edge_type=edge_type,
            weight=weight, created_at=time.time(), metadata=metadata,
        )
        await self.store.upsert_directed_edge(edge)
        return edge

    async def unrelate(
        self, src_id: str, dst_id: str, edge_type: str | None = None,
    ) -> int:
        return await self.store.delete_directed_edge(src_id, dst_id, edge_type)

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
        else:
            ordered = sorted(
                states_by_id.values(),
                key=lambda s: (s.mass, s.last_access),
                reverse=True,
            )
            survivor = ordered[0]
            states_by_id.pop(survivor.id)

        outcomes: list[MergeOutcome] = []
        now = time.time()
        for absorbed in states_by_id.values():
            survivor, _ = pick_survivor(survivor, absorbed)  # ensure heavier wins overall
            if survivor.id == absorbed.id:
                # pick_survivor flipped — swap
                survivor, absorbed = absorbed, survivor
            outcome = merge_pair(survivor, absorbed, self.cache, self.config, now=now)
            outcomes.append(outcome)
            # Evict absorbed from cache after marking dirty so flush persists state
            await self.cache.flush_to_store(self.store)
            self.cache.evict_node(absorbed.id)
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
            for state in list(self.cache.get_all_nodes()):
                if state.expires_at is not None and state.expires_at <= now:
                    self.cache.evict_node(state.id)
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
        """Drop archived/merged vectors from FAISS by rebuilding the flat index."""
        active_ids = [
            state.id for state in self.cache.get_all_nodes() if not state.is_archived
        ]
        if not active_ids:
            self.faiss_index.reset()
            return
        vecs = self.faiss_index.get_vectors(active_ids)
        present = [(nid, vecs[nid]) for nid in active_ids if nid in vecs]
        if not present:
            return
        matrix = np.stack([v for _, v in present]).astype(np.float32)
        self.faiss_index.reset()
        self.faiss_index.add(matrix, [nid for nid, _ in present])

    # --- US5: State Reset ---

    async def reset(self) -> tuple[int, int]:
        nodes_count = len(self.cache.node_cache)
        edges_count = len(self.cache.get_all_edges())

        self.cache.reset()
        self.graph.reset()
        nodes_reset, edges_removed = await self.store.reset_dynamic_state()

        logger.info("Reset: %d nodes, %d edges removed", nodes_reset, edges_removed)
        return nodes_count, edges_count
