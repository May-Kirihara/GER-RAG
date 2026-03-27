from __future__ import annotations

import time
from collections import defaultdict
from itertools import combinations

from ger_rag.config import GERConfig
from ger_rag.store.cache import CacheLayer


class CooccurrenceGraph:
    def __init__(self, config: GERConfig, cache: CacheLayer):
        self._config = config
        self._cache = cache
        self._cooccurrence_counts: dict[tuple[str, str], int] = defaultdict(int)

    def update_cooccurrence(self, result_ids: list[str]) -> None:
        for id_a, id_b in combinations(result_ids, 2):
            key = (min(id_a, id_b), max(id_a, id_b))
            self._cooccurrence_counts[key] += 1
            if self._cooccurrence_counts[key] >= self._config.edge_threshold:
                current_weight = self._cache.get_neighbors(key[0]).get(key[1], 0.0)
                new_weight = current_weight + 1.0
                self._cache.set_edge(key[0], key[1], new_weight)

    def compute_graph_boost(
        self,
        node_id: str,
        candidate_scores: dict[str, float],
    ) -> float:
        neighbors = self._cache.get_neighbors(node_id)
        if not neighbors:
            return 0.0
        boost = 0.0
        for neighbor_id, weight in neighbors.items():
            if neighbor_id in candidate_scores:
                boost += weight * candidate_scores[neighbor_id]
        return self._config.rho * boost

    def decay_and_prune(self) -> None:
        all_edges = self._cache.get_all_edges()
        for edge in all_edges:
            new_weight = edge.weight * self._config.edge_decay
            if new_weight < self._config.prune_threshold:
                self._cache.remove_edge(edge.src, edge.dst)
            else:
                self._cache.set_edge(edge.src, edge.dst, new_weight)

        self._enforce_degree_cap()

    def _enforce_degree_cap(self) -> None:
        for node_id, neighbors in list(self._cache.graph_cache.items()):
            if len(neighbors) > self._config.max_degree:
                sorted_neighbors = sorted(neighbors.items(), key=lambda x: x[1])
                to_remove = sorted_neighbors[: len(neighbors) - self._config.max_degree]
                for neighbor_id, _ in to_remove:
                    self._cache.remove_edge(node_id, neighbor_id)

    def reset(self) -> None:
        self._cooccurrence_counts.clear()
