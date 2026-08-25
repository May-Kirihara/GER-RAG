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
        # legacy ambient gate — this file's ambient tests pin the pre-WP-D
        # veto contract (empty return on a min_score reject); the promoted
        # OR default is pinned in tests/integration/test_ambient_or_gate.py.
        ambient_gate_or_semantic=False,
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


async def test_recall_passive_is_read_only(rest_client):
    """Ambient Recall — POST /recall with ``passive=true`` returns results
    but reports zero field movement (no mass / displacement change)."""
    await rest_client.post(
        "/remember",
        json={"content": "ambient passive recall probe vega", "source": "agent"},
    )
    resp = await rest_client.post(
        "/recall",
        json={"query": "passive probe vega", "top_k": 3, "passive": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    td = data.get("training_delta")
    if td is not None:
        assert all(
            v == 0.0 for v in (td.get("displacement_changes") or {}).values()
        ), "passive recall moved displacement"
        assert all(
            v == 0.0 for v in (td.get("mass_changes") or {}).values()
        ), "passive recall changed mass"


async def test_ambient_recall_rest_roundtrip(rest_client):
    """Ambient Recall Enrichment — POST /ambient_recall returns the structured
    AmbientRecallResponse JSON; the min_score gate empties it."""
    for i in range(4):
        await rest_client.post(
            "/remember",
            json={"content": f"ambient rest probe {i}", "source": "agent"},
        )
    resp = await rest_client.post(
        "/ambient_recall", json={"query": "ambient rest probe", "direct_k": 2},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    assert 1 <= len(data["direct"]) <= 2
    item = data["direct"][0]
    assert item["source"] == "agent"
    assert "certainty" in item and "age_days" in item
    assert "lensing" in data and "tensions" in data and "persona" in data
    # relevance gate — nothing clears 0.999 → empty response
    gated = await rest_client.post(
        "/ambient_recall",
        json={"query": "ambient rest probe", "min_score": 0.999},
    )
    assert gated.status_code == 200
    assert gated.json()["count"] == 0


async def test_ambient_recall_exclude_tags_rest_parity(rest_client):
    """Refinement Stage 2 — POST /ambient_recall accepts ``exclude_tags``
    (MCP + REST parity rule)."""
    # tagged + plain agent docs
    for i in range(3):
        await rest_client.post(
            "/remember",
            json={
                "content": f"exclude-rest probe {i}",
                "source": "agent",
                "tags": ["smoke-test"] if i == 0 else None,
            },
        )
    resp = await rest_client.post(
        "/ambient_recall",
        json={
            "query": "exclude-rest probe",
            "direct_k": 3,
            "exclude_tags": ["smoke-test"],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    # The smoke-tagged item must not appear in any direct slot.
    for item in data["direct"]:
        assert "smoke-test" not in (item.get("tags") or []), (
            "REST exclude_tags must drop matching items from direct hits"
        )


async def test_recall_training_delta_in_rest_response(rest_client):
    """Phase O Stage 2 — REST JSON response carries TrainingDelta."""
    await rest_client.post(
        "/remember", json={"content": "phase-o-stage-2 alpha gamma", "source": "user"},
    )
    resp = await rest_client.post("/recall", json={"query": "alpha gamma", "top_k": 3})
    assert resp.status_code == 200
    data = resp.json()
    assert "training_delta" in data
    td = data["training_delta"]
    assert td is not None
    for field in [
        "displacement_changes", "mass_changes", "wave_reached_count",
        "wave_max_depth", "persona_hop_reached", "supernova_triggered",
        "cache_hit", "topk_only",
    ]:
        assert field in td, f"missing training_delta field: {field}"
    assert td["cache_hit"] is False
    assert td["supernova_triggered"] is False
    assert isinstance(td["displacement_changes"], dict)
    assert isinstance(td["mass_changes"], dict)


async def test_recall_score_breakdown_in_rest_response(rest_client):
    """Phase O Stage 1 — REST JSON response carries ScoreBreakdown."""
    await rest_client.post(
        "/remember", json={"content": "alpha gamma kappa", "source": "user"},
    )
    resp = await rest_client.post("/recall", json={"query": "alpha gamma", "top_k": 3})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    item = data["items"][0]
    assert "score_breakdown" in item
    b = item["score_breakdown"]
    assert b is not None
    # All breakdown fields present and serialized as plain JSON values
    for field in [
        "raw_cosine", "virtual_cosine", "decay_factor", "wave_score",
        "mass_boost", "emotion_term", "certainty_term", "saturation",
        "persona_proximity", "bm25_contributed", "forced_inclusion",
    ]:
        assert field in b, f"missing breakdown field: {field}"
    # expected_sum is a @property — pydantic doesn't serialize properties
    # by default, so we verify the additive structure ourselves
    expected = (
        b["virtual_cosine"] * b["decay_factor"]
        + b["wave_score"] + b["mass_boost"]
        + b["emotion_term"] + b["certainty_term"]
    ) * b["saturation"]
    final = item["final_score"]
    assert abs(expected - final) <= max(1e-6, abs(final) * 1e-4)


async def test_recall_routing_hint_in_rest_response(rest_client):
    """Phase O Stage 3 — REST JSON response carries RoutingHint when auto-routed."""
    await rest_client.post(
        "/remember",
        json={
            "content": "Phase O Stage 3 を完了する",
            "source": "commitment",
        },
    )
    resp = await rest_client.post(
        "/recall",
        json={"query": "現在 active な commitment は?", "top_k": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "routing_hint" in data
    h = data["routing_hint"]
    assert h is not None
    for field in ("aspect", "pattern_matched", "auto_routed", "reflect_summary"):
        assert field in h, f"missing routing_hint field: {field}"
    assert h["pattern_matched"] is True
    assert h["aspect"] == "commitments"
    assert h["auto_routed"] is True
    assert h["reflect_summary"] is not None
    assert "Phase O Stage 3" in h["reflect_summary"]


async def test_recall_list_mode_truncates_content(rest_client):
    """Phase O Stage 4 — REST ``/recall`` honours ``mode='list'``."""
    long = "alpha gamma " + ("x" * 500)
    await rest_client.post("/remember", json={"content": long, "source": "user"})
    resp = await rest_client.post(
        "/recall", json={"query": "alpha gamma", "top_k": 5, "mode": "list"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    for item in data["items"]:
        assert len(item["content"]) <= 80
        assert "\n" not in item["content"]


async def test_explore_dormant_mode_via_rest(rest_client):
    """Phase O Stage 5 — REST ``/explore`` honours ``mode='dormant'``."""
    import time
    remember = await rest_client.post(
        "/remember", json={"content": "dormant agent memo", "source": "agent"},
    )
    nid = remember.json()["id"]
    # Backdate via the live engine fixture
    engine = app_engine_from_request_state(rest_client)
    state = engine.cache.get_node(nid)
    state.last_access = time.time() - 60 * 86400
    state.mass = 1.0
    resp = await rest_client.post(
        "/explore",
        json={"query": "_ignored", "top_k": 5, "mode": "dormant"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert any(item["id"] == nid for item in data["items"])
    # dormant skips wave → no training_delta / routing_hint
    assert data.get("training_delta") is None
    assert data.get("routing_hint") is None


def app_engine_from_request_state(client):
    """Reach into the ASGI app's stored engine — fixture leaves it on app.state.engine."""
    from gaottt.server.app import app
    return app.state.engine


async def test_recall_auto_route_false_no_summary(rest_client):
    """auto_route=False on the request suppresses the reflect run."""
    await rest_client.post(
        "/remember", json={"content": "no-route check", "source": "commitment"},
    )
    resp = await rest_client.post(
        "/recall",
        json={
            "query": "現在 active な commitment",
            "top_k": 3,
            "auto_route": False,
        },
    )
    data = resp.json()
    h = data.get("routing_hint")
    if h is not None:
        assert h["auto_routed"] is False
        assert h["reflect_summary"] is None


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
