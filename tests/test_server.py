"""Tests for MCP server tool dispatch."""

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import respx
import httpx

from signal_mcp.config import DAEMON_URL
from signal_mcp.models import Message
from signal_mcp.server import call_tool
from signal_mcp.client import SignalClient


def rpc_ok(result) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


@pytest.fixture(autouse=True)
def reset_client(monkeypatch):
    test_client = SignalClient(account="+10000000000")
    monkeypatch.setattr("signal_mcp.server._client", test_client)
    async def noop(): pass
    monkeypatch.setattr(test_client, "ensure_daemon", noop)
    return test_client


@respx.mock
@pytest.mark.asyncio
async def test_tool_send_message():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 123})))
    result = await call_tool("send_message", {"recipient": "+19999999999", "message": "Hi"})
    assert "sent" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_list_contacts_empty():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([])))
    result = await call_tool("list_contacts", {})
    assert "[]" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_list_groups_empty():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([])))
    result = await call_tool("list_groups", {})
    assert "[]" in result[0].text


@pytest.mark.asyncio
async def test_tool_unknown():
    result = await call_tool("nonexistent_tool", {})
    assert "Unknown tool" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_block_contact():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("block_contact", {"number": "+19999999999"})
    assert "blocked" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_set_typing():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("set_typing", {"recipient": "+19999999999"})
    assert "typing" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_react():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("react_to_message", {
        "recipient": "+19999999999",
        "target_author": "+11111111111",
        "target_timestamp": 1700000000000,
        "emoji": "❤️",
    })
    assert "reaction sent" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_send_group_message():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 456})))
    result = await call_tool("send_group_message", {"group_id": "abc123==", "message": "Hello group"})
    assert "sent" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_receive_empty():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([])))
    result = await call_tool("receive_messages", {"timeout": 1})
    assert "[]" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_get_unread_empty():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([])))
    result = await call_tool("get_unread", {"timeout": 1})
    assert "[]" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_send_group_attachment():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 789})))
    result = await call_tool("send_group_attachment", {"group_id": "abc123==", "path": "/tmp/photo.jpg"})
    assert "sent" in result[0].text


@pytest.mark.asyncio
async def test_tool_import_desktop_missing_db():
    with patch("signal_mcp.desktop.SIGNAL_DB") as mock_db:
        mock_db.exists.return_value = False
        result = await call_tool("import_desktop", {})
    assert "Error" in result[0].text


@pytest.mark.asyncio
async def test_tool_list_conversations_empty(tmp_path, monkeypatch):
    import signal_mcp.store as store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("signal_mcp.server._store", store)
    result = await call_tool("list_conversations", {})
    assert "[]" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_delete_message():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("delete_message", {"recipient": "+19999999999", "target_timestamp": 1700000000000})
    assert "deleted" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_send_read_receipt():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("send_read_receipt", {"sender": "+19999999999", "timestamps": [1700000000000]})
    assert "read receipt" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_update_contact():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("update_contact", {"number": "+19999999999", "name": "Alice"})
    assert "updated" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_leave_group():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("leave_group", {"group_id": "grp123=="})
    assert "left group" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_list_identities():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([])))
    result = await call_tool("list_identities", {})
    assert "[]" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_trust_identity():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("trust_identity", {"number": "+19999999999"})
    assert "trusted" in result[0].text


@pytest.mark.asyncio
async def test_tool_store_stats_no_daemon_needed(monkeypatch):
    # store_stats should work even when ensure_daemon raises
    async def fail(): raise Exception("daemon unavailable")
    import signal_mcp.server as server_mod
    monkeypatch.setattr(server_mod._client, "ensure_daemon", fail)
    result = await call_tool("store_stats", {})
    # Should succeed (store_stats is daemon-free)
    assert "total_messages" in result[0].text
