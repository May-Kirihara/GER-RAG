"""Unit tests for the GAOTTT_BACKEND_TOKEN auth middleware.

The middleware lives in ``gaottt.server.mcp_server`` and is split into:

* ``build_token_middleware(expected_token)`` — pure factory returning a
  ``BaseHTTPMiddleware`` subclass. This is what the behaviour tests exercise
  against a minimal Starlette app (no FastMCP / engine needed).
* ``_install_token_middleware()`` — env-driven installer that monkey-patches
  ``mcp.streamable_http_app`` / ``mcp.sse_app`` exactly like
  ``_install_idle_watcher`` does.

Design invariant under test: the token middleware is installed *after* the
idle-watcher's ActivityMiddleware, which (because Starlette's last-added
middleware is the outermost) makes it the outer layer. A 401 therefore never
reaches ``call_next`` and cannot refresh the idle watcher's activity clock.
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from gaottt.server.mcp_server import (
    _install_token_middleware,
    build_token_middleware,
)
from gaottt.server.mcp_server import mcp as mcp_instance


# --- helpers ---------------------------------------------------------------


def _always_ok(_request):
    return JSONResponse({"ok": True})


def _make_app(expected_token: str):
    """Minimal Starlette app guarded by the token middleware.

    The middleware is the last ``add_middleware`` call so it is the
    outermost layer (mirrors how ``_install_token_middleware`` stacks it
    above ActivityMiddleware in production).
    """
    app = Starlette(routes=[Route("/", _always_ok)])
    app.add_middleware(build_token_middleware(expected_token))
    return app


def _make_app_with_activity_recorder(expected_token: str):
    """App whose token middleware sits *outside* a test-double that mirrors
    ActivityMiddleware's activity refresh. Used to verify the ordering
    invariant: a 401 must not reach the inner layer.
    """
    activity = {"refreshed": False}

    class ActivityRecorder(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            activity["refreshed"] = True
            return await call_next(request)

    app = Starlette(routes=[Route("/", _always_ok)])
    app.add_middleware(ActivityRecorder)  # inner (added first)
    app.add_middleware(build_token_middleware(expected_token))  # outer
    return app, activity


# --- env-driven installer: default-off (case 1) ---------------------------


@pytest.fixture
def clean_mcp_factories():
    """Ensure the mcp factory attributes start at their class-defined
    (unpatched) state and are restored afterwards.

    ``_install_token_middleware`` (like ``_install_idle_watcher``) shadows
    the class method by assigning an instance attribute of the same name.
    A bound method is a fresh object on every access, so ``is`` comparisons
    are unreliable — instead we detect patching via instance ``__dict__``
    membership (``vars``).
    """
    inst = mcp_instance
    saved = {
        name: vars(inst)[name]
        for name in ("streamable_http_app", "sse_app")
        if name in vars(inst)
    }
    for name in saved:
        delattr(inst, name)
    yield inst
    # Drop whatever shadows the tests created, then restore prior shadows.
    for name in ("streamable_http_app", "sse_app"):
        if name in vars(inst):
            delattr(inst, name)
    for name, val in saved.items():
        setattr(inst, name, val)  # type: ignore[method-assign]


class TestInstallerDefaultOff:
    def test_env_unset_does_not_patch_factories(self, monkeypatch, clean_mcp_factories):
        monkeypatch.delenv("GAOTTT_BACKEND_TOKEN", raising=False)
        _install_token_middleware()
        # No instance-attribute shadow = the class method is still in effect =
        # default pass-through posture, nothing installed.
        assert "streamable_http_app" not in vars(clean_mcp_factories)
        assert "sse_app" not in vars(clean_mcp_factories)

    def test_env_empty_string_does_not_patch_factories(
        self, monkeypatch, clean_mcp_factories
    ):
        monkeypatch.setenv("GAOTTT_BACKEND_TOKEN", "")
        _install_token_middleware()
        assert "streamable_http_app" not in vars(clean_mcp_factories)

    def test_env_set_patches_both_factories(self, monkeypatch, clean_mcp_factories):
        monkeypatch.setenv("GAOTTT_BACKEND_TOKEN", "s3cret")
        _install_token_middleware()
        assert "streamable_http_app" in vars(clean_mcp_factories)
        assert "sse_app" in vars(clean_mcp_factories)


# --- middleware behaviour (cases 2-7) --------------------------------------


class TestTokenMiddlewareBehaviour:
    def test_valid_bearer_passes_through(self):
        client = TestClient(_make_app("s3cret"))
        resp = client.get("/", headers={"Authorization": "Bearer s3cret"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_wrong_bearer_rejected(self):
        client = TestClient(_make_app("s3cret"))
        resp = client.get("/", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Unauthorized"}

    def test_missing_authorization_header_rejected(self):
        client = TestClient(_make_app("s3cret"))
        resp = client.get("/")
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Unauthorized"}

    def test_bearer_keyword_only_no_token_rejected(self):
        client = TestClient(_make_app("s3cret"))
        # "Bearer" with no trailing token.
        resp = client.get("/", headers={"Authorization": "Bearer"})
        assert resp.status_code == 401
        # "Bearer " with trailing whitespace but empty token.
        resp2 = client.get("/", headers={"Authorization": "Bearer "})
        assert resp2.status_code == 401

    def test_non_bearer_scheme_rejected(self):
        client = TestClient(_make_app("s3cret"))
        resp = client.get("/", headers={"Authorization": "Basic s3cret"})
        assert resp.status_code == 401

    def test_multiple_tokens_treated_as_malformed(self):
        client = TestClient(_make_app("s3cret"))
        resp = client.get("/", headers={"Authorization": "Bearer xxx yyy"})
        assert resp.status_code == 401

    def test_empty_presented_token_does_not_crash(self):
        # Guards compare_digest against pathological inputs and confirms a
        # length mismatch still yields 401 rather than an exception.
        client = TestClient(_make_app("nonemptytoken"))
        resp = client.get("/", headers={"Authorization": "Bearer "})
        assert resp.status_code == 401


# --- middleware ordering (case 8) ------------------------------------------


class TestMiddlewareOrdering:
    def test_unauthorized_request_does_not_refresh_activity(self):
        app, activity = _make_app_with_activity_recorder("s3cret")
        client = TestClient(app)
        resp = client.get("/", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401
        # The 401 short-circuited before the inner ActivityRecorder ran,
        # so brute-force attempts cannot keep a sleeping backend alive.
        assert activity["refreshed"] is False

    def test_authorized_request_does_refresh_activity(self):
        app, activity = _make_app_with_activity_recorder("s3cret")
        client = TestClient(app)
        resp = client.get("/", headers={"Authorization": "Bearer s3cret"})
        assert resp.status_code == 200
        assert activity["refreshed"] is True


# --- timing-safe comparison guard -----------------------------------------


class TestUsesSecretsCompareDigest:
    """Functional guard: the middleware must actually *invoke*
    ``secrets.compare_digest`` at request time, not merely import it.

    A source-level ``"compare_digest" in source`` check would pass even if the
    symbol were imported but unused. Instead we spy on the real callable
    (``wraps`` delegates to the genuine implementation so behaviour is
    unchanged) and assert it was called for every well-formed Bearer request —
    both the accepted and the rejected one. Malformed requests (no Bearer
    scheme) short-circuit earlier and are expected NOT to reach the comparison.
    """

    def test_valid_token_invokes_compare_digest(self):
        import secrets as secrets_mod
        from unittest.mock import patch

        client = TestClient(_make_app("s3cret"))
        with patch(
            "gaottt.server.mcp_server.secrets.compare_digest",
            wraps=secrets_mod.compare_digest,
        ) as spy:
            resp = client.get("/", headers={"Authorization": "Bearer s3cret"})
        assert resp.status_code == 200
        spy.assert_called_once_with("s3cret", "s3cret")

    def test_wrong_token_invokes_compare_digest_before_401(self):
        import secrets as secrets_mod
        from unittest.mock import patch

        client = TestClient(_make_app("s3cret"))
        with patch(
            "gaottt.server.mcp_server.secrets.compare_digest",
            wraps=secrets_mod.compare_digest,
        ) as spy:
            resp = client.get("/", headers={"Authorization": "Bearer wrong"})
        # The 401 must come from compare_digest returning False, proving the
        # timing-safe path — not a structural rejection — produced the denial.
        assert resp.status_code == 401
        spy.assert_called_once_with("wrong", "s3cret")

    def test_malformed_request_does_not_reach_compare_digest(self):
        import secrets as secrets_mod
        from unittest.mock import patch

        client = TestClient(_make_app("s3cret"))
        with patch(
            "gaottt.server.mcp_server.secrets.compare_digest",
            wraps=secrets_mod.compare_digest,
        ) as spy:
            resp = client.get("/", headers={"Authorization": "Bearer xxx yyy"})
        assert resp.status_code == 401
        # Collapsed to a malformed shape before any comparison — the guard
        # against multi-token inputs stays structural, not timing-sensitive.
        spy.assert_not_called()
