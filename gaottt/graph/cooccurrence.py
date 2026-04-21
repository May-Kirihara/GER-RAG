from __future__ import annotations

import time
from collections import defaultdict
from itertools import combinations

from gaottt.config import GaOTTTConfig
from gaottt.store.cache import CacheLayer


class CooccurrenceGraph:
    def __init__(self, config: GaOTTTConfig, cache: CacheLayer):
        self._config = config
        self._cache = cache
        self._cooccurrence_counts: dict[tuple[str, str], int] = defaultdict(int)

    def update_cooccurrence(self, result_ids: list[str]) -> None:
        for id_a, id_b in combinations(result_ids, 2):
            key = (min(id_a, id_b), max(id_a, id_b))
            self._cooccurrence_counts[key] += 1
            current_weight = self._cache.get_neighbors(key[0]).get(key[1], 0.0)
            edge_exists = current_weight > 0.0
            if edge_exists or self._cooccurrence_counts[key] >= self._config.edge_threshold:
                self._cache.set_edge(key[0], key[1], current_weight + 1.0)

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
