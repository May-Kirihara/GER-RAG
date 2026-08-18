"""WP-2a: embedder lazy spawn unit tests (RED stage).

Tests for the embedder lazy-spawn machinery that WP-2b will add to
``gaottt/multiverse/supervisor.py``:

- ``_spawn_embedder_detached(host, port, log_path, model) -> int``
- ``_Supervisor._build_embedder_spawn_env(self) -> dict[str, str]``
- ``_Supervisor._probe_embedder_health(self) -> bool``  (async)

These tests are **RED**: the functions do not exist yet, so every test
fails with ``AttributeError`` / ``ImportError`` at call time. Collection
still succeeds because each test imports the not-yet-implemented symbol
inside the function body (function-scope import), not at module level
(id=18aa877f lesson 2: a module-level ``ImportError`` would stop the
entire file from collecting and mask the other tests).

Contract source: ``docs/plans/embedder-auto-spawn-supervisor.md`` v3
§3.2-§3.8. Reference patterns:

- ``_spawn_backend_detached`` (spawn + detach): ``gaottt/server/mcp_proxy.py``
- ``_build_spawn_env`` (env strip): ``gaottt/multiverse/supervisor.py``
- ``_probe_backend_with_token`` AsyncClient mock:
  ``tests/unit/test_supervisor.py`` (test_probe_initialize_*)

B2-1 (Codex v2): the existing ``_embedder_ok()`` helper mocks
``httpx.Client.get`` for ``/info``. The new ``_probe_embedder_health``
uses ``httpx.AsyncClient`` for ``/healthz`` — a different seam. Test (d)
proves this independence so that WP-2b's handler integration does not
break the existing ``test_supervisor.py`` mock seam.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gaottt.config import GaOTTTConfig

ADMIN_KEY = "test-admin-key-spawn"


# ---------------------------------------------------------------------------
# fixtures (self-contained — not imported from test_supervisor.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_multiverse_root(tmp_path: Path) -> Path:
    root = tmp_path / "multiverse"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


@pytest.fixture
def stub_config(tmp_multiverse_root: Path) -> GaOTTTConfig:
    return GaOTTTConfig(
        multiverse_root=str(tmp_multiverse_root),
        supervisor_admin_key=ADMIN_KEY,
        embedder_endpoint="http://127.0.0.1:9999",
        supervisor_readiness_timeout=5.0,
    )


# ===========================================================================
# a. _spawn_embedder_detached — cmd construction + platform-specific detach
# ===========================================================================

def test_spawn_embedder_detached_linux_cmd_and_kwargs(tmp_path: Path):
    """a: cmd carries host/port/model as --flag value pairs; Linux uses
    ``start_new_session=True`` and ``stdin=DEVNULL``; the log file handle
    opened by the parent is closed after Popen (no fd leak)."""
    # function-scope import: RED until WP-2b implements this
    from gaottt.multiverse.supervisor import _spawn_embedder_detached

    log_path = tmp_path / "logs" / "embedder.log"
    popen = MagicMock()
    popen.return_value.pid = 12345

    with patch("gaottt.multiverse.supervisor.subprocess.Popen", popen), \
         patch("gaottt.multiverse.supervisor.sys.platform", "linux"):
        pid = _spawn_embedder_detached(
            host="127.0.0.1", port=7879,
            log_path=log_path, model="ruri-v3-310m",
        )

    assert pid == 12345
    assert popen.call_count == 1

    cmd = popen.call_args.args[0]
    kwargs = popen.call_args.kwargs

    # host / port / model appear as --flag value pairs
    assert "--host" in cmd
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
    assert "--port" in cmd
    assert cmd[cmd.index("--port") + 1] == "7879"
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "ruri-v3-310m"

    # Linux detach: new session so the child survives the parent's death
    assert kwargs.get("start_new_session") is True
    # stdin is DEVNULL — detached child must not read the parent's stdin
    assert kwargs.get("stdin") is subprocess.DEVNULL

    # stdout is the log file opened for append (not a raw fd or PIPE)
    log_file = kwargs.get("stdout")
    assert log_file is not None
    # The parent opened this handle; it must close it after handing it to
    # the child so it does not leak in the supervisor process.
    assert log_file.closed, (
        "log_file handle should be closed in the parent after Popen"
    )


def test_spawn_embedder_detached_windows_uses_creationflags(tmp_path: Path):
    """a: on Windows the function uses ``DETACHED_PROCESS |
    CREATE_NEW_PROCESS_GROUP`` instead of ``start_new_session``."""
    from gaottt.multiverse.supervisor import _spawn_embedder_detached

    # subprocess.DETACHED_PROCESS / CREATE_NEW_PROCESS_GROUP are Windows-only
    # constants absent on Linux's subprocess module; patch them into existence.
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200

    log_path = tmp_path / "embedder.log"
    popen = MagicMock()
    popen.return_value.pid = 54321

    with patch("gaottt.multiverse.supervisor.subprocess.Popen", popen), \
         patch("gaottt.multiverse.supervisor.sys.platform", "win32"), \
         patch(
             "gaottt.multiverse.supervisor.subprocess.DETACHED_PROCESS",
             DETACHED_PROCESS, create=True,
         ), \
         patch(
             "gaottt.multiverse.supervisor.subprocess.CREATE_NEW_PROCESS_GROUP",
             CREATE_NEW_PROCESS_GROUP, create=True,
         ):
        _spawn_embedder_detached(
            host="127.0.0.1", port=7879,
            log_path=log_path, model="ruri-v3-310m",
        )

    kwargs = popen.call_args.kwargs
    # Windows: creationflags, NOT start_new_session
    assert "start_new_session" not in kwargs
    assert kwargs.get("creationflags") == (
        DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    )
    assert kwargs.get("stdin") is subprocess.DEVNULL


# ===========================================================================
# b. _build_embedder_spawn_env — env strip pattern (mirrors _build_spawn_env)
# ===========================================================================

def test_build_embedder_spawn_env_strips_gaottt_keeps_path_home(
    monkeypatch, stub_config: GaOTTTConfig,
):
    """b: strips all GAOTTT_* from the supervisor's env (so the
    supervisor's GAOTTT_DATA_DIR / backend token do not leak into the
    embedder subprocess), preserves OS essentials (PATH / HOME), and
    returns a plain dict. Same pattern as ``_build_spawn_env``
    (supervisor.py:273)."""
    from gaottt.multiverse.supervisor import _Supervisor

    # Supervisor's own GAOTTT_* — must NOT leak into the embedder env
    monkeypatch.setenv("GAOTTT_DATA_DIR", "/supervisor/data/leak-test")
    monkeypatch.setenv("GAOTTT_BACKEND_TOKEN", "supervisor-secret")
    # OS essentials the subprocess needs to run
    monkeypatch.setenv("PATH", "/custom/path/bin")
    monkeypatch.setenv("HOME", "/custom/home")

    sup = _Supervisor(stub_config, MagicMock())
    env = sup._build_embedder_spawn_env()

    assert isinstance(env, dict)
    # Supervisor's GAOTTT_* are stripped (not inherited verbatim)
    assert env.get("GAOTTT_DATA_DIR") != "/supervisor/data/leak-test"
    assert env.get("GAOTTT_BACKEND_TOKEN") != "supervisor-secret"
    # PATH / HOME are preserved
    assert env.get("PATH") == "/custom/path/bin"
    assert env.get("HOME") == "/custom/home"


# ===========================================================================
# c. _probe_embedder_health — AsyncClient seam (httpx.AsyncClient for /healthz)
# ===========================================================================

async def test_probe_embedder_health_returns_true_on_200_ok(
    stub_config: GaOTTTConfig,
):
    """c: /healthz answers 200 with ``{"status": "ok"}`` → True."""
    from gaottt.multiverse.supervisor import _Supervisor

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "ok"}

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    # ``async with httpx.AsyncClient(...) as c:`` binds c to __aenter__'s
    # return value — point it back at the configured mock.
    mock_client.__aenter__.return_value = mock_client

    sup = _Supervisor(stub_config, MagicMock())

    with patch(
        "gaottt.multiverse.supervisor.httpx.AsyncClient",
        return_value=mock_client,
    ):
        result = await sup._probe_embedder_health()

    assert result is True


async def test_probe_embedder_health_returns_false_on_500(
    stub_config: GaOTTTConfig,
):
    """c: non-200 status → False."""
    from gaottt.multiverse.supervisor import _Supervisor

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.json.return_value = {"status": "error"}

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__.return_value = mock_client

    sup = _Supervisor(stub_config, MagicMock())

    with patch(
        "gaottt.multiverse.supervisor.httpx.AsyncClient",
        return_value=mock_client,
    ):
        result = await sup._probe_embedder_health()

    assert result is False


async def test_probe_embedder_health_returns_false_on_http_error(
    stub_config: GaOTTTConfig,
):
    """c: ``httpx.HTTPError`` (connection refused / timeout) → False."""
    import httpx

    from gaottt.multiverse.supervisor import _Supervisor

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=httpx.HTTPError("connection refused"),
    )
    mock_client.__aenter__.return_value = mock_client

    sup = _Supervisor(stub_config, MagicMock())

    with patch(
        "gaottt.multiverse.supervisor.httpx.AsyncClient",
        return_value=mock_client,
    ):
        result = await sup._probe_embedder_health()

    assert result is False


async def test_probe_embedder_health_returns_false_when_endpoint_unset():
    """c: ``embedder_endpoint`` not configured → False (nothing to probe)."""
    from gaottt.multiverse.supervisor import _Supervisor

    config = GaOTTTConfig(
        multiverse_root="/tmp/gaottt-test-probe-endpoint-unset",
        supervisor_admin_key=ADMIN_KEY,
        embedder_endpoint="",
    )
    sup = _Supervisor(config, MagicMock())

    result = await sup._probe_embedder_health()
    assert result is False


# ===========================================================================
# d. B2-1 regression — _probe_embedder_health must not touch httpx.Client.get
# ===========================================================================

async def test_probe_embedder_health_does_not_touch_sync_httpx_client_get(
    stub_config: GaOTTTConfig,
):
    """d (B2-1): ``_probe_embedder_health`` uses ``httpx.AsyncClient``
    (/healthz) and must NEVER touch ``httpx.Client.get`` (the /info seam
    that ``_validate_embedder`` and the existing ``_embedder_ok()``
    helper rely on). The two seams are independent; this is what lets
    WP-2b integrate ``ensure_embedder_up`` into the handler path without
    breaking existing ``test_supervisor.py`` mock seams."""
    from gaottt.multiverse.supervisor import _Supervisor

    # If _probe_embedder_health touches httpx.Client.get, this fires.
    forbidden_sync_get = MagicMock(
        side_effect=AssertionError(
            "_probe_embedder_health must not call httpx.Client.get — "
            "that seam belongs to _validate_embedder (/info)"
        ),
    )

    # Healthy /healthz response via AsyncClient
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "ok"}
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__.return_value = mock_client

    sup = _Supervisor(stub_config, MagicMock())

    with patch("httpx.Client.get", forbidden_sync_get), \
         patch(
             "gaottt.multiverse.supervisor.httpx.AsyncClient",
             return_value=mock_client,
         ):
        result = await sup._probe_embedder_health()

    assert result is True
    forbidden_sync_get.assert_not_called()


# ===========================================================================
# WP-3a — embedder lifecycle tests (RED stage)
#
# Tests for the lifecycle machinery WP-3b will add:
#   _embedder_idle_watchdog / _terminate_embedder / _wait_for_pid_exit
#   _reap_dead_backend_pids / _has_tracked_live_backends
#   _has_untracked_live_backends / _reset_embedder_state (extension)
#
# Contract source: docs/plans/embedder-auto-spawn-supervisor.md v3 §3.5-§3.6,
# §3.9, §5, §6 WP-3a. B2-6 (PermissionError / SIGKILL-後-alive → state stays
# owned_terminating) is the key behavioural contract.
#
# Each test imports the not-yet-implemented symbol inside the function body
# (function-scope import) so collection succeeds even though the methods
# are RED (AttributeError at call time).
# ===========================================================================


# ---------------------------------------------------------------------------
# e. _wait_for_pid_exit — waitpid-based exit poll
# ---------------------------------------------------------------------------

async def test_wait_for_pid_exit_true_on_immediate_exit(stub_config: GaOTTTConfig):
    """e: ``os.waitpid`` returns a non-zero first element (process exited)
    → True immediately, no sleeping."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    # (pid, status) — done != 0 means the process has exited
    with patch("gaottt.multiverse.supervisor.os.waitpid",
               return_value=(12345, 0)):
        result = await sup._wait_for_pid_exit(12345, timeout=5.0)
    assert result is True


async def test_wait_for_pid_exit_true_on_childprocesserror(stub_config: GaOTTTConfig):
    """e: ``ChildProcessError`` (already reaped / never ours) → True."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    with patch("gaottt.multiverse.supervisor.os.waitpid",
               side_effect=ChildProcessError()):
        result = await sup._wait_for_pid_exit(12345, timeout=5.0)
    assert result is True


async def test_wait_for_pid_exit_false_on_timeout(stub_config: GaOTTTConfig):
    """e: process never exits within ``timeout`` → False."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    # waitpid always returns (0, 0) → still running
    # Drive monotonic past the deadline on the 3rd call:
    #   1st: deadline = T0 + timeout
    #   2nd: while-check T0 < deadline → enter loop
    #   3rd: while-check T0+big >= deadline → exit → return False
    with patch("gaottt.multiverse.supervisor.os.waitpid",
               return_value=(0, 0)), \
         patch("gaottt.multiverse.supervisor.time.monotonic",
               side_effect=[0.0, 0.0, 1000.0]), \
         patch("gaottt.multiverse.supervisor.asyncio.sleep",
               AsyncMock()):
        result = await sup._wait_for_pid_exit(12345, timeout=0.1)
    assert result is False


# ---------------------------------------------------------------------------
# f. _terminate_embedder — B2-6 state machine
#    (owned_idle → owned_terminating → unowned / stays owned_terminating)
# ---------------------------------------------------------------------------

async def test_terminate_embedder_returns_if_not_owned_idle(
    stub_config: GaOTTTConfig,
):
    """f: state is not ``owned_idle`` → no-op (no signal, state unchanged)."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    sup._embedder_state = "unowned"
    sup._embedder_pid = 12345

    with patch("gaottt.multiverse.supervisor.os.kill") as kill:
        await sup._terminate_embedder()

    kill.assert_not_called()
    assert sup._embedder_state == "unowned"


async def test_terminate_embedder_pid_none_resets(stub_config: GaOTTTConfig):
    """f: ``owned_idle`` but ``_embedder_pid`` is None → reset to unowned."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    sup._embedder_state = "owned_idle"
    sup._embedder_pid = None

    with patch("gaottt.multiverse.supervisor.os.kill") as kill:
        await sup._terminate_embedder()

    kill.assert_not_called()
    assert sup._embedder_state == "unowned"


async def test_terminate_embedder_clean_exit_on_sigterm(stub_config: GaOTTTConfig):
    """f: SIGTERM → process exits within 5s poll → state reset to unowned."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    sup._embedder_state = "owned_idle"
    sup._embedder_pid = 12345
    sup._embedder_info_cache = {"model_name": "x"}

    with patch("gaottt.multiverse.supervisor.os.kill") as kill, \
         patch.object(_Supervisor, "_wait_for_pid_exit",
                      AsyncMock(return_value=True)):
        await sup._terminate_embedder()

    # Only SIGTERM was needed
    kill.assert_called_once_with(12345, signal.SIGTERM)
    assert sup._embedder_state == "unowned"
    assert sup._embedder_pid is None
    assert sup._embedder_info_cache is None


async def test_terminate_embedder_sigkill_after_sigterm_timeout(
    stub_config: GaOTTTConfig,
):
    """f: SIGTERM does not make the process exit within 5s → escalate to
    SIGKILL → process exits → state reset to unowned."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    sup._embedder_state = "owned_idle"
    sup._embedder_pid = 12345

    # 1st wait (after SIGTERM, 5s) → False; 2nd wait (after SIGKILL, 2s) → True
    with patch("gaottt.multiverse.supervisor.os.kill") as kill, \
         patch.object(_Supervisor, "_wait_for_pid_exit",
                      AsyncMock(side_effect=[False, True])):
        await sup._terminate_embedder()

    assert kill.call_count == 2
    kill.assert_any_call(12345, signal.SIGTERM)
    kill.assert_any_call(12345, signal.SIGKILL)
    assert sup._embedder_state == "unowned"


async def test_terminate_embedder_processlookuperror_resets(
    stub_config: GaOTTTConfig,
):
    """f: SIGTERM raises ``ProcessLookupError`` (child already reaped) →
    reset to unowned."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    sup._embedder_state = "owned_idle"
    sup._embedder_pid = 12345

    with patch("gaottt.multiverse.supervisor.os.kill",
               side_effect=ProcessLookupError()):
        await sup._terminate_embedder()

    assert sup._embedder_state == "unowned"


async def test_terminate_embedder_sigterm_permissionerror_stays_owned_terminating(
    stub_config: GaOTTTConfig,
):
    """f (B2-6): ``PermissionError`` on SIGTERM → state stays
    ``owned_terminating`` (manual recovery required), NOT reset to unowned."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    sup._embedder_state = "owned_idle"
    sup._embedder_pid = 12345

    with patch("gaottt.multiverse.supervisor.os.kill",
               side_effect=PermissionError()):
        await sup._terminate_embedder()

    assert sup._embedder_state == "owned_terminating"


async def test_terminate_embedder_sigkill_permissionerror_stays_owned_terminating(
    stub_config: GaOTTTConfig,
):
    """f (B2-6): SIGTERM times out, SIGKILL raises ``PermissionError`` →
    state stays ``owned_terminating``."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    sup._embedder_state = "owned_idle"
    sup._embedder_pid = 12345

    # os.kill: 1st call (SIGTERM) ok, 2nd call (SIGKILL) raises PermissionError
    kill_calls = []

    def kill_side_effect(pid, sig):
        kill_calls.append(sig)
        if sig == signal.SIGKILL:
            raise PermissionError()

    with patch("gaottt.multiverse.supervisor.os.kill",
               side_effect=kill_side_effect), \
         patch.object(_Supervisor, "_wait_for_pid_exit",
                      AsyncMock(return_value=False)):
        await sup._terminate_embedder()

    assert signal.SIGKILL in kill_calls  # SIGKILL was attempted
    assert sup._embedder_state == "owned_terminating"


async def test_terminate_embedder_survives_sigkill_stays_owned_terminating(
    stub_config: GaOTTTConfig,
):
    """f (B2-6): process survives SIGKILL (``_wait_for_pid_exit`` returns
    False) → state stays ``owned_terminating``."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    sup._embedder_state = "owned_idle"
    sup._embedder_pid = 12345

    # Both waits return False — process never exits
    with patch("gaottt.multiverse.supervisor.os.kill"), \
         patch.object(_Supervisor, "_wait_for_pid_exit",
                      AsyncMock(side_effect=[False, False])):
        await sup._terminate_embedder()

    assert sup._embedder_state == "owned_terminating"


# ---------------------------------------------------------------------------
# g. _embedder_idle_watchdog — idle-timeout → _terminate_embedder loop
# ---------------------------------------------------------------------------

async def test_idle_watchdog_returns_if_not_owned_idle(
    stub_config: GaOTTTConfig,
):
    """g: state is not ``owned_idle`` → the watchdog returns immediately
    without sleeping or terminating."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    sup._embedder_state = "unowned"

    terminate = AsyncMock()
    with patch.object(_Supervisor, "_terminate_embedder", terminate), \
         patch("gaottt.multiverse.supervisor.asyncio.sleep", AsyncMock()):
        await sup._embedder_idle_watchdog()

    terminate.assert_not_called()


async def test_idle_watchdog_terminates_on_idle_timeout_no_backends(
    stub_config: GaOTTTConfig,
):
    """g: idle timeout elapsed + no live backends → ``_terminate_embedder``
    is called and the watchdog exits."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    sup._embedder_state = "owned_idle"
    sup._last_backend_active_at = 0.0  # very old → idle definitely elapsed

    terminate = AsyncMock()
    with patch.object(_Supervisor, "_terminate_embedder", terminate), \
         patch.object(_Supervisor, "_has_tracked_live_backends",
                      return_value=False), \
         patch.object(_Supervisor, "_has_untracked_live_backends",
                      AsyncMock(return_value=False)), \
         patch("gaottt.multiverse.supervisor.asyncio.sleep", AsyncMock()):
        await sup._embedder_idle_watchdog()

    terminate.assert_awaited_once()


async def test_idle_watchdog_skips_terminate_if_tracked_backends_live(
    stub_config: GaOTTTConfig,
):
    """g: idle timeout elapsed but tracked backends are live → no terminate."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    sup._embedder_state = "owned_idle"
    sup._last_backend_active_at = 0.0

    terminate = AsyncMock()
    # Break the loop on the 2nd sleep so the test does not hang forever.
    calls = [0]

    async def fake_sleep(_):
        calls[0] += 1
        if calls[0] >= 2:
            raise asyncio.CancelledError()

    with patch.object(_Supervisor, "_terminate_embedder", terminate), \
         patch.object(_Supervisor, "_has_tracked_live_backends",
                      return_value=True), \
         patch("gaottt.multiverse.supervisor.asyncio.sleep",
               side_effect=fake_sleep):
        try:
            await sup._embedder_idle_watchdog()
        except asyncio.CancelledError:
            pass

    terminate.assert_not_called()


async def test_idle_watchdog_skips_if_not_yet_idle(stub_config: GaOTTTConfig):
    """g: idle timeout has NOT elapsed → no terminate."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    sup._embedder_state = "owned_idle"
    # _last_backend_active_at is recent (set in __init__), idle_timeout is
    # 300s by default → idle has not elapsed.

    terminate = AsyncMock()
    calls = [0]

    async def fake_sleep(_):
        calls[0] += 1
        if calls[0] >= 2:
            raise asyncio.CancelledError()

    with patch.object(_Supervisor, "_terminate_embedder", terminate), \
         patch("gaottt.multiverse.supervisor.asyncio.sleep",
               side_effect=fake_sleep):
        try:
            await sup._embedder_idle_watchdog()
        except asyncio.CancelledError:
            pass

    terminate.assert_not_called()


# ---------------------------------------------------------------------------
# h. _reap_dead_backend_pids — zombie / exited PID cleanup
# ---------------------------------------------------------------------------

def test_reap_dead_backend_pids_removes_exited_keeps_alive(
    stub_config: GaOTTTConfig,
):
    """h: ``_backend_pids`` entries whose process has exited are removed;
    live ones are kept."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    sup._backend_pids = {"u1": 100, "u2": 200}

    # pid 100 exited (done=100), pid 200 still running (done=0)
    def waitpid_side(pid, _flags):
        return (pid, 0) if pid == 100 else (0, 0)

    with patch("gaottt.multiverse.supervisor.os.waitpid",
               side_effect=waitpid_side):
        sup._reap_dead_backend_pids()

    assert "u1" not in sup._backend_pids
    assert "u2" in sup._backend_pids


def test_reap_dead_backend_pids_removes_childprocesserror(
    stub_config: GaOTTTConfig,
):
    """h: ``ChildProcessError`` (already reaped / not our child) → removed."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    sup._backend_pids = {"u1": 100}

    with patch("gaottt.multiverse.supervisor.os.waitpid",
               side_effect=ChildProcessError()):
        sup._reap_dead_backend_pids()

    assert "u1" not in sup._backend_pids


# ---------------------------------------------------------------------------
# i. _has_tracked_live_backends — any tracked PID alive?
# ---------------------------------------------------------------------------

def test_has_tracked_live_backends_true_when_any_alive(
    stub_config: GaOTTTConfig,
):
    """i: at least one tracked PID is alive → True."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    sup._backend_pids = {"u1": 100, "u2": 200}

    # pid 100 dead (done=100), pid 200 alive (done=0) → True
    def waitpid_side(pid, _flags):
        return (pid, 0) if pid == 100 else (0, 0)

    with patch("gaottt.multiverse.supervisor.os.waitpid",
               side_effect=waitpid_side):
        result = sup._has_tracked_live_backends()

    assert result is True


def test_has_tracked_live_backends_false_when_empty(
    stub_config: GaOTTTConfig,
):
    """i: no tracked PIDs → False."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    sup._backend_pids = {}

    result = sup._has_tracked_live_backends()
    assert result is False


def test_has_tracked_live_backends_false_when_all_dead(
    stub_config: GaOTTTConfig,
):
    """i: all tracked PIDs have exited → False."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    sup._backend_pids = {"u1": 100, "u2": 200}

    # both dead
    with patch("gaottt.multiverse.supervisor.os.waitpid",
               side_effect=[(100, 0), (200, 0)]):
        result = sup._has_tracked_live_backends()

    assert result is False


# ---------------------------------------------------------------------------
# j. _has_untracked_live_backends — registry active universe port probe
# ---------------------------------------------------------------------------

async def test_has_untracked_live_backends_true_when_untracked_responds(
    stub_config: GaOTTTConfig,
):
    """j: an active universe NOT in ``_backend_pids`` responds PROBE_OK → True."""
    from gaottt.multiverse.supervisor import _Supervisor, PROBE_OK

    registry = MagicMock()
    registry.list_universes = AsyncMock(return_value=[
        {"universe_id": "u1", "port": 8001, "status": "active"},
        {"universe_id": "u2", "port": 8002, "status": "active"},
    ])
    sup = _Supervisor(stub_config, registry)
    sup._backend_pids = {"u1": 100}  # u1 tracked, u2 untracked

    with patch.object(_Supervisor, "_load_token", return_value="tok"), \
         patch("gaottt.multiverse.supervisor._probe_backend_with_token",
               AsyncMock(return_value=PROBE_OK)):
        result = await sup._has_untracked_live_backends()

    assert result is True


async def test_has_untracked_live_backends_false_when_all_tracked(
    stub_config: GaOTTTConfig,
):
    """j: every active universe is tracked → no probe needed → False."""
    from gaottt.multiverse.supervisor import _Supervisor

    registry = MagicMock()
    registry.list_universes = AsyncMock(return_value=[
        {"universe_id": "u1", "port": 8001, "status": "active"},
    ])
    sup = _Supervisor(stub_config, registry)
    sup._backend_pids = {"u1": 100}  # all tracked

    with patch("gaottt.multiverse.supervisor._probe_backend_with_token",
               AsyncMock()) as probe:
        result = await sup._has_untracked_live_backends()

    assert result is False
    probe.assert_not_called()


async def test_has_untracked_live_backends_false_when_probe_down(
    stub_config: GaOTTTConfig,
):
    """j: untracked universe exists but probe returns PROBE_DOWN → False."""
    from gaottt.multiverse.supervisor import _Supervisor, PROBE_DOWN

    registry = MagicMock()
    registry.list_universes = AsyncMock(return_value=[
        {"universe_id": "u2", "port": 8002, "status": "active"},
    ])
    sup = _Supervisor(stub_config, registry)
    sup._backend_pids = {}  # u2 untracked

    with patch.object(_Supervisor, "_load_token", return_value="tok"), \
         patch("gaottt.multiverse.supervisor._probe_backend_with_token",
               AsyncMock(return_value=PROBE_DOWN)):
        result = await sup._has_untracked_live_backends()

    assert result is False


async def test_has_untracked_live_backends_skips_non_active(
    stub_config: GaOTTTConfig,
):
    """j: non-active universes (deleted / orphan) are never probed → False."""
    from gaottt.multiverse.supervisor import _Supervisor

    registry = MagicMock()
    registry.list_universes = AsyncMock(return_value=[
        {"universe_id": "u1", "port": 8001, "status": "deleted"},
        {"universe_id": "u2", "port": 8002, "status": "orphan"},
    ])
    sup = _Supervisor(stub_config, registry)
    sup._backend_pids = {}

    with patch("gaottt.multiverse.supervisor._probe_backend_with_token",
               AsyncMock()) as probe:
        result = await sup._has_untracked_live_backends()

    assert result is False
    probe.assert_not_called()


# ---------------------------------------------------------------------------
# k. _reset_embedder_state — basic fields (WP-2b minimal, regression guard)
#    + extended watchdog cancellation (WP-3b, RED)
# ---------------------------------------------------------------------------

def test_reset_embedder_state_clears_basic_fields(stub_config: GaOTTTConfig):
    """k: minimal reset clears state / pid / info_cache (WP-2b, passes now)."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    sup._embedder_state = "owned_idle"
    sup._embedder_pid = 12345
    sup._embedder_info_cache = {"model_name": "x"}

    sup._reset_embedder_state()

    assert sup._embedder_state == "unowned"
    assert sup._embedder_pid is None
    assert sup._embedder_info_cache is None


def test_reset_embedder_state_cancels_watchdog_task(
    stub_config: GaOTTTConfig,
):
    """k (WP-3b extension): when a watchdog task is registered,
    ``_reset_embedder_state`` cancels it and clears the task field, so a
    state reset does not leave an orphaned watchdog."""
    from gaottt.multiverse.supervisor import _Supervisor

    sup = _Supervisor(stub_config, MagicMock())
    watchdog = MagicMock()
    sup._embedder_watchdog_task = watchdog
    sup._embedder_state = "owned_idle"

    sup._reset_embedder_state()

    watchdog.cancel.assert_called_once()
    assert sup._embedder_watchdog_task is None


# ===========================================================================
# Codex final-review blocking fixes (B-F1 / B-F2 / B-F4)
# ===========================================================================
#
# B-F1: owned_terminating state must fail-fast on the NEXT ensure_embedder_up,
#       not silently recover via /healthz or spawn (B2-6 contract).
# B-F2: _spawn_embedder_detached must actually pass the sanitized env to
#       subprocess.Popen (not just compute it). Verifies GAOTTT_* strip
#       end-to-end through the Popen kwargs.
# B-F4: embedder_endpoint="" (config default) must NOT trigger lazy spawn
#       even when supervisor_spawn_embedder=True. The default stays inert.
# ===========================================================================


# ---------------------------------------------------------------------------
# B-F1 — owned_terminating ⇒ next ensure_embedder_up raises immediately
# ---------------------------------------------------------------------------

async def test_ensure_embedder_up_fails_fast_in_owned_terminating_state(
    stub_config: GaOTTTConfig,
):
    """B-F1: when ``_embedder_state == "owned_terminating"``, the next
    ``ensure_embedder_up`` MUST raise ``EmbedderValidationError`` BEFORE
    touching the cache, probing ``/healthz``, or attempting any spawn.

    B2-6 retains the ``owned_terminating`` state when ``_terminate_embedder``
    gives up (PermissionError / SIGKILL survivor) precisely so the next
    caller is forced into manual recovery. Without this fail-fast the
    supervisor would silently re-validate via ``/healthz`` and, if
    something answered, resume using an embedder it no longer tracks —
    exactly the ownership hazard B2-6 was designed to surface.

    The test installs mocks for BOTH observable recovery paths
    (``_validate_embedder`` and ``_probe_embedder_health``) and asserts
    they are NEVER awaited, proving the raise happens upstream of both.
    """
    from gaottt.multiverse.supervisor import (
        EmbedderValidationError,
        _Supervisor,
    )

    sup = _Supervisor(stub_config, MagicMock())
    # Pre-B2-6 termination left the state wedged.
    sup._embedder_state = "owned_terminating"
    sup._embedder_pid = 12345
    # A non-empty cache would tempt a buggy impl to trust it; populate it
    # so the test proves the raise happens regardless.
    sup._embedder_info_cache = {"model_name": "stale", "dimension": 1}

    # Mocks for the two recovery paths a buggy impl might take. If either
    # fires, the B-F1 fence is missing.
    validate = patch(
        "gaottt.multiverse.supervisor._validate_embedder",
        return_value={"model_name": "x", "dimension": 1},
    )
    health = patch.object(
        _Supervisor, "_probe_embedder_health", AsyncMock(return_value=True),
    )

    with pytest.raises(EmbedderValidationError, match="owned_terminating"), \
            validate as validate_mock, health as health_mock:
        await sup.ensure_embedder_up()

    validate_mock.assert_not_called()
    health_mock.assert_not_called()
    # State was not silently cleared.
    assert sup._embedder_state == "owned_terminating"


# ---------------------------------------------------------------------------
# B-F2 — sanitized env is actually handed to Popen (GAOTTT_* stripped)
# ---------------------------------------------------------------------------

def test_spawn_embedder_detached_passes_sanitized_env_to_popen(
    monkeypatch, tmp_path: Path, stub_config: GaOTTTConfig,
):
    """B-F2: when ``env`` is supplied, ``_spawn_embedder_detached`` puts it
    into ``Popen``'s kwargs as ``env=``, AND the value the supervisor
    builds (``_build_embedder_spawn_env``) has every ``GAOTTT_*`` key
    stripped so the supervisor's own data-dir / backend-token / owner-lease
    state cannot leak into the embedder subprocess.

    Pre-B-F2 ``_build_embedder_spawn_env`` already existed but was never
    threaded into ``Popen`` — the supervisor computed a sanitized env and
    then threw it away, inheriting ``os.environ`` verbatim. This test
    exercises the full path (supervisor helper → spawn function → Popen)
    so the security guarantee is verified end-to-end, not just at the
    helper level.
    """
    from gaottt.multiverse.supervisor import (
        _Supervisor,
        _spawn_embedder_detached,
    )

    # Supervisor's own GAOTTT_* — must NOT reach the embedder subprocess.
    monkeypatch.setenv("GAOTTT_DATA_DIR", "/supervisor/secret/data")
    monkeypatch.setenv("GAOTTT_BACKEND_TOKEN", "supervisor-leak-token")
    monkeypatch.setenv("GAOTTT_OWNER_LEASE_ENABLED", "true")
    # OS essentials the embedder needs to run — must be preserved.
    monkeypatch.setenv("PATH", "/custom/path/bin")
    monkeypatch.setenv("HOME", "/custom/home")

    sup = _Supervisor(stub_config, MagicMock())
    sanitized_env = sup._build_embedder_spawn_env()

    # Pre-condition: the helper itself strips GAOTTT_*. If this fails the
    # bug is in the helper, not the spawn plumbing.
    assert "GAOTTT_DATA_DIR" not in sanitized_env
    assert "GAOTTT_BACKEND_TOKEN" not in sanitized_env

    log_path = tmp_path / "logs" / "embedder.log"
    popen = MagicMock()
    popen.return_value.pid = 99999

    with patch("gaottt.multiverse.supervisor.subprocess.Popen", popen), \
            patch("gaottt.multiverse.supervisor.sys.platform", "linux"):
        pid = _spawn_embedder_detached(
            host="127.0.0.1", port=7879,
            log_path=log_path, model="ruri-v3-310m",
            env=sanitized_env,
        )

    # env made it into Popen's kwargs (the B-F2 fix).
    assert pid == 99999
    kwargs = popen.call_args.kwargs
    assert "env" in kwargs, "sanitized env was not passed to Popen"
    passed_env = kwargs["env"]

    # GAOTTT_* did not leak through to the subprocess.
    leaked = [k for k in passed_env if k.startswith("GAOTTT_")]
    assert not leaked, f"GAOTTT_* leaked into embedder env: {leaked}"
    assert passed_env.get("GAOTTT_DATA_DIR") != "/supervisor/secret/data"
    assert passed_env.get("GAOTTT_BACKEND_TOKEN") != "supervisor-leak-token"

    # OS essentials preserved (the strip is selective, not a blank env).
    assert passed_env.get("PATH") == "/custom/path/bin"
    assert passed_env.get("HOME") == "/custom/home"

    # The spawn detach shape is unchanged on Linux.
    assert kwargs.get("start_new_session") is True
    assert kwargs.get("stdin") is subprocess.DEVNULL


# ---------------------------------------------------------------------------
# B-F4 — embedder_endpoint="" ⇒ no lazy spawn (config default stays inert)
# ---------------------------------------------------------------------------

async def test_ensure_embedder_up_does_not_lazy_spawn_when_endpoint_empty(
    tmp_path: Path,
):
    """B-F4: with the config default ``embedder_endpoint=""`` AND
    ``supervisor_spawn_embedder=True``, ``ensure_embedder_up`` MUST raise
    ``EmbedderValidationError`` instead of falling through to the lazy-spawn
    path.

    Why this matters: ``_validate_embedder`` raises on an empty endpoint
    (correctly), but pre-B-F4 the except block only checked
    ``supervisor_spawn_embedder`` and then dropped into the spawn path.
    With the default ``supervisor_spawn_embedder=True`` that meant an
    unconfigured supervisor would try to spawn an embedder on a port
    derived from the empty URL (urlparse → ``127.0.0.1:7879``) — a
    surprising side effect for what the docs call a "feature off" default.
    B-F4 makes the empty-endpoint contract literal: no endpoint ⇒ no
    spawn, full stop.

    The test installs mocks for both downstream paths (lazy spawn +
    /healthz probe) and asserts neither fires.
    """
    from gaottt.multiverse.supervisor import (
        EmbedderValidationError,
        _Supervisor,
    )

    # Default config: endpoint unset, spawn ENABLED. The combination B-F4
    # targets — pre-fix this would spawn onto 127.0.0.1:7879.
    config = GaOTTTConfig(
        multiverse_root=str(tmp_path / "mv-empty-endpoint"),
        supervisor_admin_key="k",
        embedder_endpoint="",
        supervisor_spawn_embedder=True,
    )
    sup = _Supervisor(config, MagicMock())

    spawn = patch.object(
        _Supervisor, "_spawn_embedder_owned", AsyncMock(),
    )
    health = patch.object(
        _Supervisor, "_probe_embedder_health", AsyncMock(return_value=True),
    )

    with pytest.raises(EmbedderValidationError), \
            spawn as spawn_mock, health as health_mock:
        await sup.ensure_embedder_up()

    spawn_mock.assert_not_called()
    health_mock.assert_not_called()


# ---------------------------------------------------------------------------
# B-F4 (stale cache) — endpoint="" + non-empty _embedder_info_cache ⇒ no spawn
# ---------------------------------------------------------------------------

async def test_ensure_embedder_up_endpoint_empty_with_stale_cache_no_spawn(
    tmp_path: Path,
):
    """B-F4 (stale cache completion): with ``embedder_endpoint=""`` AND a
    non-empty ``_embedder_info_cache`` (e.g. left over from a prior
    supervisor run that cached an external embedder's /info),
    ``ensure_embedder_up`` MUST raise ``EmbedderValidationError`` without
    reaching the lazy-spawn path.

    Codex final-review round 2 flagged this gap: the original B-F4 fence
    lived inside the ``except EmbedderValidationError`` block, reachable
    only when ``_embedder_info_cache is None``. A stale cache skipped
    ``_validate_embedder`` entirely, so the endpoint-empty check was
    bypassed and the flow fell through to ``_spawn_embedder_owned`` —
    spawning onto a port derived from an empty URL. The fix hoists the
    endpoint-empty check to the top of the spawn lock (inside
    ``_embedder_spawn_lock``, after the B-F1 ``owned_terminating``
    fail-fast) so cache state cannot gate it.

    The test does NOT mock ``_probe_embedder_health``: the real
    implementation returns ``False`` for an empty endpoint without any
    network call, which is exactly the path a buggy impl would take to
    reach spawn. With the fix the early check raises before any health
    probe.
    """
    from gaottt.multiverse.supervisor import (
        EmbedderValidationError,
        _Supervisor,
    )

    config = GaOTTTConfig(
        multiverse_root=str(tmp_path / "mv-empty-endpoint-stale"),
        supervisor_admin_key="k",
        embedder_endpoint="",
        supervisor_spawn_embedder=True,
    )
    sup = _Supervisor(config, MagicMock())
    # Stale cache that lets a buggy impl skip _validate_embedder and drop
    # straight into the unowned-cache-freshness → spawn path.
    sup._embedder_info_cache = {"model_name": "stale", "dimension": 1}

    spawn = patch.object(
        _Supervisor, "_spawn_embedder_owned", AsyncMock(),
    )

    with pytest.raises(EmbedderValidationError), spawn as spawn_mock:
        await sup.ensure_embedder_up()

    spawn_mock.assert_not_called()
