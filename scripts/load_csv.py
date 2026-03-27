"""Load documents.csv into GER-RAG via the /index API.

Usage:
    python scripts/load_csv.py [--url URL] [--batch-size N] [--max-chunk-chars N]

Reads input/documents.csv, chunks long documents, and POSTs to /index in batches.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time

import httpx

DEFAULT_URL = "http://localhost:8000"
DEFAULT_BATCH = 50
MAX_CHUNK_CHARS = 2000  # RURI-v3 handles 8192 tokens, ~2000 chars is safe


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split long text into chunks, preferring '---' separators or paragraph breaks."""
    if len(text) <= max_chars:
        return [text.strip()] if text.strip() else []

    # Try splitting on '---' separator (common in likes batches)
    parts = re.split(r'\n---\n', text)
    if len(parts) > 1:
        chunks = []
        current = ""
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if len(current) + len(part) + 4 <= max_chars:
                current = current + "\n---\n" + part if current else part
            else:
                if current:
                    chunks.append(current)
                if len(part) > max_chars:
                    chunks.extend(_hard_split(part, max_chars))
                else:
                    current = part
        if current:
            chunks.append(current)
        return chunks

    # Fallback: split on double newlines
    paragraphs = text.split("\n\n")
    if len(paragraphs) > 1:
        chunks = []
        current = ""
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            if len(current) + len(para) + 2 <= max_chars:
                current = current + "\n\n" + para if current else para
            else:
                if current:
                    chunks.append(current)
                if len(para) > max_chars:
                    chunks.extend(_hard_split(para, max_chars))
                else:
                    current = para
        if current:
            chunks.append(current)
        return chunks

    return _hard_split(text, max_chars)


def _hard_split(text: str, max_chars: int) -> list[str]:
    """Last resort: split at max_chars boundaries on sentence endings."""
    chunks = []
    while len(text) > max_chars:
        # Find last sentence boundary within limit
        cut = max_chars
        for sep in ["。", "．", "\n", ".", "！", "？"]:
            pos = text.rfind(sep, 0, max_chars)
            if pos > max_chars // 2:
                cut = pos + len(sep)
                break
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


def load_csv(path: str, max_chunk_chars: int) -> list[dict]:
    """Read CSV and return list of {content, metadata} dicts, chunked as needed."""
    documents = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            text = row["text"].strip()
            if not text:
                continue

            source = row.get("source", "")
            # Skip DMs for privacy
            if source in ("dm", "group_dm"):
                continue

            metadata = {
                "original_id": row["id"],
                "title": row.get("title", ""),
                "source": source,
                "date": row.get("date", ""),
            }

            chunks = chunk_text(text, max_chunk_chars)
            for i, chunk in enumerate(chunks):
                doc_meta = {**metadata}
                if len(chunks) > 1:
                    doc_meta["chunk_index"] = i
                    doc_meta["total_chunks"] = len(chunks)
                documents.append({"content": chunk, "metadata": doc_meta})

    return documents


def send_batch(client: httpx.Client, url: str, docs: list[dict]) -> int:
    resp = client.post(
        f"{url}/index",
        json={"documents": [{"content": d["content"], "metadata": d["metadata"]} for d in docs]},
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.json()["count"]


def main():
    parser = argparse.ArgumentParser(description="Load documents.csv into GER-RAG")
    parser.add_argument("--url", default=DEFAULT_URL, help="GER-RAG server URL")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--max-chunk-chars", type=int, default=MAX_CHUNK_CHARS)
    parser.add_argument("--csv", default="input/documents.csv", help="Path to CSV")
    parser.add_argument("--limit", type=int, default=0, help="Max docs to load (0=all)")
    args = parser.parse_args()

    print(f"Reading {args.csv}...")
    documents = load_csv(args.csv, args.max_chunk_chars)
    if args.limit > 0:
        documents = documents[: args.limit]
    print(f"Prepared {len(documents)} documents (after chunking, excluding DMs)")

    total_indexed = 0
    start = time.time()

    with httpx.Client() as client:
        for i in range(0, len(documents), args.batch_size):
            batch = documents[i : i + args.batch_size]
            try:
                count = send_batch(client, args.url, batch)
                total_indexed += count
                elapsed = time.time() - start
                print(
                    f"  Batch {i // args.batch_size + 1}: "
                    f"{count} indexed ({total_indexed}/{len(documents)}) "
                    f"[{elapsed:.1f}s]"
                )
            except httpx.HTTPStatusError as e:
                print(f"  ERROR batch {i // args.batch_size + 1}: {e.response.status_code} {e.response.text}", file=sys.stderr)
            except httpx.ConnectError:
                print(f"ERROR: Cannot connect to {args.url}. Is the server running?", file=sys.stderr)
                sys.exit(1)

    elapsed = time.time() - start
    print(f"\nDone: {total_indexed} documents indexed in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
