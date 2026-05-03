"""Tests for MCP server tool dispatch."""

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import respx
import httpx

import signal_mcp.store as _store_mod
from signal_mcp.config import DAEMON_URL
from signal_mcp.models import Message
from signal_mcp.server import call_tool
from signal_mcp.client import SignalClient


def rpc_ok(result) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


@pytest.fixture(autouse=True)
def reset_client(monkeypatch, tmp_path):
    # Redirect store to temp DB and reset init flag for every test
    monkeypatch.setattr(_store_mod, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(_store_mod, "_initialized", False)

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


@pytest.mark.asyncio
async def test_tool_get_unread_empty():
    result = await call_tool("get_unread", {})
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
async def test_tool_list_conversations_empty():
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
    async def fail(): raise Exception("daemon unavailable")
    import signal_mcp.server as server_mod
    monkeypatch.setattr(server_mod._client, "ensure_daemon", fail)
    result = await call_tool("store_stats", {})
    assert "total_messages" in result[0].text


# ── New v1.2 tools ─────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_unblock_contact():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("unblock_contact", {"number": "+19999999999"})
    assert "unblocked" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_remove_contact():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("remove_contact", {"number": "+19999999999"})
    assert "removed" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_update_profile():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("update_profile", {"name": "Alice", "about": "Hey there"})
    assert "profile updated" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_create_group():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"groupId": "newgrp=="})))
    result = await call_tool("create_group", {"name": "My Group", "members": ["+19999999999"]})
    assert "group created" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_join_group():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"groupId": "joinedgrp=="})))
    result = await call_tool("join_group", {"uri": "https://signal.group/#abc"})
    assert "joined group" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_list_devices():
    devices = [{"id": 1, "name": "iPhone"}, {"id": 2, "name": "MacBook"}]
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok(devices)))
    result = await call_tool("list_devices", {})
    assert "iPhone" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_add_device():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("add_device", {"uri": "sgnl://linkdevice?uuid=abc&pub_key=xyz"})
    assert "device linked" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_remove_device():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("remove_device", {"device_id": 2})
    assert "device removed" in result[0].text


@pytest.mark.asyncio
async def test_tool_get_own_number():
    result = await call_tool("get_own_number", {})
    assert "+10000000000" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_send_read_receipt_marks_store_read():
    """send_read_receipt should update is_read in local store."""
    from signal_mcp.models import Message
    from datetime import datetime
    # Save a message that will be "read"
    msg = Message(id="1700000000000", sender="+2", body="hi",
                  timestamp=datetime(2024, 1, 1))
    _store_mod.save_message(msg)
    assert _store_mod.get_unread_messages(own_number="+10000000000")[0].id == "1700000000000"

    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await call_tool("send_read_receipt", {"sender": "+2", "timestamps": [1700000000000]})
    assert _store_mod.get_unread_messages(own_number="+10000000000") == []


@respx.mock
@pytest.mark.asyncio
async def test_tool_update_group():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("update_group", {"group_id": "grp1==", "name": "New Name"})
    assert "group updated" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_get_profile():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([
        {"number": "+19999999999", "uuid": "uuid-1", "profile": {"givenName": "Alice", "familyName": "Smith"}}
    ])))
    result = await call_tool("get_profile", {"number": "+19999999999"})
    assert "+19999999999" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_send_note_to_self():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 999})))
    result = await call_tool("send_note_to_self", {"message": "Remember to buy milk"})
    assert "sent" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_edit_message_dm():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("edit_message", {
        "target_timestamp": 1700000000000,
        "message": "corrected text",
        "recipient": "+19999999999",
    })
    assert "edited" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_edit_message_group():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("edit_message", {
        "target_timestamp": 1700000000000,
        "message": "corrected",
        "group_id": "grp1==",
    })
    assert "edited" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_send_message_with_quote():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 1})))
    result = await call_tool("send_message", {
        "recipient": "+19999999999",
        "message": "Replying!",
        "quote_author": "+11111111111",
        "quote_timestamp": 1700000000000,
    })
    assert "sent" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_send_group_message_with_mentions():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 2})))
    result = await call_tool("send_group_message", {
        "group_id": "grp1==",
        "message": "Hey +19999999999!",
        "mentions": [{"start": 4, "length": 12, "author": "+19999999999"}],
    })
    assert "sent" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_send_attachment_view_once():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 3})))
    result = await call_tool("send_attachment", {
        "recipient": "+19999999999",
        "path": "/tmp/photo.jpg",
        "view_once": True,
    })
    assert "sent" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_update_group_admin_management():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("update_group", {
        "group_id": "grp1==",
        "add_admins": ["+19999999999"],
        "remove_admins": ["+11111111111"],
    })
    assert "group updated" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_receive_delivery_receipt():
    envelopes = [
        {
            "envelope": {
                "source": "+13333333333",
                "timestamp": 1700000000000,
                "receiptMessage": {"type": "DELIVERY", "timestamps": [1699999999000]},
            }
        }
    ]
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok(envelopes)))
    result = await call_tool("receive_messages", {"timeout": 1})
    data = json.loads(result[0].text)
    assert len(data) == 1
    assert data[0]["receipt_type"] == "DELIVERY"


@pytest.mark.asyncio
async def test_tool_get_conversation_pagination():
    from signal_mcp.models import Message
    from datetime import datetime
    for i in range(5):
        _store_mod.save_message(Message(
            id=f"msg{i}", sender="+2", body=f"msg {i}",
            timestamp=datetime(2024, 1, 1, 0, 0, i),
        ))
    result_all = await call_tool("get_conversation", {"recipient": "+2", "limit": 5})
    result_page = await call_tool("get_conversation", {"recipient": "+2", "limit": 3, "offset": 0})
    result_next = await call_tool("get_conversation", {"recipient": "+2", "limit": 3, "offset": 3})
    all_msgs = json.loads(result_all[0].text)
    page1 = json.loads(result_page[0].text)
    page2 = json.loads(result_next[0].text)
    assert len(all_msgs) == 5
    assert len(page1) == 3
    assert len(page2) == 2
