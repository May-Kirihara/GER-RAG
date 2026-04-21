"""REST integration tests for the memory service endpoints (Phase S2)."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.index.faiss_index import FaissIndex
from gaottt.server.app import app
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore
from tests.integration.test_engine_archive_ttl import StubEmbedder


@pytest.fixture
async def rest_client(tmp_path):
    cfg = GaOTTTConfig(
        embedding_dim=32,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "ger.db"),
        faiss_index_path=str(tmp_path / "ger.faiss"),
        flush_interval_seconds=999.0,
        default_hypothesis_ttl_seconds=60.0,
    )
    eng = GaOTTTEngine(
        config=cfg,
        embedder=StubEmbedder(dimension=32),
        faiss_index=FaissIndex(dimension=32),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=cfg.db_path),
    )
    await eng.startup()
    app.state.engine = eng
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await eng.shutdown()


async def test_remember_returns_id_and_persists(rest_client):
    resp = await rest_client.post(
        "/remember",
        json={"content": "uv beats pip for gaottt", "source": "user", "tags": ["pref"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["duplicate"] is False
    assert data["id"]
    assert data["expires_at"] is None  # permanent for source=user


async def test_remember_duplicate_content_flagged(rest_client):
    body = {"content": "duplicate fact", "source": "agent"}
    first = await rest_client.post("/remember", json=body)
    assert first.json()["id"]
    second = await rest_client.post("/remember", json=body)
    assert second.json()["duplicate"] is True
    assert second.json()["id"] is None


async def test_remember_hypothesis_gets_default_ttl(rest_client):
    resp = await rest_client.post(
        "/remember",
        json={"content": "hypothesis about gravity", "source": "hypothesis"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["expires_at"] is not None


async def test_recall_returns_items_with_source_and_displacement(rest_client):
    await rest_client.post(
        "/remember", json={"content": "tidal dynamics in FAISS", "source": "agent"},
    )
    resp = await rest_client.post("/recall", json={"query": "tidal", "top_k": 3})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    item = data["items"][0]
    assert "source" in item
    assert "displacement_norm" in item
    assert "tags" in item


async def test_recall_source_filter_narrows_results(rest_client):
    await rest_client.post(
        "/remember", json={"content": "agent note alpha", "source": "agent"},
    )
    await rest_client.post(
        "/remember", json={"content": "user note alpha", "source": "user"},
    )
    resp = await rest_client.post(
        "/recall",
        json={"query": "alpha", "top_k": 5, "source_filter": ["user"]},
    )
    data = resp.json()
    assert all(item["source"] == "user" for item in data["items"])


async def test_forget_then_restore_roundtrip(rest_client):
    create = await rest_client.post(
        "/remember", json={"content": "transient record"},
    )
    node_id = create.json()["id"]

    forget = await rest_client.post(
        "/forget", json={"node_ids": [node_id]},
    )
    assert forget.json() == {"affected": 1, "requested": 1, "hard": False}

    restore = await rest_client.post(
        "/restore", json={"node_ids": [node_id]},
    )
    assert restore.json() == {"affected": 1, "requested": 1}


async def test_forget_hard_removes_document(rest_client):
    create = await rest_client.post(
        "/remember", json={"content": "to be hard-deleted"},
    )
    node_id = create.json()["id"]

    forget = await rest_client.post(
        "/forget", json={"node_ids": [node_id], "hard": True},
    )
    assert forget.json()["hard"] is True
    assert forget.json()["affected"] == 1

    restore = await rest_client.post(
        "/restore", json={"node_ids": [node_id]},
    )
    assert restore.json()["affected"] == 0  # gone for good


async def test_revalidate_updates_certainty(rest_client):
    create = await rest_client.post(
        "/remember", json={"content": "fact to revalidate", "certainty": 0.5},
    )
    node_id = create.json()["id"]

    resp = await rest_client.post(
        "/revalidate",
        json={"node_id": node_id, "certainty": 0.95, "emotion": 0.3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["found"] is True
    assert data["certainty"] == pytest.approx(0.95)
    assert data["emotion_weight"] == pytest.approx(0.3)


async def test_revalidate_unknown_node_returns_404(rest_client):
    resp = await rest_client.post(
        "/revalidate",
        json={"node_id": "00000000-0000-0000-0000-000000000000", "certainty": 1.0},
    )
    assert resp.status_code == 404


async def test_explore_returns_diversity_marker(rest_client):
    await rest_client.post(
        "/remember", json={"content": "some wandering note"},
    )
    resp = await rest_client.post(
        "/explore", json={"query": "wander", "diversity": 0.7, "top_k": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["diversity"] == pytest.approx(0.7)
    assert "items" in data


async def test_legacy_index_and_query_still_work(rest_client):
    idx = await rest_client.post(
        "/index", json={"documents": [{"content": "legacy path still green"}]},
    )
    assert idx.status_code == 200
    assert idx.json()["count"] == 1

    q = await rest_client.post("/query", json={"text": "legacy path", "top_k": 3})
    assert q.status_code == 200
    qdata = q.json()
    assert qdata["count"] >= 1
    # Legacy shape: no source/tags/displacement in results
    assert "source" not in qdata["results"][0]
