"""MV1 — RemoteEmbedder unit tests (test-first / RED stage).

These tests assume the ``gaottt.embedding.remote.RemoteEmbedder`` contract
pinned in ``docs/maintainers/multiverse-implementation-plan.md`` §MV1-3 and
the wire protocol in §MV1-1. Until WP-6 lands the module, importing it raises
``ModuleNotFoundError`` — the expected test-first RED state. Once WP-6
implements the contract below, every test here should turn GREEN with no
further edits.

To keep pytest collection intact (WP-1 learning: a module-top-level import of
an unimplemented module aborts collection for the whole suite), the
unimplemented module is imported **inside each test function**. Module
top-level imports are limited to already-available libraries (httpx / msgpack
/ numpy / pytest / json).

Pinned contract (what these tests assert):

  GET  /info   -> JSON {"model_name", "dimension", "version", "batch_size"}
  POST /encode -> JSON request  {"kind": "query" | "document", "texts": [...]}
              -> application/x-msgpack
                 {"shape": [N, dim], "dtype": "float32",
                  "data": <np.float32 little-endian bytes>}

  class RemoteEmbedder:
      def __init__(self, endpoint: str, timeout: float = 30.0,
                   client: httpx.Client | None = None): ...
        # GET /info on construction and cache it.
        # httpx.ConnectError (after the single connection-error retry) is
        # converted to the builtin ConnectionError so callers need not import
        # httpx to catch "service unreachable".
      retry: connection errors only, exactly once, 0.5s backoff.
             encode 4xx/5xx and timeouts are NOT retried.
      dimension / embedder_id / embedder_version -> cached /info fields.
      encode_documents / encode_queries / encode_query -> POST /encode.

Returned vectors are L2-normalized float32; RemoteEmbedder trusts the
service-side normalization (does not re-normalize).
"""
from __future__ import annotations

import json

import httpx
import msgpack
import numpy as np
import pytest

DIM = 32
ENDPOINT = "http://stub.local"


# ---------------------------------------------------------------------------
# response builders (test-side only; the service is the one that packs these)
# ---------------------------------------------------------------------------

def _info_response(**overrides) -> httpx.Response:
    payload = {
        "model_name": "stub-service",
        "dimension": DIM,
        "version": "stub-v0",
        "batch_size": 16,
    }
    payload.update(overrides)
    return httpx.Response(200, json=payload)


def _unit_vectors(n: int, dim: int = DIM) -> np.ndarray:
    """Deterministic batch of n L2-normalized float32 row vectors."""
    rng = np.random.default_rng(0)
    v = rng.standard_normal((n, dim)).astype(np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-9
    return v


def _encode_response(vecs: np.ndarray) -> httpx.Response:
    body = msgpack.packb(
        {
            "shape": list(vecs.shape),
            "dtype": "float32",
            "data": vecs.astype(np.float32).tobytes(),
        }
    )
    return httpx.Response(
        200,
        content=body,
        headers={"content-type": "application/x-msgpack"},
    )


def _client(handler) -> httpx.Client:
    """Wrap a MockTransport handler in a sync httpx.Client (DI seam)."""
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# 1. __init__ caches /info and exposes it via properties
# ---------------------------------------------------------------------------

def test_init_caches_info_and_exposes_properties():
    from gaottt.embedding.remote import RemoteEmbedder

    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return _info_response(
            model_name="stub-service",
            dimension=DIM,
            version="stub-v0",
            batch_size=16,
        )

    embedder = RemoteEmbedder(ENDPOINT, client=_client(handler))

    assert embedder.dimension == DIM
    assert embedder.embedder_id == "stub-service"
    assert embedder.embedder_version == "stub-v0"
    # /info was hit exactly once during construction (cached thereafter).
    assert seen_paths == ["/info"]


# ---------------------------------------------------------------------------
# 2. /info connection failure -> builtin ConnectionError (after 1 retry)
# ---------------------------------------------------------------------------

def test_init_raises_connection_error_when_info_unreachable():
    from gaottt.embedding.remote import RemoteEmbedder

    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        raise httpx.ConnectError("service down")

    with pytest.raises(ConnectionError):
        RemoteEmbedder(ENDPOINT, client=_client(handler))
    # original attempt + exactly one connection-error retry.
    assert hits["n"] == 2


# ---------------------------------------------------------------------------
# 3. dimension reflects /info (RemoteEmbedder reports; the factory in WP-6
#    is what rejects dim mismatch against config.embedding_dim)
# ---------------------------------------------------------------------------

def test_dimension_reflects_info_value():
    from gaottt.embedding.remote import RemoteEmbedder

    def handler(request: httpx.Request) -> httpx.Response:
        return _info_response(dimension=64)

    embedder = RemoteEmbedder(ENDPOINT, client=_client(handler))
    assert embedder.dimension == 64


# ---------------------------------------------------------------------------
# 4. encode_documents wire protocol: kind=document, shape/dtype decoded
# ---------------------------------------------------------------------------

def test_encode_documents_posts_kind_document_and_decodes_array():
    from gaottt.embedding.remote import RemoteEmbedder

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return _info_response()
        assert request.url.path == "/encode"
        captured["payload"] = json.loads(request.content)
        n = len(captured["payload"]["texts"])
        return _encode_response(_unit_vectors(n))

    embedder = RemoteEmbedder(ENDPOINT, client=_client(handler))
    arr = embedder.encode_documents(["a b", "c d", "e f"])

    assert arr.shape == (3, DIM)
    assert arr.dtype == np.float32
    assert captured["payload"]["kind"] == "document"
    assert captured["payload"]["texts"] == ["a b", "c d", "e f"]


# ---------------------------------------------------------------------------
# 5. encode_queries wire protocol: kind=query
# ---------------------------------------------------------------------------

def test_encode_queries_posts_kind_query_and_decodes_array():
    from gaottt.embedding.remote import RemoteEmbedder

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return _info_response()
        captured["payload"] = json.loads(request.content)
        n = len(captured["payload"]["texts"])
        return _encode_response(_unit_vectors(n))

    embedder = RemoteEmbedder(ENDPOINT, client=_client(handler))
    arr = embedder.encode_queries(["q1", "q2"])

    assert arr.shape == (2, DIM)
    assert arr.dtype == np.float32
    assert captured["payload"]["kind"] == "query"
    assert captured["payload"]["texts"] == ["q1", "q2"]


# ---------------------------------------------------------------------------
# 6. encode_query returns a single (1, dim) row
# ---------------------------------------------------------------------------

def test_encode_query_returns_single_row():
    from gaottt.embedding.remote import RemoteEmbedder

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return _info_response()
        return _encode_response(_unit_vectors(1))

    embedder = RemoteEmbedder(ENDPOINT, client=_client(handler))
    arr = embedder.encode_query("only one")
    assert arr.shape == (1, DIM)
    assert arr.dtype == np.float32


# ---------------------------------------------------------------------------
# 7. Returned vectors are L2-normalized (service normalizes; client trusts)
# ---------------------------------------------------------------------------

def test_returned_vectors_are_l2_normalized():
    from gaottt.embedding.remote import RemoteEmbedder

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return _info_response()
        return _encode_response(_unit_vectors(5))

    embedder = RemoteEmbedder(ENDPOINT, client=_client(handler))
    arr = embedder.encode_documents(["t0", "t1", "t2", "t3", "t4"])
    norms = np.linalg.norm(arr, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


# ---------------------------------------------------------------------------
# 8. 503 (queue full) is surfaced and NOT retried
# ---------------------------------------------------------------------------

def test_503_is_not_retried():
    from gaottt.embedding.remote import RemoteEmbedder

    encode_hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return _info_response()
        encode_hits["n"] += 1
        return httpx.Response(
            503,
            headers={"Retry-After": "1"},
            json={"detail": "queue full"},
        )

    embedder = RemoteEmbedder(ENDPOINT, client=_client(handler))
    with pytest.raises(httpx.HTTPStatusError):
        embedder.encode_documents(["a"])
    # 4xx/5xx on encode must not trigger a retry.
    assert encode_hits["n"] == 1


# ---------------------------------------------------------------------------
# 9. Timeout surfaces and is NOT retried (timeouts are not connection errors)
# ---------------------------------------------------------------------------

def test_timeout_surfaces_and_is_not_retried():
    from gaottt.embedding.remote import RemoteEmbedder

    encode_hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return _info_response()
        encode_hits["n"] += 1
        raise httpx.ReadTimeout("read timed out")

    # timeout=0.001 documents intent; the handler raises the exception directly
    # so the test does not depend on MockTransport wall-clock enforcement.
    embedder = RemoteEmbedder(ENDPOINT, timeout=0.001, client=_client(handler))
    with pytest.raises(httpx.TimeoutException):
        embedder.encode_documents(["a"])
    assert encode_hits["n"] == 1


# ---------------------------------------------------------------------------
# 10. Connection error retried exactly once, then succeeds
# ---------------------------------------------------------------------------

def test_connection_error_retried_once_then_succeeds():
    from gaottt.embedding.remote import RemoteEmbedder

    info_hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            info_hits["n"] += 1
            if info_hits["n"] == 1:
                raise httpx.ConnectError("transient")
            return _info_response()
        return _encode_response(_unit_vectors(1))

    embedder = RemoteEmbedder(ENDPOINT, client=_client(handler))
    # Construction succeeded on the second /info attempt.
    assert info_hits["n"] == 2
    assert embedder.dimension == DIM
    # The encode path still works after the retried init.
    arr = embedder.encode_query("post retry")
    assert arr.shape == (1, DIM)


# ---------------------------------------------------------------------------
# 11. DI seam: an externally-built client is used as-is
# ---------------------------------------------------------------------------

def test_di_client_argument_is_used():
    from gaottt.embedding.remote import RemoteEmbedder

    info_hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            info_hits["n"] += 1
            return _info_response()
        return _encode_response(_unit_vectors(1))

    injected = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = RemoteEmbedder(ENDPOINT, client=injected)
    assert embedder.dimension == DIM
    # The injected transport handled the /info call.
    assert info_hits["n"] == 1
