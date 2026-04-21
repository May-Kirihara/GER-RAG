"""Tests for SqliteStore.delete_edges (added for B-01b)."""
from __future__ import annotations

import time

import pytest

from gaottt.core.types import CooccurrenceEdge
from gaottt.store.sqlite_store import SqliteStore


@pytest.fixture
async def store(tmp_path):
    s = SqliteStore(db_path=str(tmp_path / "gaottt.db"))
    await s.initialize()
    yield s
    await s.close()


async def test_delete_edges_removes_rows(store):
    now = time.time()
    await store.save_edges([
        CooccurrenceEdge(src="a", dst="b", weight=2.0, last_update=now),
        CooccurrenceEdge(src="a", dst="c", weight=3.0, last_update=now),
        CooccurrenceEdge(src="b", dst="c", weight=1.0, last_update=now),
    ])

    deleted = await store.delete_edges([("a", "b")])
    assert deleted == 1

    remaining = await store.get_all_edges()
    remaining_pairs = {(e.src, e.dst) for e in remaining}
    assert remaining_pairs == {("a", "c"), ("b", "c")}


async def test_delete_edges_normalizes_ordering(store):
    """Caller may pass (dst, src) — rows are stored with (min, max) ordering."""
    now = time.time()
    await store.save_edges([
        CooccurrenceEdge(src="a", dst="b", weight=2.0, last_update=now),
    ])

    deleted = await store.delete_edges([("b", "a")])
    assert deleted == 1
    assert await store.get_all_edges() == []


async def test_delete_edges_empty_is_noop(store):
    now = time.time()
    await store.save_edges([
        CooccurrenceEdge(src="a", dst="b", weight=2.0, last_update=now),
    ])
    deleted = await store.delete_edges([])
    assert deleted == 0
    assert len(await store.get_all_edges()) == 1
