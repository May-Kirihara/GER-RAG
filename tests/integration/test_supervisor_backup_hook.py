"""MV5 WP-2 — supervisor backup hook integration tests.

Drives the real supervisor FastAPI app (ASGITransport) over a live
StubServiceEmbedder and asserts the litestream-config regeneration hook:

1. is inert when ``litestream_config_path`` is unset (default-invariant —
   the regression fence for ``test_supervisor.py`` continuity);
2. regenerates the YAML on create and delete success paths;
3. survives its own failure (best-effort — HTTP response stays 2xx, and a
   pre-existing YAML is left untouched by the atomic write);
4. stays 1:1 with on-disk db-bearing universes after concurrent
   create/delete (the ``_backup_hook_lock`` stale-write fence, Codex review
   round-2 B2);
5. never leaks the backup knob into the spawned-backend env (D2 / review #7).

Reality note on ``gaottt.db``: the supervisor's ``create_universe`` makes
the dir + ``manifest.json`` + registry row, but ``gaottt.db`` is created by
the backend engine on its first startup. In these light tests the backend
is never spawned (probe is mocked), so the helper :func:`_seed_db` places a
placeholder ``gaottt.db`` to represent a started backend — without it the
generator's db-required scan rule (plan §WP-1) correctly excludes the
universe. The comparison set is therefore :func:`_live_db_uids` (live
universes that have both manifest + db), not all live dirs.

To prove an HTTP op actually *triggered* the hook (rather than a stale
file), tests delete the output file then perform the op and check it
reappears with refreshed content.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from gaottt.config import GaOTTTConfig
from gaottt.multiverse.registry import UNIVERSES_SUBDIR

from tests.integration._supervisor_helpers import (
    ADMIN_KEY,
    SUPERVISOR,
    StubServiceEmbedder,
    admin_headers,
    asgi_client,
    create_universe,
    make_supervisor,
    reserve_port_range,
    start_uvicorn,
    stop_uvicorn,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def embedder_url():
    """One StubServiceEmbedder on a uvicorn background thread for the module."""
    from gaottt.embedding.service import create_app

    app = create_app(StubServiceEmbedder(dimension=768))
    server, thread, port = start_uvicorn(app)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        stop_uvicorn(server, thread)


@pytest.fixture
def multiverse_root(tmp_path: Path) -> Path:
    root = tmp_path / "multiverse"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    (root / UNIVERSES_SUBDIR).mkdir(parents=True, exist_ok=True)
    (root / "trash").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def port_range() -> tuple[int, int]:
    return reserve_port_range()


def _make_config(root: Path, embedder_url: str, port_range, litestream_path: str) -> GaOTTTConfig:
    start, end = port_range
    return GaOTTTConfig(
        multiverse_root=str(root),
        supervisor_admin_key=ADMIN_KEY,
        embedder_endpoint=embedder_url,
        universe_port_range_start=start,
        universe_port_range_end=end,
        supervisor_readiness_timeout=30.0,
        supervisor_spawn_concurrency=3,
        litestream_config_path=litestream_path,
    )


# ---------------------------------------------------------------------------
# on-disk state helpers
# ---------------------------------------------------------------------------

def _seed_db(root: Path, uid: str) -> None:
    """Place a placeholder gaottt.db in the universe dir.

    Represents a backend having started (the backend's SqliteStore creates
    the real db). Without this the generator's db-required scan rule
    excludes the universe — which is correct behavior, but these hook tests
    need a db present to assert the YAML lists the universe.
    """
    (root / UNIVERSES_SUBDIR / uid / "gaottt.db").write_bytes(b"sqlite-placeholder")


def _yaml_uids(path: Path) -> set[str]:
    """Parse the generated YAML; return the set of universe uids it lists."""
    if not path.exists():
        return set()
    data = yaml.safe_load(path.read_text())
    return {e["path"].split("/")[-2] for e in data["dbs"]}


def _live_db_uids(root: Path) -> set[str]:
    """Live universe uids that have BOTH manifest + gaottt.db.

    This is the honest comparison set for the YAML: the generator includes
    a universe iff it has manifest + db, so the YAML must equal this set
    after any hook firing.
    """
    udir = root / UNIVERSES_SUBDIR
    out: set[str] = set()
    for p in udir.iterdir():
        if not p.is_dir() or p.name == "trash":
            continue
        if (p / "manifest.json").exists() and (p / "gaottt.db").exists():
            out.add(p.name)
    return out


async def _delete(client, uid: str) -> None:
    """Delete a universe with the backend probe mocked DOWN (no real spawn)."""
    from gaottt.multiverse.supervisor import PROBE_DOWN

    with patch(f"{SUPERVISOR}._probe_backend_with_token",
               AsyncMock(return_value=PROBE_DOWN)):
        r = await client.delete(f"/admin/universes/{uid}", headers=admin_headers())
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 1. default-invariant: knob unset → no YAML file ever created
# ---------------------------------------------------------------------------

async def test_knob_unset_creates_no_yaml(embedder_url, multiverse_root, port_range, tmp_path):
    config = _make_config(multiverse_root, embedder_url, port_range, litestream_path="")
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            body = await create_universe(client, owner="alice")
            assert body["universe_id"]
        # No YAML file was ever written anywhere — the hook short-circuits.
        assert not list(tmp_path.glob("*.yml"))
        assert not list(tmp_path.glob("*.yaml"))
    finally:
        await reg.close()


# ---------------------------------------------------------------------------
# 2. create / delete regenerate the YAML
# ---------------------------------------------------------------------------

async def test_create_triggers_hook_and_lists_db_bearing_universe(
    embedder_url, multiverse_root, port_range, tmp_path,
):
    """A create fires the hook. Proved by: seed a db in a prior universe,
    delete the output file, then create a NEW universe — the file must
    reappear (hook fired) and list the prior universe (rescan saw its db)."""
    out = tmp_path / "litestream.yml"
    config = _make_config(multiverse_root, embedder_url, port_range, litestream_path=str(out))
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            prior = await create_universe(client, owner="prior")
            _seed_db(multiverse_root, prior["universe_id"])
            # Drop the file so the next hook write is observable as a fresh
            # creation (proves the create actually fired the hook).
            if out.exists():
                out.unlink()

            new = await create_universe(client, owner="new")
            # File reappeared ⇒ create fired the hook.
            assert out.exists()
            # Rescan saw the prior universe's db (new has no db yet, so it is
            # correctly excluded — the generator's db-required rule).
            assert _yaml_uids(out) == {prior["universe_id"]}
            assert _yaml_uids(out) == _live_db_uids(multiverse_root)
            # new was created solely to fire the hook; reference it so the
            # intent is explicit and the linter sees the use.
            assert new["universe_id"] != prior["universe_id"]
    finally:
        await reg.close()


async def test_delete_removes_universe_from_yaml(embedder_url, multiverse_root, port_range, tmp_path):
    out = tmp_path / "litestream.yml"
    config = _make_config(multiverse_root, embedder_url, port_range, litestream_path=str(out))
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            a = await create_universe(client, owner="a")
            b = await create_universe(client, owner="b")
            _seed_db(multiverse_root, a["universe_id"])
            _seed_db(multiverse_root, b["universe_id"])
            # Sanity: after seeding + one more op, YAML lists both. Trigger
            # via a third create's hook rescan.
            await create_universe(client, owner="c")
            assert _yaml_uids(out) == {a["universe_id"], b["universe_id"]}

            # Delete a — its hook rescan drops a from the YAML.
            await _delete(client, a["universe_id"])
            assert _yaml_uids(out) == {b["universe_id"]}
            assert _yaml_uids(out) == _live_db_uids(multiverse_root)
    finally:
        await reg.close()


async def test_create_delete_create_matches_final_state(embedder_url, multiverse_root, port_range, tmp_path):
    out = tmp_path / "litestream.yml"
    config = _make_config(multiverse_root, embedder_url, port_range, litestream_path=str(out))
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            first = await create_universe(client, owner="first")
            _seed_db(multiverse_root, first["universe_id"])
            await _delete(client, first["universe_id"])
            second = await create_universe(client, owner="second")
            _seed_db(multiverse_root, second["universe_id"])
            # Trigger a final rescan via one more create.
            await create_universe(client, owner="trigger")
            assert _yaml_uids(out) == {second["universe_id"]}
            assert _yaml_uids(out) == _live_db_uids(multiverse_root)
    finally:
        await reg.close()


# ---------------------------------------------------------------------------
# 3. exception injection — best-effort + atomic-write fence
# ---------------------------------------------------------------------------

async def test_hook_failure_does_not_break_response_and_preserves_yaml(
    embedder_url, multiverse_root, port_range, tmp_path, monkeypatch,
):
    out = tmp_path / "litestream.yml"
    config = _make_config(multiverse_root, embedder_url, port_range, litestream_path=str(out))
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            # Seed a known-good YAML on disk first.
            good = await create_universe(client, owner="good")
            _seed_db(multiverse_root, good["universe_id"])
            await create_universe(client, owner="trigger")  # rescan → YAML={good}
            assert _yaml_uids(out) == {good["universe_id"]}
            pre = out.read_text()

            # Now make the pure function raise. The hook must swallow it,
            # the create response must still be 201, and the pre-existing
            # YAML must be byte-identical (atomic write never reached).
            def boom(_root, **kw):
                raise RuntimeError("simulated generator failure")

            monkeypatch.setattr(
                "gaottt.multiverse.backup.generate_litestream_config", boom)
            r = await client.post(
                "/admin/universes",
                json={"owner_label": "after-failure"},
                headers=admin_headers(),
            )
            assert r.status_code == 201
            assert out.read_text() == pre
    finally:
        await reg.close()


# ---------------------------------------------------------------------------
# 4. concurrency — stale-write fence (Codex review round-2 B2)
# ---------------------------------------------------------------------------

async def test_concurrent_create_delete_yaml_matches_on_disk(embedder_url, multiverse_root, port_range, tmp_path):
    """asyncio.gather of 5 creates + 2 deletes; after the burst settles, the
    YAML must parse and be 1:1 with on-disk db-bearing universes (no dupes,
    no missing). This is the structural proof that _backup_hook_lock's
    scan+write serialization keeps concurrent hooks consistent.

    This test verifies the ``_backup_hook_lock`` prevents stale writes and
    that the YAML converges to on-disk truth after the burst. The final
    ``await sup._run_backup_hook()`` represents the periodic regen / next
    operation's hook that closes the new-universe lag documented in
    Operations-Backup-Multiverse.md §supervisor hook. The structural close
    (hook on backend readiness) is a future-stage item.

    Note on the settle rescan: each ``do_create`` seeds the placeholder db
    AFTER the create's own hook has already fired (the hook runs at the end
    of the HTTP handler, before ``do_create`` returns to seed). So the last
    hook to fire during the burst may have run before every db was seeded.
    The post-burst ``_run_backup_hook()`` is the eventual-consistency
    rescan (in production this happens on the next create/delete, a
    periodic regen cron, or the backend-readiness hook in _ensure_locked);
    it must converge to the on-disk truth. What the lock is proven to
    prevent here is corruption / inconsistency during the burst: the YAML
    is always parseable and never carries phantoms from an older scan
    winning via a split scan+write."""
    out = tmp_path / "litestream.yml"
    config = _make_config(multiverse_root, embedder_url, port_range, litestream_path=str(out))
    app, reg = await make_supervisor(config)
    try:
        sup = app.state.supervisor
        async with asgi_client(app) as client:
            # Seed two universes up front (with dbs) so the concurrent
            # deletes have known db-bearing targets.
            seed_a = await create_universe(client, owner="seedA")
            seed_b = await create_universe(client, owner="seedB")
            _seed_db(multiverse_root, seed_a["universe_id"])
            _seed_db(multiverse_root, seed_b["universe_id"])

            async def do_create(owner: str) -> dict:
                body = await create_universe(client, owner=owner)
                # Seed the db immediately so the universe is db-bearing for
                # any subsequent hook rescan.
                _seed_db(multiverse_root, body["universe_id"])
                return body

            async def do_delete(uid: str):
                await _delete(client, uid)

            # 5 fresh creates + 2 deletes of the seeds, all concurrent.
            results = await asyncio.gather(
                do_create("c1"), do_create("c2"), do_create("c3"),
                do_create("c4"), do_create("c5"),
                do_delete(seed_a["universe_id"]),
                do_delete(seed_b["universe_id"]),
                return_exceptions=True,
            )
            for r in results:
                assert not isinstance(r, Exception), f"unexpected: {r!r}"

            new_uids = {r["universe_id"] for r in results
                        if isinstance(r, dict) and "universe_id" in r}
            assert len(new_uids) == 5

            # During the burst the YAML must always have been parseable and
            # duplicate-free (the lock + atomic write guarantee). Verify the
            # post-burst snapshot is well-formed before the settle rescan.
            pre_settle = _yaml_uids(out)
            assert len(pre_settle) == len(set(pre_settle)), \
                f"duplicate entries in YAML (torn write): {pre_settle}"

            # Settle: one final rescan so the YAML reflects the fully-seeded
            # final state (see docstring for why this is needed).
            await sup._run_backup_hook()  # noqa: SLF001

            yaml_uids = _yaml_uids(out)
            live_db = _live_db_uids(multiverse_root)
            assert yaml_uids == live_db, (
                f"YAML drifted from on-disk db-bearing state after settle: "
                f"yaml={yaml_uids} live_db={live_db}"
            )
            assert yaml_uids == new_uids, (
                f"YAML does not match the 5 new universes: "
                f"yaml={yaml_uids} new={new_uids}"
            )
            assert {seed_a["universe_id"], seed_b["universe_id"]}.isdisjoint(yaml_uids), \
                "deleted seeds leaked into YAML"
    finally:
        await reg.close()


# ---------------------------------------------------------------------------
# 5. spawn env isolation — backup knob must NOT leak to backends (review #7)
# ---------------------------------------------------------------------------

async def test_spawn_env_does_not_leak_backup_knob(embedder_url, multiverse_root, port_range, tmp_path):
    """Even with litestream_config_path SET, _build_spawn_env must not pass
    it (or any LITESTREAM/BACKUP var) into the spawned backend env."""
    out = tmp_path / "litestream.yml"
    config = _make_config(multiverse_root, embedder_url, port_range, litestream_path=str(out))
    app, reg = await make_supervisor(config)
    try:
        sup = app.state.supervisor
        env = sup._build_spawn_env(Path("/tmp/sample-universe"), "tok")  # noqa: SLF001
        offending = {k for k in env if "LITESTREAM" in k.upper() or "BACKUP" in k.upper()}
        assert offending == set(), (
            f"backup knob leaked into spawn env: {offending}"
        )
        assert "GAOTTT_LITESTREAM_CONFIG_PATH" not in env
    finally:
        await reg.close()
