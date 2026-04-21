import numpy as np

from gaottt.core.clustering import (
    cluster_by_similarity,
    find_merge_candidates,
)


def _unit(*v: float) -> np.ndarray:
    arr = np.array(v, dtype=np.float32)
    arr /= np.linalg.norm(arr) + 1e-9
    return arr


def test_empty_input_returns_empty():
    assert cluster_by_similarity({}) == []
    assert find_merge_candidates({}) == []


def test_groups_only_pairs_above_threshold():
    embs = {
        "a": _unit(1.0, 0.0),
        "b": _unit(0.99, 0.14),  # very close to a
        "c": _unit(0.0, 1.0),     # orthogonal
    }
    clusters = cluster_by_similarity(embs, threshold=0.95)
    assert len(clusters) == 1
    assert set(clusters[0].ids) == {"a", "b"}
    assert clusters[0].avg_pairwise_similarity > 0.95


def test_threshold_can_isolate_everything():
    embs = {
        "a": _unit(1.0, 0.0),
        "b": _unit(0.99, 0.14),
    }
    assert cluster_by_similarity(embs, threshold=0.999) == []


def test_three_node_chain_collapses_via_union_find():
    # a-b similar, b-c similar, but a-c below threshold — union-find still groups all
    embs = {
        "a": _unit(1.0, 0.0, 0.0),
        "b": _unit(0.96, 0.28, 0.0),
        "c": _unit(0.85, 0.5, 0.15),
    }
    clusters = cluster_by_similarity(embs, threshold=0.94)
    assert len(clusters) == 1
    assert set(clusters[0].ids) == {"a", "b", "c"}


def test_find_merge_candidates_sorted_by_similarity_desc():
    embs = {
        "a": _unit(1.0, 0.0),
        "b": _unit(0.9999, 0.014),  # very close to a
        "c": _unit(0.96, 0.28),       # somewhat close to a
    }
    pairs = find_merge_candidates(embs, threshold=0.9)
    assert pairs[0][2] >= pairs[-1][2]
    # The closest pair (a,b) ranks first
    assert {pairs[0][0], pairs[0][1]} == {"a", "b"}
