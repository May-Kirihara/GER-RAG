"""Phase T Stage 6 — explore MMR pure functions (unit).

Covers the canonical MMR selection contract from
docs/wiki/Plans-Phase-T-Semantic-Requalification.md §3 Stage 6:

- ``normalize_relevance`` — pool final_score min-max normalization to
  [0, 1]; a degenerate (all-equal / single-candidate) pool maps every
  candidate to 1.0 so relevance cannot rank what is tied
- ``apply_relevance_floor`` — lateral candidates must clear the raw
  cosine floor; an id with no measured cosine can never certify
  relevance and is excluded
- ``mmr_select`` — greedy canonical MMR:
  ``score_i = λ·rel_i − (1−λ)·red_i − diversity·cohort_penalty·[cluster hit]``
  with ``λ = 1 − 0.5·diversity``; redundancy is measured against the
  *selected* set (preselected forced items included — they occupy
  slots); ties resolve to the earlier candidate (stable, engine passes
  candidates in final_score-desc order)
"""
from __future__ import annotations

import numpy as np
import pytest

from gaottt.core.diversity import (
    apply_relevance_floor,
    mmr_select,
    normalize_relevance,
)


def _emb(*coords: float) -> np.ndarray:
    v = np.array(coords, dtype=np.float64)
    return v / np.linalg.norm(v)


# orthogonal basis
E_A = _emb(1.0, 0.0, 0.0)
E_B = _emb(0.0, 1.0, 0.0)
E_C = _emb(0.0, 0.0, 1.0)
# near-duplicate of E_A (cos ≈ 0.98)
E_A_DUP = _emb(0.98, 0.199, 0.0)


def _no_cluster(node_id: str) -> str | None:
    return None


# --- normalize_relevance -------------------------------------------------


def test_normalize_typical_minmax():
    rel = normalize_relevance({"a": 0.2, "b": 0.6, "c": 1.0})
    assert rel["a"] == pytest.approx(0.0)
    assert rel["b"] == pytest.approx(0.5)
    assert rel["c"] == pytest.approx(1.0)


def test_normalize_all_equal_maps_to_one():
    # all-equal pool: relevance must not differentiate (plan: max==min →
    # every candidate rel = 1.0)
    rel = normalize_relevance({"a": 0.5, "b": 0.5, "c": 0.5})
    assert rel == {"a": 1.0, "b": 1.0, "c": 1.0}


def test_normalize_single_candidate():
    rel = normalize_relevance({"only": 0.3})
    assert rel == {"only": 1.0}


# --- apply_relevance_floor ------------------------------------------------


def test_floor_keeps_only_qualified_candidates():
    kept = apply_relevance_floor(
        ["a", "b", "c"], {"a": 0.5, "b": 0.44, "c": 0.45}, 0.45,
    )
    # boundary is inclusive; order is preserved
    assert kept == ["a", "c"]


def test_floor_excludes_unmeasured_cosine():
    kept = apply_relevance_floor(["a", "ghost"], {"a": 0.9}, 0.45)
    assert kept == ["a"]


# --- mmr_select ------------------------------------------------------------


def test_mmr_avoids_redundant_candidate():
    # rel: A=1.0, A_DUP=0.95, B=0.8 — plain relevance picks A then A_DUP,
    # but A_DUP is a near-duplicate of the already-picked A.
    candidates = ["A", "A_DUP", "B"]
    rel = {"A": 1.0, "A_DUP": 0.95, "B": 0.8}
    embeddings = {"A": E_A, "A_DUP": E_A_DUP, "B": E_B}
    picked = mmr_select(
        candidates, rel, embeddings,
        diversity=0.8, cohort_penalty=0.05,
        cluster_key_of=_no_cluster, n_select=2,
    )
    assert picked == ["A", "B"]


def test_mmr_lambda_one_is_relevance_order():
    # diversity=0.0 → λ=1: the redundancy term vanishes entirely and the
    # greedy reduces to relevance order (the bypass-equivalent behaviour).
    candidates = ["A", "A_DUP", "B"]
    rel = {"A": 1.0, "A_DUP": 0.95, "B": 0.8}
    embeddings = {"A": E_A, "A_DUP": E_A_DUP, "B": E_B}
    picked = mmr_select(
        candidates, rel, embeddings,
        diversity=0.0, cohort_penalty=0.05,
        cluster_key_of=_no_cluster, n_select=3,
    )
    assert picked == ["A", "A_DUP", "B"]


def test_mmr_cohort_penalty_scales_with_diversity():
    # cand "same" shares cluster "k" with the preselected forced item;
    # "other" is a singleton. At low diversity the relevance gap (0.9 vs
    # 0.75) survives the penalty; at diversity=1.0 the penalty flips it.
    rel = {"same": 0.9, "other": 0.75}
    embeddings = {"same": E_A, "other": E_B, "forced": E_C}
    cluster_keys = {"same": "k", "other": None, "forced": "k"}

    picked_low = mmr_select(
        ["same", "other"], rel, embeddings,
        diversity=0.4, cohort_penalty=0.2,
        cluster_key_of=lambda nid: cluster_keys[nid],
        n_select=1, preselected=["forced"],
    )
    assert picked_low == ["same"]

    picked_high = mmr_select(
        ["same", "other"], rel, embeddings,
        diversity=1.0, cohort_penalty=0.2,
        cluster_key_of=lambda nid: cluster_keys[nid],
        n_select=1, preselected=["forced"],
    )
    assert picked_high == ["other"]


def test_mmr_preselected_counts_for_redundancy_but_never_picked():
    # "dup" has the highest relevance but is a near-duplicate of the
    # preselected (forced) item — redundancy against the forced set must
    # demote it below the orthogonal "alt".
    rel = {"dup": 1.0, "alt": 0.9}
    embeddings = {"dup": E_A_DUP, "alt": E_B, "forced": E_A}
    picked = mmr_select(
        ["dup", "alt"], rel, embeddings,
        diversity=0.8, cohort_penalty=0.05,
        cluster_key_of=_no_cluster, n_select=2, preselected=["forced"],
    )
    assert picked == ["alt", "dup"]
    assert "forced" not in picked


def test_mmr_tie_breaks_by_input_order():
    # identical relevance and identical embeddings: the earlier candidate
    # (higher final_score in the engine contract) must win.
    rel = {"first": 0.7, "second": 0.7}
    embeddings = {"first": E_A, "second": E_A}
    picked = mmr_select(
        ["first", "second"], rel, embeddings,
        diversity=0.8, cohort_penalty=0.05,
        cluster_key_of=_no_cluster, n_select=1,
    )
    assert picked == ["first"]


def test_mmr_n_select_zero_and_short_pool():
    rel = {"a": 1.0}
    embeddings = {"a": E_A}
    assert mmr_select(
        ["a"], rel, embeddings,
        diversity=0.8, cohort_penalty=0.05,
        cluster_key_of=_no_cluster, n_select=0,
    ) == []
    # pool smaller than n_select → returns the whole pool
    assert mmr_select(
        ["a"], rel, embeddings,
        diversity=0.8, cohort_penalty=0.05,
        cluster_key_of=_no_cluster, n_select=5,
    ) == ["a"]


def test_mmr_zero_norm_embedding_is_safe():
    # zero-vector embedding must not raise (cosine guard → 0.0)
    rel = {"z": 1.0, "b": 0.9}
    embeddings = {"z": np.zeros(3), "b": E_B}
    picked = mmr_select(
        ["z", "b"], rel, embeddings,
        diversity=0.8, cohort_penalty=0.05,
        cluster_key_of=_no_cluster, n_select=2,
    )
    assert set(picked) == {"z", "b"}


def test_mmr_skips_candidate_missing_embedding():
    # an id absent from the embeddings map cannot have its redundancy
    # computed — excluded from selection instead of raising KeyError
    # (even when it carries the highest relevance)
    rel = {"ghost": 1.0, "real": 0.9}
    embeddings = {"real": E_B}  # "ghost" has no measured embedding
    picked = mmr_select(
        ["ghost", "real"], rel, embeddings,
        diversity=0.8, cohort_penalty=0.05,
        cluster_key_of=_no_cluster, n_select=2,
    )
    assert picked == ["real"]


def test_mmr_preselected_missing_embedding_keeps_cluster_anchor():
    # a preselected (forced) id without an embedding cannot anchor
    # redundancy, but its cluster identity comes from the cache — the
    # cohort penalty against same-cluster candidates must survive.
    # diversity=1.0, cohort_penalty=0.2, λ=0.5:
    #   score(same)  = 0.5·0.9 − 1.0·0.2 = 0.25  (cluster "k" hit)
    #   score(other) = 0.5·0.75         = 0.375
    rel = {"same": 0.9, "other": 0.75}
    embeddings = {"same": E_A, "other": E_B}  # "forced" missing
    cluster_keys = {"same": "k", "other": None, "forced": "k"}
    picked = mmr_select(
        ["same", "other"], rel, embeddings,
        diversity=1.0, cohort_penalty=0.2,
        cluster_key_of=lambda nid: cluster_keys[nid],
        n_select=1, preselected=["forced"],
    )
    assert picked == ["other"]
