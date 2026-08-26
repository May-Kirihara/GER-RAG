"""Phase U WP-3 — ambient composite gate: pure decision primitives.

R3 の根因は RURI cosine の狭帯 (production raw cosine が ~0.70-0.86 に集中)
により **絶対閾値では off-topic を拒否できない** こと。本 module は
ambient gate の composite 判定

::

    accept = bm25_strong
          OR ( virt_percentile >= percentile_min
               AND top_margin    >= margin_min
               AND raw_top1      >= raw_floor )

を **engine に依存しない純関数** として提供する (unit-test 可能にするため。
runtime glue は ``services.memory``)。3 軸の意味:

- ``virt_percentile`` — 参照分布 (較正 query population の top-1 virtual
  cosine 分布) に対する empirical CDF percentile。狭帯の正規化 (分布を
  [0,100] に写像) であり、判別は labeled 閾値選定で担保する。
- ``top_margin`` — pool top-1 virtual − pool virtual median。pool 全体が
  同じ高い帯に張り付く off-topic (penguin 事例の virt 0.835 vs 狭帯) を
  拒否するための相対軸。
- ``raw_top1`` — engine 自身の raw FAISS 検索による top-1 cosine。
  ``expose_score_breakdown`` に **非依存** (Phase T の「raw 軸欠損」問題
  の再発防止契約)。

fail-closed 契約: 参照 artifact 欠損・破損・fingerprint 不一致・count drift
時は BM25 のみが accept 経路になる (既知 false-positive 経路の "or" への
open fallback は禁止)。理由は ``composite_signal`` /
``empty_reason`` に離散値で現れる:

- ``composite_reject``             — semantic composite を評価したが閾値未達
- ``composite_pool_too_small``     — pool < 2 で margin が未定義
- ``composite_reference_unavailable`` — 参照 artifact が使えない (fail-closed)

しきい値の実行時の権威は **config** (``ambient_semantic_percentile_min`` 等)。
artifact 内の ``thresholds`` は較正時の推奨値の echo (provenance 専用) で
あり、runtime は参照しない — tuning は常に一本道 (env / config) に保つ。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Artifact schema contract。format 名の改名は既存 artifact の孤立 (= 全
# composite fail-closed) を意味するため、定数を pin して test で守る。
COMPOSITE_ARTIFACT_FORMAT = "gaottt-ambient-composite-reference"
COMPOSITE_ARTIFACT_SCHEMA_VERSION = 1

# discrete accept/reject signals (AmbientGateDiagnostics.composite_signal
# および empty_reason の新値としてそのまま使う)
SIGNAL_BM25_STRONG = "bm25_strong"
SIGNAL_SEMANTIC_COMPOSITE = "semantic_composite"
SIGNAL_COMPOSITE_REJECT = "composite_reject"
SIGNAL_COMPOSITE_POOL_TOO_SMALL = "composite_pool_too_small"
SIGNAL_COMPOSITE_REFERENCE_UNAVAILABLE = "composite_reference_unavailable"


class CompositeReferenceError(ValueError):
    """Reference artifact が読めない / schema 契約を満たさない。

    呼び出し側 (services.memory) はこれを捕捉して fail-closed
    (BM25-only) に落とす — 例外を上へ伝播させて ambient recall を
    落とすことはない。
    """


@dataclass(frozen=True)
class CompositeGateThresholds:
    """composite 判定の 3 閾値 (config 由来)。

    ``percentile_min`` は [0, 100]。``margin_min`` / ``raw_floor`` は
    cosine スケール。
    """

    percentile_min: float
    margin_min: float
    raw_floor: float


@dataclass(frozen=True)
class CompositeVerdict:
    """composite gate の判定結果。

    ``signal`` は accept なら ``bm25_strong`` / ``semantic_composite``、
    reject なら拒否理由 (empty_reason と同じ離散値)。``virt_percentile``
    / ``margin`` / ``raw_top1`` は拒否時も **計算可能な限り** 埋まる
    (silence triage 用 — なぜ落ちたかを axes から読めるように)。
    """

    accepted: bool
    signal: str
    reason: str | None = None          # empty_reason 値 (reject のときのみ)
    virt_percentile: float | None = None
    margin: float | None = None
    raw_top1: float | None = None
    detail: str | None = None          # human-readable triage 補足


@dataclass(frozen=True)
class CompositeReference:
    """検証済み参照 artifact の内容 (loader が schema 検証を済ませたもの)。"""

    schema_version: int
    embedder_id: str
    embedder_version: str
    corpus_digest: str
    active_count: int
    virt_top1_distribution: list[float]
    # provenance 専用の echo。runtime の閾値権威は config 側。
    thresholds_echo: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)


def percentile_of(value: float, reference: list[float]) -> float:
    """Empirical-CDF percentile of ``value`` in [0, 100] with midrank ties.

    ``P = 100 · (#{r < v} + 0.5·#{r == v}) / n`` — 同順位は中央順位に
    割り振る (標準的な midrank)。empty reference は呼び出し側バグ
    (decision は empty を unavailable として先に弾く) なので ValueError。
    """
    if not reference:
        raise ValueError("percentile_of: reference distribution is empty")
    if not math.isfinite(value):
        raise ValueError("percentile_of: value must be finite")
    below = sum(1 for r in reference if r < value)
    tied = sum(1 for r in reference if r == value)
    return 100.0 * (below + 0.5 * tied) / len(reference)


def compute_corpus_digest(
    contents: dict[str, str], active_ids: Iterable[str],
) -> tuple[str, int]:
    """sha256 over sorted ``(id, sha256(content))`` of ACTIVE nodes.

    WP-3 fingerprint 契約: timestamp 系 (count + max(updated_at) 等) は
    content 変更を取りこぼすため使わない (WP-6d と同一の collision 指摘
    対応)。active 判定は ``engine.cache.node_cache`` の membership
    (``_build_bm25_from_store`` と同一規則)。空 content の doc は除外。
    戻り値は ``(digest_hex, active_count)``。
    """
    h = hashlib.sha256()
    count = 0
    for nid in sorted(active_ids):
        text = contents.get(nid)
        if not text:
            continue
        per = hashlib.sha256(text.encode("utf-8")).hexdigest()
        h.update(f"{nid}:{per}\n".encode("utf-8"))
        count += 1
    return h.hexdigest(), count


def composite_gate_decision(
    *,
    bm25_top: float | None,
    bm25_threshold: float,
    virt_top1: float | None,
    virt_median: float | None,
    raw_top1: float | None,
    reference: list[float] | None,
    thresholds: CompositeGateThresholds,
    pool_size: int,
) -> CompositeVerdict:
    """Pure composite judgment — 評価順序は契約の一部。

    1. 非有限入力 (NaN/Inf) は全て reject — ``inf`` が閾値を貫くのを
       数値比較より先に断つ (fail-closed)。
    2. ``bm25_strong`` は参照 artifact に依存せず accept する
       (fail-closed 時の唯一の accept 経路として機能するよう、参照
       チェックより前に置く)。
    3. 参照なし → ``composite_reference_unavailable`` (fail-closed)。
    4. pool < 2 → margin 未定義 → ``composite_pool_too_small``。
    5. raw 軸欠損 (None) → reject (breakdown 非依存の自前検索でも
       FAISS 空なら欠ける)。
    6. 3 軸 (percentile / margin / raw_floor) 全て ``>=`` で通れば
       ``semantic_composite`` accept。
    """
    def _reject(
        signal: str, *, pct: float | None = None, margin: float | None = None,
        raw: float | None = None, detail: str | None = None,
    ) -> CompositeVerdict:
        return CompositeVerdict(
            accepted=False, signal=signal, reason=signal,
            virt_percentile=pct, margin=margin, raw_top1=raw, detail=detail,
        )

    # (1) finiteness — provided inputs only (None axes are handled below).
    named: list[tuple[str, float | None]] = [
        ("bm25_top", bm25_top),
        ("virt_top1", virt_top1),
        ("virt_median", virt_median),
        ("raw_top1", raw_top1),
        ("thresholds.percentile_min", thresholds.percentile_min),
        ("thresholds.margin_min", thresholds.margin_min),
        ("thresholds.raw_floor", thresholds.raw_floor),
        ("bm25_threshold", bm25_threshold),
    ]
    for name, v in named:
        if v is not None and not math.isfinite(v):
            return _reject(
                SIGNAL_COMPOSITE_REJECT, detail=f"non-finite input: {name}",
            )

    # triage axes — computed when the inputs allow, regardless of verdict.
    pct: float | None = None
    margin: float | None = None
    if virt_top1 is not None and virt_median is not None:
        margin = virt_top1 - virt_median
    if reference and virt_top1 is not None:
        pct = percentile_of(virt_top1, reference)

    # (2) BM25 arm — independent of the reference (fail-closed anchor).
    if bm25_top is not None and bm25_top >= bm25_threshold:
        return CompositeVerdict(
            accepted=True, signal=SIGNAL_BM25_STRONG,
            virt_percentile=pct, margin=margin, raw_top1=raw_top1,
        )

    # (3) fail-closed: no usable reference → BM25 was the only accept path.
    if not reference:
        return _reject(
            SIGNAL_COMPOSITE_REFERENCE_UNAVAILABLE, pct=pct, margin=margin,
            raw=raw_top1, detail="reference artifact unavailable/invalid",
        )

    # (4) margin is undefined on a pool smaller than 2.
    if pool_size < 2:
        return _reject(
            SIGNAL_COMPOSITE_POOL_TOO_SMALL, pct=pct, margin=margin,
            raw=raw_top1, detail=f"pool_size={pool_size}",
        )

    if virt_top1 is None or virt_median is None:
        return _reject(
            SIGNAL_COMPOSITE_REJECT, pct=pct, margin=margin, raw=raw_top1,
            detail="virtual axis missing",
        )

    # (5) raw axis — breakdown-independent own FAISS search; None = unusable.
    if raw_top1 is None:
        return _reject(
            SIGNAL_COMPOSITE_REJECT, pct=pct, margin=margin, raw=None,
            detail="raw axis unavailable (empty FAISS index)",
        )

    # (6) semantic composite — all three axes must clear (>= semantics).
    failed: list[str] = []
    if pct < thresholds.percentile_min:
        failed.append(f"pct {pct:.1f} < {thresholds.percentile_min:.1f}")
    if margin < thresholds.margin_min:
        failed.append(f"margin {margin:.4f} < {thresholds.margin_min:.4f}")
    if raw_top1 < thresholds.raw_floor:
        failed.append(f"raw {raw_top1:.4f} < {thresholds.raw_floor:.4f}")
    if failed:
        return _reject(
            SIGNAL_COMPOSITE_REJECT, pct=pct, margin=margin, raw=raw_top1,
            detail="; ".join(failed),
        )
    return CompositeVerdict(
        accepted=True, signal=SIGNAL_SEMANTIC_COMPOSITE,
        virt_percentile=pct, margin=margin, raw_top1=raw_top1,
    )


# --- Reference artifact (schema v1) ---------------------------------------------

_THRESHOLD_ECHO_KEYS = (
    "ambient_bm25_min_score",
    "ambient_semantic_percentile_min",
    "ambient_margin_min",
    "ambient_raw_floor_composite",
)


def build_artifact_payload(
    *,
    embedder_id: str,
    embedder_version: str,
    corpus_digest: str,
    active_count: int,
    virt_top1_distribution: list[float],
    thresholds: dict[str, Any],
    provenance: dict[str, Any],
    created_at: float | None = None,
) -> dict[str, Any]:
    """Build the reference-artifact payload dict (schema v1).

    ``thresholds`` は **推奨値の echo** (provenance)。runtime の閾値権威は
    config 側なので、loader は key の存在のみ検証する。
    """
    return {
        "format": COMPOSITE_ARTIFACT_FORMAT,
        "schema_version": COMPOSITE_ARTIFACT_SCHEMA_VERSION,
        "created_at": created_at if created_at is not None else time.time(),
        "fingerprint": {
            "embedder_id": embedder_id,
            "embedder_version": embedder_version,
            "corpus_digest": corpus_digest,
            "active_count": active_count,
        },
        "reference_distribution": {
            "virt_top1": [float(v) for v in virt_top1_distribution],
        },
        "thresholds": dict(thresholds),
        "provenance": dict(provenance),
    }


def write_reference_artifact(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write the artifact (tmp + ``os.replace``).

    manifest.py の atomic-write 規約と同じ: reader は torn file を
    見ない。失敗時は scratch を除去して元の例外を伝播。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def load_composite_reference(path: Path) -> CompositeReference:
    """Load + validate a reference artifact.

    契約違反 (欠損 / 破損 / schema 不一致 / 非有限分布 / count<=0) は全て
    :class:`CompositeReferenceError` — 呼び出し側は fail-closed に落とす。
    schema_version 内の未知 key は無視 (forward-compat)。
    """
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CompositeReferenceError(f"reference artifact missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CompositeReferenceError(
            f"reference artifact unreadable: {path} ({exc})",
        ) from exc
    if not isinstance(data, dict):
        raise CompositeReferenceError("artifact root must be a JSON object")
    if data.get("format") != COMPOSITE_ARTIFACT_FORMAT:
        raise CompositeReferenceError(
            f"artifact format mismatch: {data.get('format')!r} != "
            f"{COMPOSITE_ARTIFACT_FORMAT!r}",
        )
    if data.get("schema_version") != COMPOSITE_ARTIFACT_SCHEMA_VERSION:
        raise CompositeReferenceError(
            f"artifact schema_version mismatch: {data.get('schema_version')!r} "
            f"!= {COMPOSITE_ARTIFACT_SCHEMA_VERSION!r}",
        )
    fp = data.get("fingerprint")
    if not isinstance(fp, dict):
        raise CompositeReferenceError("artifact fingerprint block missing")
    for key in ("embedder_id", "embedder_version", "corpus_digest"):
        if not isinstance(fp.get(key), str) or not fp.get(key):
            raise CompositeReferenceError(
                f"artifact fingerprint.{key} missing/invalid",
            )
    active_count = fp.get("active_count")
    if not isinstance(active_count, int) or isinstance(active_count, bool) \
            or active_count <= 0:
        raise CompositeReferenceError(
            f"artifact fingerprint.active_count invalid: {active_count!r}",
        )
    dist_block = data.get("reference_distribution")
    if not isinstance(dist_block, dict):
        raise CompositeReferenceError("reference_distribution block missing")
    dist = dist_block.get("virt_top1")
    if not isinstance(dist, list) or not dist:
        raise CompositeReferenceError(
            "reference_distribution.virt_top1 must be a non-empty list",
        )
    for v in dist:
        if not isinstance(v, (int, float)) or isinstance(v, bool) \
                or not math.isfinite(float(v)):
            raise CompositeReferenceError(
                f"non-finite value in reference distribution: {v!r}",
            )
    thresholds = data.get("thresholds")
    if not isinstance(thresholds, dict) or any(
        k not in thresholds for k in _THRESHOLD_ECHO_KEYS
    ):
        raise CompositeReferenceError(
            "thresholds echo missing required keys: "
            f"{list(_THRESHOLD_ECHO_KEYS)}",
        )
    provenance = data.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    return CompositeReference(
        schema_version=int(data["schema_version"]),
        embedder_id=fp["embedder_id"],
        embedder_version=fp["embedder_version"],
        corpus_digest=fp["corpus_digest"],
        active_count=active_count,
        virt_top1_distribution=[float(v) for v in dist],
        thresholds_echo=dict(thresholds),
        provenance=provenance,
    )
