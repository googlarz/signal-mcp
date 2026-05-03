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
    if getattr(_store_mod._thread_local, "conn", None) is not None:
        _store_mod._thread_local.conn.close()
        _store_mod._thread_local.conn = None

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
async def test_tool_delete_group_message():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("delete_group_message", {"group_id": "grp1==", "target_timestamp": 1700000000000})
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
    all_data = json.loads(result_all[0].text)
    page1 = json.loads(result_page[0].text)
    page2 = json.loads(result_next[0].text)
    assert len(all_data["messages"]) == 5
    assert all_data["total"] == 5
    assert all_data["has_more"] is False
    assert len(page1["messages"]) == 3
    assert page1["has_more"] is True
    assert len(page2["messages"]) == 2
    assert page2["has_more"] is False


# ── New tools ─────────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_send_sticker():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 9})))
    result = await call_tool("send_sticker", {
        "recipient": "+19999999999",
        "pack_id": "aabbcc",
        "sticker_id": 3,
    })
    data = json.loads(result[0].text)
    assert data["status"] == "sent"
    assert data["timestamp"] == 9


@respx.mock
@pytest.mark.asyncio
async def test_tool_send_group_sticker():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 10})))
    result = await call_tool("send_group_sticker", {
        "group_id": "grp1==",
        "pack_id": "aabbcc",
        "sticker_id": 0,
    })
    data = json.loads(result[0].text)
    assert data["status"] == "sent"


@pytest.mark.asyncio
async def test_tool_list_attachments_empty(tmp_path, monkeypatch):
    import signal_mcp.client as _client_mod
    monkeypatch.setattr(_client_mod, "ATTACHMENT_DIR", tmp_path / "no-such-dir")
    result = await call_tool("list_attachments", {})
    assert json.loads(result[0].text) == []


@pytest.mark.asyncio
async def test_tool_list_attachments_returns_files(tmp_path, monkeypatch):
    import signal_mcp.client as _client_mod
    att_dir = tmp_path / "attachments"
    att_dir.mkdir()
    (att_dir / "photo.jpg").write_bytes(b"x" * 100)
    monkeypatch.setattr(_client_mod, "ATTACHMENT_DIR", att_dir)
    result = await call_tool("list_attachments", {})
    files = json.loads(result[0].text)
    assert len(files) == 1
    assert files[0]["filename"] == "photo.jpg"
    assert files[0]["size"] == 100


@pytest.mark.asyncio
async def test_tool_get_attachment(tmp_path, monkeypatch):
    import signal_mcp.client as _client_mod
    att_dir = tmp_path / "attachments"
    att_dir.mkdir()
    (att_dir / "file.pdf").write_bytes(b"y" * 42)
    monkeypatch.setattr(_client_mod, "ATTACHMENT_DIR", att_dir)
    result = await call_tool("get_attachment", {"filename": "file.pdf"})
    data = json.loads(result[0].text)
    assert data["filename"] == "file.pdf"
    assert data["size"] == 42


@pytest.mark.asyncio
async def test_tool_get_attachment_not_found(tmp_path, monkeypatch):
    import signal_mcp.client as _client_mod
    att_dir = tmp_path / "attachments"
    att_dir.mkdir()
    monkeypatch.setattr(_client_mod, "ATTACHMENT_DIR", att_dir)
    result = await call_tool("get_attachment", {"filename": "missing.jpg"})
    assert "Error" in result[0].text


@pytest.mark.asyncio
async def test_get_conversation_auto_marks_read():
    """get_conversation should auto-mark received messages as read."""
    _store_mod.init_db()
    _store_mod.save_message(Message(
        id="msg_unread", sender="+19999999999", body="hello",
        timestamp=datetime(2024, 1, 1), is_read=False,
    ))
    await call_tool("get_conversation", {"recipient": "+19999999999"})
    # Re-fetch from store — should now be marked read
    msgs = _store_mod.get_conversation("+19999999999")
    assert all(m.is_read for m in msgs)


@pytest.mark.asyncio
async def test_get_conversation_enriches_sender_name(monkeypatch):
    """get_conversation response includes sender_name field."""
    import signal_mcp.client as _client_mod
    monkeypatch.setattr(_client_mod, "_contact_cache", {"+19999999999": "Alice"})
    monkeypatch.setattr(_client_mod, "_contact_cache_loaded", True)
    _store_mod.init_db()
    _store_mod.save_message(Message(
        id="msg_alice", sender="+19999999999", body="hi",
        timestamp=datetime(2024, 1, 1),
    ))
    result = await call_tool("get_conversation", {"recipient": "+19999999999"})
    data = json.loads(result[0].text)
    assert data["messages"][0]["sender_name"] == "Alice"


# ── Input validation ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_required_param_returns_error():
    """Missing required params should return a clean error, not a KeyError."""
    result = await call_tool("send_message", {"recipient": "+19999999999"})  # missing "message"
    assert "Missing required parameter" in result[0].text
    assert "message" in result[0].text


@pytest.mark.asyncio
async def test_missing_multiple_required_params():
    result = await call_tool("send_message", {})
    assert "recipient" in result[0].text
    assert "message" in result[0].text


# ── Configuration tools ────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_get_configuration():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({
        "readReceipts": True, "typingIndicators": False,
    })))
    result = await call_tool("get_configuration", {})
    data = json.loads(result[0].text)
    assert data["readReceipts"] is True


@respx.mock
@pytest.mark.asyncio
async def test_tool_update_configuration():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("update_configuration", {"read_receipts": False})
    data = json.loads(result[0].text)
    assert data["status"] == "updated"


# ── Sticker pack tools ─────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_list_sticker_packs():
    packs = [{"packId": "abc", "title": "Fun"}]
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok(packs)))
    result = await call_tool("list_sticker_packs", {})
    data = json.loads(result[0].text)
    assert data[0]["packId"] == "abc"


@respx.mock
@pytest.mark.asyncio
async def test_tool_add_sticker_pack():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("add_sticker_pack", {"uri": "https://signal.art/addstickers/#pack_id=abc&pack_key=xyz"})
    data = json.loads(result[0].text)
    assert data["status"] == "installed"


@pytest.mark.asyncio
async def test_tool_add_sticker_pack_missing_uri():
    result = await call_tool("add_sticker_pack", {})
    assert "Missing required parameter" in result[0].text


# ── Store management tools ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_clear_local_store():
    from datetime import datetime as _dt
    _store_mod.init_db()
    _store_mod.save_message(Message(id="x1", sender="+1", body="a", timestamp=_dt(2024,1,1)))
    _store_mod.save_message(Message(id="x2", sender="+2", body="b", timestamp=_dt(2024,1,2)))
    result = await call_tool("clear_local_store", {"confirm": True})
    data = json.loads(result[0].text)
    assert data["deleted"] == 2
    assert _store_mod.get_stats()["total_messages"] == 0


@pytest.mark.asyncio
async def test_tool_clear_local_store_requires_confirm():
    result = await call_tool("clear_local_store", {"confirm": False})
    assert "Error" in result[0].text


@pytest.mark.asyncio
async def test_tool_delete_local_messages():
    from datetime import datetime as _dt
    _store_mod.init_db()
    _store_mod.save_message(Message(id="d1", sender="+19999999999", body="hi", timestamp=_dt(2024,1,1)))
    _store_mod.save_message(Message(id="d2", sender="+18888888888", body="yo", timestamp=_dt(2024,1,2)))
    result = await call_tool("delete_local_messages", {"recipient": "+19999999999"})
    data = json.loads(result[0].text)
    assert data["deleted"] == 1
    assert _store_mod.get_stats()["total_messages"] == 1


# ── has_more / total in get_conversation ──────────────────────────────────────

@pytest.mark.asyncio
async def test_get_conversation_returns_pagination_metadata():
    from datetime import datetime as _dt
    _store_mod.init_db()
    for i in range(5):
        _store_mod.save_message(Message(
            id=f"pg{i}", sender="+19999999999", body=f"msg{i}",
            timestamp=_dt(2024, 1, i + 1),
        ))
    result = await call_tool("get_conversation", {"recipient": "+19999999999", "limit": 3})
    data = json.loads(result[0].text)
    assert "messages" in data
    assert data["total"] == 5
    assert data["has_more"] is True
    assert data["limit"] == 3
    assert len(data["messages"]) == 3


@pytest.mark.asyncio
async def test_get_conversation_has_more_false_when_all_returned():
    from datetime import datetime as _dt
    _store_mod.init_db()
    for i in range(3):
        _store_mod.save_message(Message(
            id=f"all{i}", sender="+19999999999", body=f"msg{i}",
            timestamp=_dt(2024, 1, i + 1),
        ))
    result = await call_tool("get_conversation", {"recipient": "+19999999999"})
    data = json.loads(result[0].text)
    assert data["total"] == 3
    assert data["has_more"] is False


# ── E.164 validation ───────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_send_message_invalid_number():
    result = await call_tool("send_message", {"recipient": "notanumber", "message": "hi"})
    assert "Error" in result[0].text
    assert "E.164" in result[0].text


# ── search_messages with sender filter ────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_search_messages_sender_filter():
    from datetime import datetime as _dt
    _store_mod.init_db()
    _store_mod.save_message(Message(id="s1", sender="+11111111111", body="hello from 1", timestamp=_dt(2024, 1, 1)))
    _store_mod.save_message(Message(id="s2", sender="+12222222222", body="hello from 2", timestamp=_dt(2024, 1, 2)))
    result = await call_tool("search_messages", {"query": "hello", "sender": "+11111111111"})
    data = json.loads(result[0].text)
    assert len(data) == 1
    assert data[0]["sender"] == "+11111111111"


# ── export_messages tool ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_export_messages_json():
    from datetime import datetime as _dt
    _store_mod.init_db()
    _store_mod.save_message(Message(id="e1", sender="+11111111111", body="exportable", timestamp=_dt(2024, 1, 1)))
    result = await call_tool("export_messages", {"format": "json"})
    data = json.loads(result[0].text)
    assert data["format"] == "json"
    payload = json.loads(data["data"])
    assert len(payload) == 1
    assert payload[0]["body"] == "exportable"


@pytest.mark.asyncio
async def test_tool_export_messages_csv():
    from datetime import datetime as _dt
    _store_mod.init_db()
    _store_mod.save_message(Message(id="e2", sender="+11111111111", body="csv row", timestamp=_dt(2024, 1, 1)))
    result = await call_tool("export_messages", {"format": "csv"})
    data = json.loads(result[0].text)
    assert data["format"] == "csv"
    assert "csv row" in data["data"]


@pytest.mark.asyncio
async def test_tool_export_messages_invalid_format():
    result = await call_tool("export_messages", {"format": "xml"})
    assert "Error" in result[0].text


@pytest.mark.asyncio
async def test_tool_export_messages_invalid_since():
    result = await call_tool("export_messages", {"since": "not-a-date"})
    assert "Error" in result[0].text
