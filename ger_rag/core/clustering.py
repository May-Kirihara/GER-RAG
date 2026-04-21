"""Non-destructive similarity clustering — F2.

Group nodes whose virtual positions (or raw embeddings) lie within
a cosine-similarity threshold. Used for:

  - reflect(aspect="duplicates") to surface near-duplicate memories
  - recall(suggest_clusters=True) to flag returns that share a cluster
  - feeding engine.compact(auto_merge=True) candidate pairs to collide

Implementation: O(N^2) brute-force pairwise scan with a union-find
collapse. This is fine for ``N`` up to a few thousand; for larger
populations restrict ``ids`` to a hot-topic subset before calling.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Cluster:
    """A group of node IDs deemed near-duplicate to each other."""
    ids: tuple[str, ...]
    centroid_norm: float
    avg_pairwise_similarity: float


class _UnionFind:
    def __init__(self, items: list[str]):
        self._parent: dict[str, str] = {x: x for x in items}

    def find(self, x: str) -> str:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for x in self._parent:
            r = self.find(x)
            out.setdefault(r, []).append(x)
        return out


def cluster_by_similarity(
    embeddings: dict[str, np.ndarray],
    threshold: float = 0.95,
    *,
    min_cluster_size: int = 2,
) -> list[Cluster]:
    """Find clusters of IDs whose pairwise cosine similarity ≥ threshold.

    The embeddings are assumed L2-normalized; cosine ≡ dot product.
    Returns clusters of size ≥ ``min_cluster_size``, sorted by descending size.
    """
    if not embeddings:
        return []
    ids = list(embeddings.keys())
    matrix = np.stack([embeddings[i] for i in ids]).astype(np.float32)

    sims = matrix @ matrix.T
    np.fill_diagonal(sims, 0.0)

    uf = _UnionFind(ids)
    pair_sims: dict[tuple[str, str], float] = {}
    n = len(ids)
    for i in range(n):
        for j in range(i + 1, n):
            s = float(sims[i, j])
            if s >= threshold:
                a, b = ids[i], ids[j]
                uf.union(a, b)
                key = (a, b) if a < b else (b, a)
                pair_sims[key] = s

    raw_groups = uf.groups()
    clusters: list[Cluster] = []
    for members in raw_groups.values():
        if len(members) < min_cluster_size:
            continue
        idxs = [ids.index(m) for m in members]
        sub = matrix[idxs]
        centroid = sub.mean(axis=0)
        centroid_norm = float(np.linalg.norm(centroid))
        # average pairwise similarity within the cluster
        if len(members) >= 2:
            sub_sims = sub @ sub.T
            triu = np.triu_indices(len(members), k=1)
            avg_sim = float(sub_sims[triu].mean())
        else:
            avg_sim = 1.0
        clusters.append(
            Cluster(
                ids=tuple(sorted(members)),
                centroid_norm=centroid_norm,
                avg_pairwise_similarity=avg_sim,
            )
        )
    clusters.sort(key=lambda c: (-len(c.ids), -c.avg_pairwise_similarity))
    return clusters


def find_merge_candidates(
    embeddings: dict[str, np.ndarray],
    threshold: float = 0.95,
) -> list[tuple[str, str, float]]:
    """Return (a, b, similarity) triples for every pair above threshold.

    Used by engine.compact(auto_merge=True). Sorted by descending similarity
    so the strongest candidates are merged first.
    """
    if len(embeddings) < 2:
        return []
    ids = list(embeddings.keys())
    matrix = np.stack([embeddings[i] for i in ids]).astype(np.float32)
    sims = matrix @ matrix.T
    np.fill_diagonal(sims, 0.0)
    pairs: list[tuple[str, str, float]] = []
    n = len(ids)
    for i in range(n):
        for j in range(i + 1, n):
            s = float(sims[i, j])
            if s >= threshold:
                pairs.append((ids[i], ids[j], s))
    pairs.sort(key=lambda t: t[2], reverse=True)
    return pairs
