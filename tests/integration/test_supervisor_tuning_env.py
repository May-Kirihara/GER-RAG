"""Phase U WP-2 — supervisor runtime-tuning env allowlist integration tests.

R1 の rollback 経路: supervisor の spawn env は全 ``GAOTTT_*`` を strip する
設計 (MV3) のままでは Phase T/U の tuning knob を backend に届けられない。
本 module は ``_build_spawn_env`` の exact-name allowlist merge と
spawn 時 validation の fail-fast を、実 supervisor FastAPI app
(ASGITransport + 実 StubServiceEmbedder) 経由で検証する:

1. allowlist 値の spawn env 通行 (identity 系は常に勝つ) — ``_build_spawn_env``
   直接呼び出し (``test_supervisor_backup_hook.py`` の review #7 test と
   同一 pattern)。
2. allowlist 値が spawn された backend の env に届くこと — /route 経由、
   ``subprocess.Popen`` / probe seam を patch (``test_supervisor.py`` の
   ``test_token_stale_recovery`` と同一 pattern)。
3. 不正値 (非 bool / NaN / 不正 int) で spawn 拒否 + /route 500 に
   validation error が載ること (fail-fast・観測可能)。
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gaottt.multiverse.registry import UNIVERSES_SUBDIR
from gaottt.multiverse.supervisor import PROBE_DOWN, PROBE_OK

from tests.integration._supervisor_helpers import (
    SUPERVISOR,
    StubServiceEmbedder,
    asgi_client,
    create_universe,
    kill_backends_in_range,
    make_config,
    make_supervisor,
    reserve_port_range,
    start_uvicorn,
    stop_uvicorn,
)


# ---------------------------------------------------------------------------
# fixtures — test_supervisor_backup_hook.py と同一構成 (module 固有)
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


@pytest.fixture(autouse=True)
def _kill_spawned_backends(port_range: tuple[int, int]):
    """test_supervisor.py と同一の後始末 fence (実 process は spawn しない
    のが本 module の前提だが、patch の貼り忘れ回帰時に備える)。"""
    yield
    kill_backends_in_range(port_range[0], port_range[1])


# ---------------------------------------------------------------------------
# 1. _build_spawn_env 直接呼び出し — allowlist 通行 + identity 優先
# ---------------------------------------------------------------------------

async def test_build_spawn_env_tuning_passthrough_and_identity_precedence(
    embedder_url, multiverse_root, port_range, monkeypatch,
):
    """Allowlisted tuning knob は通行、identity 系 4 key は parent env に
    攻撃値があっても supervisor の明示上書きが常に勝つこと。"""
    config = make_config(multiverse_root, embedder_url, port_range=port_range)
    app, reg = await make_supervisor(config)
    try:
        sup = app.state.supervisor
        universe_dir = multiverse_root / UNIVERSES_SUBDIR / "sample"

        # parent env: 有効な allowlist 値 + identity 系への攻撃値 +
        # allowlist 外の GAOTTT_* knob。config 構築後に setenv するため
        # data_dir factory の副作用 (mkdir) は発生しない。
        monkeypatch.setenv("GAOTTT_DIRECT_QUALIFICATION_ENABLED", "false")
        monkeypatch.setenv("GAOTTT_SEMANTIC_FLOOR", "0.25")
        monkeypatch.setenv("GAOTTT_DATA_DIR", str(multiverse_root / "attacker"))
        monkeypatch.setenv("GAOTTT_BACKEND_TOKEN", "attacker-token")
        monkeypatch.setenv("GAOTTT_EMBEDDER_ENDPOINT", "http://127.0.0.1:1/x")
        monkeypatch.setenv("GAOTTT_OWNER_LEASE_ENABLED", "false")
        monkeypatch.setenv("GAOTTT_CONFIG", str(multiverse_root / "attacker.json"))
        monkeypatch.setenv("GAOTTT_FUTURE_KNOB", "1")

        env = sup._build_spawn_env(universe_dir, "tok")  # noqa: SLF001

        # allowlist 値はそのまま通行
        assert env["GAOTTT_DIRECT_QUALIFICATION_ENABLED"] == "false"
        assert env["GAOTTT_SEMANTIC_FLOOR"] == "0.25"
        # identity overlay は常に勝つ (parent env の攻撃値では上書きできない)
        assert env["GAOTTT_DATA_DIR"] == str(universe_dir)
        assert env["GAOTTT_BACKEND_TOKEN"] == "tok"
        assert env["GAOTTT_EMBEDDER_ENDPOINT"] == embedder_url
        assert env["GAOTTT_OWNER_LEASE_ENABLED"] == "true"
        # allowlist 外の GAOTTT_* は spawn env に届かない
        assert "GAOTTT_CONFIG" not in env
        assert "GAOTTT_FUTURE_KNOB" not in env
        # MV5 review #7 fence: LITESTREAM / BACKUP 系も届かない
        offending = {
            k for k in env
            if "LITESTREAM" in k.upper() or "BACKUP" in k.upper()
        }
        assert offending == set()
    finally:
        await reg.close()


# ---------------------------------------------------------------------------
# 2. /route 経由 — allowlist 値が spawned backend env に届く
# ---------------------------------------------------------------------------

async def test_allowlisted_tuning_env_reaches_spawned_backend_env(
    embedder_url, multiverse_root, port_range, monkeypatch,
):
    """supervisor 経由の rollback: GAOTTT_DIRECT_QUALIFICATION_ENABLED=false を
    parent env に置くと、spawn された backend の env に届く (R1 rollback 経路)。
    parent env の GAOTTT_DATA_DIR 攻撃値は universe dir に敗北する。"""
    monkeypatch.setenv("GAOTTT_DIRECT_QUALIFICATION_ENABLED", "false")
    monkeypatch.setenv("GAOTTT_TTT_QUALIFICATION_ENABLED", "false")
    monkeypatch.setenv("GAOTTT_DATA_DIR", str(multiverse_root / "attacker"))

    config = make_config(multiverse_root, embedder_url, port_range=port_range)
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            body = await create_universe(client, owner="tuning-owner")
            probe = AsyncMock(side_effect=[PROBE_DOWN, PROBE_OK])
            popen = MagicMock()
            with patch(f"{SUPERVISOR}._probe_backend_with_token", probe), \
                    patch(f"{SUPERVISOR}.subprocess.Popen", popen):
                r = await client.post("/route", json={"api_key": body["api_key"]})
            assert r.status_code == 200, r.text
            popen.assert_called_once()

            spawn_env = popen.call_args.kwargs["env"]
            assert spawn_env["GAOTTT_DIRECT_QUALIFICATION_ENABLED"] == "false"
            assert spawn_env["GAOTTT_TTT_QUALIFICATION_ENABLED"] == "false"
            expected_dir = (
                multiverse_root / UNIVERSES_SUBDIR / body["universe_id"]
            )
            assert spawn_env["GAOTTT_DATA_DIR"] == str(expected_dir)
    finally:
        await reg.close()


# ---------------------------------------------------------------------------
# 3. /route 経由 — 不正値で spawn 拒否 + 500 に validation error
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("bad_name", "bad_value"),
    [
        # 非 bool token (bool field)
        ("GAOTTT_AMBIENT_GATE_OR_SEMANTIC", "maybe"),
        # NaN (float field)
        ("GAOTTT_SEMANTIC_FLOOR", "nan"),
        # 不正 int (int field)
        ("GAOTTT_DIRECT_BM25_POOL_SIZE", "many"),
    ],
)
async def test_invalid_tuning_env_refuses_spawn_and_surfaces_500(
    embedder_url, multiverse_root, port_range, monkeypatch,
    bad_name, bad_value,
):
    """不正な tuning 値では backend を spawn せず、/route が 500 +
    validation error 明細を返す (fail-fast・観測可能)。"""
    monkeypatch.setenv(bad_name, bad_value)

    config = make_config(multiverse_root, embedder_url, port_range=port_range)
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            body = await create_universe(client, owner="bad-tuning-owner")
            probe = AsyncMock(return_value=PROBE_DOWN)
            popen = MagicMock()
            with patch(f"{SUPERVISOR}._probe_backend_with_token", probe), \
                    patch(f"{SUPERVISOR}.subprocess.Popen", popen):
                r = await client.post("/route", json={"api_key": body["api_key"]})

            assert r.status_code == 500, r.text
            assert bad_name in r.json()["detail"], (
                f"validation error for {bad_name} must surface in /route detail"
            )
            # spawn 拒否: Popen 未呼び出し + PID 未記録
            popen.assert_not_called()
            sup = app.state.supervisor
            assert body["universe_id"] not in sup._backend_pids  # noqa: SLF001
    finally:
        await reg.close()
