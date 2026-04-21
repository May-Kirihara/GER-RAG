"""Tests for the new archive / forget / TTL columns on SqliteStore."""
from __future__ import annotations

import time

import pytest

from gaottt.core.types import CooccurrenceEdge, NodeState
from gaottt.store.sqlite_store import SqliteStore


@pytest.fixture
async def store(tmp_path):
    s = SqliteStore(db_path=str(tmp_path / "ger.db"))
    await s.initialize()
    yield s
    await s.close()


async def _seed_node(store: SqliteStore, node_id: str, **fields) -> None:
    await store.save_documents([{"id": node_id, "content": f"content for {node_id}", "metadata": None}])
    state = NodeState(id=node_id, last_access=time.time(), **fields)
    await store.save_node_states([state])


async def test_round_trip_preserves_new_fields(store):
    expires = time.time() + 3600
    state = NodeState(id="n1", expires_at=expires, is_archived=True)
    await store.save_documents([{"id": "n1", "content": "c", "metadata": None}])
    await store.save_node_states([state])

    loaded = await store.get_node_states(["n1"])
    assert loaded["n1"].expires_at == pytest.approx(expires)
    assert loaded["n1"].is_archived is True


async def test_set_archived_flips_flag(store):
    await _seed_node(store, "n1")
    await _seed_node(store, "n2")

    affected = await store.set_archived(["n1"], archived=True)
    assert affected == 1
    states = await store.get_node_states(["n1", "n2"])
    assert states["n1"].is_archived is True
    assert states["n2"].is_archived is False

    affected = await store.set_archived(["n1"], archived=False)
    assert affected == 1
    states = await store.get_node_states(["n1"])
    assert states["n1"].is_archived is False


async def test_hard_delete_removes_node_doc_and_edges(store):
    await _seed_node(store, "n1")
    await _seed_node(store, "n2")
    await _seed_node(store, "n3")
    await store.save_edges([
        CooccurrenceEdge(src="n1", dst="n2", weight=2.0, last_update=time.time()),
        CooccurrenceEdge(src="n2", dst="n3", weight=1.0, last_update=time.time()),
    ])

    deleted = await store.hard_delete_nodes(["n2"])
    assert deleted == 1

    remaining_states = await store.get_node_states(["n1", "n2", "n3"])
    assert "n2" not in remaining_states
    assert (await store.get_document("n2")) is None

    edges = await store.get_all_edges()
    assert all("n2" not in (e.src, e.dst) for e in edges)


async def test_expire_due_nodes_archives_only_expired(store):
    now = time.time()
    await _seed_node(store, "old", expires_at=now - 10)
    await _seed_node(store, "future", expires_at=now + 3600)
    await _seed_node(store, "permanent")  # expires_at=None

    affected = await store.expire_due_nodes(now)
    assert affected == 1

    states = await store.get_node_states(["old", "future", "permanent"])
    assert states["old"].is_archived is True
    assert states["future"].is_archived is False
    assert states["permanent"].is_archived is False


async def test_load_displacements_filters_by_ids(store):
    import numpy as np
    await _seed_node(store, "n1")
    await _seed_node(store, "n2")
    await _seed_node(store, "n3")
    await store.save_displacements({
        "n1": np.array([1.0, 0.0], dtype=np.float32),
        "n2": np.array([0.0, 1.0], dtype=np.float32),
        "n3": np.array([1.0, 1.0], dtype=np.float32),
    })

    all_disps = await store.load_displacements()
    assert set(all_disps.keys()) == {"n1", "n2", "n3"}

    subset = await store.load_displacements(ids=["n1", "n3"])
    assert set(subset.keys()) == {"n1", "n3"}

    empty = await store.load_displacements(ids=[])
    assert empty == {}


async def test_load_velocities_filters_by_ids(store):
    import numpy as np
    await _seed_node(store, "n1")
    await _seed_node(store, "n2")
    await store.save_velocities({
        "n1": np.array([0.1, 0.0], dtype=np.float32),
        "n2": np.array([0.0, 0.2], dtype=np.float32),
    })
    subset = await store.load_velocities(ids=["n2"])
    assert set(subset.keys()) == {"n2"}


async def test_reset_dynamic_state_clears_new_columns(store):
    await _seed_node(store, "n1", expires_at=time.time() + 60, is_archived=True)
    nodes_count, _ = await store.reset_dynamic_state()
    assert nodes_count == 1
    states = await store.get_node_states(["n1"])
    assert states["n1"].is_archived is False
    assert states["n1"].expires_at is None
