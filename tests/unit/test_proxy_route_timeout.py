"""WP-8 (Phase U final review) — proxy /route timeout と supervisor 契約の整合 fence。

Codex final review blocking #1 (round-2): proxy shim の /route HTTP timeout が
supervisor 側の正当な待ち時間より短いと、cold client は shim 側で先に
timeout し ``readiness:"starting"`` 応答を受け取れない。完全 cold の
POST /route は 3 stage を直列に消費し得る:

  (a) embedder lazy-spawn readiness  ``embedder_spawn_readiness_timeout_seconds``
  (b) fresh backend spawn probe      ``supervisor_readiness_timeout``
  (c) engine readiness poll          ``route_readiness_timeout_seconds``

本 module は両側の値を import して結合の不変量を検証する — どちらかの
bound を釣り上げても shim timeout が追従しなければ fail (coherence fence)。
auto-spawn 経路の budget 分離 (supervisor 起動 poll と /route 本体は別
budget) も fence する。
"""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from gaottt.config import GaOTTTConfig

PROXY = "gaottt.server.mcp_proxy"


def _field_default(name: str) -> float:
    """GaOTTTConfig の code default を定義 site から直接読む。

    ``GaOTTTConfig()`` の instantiation は ``__post_init__`` での env 読み
    / data_dir 解決を伴うため、``dataclasses.fields`` から class 定義時の
    pure default を読む (test 実行環境の GAOTTT_* env に左右されない)。
    """
    for f in dataclasses.fields(GaOTTTConfig):
        if f.name == name:
            return float(f.default)
    raise AssertionError(f"GaOTTTConfig has no field named {name!r}")


def test_route_timeout_covers_full_cold_route_bounds():
    """shim の /route timeout は完全 cold route の 3 stage bound の和以上。

    supervisor は冷えた /route で (a) embedder lazy-spawn readiness probe に
    最大 ``embedder_spawn_readiness_timeout_seconds``、(b) fresh spawn
    backend の readiness probe に最大 ``supervisor_readiness_timeout``、
    (c) engine readiness poll に最大 ``route_readiness_timeout_seconds``
    を使い得る (round-2 review: 旧 130s fence は (a) を見落としていた)。
    どれか 1 つでも bound が constant を上回れば fail — 定数を hardcode に
    戻しても (130 < 215 で) fail。
    """
    from gaottt.server.mcp_proxy import PROXY_ROUTE_TIMEOUT_SECONDS

    embedder = _field_default("embedder_spawn_readiness_timeout_seconds")
    spawn = _field_default("supervisor_readiness_timeout")
    readiness = _field_default("route_readiness_timeout_seconds")
    total = embedder + spawn + readiness
    assert PROXY_ROUTE_TIMEOUT_SECONDS >= total, (
        f"PROXY_ROUTE_TIMEOUT_SECONDS={PROXY_ROUTE_TIMEOUT_SECONDS} must cover "
        f"embedder_spawn_readiness_timeout_seconds({embedder}) + "
        f"supervisor_readiness_timeout({spawn}) + "
        f"route_readiness_timeout_seconds({readiness}) = {total}"
    )


@pytest.mark.asyncio
async def test_autospawn_gives_route_full_bound_after_slow_supervisor_start():
    """auto-spawn 経路の budget 分離 (round-2 review blocking #1)。

    supervisor 起動 poll (/openapi.json) に時間が掛かっても、起動後の
    /route には「起動 budget の残り」ではなく full
    ``PROXY_ROUTE_TIMEOUT_SECONDS`` を渡す — 完全 cold /route は起動完了
    後にさらに embedder spawn + backend spawn + readiness poll を消費し
    得るため。旧実装の ``deadline - now`` 流用は supervisor 起動 budget
    と route budget を conflate し cold path を頭打ちにする。
    """
    import gaottt.server.mcp_proxy as mod

    ready = MagicMock(status_code=200)
    polls: list[str] = []

    def openapi_poll(url, timeout=None):
        polls.append(url)
        if len(polls) < 3:  # supervisor 起動に 2 poll 失敗 (= 時間経過を模擬)
            raise httpx.ConnectError("starting up")
        return ready

    with patch(
        f"{PROXY}._route_to_supervisor",
        side_effect=[mod._SupervisorUnreachable("down"), ("http://b/mcp", "tok")],
    ) as route, patch(
        "gaottt.config.GaOTTTConfig.from_config_file",
        return_value=SimpleNamespace(
            multiverse_root="/tmp/multiverse",
            supervisor_admin_key="admin",
        ),
    ), patch(f"{PROXY}._spawn_supervisor_detached", return_value=123), \
            patch(f"{PROXY}.httpx.get", side_effect=openapi_poll):
        result = await mod._route_with_supervisor_autospawn(
            "http://127.0.0.1:7880", "key", enabled=True,
        )

    assert result == ("http://b/mcp", "tok")
    assert len(polls) == 3, "poll loop must observe the slow start"
    # 起動後の /route は 1 回だけ (重複 route request で token rotation しない)
    assert route.call_count == 2
    assert route.call_args.kwargs["timeout"] == mod.PROXY_ROUTE_TIMEOUT_SECONDS, (
        "post-startup /route must get the FULL route bound, not the "
        "supervisor-startup deadline remainder"
    )


def test_route_to_supervisor_uses_constant_by_default():
    """``_route_to_supervisor`` は明示 override 無しに定数を timeout に使う
    (短い固定値への退化 fence)。"""
    from gaottt.server.mcp_proxy import (
        PROXY_ROUTE_TIMEOUT_SECONDS, _route_to_supervisor,
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"url": "http://b/mcp", "token": "t"}
    with patch(f"{PROXY}.httpx.post", return_value=mock_response) as mock_post:
        _route_to_supervisor("http://sup:7880", "key")

    assert mock_post.call_args.kwargs["timeout"] == PROXY_ROUTE_TIMEOUT_SECONDS


def test_route_to_supervisor_explicit_timeout_still_honored():
    """明示 timeout 指定は従来どおり尊重される (関数の一般契約)。"""
    from gaottt.server.mcp_proxy import _route_to_supervisor

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"url": "http://b/mcp", "token": "t"}
    with patch(f"{PROXY}.httpx.post", return_value=mock_response) as mock_post:
        _route_to_supervisor("http://sup:7880", "key", timeout=42.0)

    assert mock_post.call_args.kwargs["timeout"] == 42.0
