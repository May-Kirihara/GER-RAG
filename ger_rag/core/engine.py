from __future__ import annotations

import hashlib
import logging
import time
import uuid

import numpy as np

from ger_rag.config import GERConfig
from ger_rag.core.gravity import (
    apply_displacement_decay,
    compute_virtual_position,
    propagate_gravity_wave,
    update_displacements_for_cooccurrence,
)
from ger_rag.core.scorer import (
    compute_decay,
    compute_mass_boost,
)
from ger_rag.core.types import (
    CooccurrenceEdge,
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

    async def startup(self) -> None:
        await self.store.initialize()
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
            state = NodeState(id=doc_id, last_access=now)
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
    ) -> list[QueryResultItem]:
        k = top_k or self.config.top_k
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
            if state is None:
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

            final = gravity_sim * decay + mass_boost + wave_boost

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

        # Step 4: Sort and take top-K
        results.sort(key=lambda r: r.final_score, reverse=True)
        results = results[:k]

        # Step 5: Post-query state update
        result_ids = [r.id for r in results]
        self._update_state_after_query(result_ids, reached, original_embs, now)

        return results

    def _update_state_after_query(
        self,
        result_ids: list[str],
        reached: dict[str, float],
        original_embs: dict[str, np.ndarray],
        now: float,
    ) -> None:
        masses = {}
        for node_id in result_ids:
            state = self.cache.get_node(node_id)
            if state is None:
                continue

            raw_force = reached.get(node_id, 0.0)

            # Mass update with logistic saturation
            state.mass += self.config.eta * raw_force * (1.0 - state.mass / self.config.m_max)

            # Sim history ring buffer (use force as proxy for relevance)
            state.sim_history.append(raw_force)
            if len(state.sim_history) > self.config.sim_buffer_size:
                state.sim_history = state.sim_history[-self.config.sim_buffer_size:]

            # Temperature = gamma * variance(sim_history)
            if len(state.sim_history) >= 2:
                arr = np.array(state.sim_history)
                state.temperature = self.config.gamma * float(np.var(arr))
            else:
                state.temperature = 0.0

            state.last_access = now
            self.cache.set_node(state, dirty=True)
            masses[node_id] = state.mass

        # Co-occurrence graph update
        if result_ids:
            self.graph.update_cooccurrence(result_ids)

        # Gravitational displacement update
        if len(result_ids) >= 2:
            current_displacements = {}
            for nid in result_ids:
                if nid not in original_embs:
                    continue
                cached = self.cache.get_displacement(nid)
                if cached is not None:
                    current_displacements[nid] = cached
                else:
                    current_displacements[nid] = np.zeros(self.config.embedding_dim, dtype=np.float32)

            for nid in current_displacements:
                state = self.cache.get_node(nid)
                if state is not None:
                    current_displacements[nid] = apply_displacement_decay(
                        current_displacements[nid],
                        self.config.displacement_decay,
                        state.last_access,
                        now,
                        self.config.displacement_age_delta,
                    )

            updated = update_displacements_for_cooccurrence(
                [nid for nid in result_ids if nid in original_embs],
                original_embs,
                current_displacements,
                masses,
                self.config,
            )

            for nid, disp in updated.items():
                self.cache.set_displacement(nid, disp)

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

    # --- US5: State Reset ---

    async def reset(self) -> tuple[int, int]:
        nodes_count = len(self.cache.node_cache)
        edges_count = len(self.cache.get_all_edges())

        self.cache.reset()
        self.graph.reset()
        nodes_reset, edges_removed = await self.store.reset_dynamic_state()

        logger.info("Reset: %d nodes, %d edges removed", nodes_reset, edges_removed)
        return nodes_count, edges_count
