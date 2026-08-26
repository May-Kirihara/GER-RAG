"""Phase U §10 R3 follow-up — ambient composite gate: pure decision primitives.

R3 の根因は RURI cosine の狭帯 (production raw cosine が ~0.70-0.86 に集中)
により **絶対閾値では off-topic を拒否できない** こと。v2 較正
(``docs/notes/phase-u/ambient-composite-calibration.md``) で positives
(virt 0.819-0.909) と negatives (0.786-0.848) の重なる狭帯では単一
semantic 軸 (percentile/margin) で incident query と negative 最高値が
不可分と判明したため、判定は **3-arm 構造** (plan §10, 2026-08-26
事前登録) に置換した::

    accept = bm25_strong (bm25_top >= bm25_strong_threshold)
          OR (virt_top1 >= virt_hi)
          OR (bm25_top >= bm25_mid AND virt_top1 >= virt_mid)

軸ごとに強みが異なる query (incident は bm25 中堅、言い換え positive は
virt 高位) を別々の arm で拾う。本 module はこの判定を **engine に依存
しない純関数** として提供する (unit-test 可能にするため。runtime glue は
``services.memory``)。

fail-closed 契約: 参照 artifact 欠損・破損・fingerprint 不一致・count drift
時は BM25 のみが accept 経路になる (既知 false-positive 経路の "or" への
open fallback は禁止)。参照分布は 3-arm の判定には **使われない** が、
較正 provenance と corpus drift 検出のため artifact 機構は維持する。
理由は ``composite_signal`` / ``empty_reason`` に離散値で現れる:

- ``composite_reject``             — 3-arm すべて閾値未達
- ``composite_pool_too_small``     — pool < 2 (pool 統計を信頼しない契約)
- ``composite_reference_unavailable`` — 参照 artifact が使えない (fail-closed)

accept 時の ``composite_signal`` は発火した arm 名 (``bm25_strong`` /
``virt_hi`` / ``bm25_virt_mid``) — diagnostics が accept 経路を読める。

しきい値の実行時の権威は **config** (``ambient_composite_virt_hi`` 等)。
artifact 内の ``thresholds`` は較正時の推奨値の echo (provenance 専用)
であり、runtime は参照しない — tuning は常に一本道 (env / config) に
保つ。
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
# schema_version は plan §10 の指示どおり維持 (v2 旧 percentile/margin/
# raw_floor echo の artifact は thresholds echo key 検証で fail-closed
# になる = 再較正要求)。
COMPOSITE_ARTIFACT_FORMAT = "gaottt-ambient-composite-reference"
COMPOSITE_ARTIFACT_SCHEMA_VERSION = 1

# discrete accept/reject signals (AmbientGateDiagnostics.composite_signal
# および empty_reason の新値としてそのまま使う)。accept 側は 3-arm の
# どの arm が発火したかを示す。
SIGNAL_BM25_STRONG = "bm25_strong"
SIGNAL_VIRT_HI = "virt_hi"
SIGNAL_BM25_VIRT_MID = "bm25_virt_mid"
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
    """3-arm 判定の閾値 (config 由来)。

    ``virt_hi`` / ``virt_mid`` は virtual cosine スケール、``bm25_mid``
    は gate index の BM25 score スケール (arm1 の bm25_strong_threshold
    (= ``ambient_bm25_min_score``) とは別値)。
    """

    virt_hi: float
    bm25_mid: float
    virt_mid: float


@dataclass(frozen=True)
class CompositeVerdict:
    """composite gate の判定結果。

    ``signal`` は accept なら発火 arm (``bm25_strong`` / ``virt_hi`` /
    ``bm25_virt_mid``)、reject なら拒否理由 (empty_reason と同じ離散値)。
    ``virt_top1`` / ``bm25_top`` は判定入力の echo で、拒否時も
    **計算可能な限り** 埋まる (silence triage 用 — なぜ落ちたかを
    軸から読めるように)。
    """

    accepted: bool
    signal: str
    reason: str | None = None          # empty_reason 値 (reject のときのみ)
    virt_top1: float | None = None
    bm25_top: float | None = None
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
    reference_available: bool,
    thresholds: CompositeGateThresholds,
    pool_size: int,
) -> CompositeVerdict:
    """Pure 3-arm judgment — 評価順序は契約の一部。

    1. 非有限入力 (NaN/Inf) は全て reject — ``inf`` が閾値を貫くのを
       数値比較より先に断つ (fail-closed)。
    2. ``bm25_strong`` は参照 artifact に依存せず accept する
       (fail-closed 時の唯一の accept 経路として機能するよう、参照
       チェックより前に置く)。
    3. 参照なし → ``composite_reference_unavailable`` (fail-closed)。
       3-arm 自体は参照分布の値を使わないが、較正 provenance /
       corpus drift 検出の契約として semantic arm 2/3 は参照が有効な
       場合にのみ発火する。
    4. pool < 2 → ``composite_pool_too_small`` (pool 統計を信頼しない
       edge-case 契約、plan §10 で維持)。
    5. virtual 軸欠損 (None) → reject (両 semantic arm の前提)。
    6. arm 2 (``virt_hi``) → arm 3 (``bm25_virt_mid``) の順に判定し、
       先に発火した arm を signal として報告 (両方発火時はより強い
       semantic-only arm = ``virt_hi`` 優先)。
    """
    def _reject(
        signal: str, *, detail: str | None = None,
    ) -> CompositeVerdict:
        return CompositeVerdict(
            accepted=False, signal=signal, reason=signal,
            virt_top1=virt_top1, bm25_top=bm25_top, detail=detail,
        )

    # (1) finiteness — provided inputs only (None axes are handled below).
    named: list[tuple[str, float | None]] = [
        ("bm25_top", bm25_top),
        ("virt_top1", virt_top1),
        ("thresholds.virt_hi", thresholds.virt_hi),
        ("thresholds.bm25_mid", thresholds.bm25_mid),
        ("thresholds.virt_mid", thresholds.virt_mid),
        ("bm25_threshold", bm25_threshold),
    ]
    for name, v in named:
        if v is not None and not math.isfinite(v):
            return _reject(
                SIGNAL_COMPOSITE_REJECT, detail=f"non-finite input: {name}",
            )

    # (2) arm 1 — BM25 strong, independent of the reference (fail-closed
    # anchor). >= semantics.
    if bm25_top is not None and bm25_top >= bm25_threshold:
        return CompositeVerdict(
            accepted=True, signal=SIGNAL_BM25_STRONG,
            virt_top1=virt_top1, bm25_top=bm25_top,
        )

    # (3) fail-closed: no usable reference → BM25 was the only accept path.
    if not reference_available:
        return _reject(
            SIGNAL_COMPOSITE_REFERENCE_UNAVAILABLE,
            detail="reference artifact unavailable/invalid",
        )

    # (4) a pool smaller than 2 is not trusted for the semantic arms.
    if pool_size < 2:
        return _reject(
            SIGNAL_COMPOSITE_POOL_TOO_SMALL, detail=f"pool_size={pool_size}",
        )

    # (5) virtual axis missing — precondition of both semantic arms.
    if virt_top1 is None:
        return _reject(
            SIGNAL_COMPOSITE_REJECT, detail="virtual axis missing",
        )

    # (6) semantic arms, stronger-first (>= semantics on every threshold).
    if virt_top1 >= thresholds.virt_hi:
        return CompositeVerdict(
            accepted=True, signal=SIGNAL_VIRT_HI,
            virt_top1=virt_top1, bm25_top=bm25_top,
        )
    if (
        bm25_top is not None
        and bm25_top >= thresholds.bm25_mid
        and virt_top1 >= thresholds.virt_mid
    ):
        return CompositeVerdict(
            accepted=True, signal=SIGNAL_BM25_VIRT_MID,
            virt_top1=virt_top1, bm25_top=bm25_top,
        )
    failed: list[str] = []
    if virt_top1 < thresholds.virt_hi:
        failed.append(
            f"virt {virt_top1:.4f} < virt_hi {thresholds.virt_hi:.4f}"
        )
    if bm25_top is None or bm25_top < thresholds.bm25_mid:
        bm25_repr = "n/a" if bm25_top is None else f"{bm25_top:.1f}"
        failed.append(
            f"bm25 {bm25_repr} < bm25_mid {thresholds.bm25_mid:.1f}"
        )
    elif virt_top1 < thresholds.virt_mid:
        failed.append(
            f"virt {virt_top1:.4f} < virt_mid {thresholds.virt_mid:.4f}"
        )
    return _reject(SIGNAL_COMPOSITE_REJECT, detail="; ".join(failed))


# --- Reference artifact (schema v1) ---------------------------------------------

# thresholds echo に必須の key (3-arm + arm1 の固定閾値)。v2 旧
# percentile/margin/raw_floor echo の artifact はここで検証 fail →
# fail-closed (再較正要求)。
_THRESHOLD_ECHO_KEYS = (
    "ambient_bm25_min_score",
    "ambient_composite_virt_hi",
    "ambient_composite_bm25_mid",
    "ambient_composite_virt_mid",
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
    ``virt_top1_distribution`` は 3-arm 判定には使われないが、再較正時の
    分布 provenance として保持する (plan §10)。
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
