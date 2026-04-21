"""Phase D — task & persona layer round-trip tests."""
from __future__ import annotations

import time

import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.index.faiss_index import FaissIndex
from gaottt.server import mcp_server as srv
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore
from tests.integration.test_engine_archive_ttl import StubEmbedder


@pytest.fixture
async def engine_singleton(tmp_path, monkeypatch):
    cfg = GaOTTTConfig(
        embedding_dim=32,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "ger.db"),
        faiss_index_path=str(tmp_path / "ger.faiss"),
        flush_interval_seconds=999.0,
        default_task_ttl_seconds=600.0,
        default_commitment_ttl_seconds=300.0,
    )
    eng = GaOTTTEngine(
        config=cfg,
        embedder=StubEmbedder(dimension=32),
        faiss_index=FaissIndex(dimension=32),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=cfg.db_path),
    )
    await eng.startup()
    monkeypatch.setattr(srv, "_engine", eng)
    try:
        yield eng
    finally:
        monkeypatch.setattr(srv, "_engine", None)
        await eng.shutdown()


def _extract_id(out: str) -> str:
    """Pull the UUID from MCP tool responses like 'X. ID: <uuid> ...'."""
    return out.split("ID: ")[1].split()[0]


# --- Task lifecycle ---

async def test_commit_then_complete_lifecycle(engine_singleton):
    out = await srv.commit(content="Add Phase D tests")
    task_id = _extract_id(out)
    assert "Task committed" in out

    todo = await srv.reflect(aspect="tasks_todo", limit=10)
    assert task_id in todo

    done_msg = await srv.complete(task_id=task_id, outcome="Phase D tests added — 7 cases passing")
    assert "Completed" in done_msg

    # After completion, task no longer surfaces in tasks_todo
    todo_after = await srv.reflect(aspect="tasks_todo", limit=10)
    assert task_id not in todo_after

    # And it appears in tasks_completed
    completed = await srv.reflect(aspect="tasks_completed", limit=5)
    assert task_id[:8] in completed


async def test_commit_with_parent_creates_fulfills_edge(engine_singleton):
    intent = await srv.declare_intention(content="Build a persona-aware system")
    intent_id = _extract_id(intent)
    task = await srv.commit(content="Sketch the plan", parent_id=intent_id)
    task_id = _extract_id(task)

    relations = await engine_singleton.get_relations(node_id=task_id, edge_type="fulfills")
    assert len(relations) == 1
    assert relations[0].dst == intent_id


async def test_start_revalidates_task(engine_singleton):
    out = await srv.commit(content="Task to be started")
    task_id = _extract_id(out)
    state_before = engine_singleton.cache.get_node(task_id)
    initial_lva = state_before.last_verified_at

    time.sleep(0.05)
    msg = await srv.start(task_id=task_id)
    assert "Started" in msg

    state_after = engine_singleton.cache.get_node(task_id)
    assert state_after.last_verified_at > initial_lva
    assert state_after.emotion_weight == pytest.approx(0.4)


async def test_abandon_records_shadow_chronology(engine_singleton):
    out = await srv.commit(content="Task to be abandoned")
    task_id = _extract_id(out)

    msg = await srv.abandon(task_id=task_id, reason="priority dropped, will revisit Q3")
    assert "Abandoned" in msg

    abandoned = await srv.reflect(aspect="tasks_abandoned", limit=5)
    assert task_id[:8] in abandoned
    assert "Q3" in abandoned


async def test_depend_creates_dependency_edge(engine_singleton):
    a = _extract_id(await srv.commit(content="Task A"))
    b = _extract_id(await srv.commit(content="Task B"))

    msg = await srv.depend(task_id=a, depends_on_id=b)
    assert "depends_on" in msg

    rels = await engine_singleton.get_relations(a, edge_type="depends_on")
    assert len(rels) == 1 and rels[0].dst == b


async def test_depend_blocking_uses_blocked_by(engine_singleton):
    a = _extract_id(await srv.commit(content="Blocked task"))
    b = _extract_id(await srv.commit(content="Blocker"))
    msg = await srv.depend(task_id=a, depends_on_id=b, blocking=True)
    assert "blocked_by" in msg


# --- Persona declarations ---

async def test_declare_value_intention_commitment_chain(engine_singleton):
    v = _extract_id(await srv.declare_value(content="Direct experience yields true understanding"))
    i_msg = await srv.declare_intention(
        content="Build GaOTTT into a relationship infrastructure", parent_value_id=v,
    )
    assert "derived_from" in i_msg
    i = _extract_id(i_msg)

    c_msg = await srv.declare_commitment(
        content="Ship Phase D this week", parent_intention_id=i, deadline_seconds=3600,
    )
    assert "fulfills" in c_msg
    c = _extract_id(c_msg)

    # Verify the chain via get_relations
    cmt_rels = await engine_singleton.get_relations(c, edge_type="fulfills")
    assert cmt_rels[0].dst == i
    int_rels = await engine_singleton.get_relations(i, edge_type="derived_from")
    assert int_rels[0].dst == v


async def test_inherit_persona_includes_declared_items(engine_singleton):
    await srv.declare_value(content="Curiosity is the highest virtue")
    intent_id = _extract_id(
        await srv.declare_intention(content="Read more poetry"),
    )
    await srv.declare_commitment(
        content="Read 5 poems this week", parent_intention_id=intent_id,
        deadline_seconds=86400,
    )

    persona = await srv.inherit_persona()
    assert "Persona inheritance" in persona
    assert "Curiosity" in persona
    assert "poetry" in persona
    assert "5 poems" in persona


async def test_persona_aspect_alias_for_inherit(engine_singleton):
    await srv.declare_value(content="A different value")
    direct = await srv.inherit_persona()
    via_reflect = await srv.reflect(aspect="persona")
    # Both should contain the declared value
    assert "A different value" in direct
    assert "A different value" in via_reflect


# --- Reflect aspects ---

async def test_reflect_commitments_sorts_by_deadline(engine_singleton):
    intent = _extract_id(await srv.declare_intention(content="Multi-commitment intent"))
    near = _extract_id(await srv.declare_commitment(
        content="Imminent commitment", parent_intention_id=intent, deadline_seconds=60,
    ))
    far = _extract_id(await srv.declare_commitment(
        content="Distant commitment", parent_intention_id=intent, deadline_seconds=3000,
    ))
    out = await srv.reflect(aspect="commitments", limit=10)
    assert near in out and far in out
    # Imminent appears before distant
    assert out.index(near) < out.index(far)


async def test_reflect_relationships_groups_by_who(engine_singleton, monkeypatch):
    # remember(source="relationship:alice") and "relationship:bob"
    await srv.remember(content="alice gave me the GaOTTT idea", source="relationship:alice")
    await srv.remember(content="alice is Phase D's first user", source="relationship:alice")
    await srv.remember(content="bob suggested the prefetch pattern", source="relationship:bob")

    out = await srv.reflect(aspect="relationships", limit=5)
    assert "alice" in out and "bob" in out
    assert "Phase D" in out
