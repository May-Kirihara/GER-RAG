"""MV1 — host-shared embedding service (FastAPI).

Exposes a RURI model (or any EmbedderProtocol) over localhost HTTP so that
multiple engine processes share a single in-RAM/VRAM model load. The engine
side consumes this via ``gaottt.embedding.remote.RemoteEmbedder`` (WP-6).

DI seam
-------
``create_app(embedder)`` returns a fully-wired FastAPI app bound to the given
embedder. Tests pass a lightweight stub; the ``__main__`` entry is the only
place that constructs the heavy ``RuriEmbedder``. This keeps unit/integration
tests deterministic and free of the real model download.

Wire protocol (docs/maintainers/multiverse-implementation-plan.md §MV1-1)
-------------------------------------------------------------------------
POST /encode  {"kind": "query"|"document", "texts": [...]}
              -> application/x-msgpack
                 {"shape": [N, dim], "dtype": "float32", "data": <bytes>}
GET  /info    -> {"model_name", "dimension", "version", "batch_size"}
GET  /healthz -> {"status": "ok"}

Vectors are L2-normalized float32; normalization and RURI prefix application
are the embedder's responsibility (the service forwards raw text). The service
is unauthenticated and MUST bind to localhost only.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Literal

import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response
from msgpack import packb
from pydantic import BaseModel, ValidationError

from gaottt.embedding.base import EmbedderProtocol

logger = logging.getLogger(__name__)

# Input caps (§MV1-1 "shared SPOF defence"). Exceeding any is 413.
MAX_TEXTS = 256
MAX_TOTAL_CHARS = 200_000
MAX_BODY_BYTES = 10 * 1024 * 1024

# /info batch_size is informational: v1 forwards one request's whole texts
# list as a single encode call (per-request batching, not cross-request
# micro-batching — the latter is MV1.5).
INFO_BATCH_SIZE = 32

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7879
_DEFAULT_MODEL = "cl-nagoya/ruri-v3-310m"
_DEFAULT_MAX_QUEUE = 32
_LOCALHOST_HOSTS = {"127.0.0.1", "localhost", "::1"}


class EncodeRequest(BaseModel):
    kind: Literal["query", "document"]
    texts: list[str]


class _AdmissionCounter:
    """In-event-loop-atomic counter capping concurrent in-flight requests.

    Asyncio request handlers run on a single thread, so ``try_acquire``
    (check-then-increment with no ``await`` between) and ``release`` are
    race-free. Capacity is ``max_queue + 1``: one slot holds the GPU while up
    to ``max_queue`` others wait on the GPU semaphore; the next arrival is
    rejected with 503.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._occupied = 0

    def try_acquire(self) -> bool:
        if self._occupied >= self._capacity:
            return False
        self._occupied += 1
        return True

    def release(self) -> None:
        if self._occupied > 0:
            self._occupied -= 1


def create_app(
    embedder: EmbedderProtocol,
    *,
    max_queue: int = _DEFAULT_MAX_QUEUE,
) -> FastAPI:
    """Build a FastAPI app that serves ``embedder`` over the MV1 wire protocol.

    ``embedder`` is held in closure; it must expose ``dimension``,
    ``embedder_id``, ``embedder_version``, ``encode_documents`` and
    ``encode_queries`` (the EmbedderProtocol surface). Tests inject a stub;
    the ``__main__`` entry injects a real ``RuriEmbedder``.
    """
    app = FastAPI(title="GaOTTT Embedding Service")

    # GPU serialization: one encode call on the model at a time. A request's
    # whole ``texts`` list is forwarded as one batch, so the underlying
    # model's per-batch efficiency is preserved.
    gpu_lock = asyncio.Semaphore(1)
    admission = _AdmissionCounter(max_queue + 1)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/info")
    async def info() -> dict[str, object]:
        return {
            "model_name": embedder.embedder_id,
            "dimension": embedder.dimension,
            "version": embedder.embedder_version,
            "batch_size": INFO_BATCH_SIZE,
        }

    @app.post("/encode")
    async def encode(request: Request) -> Response:
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"request body exceeds {MAX_BODY_BYTES} byte limit",
            )
        try:
            payload = EncodeRequest.model_validate_json(body)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"invalid request body: {exc}",
            ) from exc

        texts = payload.texts
        if not texts:
            raise HTTPException(status_code=400, detail="texts must be a non-empty list")
        if len(texts) > MAX_TEXTS:
            raise HTTPException(
                status_code=413,
                detail=f"too many texts: {len(texts)} > {MAX_TEXTS}",
            )
        total_chars = sum(len(t) for t in texts)
        if total_chars > MAX_TOTAL_CHARS:
            raise HTTPException(
                status_code=413,
                detail=f"total text length {total_chars} exceeds {MAX_TOTAL_CHARS} chars",
            )

        if not admission.try_acquire():
            raise HTTPException(
                status_code=503,
                detail="embedding service queue is full",
                headers={"Retry-After": "1"},
            )
        try:
            async with gpu_lock:
                try:
                    if payload.kind == "document":
                        arr = await asyncio.to_thread(embedder.encode_documents, texts)
                    else:
                        arr = await asyncio.to_thread(embedder.encode_queries, texts)
                except Exception as exc:
                    logger.exception(
                        "encode failed (kind=%s, n=%d)", payload.kind, len(texts)
                    )
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            f"encode failed: {exc}. Consider reducing the request "
                            f"batch size (current: {len(texts)} texts)."
                        ),
                    ) from exc
        finally:
            admission.release()

        arr = np.ascontiguousarray(arr, dtype=np.float32)
        packed = packb(
            {
                "shape": list(arr.shape),
                "dtype": "float32",
                "data": arr.tobytes(),
            }
        )
        return Response(content=packed, media_type="application/x-msgpack")

    return app


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gaottt.embedding.service",
        description="GaOTTT host-shared embedding service (RURI).",
    )
    parser.add_argument("--host", default=_DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--model", default=_DEFAULT_MODEL)
    parser.add_argument("--max-queue", type=int, default=_DEFAULT_MAX_QUEUE)
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.host not in _LOCALHOST_HOSTS:
        # The service has no authentication: /encode must not be reachable off
        # the loopback. Refuse instead of warning — a warning cannot protect an
        # unauthenticated endpoint once bound.
        raise SystemExit(
            f"embedding service refuses to bind to non-localhost host "
            f"{args.host!r}: the service has no authentication and MUST bind "
            f"to localhost. Use one of {sorted(_LOCALHOST_HOSTS)}. "
            f"If you need remote access, put a reverse proxy with auth in "
            f"front."
        )

    # Heavy model load deferred to the real entry point only.
    from gaottt.embedding.ruri import RuriEmbedder
    import uvicorn

    embedder = RuriEmbedder(model_name=args.model)
    app = create_app(embedder, max_queue=args.max_queue)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
