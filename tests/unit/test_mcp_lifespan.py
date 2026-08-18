from unittest.mock import AsyncMock

import pytest

from gaottt.server import mcp_server


@pytest.mark.asyncio
async def test_mcp_lifespan_shuts_down_and_clears_lazy_engine(monkeypatch):
    engine = AsyncMock()
    monkeypatch.setattr(mcp_server, "_engine", engine)

    async with mcp_server._mcp_lifespan(mcp_server.mcp):
        assert mcp_server._engine is engine

    engine.shutdown.assert_awaited_once_with()
    assert mcp_server._engine is None


@pytest.mark.asyncio
async def test_mcp_lifespan_without_initialized_engine(monkeypatch):
    monkeypatch.setattr(mcp_server, "_engine", None)

    async with mcp_server._mcp_lifespan(mcp_server.mcp):
        pass

    assert mcp_server._engine is None
