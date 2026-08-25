"""Phase T Stage 6 — canonical MMR selection for explore's diversified
presentation (pure functions; ``engine.query`` wires them into Step 4).

Contract (docs/wiki/Plans-Phase-T-Semantic-Requalification.md §3 Stage 6):

- relevance: min-max normalized pool ``final_score`` → [0, 1]; a
  degenerate pool (all-equal, single candidate) maps every candidate to
  1.0 so a tied pool is never ranked by an artifact of the normalization.
- redundancy: max cosine (raw embeddings) against the *selected* set.
  Forced items occupy presentation slots, so they enter the selected set
  for redundancy even though MMR never reorders them.
- cohort penalty: ``diversity × explore_cohort_penalty`` when the
  candidate's cluster key (``cohort_id`` OR ``original_id`` — the same
  structural identity Phase M uses, no source branching) already appears
  in the selected set. A ``None`` cluster key never matches: singletons
  are intrinsically diverse (same rule as
  ``services.memory._cluster_key_for``, mirrored here because core must
  not import services).
- ties resolve to the earlier candidate — the engine passes candidates
  in final_score-descending order, so a tie keeps the higher-relevance
  item (stable greedy).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import numpy as np


def normalize_relevance(final_scores: Mapping[str, float]) -> dict[str, float]:
    """Min-max normalize pool final scores to [0, 1].

    ``max == min`` (all-equal pool, single candidate) → every candidate
    maps to 1.0: relevance must not differentiate what is tied.
    """
    if not final_scores:
        return {}
    lo = min(final_scores.values())
    hi = max(final_scores.values())
    if hi <= lo:
        return {nid: 1.0 for nid in final_scores}
    span = hi - lo
    return {nid: (s - lo) / span for nid, s in final_scores.items()}


def apply_relevance_floor(
    candidates: Sequence[str],
    raw_cosines: Mapping[str, float],
    floor: float,
) -> list[str]:
    """Keep only candidates whose measured raw cosine clears ``floor``.

    An id with no measured cosine is excluded — absence of evidence
    cannot certify relevance. Order is preserved (the engine passes
    candidates in presentation order).
    """
    return [
        nid for nid in candidates
        if raw_cosines.get(nid, float("-inf")) >= floor
    ]


def cluster_key_from_cache(cache, node_id: str) -> str | None:
    """Structural cluster identity: ``cohort_id`` OR ``original_id``.

    Duck-typed over the cache layer (``get_cohort`` / ``get_original``)
    so core never imports services. ``None`` = singleton-class identity.
    """
    return cache.get_cohort(node_id) or cache.get_original(node_id)


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return float(np.dot(a, b)) / (na * nb)


def mmr_select(
    candidates: Sequence[str],
    relevance: Mapping[str, float],
    embeddings: Mapping[str, np.ndarray],
    *,
    diversity: float,
    cohort_penalty: float,
    cluster_key_of: Callable[[str], str | None],
    n_select: int,
    preselected: Sequence[str] = (),
) -> list[str]:
    """Greedy canonical MMR over ``candidates``.

    ``score_i = λ·rel_i − (1−λ)·red_i − diversity·cohort_penalty·[cluster hit]``
    with ``λ = 1 − 0.5·diversity`` and ``red_i`` the max raw-embedding
    cosine against the selected set. ``preselected`` (forced items)
    anchors both redundancy and cluster keys from the start but is never
    returned. An id without an entry in ``embeddings`` cannot have its
    redundancy computed and is excluded instead of raising: candidates
    are dropped from selection, while a preselected id keeps its
    cluster-key anchor (cluster identity comes from the cache, not the
    embedding) but no longer anchors redundancy. Iteration keeps the
    first candidate on ties (stable against the engine's
    final_score-desc input order).
    """
    lambda_ = 1.0 - 0.5 * diversity
    selected = [nid for nid in preselected if nid in embeddings]
    selected_cluster_keys = {
        key for key in (cluster_key_of(nid) for nid in preselected)
        if key is not None
    }
    remaining = [nid for nid in candidates if nid in embeddings]
    picks: list[str] = []
    n_select = max(0, n_select)
    while remaining and len(picks) < n_select:
        best_nid: str | None = None
        best_score = float("-inf")
        for nid in remaining:
            red = max(
                (_cosine(embeddings[nid], embeddings[s]) for s in selected),
                default=0.0,
            )
            key = cluster_key_of(nid)
            penalty = (
                cohort_penalty
                if key is not None and key in selected_cluster_keys
                else 0.0
            )
            score = (
                lambda_ * relevance.get(nid, 0.0)
                - (1.0 - lambda_) * red
                - diversity * penalty
            )
            if score > best_score:
                best_score = score
                best_nid = nid
        assert best_nid is not None
        picks.append(best_nid)
        selected.append(best_nid)
        key = cluster_key_of(best_nid)
        if key is not None:
            selected_cluster_keys.add(key)
        remaining.remove(best_nid)
    return picks
