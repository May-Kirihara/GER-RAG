from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import aiosqlite
import msgpack
import numpy as np

from gaottt.core.types import CooccurrenceEdge, DirectedEdge, NodeState
from gaottt.store.base import StoreBase

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id           TEXT PRIMARY KEY,
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    metadata     TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_content_hash ON documents(content_hash);

CREATE TABLE IF NOT EXISTS nodes (
    id               TEXT PRIMARY KEY,
    mass             REAL DEFAULT 1.0,
    temperature      REAL DEFAULT 0.0,
    last_access      REAL,
    sim_history      BLOB,
    displacement     BLOB,
    velocity         BLOB,
    return_count     REAL DEFAULT 0.0,
    expires_at       REAL,
    is_archived      INTEGER DEFAULT 0,
    merged_into      TEXT,
    merge_count      INTEGER DEFAULT 0,
    merged_at        REAL,
    emotion_weight   REAL DEFAULT 0.0,
    certainty        REAL DEFAULT 1.0,
    last_verified_at REAL
);
-- Indexes for archive/TTL/merge columns are created post-migration in initialize(),
-- so older DBs that need ALTER TABLE first can still bootstrap cleanly.

CREATE TABLE IF NOT EXISTS edges (
    src         TEXT,
    dst         TEXT,
    weight      REAL DEFAULT 0.0,
    last_update REAL,
    PRIMARY KEY (src, dst)
);

CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);

CREATE TABLE IF NOT EXISTS directed_edges (
    src         TEXT NOT NULL,
    dst         TEXT NOT NULL,
    edge_type   TEXT NOT NULL,
    weight      REAL DEFAULT 1.0,
    created_at  REAL,
    metadata    TEXT,
    PRIMARY KEY (src, dst, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_directed_src ON directed_edges(src);
CREATE INDEX IF NOT EXISTS idx_directed_dst ON directed_edges(dst);
CREATE INDEX IF NOT EXISTS idx_directed_type ON directed_edges(edge_type);
CREATE INDEX IF NOT EXISTS idx_directed_created ON directed_edges(created_at);
"""


class SqliteStore(StoreBase):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        # Multi-process safety: when several MCP servers (e.g. one per agent
        # terminal) share the same DB, write-lock contention is normal. Wait
        # up to 30 s for a lock instead of raising "database is locked"
        # immediately. WAL keeps reads non-blocking; this only affects writes.
        await self._conn.execute("PRAGMA busy_timeout = 30000")
        # Keep the WAL from growing unbounded under sustained multi-process
        # write load. Default is 1000 pages (~4 MB); we let WAL get a bit
        # larger for throughput, but an explicit autocheckpoint keeps it bounded.
        await self._conn.execute("PRAGMA wal_autocheckpoint = 2000")
        await self._conn.executescript(SCHEMA)
        # Migrate: add columns if missing (older DBs)
        for col, col_type in [
            ("displacement", "BLOB"),
            ("velocity", "BLOB"),
            ("return_count", "REAL DEFAULT 0.0"),
            ("expires_at", "REAL"),
            ("is_archived", "INTEGER DEFAULT 0"),
            ("merged_into", "TEXT"),
            ("merge_count", "INTEGER DEFAULT 0"),
            ("merged_at", "REAL"),
            ("emotion_weight", "REAL DEFAULT 0.0"),
            ("certainty", "REAL DEFAULT 1.0"),
            ("last_verified_at", "REAL"),
        ]:
            try:
                await self._conn.execute(f"SELECT {col} FROM nodes LIMIT 1")
            except aiosqlite.OperationalError:
                await self._conn.execute(f"ALTER TABLE nodes ADD COLUMN {col} {col_type}")
        # Indexes that may be missing on older DBs
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_nodes_archived ON nodes(is_archived)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_nodes_expires_at ON nodes(expires_at)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_nodes_merged_into ON nodes(merged_into)"
        )
        # Phase D: ordering completed/abandoned tasks by recency
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_directed_created ON directed_edges(created_at)"
        )
        await self._conn.commit()

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    async def find_existing_hashes(self, hashes: list[str]) -> set[str]:
        """Return the subset of content hashes that already exist in the DB."""
        assert self._conn is not None
        if not hashes:
            return set()
        placeholders = ",".join("?" for _ in hashes)
        cursor = await self._conn.execute(
            f"SELECT content_hash FROM documents WHERE content_hash IN ({placeholders})",
            hashes,
        )
        return {row[0] async for row in cursor}

    async def save_documents(self, docs: list[dict[str, Any]]) -> None:
        assert self._conn is not None
        await self._conn.executemany(
            "INSERT OR IGNORE INTO documents (id, content, content_hash, metadata) VALUES (?, ?, ?, ?)",
            [
                (
                    d["id"],
                    d["content"],
                    self._content_hash(d["content"]),
                    json.dumps(d.get("metadata")) if d.get("metadata") else None,
                )
                for d in docs
            ],
        )
        await self._conn.commit()

    async def get_document(self, doc_id: str) -> dict[str, Any] | None:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT id, content, metadata FROM documents WHERE id = ?", (doc_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "content": row[1],
            "metadata": json.loads(row[2]) if row[2] else None,
        }

    _NODE_COLS = (
        "id, mass, temperature, last_access, sim_history, return_count, "
        "expires_at, is_archived, merged_into, merge_count, merged_at, "
        "emotion_weight, certainty, last_verified_at"
    )

    async def save_node_states(self, states: list[NodeState]) -> None:
        assert self._conn is not None
        await self._conn.executemany(
            f"INSERT OR REPLACE INTO nodes ({self._NODE_COLS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    s.id, s.mass, s.temperature, s.last_access,
                    msgpack.packb(s.sim_history), s.return_count,
                    s.expires_at, 1 if s.is_archived else 0,
                    s.merged_into, s.merge_count, s.merged_at,
                    s.emotion_weight, s.certainty, s.last_verified_at,
                )
                for s in states
            ],
        )
        await self._conn.commit()

    @staticmethod
    def _row_to_node_state(row: tuple) -> NodeState:
        sim_history = msgpack.unpackb(row[4]) if row[4] else []
        return NodeState(
            id=row[0],
            mass=row[1] if row[1] is not None else 1.0,
            temperature=row[2] if row[2] is not None else 0.0,
            last_access=row[3] if row[3] is not None else time.time(),
            sim_history=sim_history,
            return_count=row[5] if row[5] is not None else 0.0,
            expires_at=row[6] if len(row) > 6 else None,
            is_archived=bool(row[7]) if len(row) > 7 and row[7] is not None else False,
            merged_into=row[8] if len(row) > 8 else None,
            merge_count=int(row[9]) if len(row) > 9 and row[9] is not None else 0,
            merged_at=row[10] if len(row) > 10 else None,
            emotion_weight=row[11] if len(row) > 11 and row[11] is not None else 0.0,
            certainty=row[12] if len(row) > 12 and row[12] is not None else 1.0,
            last_verified_at=row[13] if len(row) > 13 else None,
        )

    async def get_node_states(self, ids: list[str]) -> dict[str, NodeState]:
        assert self._conn is not None
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        cursor = await self._conn.execute(
            f"SELECT {self._NODE_COLS} FROM nodes WHERE id IN ({placeholders})",
            ids,
        )
        result = {}
        async for row in cursor:
            result[row[0]] = self._row_to_node_state(row)
        return result

    async def get_all_node_states(self) -> list[NodeState]:
        assert self._conn is not None
        cursor = await self._conn.execute(
            f"SELECT {self._NODE_COLS} FROM nodes"
        )
        states = []
        async for row in cursor:
            states.append(self._row_to_node_state(row))
        return states

    async def save_edges(self, edges: list[CooccurrenceEdge]) -> None:
        assert self._conn is not None
        await self._conn.executemany(
            "INSERT OR REPLACE INTO edges (src, dst, weight, last_update) VALUES (?, ?, ?, ?)",
            [(e.src, e.dst, e.weight, e.last_update) for e in edges],
        )
        await self._conn.commit()

    async def get_edges_for_node(self, node_id: str) -> list[CooccurrenceEdge]:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT src, dst, weight, last_update FROM edges WHERE src = ? OR dst = ?",
            (node_id, node_id),
        )
        edges = []
        async for row in cursor:
            edges.append(
                CooccurrenceEdge(src=row[0], dst=row[1], weight=row[2], last_update=row[3])
            )
        return edges

    async def get_all_edges(self) -> list[CooccurrenceEdge]:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT src, dst, weight, last_update FROM edges"
        )
        edges = []
        async for row in cursor:
            edges.append(
                CooccurrenceEdge(src=row[0], dst=row[1], weight=row[2], last_update=row[3])
            )
        return edges

    async def save_displacements(self, displacements: dict[str, np.ndarray]) -> None:
        assert self._conn is not None
        await self._conn.executemany(
            "UPDATE nodes SET displacement = ? WHERE id = ?",
            [(disp.tobytes(), node_id) for node_id, disp in displacements.items()],
        )
        await self._conn.commit()

    async def load_displacements(
        self, ids: list[str] | None = None,
    ) -> dict[str, np.ndarray]:
        assert self._conn is not None
        if ids is not None:
            if not ids:
                return {}
            placeholders = ",".join("?" for _ in ids)
            cursor = await self._conn.execute(
                f"SELECT id, displacement FROM nodes "
                f"WHERE displacement IS NOT NULL AND id IN ({placeholders})",
                ids,
            )
        else:
            cursor = await self._conn.execute(
                "SELECT id, displacement FROM nodes WHERE displacement IS NOT NULL"
            )
        result = {}
        async for row in cursor:
            result[row[0]] = np.frombuffer(row[1], dtype=np.float32).copy()
        return result

    async def save_velocities(self, velocities: dict[str, np.ndarray]) -> None:
        assert self._conn is not None
        await self._conn.executemany(
            "UPDATE nodes SET velocity = ? WHERE id = ?",
            [(vel.tobytes(), node_id) for node_id, vel in velocities.items()],
        )
        await self._conn.commit()

    async def load_velocities(
        self, ids: list[str] | None = None,
    ) -> dict[str, np.ndarray]:
        assert self._conn is not None
        if ids is not None:
            if not ids:
                return {}
            placeholders = ",".join("?" for _ in ids)
            cursor = await self._conn.execute(
                f"SELECT id, velocity FROM nodes "
                f"WHERE velocity IS NOT NULL AND id IN ({placeholders})",
                ids,
            )
        else:
            cursor = await self._conn.execute(
                "SELECT id, velocity FROM nodes WHERE velocity IS NOT NULL"
            )
        result = {}
        async for row in cursor:
            result[row[0]] = np.frombuffer(row[1], dtype=np.float32).copy()
        return result

    async def reset_dynamic_state(self) -> tuple[int, int]:
        assert self._conn is not None
        cursor = await self._conn.execute("SELECT COUNT(*) FROM nodes")
        row = await cursor.fetchone()
        nodes_count = row[0] if row else 0

        cursor = await self._conn.execute("SELECT COUNT(*) FROM edges")
        row = await cursor.fetchone()
        edges_count = row[0] if row else 0

        await self._conn.execute(
            "UPDATE nodes SET mass = 1.0, temperature = 0.0, last_access = NULL, "
            "sim_history = NULL, displacement = NULL, velocity = NULL, return_count = 0.0, "
            "expires_at = NULL, is_archived = 0, "
            "merged_into = NULL, merge_count = 0, merged_at = NULL, "
            "emotion_weight = 0.0, certainty = 1.0, last_verified_at = NULL"
        )
        await self._conn.execute("DELETE FROM edges")
        await self._conn.execute("DELETE FROM directed_edges")
        await self._conn.commit()
        return nodes_count, edges_count

    # --- Archive / delete (F4 + F5) ---

    async def set_archived(self, node_ids: list[str], archived: bool) -> int:
        """Soft-delete: flip is_archived flag. Returns count of affected rows."""
        assert self._conn is not None
        if not node_ids:
            return 0
        placeholders = ",".join("?" for _ in node_ids)
        cursor = await self._conn.execute(
            f"UPDATE nodes SET is_archived = ? WHERE id IN ({placeholders})",
            [1 if archived else 0, *node_ids],
        )
        await self._conn.commit()
        return cursor.rowcount or 0

    async def hard_delete_nodes(self, node_ids: list[str]) -> int:
        """Physically delete nodes, their documents, and any edges touching them."""
        assert self._conn is not None
        if not node_ids:
            return 0
        placeholders = ",".join("?" for _ in node_ids)
        await self._conn.execute(
            f"DELETE FROM edges WHERE src IN ({placeholders}) OR dst IN ({placeholders})",
            [*node_ids, *node_ids],
        )
        await self._conn.execute(
            f"DELETE FROM directed_edges "
            f"WHERE src IN ({placeholders}) OR dst IN ({placeholders})",
            [*node_ids, *node_ids],
        )
        cursor = await self._conn.execute(
            f"DELETE FROM nodes WHERE id IN ({placeholders})", node_ids,
        )
        deleted = cursor.rowcount or 0
        await self._conn.execute(
            f"DELETE FROM documents WHERE id IN ({placeholders})", node_ids,
        )
        await self._conn.commit()
        return deleted

    # --- F3: Directed (typed) edges ---

    async def upsert_directed_edge(self, edge: DirectedEdge) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "INSERT OR REPLACE INTO directed_edges "
            "(src, dst, edge_type, weight, created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                edge.src, edge.dst, edge.edge_type, edge.weight, edge.created_at,
                json.dumps(edge.metadata, ensure_ascii=False) if edge.metadata else None,
            ),
        )
        await self._conn.commit()

    async def delete_directed_edge(
        self, src: str, dst: str, edge_type: str | None = None,
    ) -> int:
        assert self._conn is not None
        if edge_type is None:
            cursor = await self._conn.execute(
                "DELETE FROM directed_edges WHERE src = ? AND dst = ?", (src, dst),
            )
        else:
            cursor = await self._conn.execute(
                "DELETE FROM directed_edges WHERE src = ? AND dst = ? AND edge_type = ?",
                (src, dst, edge_type),
            )
        await self._conn.commit()
        return cursor.rowcount or 0

    async def get_directed_edges(
        self,
        node_id: str | None = None,
        edge_type: str | None = None,
        direction: str = "out",
    ) -> list[DirectedEdge]:
        """Fetch directed edges. ``direction`` is 'out' (src=node), 'in' (dst=node), or 'both'.

        With ``node_id=None`` returns every edge (filtered by edge_type if given).
        """
        assert self._conn is not None
        clauses: list[str] = []
        params: list = []
        if node_id is not None:
            if direction == "out":
                clauses.append("src = ?")
                params.append(node_id)
            elif direction == "in":
                clauses.append("dst = ?")
                params.append(node_id)
            elif direction == "both":
                clauses.append("(src = ? OR dst = ?)")
                params.extend([node_id, node_id])
            else:
                raise ValueError(f"Unknown direction: {direction!r}")
        if edge_type is not None:
            clauses.append("edge_type = ?")
            params.append(edge_type)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = await self._conn.execute(
            f"SELECT src, dst, edge_type, weight, created_at, metadata "
            f"FROM directed_edges{where}",
            params,
        )
        edges: list[DirectedEdge] = []
        async for row in cursor:
            metadata = json.loads(row[5]) if row[5] else None
            edges.append(DirectedEdge(
                src=row[0], dst=row[1], edge_type=row[2],
                weight=row[3] if row[3] is not None else 1.0,
                created_at=row[4] if row[4] is not None else 0.0,
                metadata=metadata,
            ))
        return edges

    async def delete_directed_edges_for_node(self, node_id: str) -> int:
        """Drop all directed edges touching a node (used when hard-deleting)."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "DELETE FROM directed_edges WHERE src = ? OR dst = ?", (node_id, node_id),
        )
        await self._conn.commit()
        return cursor.rowcount or 0

    async def expire_due_nodes(self, now: float) -> int:
        """Mark nodes whose expires_at has passed as archived. Returns count."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "UPDATE nodes SET is_archived = 1 "
            "WHERE expires_at IS NOT NULL AND expires_at <= ? AND is_archived = 0",
            (now,),
        )
        await self._conn.commit()
        return cursor.rowcount or 0

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
