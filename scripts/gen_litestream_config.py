#!/usr/bin/env python3
"""MV5 WP-1 — Litestream config generator CLI.

Scans ``<multiverse_root>/universes/*/`` and emits a litestream v0.3
``dbs:`` YAML block. The pure scanning logic lives in
:mod:`gaottt.multiverse.backup`; this script is the operator-facing CLI.

Output separation (Codex review B1):

* **stdout** — the generated YAML only (always parseable by
  ``yaml.safe_load``). Nothing else is written to stdout on success.
* **stderr** — all diagnostics (INFO / WARN / ERROR). Route the module
  logger to a stderr ``StreamHandler`` so the pure function's log records
  reach stderr without polluting stdout.

``--output FILE`` writes the YAML atomically (tmp + fsync + os.replace);
on success nothing goes to stdout, diagnostics still go to stderr. If the
generator raises, the existing output file is left untouched (the tmp file
is the only mutation, and it is cleaned up before the error propagates —
Codex review B2).

Usage::

    .venv/bin/python scripts/gen_litestream_config.py --root /path/to/multiverse
    .venv/bin/python scripts/gen_litestream_config.py --root ... --output /etc/litestream/gaottt.yml
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from gaottt.multiverse.backup import (
    DEFAULT_REPLICA_PREFIX,
    atomic_write_text,
    generate_litestream_config,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a litestream dbs: YAML block for a GaOTTT multiverse "
            "root. stdout = YAML only; diagnostics go to stderr."
        ),
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("GAOTTT_MULTIVERSE_ROOT"),
        help=(
            "multiverse root directory (the value of config.multiverse_root). "
            "Defaults to $GAOTTT_MULTIVERSE_ROOT; error if neither is set."
        ),
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help=(
            "write YAML to this file (atomic: tmp + fsync + os.replace). "
            "Omit to write YAML to stdout."
        ),
    )
    parser.add_argument(
        "--replica-prefix",
        default=DEFAULT_REPLICA_PREFIX,
        help=f"base path for generated file replicas (default: {DEFAULT_REPLICA_PREFIX})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Route the backup module's diagnostics to stderr (stdout is reserved
    # for the YAML). A dedicated logger name keeps this from reconfiguring
    # the root logger and surprising other handlers.
    diag = logging.getLogger("gaottt.multiverse.backup")
    if not diag.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        diag.addHandler(h)
    diag.setLevel(logging.INFO)
    diag.propagate = False

    if not args.root:
        # Error to stderr only — stdout stays empty, never a partial YAML.
        print(
            "ERROR: --root is required (or set $GAOTTT_MULTIVERSE_ROOT)",
            file=sys.stderr,
        )
        return 2

    root = Path(args.root)
    try:
        yaml_text = generate_litestream_config(root, replica_prefix=args.replica_prefix)
    except Exception as exc:  # noqa: BLE001 — top-level CLI guard
        print(f"ERROR: litestream config generation failed: {exc}", file=sys.stderr)
        return 1

    if args.output:
        out = Path(args.output)
        try:
            atomic_write_text(out, yaml_text)
        except OSError as exc:
            print(f"ERROR: could not write {out}: {exc}", file=sys.stderr)
            return 1
        # Success: nothing to stdout. Diagnostics already went to stderr
        # during scan.
    else:
        sys.stdout.write(yaml_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
