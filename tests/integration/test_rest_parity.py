"""REST integration tests for Phase B/C/D endpoints (Phase S5).

The service layer is already exercised by ``tests/integration/test_mcp_tools.py``
and ``test_mcp_phase_d.py``; these tests validate that the REST wiring to the
same services behaves correctly and returns the expected Pydantic shapes.
"""
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


# ---------- Relations ----------

async def test_relate_unrelate_get_relations(rest_client):
    a = (await rest_client.post("/remember", json={"content": "old judgment"})).json()["id"]
    b = (await rest_client.post("/remember", json={"content": "new judgment"})).json()["id"]

    relate = await rest_client.post(
        "/relations",
        json={"src_id": b, "dst_id": a, "edge_type": "supersedes"},
    )
    assert relate.status_code == 200
    assert relate.json()["edge"]["edge_type"] == "supersedes"

    listing = await rest_client.get(f"/relations/{b}", params={"direction": "out"})
    assert listing.status_code == 200
    assert listing.json()["count"] == 1
    assert listing.json()["edges"][0]["dst"] == a

    delete = await rest_client.delete(
        "/relations", params={"src_id": b, "dst_id": a},
    )
    assert delete.status_code == 200
    assert delete.json()["removed"] == 1


async def test_relate_self_returns_400(rest_client):
    a = (await rest_client.post("/remember", json={"content": "self target"})).json()["id"]
    resp = await rest_client.post(
        "/relations",
        json={"src_id": a, "dst_id": a, "edge_type": "derived_from"},
    )
    assert resp.status_code == 400


# ---------- Maintenance ----------

async def test_merge_collapses_two_nodes(rest_client):
    a = (await rest_client.post("/remember", json={"content": "tidal variant one"})).json()["id"]
    b = (await rest_client.post("/remember", json={"content": "tidal variant one extra"})).json()["id"]

    resp = await rest_client.post("/merge", json={"node_ids": [a, b]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["outcomes"][0]["absorbed_id"] in {a, b}


async def test_compact_reports_structure(rest_client):
    await rest_client.post(
        "/remember", json={"content": "will expire", "source": "hypothesis", "ttl_seconds": 0.05},
    )
    import time as _t
    _t.sleep(0.1)
    resp = await rest_client.post("/compact", json={})
    assert resp.status_code == 200
    data = resp.json()
    for key in ("expired", "merged_pairs", "faiss_rebuilt", "vectors_before", "vectors_after"):
        assert key in data


async def test_prefetch_then_status_shape(rest_client):
    await rest_client.post("/remember", json={"content": "prefetch target memo"})
    resp = await rest_client.post(
        "/prefetch", json={"query": "prefetch", "top_k": 3},
    )
    assert resp.status_code == 200
    assert resp.json()["scheduled"] is True

    status = await rest_client.get("/prefetch/status")
    assert status.status_code == 200
    assert "cache" in status.json()
    assert "pool" in status.json()


# ---------- Auto-remember ----------

async def test_auto_remember_returns_candidates(rest_client):
    transcript = (
        "ユーザー: pip禁止。uvを使ってください\n"
        "失敗: numpyにor演算子でValueError\n"
    )
    resp = await rest_client.post(
        "/auto_remember",
        json={"transcript": transcript, "max_candidates": 5},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    assert all("content" in c for c in data["candidates"])


# ---------- Save-Candidates (Plans-Save-Candidates-Hook.md) ----------

async def test_save_candidates_round_trip(rest_client):
    """REST/MCP parity — same input → same SaveCandidatesResponse shape on
    both transports."""
    transcript = (
        "[user] 設計判断: 観測層と物理層を分離\n"
        "[assistant] 観測のみ自動化、save は能動的判断のまま\n"
    )
    resp = await rest_client.post(
        "/save_candidates",
        json={"transcript": transcript, "max_candidates": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "candidates" in data
    assert "count" in data
    assert data["count"] == len(data["candidates"])


async def test_save_candidates_persona_toggle(rest_client):
    """``include_persona=False`` body field omits the persona slot — the
    same knob the Stop hook flips when ambient_recall already injects
    one upstream."""
    resp = await rest_client.post(
        "/save_candidates",
        json={
            "transcript": "[user] 確定: テストは pytest で書く\n",
            "include_persona": False,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["persona"] is None


# ---------- Reflection ----------

async def test_reflect_summary_shape(rest_client):
    await rest_client.post("/remember", json={"content": "one"})
    await rest_client.post("/remember", json={"content": "two"})
    resp = await rest_client.post("/reflect/summary")
    assert resp.status_code == 200
    data = resp.json()
    for key in ("total_memories", "active_memories", "displaced_nodes", "total_edges", "sources"):
        assert key in data
    assert data["total_memories"] >= 2


async def test_reflect_hot_topics_with_limit(rest_client):
    for i in range(3):
        await rest_client.post("/remember", json={"content": f"hot item {i}"})
    resp = await rest_client.post("/reflect/hot_topics", params={"limit": 2})
    assert resp.status_code == 200
    assert len(resp.json()["items"]) <= 2


async def test_reflect_duplicates_structure(rest_client):
    await rest_client.post("/remember", json={"content": "dup content alpha"})
    await rest_client.post("/remember", json={"content": "dup content alpha extra"})
    resp = await rest_client.post("/reflect/duplicates", params={"limit": 5})
    assert resp.status_code == 200
    assert "clusters" in resp.json()


async def test_rest_connections_bucket_persona(rest_client):
    """POST /reflect/connections?bucket=persona filters to persona edges only."""
    # Seed a persona edge (value ↔ intention) and an ingest edge (file ↔ file).
    v = (await rest_client.post("/remember", json={"content": "rest value X", "source": "value"})).json()["id"]
    i = (await rest_client.post("/remember", json={"content": "rest intention X", "source": "intention"})).json()["id"]
    f1 = (await rest_client.post("/remember", json={"content": "rest file A", "source": "file"})).json()["id"]
    f2 = (await rest_client.post("/remember", json={"content": "rest file B", "source": "file"})).json()["id"]
    # Directly plant the co-occurrence edges via the engine.
    eng = app.state.engine
    eng.cache.set_edge(v, i, weight=1.0)
    eng.cache.set_edge(f1, f2, weight=10.0)

    resp = await rest_client.post("/reflect/connections", params={"bucket": "persona", "limit": 10})
    assert resp.status_code == 200
    data = resp.json()
    assert data["filter_bucket"] == "persona"
    assert data["filtered_total"] == 1
    assert len(data["items"]) == 1
    pair = {data["items"][0]["src"], data["items"][0]["dst"]}
    assert pair == {v, i}


async def test_rest_connections_invalid_bucket_422(rest_client):
    """An invalid bucket value is rejected by FastAPI's Literal validation
    with HTTP 422 — the service-layer ValueError never fires."""
    resp = await rest_client.post("/reflect/connections", params={"bucket": "invalid"})
    assert resp.status_code == 422


# ---------- Phase D: tasks ----------

async def test_task_lifecycle_commit_start_complete(rest_client):
    commit = await rest_client.post(
        "/tasks",
        json={"content": "fix the FAISS leak", "deadline_seconds": 600},
    )
    assert commit.status_code == 200
    task_id = commit.json()["id"]
    assert task_id

    start = await rest_client.post(f"/tasks/{task_id}/start")
    assert start.status_code == 200
    assert start.json()["found"] is True

    complete = await rest_client.post(
        f"/tasks/{task_id}/complete",
        json={"outcome": "patched in engine.py", "emotion": 0.7},
    )
    assert complete.status_code == 200
    data = complete.json()
    assert data["outcome_id"]
    assert data["task_id"] == task_id


async def test_task_start_404_when_unknown(rest_client):
    resp = await rest_client.post("/tasks/00000000-0000-0000-0000-000000000000/start")
    assert resp.status_code == 404


async def test_task_abandon_flow(rest_client):
    task_id = (await rest_client.post(
        "/tasks", json={"content": "dropping this later"},
    )).json()["id"]
    resp = await rest_client.post(
        f"/tasks/{task_id}/abandon",
        json={"reason": "priority shifted"},
    )
    assert resp.status_code == 200
    assert resp.json()["reason_id"]


# ---------- Phase D: persona ----------

async def test_declare_value_intention_commitment_chain(rest_client):
    value = await rest_client.post(
        "/persona/values", json={"content": "curiosity is load-bearing"},
    )
    value_id = value.json()["id"]
    assert value_id

    intention = await rest_client.post(
        "/persona/intentions",
        json={"content": "teach by building", "parent_value_id": value_id},
    )
    intention_id = intention.json()["id"]
    assert intention_id
    assert intention.json()["parent_value_id"] == value_id

    commitment = await rest_client.post(
        "/persona/commitments",
        json={
            "content": "ship S5 by next week",
            "parent_intention_id": intention_id,
            "deadline_seconds": 604800,
        },
    )
    assert commitment.status_code == 200
    cdata = commitment.json()
    assert cdata["id"]
    assert cdata["expires_at"]


async def test_inherit_persona_returns_snapshot(rest_client):
    await rest_client.post("/persona/values", json={"content": "care about clarity"})
    resp = await rest_client.get("/persona")
    assert resp.status_code == 200
    data = resp.json()
    for key in ("values", "intentions", "commitments", "styles", "relationships"):
        assert key in data
    assert any(v["content"] == "care about clarity" for v in data["values"])


# ---------- get_node detail (Observation Apparatus Round 2 Stage A) ----------

async def test_get_node_detail_roundtrip(rest_client):
    mem = (await rest_client.post(
        "/remember",
        json={"content": "rest detail probe", "source": "agent", "tags": ["rest-detail"]},
    )).json()
    node_id = mem["id"]

    resp = await rest_client.get(f"/node/{node_id}/detail")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == node_id
    assert body["content"] == "rest detail probe"
    assert body["source"] == "agent"
    assert "rest-detail" in body["tags"]
    assert body["mass"] > 0

    missing = await rest_client.get("/node/no-such-id/detail")
    assert missing.status_code == 404


# ---------- ambient_recall gate diagnostics (Phase T Stage 5 / WP-4) ----------

async def test_ambient_recall_gate_diagnostics_roundtrip(rest_client):
    """POST /ambient_recall must carry ``gate_diagnostics`` (and the
    ``expose_breakdown`` echo) on the response — parity with the ambient
    tool's structured return. The rest_client engine has no ambient gate
    index, so ``bm25_gate`` reports None (gate unavailable). Phase U WP-3:
    the composite diagnostic fields ride the same Pydantic model — in
    mode="or" they are present-but-None on the wire."""
    await rest_client.post(
        "/remember",
        json={"content": "ambient gate diagnostics probe", "source": "agent"},
    )
    resp = await rest_client.post(
        "/ambient_recall",
        json={"query": "ambient gate diagnostics probe", "direct_k": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    diag = body["gate_diagnostics"]
    assert diag is not None
    assert diag["bm25_gate"] is None  # no gate index wired in this fixture
    assert diag["bm25_top_score"] is None
    assert diag["candidates_generated"] >= 1
    assert diag["direct_selected"] == len(body["direct"])
    assert diag["empty_reason"] is None
    assert body["expose_breakdown"] is False
    # Phase U §10 R3 follow-up — 3-arm composite axes exist on the wire
    # (None in "or" mode)
    assert diag["virt_top1"] is None
    assert diag["bm25_top"] is None
    assert diag["composite_signal"] is None


# ---------- ambient composite gate diagnostics (Phase U §10 R3 follow-up) ----------

async def test_ambient_composite_gate_diagnostics_rest_roundtrip(tmp_path, monkeypatch):
    """mode="composite": the composite ``gate_diagnostics`` fields must
    round-trip through POST /ambient_recall with real values on both an
    accepted (virt_hi arm) and a rejected (composite_reject) call.
    Reuses the crafted corpus/artifact fixture from the composite gate
    integration tests (backbone-band corpus + live-fingerprint artifact)."""
    from tests.integration.test_ambient_composite_gate import (
        BM25_REJECT as WEAK_BM25,
        _force_bm25,
        _make_engine,
        _seed_docs,
        _write_valid_artifact,
        N_FLAT_QUERY,
        P_QUERY,
        VIRT_HI,
    )

    eng = _make_engine(tmp_path / "composite")
    await eng.startup()
    await _seed_docs(eng)
    await _write_valid_artifact(eng)
    # rest_client fixture is not used (mode="composite" engine); force the
    # BM25 axis weak the same way the integration tests do.
    _force_bm25(monkeypatch, WEAK_BM25)
    app.state.engine = eng
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            accepted = await client.post(
                "/ambient_recall", json={"query": P_QUERY, "direct_k": 2},
            )
            assert accepted.status_code == 200
            body = accepted.json()
            assert body["count"] >= 1
            diag = body["gate_diagnostics"]
            assert diag["composite_signal"] == "virt_hi"
            assert diag["empty_reason"] is None
            assert diag["virt_top1"] >= VIRT_HI
            assert diag["bm25_top"] == WEAK_BM25

            rejected = await client.post(
                "/ambient_recall", json={"query": N_FLAT_QUERY, "direct_k": 2},
            )
            assert rejected.status_code == 200
            rbody = rejected.json()
            assert rbody["count"] == 0
            rdiag = rbody["gate_diagnostics"]
            assert rdiag["composite_signal"] == "composite_reject"
            assert rdiag["empty_reason"] == "composite_reject"
            assert rdiag["virt_top1"] is not None  # triage axes survive the reject
    finally:
        await eng.shutdown()


# ---------- explore diversity roundtrip (Phase T Stage 6 / WP-5) ----------

async def test_explore_diversity_roundtrip(tmp_path):
    """POST /explore with ``diversity=0.8`` must round-trip the engine's
    diversified presentation path (flag ON) and echo the diversity on the
    response. The shared ``rest_client`` fixture keeps the flag OFF, so
    this test stands up its own engine — the fixture stays untouched."""
    cfg = GaOTTTConfig(
        embedding_dim=32,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "ger.db"),
        faiss_index_path=str(tmp_path / "ger.faiss"),
        flush_interval_seconds=999.0,
        explore_diversified_presentation_enabled=True,
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
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for i in range(3):
                resp = await client.post(
                    "/remember",
                    json={"content": f"explore diversity probe {i}"},
                )
                assert resp.status_code == 200
            resp = await client.post(
                "/explore",
                json={"query": "explore diversity probe", "diversity": 0.8, "top_k": 3},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["diversity"] == pytest.approx(0.8)
        assert body["count"] >= 1
        assert len(body["items"]) == body["count"]
    finally:
        await eng.shutdown()


# ---------- explore selection trace (Phase U WP-5) ----------

async def test_explore_selection_trace_roundtrip(tmp_path):
    """POST /explore under the promoted defaults must carry the WP-5
    selection trace: ``wave_depth`` / ``wave_reached`` on the response,
    and cohort / provenance / in_learn_set on every item breakdown
    (active explore + promoted ttt → in_learn_set is a bool). The recall
    breakdown keeps its legacy field set — the trace fields are strictly
    additive keys."""
    cfg = GaOTTTConfig(
        embedding_dim=32,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "ger.db"),
        faiss_index_path=str(tmp_path / "ger.faiss"),
        flush_interval_seconds=999.0,
    )
    # fixture guard: the promoted defaults themselves are under test
    assert cfg.explore_diversified_presentation_enabled is True
    assert cfg.ttt_qualification_enabled is True

    eng = GaOTTTEngine(
        config=cfg,
        embedder=StubEmbedder(dimension=32),
        faiss_index=FaissIndex(dimension=32),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=cfg.db_path),
    )
    await eng.startup()
    app.state.engine = eng
    try:
        # corpus with an explicit original_id cluster pair + a singleton;
        # batch=1 per doc (no supernova cohort stamping — cluster identity
        # comes from explicit original_id, same convention as the
        # test_engine_explore_diversity fixture)
        docs = [
            (
                "trace probe alpha beta gamma one",
                {"source": "user", "original_id": "book-trace"},
            ),
            (
                "trace probe alpha beta gamma two",
                {"source": "user", "original_id": "book-trace"},
            ),
            ("trace probe alpha beta lateral side", {"source": "user"}),
        ]
        ids = []
        for content, meta in docs:
            indexed = await eng.index_documents(
                [{"content": content, "metadata": meta}],
            )
            ids.append(indexed[0])
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/explore",
                json={"query": "trace probe alpha beta", "diversity": 0.8, "top_k": 3},
            )
            recall = await client.post(
                "/recall",
                json={"query": "trace probe alpha beta", "top_k": 3},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] >= 1
        assert isinstance(body["wave_depth"], int) and body["wave_depth"] >= 1
        assert isinstance(body["wave_reached"], int)
        assert body["wave_reached"] >= body["count"]

        cohort_of = {}
        for item in body["items"]:
            b = item["score_breakdown"]
            assert b is not None
            assert isinstance(b["cohort"], str) and b["cohort"]
            assert b["provenance"] in {"raw", "virtual", "forced"}
            assert isinstance(b["in_learn_set"], bool)
            cohort_of[item["id"]] = b["cohort"]
        # the book-trace pair shares one cohort key on the wire
        assert cohort_of[ids[0]] == cohort_of[ids[1]] == "book-trace"

        # recall breakdown: legacy field set unchanged, trace additive
        assert recall.status_code == 200
        rdata = recall.json()
        legacy_keys = {
            "raw_cosine", "virtual_cosine", "decay_factor", "wave_score",
            "mass_boost", "emotion_term", "certainty_term", "saturation",
            "persona_proximity", "bm25_contributed", "forced_inclusion",
            "qualified", "direct_score", "field_score",
        }
        assert rdata["count"] >= 1
        for item in rdata["items"]:
            b = item["score_breakdown"]
            assert b is not None
            assert legacy_keys <= set(b.keys())
            assert "cohort" in b and "provenance" in b and "in_learn_set" in b
            # active recall + promoted ttt → membership is a real verdict
            assert isinstance(b["in_learn_set"], bool)
    finally:
        await eng.shutdown()


# ---------- recall breakdown qualification segments (Phase U WP-1) ----------

async def test_recall_breakdown_qualification_segments_default_config(rest_client):
    """POST /recall on the shared default-config engine (no flag pins —
    the Phase U WP-1 promoted defaults) must carry the Stage 3 breakdown
    fields on every item: the qualification verdict and the
    pre-saturation direct/field decomposition. Parity with the MCP
    formatter's ``q/d/f/gap`` segments (tests/integration/
    test_promoted_combination.py)."""
    await rest_client.post(
        "/remember",
        json={"content": "promoted breakdown rest probe alpha beta", "source": "user"},
    )
    resp = await rest_client.post(
        "/recall", json={"query": "promoted breakdown rest probe alpha", "top_k": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    for item in data["items"]:
        b = item["score_breakdown"]
        assert b is not None
        # qualification ran under the promoted defaults → verdict present
        assert b["qualified"] is not None
        # pre-saturation decomposition + gap are populated (they are None
        # only when both qualification flags are OFF)
        assert b["direct_score"] is not None
        assert b["field_score"] is not None
        assert isinstance(b["lensing_gap"], float)
    # the near-duplicate probe clears the raw-cosine axis → q=+ on top item
    assert data["items"][0]["score_breakdown"]["qualified"] is True
