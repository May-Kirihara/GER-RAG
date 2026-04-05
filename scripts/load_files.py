"""Load files from a directory into GER-RAG via the /index API.

自炊した書籍のmd、メモのtxt、データのcsvなどを一括取り込み。

Usage:
    python scripts/load_files.py /path/to/books/
    python scripts/load_files.py /path/to/books/ --pattern "*.md" --recursive
    python scripts/load_files.py ./notes/ --pattern "*.md,*.txt" --source notebook
    python scripts/load_files.py ./data.csv --source articles

Examples:
    # 自炊した書籍ディレクトリ（再帰的にmdを収集）
    python scripts/load_files.py ~/books/自炊/ --recursive

    # 特定のファイルだけ
    python scripts/load_files.py ~/notes/meeting_notes.md

    # txtとmdを混在で取り込み
    python scripts/load_files.py ~/documents/ --pattern "*.md,*.txt" --recursive

    # チャンクサイズ変更（長い章を大きめに保持）
    python scripts/load_files.py ~/books/ --chunk-size 3000 --recursive
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

from ger_rag.ingest.loader import ingest_path

DEFAULT_URL = "http://localhost:8000"
DEFAULT_BATCH = 50


def send_batch(client: httpx.Client, url: str, docs: list[dict]) -> tuple[int, int]:
    """Send a batch of documents. Returns (indexed, skipped)."""
    payload = {
        "documents": [
            {"content": d["content"], "metadata": d.get("metadata")}
            for d in docs
        ]
    }
    resp = client.post(f"{url}/index", json=payload, timeout=120.0)
    resp.raise_for_status()
    data = resp.json()
    return data["count"], data.get("skipped", 0)


def main():
    parser = argparse.ArgumentParser(
        description="Load files into GER-RAG (md, txt, csv)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s ~/books/自炊/ --recursive\n"
            "  %(prog)s ~/notes/meeting.md\n"
            "  %(prog)s ~/docs/ --pattern '*.md,*.txt' --recursive\n"
        ),
    )
    parser.add_argument("path", help="File or directory to ingest")
    parser.add_argument("--url", default=DEFAULT_URL, help="GER-RAG server URL")
    parser.add_argument("--pattern", default="*.md,*.txt",
                        help="Glob patterns (comma-separated, default: '*.md,*.txt')")
    parser.add_argument("--recursive", "-r", action="store_true",
                        help="Recursively scan subdirectories")
    parser.add_argument("--source", default="file",
                        help="Source label for metadata (default: 'file')")
    parser.add_argument("--chunk-size", type=int, default=2000,
                        help="Max characters per chunk (default: 2000)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH,
                        help="Documents per API request (default: 50)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be ingested without sending")
    args = parser.parse_args()

    p = Path(args.path)
    if not p.exists():
        print(f"ERROR: {args.path} does not exist")
        sys.exit(1)

    # Ingest files
    print(f"Scanning: {args.path}")
    if p.is_dir():
        print(f"  Pattern: {args.pattern}, Recursive: {args.recursive}")

    documents = ingest_path(
        args.path,
        source=args.source,
        recursive=args.recursive,
        pattern=args.pattern,
        chunk_size=args.chunk_size,
    )

    if not documents:
        print("No documents found.")
        sys.exit(0)

    # Summary
    files = set()
    for d in documents:
        meta = d.get("metadata", {})
        if meta.get("file_path"):
            files.add(meta["file_path"])

    print(f"  Files: {len(files)}")
    print(f"  Chunks: {len(documents)}")

    # Show file breakdown
    file_chunks: dict[str, int] = {}
    for d in documents:
        meta = d.get("metadata", {})
        fname = meta.get("file_name", "?")
        file_chunks[fname] = file_chunks.get(fname, 0) + 1

    for fname, count in sorted(file_chunks.items()):
        print(f"    {fname}: {count} chunks")

    if args.dry_run:
        print("\n[DRY RUN] No documents sent.")
        # Show first few chunks as preview
        for d in documents[:3]:
            meta = d.get("metadata", {})
            section = meta.get("section", "")
            preview = d["content"][:100].replace("\n", " ")
            print(f"  [{meta.get('file_name', '?')}:{section}] {preview}...")
        if len(documents) > 3:
            print(f"  ... and {len(documents) - 3} more")
        return

    # Check server
    try:
        httpx.get(f"{args.url}/docs", timeout=5.0)
    except httpx.ConnectError:
        print(f"\nERROR: Cannot connect to {args.url}. Is the server running?")
        sys.exit(1)

    # Send
    total_indexed = 0
    total_skipped = 0
    start = time.time()

    print(f"\nSending to {args.url}...")
    with httpx.Client() as client:
        for i in range(0, len(documents), args.batch_size):
            batch = documents[i: i + args.batch_size]
            try:
                indexed, skipped = send_batch(client, args.url, batch)
                total_indexed += indexed
                total_skipped += skipped
                elapsed = time.time() - start
                print(
                    f"  Batch {i // args.batch_size + 1}: "
                    f"+{indexed} indexed, {skipped} skipped "
                    f"({total_indexed + total_skipped}/{len(documents)}) "
                    f"[{elapsed:.1f}s]"
                )
            except httpx.HTTPStatusError as e:
                print(f"  ERROR: {e.response.status_code} {e.response.text[:200]}", file=sys.stderr)

    elapsed = time.time() - start
    print(f"\nDone: {total_indexed} indexed, {total_skipped} skipped in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
