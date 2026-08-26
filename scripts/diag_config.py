#!/usr/bin/env python
"""Phase U WP-1 — effective-config diagnostic with per-field provenance.

Read-only: prints a table of field / effective value / source for every
GaOTTTConfig field, using :meth:`GaOTTTConfig.resolve_config_with_sources`
(true resolution-time provenance — ``env`` / ``file`` / ``default`` — not
a heuristic diff against a default instance). Phase T/U tuning knobs are
printed first and flagged, so an operator can answer "which flag is
actually effective on this box, and where did it come from?" — the
question the Phase U review asked after discovering multiverse
supervisors strip ``GAOTTT_*`` env from backend spawns.

Side-effect note: constructing a GaOTTTConfig resolves (and mkdirs, if
missing) the default data directory — the same thing any GaOTTT process
startup does. No DB / FAISS / engine is opened and nothing is written to
the memory store.

Usage::

    .venv/bin/python scripts/diag_config.py
    .venv/bin/python scripts/diag_config.py --all        # 全 scalar field を詳細表示 (default)
    .venv/bin/python scripts/diag_config.py --knobs-only # Phase T/U knob のみ
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import fields

from gaottt.config import GaOTTTConfig

# Phase T (semantic requalification) / Phase U (review hardening) tuning
# knobs — the fields the review's rollback / allowlist discussion names.
# Order is significant: shown first in the table, grouped as declared.
PHASE_TU_KNOBS: list[tuple[str, str]] = [
    # Stage 2 — semantic half-life + floor
    ("semantic_halflife_enabled", "T-S2"),
    ("semantic_half_life_seconds", "T-S2"),
    ("semantic_floor", "T-S2"),
    # Stage 3 — direct relevance qualification
    ("direct_qualification_enabled", "T-S3 / U-WP1 promoted"),
    ("direct_raw_cosine_min", "T-S3"),
    ("direct_virtual_cosine_min", "T-S3"),
    ("direct_bm25_relative_min", "T-S3"),
    ("direct_bm25_absolute_min", "T-S3"),
    ("direct_bm25_pool_size", "T-S3"),
    # Stage 4 — TTT update qualification
    ("ttt_qualification_enabled", "T-S4 / U-WP1 promoted"),
    # Stage 5 — ambient OR gate
    ("ambient_gate_or_semantic", "T-S5"),
    ("ambient_semantic_raw_min", "T-S5"),
    ("ambient_min_score", "T-S5"),
    ("ambient_bm25_min_score", "T-S5"),
    ("ambient_gate_use_bm25", "T-S5"),
    # Stage 6 — explore diversified presentation (WP-5 で昇格予定)
    ("explore_diversified_presentation_enabled", "T-S6"),
    ("explore_cohort_penalty", "T-S6"),
    ("explore_diversity_pool_multiplier", "T-S6"),
    ("explore_min_semantic", "T-S6"),
]

_SOURCE_LABEL = {
    "env": "env",
    "file": "config-file",
    "default": "default",
}


def _format_value(value: object) -> str:
    if isinstance(value, float):
        return repr(value)
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--knobs-only",
        action="store_true",
        help="Phase T/U knob のみ表示 (それ以外の field は省く)",
    )
    args = parser.parse_args()

    config, sources = GaOTTTConfig.resolve_config_with_sources()
    field_names = {f.name for f in fields(GaOTTTConfig)}

    knob_names: set[str] = set()
    rows: list[tuple[str, str, str, str]] = []  # (group, field, value, source)
    for name, group in PHASE_TU_KNOBS:
        if name not in field_names:
            print(
                f"warning: PHASE_TU_KNOBS entry {name!r} is not a config "
                "field (renamed?) — update scripts/diag_config.py",
                file=sys.stderr,
            )
            continue
        knob_names.add(name)
        rows.append((
            group, name,
            _format_value(getattr(config, name)), sources[name],
        ))
    if not args.knobs_only:
        for f in fields(GaOTTTConfig):
            if f.name in knob_names:
                continue
            rows.append((
                "", f.name,
                _format_value(getattr(config, f.name)), sources[f.name],
            ))

    width_field = max(len(r[1]) for r in rows)
    width_value = max(len(r[2]) for r in rows)
    width_group = max(max(len(r[0]) for r in rows), len("phase"))
    header = (
        f"{'phase':<{width_group}}  {'field':<{width_field}}  "
        f"{'effective value':<{width_value}}  source"
    )
    print(header)
    print("-" * len(header))
    for group, name, value, source in rows:
        label = _SOURCE_LABEL[source]
        print(
            f"{group:<{width_group}}  {name:<{width_field}}  "
            f"{value:<{width_value}}  {label}"
        )

    env_fields = [r[1] for r in rows if r[3] == "env"]
    file_fields = [r[1] for r in rows if r[3] == "file"]
    print()
    print(
        f"fields: {len(rows)} total / env: {len(env_fields)} / "
        f"config-file: {len(file_fields)} / default: "
        f"{len(rows) - len(env_fields) - len(file_fields)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
