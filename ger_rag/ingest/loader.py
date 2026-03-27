"""File ingestion: load documents from files and directories.

Supports Markdown (.md), plain text (.txt), and CSV (.csv).
"""

from __future__ import annotations

import csv
import re
from pathlib import Path


def ingest_path(
    path: str,
    source: str = "file",
    recursive: bool = False,
    pattern: str = "*.md,*.txt",
    chunk_size: int = 2000,
) -> list[dict]:
    """Load documents from a file or directory.

    Returns list of {"content": str, "metadata": dict} dicts.
    """
    p = Path(path)
    if not p.exists():
        return []

    if p.is_file():
        return _ingest_file(p, source, chunk_size)

    # Directory: collect files by pattern
    patterns = [pat.strip() for pat in pattern.split(",")]
    files: list[Path] = []
    for pat in patterns:
        if recursive:
            files.extend(p.rglob(pat))
        else:
            files.extend(p.glob(pat))

    documents = []
    for f in sorted(files):
        documents.extend(_ingest_file(f, source, chunk_size))
    return documents


def _ingest_file(path: Path, source: str, chunk_size: int) -> list[dict]:
    """Ingest a single file based on extension."""
    suffix = path.suffix.lower()
    if suffix == ".md":
        return _ingest_markdown(path, source, chunk_size)
    elif suffix == ".csv":
        return _ingest_csv(path, source, chunk_size)
    else:  # .txt and others
        return _ingest_plaintext(path, source, chunk_size)


# -----------------------------------------------------------------------
# Markdown
# -----------------------------------------------------------------------

def _ingest_markdown(path: Path, source: str, chunk_size: int) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []

    # Extract title from # heading
    title = path.stem
    title_match = re.match(r"^#\s+(.+)", text)
    if title_match:
        title = title_match.group(1).strip()

    # Split on ## headings
    sections = re.split(r"(?=^##\s)", text, flags=re.MULTILINE)

    documents = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        # Skip title-only section (just the # heading)
        if section.startswith("# ") and "\n" not in section.strip():
            continue

        # Extract section heading
        heading = ""
        heading_match = re.match(r"^##\s+(.+)", section)
        if heading_match:
            heading = heading_match.group(1).strip()

        # Chunk if too long
        chunks = _chunk_text(section, chunk_size)
        for i, chunk in enumerate(chunks):
            meta = {
                "source": source,
                "file_path": str(path),
                "file_name": path.name,
                "title": title,
            }
            if heading:
                meta["section"] = heading
            if len(chunks) > 1:
                meta["chunk_index"] = i
                meta["total_chunks"] = len(chunks)
            documents.append({"content": chunk, "metadata": meta})

    return documents


# -----------------------------------------------------------------------
# Plain text
# -----------------------------------------------------------------------

def _ingest_plaintext(path: Path, source: str, chunk_size: int) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []

    chunks = _chunk_text(text, chunk_size)
    documents = []
    for i, chunk in enumerate(chunks):
        meta = {
            "source": source,
            "file_path": str(path),
            "file_name": path.name,
        }
        if len(chunks) > 1:
            meta["chunk_index"] = i
            meta["total_chunks"] = len(chunks)
        documents.append({"content": chunk, "metadata": meta})

    return documents


# -----------------------------------------------------------------------
# CSV
# -----------------------------------------------------------------------

def _ingest_csv(path: Path, source: str, chunk_size: int) -> list[dict]:
    documents = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return []

        # Auto-detect content column
        content_col = None
        for candidate in ["content", "text", "body", "message"]:
            if candidate in reader.fieldnames:
                content_col = candidate
                break
        if content_col is None:
            content_col = reader.fieldnames[0]

        for row in reader:
            text = row.get(content_col, "").strip()
            if not text:
                continue

            meta = {"source": source, "file_path": str(path), "file_name": path.name}
            # Include other columns as metadata
            for col in reader.fieldnames:
                if col != content_col and row.get(col):
                    meta[col] = row[col]

            chunks = _chunk_text(text, chunk_size)
            for i, chunk in enumerate(chunks):
                doc_meta = {**meta}
                if len(chunks) > 1:
                    doc_meta["chunk_index"] = i
                    doc_meta["total_chunks"] = len(chunks)
                documents.append({"content": chunk, "metadata": doc_meta})

    return documents


# -----------------------------------------------------------------------
# Chunking
# -----------------------------------------------------------------------

def _chunk_text(text: str, max_chars: int) -> list[str]:
    """Split text into chunks, preserving paragraph boundaries."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    # Try paragraph splitting
    paragraphs = re.split(r"\n\n+", text)
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
    chunks = []
    while len(text) > max_chars:
        cut = max_chars
        for sep in ["。", "\n", "．", ".", "！", "？"]:
            pos = text.rfind(sep, 0, max_chars)
            if pos > max_chars // 2:
                cut = pos + len(sep)
                break
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks
