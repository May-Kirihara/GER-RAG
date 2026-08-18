"""Owner lease — single-owner coordination via a lock file + guard flock (WP-2).

Coordinates exclusive ownership of a data directory across processes using two
files:

* ``owner.lock``      — JSON bookkeeping (``owner_id``, ``pid``, ``hostname``,
                        ``started_at``, ``heartbeat_at``, ``takeover_count``).
                        Its existence and content encode "who owns the
                        directory right now".
* ``owner.lock.guard``— an empty file whose ``fcntl.flock(LOCK_EX)`` serializes
                        every read-modify-write operation on ``owner.lock``.

Acquisition atomicity rests on two independent mechanisms:

1. **Fresh create** uses ``os.open(O_CREAT | O_WRONLY | O_EXCL)`` — the kernel
   guarantees exactly one process wins; every other contender gets
   ``FileExistsError`` and falls through to the guard path. No guard is held
   for create because the kernel already made it atomic.
2. **Every other RMW** (stale takeover, force takeover, heartbeat refresh,
   release) holds ``flock(LOCK_EX)`` on the guard across the whole
   read→judge→write section. This closes the TOCTOU window where a stale read
   could commit to a takeover and clobber a fresh owner that landed in the gap.

The guard file is persistent (never deleted); only the flock is acquired and
released per critical section. Stale judgement is strict: a lock is stale iff
``now - heartbeat_at > config.lease_stale_seconds`` (``==`` is NOT stale).
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import socket
import time
import uuid
from pathlib import Path

from gaottt.config import GaOTTTConfig

logger = logging.getLogger(__name__)


class LeaseHeldError(Exception):
    """Raised when acquire() finds an active owner."""


class LeaseLostError(Exception):
    """Raised by mutating engine operations after the lease was lost.

    The heartbeat loop detected an owner_id mismatch (another process took
    over via stale/force). The engine has transitioned to read-only: reads
    still work, but writes are rejected to prevent reverse-overwriting the
    new owner's state.
    """


class OwnerLease:
    LOCK_FILENAME = "owner.lock"
    GUARD_FILENAME = "owner.lock.guard"

    def __init__(self, data_dir: Path, config: GaOTTTConfig):
        self._data_dir = Path(data_dir)
        self._lock_path = self._data_dir / self.LOCK_FILENAME
        self._guard_path = self._data_dir / self.GUARD_FILENAME
        self._config = config
        self.owner_id = uuid.uuid4().hex
        self._is_active = False

    # -- public API ---------------------------------------------------------

    def acquire(self, force: bool = False) -> None:
        """Become the owner, creating a fresh lock or taking over an existing one.

        Fresh path: ``O_CREAT | O_EXCL`` — kernel-atomic, exactly one winner.
        Existing path: guard flock → re-read → judge stale/force → atomic
        replace or raise ``LeaseHeldError`` (guard released in ``finally``).
        """
        now = time.time()
        payload = self._new_payload(now=now, takeover_count=0)
        try:
            fd = os.open(
                str(self._lock_path),
                os.O_CREAT | os.O_WRONLY | os.O_EXCL,
                0o644,
            )
        except FileExistsError:
            self._acquire_existing(force=force)
            return
        # We won the kernel race; publish the JSON. A contender that lost
        # O_EXCL and reads during this write sees an empty/corrupt lock and
        # (under the guard) treats it as held — the correct conservative
        # outcome, never a double-create.
        try:
            os.write(fd, json.dumps(payload).encode("utf-8"))
            os.close(fd)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                self._lock_path.unlink()
            except FileNotFoundError:
                pass
            raise
        self._is_active = True

    async def heartbeat_loop(self, stop_event: asyncio.Event) -> None:
        """Refresh ``heartbeat_at`` on a fixed cadence until ``stop_event`` is set.

        Returns early when the stop event fires or when ``_refresh_heartbeat``
        detects that ownership was lost out from under us (foreign owner read
        back under the guard). Never overwrites another owner's lock.
        """
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._config.lease_heartbeat_seconds,
                )
                # stop_event was set before the heartbeat interval elapsed.
                return
            except asyncio.TimeoutError:
                pass
            if not self._refresh_heartbeat():
                return  # owner loss detected

    def release(self) -> None:
        """Release the lease, deleting ``owner.lock`` only if we still own it.

        Guard-protected so a concurrent takeover cannot wedge the unlink into
        deleting another owner's lock. Missing lock is a safe no-op.
        """
        guard_fd = self._open_guard()
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_EX)
            data = self._read_lock_json()
            if data is None:
                return
            if data.get("owner_id") == self.owner_id:
                try:
                    self._lock_path.unlink()
                except FileNotFoundError:
                    pass
            # Not our lock (taken over) — leave it intact.
            self._is_active = False
        finally:
            self._release_guard(guard_fd)

    @property
    def is_active(self) -> bool:
        """Read-back ownership check: True iff the persisted owner is us.

        Re-reads ``owner.lock`` every call so an external takeover is observed
        without any heartbeat tick. Missing or corrupt lock → False.
        """
        data = self._read_lock_json()
        if data is None:
            return False
        return data.get("owner_id") == self.owner_id

    # -- guard-protected RMW ------------------------------------------------

    def _acquire_existing(self, *, force: bool) -> None:
        guard_fd = self._open_guard()
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_EX)
            now = time.time()
            data = self._read_lock_json()
            if data is None:
                # The file existed at O_EXCL time but is now missing/unreadable.
                # Treat as held — we cannot prove it is safely takeable, and a
                # concurrent creator may be mid-publish.
                raise LeaseHeldError(
                    f"owner.lock at {self._lock_path} exists but is unreadable; "
                    f"cannot determine ownership"
                )
            heartbeat_at = float(data.get("heartbeat_at", 0.0))
            is_stale = (now - heartbeat_at) > self._config.lease_stale_seconds
            if force or self._config.lease_force_takeover or is_stale:
                takeover_count = int(data.get("takeover_count", 0)) + 1
                payload = self._new_payload(now=now, takeover_count=takeover_count)
                self._write_lock_atomic(payload)
                if is_stale:
                    logger.warning(
                        "owner lease stale takeover: data_dir=%s old_owner=%s "
                        "stale_age=%.1fs threshold=%.1fs takeover_count=%d",
                        self._data_dir, data.get("owner_id"),
                        now - heartbeat_at,
                        self._config.lease_stale_seconds,
                        takeover_count,
                    )
                else:
                    logger.warning(
                        "owner lease force takeover: data_dir=%s "
                        "displaced_owner=%s takeover_count=%d",
                        self._data_dir, data.get("owner_id"),
                        takeover_count,
                    )
                self._is_active = True
                return
            raise LeaseHeldError(
                f"owner.lock at {self._lock_path} is held by active owner "
                f"{data.get('owner_id')!r} "
                f"(heartbeat_age={now - heartbeat_at:.1f}s)"
            )
        finally:
            self._release_guard(guard_fd)

    def _refresh_heartbeat(self) -> bool:
        """Advance ``heartbeat_at`` if we still own the lock.

        Returns False (and logs ERROR, leaving the lock untouched) when a
        foreign owner is read back — the heartbeat loop stops on this signal.
        """
        guard_fd = self._open_guard()
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_EX)
            data = self._read_lock_json()
            if data is None or data.get("owner_id") != self.owner_id:
                self._is_active = False
                logger.error(
                    "owner lease heartbeat detected owner loss: data_dir=%s "
                    "expected_owner=%s found_owner=%r — stopping heartbeat; "
                    "foreign lock left untouched",
                    self._data_dir, self.owner_id,
                    None if data is None else data.get("owner_id"),
                )
                return False
            data["heartbeat_at"] = time.time()
            self._write_lock_atomic(data)
            return True
        finally:
            self._release_guard(guard_fd)

    # -- helpers ------------------------------------------------------------

    def _open_guard(self) -> int:
        # The data_dir must exist for guard creation; acquire() may be the very
        # first touch of a fresh directory.
        self._data_dir.mkdir(parents=True, exist_ok=True)
        return os.open(str(self._guard_path), os.O_CREAT | os.O_RDWR, 0o644)

    def _release_guard(self, guard_fd: int) -> None:
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(guard_fd)
        except OSError:
            pass

    def _read_lock_json(self) -> dict | None:
        """Return the lock dict, or None if missing / corrupt / non-dict."""
        try:
            text = self._lock_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _write_lock_atomic(self, payload: dict) -> None:
        """Atomically replace ``owner.lock`` via tmp + ``os.replace``.

        Mirrors ``store.manifest.write_manifest``: a unique tmp name (pid +
        uuid) so concurrent writers cannot clobber each other's scratch, and
        cleanup of the tmp on replace failure so no ``*.tmp`` survives.
        """
        text = json.dumps(payload)
        tmp_name = (
            f"{self.LOCK_FILENAME}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        )
        tmp_path = self._data_dir / tmp_name
        tmp_path.write_text(text, encoding="utf-8")
        try:
            os.replace(tmp_path, self._lock_path)
        except Exception:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            raise

    def _new_payload(self, *, now: float, takeover_count: int) -> dict:
        # ``started_at`` is the NEW owner's start time; a takeover does not
        # inherit the displaced owner's start time.
        return {
            "owner_id": self.owner_id,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "started_at": now,
            "heartbeat_at": now,
            "takeover_count": takeover_count,
        }
