"""Ingest service — bulk-load files into memory."""
from __future__ import annotations

from gaottt.core.engine import GaOTTTEngine
from gaottt.core.types import IngestResponse
from gaottt.ingest.loader import ingest_path


async def ingest(
    engine: GaOTTTEngine,
    path: str,
    source: str = "file",
    recursive: bool = False,
    pattern: str = "*.md,*.txt",
    chunk_size: int = 2000,
) -> IngestResponse:
    documents = ingest_path(
        path, source=source, recursive=recursive,
        pattern=pattern, chunk_size=chunk_size,
    )
    if not documents:
        return IngestResponse(path=path, ingested=0, skipped=0, found=0)
    ids = await engine.index_documents(documents)
    found = len(documents)
    ingested = len(ids)
    return IngestResponse(
        path=path, ingested=ingested, skipped=found - ingested, found=found,
    )
