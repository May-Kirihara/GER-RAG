#!/usr/bin/env python3
"""Merge nodes created after standalone→multiverse migration into one universe.

Only source-only IDs are copied. Existing universe rows are never updated, so
the destination's independently evolved gravity state remains authoritative.
Co-occurrence and directed edges are copied when both endpoints exist after
the node merge. Run with all GaOTTT writers stopped and back up the destination
first; rebuild FAISS afterward to embed the newly copied nodes.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    source = args.source.resolve()
    target = args.target.resolve()
    if source == target:
        parser.error("source and target must differ")

    con = sqlite3.connect(target, uri=True)
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("ATTACH DATABASE ? AS legacy", (f"file:{source}?mode=ro",))
    try:
        missing_ids = [row[0] for row in con.execute(
            "SELECT l.id FROM legacy.nodes l "
            "WHERE NOT EXISTS (SELECT 1 FROM main.nodes n WHERE n.id=l.id)"
        )]
        missing = len(missing_ids)
        print(f"source-only nodes: {missing:,}")
        if not args.apply:
            print("Dry run only; pass --apply to merge.")
            return 0

        con.execute("BEGIN IMMEDIATE")
        before_nodes = con.execute("SELECT COUNT(*) FROM main.nodes").fetchone()[0]
        before_docs = con.execute("SELECT COUNT(*) FROM main.documents").fetchone()[0]
        before_edges = con.execute("SELECT COUNT(*) FROM main.edges").fetchone()[0]
        before_directed = con.execute(
            "SELECT COUNT(*) FROM main.directed_edges"
        ).fetchone()[0]

        con.execute(
            "INSERT INTO main.nodes SELECT l.* FROM legacy.nodes l "
            "WHERE NOT EXISTS (SELECT 1 FROM main.nodes n WHERE n.id=l.id)"
        )
        con.execute(
            "INSERT INTO main.documents SELECT d.* FROM legacy.documents d "
            "WHERE EXISTS (SELECT 1 FROM main.nodes n WHERE n.id=d.id) "
            "AND NOT EXISTS (SELECT 1 FROM main.documents x WHERE x.id=d.id)"
        )
        con.execute(
            "INSERT INTO main.edges SELECT e.* FROM legacy.edges e "
            "WHERE EXISTS (SELECT 1 FROM main.nodes n WHERE n.id=e.src) "
            "AND EXISTS (SELECT 1 FROM main.nodes n WHERE n.id=e.dst) "
            "AND NOT EXISTS (SELECT 1 FROM main.edges x "
            "WHERE x.src=e.src AND x.dst=e.dst)"
        )
        con.execute(
            "INSERT INTO main.directed_edges SELECT e.* FROM legacy.directed_edges e "
            "WHERE EXISTS (SELECT 1 FROM main.nodes n WHERE n.id=e.src) "
            "AND EXISTS (SELECT 1 FROM main.nodes n WHERE n.id=e.dst) "
            "AND NOT EXISTS (SELECT 1 FROM main.directed_edges x "
            "WHERE x.src=e.src AND x.dst=e.dst AND x.edge_type=e.edge_type)"
        )

        after_nodes = con.execute("SELECT COUNT(*) FROM main.nodes").fetchone()[0]
        after_docs = con.execute("SELECT COUNT(*) FROM main.documents").fetchone()[0]
        after_edges = con.execute("SELECT COUNT(*) FROM main.edges").fetchone()[0]
        after_directed = con.execute(
            "SELECT COUNT(*) FROM main.directed_edges"
        ).fetchone()[0]
        remaining = con.execute(
            "SELECT COUNT(*) FROM legacy.nodes l "
            "WHERE NOT EXISTS (SELECT 1 FROM main.nodes n WHERE n.id=l.id)"
        ).fetchone()[0]
        placeholders = ",".join("?" for _ in missing_ids)
        orphan_docs = (
            con.execute(
                f"SELECT COUNT(*) FROM main.nodes n WHERE n.id IN ({placeholders}) "
                "AND NOT EXISTS (SELECT 1 FROM main.documents d WHERE d.id=n.id)",
                missing_ids,
            ).fetchone()[0]
            if missing_ids else 0
        )
        if remaining or orphan_docs:
            con.rollback()
            raise RuntimeError(
                f"verification failed: remaining={remaining}, orphan_docs={orphan_docs}"
            )
        con.commit()
        print(
            "merged: "
            f"nodes={after_nodes-before_nodes}, docs={after_docs-before_docs}, "
            f"edges={after_edges-before_edges}, "
            f"directed_edges={after_directed-before_directed}"
        )
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
