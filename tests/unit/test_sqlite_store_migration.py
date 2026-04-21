"""Verify older DBs without expires_at / is_archived can be reopened."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ger_rag.store.sqlite_store import SqliteStore


def _create_legacy_db(path: Path) -> None:
    """Mimic the schema the project shipped before the F4/F5 migration."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE documents (
                id           TEXT PRIMARY KEY,
                content      TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                metadata     TEXT
            );
            CREATE TABLE nodes (
                id          TEXT PRIMARY KEY,
                mass        REAL DEFAULT 1.0,
                temperature REAL DEFAULT 0.0,
                last_access REAL,
                sim_history BLOB,
                displacement BLOB,
                velocity BLOB,
                return_count REAL DEFAULT 0.0
            );
            CREATE TABLE edges (
                src         TEXT,
                dst         TEXT,
                weight      REAL DEFAULT 0.0,
                last_update REAL,
                PRIMARY KEY (src, dst)
            );
            INSERT INTO documents (id, content, content_hash, metadata)
                VALUES ('legacy-1', 'hello', 'hash-1', NULL);
            INSERT INTO nodes (id, mass, last_access, return_count)
                VALUES ('legacy-1', 1.5, 1234.0, 0.0);
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def legacy_db_path(tmp_path):
    p = tmp_path / "legacy.db"
    _create_legacy_db(p)
    return p


async def test_legacy_db_gets_new_columns_on_initialize(legacy_db_path):
    store = SqliteStore(db_path=str(legacy_db_path))
    await store.initialize()
    try:
        states = await store.get_all_node_states()
        assert len(states) == 1
        assert states[0].id == "legacy-1"
        # New columns should default to None / False without breaking load.
        assert states[0].expires_at is None
        assert states[0].is_archived is False
    finally:
        await store.close()
