"""MV4 ControlClient — supervisor → control plane pull/push client (WP-3).

Talks to the control plane (``control/`` package, a separate localhost
FastAPI process backed by Postgres) over HTTP. The supervisor constructs one
of these when the 3-point config gate (``control_plane_url`` +
``control_host_id`` + ``control_host_token``) is satisfied; otherwise the
feature is fully inert and the supervisor runs local-only (default 不変).

Two communication directions:

* **pull** — ``reconcile_with_control`` calls ``GET /hosts/{hid}/universes``
  to fetch the control plane's view of this host's universes, compares it to
  the local registry (local is authoritative, J5), and reports the local
  state via ``POST /hosts/{hid}/sync``. Conflicts (control knows of a
  universe the local host does not, or control says deleted but local is
  active) are WARNING-logged and included in the sync payload for the control
  plane to audit.

* **push (usage)** — ``arecord_event`` accumulates activity telemetry
  (``route_resolution`` etc., J1=A — NOT a precise operation count) in an
  in-memory counter. ``flush_usage`` snapshots the counter, writes it to an
  **idempotent local spool** keyed by ``batch_id`` (UUID4, Codex B4), then
  POSTs ``POST /hosts/{hid}/usage``. On success the spool file is deleted; on
  failure it stays for the next replay. The spool is durable: temp file →
  ``flush()`` → ``os.fsync()`` → ``os.replace()``.

Degraded mode vs permanent auth failure (review #2 blocking #4):

  * network error / 5xx → WARNING + spool retained + retry next cycle.
  * **401** → ERROR + ``_auth_failed = True`` + all further POST attempts
    are skipped (stop wasting requests against a revoked token) BUT spool
    writing continues so a credential rotation + restart can drain the
    accumulated batches in one shot. The supervisor's ``/route`` and
    universe lifecycle are unaffected (local authority).

Import constraint (J9): this module imports ONLY from ``gaottt.config``,
``gaottt.multiverse.registry``, stdlib, and ``httpx``. NEVER ``asyncpg`` —
that dependency lives entirely inside the separate ``control/`` package.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from gaottt.config import GaOTTTConfig
from gaottt.multiverse.registry import MultiverseRegistry

logger = logging.getLogger(__name__)

__all__ = [
    "ControlClient",
    # Event-type constants (base form, matching usage_events.event_type).
    # The audit_log.action vocabulary (past tense) lives in control.api;
    # these are the client-side classification names only (J1=A).
    "ROUTE_RESOLUTION",
    "UNIVERSE_CREATE",
    "UNIVERSE_DELETE",
    "UNIVERSE_RESTORE",
]

# --- event type constants (J1=A: route-resolution activity telemetry) --------
# These are the BASE form classifications written into usage_events.event_type.
# They are deliberately NOT operation counts: ``route_resolution`` counts
# supervisor /route resolutions (proxy reconnect can under-count), not
# recall/remember operations.
ROUTE_RESOLUTION = "route_resolution"
UNIVERSE_CREATE = "universe_create"
UNIVERSE_DELETE = "universe_delete"
UNIVERSE_RESTORE = "universe_restore"

# HTTP client timeout for all control-plane requests.
_HTTP_TIMEOUT = 30.0


@dataclass
class _EventAccum:
    """In-memory accumulator for one (universe_id, event_type) pair.

    tenant_id is stored alongside the count so ``flush_usage`` knows which
    tenant to attribute the events to without re-deriving it. In v1 the
    tenant is the single config default; the field is here so future
    multi-tenant support does not need a counter redesign.
    """

    universe_id: str
    event_type: str
    tenant_id: str
    count: int = 0


class ControlClient:
    """Supervisor → control plane pull/push client (MV4 WP-3).

    All network failures degrade gracefully (WARNING + retry/spool). A 401
    (revoked/invalid host token) is a permanent auth failure: ERROR +
    ``_auth_failed`` flag + subsequent POST attempts are skipped, but spool
    writing continues for post-rotation replay.
    """

    def __init__(
        self,
        config: GaOTTTConfig,
        registry: MultiverseRegistry,
        *,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        """Construct the client.

        ``transport`` is a DI seam for unit tests (httpx.MockTransport).
        Production callers omit it and the real httpx transport is used.
        """
        self._config = config
        self._registry = registry
        self._host_id = config.control_host_id
        self._token = config.control_host_token
        self._base_url = config.control_plane_url.rstrip("/")
        # J11: empty config default → implicit single tenant "default".
        self._default_tenant_id = config.control_default_tenant_id or "default"
        self._sync_interval = config.control_sync_interval_seconds
        self._push_interval = config.usage_push_interval_seconds
        self._transport = transport

        # Resolve the effective spool directory lazily here (not in config)
        # so the config knob stays a plain str. Empty usage_spool_dir falls
        # back to <multiverse_root>/logs/usage-spool/; if multiverse_root is
        # also empty, usage push is disabled (no durability target).
        if config.usage_spool_dir:
            self._spool_dir: Path | None = Path(config.usage_spool_dir)
        elif config.multiverse_root:
            self._spool_dir = (
                Path(config.multiverse_root) / "logs" / "usage-spool"
            )
        else:
            self._spool_dir = None

        # 3-point gate: all three auth fields must be set.
        self._enabled = bool(
            config.control_plane_url
            and config.control_host_id
            and config.control_host_token
        )

        # Permanent auth failure (401) state. Once True, all POST attempts
        # are skipped until the supervisor is restarted with a fresh token.
        self._auth_failed: bool = False
        self._auth_failed_since: float | None = None

        # Usage event counter. Keyed by (universe_id, event_type) so multiple
        # events for the same universe coalesce into one accumulator.
        self._counter: dict[tuple[str, str], _EventAccum] = {}
        self._lock = asyncio.Lock()

        # The httpx client is created lazily on first use so construction
        # (which may happen outside an event loop) never opens connections.
        self._client: httpx.AsyncClient | None = None
        self._tasks: list[asyncio.Task] = []

        if not self._enabled:
            logger.debug(
                "control_client: disabled "
                "(control_plane_url / control_host_id / control_host_token "
                "not all set)"
            )

    # ------------------------------------------------------------------ #
    # usage accumulation                                                  #
    # ------------------------------------------------------------------ #

    @property
    def _usage_enabled(self) -> bool:
        """Usage push needs both the client gate AND a spool directory."""
        return self._enabled and self._spool_dir is not None

    async def arecord_event(
        self,
        universe_id: str,
        event_type: str,
        tenant_id: str | None = None,
        count: int = 1,
    ) -> None:
        """Accumulate one usage event into the in-memory counter.

        Async because the counter is guarded by an asyncio.Lock — a sync
        method cannot acquire it (review #2 blocking #3). The lock is held
        for an O(1) dict update only, so the /route latency impact is
        negligible. The supervisor calls this AFTER the route response is
        prepared so telemetry never blocks the caller-facing path.
        """
        if not self._usage_enabled:
            return
        tid = tenant_id or self._default_tenant_id
        key = (universe_id, event_type)
        async with self._lock:
            acc = self._counter.get(key)
            if acc is None:
                acc = _EventAccum(universe_id, event_type, tid, 0)
                self._counter[key] = acc
            acc.count += count
            # Last-write-wins for tenant_id. In v1 (single default tenant)
            # this is always the same value; the overwrite is a no-op.
            acc.tenant_id = tid

    # ------------------------------------------------------------------ #
    # usage flush                                                         #
    # ------------------------------------------------------------------ #

    async def flush_usage(self) -> None:
        """Snapshot the counter, write to spool, then drain all spool files.

        Sequence:
          1. Snapshot + clear the counter under the lock (O(1) hold).
          2. If non-empty: build a batch_id-keyed payload and write it to
             the spool atomically. On spool-write OSError (disk full etc.)
             the snapshot is RESTORED to the counter so the next flush
             retries — no silent data loss.
          3. Replay every spool file (the just-written current batch plus
             any stale crashed batches) in window_start ascending order,
             POSTing each. On 200 the spool file is deleted; on failure it
             is retained.
        """
        if not self._usage_enabled:
            return

        async with self._lock:
            snapshot = dict(self._counter)
            self._counter.clear()

        if snapshot:
            batch_id = str(uuid4())
            now = datetime.now(timezone.utc)
            window_start_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            events = [
                {
                    "universe_id": acc.universe_id,
                    "tenant_id": acc.tenant_id,
                    "event_type": acc.event_type,
                    "count": acc.count,
                }
                for acc in snapshot.values()
            ]
            payload = {
                "batch_id": batch_id,
                "window_start": window_start_iso,
                "window_end": window_start_iso,
                "host_id": self._host_id,
                "events": events,
            }
            try:
                self._write_spool_atomically(batch_id, now, payload)
            except OSError as exc:
                total = sum(a.count for a in snapshot.values())
                logger.error(
                    "control_client: spool write failed (%s); "
                    "restoring %d events (%d buckets) to in-memory counter",
                    exc,
                    total,
                    len(snapshot),
                )
                # Restore the snapshot back into the counter so the data is
                # not lost — the next flush will retry the spool write.
                async with self._lock:
                    for key, acc in snapshot.items():
                        existing = self._counter.get(key)
                        if existing is None:
                            self._counter[key] = acc
                        else:
                            existing.count += acc.count
                return

        # Drain the spool (current batch + any stale batches), oldest first.
        await self.replay_stale_spool()

    def _write_spool_atomically(
        self, batch_id: str, window_start: datetime, payload: dict
    ) -> Path:
        """Write ``payload`` to the spool dir as a durable JSON Lines file.

        Filename = ``{window_start_basic_iso}_{batch_id}.jsonl`` where the
        timestamp prefix is UTC basic ISO8601 (e.g. ``20260703T100000Z``) so
        a plain lexical sort of filenames yields window_start ascending
        order (A7 — FIFO replay). The write is atomic: temp file in the same
        directory → write → flush → fsync → os.replace.

        Raises OSError on disk full / permission denied. The caller
        (flush_usage) restores the in-memory counter on failure.

        Returns the final (post-rename) Path.
        """
        assert self._spool_dir is not None  # ensured by _usage_enabled gate
        self._spool_dir.mkdir(parents=True, exist_ok=True)
        ts = window_start.strftime("%Y%m%dT%H%M%SZ")
        final = self._spool_dir / f"{ts}_{batch_id}.jsonl"
        tmp = tempfile.NamedTemporaryFile(
            dir=str(self._spool_dir), delete=False, suffix=".tmp", mode="w"
        )
        try:
            tmp.write(json.dumps(payload) + "\n")
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
        except OSError:
            tmp.close()
            # Best-effort cleanup of the temp file so it doesn't accumulate.
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise
        os.replace(tmp.name, final)
        return final

    def _quarantine_corrupt_spool(self, path: Path) -> None:
        """Move a corrupt spool file (JSON parse failure) to ``quarantine/``.

        Does NOT raise — a corrupt file must not block subsequent flushes
        (Codex missing #6). Permanent-auth-failure spool files are NOT
        corrupt: they are valid JSON accumulating for post-rotation replay,
        and stay in the main spool dir.
        """
        if self._spool_dir is None:
            return
        quarantine = self._spool_dir / "quarantine"
        try:
            quarantine.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(quarantine / path.name))
            logger.warning(
                "control_client: quarantined corrupt spool file %s",
                path.name,
            )
        except OSError as exc:
            logger.error(
                "control_client: failed to quarantine corrupt spool %s: %s",
                path,
                exc,
            )

    async def replay_stale_spool(self) -> None:
        """Replay every ``*.jsonl`` file in the spool dir, oldest first.

        Files are sorted by filename (= window_start ascending, A7). Each
        is read, JSON-parsed (corrupt → quarantine), and POSTed via
        ``_post_usage_batch``. On 200 the file is deleted; on failure it is
        retained for the next replay.
        """
        if not self._usage_enabled:
            return
        if self._spool_dir is None or not self._spool_dir.is_dir():
            return
        files = sorted(self._spool_dir.glob("*.jsonl"))
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
                payload = json.loads(text)
            except json.JSONDecodeError:
                self._quarantine_corrupt_spool(path)
                continue
            except FileNotFoundError:
                # A concurrent replay already processed this file.
                continue
            except OSError:
                self._quarantine_corrupt_spool(path)
                continue
            await self._post_usage_batch(payload, path)

    async def _post_usage_batch(
        self, payload: dict, spool_path: Path | None
    ) -> None:
        """POST one usage batch to ``/hosts/{hid}/usage``.

        On HTTP 200 the ``spool_path`` file is deleted. On 401 the permanent
        ``_auth_failed`` flag is set. On network error / 5xx / other 4xx the
        spool file is retained for retry. The method never raises — all
        failures are logged and the caller continues with the next file.
        """
        if self._auth_failed:
            logger.debug("control_client: skipping usage POST (auth failed)")
            return

        client = self._get_client()
        url = f"{self._base_url}/hosts/{self._host_id}/usage"
        # The wire body matches UsageBatchBody (no top-level host_id — that
        # comes from the URL path). host_id in the spool payload is debug
        # metadata only.
        body = {
            "batch_id": payload["batch_id"],
            "window_start": payload["window_start"],
            "window_end": payload["window_end"],
            "events": payload["events"],
        }
        try:
            response = await client.post(url, json=body)
        except (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.NetworkError,
        ) as exc:
            logger.warning(
                "control_client: usage POST network error: %s", exc
            )
            return  # leave spool for next replay

        if response.status_code == 200:
            if spool_path is not None:
                try:
                    spool_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    logger.warning(
                        "control_client: failed to delete spool %s: %s",
                        spool_path,
                        exc,
                    )
            return

        if response.status_code == 401:
            self._auth_failed = True
            self._auth_failed_since = time.time()
            logger.error(
                "control_client: 401 on usage POST — permanent auth failure; "
                "spool accumulates for post-rotation replay"
            )
            return

        if 500 <= response.status_code < 600:
            logger.warning(
                "control_client: usage POST got %d; spool retained for retry",
                response.status_code,
            )
            return

        # Other 4xx (not 401) — likely a payload bug; operator must investigate.
        logger.error(
            "control_client: usage POST got %d (%s); spool retained — "
            "likely payload bug, investigate",
            response.status_code,
            response.text[:200],
        )

    # ------------------------------------------------------------------ #
    # pull (sync)                                                        #
    # ------------------------------------------------------------------ #

    async def pull_host_universes(self) -> list[dict] | None:
        """``GET /hosts/{hid}/universes`` — fetch control's view of this host.

        Returns the JSON list on 200, or None on any failure (the caller
        continues in degraded mode with the local registry). A 401 sets the
        permanent ``_auth_failed`` flag; subsequent calls return None
        immediately without touching the network.
        """
        if not self._enabled:
            return None
        if self._auth_failed:
            return None

        client = self._get_client()
        url = f"{self._base_url}/hosts/{self._host_id}/universes"
        try:
            response = await client.get(url)
        except (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.NetworkError,
        ) as exc:
            logger.warning(
                "control_client: pull_host_universes network error: %s", exc
            )
            return None

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                logger.warning(
                    "control_client: pull_host_universes returned non-JSON"
                )
                return None

        if response.status_code == 401:
            self._auth_failed = True
            self._auth_failed_since = time.time()
            logger.error(
                "control_client: 401 on pull_host_universes — "
                "permanent auth failure"
            )
            return None

        logger.warning(
            "control_client: pull_host_universes got %d",
            response.status_code,
        )
        return None

    async def reconcile_with_control(self) -> None:
        """Pull control's view, detect conflicts, POST local state.

        Local is authoritative (J5): the sync payload reflects the local
        registry, and the control plane records any mismatch in its audit
        log. Two conflict classes are WARNING-logged client-side:

          * control knows of a universe the local host does not have, and
          * control says a universe is ``deleted`` but local has it ``active``.

        Neither triggers a local mutation — the operator resolves conflicts
        manually (J5).
        """
        if not self._enabled:
            return

        remote = await self.pull_host_universes()
        if remote is None:
            return  # degraded — control unreachable or auth failed

        local = await self._registry.list_universes()
        local_by_id = {row["universe_id"]: row for row in local}
        remote_by_id = {
            r["universe_id"]: r
            for r in remote
            if r.get("universe_id")
        }

        # Conflict detection (WARNING only — local is never mutated).
        for uid, rem in remote_by_id.items():
            if uid not in local_by_id:
                logger.warning(
                    "control_client: control knows of universe %s "
                    "not in local registry (possible stale control state)",
                    uid,
                )
            elif (
                rem.get("status") == "deleted"
                and local_by_id[uid].get("status") == "active"
            ):
                logger.warning(
                    "control_client: universe %s marked deleted by control "
                    "but active locally (conflict — not auto-deleting, J5)",
                    uid,
                )

        # Build the sync payload from local state.
        local_universes = []
        deleted_universes = []
        for row in local:
            entry = {
                "universe_id": row["universe_id"],
                "tenant_id": self._default_tenant_id,
                "host_id": self._host_id,
                "embedder_id": row["embedder_id"],
                "embedder_version": row["embedder_version"],
                "status": row["status"],
            }
            local_universes.append(entry)
            if row["status"] == "deleted":
                deleted_universes.append(row["universe_id"])

        sync_body = {
            "local_universes": local_universes,
            "deleted_universes": deleted_universes,
        }

        if self._auth_failed:
            logger.debug(
                "control_client: skipping sync POST (auth failed)"
            )
            return

        client = self._get_client()
        url = f"{self._base_url}/hosts/{self._host_id}/sync"
        try:
            response = await client.post(url, json=sync_body)
        except (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.NetworkError,
        ) as exc:
            logger.warning(
                "control_client: sync POST network error: %s", exc
            )
            return

        if response.status_code == 200:
            try:
                data = response.json()
                logger.info(
                    "control_client: sync result: %s", data
                )
            except ValueError:
                logger.info("control_client: sync POST succeeded (non-JSON)")
            return

        if response.status_code == 401:
            self._auth_failed = True
            self._auth_failed_since = time.time()
            logger.error(
                "control_client: 401 on sync POST — permanent auth failure"
            )
            return

        logger.warning(
            "control_client: sync POST got %d", response.status_code
        )

    # ------------------------------------------------------------------ #
    # auth failure state accessor                                        #
    # ------------------------------------------------------------------ #

    def auth_failure_state(self) -> dict:
        """Non-async accessor for the supervisor status endpoint.

        Returns ``{"auth_failed": bool, "since": float | None,
        "spool_pending": int}``. Operators poll this for early detection of
        a revoked token so they can rotate and restart before the spool
        grows unbounded.
        """
        pending = 0
        if self._spool_dir is not None and self._spool_dir.is_dir():
            pending = sum(1 for _ in self._spool_dir.glob("*.jsonl"))
        return {
            "auth_failed": self._auth_failed,
            "since": self._auth_failed_since,
            "spool_pending": pending,
        }

    # ------------------------------------------------------------------ #
    # lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def _get_client(self) -> httpx.AsyncClient:
        """Lazily create the AsyncClient (preserves the injected transport)."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT,
                headers={"Authorization": f"Bearer {self._token}"},
                transport=self._transport,
            )
        return self._client

    async def start(self) -> None:
        """Start the pull/push loops.

        Spawns the periodic ``_pull_loop`` (reconcile), ``_push_loop``
        (flush), and a one-shot ``_replay_loop`` that drains crashed spool
        files. The replay runs in the BACKGROUND so ``start()`` returns
        immediately even when the control plane is unreachable — a slow or
        unreachable control plane must never block supervisor startup
        (Codex B2: previously the synchronous ``replay_stale_spool`` could
        delay startup by up to ``_HTTP_TIMEOUT`` per spool file). Each
        background POST is still subject to the normal timeout/retry/spool-
        retain logic; if control is down the POST fails fast (WARNING + spool
        retained) and the push loop retries on its next tick.
        """
        if not self._enabled:
            return
        # Ensure the client exists (also done lazily in _get_client, but
        # creating it here makes start() the explicit lifecycle boundary).
        self._get_client()
        self._tasks = [
            asyncio.create_task(self._pull_loop()),
            asyncio.create_task(self._push_loop()),
            asyncio.create_task(self._replay_loop()),
        ]

    async def _replay_loop(self) -> None:
        """One-shot background replay of crashed spool files at startup.

        Runs ``replay_stale_spool()`` once so a restart after a crash
        attempts to drain the spool without blocking ``start()``.
        Exceptions are logged so the task never silently dies; a failure
        leaves the spool intact for the push loop to retry on its next
        tick.
        """
        try:
            await self.replay_stale_spool()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — startup replay must not kill client
            logger.warning(
                "control_client: startup spool replay failed",
                exc_info=True,
            )

    async def _pull_loop(self) -> None:
        """Periodically reconcile with control. Exceptions are logged."""
        while True:
            try:
                await asyncio.sleep(self._sync_interval)
                await self.reconcile_with_control()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — loops must not die
                logger.warning(
                    "control_client: pull loop iteration failed",
                    exc_info=True,
                )

    async def _push_loop(self) -> None:
        """Periodically flush usage. Exceptions are logged."""
        while True:
            try:
                await asyncio.sleep(self._push_interval)
                await self.flush_usage()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — loops must not die
                logger.warning(
                    "control_client: push loop iteration failed",
                    exc_info=True,
                )

    async def stop(self) -> None:
        """Cancel loops, do a final flush, close the HTTP client."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
        # Final flush to drain any counter accumulated since the last tick.
        await self.flush_usage()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
