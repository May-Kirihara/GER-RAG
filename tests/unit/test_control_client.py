"""MV4 ControlClient unit tests (WP-3, test-first / RED→GREEN).

All tests are docker-free: the control plane is simulated with
``httpx.MockTransport`` (the same DI-seam pattern MV1 RemoteEmbedder uses),
and the ``MultiverseRegistry`` runs against a tmp-path aiosqlite database.

These tests pin the WP-3 contract documented in
``docs/maintainers/multiverse-mv4-execution-plan.md``:

  * 3-point config gate (control_plane_url + control_host_id +
    control_host_token) — all methods no-op when any is missing.
  * arecord_event is async + asyncio.Lock-guarded — concurrent calls +
    flush lose zero increments.
  * flush_usage writes a ``batch_id``-keyed spool atomically (temp + fsync +
    rename), POSTs ``/hosts/{hid}/usage``, deletes the spool on 200.
  * Network error / 5xx → spool retained. 401 → permanent auth failure
    (subsequent POST attempts skipped, spool keeps accumulating).
  * Crash-after-POST-before-delete is recovered via ``batch_id`` idempotent
    replay (the control plane deduplicates on batch_id — WP-2).
  * Spool replay is FIFO by ``window_start`` filename prefix (A7).
  * Corrupt spool (JSON parse failure) → quarantine, does not block
    subsequent flushes.
  * Disk full on spool write → ERROR log + counter restored (no silent
    data loss).
  * reconcile_with_control: local is authoritative (J5); conflicts are
    WARNING-logged and reported via sync, never auto-resolved.
"""
from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import socket
import time
from pathlib import Path

import httpx

from gaottt.config import GaOTTTConfig
from gaottt.multiverse.control_client import ControlClient
from gaottt.multiverse.registry import MultiverseRegistry

HOST_ID = "h1"
HOST_TOKEN = "secret-host-token"
BASE_URL = "http://stub.control.local"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _grab_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


async def _make_registry(root: Path) -> MultiverseRegistry:
    """Construct + initialize a registry rooted at ``root``."""
    reg = MultiverseRegistry(root)
    await reg.initialize()
    return reg


def _enabled_config(root: Path, **overrides) -> GaOTTTConfig:
    """A config with the 3-point gate satisfied and a tmp spool dir."""
    kwargs: dict = {
        "multiverse_root": str(root),
        "control_plane_url": BASE_URL,
        "control_host_id": HOST_ID,
        "control_host_token": HOST_TOKEN,
        "control_default_tenant_id": "default",
        "usage_spool_dir": str(root / "spool"),
        "control_sync_interval_seconds": 9999.0,  # loops won't fire in tests
        "usage_push_interval_seconds": 9999.0,
    }
    kwargs.update(overrides)
    return GaOTTTConfig(**kwargs)


def _make_client(
    config: GaOTTTConfig,
    registry: MultiverseRegistry,
    handler,
) -> ControlClient:
    transport = httpx.MockTransport(handler)
    return ControlClient(config, registry, transport=transport)


def _usage_ok_handler(captured: list | None = None) -> callable:
    """A handler that 200-OKs every usage POST, optionally capturing bodies."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/hosts/{HOST_ID}/usage":
            body = json.loads(request.content)
            if captured is not None:
                captured.append(body)
            return httpx.Response(
                200,
                json={
                    "batch_id": body["batch_id"],
                    "events_ingested": len(body["events"]),
                },
            )
        if path == f"/hosts/{HOST_ID}/universes":
            return httpx.Response(200, json=[])
        if path == f"/hosts/{HOST_ID}/sync":
            return httpx.Response(200, json={"inserted": 0, "conflicts": 0})
        return httpx.Response(404)

    return handler


# ===========================================================================
# 1. disabled when config incomplete
# ===========================================================================


async def test_disabled_when_config_incomplete(tmp_path: Path):
    """Missing any of the 3 auth fields → every method is a no-op."""
    root = tmp_path / "mv"
    root.mkdir()
    reg = await _make_registry(root)
    try:
        # No control_* fields set → all empty.
        config = GaOTTTConfig(multiverse_root=str(root))
        # A handler that FAILS if ever hit — proves no HTTP was attempted.
        def fail_handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(
                f"disabled client must not make HTTP requests (hit {request.url})"
            )

        cc = _make_client(config, reg, fail_handler)
        assert not cc._enabled

        # Every public method returns without error and without HTTP.
        await cc.start()
        await cc.arecord_event("u1", "route_resolution")
        await cc.flush_usage()
        assert await cc.pull_host_universes() is None
        await cc.reconcile_with_control()
        await cc.replay_stale_spool()
        await cc.stop()
        # auth_failure_state still works on a disabled client.
        state = cc.auth_failure_state()
        assert state["auth_failed"] is False
        assert state["spool_pending"] == 0
    finally:
        await reg.close()


# ===========================================================================
# 2. arecord_event accumulation
# ===========================================================================


async def test_arecord_event_accumulates(tmp_path: Path):
    root = tmp_path / "mv"
    reg = await _make_registry(root)
    try:
        cc = _make_client(_enabled_config(root), reg, _usage_ok_handler())
        for _ in range(3):
            await cc.arecord_event("u1", "route_resolution")
        assert len(cc._counter) == 1
        key = ("u1", "route_resolution")
        assert cc._counter[key].count == 3
        await cc.stop()
    finally:
        await reg.close()


async def test_arecord_event_concurrent_safe(tmp_path: Path):
    """100 concurrent arecord_event + 1 flush → zero lost increments."""
    root = tmp_path / "mv"
    reg = await _make_registry(root)
    try:
        posted_counts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == f"/hosts/{HOST_ID}/usage":
                body = json.loads(request.content)
                for ev in body["events"]:
                    posted_counts.append(ev["count"])
                return httpx.Response(
                    200,
                    json={
                        "batch_id": body["batch_id"],
                        "events_ingested": len(body["events"]),
                    },
                )
            return httpx.Response(404)

        cc = _make_client(_enabled_config(root), reg, handler)

        # 100 concurrent increments racing with one flush.
        tasks = [
            cc.arecord_event("u1", "route_resolution") for _ in range(100)
        ]
        tasks.append(cc.flush_usage())
        await asyncio.gather(*tasks)
        # Final flush drains whatever the racing flush missed.
        await cc.flush_usage()
        await cc.stop()

        # No lost increments: every one of the 100 events made it to a POST.
        assert sum(posted_counts) == 100, (
            f"expected 100 total, got {sum(posted_counts)} "
            f"(posts={posted_counts})"
        )
    finally:
        await reg.close()


# ===========================================================================
# 3. flush_usage success / failure / spool
# ===========================================================================


async def test_flush_usage_success_deletes_spool(tmp_path: Path):
    root = tmp_path / "mv"
    reg = await _make_registry(root)
    try:
        cc = _make_client(_enabled_config(root), reg, _usage_ok_handler())
        await cc.arecord_event("u1", "route_resolution", count=5)
        await cc.flush_usage()
        spool_dir = Path(_enabled_config(root).usage_spool_dir)
        # Successful POST (200) deletes the spool file.
        assert list(spool_dir.glob("*.jsonl")) == []
        await cc.stop()
    finally:
        await reg.close()


async def test_flush_usage_failure_retains_spool(tmp_path: Path):
    root = tmp_path / "mv"
    reg = await _make_registry(root)
    try:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == f"/hosts/{HOST_ID}/usage":
                return httpx.Response(503)
            return httpx.Response(404)

        cc = _make_client(_enabled_config(root), reg, handler)
        await cc.arecord_event("u1", "route_resolution", count=5)
        await cc.flush_usage()
        spool_dir = Path(_enabled_config(root).usage_spool_dir)
        files = list(spool_dir.glob("*.jsonl"))
        assert len(files) == 1, "spool file must survive a 5xx"
        await cc.stop()
    finally:
        await reg.close()


async def test_flush_usage_idempotent_replay(tmp_path: Path):
    """Spool that survives a successful POST is replayed; same batch_id POSTed twice."""
    root = tmp_path / "mv"
    reg = await _make_registry(root)
    try:
        posted_batch_ids: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == f"/hosts/{HOST_ID}/usage":
                body = json.loads(request.content)
                posted_batch_ids.append(body["batch_id"])
                return httpx.Response(
                    200,
                    json={
                        "batch_id": body["batch_id"],
                        "events_ingested": len(body["events"]),
                    },
                )
            return httpx.Response(404)

        cc = _make_client(_enabled_config(root), reg, handler)

        # Monkeypatch Path.unlink to a no-op so the spool file survives the
        # first successful POST (simulates "crash after POST before delete"
        # at the spool level).
        original_unlink = Path.unlink

        def noop_unlink(self, *args, **kwargs):
            pass

        Path.unlink = noop_unlink  # type: ignore[method-assign]
        try:
            await cc.arecord_event("u1", "route_resolution", count=7)
            await cc.flush_usage()
        finally:
            Path.unlink = original_unlink  # type: ignore[method-assign]

        assert len(posted_batch_ids) == 1
        spool_dir = Path(_enabled_config(root).usage_spool_dir)
        files = list(spool_dir.glob("*.jsonl"))
        assert len(files) == 1, "spool survived the noop unlink"

        # Now replay with real unlink — same batch_id POSTed again, then deleted.
        await cc.replay_stale_spool()
        assert len(posted_batch_ids) == 2
        assert posted_batch_ids[0] == posted_batch_ids[1], (
            "replay must POST the same batch_id (idempotent dedup is control-side)"
        )
        assert list(spool_dir.glob("*.jsonl")) == [], "spool cleared after replay"
        await cc.stop()
    finally:
        await reg.close()


async def test_flush_usage_crash_after_post_before_delete(tmp_path: Path):
    """POST 200 then unlink raises → spool stays → replay POSTs again (idempotent)."""
    root = tmp_path / "mv"
    reg = await _make_registry(root)
    try:
        post_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == f"/hosts/{HOST_ID}/usage":
                post_count["n"] += 1
                body = json.loads(request.content)
                return httpx.Response(
                    200,
                    json={
                        "batch_id": body["batch_id"],
                        "events_ingested": len(body["events"]),
                    },
                )
            return httpx.Response(404)

        cc = _make_client(_enabled_config(root), reg, handler)

        # First unlink raises (simulated crash); the OSError is caught inside
        # _post_usage_batch and logged as WARNING — flush_usage does not raise.
        original_unlink = Path.unlink
        call_count = {"n": 0}

        def crashing_unlink(self, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError("simulated crash before delete")
            return original_unlink(self, *args, **kwargs)

        Path.unlink = crashing_unlink  # type: ignore[method-assign]
        try:
            await cc.arecord_event("u1", "route_resolution", count=3)
            await cc.flush_usage()  # POST 200, unlink raises (caught) → spool stays
        finally:
            Path.unlink = original_unlink  # type: ignore[method-assign]

        assert post_count["n"] == 1
        spool_dir = Path(_enabled_config(root).usage_spool_dir)
        assert len(list(spool_dir.glob("*.jsonl"))) == 1

        # Replay POSTs the same batch_id again (control-side idempotent) and
        # this time the delete succeeds.
        await cc.replay_stale_spool()
        assert post_count["n"] == 2
        assert list(spool_dir.glob("*.jsonl")) == []
        await cc.stop()
    finally:
        await reg.close()


async def test_flush_usage_stale_spool_window_start_order(tmp_path: Path):
    """Replay POSTs spool files in window_start ascending (filename sort) order."""
    root = tmp_path / "mv"
    reg = await _make_registry(root)
    try:
        posted_order: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == f"/hosts/{HOST_ID}/usage":
                body = json.loads(request.content)
                posted_order.append(body["batch_id"])
                return httpx.Response(
                    200,
                    json={
                        "batch_id": body["batch_id"],
                        "events_ingested": 0,
                    },
                )
            return httpx.Response(404)

        cc = _make_client(_enabled_config(root), reg, handler)
        spool_dir = Path(_enabled_config(root).usage_spool_dir)
        spool_dir.mkdir(parents=True, exist_ok=True)

        # Write 3 files OUT of window_start order on disk. The filenames embed
        # the window_start prefix; lexical sort must recover chronological order.
        payloads = [
            ("20260703T100500Z", "batch-c"),  # newest
            ("20260703T100000Z", "batch-a"),  # oldest
            ("20260703T100200Z", "batch-b"),  # middle
        ]
        for ts, bid in payloads:
            payload = {
                "batch_id": bid,
                "window_start": f"2026-07-03T{ts[9:11]}:{ts[11:13]}:{ts[13:15]}Z",
                "window_end": f"2026-07-03T{ts[9:11]}:{ts[11:13]}:{ts[13:15]}Z",
                "host_id": HOST_ID,
                "events": [],
            }
            (spool_dir / f"{ts}_{bid}.jsonl").write_text(
                json.dumps(payload) + "\n"
            )

        await cc.replay_stale_spool()

        # Sorted by filename = window_start ascending: a, b, c.
        assert posted_order == ["batch-a", "batch-b", "batch-c"], posted_order
        assert list(spool_dir.glob("*.jsonl")) == []
        await cc.stop()
    finally:
        await reg.close()


# ===========================================================================
# 4. spool atomic write + disk full
# ===========================================================================


async def test_spool_atomic_write_format(tmp_path: Path):
    """After a flush whose POST fails (spool retained), the file name + JSON are correct."""
    root = tmp_path / "mv"
    reg = await _make_registry(root)
    try:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == f"/hosts/{HOST_ID}/usage":
                return httpx.Response(503)  # retain spool
            return httpx.Response(404)

        cc = _make_client(_enabled_config(root), reg, handler)
        await cc.arecord_event("u1", "route_resolution", count=2)
        await cc.flush_usage()

        spool_dir = Path(_enabled_config(root).usage_spool_dir)
        files = list(spool_dir.glob("*.jsonl"))
        assert len(files) == 1
        name = files[0].name
        # Name pattern: {window_start_basic_iso}_{batch_id}.jsonl
        assert name.endswith(".jsonl")
        prefix, _, batch_part = name.rpartition("_")
        assert prefix.startswith("20"), f"filename timestamp prefix wrong: {name}"
        # batch_part = "<uuid>.jsonl"
        assert batch_part.endswith(".jsonl")

        data = json.loads(files[0].read_text())
        assert "batch_id" in data
        assert "window_start" in data
        assert "window_end" in data
        assert "events" in data
        assert data["host_id"] == HOST_ID
        assert len(data["events"]) == 1
        assert data["events"][0]["universe_id"] == "u1"
        assert data["events"][0]["count"] == 2
        await cc.stop()
    finally:
        await reg.close()


async def test_spool_disk_full_error_observable(
    tmp_path: Path, caplog
):
    """OSError on fsync → ERROR log + counter restored + no spool file."""
    root = tmp_path / "mv"
    reg = await _make_registry(root)
    try:
        post_hits = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == f"/hosts/{HOST_ID}/usage":
                post_hits["n"] += 1
                body = json.loads(request.content)
                return httpx.Response(
                    200,
                    json={
                        "batch_id": body["batch_id"],
                        "events_ingested": len(body["events"]),
                    },
                )
            return httpx.Response(404)

        cc = _make_client(_enabled_config(root), reg, handler)

        original_fsync = os.fsync

        def failing_fsync(fd):
            raise OSError(errno.ENOSPC, "No space left on device")

        os.fsync = failing_fsync  # type: ignore[assignment]
        caplog.set_level(logging.ERROR, logger="gaottt.multiverse.control_client")
        try:
            await cc.arecord_event("u1", "route_resolution", count=5)
            await cc.flush_usage()
        finally:
            os.fsync = original_fsync  # type: ignore[assignment]

        # The disk-full flush did NOT reach the POST phase.
        assert post_hits["n"] == 0, "no POST should fire when spool write fails"

        # ERROR log was emitted (not silent data loss).
        assert any(
            "spool write failed" in r.message for r in caplog.records
        ), [r.message for r in caplog.records]

        # Counter was restored: the 5 events are back in memory.
        assert ("u1", "route_resolution") in cc._counter
        assert cc._counter[("u1", "route_resolution")].count == 5

        # No .jsonl spool file was produced by the failed flush.
        spool_dir = Path(_enabled_config(root).usage_spool_dir)
        assert list(spool_dir.glob("*.jsonl")) == []

        # stop() does a final flush (fsync restored) — that one POSTs fine.
        await cc.stop()
        assert post_hits["n"] == 1, "final flush during stop() should succeed"
    finally:
        await reg.close()


# ===========================================================================
# 5. quarantine corrupt spool
# ===========================================================================


async def test_quarantine_corrupt_spool(tmp_path: Path):
    """Malformed JSON in spool → moved to quarantine/, valid files still POST."""
    root = tmp_path / "mv"
    reg = await _make_registry(root)
    try:
        posted: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == f"/hosts/{HOST_ID}/usage":
                body = json.loads(request.content)
                posted.append(body["batch_id"])
                return httpx.Response(
                    200,
                    json={
                        "batch_id": body["batch_id"],
                        "events_ingested": 0,
                    },
                )
            return httpx.Response(404)

        cc = _make_client(_enabled_config(root), reg, handler)
        spool_dir = Path(_enabled_config(root).usage_spool_dir)
        spool_dir.mkdir(parents=True, exist_ok=True)

        # A corrupt file.
        (spool_dir / "20260703T100000Z_corrupt.jsonl").write_text(
            "{not valid json"
        )
        # A valid file.
        good_payload = {
            "batch_id": "good-1",
            "window_start": "2026-07-03T10:01:00Z",
            "window_end": "2026-07-03T10:01:00Z",
            "host_id": HOST_ID,
            "events": [],
        }
        (spool_dir / "20260703T100100Z_good-1.jsonl").write_text(
            json.dumps(good_payload) + "\n"
        )

        await cc.replay_stale_spool()

        # Corrupt file was moved to quarantine.
        quarantine = spool_dir / "quarantine"
        assert (quarantine / "20260703T100000Z_corrupt.jsonl").exists()
        assert not (spool_dir / "20260703T100000Z_corrupt.jsonl").exists()

        # Valid file was POSTed and deleted.
        assert posted == ["good-1"]
        assert not (spool_dir / "20260703T100100Z_good-1.jsonl").exists()
        await cc.stop()
    finally:
        await reg.close()


# ===========================================================================
# 6. pull_host_universes
# ===========================================================================


async def test_pull_host_universes_failure_returns_none(tmp_path: Path):
    root = tmp_path / "mv"
    reg = await _make_registry(root)
    try:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("control plane down")

        cc = _make_client(_enabled_config(root), reg, handler)
        result = await cc.pull_host_universes()
        assert result is None
        # Network error is NOT a permanent auth failure.
        assert cc._auth_failed is False
        await cc.stop()
    finally:
        await reg.close()


async def test_pull_host_universes_401_permanent_auth_failure(tmp_path: Path):
    """401 → _auth_failed=True → subsequent calls skip HTTP entirely."""
    root = tmp_path / "mv"
    reg = await _make_registry(root)
    try:
        hits = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            hits["n"] += 1
            if request.url.path == f"/hosts/{HOST_ID}/universes":
                return httpx.Response(401, json={"detail": "invalid token"})
            return httpx.Response(404)

        cc = _make_client(_enabled_config(root), reg, handler)

        # First call hits the network, gets 401, sets the flag.
        result = await cc.pull_host_universes()
        assert result is None
        assert cc._auth_failed is True
        assert cc._auth_failed_since is not None
        first_hits = hits["n"]

        # Second call returns None WITHOUT touching the network.
        result2 = await cc.pull_host_universes()
        assert result2 is None
        assert hits["n"] == first_hits, "no HTTP attempt after auth failure"

        # flush_usage also skips POST (spool accumulates).
        await cc.arecord_event("u1", "route_resolution", count=1)
        await cc.flush_usage()
        spool_dir = Path(_enabled_config(root).usage_spool_dir)
        assert len(list(spool_dir.glob("*.jsonl"))) == 1, (
            "spool must accumulate while auth_failed (for post-rotation replay)"
        )

        # auth_failure_state reflects the situation.
        state = cc.auth_failure_state()
        assert state["auth_failed"] is True
        assert state["since"] is not None
        assert state["spool_pending"] == 1
        await cc.stop()
    finally:
        await reg.close()


# ===========================================================================
# 7. reconcile_with_control
# ===========================================================================


async def test_reconcile_local_authoritative(tmp_path: Path):
    """Sync payload reflects LOCAL state; control-only universes are WARNING-logged."""
    root = tmp_path / "mv"
    reg = await _make_registry(root)
    try:
        # Local: one active universe, one deleted.
        port1 = _grab_free_port()
        port2 = _grab_free_port()
        await reg.create_universe("u_local", "owner", port1, "emb", "v1")
        await reg.create_universe("u_deleted", "owner", port2, "emb", "v1")
        await reg.delete_universe("u_deleted")

        sync_bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == f"/hosts/{HOST_ID}/universes":
                # Control knows of a universe local doesn't have.
                return httpx.Response(
                    200,
                    json=[
                        {"universe_id": "u_remote_only", "status": "active"},
                        {"universe_id": "u_local", "status": "active"},
                    ],
                )
            if request.url.path == f"/hosts/{HOST_ID}/sync":
                sync_bodies.append(json.loads(request.content))
                return httpx.Response(200, json={"inserted": 0, "conflicts": 0})
            return httpx.Response(404)

        cc = _make_client(_enabled_config(root), reg, handler)
        await cc.reconcile_with_control()

        assert len(sync_bodies) == 1
        body = sync_bodies[0]
        local_ids = {
            e["universe_id"] for e in body["local_universes"]
        }
        # LOCAL is authoritative: local_universes reflects the local registry.
        assert "u_local" in local_ids
        assert "u_deleted" in local_ids
        # Control-only universe is NOT injected into local_universes.
        assert "u_remote_only" not in local_ids
        # Deleted universes are reported.
        assert "u_deleted" in body["deleted_universes"]
        await cc.stop()
    finally:
        await reg.close()


async def test_reconcile_control_deleted_universe_local_warning(
    tmp_path: Path, caplog
):
    """Control says deleted + local active → WARNING, no local mutation."""
    root = tmp_path / "mv"
    reg = await _make_registry(root)
    try:
        port = _grab_free_port()
        await reg.create_universe("u1", "owner", port, "emb", "v1")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == f"/hosts/{HOST_ID}/universes":
                return httpx.Response(
                    200,
                    json=[{"universe_id": "u1", "status": "deleted"}],
                )
            if request.url.path == f"/hosts/{HOST_ID}/sync":
                return httpx.Response(200, json={"inserted": 0, "conflicts": 0})
            return httpx.Response(404)

        cc = _make_client(_enabled_config(root), reg, handler)
        caplog.set_level(logging.WARNING, logger="gaottt.multiverse.control_client")
        await cc.reconcile_with_control()

        # WARNING logged for the conflict.
        assert any(
            "u1" in r.message and "deleted" in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]

        # Local registry is NOT mutated (J5 — local is authoritative).
        row = await reg.get_universe("u1")
        assert row is not None
        assert row["status"] == "active"
        await cc.stop()
    finally:
        await reg.close()


# ===========================================================================
# 8. auth_failure_state accessor
# ===========================================================================


async def test_auth_failure_state_accessor(tmp_path: Path):
    """auth_failure_state reflects before/after a 401."""
    root = tmp_path / "mv"
    reg = await _make_registry(root)
    try:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == f"/hosts/{HOST_ID}/universes":
                return httpx.Response(401, json={"detail": "bad token"})
            return httpx.Response(404)

        cc = _make_client(_enabled_config(root), reg, handler)

        # Before any failure.
        state = cc.auth_failure_state()
        assert state["auth_failed"] is False
        assert state["since"] is None
        assert state["spool_pending"] == 0

        # Trigger the 401.
        await cc.pull_host_universes()

        state2 = cc.auth_failure_state()
        assert state2["auth_failed"] is True
        assert state2["since"] is not None
        await cc.stop()
    finally:
        await reg.close()


# ===========================================================================
# 9. start() is non-blocking (Codex B2)
# ===========================================================================


async def test_start_non_blocking_when_control_down(tmp_path: Path):
    """B2: stale spool files + control plane unreachable → ``start()``
    returns immediately. The replay runs in the BACKGROUND; it must not
    delay supervisor startup by up to ``_HTTP_TIMEOUT`` per spool file.

    The handler simulates an unreachable control plane with a synchronous
    sleep (long enough that a SYNCHRONOUS replay inside ``start()`` would
    blow the bound) followed by a ``ConnectError``. Because ``start()``
    (post-fix) contains no ``await`` and ``asyncio.create_task`` only
    SCHEDULES the replay, the caller resumes and measures elapsed time
    BEFORE the loop runs the background task — so the bound holds iff the
    replay was actually moved out of ``start()``.

    After yielding to the loop, the background replay hits the handler,
    fails (network error), and retains the spool. Network error is NOT a
    permanent auth failure, so ``_auth_failed`` stays False.
    """
    root = tmp_path / "mv"
    reg = await _make_registry(root)
    try:
        # Handler blocks 0.5s then raises ConnectError. A synchronous
        # replay inside start() (old code) would inherit this 0.5s.
        def handler(request: httpx.Request) -> httpx.Response:
            time.sleep(0.5)
            raise httpx.ConnectError("control plane down")

        cc = _make_client(_enabled_config(root), reg, handler)
        spool_dir = Path(_enabled_config(root).usage_spool_dir)
        spool_dir.mkdir(parents=True, exist_ok=True)
        # One stale spool file (a crashed batch from a previous run).
        payload = {
            "batch_id": "stale-1",
            "window_start": "2026-07-03T10:00:00Z",
            "window_end": "2026-07-03T10:00:00Z",
            "host_id": HOST_ID,
            "events": [],
        }
        (spool_dir / "20260703T100000Z_stale-1.jsonl").write_text(
            json.dumps(payload) + "\n"
        )

        t0 = time.monotonic()
        await cc.start()
        elapsed = time.monotonic() - t0
        # start() must return well under the handler's 0.5s block. A
        # synchronous replay (old code) would take >= 0.5s here.
        assert elapsed < 0.2, (
            f"start() blocked for {elapsed:.3f}s — the stale spool replay "
            "must run in the background, not inside start() (Codex B2)"
        )

        # Yield to the loop so the background replay task actually runs.
        # It blocks ~0.5s (handler sleep) then fails — ConnectError is
        # caught, WARNING-logged, and the spool file is RETAINED.
        await asyncio.sleep(0.7)

        # Spool retained: a network error does not delete the spool.
        files = list(spool_dir.glob("*.jsonl"))
        assert len(files) == 1, (
            f"spool should be retained on ConnectError, got {files}"
        )
        # A network error is NOT a permanent auth failure (only 401 is).
        assert cc._auth_failed is False, (
            "ConnectError must not set _auth_failed (only a 401 does)"
        )

        await cc.stop()
    finally:
        await reg.close()
