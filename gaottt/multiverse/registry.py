"""MV3 Multiverse — local registry (WP-1).

A single SQLite database at ``<multiverse_root>/registry.db`` that is the
source of truth for *which universe lives at which port* and *which API key
belongs to which universe*. It is the coordination substrate the supervisor
(WP-3) reads and mutates; the shim (WP-4) never touches it directly.

Design notes
------------
* **Lazy connection** — ``__init__`` only records the root; the database is
  opened in ``initialize()`` so constructing the object is side-effect free
  (test-friendly, and lets a supervisor build the object early).

* **Hashed key storage** — only the SHA-256 of each API key is persisted. The
  plaintext is returned from ``create_universe`` exactly once and never stored.
  Comparison at ``verify_api_key`` time is a SQL ``=`` against the hash column
  (the plaintext is never re-read, so a hash-column index lookup is the
  standard credential-database pattern).

* **Port allocation** — ``allocate_port`` consults both the registry (ports
  used by live universes) and the live OS (``is_port_free`` bind check).
  Registry-only allocation would hand out a port another process already
  holds, making the subsequent backend spawn die with ``Address already in
  use``.

* **reconcile** — aligns the on-disk ``<root>/universes/`` directory set with
  the registry, but one-way-safe: a directory without a registry entry is
  WARNING + skip (the supervisor never silently adopts a universe it did not
  create — manual admin intervention), while a registry entry (``active``)
  whose directory vanished is flipped to ``orphan``.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import socket
import time
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

REGISTRY_DB_FILENAME = "registry.db"
UNIVERSES_SUBDIR = "universes"
TRASH_SUBDIR = "trash"

# status values for the ``universes.status`` column.
STATUS_ACTIVE = "active"
STATUS_ORPHAN = "orphan"
STATUS_DELETED = "deleted"

SCHEMA = """
CREATE TABLE IF NOT EXISTS universes (
    universe_id      TEXT PRIMARY KEY,
    owner_label      TEXT NOT NULL,
    port             INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active',
    embedder_id      TEXT NOT NULL,
    embedder_version TEXT NOT NULL,
    created_at       REAL NOT NULL
);

-- B1: defense-in-depth against a concurrent port-allocation race. Two *live*
-- universes can never share a port; the supervisor's create_lock is the
-- primary serialization, this index is the DB-level backstop. The index is
-- partial so a deleted universe's port stays reusable — its row is retained
-- for audit but freed for reallocation (see allocate_port / delete_universe).
CREATE UNIQUE INDEX IF NOT EXISTS idx_universes_port_live
    ON universes(port) WHERE status != 'deleted';

CREATE TABLE IF NOT EXISTS api_keys (
    key_hash    TEXT PRIMARY KEY,
    universe_id TEXT NOT NULL REFERENCES universes(universe_id),
    created_at  REAL NOT NULL,
    revoked_at  REAL
);

CREATE INDEX IF NOT EXISTS idx_api_keys_universe ON api_keys(universe_id);
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def hash_key(plaintext: str) -> str:
    """SHA-256 hex digest of ``plaintext``."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    """A fresh CSPRNG API key (high-entropy, URL-safe)."""
    return secrets.token_urlsafe(32)


def is_port_free(host: str, port: int) -> bool:
    """Return True iff a TCP ``bind`` to ``(host, port)`` succeeds right now.

    A plain ``bind`` without ``SO_REUSEADDR`` is the strict check: any process
    actively holding the port (or a non-TIME_WAIT residual socket) makes this
    return False, which is exactly what port-allocation wants — hand out only
    ports a fresh backend server can actually listen on. The probe socket is
    always closed so the port remains free for the real server.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

class MultiverseRegistry:
    """SQLite-backed registry of universes and their API keys."""

    def __init__(self, multiverse_root: Path) -> None:
        self._root = Path(multiverse_root)
        self._db_path = self._root / REGISTRY_DB_FILENAME
        self._conn: aiosqlite.Connection | None = None

    # -- lifecycle ----------------------------------------------------------

    async def initialize(self) -> None:
        """Create the root + schema, then run ``reconcile``.

        Idempotent: safe to call on an already-populated registry.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / UNIVERSES_SUBDIR).mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        # Multi-process safety: the supervisor and any admin tooling may share
        # this DB. WAL keeps reads non-blocking; busy_timeout waits out write
        # contention instead of raising "database is locked" immediately.
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._conn.execute("PRAGMA busy_timeout = 30000")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        await self.reconcile()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError(
                "MultiverseRegistry.initialize() must be called before use"
            )
        return self._conn

    # -- port allocation ----------------------------------------------------

    async def allocate_port(self, range_start: int, range_end: int) -> int:
        """Return the lowest port in ``[range_start, range_end]`` free in both
        the registry and the OS.

        Raises ``RuntimeError`` if every port in the range is taken.
        Deleted universes do not reserve their port.
        """
        cur = await self._db.execute(
            "SELECT port FROM universes WHERE status != ?", (STATUS_DELETED,)
        )
        used = {row["port"] for row in await cur.fetchall()}
        await cur.close()

        for port in range(range_start, range_end + 1):
            if port in used:
                continue
            if not is_port_free("127.0.0.1", port):
                continue
            return port

        raise RuntimeError(
            f"No free port in range [{range_start}, {range_end}] "
            f"(registry-used={sorted(p for p in used if range_start <= p <= range_end)})"
        )

    # -- universe lifecycle -------------------------------------------------

    async def create_universe(
        self,
        universe_id: str,
        owner_label: str,
        port: int,
        embedder_id: str,
        embedder_version: str,
    ) -> str:
        """Insert a universe row and issue a fresh API key.

        Returns the **plaintext** key — it is handed out exactly once and is
        never persisted. The stored row carries only the SHA-256 hash.
        Raises on a duplicate ``universe_id`` (PRIMARY KEY conflict).
        """
        now = time.time()
        plaintext = generate_api_key()
        key_hash = hash_key(plaintext)
        await self._db.execute(
            "INSERT INTO universes "
            "(universe_id, owner_label, port, status, embedder_id, "
            " embedder_version, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (universe_id, owner_label, port, STATUS_ACTIVE,
             embedder_id, embedder_version, now),
        )
        await self._db.execute(
            "INSERT INTO api_keys (key_hash, universe_id, created_at) "
            "VALUES (?, ?, ?)",
            (key_hash, universe_id, now),
        )
        await self._db.commit()
        return plaintext

    async def delete_universe(self, universe_id: str) -> None:
        """Mark a universe ``deleted`` (row kept for audit) and revoke all its keys.

        No-op if the universe does not exist.
        """
        await self._db.execute(
            "UPDATE universes SET status = ? WHERE universe_id = ?",
            (STATUS_DELETED, universe_id),
        )
        await self._revoke_all_keys(universe_id)
        await self._db.commit()

    async def _revoke_all_keys(self, universe_id: str) -> None:
        await self._db.execute(
            "UPDATE api_keys SET revoked_at = ? "
            "WHERE universe_id = ? AND revoked_at IS NULL",
            (time.time(), universe_id),
        )

    # -- api key verification ----------------------------------------------

    async def verify_api_key(self, plaintext: str) -> str | None:
        """Resolve ``plaintext`` to a universe_id, or None if unknown/revoked.

        Comparison is a SQL ``=`` against the SHA-256 hash column restricted
        to ``revoked_at IS NULL`` — the plaintext is never stored or
        re-compared in Python.
        """
        key_hash = hash_key(plaintext)
        cur = await self._db.execute(
            "SELECT universe_id FROM api_keys "
            "WHERE key_hash = ? AND revoked_at IS NULL",
            (key_hash,),
        )
        row = await cur.fetchone()
        await cur.close()
        return row["universe_id"] if row is not None else None

    async def revoke_api_key(self, key_hash: str) -> None:
        """Revoke a single API key by its hash. Idempotent."""
        await self._db.execute(
            "UPDATE api_keys SET revoked_at = ? "
            "WHERE key_hash = ? AND revoked_at IS NULL",
            (time.time(), key_hash),
        )
        await self._db.commit()

    # -- reads --------------------------------------------------------------

    async def get_universe(self, universe_id: str) -> dict | None:
        cur = await self._db.execute(
            "SELECT * FROM universes WHERE universe_id = ?",
            (universe_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row is not None else None

    async def list_universes(self) -> list[dict]:
        cur = await self._db.execute(
            "SELECT * FROM universes ORDER BY created_at ASC"
        )
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]

    # -- reconciliation -----------------------------------------------------

    async def reconcile(self) -> None:
        """Align ``<root>/universes/`` with the registry.

        * directory present, registry missing  → WARNING + skip (the supervisor
          never silently adopts a universe it did not create; admin must act).
        * registry ``active``, directory gone   → status set to ``orphan``.
        * ``deleted`` universes are never touched (audit rows stay as-is).
        * ``trash/`` (a sibling of ``universes/``) is never scanned.
        """
        universes_dir = self._root / UNIVERSES_SUBDIR
        dir_names: set[str] = set()
        if universes_dir.is_dir():
            for child in universes_dir.iterdir():
                # Defensive: skip any stray "trash" subdir inside universes/
                # (canonical trash lives at <root>/trash/).
                if child.is_dir() and child.name != TRASH_SUBDIR:
                    dir_names.add(child.name)

        # registry universe_id -> status, for active universes only.
        cur = await self._db.execute(
            "SELECT universe_id, status FROM universes"
        )
        rows = await cur.fetchall()
        await cur.close()
        registry_ids = {r["universe_id"]: r["status"] for r in rows}

        # (1) directories the supervisor did not register → WARN + skip.
        for name in sorted(dir_names - set(registry_ids)):
            logger.warning(
                "multiverse: universe directory %r exists under %s but has no "
                "registry entry; left untouched (manual intervention required).",
                name, universes_dir,
            )

        # (2) active universes whose directory vanished → orphan.
        orphaned = [
            uid for uid, status in registry_ids.items()
            if status == STATUS_ACTIVE and uid not in dir_names
        ]
        for uid in sorted(orphaned):
            await self._db.execute(
                "UPDATE universes SET status = ? WHERE universe_id = ?",
                (STATUS_ORPHAN, uid),
            )
        if orphaned:
            await self._db.commit()
