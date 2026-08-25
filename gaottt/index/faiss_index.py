from __future__ import annotations

import contextlib
import logging
import os
import threading

import faiss
import numpy as np

logger = logging.getLogger(__name__)


class FaissIndex:
    def __init__(self, dimension: int = 768, *, lock_enabled: bool = True):
        self._dimension = dimension
        self._index = faiss.IndexFlatIP(dimension)
        self._id_map: list[str] = []
        # Guards every method that touches ``self._index`` directly so the
        # background ``to_thread`` save (real worker thread) cannot race a
        # synchronous ``add()`` / ``search()`` on the event loop. faiss may
        # release the GIL inside ``write_index`` / ``search``, so the GIL alone
        # does not serialize them — an explicit lock is required. ``threading``
        # (not ``asyncio``) because the contention is genuinely cross-thread.
        # ``search_by_id`` is intentionally NOT locked: it only composes
        # ``get_vectors`` + ``search`` (each self-locking), and the non-
        # reentrant lock would deadlock if it held it across those calls.
        self._lock: threading.Lock | contextlib.AbstractContextManager = (
            threading.Lock() if lock_enabled else contextlib.nullcontext()
        )

    @property
    def size(self) -> int:
        return self._index.ntotal

    def ids(self) -> set[str]:
        """Return a snapshot of the node IDs represented by this index.

        Startup diagnostics use this to detect a stale-but-well-formed index
        whose vector count happens to look plausible for a different SQLite
        snapshot.  Returning a copy keeps callers from mutating the sidecar
        map and avoids exposing the list while a background save is reading
        it.
        """
        with self._lock:
            return set(self._id_map)

    def add(self, vectors: np.ndarray, ids: list[str]) -> None:
        assert vectors.shape[0] == len(ids)
        assert vectors.shape[1] == self._dimension
        with self._lock:
            self._index.add(vectors.astype(np.float32))
            self._id_map.extend(ids)

    def search(self, query_vector: np.ndarray, top_k: int) -> list[tuple[str, float]]:
        with self._lock:
            if self._index.ntotal == 0:
                return []
            k = min(top_k, self._index.ntotal)
            scores, indices = self._index.search(query_vector.astype(np.float32), k)
            results = []
            id_map_len = len(self._id_map)
            for score, idx in zip(scores[0], indices[0]):
                if idx < 0 or idx >= id_map_len:
                    # Defensive: ntotal can briefly outpace _id_map under
                    # multi-process write contention (corrupted .ids file or
                    # mid-save interruption). Skip rather than IndexError.
                    continue
                results.append((self._id_map[idx], float(score)))
            return results

    def save(self, path: str) -> None:
        """Atomically persist the FAISS index + id map.

        Writes to ``path.tmp`` first, then ``os.replace`` to the final
        location. ``os.replace`` is atomic on POSIX and on Windows
        (Python 3.3+), so a kill / crash mid-write leaves the previous
        valid file at ``path`` intact — the partial write only damages
        the orphan ``.tmp`` file, which the next save overwrites.

        Why: the previous implementation called ``faiss.write_index``
        directly against ``path``, truncating the existing file before
        the new contents were fully written. A SIGTERM/SIGKILL during
        the ~100 MB write of a 30k-vector index left ``gaottt.faiss`` /
        ``gaottt.virtual.faiss`` as 0-byte stubs that the next startup
        could not load (``Error: 'ret == (1)' failed: read error``).
        Observed twice on 2026-05-14 during routine backend restarts;
        the virtual_faiss_save_loop fires every 60 s, so any kill
        roughly during that window risks corruption.
        """
        # H3: scope the scratch file to this pid. The FAISS dir is shared
        # across processes; the Stage 1 startup diagnostic sweeps ``*.tmp``.
        # With a single shared ``<path>.tmp`` name, process A booting while
        # process B is mid-write (a ~100 MB write of a 30k index) would
        # unlink B's scratch file out from under it, turning B's os.replace
        # into FileNotFoundError and losing that snapshot. A pid-scoped
        # name means no cleanup can target a live writer's file, and two
        # processes saving concurrently never collide. ``<pid>`` also lets
        # the cleanup tell a dead-process orphan from a live in-flight one.
        # Hold the lock across the whole write so a concurrent add() on the
        # event loop cannot grow ``self._index`` / ``self._id_map`` while
        # ``write_index`` reads the index in this (``to_thread``) worker
        # thread — the cross-thread race that an asyncio lock cannot cover.
        with self._lock:
            tmp = f"{path}.{os.getpid()}.tmp"
            faiss.write_index(self._index, tmp)
            os.replace(tmp, path)

            id_map_path = path + ".ids"
            id_map_tmp = f"{id_map_path}.{os.getpid()}.tmp"
            with open(id_map_tmp, "w") as f:
                for node_id in self._id_map:
                    f.write(node_id + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(id_map_tmp, id_map_path)

    def load(self, path: str) -> None:
        if not os.path.exists(path):
            return
        # Defensive: a 0-byte file is the signature of an interrupted
        # atomic save. faiss.read_index() raises RuntimeError on it,
        # which would break engine.startup() before diagnostics could
        # intervene. Skip the read and leave the index empty — the
        # Stage 1 startup diagnostic (gaottt/diagnostics/startup.py)
        # detects the empty state + nonzero SQLite and triggers a
        # rebuild from the store.
        if os.path.getsize(path) == 0:
            return
        self._index = faiss.read_index(path)
        id_map_path = path + ".ids"
        if os.path.exists(id_map_path):
            with open(id_map_path) as f:
                self._id_map = [line.strip() for line in f if line.strip()]
        # H4: the index file and the .ids sidecar are persisted by two
        # separate os.replace() calls in save(); a crash between them (or a
        # cross-process .tmp interference) can pair a new index with a
        # stale/short .ids map — or vice versa. A length mismatch means
        # every search result id is suspect: search() would either silently
        # drop the unmapped tail (bounds skip) or map vectors to the wrong
        # ids. Refuse the load and leave the index empty so the Stage 1
        # startup diagnostic detects empty-index + nonzero-SQLite and
        # rebuilds from the store — the same self-healing path the 0-byte
        # guard above defers to. Loud + recoverable beats silent + wrong.
        if len(self._id_map) != self._index.ntotal:
            logger.warning(
                "FAISS id-map/index size mismatch on load (%s): "
                "id_map=%d ntotal=%d — refusing load, deferring to "
                "startup rebuild from store",
                path, len(self._id_map), self._index.ntotal,
            )
            self.reset()
            return

    def search_by_id(self, node_id: str, top_k: int) -> list[tuple[str, float]]:
        """Search nearest neighbors of a specific node's embedding."""
        vecs = self.get_vectors([node_id])
        vec = vecs.get(node_id)
        if vec is None:
            return []
        return self.search(vec.reshape(1, -1), top_k)

    def get_vectors(self, ids: list[str]) -> dict[str, np.ndarray]:
        """Retrieve original embedding vectors by IDs."""
        with self._lock:
            if self._index.ntotal == 0:
                return {}
            id_to_idx = {nid: i for i, nid in enumerate(self._id_map)}
            indices_needed = [(node_id, id_to_idx[node_id]) for node_id in ids if node_id in id_to_idx]
            if not indices_needed:
                return {}
            # Read full matrix once
            all_vecs = faiss.rev_swig_ptr(
                self._index.get_xb(), self._index.ntotal * self._dimension
            )
            all_vecs = np.array(all_vecs).reshape(self._index.ntotal, self._dimension)
            return {node_id: all_vecs[idx].copy().astype(np.float32) for node_id, idx in indices_needed}

    def reset(self) -> None:
        with self._lock:
            self._index = faiss.IndexFlatIP(self._dimension)
            self._id_map = []
