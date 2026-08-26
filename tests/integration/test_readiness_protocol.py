"""Phase U WP-6b — staged readiness protocol の統合 test。

R5 (cold start 129s) 対策の観測・待機機構を検証する:

1. config knob — ``readiness_protocol_enabled`` (default True, env rollback
   ``GAOTTT_READINESS_PROTOCOL_ENABLED=0``) / ``readiness_wait_timeout_seconds``
   (30.0) / supervisor 側 ``route_readiness_timeout_seconds`` (35.0)。
2. ``GET /admin/readiness`` — state mapping (STARTING → SEMANTIC_READY →
   HYBRID_READY, bm25 failed → HYBRID_READY + bm25:"failed", startup 失敗 →
   FAILED + error) / token auth / timings の存在。
3. MCP handler の bounded wait — 遅い startup は timeout 内なら待って成功、
   超過なら構造化 retryable error、並行初回 call は単一 startup task を共有、
   rollback (flag OFF) は legacy lazy path bit-for-bit。
4. engine の生存期間 — transport (Starlette app lifespan) 起動で eager start、
   停止で cancel + bounded cleanup。per-session lifespan は flag ON では
   engine を tear down しない (warm reconnect で再構築しない)。
5. diagnostics Tier B — background build 中は bm25 size check を WARN から
   INFO (pending) に格下げ、state ready では従来どおり検査する。
6. supervisor /route — readiness poll (STARTING 待ち / deadline 超過は
   readiness:"starting" 付き / FAILED は 503 / 旧 backend 404 は legacy
   fallback)。proxy shim は readiness:"starting" を INFO log して接続続行。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from mcp.server.fastmcp.exceptions import ToolError

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine
from gaottt.index.bm25_index import BM25Index
from gaottt.index.faiss_index import FaissIndex
from gaottt.server import mcp_server as srv
from gaottt.store.cache import CacheLayer
from gaottt.store.sqlite_store import SqliteStore
from tests.integration.test_engine_archive_ttl import StubEmbedder

# ---------------------------------------------------------------------------
# engine / config helpers (test_mcp_tools.py / test_bm25_background_build.py
# と同一構成 — StubEmbedder + 決定論的 config)
# ---------------------------------------------------------------------------


def _make_config(tmp_path, **overrides) -> GaOTTTConfig:
    defaults = dict(
        embedding_dim=32,
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "t.db"),
        faiss_index_path=str(tmp_path / "t.faiss"),
        flush_interval_seconds=999.0,
        dream_enabled=False,
        faiss_save_interval_seconds=0.0,
        virtual_faiss_save_interval_seconds=0.0,
        # determinism: 同期 build + snapshot 無効 (WP-6c/6d 専用 suite参照)
        bm25_background_build_enabled=False,
        bm25_snapshot_enabled=False,
    )
    defaults.update(overrides)
    return GaOTTTConfig(**defaults)


def _make_engine(tmp_path, *, background: bool = False, **cfg_overrides):
    config = _make_config(
        tmp_path,
        bm25_background_build_enabled=background,
        ambient_gate_tokenizer="trigram",
        **cfg_overrides,
    )
    return GaOTTTEngine(
        config=config,
        embedder=StubEmbedder(dimension=32),
        faiss_index=FaissIndex(dimension=32),
        cache=CacheLayer(flush_interval=999.0),
        store=SqliteStore(db_path=config.db_path),
        virtual_faiss_index=FaissIndex(dimension=32),
        bm25_index=BM25Index(
            k1=config.bm25_k1, b=config.bm25_b, tokenizer=config.bm25_tokenizer,
        ),
        ambient_gate_index=BM25Index(tokenizer="trigram"),
    )


def _tracked_engine(eng: GaOTTTEngine, *, delay: float = 0.0,
                    error: Exception | None = None):
    """startup/shutdown を計測・遅延・失敗注入できるよう instance wrap する。"""
    calls = {"startup": 0, "shutdown": 0}
    orig_startup = eng.startup
    orig_shutdown = eng.shutdown

    async def tracked_startup() -> None:
        calls["startup"] += 1
        if delay:
            await asyncio.sleep(delay)
        if error is not None:
            raise error
        await orig_startup()

    async def tracked_shutdown() -> None:
        calls["shutdown"] += 1
        await orig_shutdown()

    eng.startup = tracked_startup  # type: ignore[method-assign]
    eng.shutdown = tracked_shutdown  # type: ignore[method-assign]
    return calls


@pytest.fixture(autouse=True)
def _reset_server_state():
    """module singleton (engine / readiness state) の保存・復元。

    複数 test が ``srv._engine`` / ``srv._readiness`` を直接操作するため、
    test 間での汚染を防ぐ (既存 test は ``srv._engine`` を monkeypatch で
    差し替えている — ここでは readiness 分も含めて完全 snapshot)。
    """
    saved_engine = srv._engine
    st = srv._readiness
    saved = (
        st.enabled, st.wait_timeout, st.task, st.error,
        st.started_at, st.finished_at, st.route_installed,
    )
    st.enabled = True
    st.wait_timeout = 30.0
    st.task = None
    st.error = None
    st.started_at = None
    st.finished_at = None
    st.route_installed = False
    try:
        yield
    finally:
        srv._engine = saved_engine
        (
            st.enabled, st.wait_timeout, st.task, st.error,
            st.started_at, st.finished_at, st.route_installed,
        ) = saved


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """get_engine 内の from_config_file を tmp 隔離 config に差し替える
    (本 env の data_dir 副作用を避ける)。"""
    cfg = _make_config(tmp_path / "iso")
    monkeypatch.setattr(
        srv.GaOTTTConfig, "from_config_file",
        classmethod(lambda cls, _cfg=cfg: _cfg),
    )
    return cfg


def _readiness_app():
    """readiness endpoint だけを生やした最小 Starlette app (ASGI test 用)。"""
    from starlette.applications import Starlette
    from starlette.routing import Route

    return Starlette(
        routes=[Route("/admin/readiness", srv._readiness_endpoint,
                      methods=["GET"])],
    )


async def _get_readiness(headers: dict | None = None) -> tuple[int, dict]:
    async with AsyncClient(
        transport=ASGITransport(app=_readiness_app()),
        base_url="http://test",
    ) as client:
        resp = await client.get("/admin/readiness", headers=headers)
        return resp.status_code, resp.json()


# ---------------------------------------------------------------------------
# 1. config knobs + allowlist
# ---------------------------------------------------------------------------

def test_config_defaults_for_readiness_protocol():
    """WP-6b knob は default ON / 30s / 35s (昇格済み既定値の回帰 fence)。"""
    cfg = GaOTTTConfig()
    assert cfg.readiness_protocol_enabled is True
    assert cfg.readiness_wait_timeout_seconds == 30.0
    assert cfg.route_readiness_timeout_seconds == 35.0


def test_config_env_rollback_flag(monkeypatch, tmp_path):
    """GAOTTT_READINESS_PROTOCOL_ENABLED=false で False (rollback 経路)。
    data_dir も tmp に向けて dev 環境の実 config に依存しない。"""
    monkeypatch.setenv("GAOTTT_READINESS_PROTOCOL_ENABLED", "false")
    monkeypatch.setenv("GAOTTT_DATA_DIR", str(tmp_path / "cfg-iso"))
    cfg = GaOTTTConfig.from_config_file()
    assert cfg.readiness_protocol_enabled is False


def test_readiness_and_bm25_knobs_in_tuning_allowlist():
    """WP-6b/6c/6d の rollback env 3 件が allowlist 登録済みで検証も通る。"""
    from gaottt.multiverse.tuning_env import (
        RUNTIME_TUNING_ENV_ALLOWLIST,
        validate_tuning_env,
    )

    for name in (
        "GAOTTT_READINESS_PROTOCOL_ENABLED",
        "GAOTTT_BM25_BACKGROUND_BUILD_ENABLED",
        "GAOTTT_BM25_SNAPSHOT_ENABLED",
    ):
        assert name in RUNTIME_TUNING_ENV_ALLOWLIST
    assert validate_tuning_env({
        "GAOTTT_READINESS_PROTOCOL_ENABLED": "0",
        "GAOTTT_BM25_BACKGROUND_BUILD_ENABLED": "false",
        "GAOTTT_BM25_SNAPSHOT_ENABLED": "no",
    }) == []


# ---------------------------------------------------------------------------
# 2. GET /admin/readiness — state mapping / auth / timings
# ---------------------------------------------------------------------------

async def test_readiness_state_transitions(tmp_path):
    """STARTING → SEMANTIC_READY (building/idle) → HYBRID_READY (ready)、
    bm25 failed は HYBRID_READY + bm25:"failed"、startup 失敗は FAILED。"""
    eng = _make_engine(tmp_path)
    eng.startup_timings = {"startup_total": 0.05, "bm25_build": 0.0}

    # STARTING: engine 未生成・error 無し
    status, body = await _get_readiness()
    assert status == 200
    assert body["state"] == "STARTING"
    assert body["timings"] == {}
    assert body["bm25_size"] == 0
    assert body["node_count"] == 0
    assert body["elapsed_seconds"] >= 0.0

    # SEMANTIC_READY: startup 完了 + background build 実行中
    srv._engine = eng
    eng.bm25_build_state = "building"
    status, body = await _get_readiness()
    assert body["state"] == "SEMANTIC_READY"
    assert body["timings"] == {"startup_total": 0.05, "bm25_build": 0.0}
    assert body["bm25_size"] == 0
    assert "bm25" not in body

    # idle (index 未接続) も SEMANTIC_READY
    eng.bm25_build_state = "idle"
    _, body = await _get_readiness()
    assert body["state"] == "SEMANTIC_READY"

    # HYBRID_READY: build 完了
    eng.bm25_build_state = "ready"
    _, body = await _get_readiness()
    assert body["state"] == "HYBRID_READY"

    # bm25 build 失敗: 検索は raw/virtual で稼働 → HYBRID_READY + 印
    eng.bm25_build_state = "failed"
    _, body = await _get_readiness()
    assert body["state"] == "HYBRID_READY"
    assert body["bm25"] == "failed"

    await eng.shutdown()


async def test_readiness_failed_state_carries_error():
    """startup task が例外を出した場合: FAILED + error 文字列。"""
    srv._readiness.error = "RuntimeError: manifest boom"
    srv._readiness.started_at = None
    status, body = await _get_readiness()
    assert status == 200
    assert body["state"] == "FAILED"
    assert "manifest boom" in body["error"]


async def test_readiness_auth_with_backend_token(monkeypatch):
    """GAOTTT_BACKEND_TOKEN 設定時は Bearer 認証 (401/200)、未設定は無認証。"""
    monkeypatch.setenv("GAOTTT_BACKEND_TOKEN", "sekrit-token")

    status, _ = await _get_readiness()
    assert status == 401

    status, _ = await _get_readiness(
        headers={"Authorization": "Bearer wrong-token"})
    assert status == 401

    status, body = await _get_readiness(
        headers={"Authorization": "Bearer sekrit-token"})
    assert status == 200
    assert body["state"] == "STARTING"

    monkeypatch.delenv("GAOTTT_BACKEND_TOKEN")
    status, _ = await _get_readiness()
    assert status == 200


async def test_readiness_timings_present_after_ready(tmp_path):
    """ready 後は engine.startup_timings (キー常在) がそのまま現れる。"""
    eng = _make_engine(tmp_path)
    await eng.startup()
    try:
        srv._engine = eng
        _, body = await _get_readiness()
        assert body["state"] in {"SEMANTIC_READY", "HYBRID_READY"}
        assert "startup_total" in body["timings"]
        assert body["node_count"] == 0  # 空 corpus
    finally:
        await eng.shutdown()


# ---------------------------------------------------------------------------
# 3. handler bounded wait — staged readiness 経路
# ---------------------------------------------------------------------------

async def test_get_engine_staged_waits_and_succeeds(
    tmp_path, isolated_config, monkeypatch,
):
    """timeout 内の遅い startup は待って成功。build_engine は 1 回。"""
    eng = _make_engine(tmp_path)
    calls = _tracked_engine(eng, delay=0.4)
    build_calls: list[bool] = []

    def fake_build(config):
        build_calls.append(True)
        return eng

    monkeypatch.setattr(srv, "build_engine", fake_build)
    srv._readiness.wait_timeout = 5.0

    got = await srv.get_engine()
    assert got is eng
    assert len(build_calls) == 1
    assert calls["startup"] == 1
    assert srv._readiness.task is not None and srv._readiness.task.done()
    await eng.shutdown()


async def test_get_engine_timeout_returns_structured_retryable_error(
    tmp_path, isolated_config, monkeypatch,
):
    """bounded wait 超過時: 単一行の構造化 retryable error。task は継続し、
    完成後の再呼び出しは engine を返す (再 build 無し)。"""
    eng = _make_engine(tmp_path)
    calls = _tracked_engine(eng, delay=1.0)
    monkeypatch.setattr(srv, "build_engine", lambda config: eng)
    srv._readiness.wait_timeout = 0.2

    with pytest.raises(ToolError, match=(
        r"engine starting \(state=STARTING, elapsed=[0-9.]+s\) — retry shortly"
    )):
        await srv.get_engine()

    # task は timeout で cancel されていない (共有 task の生存保証)
    assert srv._readiness.task is not None
    assert not srv._readiness.task.done()

    # "retry shortly": task 完成後の再 call は engine を即返す (再 build 無し)
    await asyncio.wait_for(srv._readiness.task, timeout=10.0)
    got = await srv.get_engine()
    assert got is eng
    assert calls["startup"] == 1  # 再 build されていない
    await eng.shutdown()


async def test_concurrent_first_calls_share_single_startup_task(
    tmp_path, isolated_config, monkeypatch,
):
    """並行初回 call は単一 startup task を共有 (build_engine 1 回)。"""
    eng = _make_engine(tmp_path)
    calls = _tracked_engine(eng, delay=0.3)
    build_calls = []
    monkeypatch.setattr(
        srv, "build_engine", lambda config: (build_calls.append(1), eng)[1])
    srv._readiness.wait_timeout = 10.0

    results = await asyncio.gather(*(srv.get_engine() for _ in range(5)))
    assert all(r is eng for r in results)
    assert len(build_calls) == 1
    assert calls["startup"] == 1
    await eng.shutdown()


async def test_get_engine_startup_failure_tool_error_and_partial_cleanup(
    tmp_path, isolated_config, monkeypatch,
):
    """startup 例外: 構造化 FAILED error + 部分構築 engine の best-effort
    shutdown。以降の call も同じ error で即失敗 (sticky)。"""
    eng = _make_engine(tmp_path)
    calls = _tracked_engine(
        eng, error=RuntimeError("boom-marker-42"))
    monkeypatch.setattr(srv, "build_engine", lambda config: eng)
    srv._readiness.wait_timeout = 5.0

    with pytest.raises(ToolError, match=(
        r"engine startup failed \(state=FAILED, "
        r"error=RuntimeError: boom-marker-42\)"
    )):
        await srv.get_engine()
    assert calls["shutdown"] >= 1  # 部分構築 engine の best-effort 片付け

    with pytest.raises(ToolError, match="boom-marker-42"):
        await srv.get_engine()
    assert calls["startup"] == 1  # 再試行で再 build はしない (sticky)


async def test_rollback_lazy_path_bitforbit(tmp_path, isolated_config, monkeypatch):
    """flag OFF: legacy lazy path — 初回 call が build を引き起こし bounded
    wait も startup task も無効 (現行挙動)。"""
    eng = _make_engine(tmp_path)
    calls = _tracked_engine(eng, delay=0.3)
    build_calls = []
    monkeypatch.setattr(
        srv, "build_engine", lambda config: (build_calls.append(1), eng)[1])
    srv._readiness.enabled = False
    # wait_timeout より長い startup でも (lazy path は timeout を見ない)
    srv._readiness.wait_timeout = 0.05

    got = await srv.get_engine()
    assert got is eng
    assert len(build_calls) == 1
    assert srv._readiness.task is None  # startup task は作られていない
    assert calls["startup"] == 1

    got2 = await srv.get_engine()
    assert got2 is eng
    assert len(build_calls) == 1
    await eng.shutdown()


# ---------------------------------------------------------------------------
# 4. install seam — custom_route + Starlette lifespan による engine 生存期間
# ---------------------------------------------------------------------------

async def test_install_adds_route_and_lifespan_owns_engine(
    tmp_path, isolated_config, monkeypatch,
):
    """_install_readiness_protocol: (a) /admin/readiness route が FastMCP
    app に載る、(b) app lifespan 起動で eager に単一 startup task が始まる、
    (c) lifespan 停止で実行中 task は cancel + 部分構築 engine の
    best-effort shutdown + 状態 reset。"""
    saved_streamable = srv.mcp.streamable_http_app
    saved_sse = srv.mcp.sse_app
    saved_routes = list(srv.mcp._custom_starlette_routes)
    eng = _make_engine(tmp_path)
    calls = _tracked_engine(eng, delay=0.5)
    monkeypatch.setattr(srv, "build_engine", lambda config: eng)
    try:
        srv._install_readiness_protocol()
        assert srv._readiness.route_installed is True

        app = srv.mcp.streamable_http_app()
        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/admin/readiness" in paths

        async with app.router.lifespan_context(app):
            # eager start: lifespan 入場時に単一 task が走っている
            assert srv._readiness.task is not None
            assert not srv._readiness.task.done()
            task = srv._readiness.task

        # lifespan 停止: cancel + bounded await + 状態 reset
        assert task.done()
        assert srv._readiness.task is None
        assert srv._engine is None
        assert calls["shutdown"] >= 1  # 部分構築 engine の best-effort 片付け
    finally:
        srv.mcp.streamable_http_app = saved_streamable  # type: ignore[method-assign]
        srv.mcp.sse_app = saved_sse  # type: ignore[method-assign]
        srv.mcp._custom_starlette_routes[:] = saved_routes


async def test_session_lifespan_no_teardown_when_transport_managed(tmp_path):
    """_mcp_lifespan (per-session): flag ON + HTTP backend (transport 管理下)
    では session close で engine を tear down しない (warm reconnect で
    再構築しない)。flag OFF (rollback) は現行どおり tear down する。"""
    eng = _make_engine(tmp_path)
    calls = _tracked_engine(eng)
    await eng.startup()
    srv._engine = eng
    try:
        # flag ON + transport managed → per-session close では何もしない
        srv._readiness.enabled = True
        srv._readiness.route_installed = True
        async with srv._mcp_lifespan(srv.mcp):
            pass
        assert calls["shutdown"] == 0
        assert srv._engine is eng

        # flag OFF (rollback) → 現行どおり per-session teardown
        srv._readiness.enabled = False
        async with srv._mcp_lifespan(srv.mcp):
            pass
        assert calls["shutdown"] == 1
        assert srv._engine is None
    finally:
        if srv._engine is not None:
            srv._engine = None
        if calls["shutdown"] == 0:
            await eng.shutdown()


# ---------------------------------------------------------------------------
# 5. diagnostics Tier B — bm25 size check の false-WARN gate
# ---------------------------------------------------------------------------

DIAG_DOCS = [
    "alpha gravitational wave probe one",
    "beta supernova cohort remnant two",
    "gamma hawking radiation drift three",
    "delta orbital tick resonance four",
    "epsilon mass evaporation sweep five",
]


async def _populate(tmp_path, docs: list[str]) -> None:
    eng = _make_engine(tmp_path)
    await eng.startup()
    try:
        await eng.index_documents([{"content": d} for d in docs])
        await eng.cache.flush_to_store(eng.store)
    finally:
        await eng.shutdown()


def _gated_fill(engine: GaOTTTEngine, gate: threading.Event):
    """fill (thread から呼ばれる) を Event で block する (既存 suite と同一 pattern)。"""
    original = GaOTTTEngine._fill_bm25_indexes

    def fill(hybrid, gate_idx, active_ids, active_texts):
        gate.wait(timeout=30.0)
        return original(engine, hybrid, gate_idx, active_ids, active_texts)

    return fill


async def test_diagnostics_bm25_pending_no_warn_during_background_build(
    tmp_path, monkeypatch, caplog,
):
    """WP-6c Risk 1: background build 窓内の diagnostics は bm25 size drift
    を WARN せず INFO (pending) に格下げする。"""
    await _populate(tmp_path, DIAG_DOCS)
    eng = _make_engine(tmp_path, background=True, bm25_snapshot_enabled=False)
    gate = threading.Event()
    monkeypatch.setattr(eng, "_fill_bm25_indexes", _gated_fill(eng, gate))
    with caplog.at_level(logging.INFO, logger="gaottt.diagnostics.startup"):
        await eng.startup()
    try:
        assert eng.bm25_build_state == "building"
        messages = [r.getMessage() for r in caplog.records]
        drift = [m for m in messages if "tier_b_bm25_size_drift" in m]
        assert drift == [], (
            f"background build 中に bm25 size drift WARN が出た: {drift}"
        )
        assert any("tier_b_bm25_size_pending" in m for m in messages)
    finally:
        gate.set()
        await eng.shutdown()


async def test_diagnostics_bm25_check_present_when_ready(tmp_path, caplog):
    """state ready (同期 build) では従来どおり size check が走る。"""
    await _populate(tmp_path, DIAG_DOCS)
    eng = _make_engine(tmp_path, background=False, bm25_snapshot_enabled=False)
    with caplog.at_level(logging.INFO, logger="gaottt.diagnostics.startup"):
        await eng.startup()
    try:
        assert eng.bm25_build_state == "ready"
        messages = [r.getMessage() for r in caplog.records]
        assert any("tier_b_bm25_size_ok" in m for m in messages)
    finally:
        await eng.shutdown()


# ---------------------------------------------------------------------------
# 6. supervisor /route readiness poll + proxy shim
# ---------------------------------------------------------------------------

SUPERVISOR = "gaottt.multiverse.supervisor"


@pytest.fixture(scope="session")
def embedder_url():
    from tests.integration._supervisor_helpers import (
        StubServiceEmbedder,
        start_uvicorn,
        stop_uvicorn,
    )
    from gaottt.embedding.service import create_app

    app = create_app(StubServiceEmbedder(dimension=768))
    server, thread, port = start_uvicorn(app)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        stop_uvicorn(server, thread)


@pytest.fixture
def multiverse_root(tmp_path: Path) -> Path:
    import os

    from gaottt.multiverse.registry import UNIVERSES_SUBDIR

    root = tmp_path / "multiverse"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    (root / UNIVERSES_SUBDIR).mkdir(parents=True, exist_ok=True)
    (root / "trash").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    return root


async def _route_with(
    embedder_url, multiverse_root, monkeypatch,
    *,
    probe_side_effect,
    readiness_side_effect,
    route_readiness_timeout: float = 35.0,
    readiness_return_value: dict | None = None,
    fresh_spawn: bool = True,
) -> tuple[int, dict]:
    from tests.integration._supervisor_helpers import (
        asgi_client,
        create_universe,
        make_config,
        make_supervisor,
    )

    config = make_config(multiverse_root, embedder_url)
    config.route_readiness_timeout_seconds = route_readiness_timeout
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            body = await create_universe(client, owner="readiness-owner")
            probe = AsyncMock(side_effect=probe_side_effect)
            if readiness_return_value is not None:
                fetch = AsyncMock(return_value=readiness_return_value)
            else:
                fetch = AsyncMock(side_effect=readiness_side_effect)
            popen = MagicMock()
            with patch(f"{SUPERVISOR}._probe_backend_with_token", probe), \
                    patch(f"{SUPERVISOR}._fetch_backend_readiness", fetch), \
                    patch(f"{SUPERVISOR}.subprocess.Popen", popen):
                r = await client.post(
                    "/route", json={"api_key": body["api_key"]})
            return r.status_code, r.json()
    finally:
        await reg.close()


async def test_route_waits_for_semantic_ready_then_returns(
    embedder_url, multiverse_root, monkeypatch,
):
    """cold spawn: STARTING を 1 回観測してから SEMANTIC_READY → 従来どおり
    の応答 (readiness field 無し)。"""
    status, body = await _route_with(
        embedder_url, multiverse_root, monkeypatch,
        probe_side_effect=["down", "ok"],
        readiness_side_effect=[
            {"state": "STARTING", "elapsed_seconds": 0.5},
            {"state": "SEMANTIC_READY", "elapsed_seconds": 6.2},
        ],
        route_readiness_timeout=5.0,
    )
    assert status == 200, body
    assert "url" in body and "token" in body
    assert "readiness" not in body  # ready → 従来 shape


async def test_route_fast_path_also_waits_readiness(
    embedder_url, multiverse_root, monkeypatch,
):
    """backend found (probe OK fast path) でも readiness を確認してから応答。"""
    status, body = await _route_with(
        embedder_url, multiverse_root, monkeypatch,
        probe_side_effect=["ok"],
        readiness_side_effect=[
            {"state": "HYBRID_READY", "elapsed_seconds": 150.0},
        ],
        fresh_spawn=False,
    )
    assert status == 200, body
    assert "readiness" not in body


async def test_route_deadline_returns_starting(
    embedder_url, multiverse_root, monkeypatch,
):
    """deadline 超過: error にせず URL を返しつつ readiness:"starting"。"""
    status, body = await _route_with(
        embedder_url, multiverse_root, monkeypatch,
        probe_side_effect=["down", "ok"],
        readiness_side_effect=None,  # 常に STARTING (deadline 超過シミュレーション)
        readiness_return_value={"state": "STARTING", "elapsed_seconds": 1.0},
        route_readiness_timeout=0.05,
    )
    assert status == 200, body
    assert body.get("readiness") == "starting"
    assert "url" in body and "token" in body


async def test_route_failed_readiness_503(
    embedder_url, multiverse_root, monkeypatch,
):
    """FAILED: 503 + error 文字列。"""
    status, body = await _route_with(
        embedder_url, multiverse_root, monkeypatch,
        probe_side_effect=["down", "ok"],
        readiness_side_effect=[
            {"state": "FAILED", "error": "RuntimeError: manifest boom"},
        ],
    )
    assert status == 503
    assert "manifest boom" in body["detail"]


async def test_route_legacy_404_falls_back_immediately(
    embedder_url, multiverse_root, monkeypatch,
):
    """旧 backend (readiness endpoint 無し → 404): 従来挙動へ即時 fallback。"""
    from gaottt.multiverse.supervisor import READINESS_LEGACY

    status, body = await _route_with(
        embedder_url, multiverse_root, monkeypatch,
        probe_side_effect=["down", "ok"],
        readiness_side_effect=[READINESS_LEGACY],
    )
    assert status == 200, body
    assert "readiness" not in body


async def test_fetch_backend_readiness_over_http():
    """_fetch_backend_readiness の実 HTTP 層: 200 payload / 404 legacy /
    不正 JSON は transient (None)。"""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Route

    from gaottt.multiverse.supervisor import (
        READINESS_LEGACY,
        _fetch_backend_readiness,
    )
    from tests.integration._supervisor_helpers import (
        start_uvicorn, stop_uvicorn,
    )

    async def ready(request):
        return JSONResponse({"state": "HYBRID_READY", "bm25_size": 5})

    async def garbage(request):
        return PlainTextResponse("not-json")

    server, thread, port = start_uvicorn(Starlette(
        routes=[Route("/admin/readiness", ready, methods=["GET"])]))
    try:
        payload = await _fetch_backend_readiness("127.0.0.1", port, "tok")
        assert payload == {"state": "HYBRID_READY", "bm25_size": 5}
    finally:
        stop_uvicorn(server, thread)

    server, thread, port = start_uvicorn(Starlette())
    try:
        # route 無し → 404 → legacy
        assert await _fetch_backend_readiness(
            "127.0.0.1", port, "tok") is READINESS_LEGACY
    finally:
        stop_uvicorn(server, thread)

    server, thread, port = start_uvicorn(Starlette(routes=[
        Route("/admin/readiness", garbage, methods=["GET"])]))
    try:
        # 200 だが不正 JSON → transient (None)
        assert await _fetch_backend_readiness("127.0.0.1", port, "tok") is None
    finally:
        stop_uvicorn(server, thread)


def test_route_to_supervisor_logs_info_when_starting(caplog):
    """proxy shim: /route が readiness:"starting" を返しても接続続行、
    INFO log を 1 回出す (block loop 無し)。"""
    from gaottt.server.mcp_proxy import _route_to_supervisor

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "url": "http://127.0.0.1:7890/mcp",
        "token": "abc",
        "readiness": "starting",
    }
    with caplog.at_level(logging.INFO, logger="gaottt.server.mcp_proxy"), \
            patch("gaottt.server.mcp_proxy.httpx.post",
                  return_value=mock_response):
        url, token = _route_to_supervisor("http://sup:7880", "mykey")

    assert url == "http://127.0.0.1:7890/mcp"
    assert token == "abc"
    assert any(
        "starting" in r.getMessage() for r in caplog.records
        if r.levelno == logging.INFO
    )


# ---------------------------------------------------------------------------
# 7. WP-8 — rollback (flag OFF) は /admin/readiness route 自体を登録しない
# ---------------------------------------------------------------------------

async def test_install_with_flag_off_registers_no_route(
    tmp_path, isolated_config,
):
    """WP-8 blocking #2: ``readiness_protocol_enabled=False`` で install した
    backend は /admin/readiness route を登録しない (= 404)。supervisor は 404
    を READINESS_LEGACY と読み即時応答するので、恒久 STARTING を 35s poll する
    退化が起こらない。(lifespan wrap の eager start は元から ``enabled`` gate
    参照 — flag OFF で task が始まらないことは rollback bit-for-bit test が
    担保。)"""
    saved_streamable = srv.mcp.streamable_http_app
    saved_sse = srv.mcp.sse_app
    saved_routes = list(srv.mcp._custom_starlette_routes)
    try:
        srv._readiness.enabled = False  # main() が boot config から反映する値
        srv._install_readiness_protocol()
        assert srv._readiness.route_installed is True  # install 済み印は立つ

        app = srv.mcp.streamable_http_app()
        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/admin/readiness" not in paths

        # end-to-end: 実 app に GET しても 404 (supervisor の legacy 判定条件)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            resp = await client.get("/admin/readiness")
        assert resp.status_code == 404
    finally:
        srv.mcp.streamable_http_app = saved_streamable  # type: ignore[method-assign]
        srv.mcp.sse_app = saved_sse  # type: ignore[method-assign]
        srv.mcp._custom_starlette_routes[:] = saved_routes


def _start_uvicorn_on_port(app, port: float):
    """stub backend を指定 port で起こす (universe port に据えるため)。"""
    import uvicorn

    config = uvicorn.Config(
        app, host="127.0.0.1", port=int(port), log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if server.started:
            return server, thread
        time.sleep(0.05)
    server.should_exit = True
    thread.join(timeout=5)
    raise RuntimeError("stub backend uvicorn did not start within 10s")


async def test_route_returns_promptly_against_readiness_disabled_backend(
    embedder_url, multiverse_root,
):
    """WP-8 blocking #2 (supervisor 面): readiness endpoint を持たない
    (= flag OFF backend 相当の) 実 HTTP backend に対する /route は、
    deadline 35s を使わず即時に legacy 応答を返す。``_fetch_backend_readiness``
    は本物を実 HTTP で叩く (404 → READINESS_LEGACY 経路の結合検証)。"""
    from starlette.applications import Starlette

    from tests.integration._supervisor_helpers import (
        asgi_client,
        create_universe,
        make_config,
        make_supervisor,
    )

    config = make_config(multiverse_root, embedder_url)
    config.route_readiness_timeout_seconds = 35.0
    app, reg = await make_supervisor(config)
    try:
        async with asgi_client(app) as client:
            body = await create_universe(client, owner="rollback-owner")
            # universe port に「/admin/readiness 無し backend」を据える
            server, thread = _start_uvicorn_on_port(
                Starlette(), body["port"],
            )
            try:
                probe = AsyncMock(return_value="ok")
                popen = MagicMock()
                with patch(f"{SUPERVISOR}._probe_backend_with_token", probe), \
                        patch(f"{SUPERVISOR}.subprocess.Popen", popen):
                    t0 = time.monotonic()
                    r = await client.post(
                        "/route", json={"api_key": body["api_key"]})
                    elapsed = time.monotonic() - t0
            finally:
                server.should_exit = True
                thread.join(timeout=5)

        assert r.status_code == 200, r.text
        assert "readiness" not in r.json(), (
            "404 backend must take the immediate legacy path, not STARTING"
        )
        # 退化 (恒久 STARTING を 35s poll) していなければ一瞬で返る
        assert elapsed < 10.0, (
            f"/route took {elapsed:.1f}s against a 404 backend — "
            "readiness rollback is polling the full deadline"
        )
    finally:
        await reg.close()
