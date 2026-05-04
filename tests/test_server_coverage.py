"""Coverage tests for signal_mcp/server.py — uncovered lines."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
import httpx

import signal_mcp.store as _store_mod
import signal_mcp.server as server_mod
from signal_mcp.config import DAEMON_URL
from signal_mcp.client import SignalClient, SignalError
from signal_mcp.server import call_tool, get_client, TOOLS
from mcp.types import Tool


@pytest.fixture(autouse=True)
def reset_server(monkeypatch, tmp_path):
    monkeypatch.setattr(_store_mod, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(_store_mod, "_initialized_paths", set())
    if getattr(_store_mod._thread_local, "conn", None) is not None:
        _store_mod._thread_local.conn.close()
        _store_mod._thread_local.conn = None

    test_client = SignalClient(account="+10000000000")
    monkeypatch.setattr(server_mod, "_client", test_client)

    async def noop():
        pass

    monkeypatch.setattr(test_client, "ensure_daemon", noop)
    return test_client


# ── get_client lazy init ──────────────────────────────────────────────────────

def test_get_client_lazy_init(monkeypatch):
    """get_client() creates SignalClient once and caches it."""
    monkeypatch.setattr(server_mod, "_client", None)
    mock_instance = MagicMock(spec=SignalClient)
    with patch("signal_mcp.server.SignalClient", return_value=mock_instance) as mock_cls:
        c1 = get_client()
        c2 = get_client()
    # Constructor called only once despite two get_client() calls
    mock_cls.assert_called_once()
    assert c1 is c2


# ── TOOLS list ────────────────────────────────────────────────────────────────

def test_tools_is_non_empty_list_of_tool_instances():
    assert isinstance(TOOLS, list)
    assert len(TOOLS) > 0
    for t in TOOLS:
        assert isinstance(t, Tool)


@pytest.mark.asyncio
async def test_list_tools_handler_returns_tools():
    """list_tools() registered handler returns the TOOLS list."""
    from signal_mcp.server import list_tools
    result = await list_tools()
    assert result is TOOLS
    assert len(result) > 0


# ── receive_messages handler re-raise when not background service ─────────────

@pytest.mark.asyncio
async def test_receive_messages_reraises_non_background_error(reset_server):
    """When receive_messages raises a SignalError that is NOT about background service,
    the exception is re-raised (not swallowed)."""
    client = reset_server

    async def bad_receive(**kwargs):
        raise SignalError("daemon not running")

    client.receive_messages = bad_receive

    result = await call_tool("receive_messages", {"timeout": 1})
    # The re-raised error should be caught by the outer try/except in call_tool
    # and returned as an error TextContent
    assert result[0].text.startswith("Error:")
    assert "daemon not running" in result[0].text
