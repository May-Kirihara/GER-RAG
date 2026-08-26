"""WP-4 — shim supervisor mode for mcp_proxy.

Verifies that ``--supervisor-url`` makes the proxy resolve its backend via the
supervisor's ``POST /route`` (returning ``{url, token}``) and attach the token
as a ``Bearer`` header to the upstream MCP session, instead of auto-spawning.
The default (no ``supervisor_url``) path is asserted to be byte-identical to
the legacy ``_ensure_backend`` route. No real process or network is used —
``httpx.post``, ``streamablehttp_client``, ``ClientSession``, ``_proxy_session``
and ``_ensure_backend`` are mocked.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

import httpx
import pytest

PROXY = "gaottt.server.mcp_proxy"


# ---------------------------------------------------------------------------
# _route_to_supervisor
# ---------------------------------------------------------------------------

def test_route_to_supervisor_success():
    """200 with {url, token} → (url, token). POST body is {api_key}."""
    from gaottt.server.mcp_proxy import (
        PROXY_ROUTE_TIMEOUT_SECONDS, _route_to_supervisor,
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "url": "http://127.0.0.1:7890/mcp",
        "token": "abc",
    }
    with patch(f"{PROXY}.httpx.post", return_value=mock_response) as mock_post:
        url, token = _route_to_supervisor("http://sup:7880", "mykey")

    assert url == "http://127.0.0.1:7890/mcp"
    assert token == "abc"
    mock_post.assert_called_once_with(
        "http://sup:7880/route",
        json={"api_key": "mykey"},
        timeout=PROXY_ROUTE_TIMEOUT_SECONDS,
    )


def test_route_to_supervisor_401():
    """401 → RuntimeError('Invalid API key')."""
    from gaottt.server.mcp_proxy import _route_to_supervisor

    mock_response = MagicMock()
    mock_response.status_code = 401
    with patch(f"{PROXY}.httpx.post", return_value=mock_response):
        with pytest.raises(RuntimeError, match="Invalid API key"):
            _route_to_supervisor("http://sup:7880", "bad")


def test_route_to_supervisor_500():
    """Non-200 (non-401) → RuntimeError('Supervisor route failed: <status>')."""
    from gaottt.server.mcp_proxy import _route_to_supervisor

    mock_response = MagicMock()
    mock_response.status_code = 500
    with patch(f"{PROXY}.httpx.post", return_value=mock_response):
        with pytest.raises(RuntimeError, match=r"Supervisor route failed"):
            _route_to_supervisor("http://sup:7880", "k")


def test_route_to_supervisor_connect_error():
    """Transport error → RuntimeError (supervisor unreachable)."""
    from gaottt.server.mcp_proxy import _route_to_supervisor

    with patch(
        f"{PROXY}.httpx.post",
        side_effect=httpx.ConnectError("Connection refused"),
    ):
        with pytest.raises(RuntimeError, match=r"Supervisor route failed"):
            _route_to_supervisor("http://sup:7880", "k")


@pytest.mark.asyncio
async def test_autospawn_starts_missing_local_supervisor():
    """Connection refusal + opt-in starts one supervisor then routes."""
    import gaottt.server.mcp_proxy as mod

    ready = MagicMock(status_code=200)
    with patch(
        f"{PROXY}._route_to_supervisor",
        side_effect=[mod._SupervisorUnreachable("down"), ("http://b/mcp", "tok")],
    ) as route, patch(
        "gaottt.config.GaOTTTConfig.from_config_file",
        return_value=SimpleNamespace(
            multiverse_root="/tmp/multiverse",
            supervisor_admin_key="admin",
        ),
    ), patch(f"{PROXY}._spawn_supervisor_detached", return_value=123) as spawn, \
            patch(f"{PROXY}.httpx.get", return_value=ready):
        result = await mod._route_with_supervisor_autospawn(
            "http://127.0.0.1:7880", "key", enabled=True,
        )

    assert result == ("http://b/mcp", "tok")
    spawn.assert_called_once()
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_autospawn_rejects_remote_supervisor():
    """Auto-spawn never turns a remote routing failure into a local process."""
    import gaottt.server.mcp_proxy as mod

    with patch(
        f"{PROXY}._route_to_supervisor",
        side_effect=mod._SupervisorUnreachable("down"),
    ), patch(f"{PROXY}._spawn_supervisor_detached") as spawn:
        with pytest.raises(RuntimeError, match="local http URL"):
            await mod._route_with_supervisor_autospawn(
                "https://example.test:7880", "key", enabled=True,
            )
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_autospawn_disabled_preserves_unreachable_error():
    """The new behaviour stays opt-in."""
    import gaottt.server.mcp_proxy as mod

    with patch(
        f"{PROXY}._route_to_supervisor",
        side_effect=mod._SupervisorUnreachable("down"),
    ), patch(f"{PROXY}._spawn_supervisor_detached") as spawn:
        with pytest.raises(mod._SupervisorUnreachable):
            await mod._route_with_supervisor_autospawn(
                "http://127.0.0.1:7880", "key", enabled=False,
            )
    spawn.assert_not_called()


# ---------------------------------------------------------------------------
# _Upstream._auth_headers
# ---------------------------------------------------------------------------

def _make_upstream(**overrides):
    from gaottt.server.mcp_proxy import _Upstream

    kwargs = dict(
        url="http://127.0.0.1:7890/mcp",
        host="127.0.0.1",
        port=7890,
        idle_timeout=300.0,
        spawn_log_path=Path("/tmp/dummy.log"),
        serialize=True,
        auto_reconnect=True,
        instructions_override=None,
    )
    kwargs.update(overrides)
    return _Upstream(**kwargs)


def test_upstream_auth_headers_with_token():
    """token set → {'Authorization': 'Bearer <token>'}."""
    up = _make_upstream(token="abc")
    assert up._auth_headers() == {"Authorization": "Bearer abc"}


def test_upstream_auth_headers_without_token():
    """token None → {} (legacy no-auth path)."""
    up = _make_upstream(token=None)
    assert up._auth_headers() == {}


# ---------------------------------------------------------------------------
# _Upstream.connect forwards auth headers to streamablehttp_client
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upstream_connect_passes_token_headers():
    """connect() must pass the Bearer header into streamablehttp_client."""
    import gaottt.server.mcp_proxy as mod

    up = _make_upstream(token="abc")

    captured: list[tuple] = []

    @contextlib.asynccontextmanager
    async def fake_client(url, headers=None, **_kw):
        captured.append((url, headers))
        yield (MagicMock(), MagicMock(), MagicMock())

    class FakeSession:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            sess = MagicMock()
            sess.initialize = AsyncMock(return_value=MagicMock(instructions="x"))
            return sess

        async def __aexit__(self, *a):
            return None

    with patch.object(mod, "streamablehttp_client", fake_client), \
            patch.object(mod, "ClientSession", FakeSession):
        await up.connect()

    assert captured == [
        ("http://127.0.0.1:7890/mcp", {"Authorization": "Bearer abc"}),
    ]
    await up.aclose()


@pytest.mark.asyncio
async def test_upstream_connect_no_token_passes_none_headers():
    """No token → connect() passes headers=None (byte-identical to legacy)."""
    import gaottt.server.mcp_proxy as mod

    up = _make_upstream(token=None)

    captured: list = []

    @contextlib.asynccontextmanager
    async def fake_client(url, headers=None, **_kw):
        captured.append(headers)
        yield (MagicMock(), MagicMock(), MagicMock())

    class FakeSession:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            sess = MagicMock()
            sess.initialize = AsyncMock(return_value=MagicMock(instructions="x"))
            return sess

        async def __aexit__(self, *a):
            return None

    with patch.object(mod, "streamablehttp_client", fake_client), \
            patch.object(mod, "ClientSession", FakeSession):
        await up.connect()

    # None (not {}) so the call is identical to the pre-WP4 streamablehttp_client(url).
    assert captured == [None]
    await up.aclose()


# ---------------------------------------------------------------------------
# _Upstream._reconnect_locked — re-route vs re-ensure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upstream_reconnect_reroutes_in_supervisor_mode():
    """Supervisor mode: reconnect re-resolves via /route (no auto-spawn)."""
    up = _make_upstream(
        token="oldtok",
        supervisor_url="http://sup:7880",
        api_key="key",
    )
    up.aclose = AsyncMock()
    up.connect = AsyncMock()

    with patch(
        f"{PROXY}._route_to_supervisor",
        return_value=("http://new:7900/mcp", "newtok"),
    ) as mock_route, patch(
        f"{PROXY}._ensure_backend", new_callable=AsyncMock,
    ) as mock_ensure:
        await up._reconnect_locked()

    mock_route.assert_called_once_with("http://sup:7880", "key")
    mock_ensure.assert_not_called()
    assert up.url == "http://new:7900/mcp"
    assert up._token == "newtok"


@pytest.mark.asyncio
async def test_upstream_reconnect_legacy_uses_ensure_backend():
    """Legacy mode: reconnect re-runs _ensure_backend (default 不変)."""
    up = _make_upstream(token=None)
    up.aclose = AsyncMock()
    up.connect = AsyncMock()

    with patch(f"{PROXY}._route_to_supervisor") as mock_route, patch(
        f"{PROXY}._ensure_backend",
        new_callable=AsyncMock,
        return_value="http://127.0.0.1:7878/mcp",
    ) as mock_ensure:
        await up._reconnect_locked()

    mock_route.assert_not_called()
    mock_ensure.assert_called_once()
    assert up.url == "http://127.0.0.1:7878/mcp"


# ---------------------------------------------------------------------------
# run_proxy — supervisor vs legacy dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_proxy_supervisor_mode_routes_first():
    """supervisor_url set → /route resolves url+token, _ensure_backend NOT called."""
    import gaottt.server.mcp_proxy as mod

    with patch(
        f"{PROXY}._route_to_supervisor",
        return_value=("http://1.2.3.4:7890/mcp", "tok"),
    ) as mock_route, patch(
        f"{PROXY}._proxy_session", new_callable=AsyncMock,
    ) as mock_session, patch(
        f"{PROXY}._ensure_backend", new_callable=AsyncMock,
    ) as mock_ensure:
        await mod.run_proxy(
            host="127.0.0.1",
            port=7878,
            idle_timeout=300.0,
            ping_interval=60.0,
            supervisor_url="http://sup:7880",
            api_key="key",
        )

    mock_route.assert_called_once_with("http://sup:7880", "key")
    mock_ensure.assert_not_called()
    args, kwargs = mock_session.call_args
    # first positional arg is the routed url
    assert args[0] == "http://1.2.3.4:7890/mcp"
    assert kwargs.get("token") == "tok"
    assert kwargs.get("supervisor_url") == "http://sup:7880"
    assert kwargs.get("api_key") == "key"


@pytest.mark.asyncio
async def test_run_proxy_legacy_mode_uses_ensure_backend():
    """No supervisor_url → legacy _ensure_backend path (default 不変)."""
    import gaottt.server.mcp_proxy as mod

    with patch(f"{PROXY}._route_to_supervisor") as mock_route, patch(
        f"{PROXY}._proxy_session", new_callable=AsyncMock,
    ) as mock_session, patch(
        f"{PROXY}._ensure_backend",
        new_callable=AsyncMock,
        return_value="http://127.0.0.1:7878/mcp",
    ) as mock_ensure:
        await mod.run_proxy(
            host="127.0.0.1",
            port=7878,
            idle_timeout=300.0,
            ping_interval=60.0,
        )

    mock_route.assert_not_called()
    mock_ensure.assert_called_once()
    args, kwargs = mock_session.call_args
    assert args[0] == "http://127.0.0.1:7878/mcp"
    assert not kwargs.get("supervisor_url")
    assert not kwargs.get("token")


@pytest.mark.asyncio
async def test_run_proxy_supervisor_mode_without_api_key_errors():
    """supervisor_url set but empty api_key → fail fast."""
    import gaottt.server.mcp_proxy as mod

    with patch(f"{PROXY}._route_to_supervisor") as mock_route, patch(
        f"{PROXY}._proxy_session", new_callable=AsyncMock,
    ), patch(f"{PROXY}._ensure_backend", new_callable=AsyncMock):
        with pytest.raises(RuntimeError, match=r"(?i)api_key|GAOTTT_API_KEY"):
            await mod.run_proxy(
                host="127.0.0.1",
                port=7878,
                idle_timeout=300.0,
                ping_interval=60.0,
                supervisor_url="http://sup:7880",
                api_key="",
            )
    mock_route.assert_not_called()
