"""MV1 — client for the host-shared embedding service (WP-6).

The engine-side counterpart to ``gaottt.embedding.service``. ``RemoteEmbedder``
talks to a running embedding service over localhost HTTP and exposes the same
``EmbedderProtocol`` surface as ``RuriEmbedder``, so the engine layer is
agnostic to whether vectors come from a local model or a shared service.

Wire protocol (docs/maintainers/multiverse-implementation-plan.md §MV1-1):
  GET  /info   -> JSON {"model_name", "dimension", "version", "batch_size"}
  POST /encode -> JSON request  {"kind": "query" | "document", "texts": [...]}
              -> application/x-msgpack
                 {"shape": [N, dim], "dtype": "float32", "data": <float32 bytes>}

``/info`` is fetched once at construction and cached; ``dimension`` /
``embedder_id`` / ``embedder_version`` read from the cache. Vectors are
L2-normalized float32 — normalization is the service's responsibility, the
client trusts it (no re-normalization, matching ``RuriEmbedder`` which leans
on ``normalize_embeddings=True`` server-side).

Retry policy (§MV1-3): connection errors are retried exactly once with a
0.5s backoff, then converted to the builtin ``ConnectionError`` so callers
need not import httpx to catch "service unreachable". HTTP status errors
(4xx/5xx) and timeouts are surfaced immediately with no retry.
"""
from __future__ import annotations

import time

import httpx
import msgpack
import numpy as np

# Hard-coded single-retry backoff for connection errors (§MV1-3).
_CONNECT_RETRY_BACKOFF_SECONDS = 0.5


class RemoteEmbedder:
    def __init__(
        self,
        endpoint: str,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        # The DI seam lets tests inject an httpx.Client wired to a
        # MockTransport without this class knowing about transports.
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        self._info: dict = self._fetch_info()

    # -- /info (cached at construction) -------------------------------------

    def _fetch_info(self) -> dict:
        response = self._request_with_retry("GET", "/info")
        return response.json()

    @property
    def dimension(self) -> int:
        return self._info["dimension"]

    @property
    def embedder_id(self) -> str:
        return self._info["model_name"]

    @property
    def embedder_version(self) -> str:
        return self._info["version"]

    # -- /encode ------------------------------------------------------------

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        return self._encode("document", texts)

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        return self._encode("query", texts)

    def encode_query(self, text: str) -> np.ndarray:
        return self.encode_queries([text])

    def _encode(self, kind: str, texts: list[str]) -> np.ndarray:
        response = self._request_with_retry(
            "POST", "/encode", json={"kind": kind, "texts": texts}
        )
        return self._decode_array(response)

    def _decode_array(self, response: httpx.Response) -> np.ndarray:
        body = msgpack.unpackb(response.content)
        arr = np.frombuffer(body["data"], dtype=body["dtype"]).reshape(body["shape"])
        return arr.astype(np.float32)

    # -- transport ----------------------------------------------------------

    def _request_with_retry(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = f"{self._endpoint}{path}"
        for attempt in range(2):  # initial attempt + one connection-error retry
            try:
                response = self._client.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except httpx.ConnectError as exc:
                if attempt == 0:
                    time.sleep(_CONNECT_RETRY_BACKOFF_SECONDS)
                    continue
                raise ConnectionError(
                    f"Embedding service at {self._endpoint} unreachable after retry: {exc}"
                ) from exc
            except (httpx.HTTPStatusError, httpx.TimeoutException):
                raise
        # Unreachable: every iteration either returns or raises.
        raise ConnectionError(
            f"Embedding service request to {url} exhausted retries"
        )
