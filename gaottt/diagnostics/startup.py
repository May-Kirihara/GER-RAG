"""Startup-time self-diagnostics — Tier A + B (Stage 1).

Called from ``engine.startup()`` after FAISS / BM25 are loaded. Each
check returns a :class:`DiagnosticResult` with a level
(INFO / WARN / ERROR) and a human-readable detail string. The combined
:class:`DiagnosticReport` is logged at the appropriate level and
returned so callers (e.g. CLI tools) can decide what to do with it.

Tier A — **FAISS integrity** (per index: raw + virtual):
  - File presence / readability
  - 0-byte detection → triggers a lazy rebuild via the engine
  - ``.tmp`` residual cleanup (interrupted atomic save)

Tier B — **Size consistency**:
  - ``faiss.size`` vs ``len(active SQLite nodes)`` — 5% drift → WARN
  - ``bm25.size`` vs ``len(active SQLite nodes)`` — 5% drift → WARN

Stage 2 (deferred): WAL audit / physics dynamics drift / JSON report
endpoint. Stage 3 (deferred): migration ledger / config sanity / CLI.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gaottt.config import GaOTTTConfig
    from gaottt.core.engine import GaOTTTEngine

logger = logging.getLogger(__name__)


class DiagnosticLevel(Enum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


@dataclass(frozen=True)
class DiagnosticResult:
    """Single check outcome.

    ``name`` is the stable identifier (e.g. ``"tier_a_faiss_zero_bytes"``)
    so callers can match on it programmatically. ``detail`` is the
    human-readable message; ``level`` is the severity.
    """
    name: str
    level: DiagnosticLevel
    detail: str

    def log(self) -> None:
        msg = "[diagnostics:%s] %s"
        if self.level is DiagnosticLevel.ERROR:
            logger.error(msg, self.name, self.detail)
        elif self.level is DiagnosticLevel.WARN:
            logger.warning(msg, self.name, self.detail)
        else:
            logger.info(msg, self.name, self.detail)


@dataclass
class DiagnosticReport:
    """Bundle of results from one ``run_startup_checks`` invocation."""
    results: list[DiagnosticResult] = field(default_factory=list)

    def add(self, name: str, level: DiagnosticLevel, detail: str) -> None:
        r = DiagnosticResult(name=name, level=level, detail=detail)
        self.results.append(r)
        r.log()

    def by_level(self, level: DiagnosticLevel) -> list[DiagnosticResult]:
        return [r for r in self.results if r.level is level]

    @property
    def has_errors(self) -> bool:
        return any(r.level is DiagnosticLevel.ERROR for r in self.results)

    @property
    def has_warnings(self) -> bool:
        return any(r.level is DiagnosticLevel.WARN for r in self.results)

    def summary(self) -> str:
        n_err = len(self.by_level(DiagnosticLevel.ERROR))
        n_warn = len(self.by_level(DiagnosticLevel.WARN))
        n_info = len(self.by_level(DiagnosticLevel.INFO))
        return f"{len(self.results)} checks ({n_err} error, {n_warn} warn, {n_info} info)"


# ---------------------------------------------------------------------------
# Tier A — FAISS integrity
# ---------------------------------------------------------------------------

# An atomic FAISS save never takes anywhere near this long even for a
# 30k-vector ~100 MB index; an unscoped .tmp older than this is safely a
# dead orphan from before the pid-scoped naming (H3) existed.
_STALE_TMP_AGE_SECONDS = 600


def _pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` currently exists.

    ``os.kill(pid, 0)`` sends no signal but performs the existence /
    permission check: ProcessLookupError → dead; PermissionError → alive
    but owned by another user (still a live writer we must not disturb).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        # Malformed / out-of-range pid parsed from a filename, or an
        # unknown errno — be conservative and treat as alive (don't
        # delete; a stale file only wastes disk, a wrong delete loses an
        # in-flight index).
        return True
    return True


def _tmp_owner_pid(tmp: Path) -> int | None:
    """Parse the owning pid from a ``<path>.<pid>.tmp`` scratch name.

    Returns ``None`` for legacy unscoped names (``<path>.tmp``) written by
    code predating H3, or the ``.ids.<pid>.tmp`` variant (handled the same
    way — the segment before ``.tmp`` is the pid in both).
    """
    stem = tmp.name[:-4] if tmp.name.endswith(".tmp") else tmp.name
    last = stem.rsplit(".", 1)[-1]
    return int(last) if last.isdigit() else None


def _cleanup_tmp_residuals(directory: Path, report: DiagnosticReport) -> None:
    """Delete *clearly-orphaned* ``*.tmp`` files in ``directory``.

    GaOTTT's atomic FAISS save (H3) writes ``<index>.<pid>.tmp`` then
    renames. The FAISS dir is shared across processes, so this sweep must
    NOT delete a scratch file a *live* sibling process is mid-write into —
    doing so turns that process's ``os.replace`` into FileNotFoundError
    and loses the index snapshot. Policy:

      * pid-scoped name, owning pid dead  → orphan, delete.
      * pid-scoped name, owning pid alive → live writer, skip.
      * legacy unscoped name, older than _STALE_TMP_AGE_SECONDS → delete.
      * legacy unscoped name, recent → could be a pre-H3 writer, skip.
    """
    if not directory.exists() or not directory.is_dir():
        return
    now = time.time()
    for tmp in directory.glob("*.tmp"):
        try:
            stat = tmp.stat()
            owner = _tmp_owner_pid(tmp)
            if owner is not None:
                if _pid_alive(owner):
                    report.add(
                        "tier_a_tmp_residual_skipped_live",
                        DiagnosticLevel.INFO,
                        f"kept {tmp.name} — pid {owner} is alive (in-flight save)",
                    )
                    continue
            else:
                age = now - stat.st_mtime
                if age < _STALE_TMP_AGE_SECONDS:
                    report.add(
                        "tier_a_tmp_residual_skipped_recent",
                        DiagnosticLevel.INFO,
                        f"kept {tmp.name} — unscoped tmp only {age:.0f}s old "
                        f"(possible pre-H3 in-flight save)",
                    )
                    continue
            tmp_size = stat.st_size
            tmp.unlink()
            report.add(
                "tier_a_tmp_residual_cleaned",
                DiagnosticLevel.INFO,
                f"removed orphan {tmp.name} ({tmp_size} bytes) — "
                f"interrupted atomic save by a dead/old process",
            )
        except OSError as e:
            report.add(
                "tier_a_tmp_residual_cleanup_failed",
                DiagnosticLevel.WARN,
                f"could not process {tmp}: {e}",
            )


async def _check_faiss_index(
    engine: "GaOTTTEngine",
    report: DiagnosticReport,
    *,
    label: str,
    path_str: str,
    is_virtual: bool,
) -> None:
    """Run Tier A checks for one FAISS index (raw or virtual)."""
    path = Path(path_str)

    # Index object presence on the engine (virtual is optional)
    index = engine.virtual_faiss_index if is_virtual else engine.faiss_index
    if index is None:
        report.add(
            f"tier_a_{label}_disabled",
            DiagnosticLevel.INFO,
            f"{label} FAISS index disabled by config — skipping integrity check",
        )
        return

    # File-level checks
    if not path.exists():
        report.add(
            f"tier_a_{label}_missing",
            DiagnosticLevel.INFO,
            f"{label} FAISS file does not exist at {path} — fresh DB or pre-first-save state",
        )
    else:
        size = path.stat().st_size
        if size == 0:
            report.add(
                f"tier_a_{label}_zero_bytes",
                DiagnosticLevel.ERROR,
                f"{label} FAISS file at {path} is 0 bytes — corrupted save, triggering rebuild",
            )
            try:
                if is_virtual:
                    await engine._rebuild_virtual_faiss_index()  # noqa: SLF001
                else:
                    await engine._rebuild_faiss_index()  # noqa: SLF001
                report.add(
                    f"tier_a_{label}_rebuilt",
                    DiagnosticLevel.INFO,
                    f"{label} FAISS rebuilt: size={index.size}",
                )
            except Exception as e:
                report.add(
                    f"tier_a_{label}_rebuild_failed",
                    DiagnosticLevel.ERROR,
                    f"{label} FAISS rebuild raised: {type(e).__name__}: {e}",
                )
        else:
            report.add(
                f"tier_a_{label}_present",
                DiagnosticLevel.INFO,
                f"{label} FAISS file ok ({size} bytes, in-memory size={index.size})",
            )


# ---------------------------------------------------------------------------
# Tier B — size consistency
# ---------------------------------------------------------------------------

WARN_DRIFT_FRACTION = 0.05


def _drift_fraction(observed: int, expected: int) -> float:
    """Return the absolute drift as a fraction of expected (0.0 if expected is 0)."""
    if expected <= 0:
        return 1.0 if observed > 0 else 0.0
    return abs(observed - expected) / expected


async def _check_size_consistency(
    engine: "GaOTTTEngine",
    report: DiagnosticReport,
) -> None:
    """Compare engine index sizes against the SQLite active-doc ground truth."""
    states = await engine.store.get_all_node_states()
    active = sum(1 for s in states if not s.is_archived)
    active_ids = {s.id for s in states if not s.is_archived}

    # FAISS raw
    faiss_size = engine.faiss_index.size
    faiss_drift = _drift_fraction(faiss_size, active)
    # Severe undersize: the loaded index holds far fewer vectors than the DB
    # has active nodes. This is the "reverse overwrite trap" signature — the
    # process is running on a corrupt/truncated index. Escalate to ERROR and
    # latch the engine's persist guard so this process never writes its broken
    # index back to disk (it would clobber a good index from a healthy
    # sibling). Recovery is manual: stop everything, run the rebuild script.
    floor = getattr(engine.config, "faiss_persist_floor", 100)
    ratio = getattr(engine.config, "faiss_persist_min_ratio", 0.5)
    guard_on = getattr(engine.config, "faiss_persist_guard_enabled", True)
    # Both directions matter.  An oversized index is the signature produced
    # when SQLite is restored/rolled back without replacing its FAISS files:
    # FAISS returns IDs from the newer corpus, the engine drops them because
    # they are absent from SQLite, and recall used to degrade to an empty list.
    # Count alone is insufficient when two snapshots are similarly sized, so
    # also compare the actual ID sets.
    faiss_ids = engine.faiss_index.ids()
    overlap = len(active_ids & faiss_ids)
    overlap_ratio = overlap / max(1, min(active, faiss_size))
    severe_size = active >= floor and (
        faiss_size < active * ratio or faiss_size > active / ratio
    )
    severe_identity = (
        active >= floor and faiss_size >= floor and overlap_ratio < ratio
    )
    severe = severe_size or severe_identity
    if severe:
        latched = ""
        if guard_on:
            engine._faiss_persist_blocked = True  # noqa: SLF001
            latched = (
                "Persist guard LATCHED: this process will not overwrite the "
                "on-disk index. "
            )
        diagnostic_name = (
            "tier_b_faiss_severe_undersize"
            if faiss_size < active * ratio
            else "tier_b_faiss_snapshot_mismatch"
        )
        report.add(
            diagnostic_name,
            DiagnosticLevel.ERROR,
            f"faiss.size={faiss_size} vs SQLite active={active}, "
            f"id overlap={overlap}/{min(active, faiss_size)} "
            f"({overlap_ratio:.1%}) — index belongs to a different or "
            f"incomplete SQLite snapshot. "
            f"{latched}"
            f"Recover: stop all gaottt processes, run "
            f"`scripts/rebuild_faiss_from_db.py --apply`, then restart.",
        )
    elif faiss_drift > WARN_DRIFT_FRACTION:
        report.add(
            "tier_b_faiss_size_drift",
            DiagnosticLevel.WARN,
            f"faiss.size={faiss_size} vs SQLite active={active} "
            f"({faiss_drift:.1%} drift > {WARN_DRIFT_FRACTION:.0%}) — run compact(rebuild_faiss=True)",
        )
    else:
        report.add(
            "tier_b_faiss_size_ok",
            DiagnosticLevel.INFO,
            f"faiss.size={faiss_size} matches SQLite active={active} "
            f"(drift={faiss_drift:.1%}, id overlap={overlap_ratio:.1%})",
        )

    # BM25
    if engine.bm25_index is not None:
        # Phase U WP-6b (WP-6c Risk 1): background build 中 (building) や
        # 失敗直後 (failed)、build 未開始 (idle) の間は size が store 構成に
        # 届く前なので drift WARN は誤報になる。state != "ready" の間は
        # INFO (pending) に格下げし、build 完了後の次回 startup で再検査
        # される。state == "ready" (同期 build / snapshot load 済み) は
        # 従来どおり WARN/OK を返す。
        if engine.bm25_build_state != "ready":
            report.add(
                "tier_b_bm25_size_pending",
                DiagnosticLevel.INFO,
                f"bm25 build state={engine.bm25_build_state} "
                f"(background build in flight or not yet complete) — size "
                f"check deferred (bm25.size={engine.bm25_index.size} vs "
                f"SQLite active={active})",
            )
        else:
            bm25_size = engine.bm25_index.size
            bm25_drift = _drift_fraction(bm25_size, active)
            if bm25_drift > WARN_DRIFT_FRACTION:
                report.add(
                    "tier_b_bm25_size_drift",
                    DiagnosticLevel.WARN,
                    f"bm25.size={bm25_size} vs SQLite active={active} "
                    f"({bm25_drift:.1%} drift > {WARN_DRIFT_FRACTION:.0%}) — restart or compact(rebuild_faiss=True)",
                )
            else:
                report.add(
                    "tier_b_bm25_size_ok",
                    DiagnosticLevel.INFO,
                    f"bm25.size={bm25_size} matches SQLite active={active} (drift={bm25_drift:.1%})",
                )

    # Virtual FAISS — informational only (it can legitimately lag raw FAISS
    # by the write-behind interval; only flag if it's empty when raw is not).
    if engine.virtual_faiss_index is not None:
        virtual_size = engine.virtual_faiss_index.size
        if virtual_size == 0 and faiss_size > 0:
            report.add(
                "tier_b_virtual_faiss_empty",
                DiagnosticLevel.WARN,
                f"virtual_faiss.size=0 but raw faiss.size={faiss_size} — "
                "rebuild will be triggered or run compact(rebuild_faiss=True)",
            )
        else:
            report.add(
                "tier_b_virtual_faiss_ok",
                DiagnosticLevel.INFO,
                f"virtual_faiss.size={virtual_size} (raw faiss.size={faiss_size})",
            )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_startup_checks(
    engine: "GaOTTTEngine",
    config: "GaOTTTConfig",
) -> DiagnosticReport:
    """Run all Stage 1 startup checks. Returns a populated report.

    Intended to be called from the end of ``engine.startup()``; failure
    of any individual check is captured in the report, not raised — the
    engine should remain bootable even if diagnostics raise WARN/ERROR.
    """
    report = DiagnosticReport()

    # Tier A — FAISS integrity (raw + virtual)
    faiss_dir = Path(config.faiss_index_path).parent
    _cleanup_tmp_residuals(faiss_dir, report)

    await _check_faiss_index(
        engine, report,
        label="raw",
        path_str=config.faiss_index_path,
        is_virtual=False,
    )
    if engine.virtual_faiss_index is not None:
        await _check_faiss_index(
            engine, report,
            label="virtual",
            path_str=config.virtual_faiss_index_path,
            is_virtual=True,
        )

    # Tier B — size consistency
    try:
        await _check_size_consistency(engine, report)
    except Exception as e:
        report.add(
            "tier_b_size_check_failed",
            DiagnosticLevel.ERROR,
            f"size consistency check raised: {type(e).__name__}: {e}",
        )

    logger.info(
        "Startup diagnostics complete: %s",
        report.summary(),
    )
    return report
