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
    monkeypatch.setattr(_store_mod, "_initialized_paths", set())
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


# ── set_expiration_timer ──────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_set_expiration_timer_dm():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("set_expiration_timer", {
        "recipient": "+19999999999", "expiration_seconds": 86400
    })
    data = json.loads(result[0].text)
    assert data["seconds"] == 86400


@respx.mock
@pytest.mark.asyncio
async def test_tool_set_expiration_timer_group():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("set_expiration_timer", {
        "group_id": "grp==", "expiration_seconds": 0
    })
    data = json.loads(result[0].text)
    assert data["seconds"] == 0


@pytest.mark.asyncio
async def test_tool_set_expiration_timer_missing_param():
    result = await call_tool("set_expiration_timer", {})
    assert "Error" in result[0].text


# ── receive_messages with message data ───────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_receive_messages_with_data():
    msg_payload = {
        "envelope": {
            "source": "+12223334444",
            "sourceNumber": "+12223334444",
            "sourceUuid": "uuid-x",
            "sourceName": "Tester",
            "sourceDevice": 1,
            "timestamp": 1700000000000,
            "dataMessage": {
                "timestamp": 1700000000000,
                "message": "live message",
                "expiresInSeconds": 0,
                "viewOnce": False,
            },
        },
        "account": "+10000000000",
    }
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([msg_payload])))
    result = await call_tool("receive_messages", {"timeout": 1})
    data = json.loads(result[0].text)
    assert len(data) == 1
    assert data[0]["body"] == "live message"
    assert data[0]["sender"] == "+12223334444"


@pytest.mark.asyncio
async def test_tool_receive_messages_invalid_timeout():
    result = await call_tool("receive_messages", {"timeout": "bad"})
    assert "Error" in result[0].text


# ── get_unread auto-marks as read ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_get_unread_marks_as_read():
    from datetime import datetime as _dt
    _store_mod.init_db()
    _store_mod.save_message(Message(
        id="ur1", sender="+12223334444", body="unread msg",
        timestamp=_dt(2024, 1, 1), is_read=False,
    ))
    assert _store_mod.get_unread_messages(own_number="+10000000000") != []
    result = await call_tool("get_unread", {})
    data = json.loads(result[0].text)
    assert len(data["messages"]) == 1
    assert data["has_more"] is False
    # Now the store should show it as read
    assert _store_mod.get_unread_messages(own_number="+10000000000") == []


# ── get_user_status ───────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_get_user_status():
    status_result = [{"recipient": "+19999999999", "isRegistered": True}]
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok(status_result)))
    result = await call_tool("get_user_status", {"recipients": ["+19999999999"]})
    data = json.loads(result[0].text)
    assert data[0]["isRegistered"] is True


@pytest.mark.asyncio
async def test_tool_get_user_status_missing_param():
    result = await call_tool("get_user_status", {})
    assert "Error" in result[0].text


# ── send_sync_request ─────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_send_sync_request():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("send_sync_request", {})
    data = json.loads(result[0].text)
    assert data["status"] == "sync requested"


# ── list_contacts search filter ───────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_list_contacts_search():
    contacts_result = [
        {"number": "+11111111111", "name": "Alice"},
        {"number": "+12222222222", "name": "Bob"},
    ]
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok(contacts_result)))
    result = await call_tool("list_contacts", {"search": "alice"})
    data = json.loads(result[0].text)
    assert len(data) == 1
    assert data[0]["number"] == "+11111111111"


# ── search_messages offset ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_search_messages_offset():
    import signal_mcp.store as _store_mod
    from signal_mcp.models import Message
    from datetime import datetime
    _store_mod.init_db()
    _store_mod.save_message(Message(id="s1", sender="+1", body="keyword first",
                                    timestamp=datetime(2024, 6, 1, 12, 0, 0)))
    _store_mod.save_message(Message(id="s2", sender="+1", body="keyword second",
                                    timestamp=datetime(2024, 6, 1, 11, 0, 0)))
    result = await call_tool("search_messages", {"query": "keyword", "limit": 10, "offset": 1})
    data = json.loads(result[0].text)
    assert len(data) == 1


# ── react_to_message remove ───────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_react_remove():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("react_to_message", {
        "target_author": "+1", "target_timestamp": 123, "emoji": "👍",
        "recipient": "+2", "remove": True,
    })
    data = json.loads(result[0].text)
    assert data["status"] == "reaction removed"


# ── pin_message / unpin_message ───────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_pin_message():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("pin_message", {
        "target_author": "+1", "target_timestamp": 123, "group_id": "grp==",
    })
    data = json.loads(result[0].text)
    assert data["status"] == "message pinned"


@pytest.mark.asyncio
async def test_tool_pin_message_missing_conversation():
    result = await call_tool("pin_message", {"target_author": "+1", "target_timestamp": 123})
    assert "Error" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_unpin_message():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("unpin_message", {
        "target_author": "+1", "target_timestamp": 123, "recipient": "+2",
    })
    data = json.loads(result[0].text)
    assert data["status"] == "message unpinned"


# ── admin_delete_message ──────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_admin_delete_message():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("admin_delete_message", {
        "group_id": "grp==", "target_author": "+1", "target_timestamp": 123,
    })
    data = json.loads(result[0].text)
    assert data["status"] == "message deleted by admin"


@pytest.mark.asyncio
async def test_tool_admin_delete_missing_param():
    result = await call_tool("admin_delete_message", {"group_id": "grp=="})
    assert "Error" in result[0].text


# ── send_contacts_sync ────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_send_contacts_sync():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("send_contacts_sync", {})
    data = json.loads(result[0].text)
    assert "synced" in data["status"]


# ── update_device ─────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_update_device():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("update_device", {"device_id": 2, "name": "My Mac"})
    data = json.loads(result[0].text)
    assert data["status"] == "device updated"
    assert data["name"] == "My Mac"


@pytest.mark.asyncio
async def test_tool_update_device_missing_param():
    result = await call_tool("update_device", {"device_id": 2})
    assert "Error" in result[0].text


# ── mark_as_unread ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_mark_as_unread():
    import signal_mcp.store as _store_mod
    from signal_mcp.models import Message
    from datetime import datetime
    _store_mod.init_db()
    _store_mod.save_message(Message(id="mu1", sender="+1", body="hi",
                                    timestamp=datetime(2024, 1, 1)))
    _store_mod.mark_as_read(["mu1"])
    result = await call_tool("mark_as_unread", {"message_ids": ["mu1"]})
    data = json.loads(result[0].text)
    assert data["count"] == 1
    assert data["status"] == "marked as unread"


@pytest.mark.asyncio
async def test_tool_mark_as_unread_missing_param():
    result = await call_tool("mark_as_unread", {})
    assert "Error" in result[0].text


# ── get_avatar ────────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_get_avatar():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"base64": "imgdata"})))
    result = await call_tool("get_avatar", {"identifier": "+19999999999"})
    data = json.loads(result[0].text)
    assert data["base64"] == "imgdata"
    assert data["has_avatar"] is True


# ── send_message_request_response ────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_send_message_request_response_accept():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("send_message_request_response", {"sender": "+1", "accept": True})
    data = json.loads(result[0].text)
    assert "accepted" in data["status"]


@respx.mock
@pytest.mark.asyncio
async def test_tool_send_message_request_response_decline():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("send_message_request_response", {"sender": "+1", "accept": False})
    data = json.loads(result[0].text)
    assert "declined" in data["status"]


# ── create_poll ───────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_create_poll():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 123})))
    result = await call_tool("create_poll", {
        "question": "Best day?", "options": ["Mon", "Fri"], "group_id": "grp==",
    })
    data = json.loads(result[0].text)
    assert data["status"] == "poll created"


@pytest.mark.asyncio
async def test_tool_create_poll_too_few_options():
    result = await call_tool("create_poll", {
        "question": "Q?", "options": ["Only one"], "group_id": "grp==",
    })
    assert "Error" in result[0].text


@pytest.mark.asyncio
async def test_tool_create_poll_missing_conversation():
    result = await call_tool("create_poll", {"question": "Q?", "options": ["A", "B"]})
    assert "Error" in result[0].text


# ── vote_poll ─────────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_vote_poll():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("vote_poll", {
        "target_author": "+1", "target_timestamp": 123,
        "poll_id": 1, "votes": [0], "group_id": "grp==",
    })
    data = json.loads(result[0].text)
    assert data["status"] == "vote sent"


# ── terminate_poll ────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_terminate_poll():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("terminate_poll", {
        "target_author": "+1", "target_timestamp": 123,
        "poll_id": 1, "group_id": "grp==",
    })
    data = json.loads(result[0].text)
    assert data["status"] == "poll terminated"


# ── send_attachment multiple paths ───────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_send_attachment_paths_array(tmp_path):
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("x")
    f2.write_text("y")
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 1})))
    result = await call_tool("send_attachment", {
        "recipient": "+19999999999",
        "paths": [str(f1), str(f2)],
    })
    data = json.loads(result[0].text)
    assert data["status"] == "sent"


@pytest.mark.asyncio
async def test_tool_send_attachment_no_path():
    result = await call_tool("send_attachment", {"recipient": "+1"})
    assert "Error" in result[0].text


# ── sendReceipt fix ───────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_send_read_receipt_uses_sendReceipt():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("send_read_receipt", {"sender": "+19999999999", "timestamps": [100, 200]})
    assert "sent" in result[0].text


# ── get_sticker ───────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_get_sticker():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"base64": "xyz"})))
    result = await call_tool("get_sticker", {"pack_id": "deadbeef", "sticker_id": 2})
    data = json.loads(result[0].text)
    assert data["base64"] == "xyz"


@pytest.mark.asyncio
async def test_tool_get_sticker_missing_params():
    result = await call_tool("get_sticker", {"pack_id": "abc"})
    assert "Error" in result[0].text


# ── upload_sticker_pack ───────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_upload_sticker_pack(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"url": "https://signal.art/x"})))
    result = await call_tool("upload_sticker_pack", {"path": str(manifest)})
    data = json.loads(result[0].text)
    assert "signal.art" in data["url"]


@pytest.mark.asyncio
async def test_tool_upload_sticker_pack_missing_path():
    result = await call_tool("upload_sticker_pack", {})
    assert "Error" in result[0].text


# ── list_accounts ─────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_list_accounts():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([{"number": "+491739048003"}])))
    result = await call_tool("list_accounts", {})
    data = json.loads(result[0].text)
    assert "+491739048003" in data


@pytest.mark.asyncio
async def test_list_accounts_not_daemon_free(monkeypatch):
    """list_accounts calls signal-cli listAccounts and must NOT be in _DAEMON_FREE."""
    import signal_mcp.server as server_mod
    assert "list_accounts" not in server_mod._DAEMON_FREE, (
        "list_accounts requires the daemon — it must not be in _DAEMON_FREE"
    )


# ── update_account ────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_update_account():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("update_account", {"device_name": "My Mac"})
    data = json.loads(result[0].text)
    assert data["status"] == "account updated"


# ── set_pin / remove_pin ──────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_set_pin():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("set_pin", {"pin": "1234"})
    data = json.loads(result[0].text)
    assert data["status"] == "PIN set"


@pytest.mark.asyncio
async def test_tool_set_pin_missing():
    result = await call_tool("set_pin", {})
    assert "Error" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_remove_pin():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("remove_pin", {})
    data = json.loads(result[0].text)
    assert data["status"] == "PIN removed"


# ── receive_messages falls back to store when service is running ──────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_receive_messages_service_conflict_falls_back(monkeypatch):
    from signal_mcp.models import Message
    from datetime import datetime
    import signal_mcp.server as _srv

    # Simulate "already being received" error from daemon
    async def _fail_receive(*a, **kw):
        from signal_mcp.client import SignalError
        raise SignalError("Receive command cannot be used if messages are already being received.")

    monkeypatch.setattr(_srv._client, "receive_messages", _fail_receive)

    result = await call_tool("receive_messages", {})
    data = json.loads(result[0].text)
    assert "note" in data
    assert "service" in data["note"].lower()
    assert "messages" in data


# ── _freshen_store / service-aware get_unread ─────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_get_unread_no_service_includes_warning(monkeypatch):
    import signal_mcp.server as _srv
    from signal_mcp.config import is_service_installed
    monkeypatch.setattr("signal_mcp.server.is_service_installed", lambda: False)
    # receive_messages succeeds (no service running conflict)
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([])))
    result = await call_tool("get_unread", {})
    data = json.loads(result[0].text)
    assert "_warning" in data
    assert "install-service" in data["_warning"]
    assert "messages" in data


@respx.mock
@pytest.mark.asyncio
async def test_get_unread_with_service_no_warning(monkeypatch):
    monkeypatch.setattr("signal_mcp.server.is_service_installed", lambda: True)
    result = await call_tool("get_unread", {})
    data = json.loads(result[0].text)
    assert "_warning" not in data
    assert "messages" in data


@respx.mock
@pytest.mark.asyncio
async def test_list_conversations_returns_list(monkeypatch):
    """list_conversations returns a plain list — no freshen, no _warning."""
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([])))
    result = await call_tool("list_conversations", {})
    data = json.loads(result[0].text)
    assert isinstance(data, list)
    assert "_warning" not in data


# ── prune_store ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prune_store_calls_store(monkeypatch):
    monkeypatch.setattr(_store_mod, "prune_old_messages", lambda days: 42)
    result = await call_tool("prune_store", {"days": 90})
    data = json.loads(result[0].text)
    assert data["deleted"] == 42
    assert data["older_than_days"] == 90


@pytest.mark.asyncio
async def test_prune_store_default_days(monkeypatch):
    captured = {}
    def fake_prune(days):
        captured["days"] = days
        return 0
    monkeypatch.setattr(_store_mod, "prune_old_messages", fake_prune)
    await call_tool("prune_store", {})
    assert captured["days"] == 180


@pytest.mark.asyncio
async def test_prune_store_rejects_zero_days():
    result = await call_tool("prune_store", {"days": 0})
    assert result[0].text.startswith("Error:")


# ── start/finish_change_number + submit_rate_limit_challenge ──────────────────

@respx.mock
@pytest.mark.asyncio
async def test_start_change_number():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("start_change_number", {"number": "+12025551234"})
    data = json.loads(result[0].text)
    assert data["status"] == "verification code sent"
    req_body = json.loads(respx.calls[-1].request.content)
    assert req_body["method"] == "startChangeNumber"
    assert req_body["params"]["number"] == "+12025551234"


@respx.mock
@pytest.mark.asyncio
async def test_start_change_number_voice():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await call_tool("start_change_number", {"number": "+12025551234", "voice": True})
    req_body = json.loads(respx.calls[-1].request.content)
    assert req_body["params"]["voice"] is True


@respx.mock
@pytest.mark.asyncio
async def test_finish_change_number():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("finish_change_number", {
        "number": "+12025551234", "verification_code": "123456"
    })
    data = json.loads(result[0].text)
    assert data["status"] == "number changed"
    req_body = json.loads(respx.calls[-1].request.content)
    assert req_body["method"] == "finishChangeNumber"
    assert req_body["params"]["verificationCode"] == "123456"


@respx.mock
@pytest.mark.asyncio
async def test_submit_rate_limit_challenge():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("submit_rate_limit_challenge", {
        "challenge": "abc123", "captcha": "signalcaptcha://token"
    })
    data = json.loads(result[0].text)
    assert data["status"] == "challenge submitted"
    req_body = json.loads(respx.calls[-1].request.content)
    assert req_body["method"] == "submitRateLimitChallenge"
    assert req_body["params"]["challenge"] == "abc123"


# ── Bug-fix regression tests ──────────────────────────────────────────────────

# Bug 2: get_unread limit+1 probe — the extra message must not be silently consumed
@pytest.mark.asyncio
async def test_get_unread_has_more_does_not_consume_extra():
    """When exactly limit+1 unread messages exist, the (limit+1)th must remain unread."""
    from datetime import timedelta
    _store_mod.init_db()
    now = datetime(2024, 6, 1, 12, 0, 0)
    # Insert 3 unread messages; call with limit=2 → expects has_more=True
    for i in range(3):
        _store_mod.save_message(Message(
            id=f"hm_{i}",
            sender="+12223334444",
            body=f"msg {i}",
            timestamp=now + timedelta(seconds=i),
            is_read=False,
        ))
    result = await call_tool("get_unread", {"limit": 2})
    data = json.loads(result[0].text)
    assert data["has_more"] is True
    assert len(data["messages"]) == 2
    # Only 2 messages marked read; the 3rd must still be unread
    remaining = _store_mod.get_unread_messages(own_number="+10000000000")
    assert len(remaining) == 1, "The (limit+1)th message must NOT be consumed"


@pytest.mark.asyncio
async def test_get_unread_has_more_false_when_exact_limit():
    """When exactly limit messages exist, has_more must be False."""
    _store_mod.init_db()
    now = datetime(2024, 6, 1, 12, 0, 0)
    from datetime import timedelta
    for i in range(2):
        _store_mod.save_message(Message(
            id=f"exact_{i}",
            sender="+12223334444",
            body=f"msg {i}",
            timestamp=now + timedelta(seconds=i),
            is_read=False,
        ))
    result = await call_tool("get_unread", {"limit": 2})
    data = json.loads(result[0].text)
    assert data["has_more"] is False
    assert len(data["messages"]) == 2
    # Both messages marked read
    assert _store_mod.get_unread_messages(own_number="+10000000000") == []


# Bug 5: dead mark_as_read block in get_conversation — verify no double-marking
@respx.mock
@pytest.mark.asyncio
async def test_get_conversation_marks_incoming_as_read():
    """get_conversation must mark incoming messages as read exactly once."""
    _store_mod.init_db()
    _store_mod.save_message(Message(
        id="cv_in1", sender="+12223334444", body="hello conv",
        timestamp=datetime(2024, 6, 1), is_read=False,
    ))
    assert len(_store_mod.get_unread_messages(own_number="+10000000000")) == 1
    result = await call_tool("get_conversation", {"recipient": "+12223334444"})
    data = json.loads(result[0].text)
    assert len(data["messages"]) == 1
    # After viewing the conversation, message must be marked read
    assert _store_mod.get_unread_messages(own_number="+10000000000") == []


# store_stats unread count must match get_unread_messages
@pytest.mark.asyncio
async def test_store_stats_unread_count_consistent():
    """store_stats unread_messages must match get_unread_messages count."""
    own = "+10000000000"
    _store_mod.init_db()
    _store_mod.save_message(Message(
        id="stat_recv", sender="+2", body="hi", timestamp=datetime(2024, 6, 1),
    ))
    # Outgoing — must not count as unread
    _store_mod.save_message(Message(
        id="stat_sent", sender=own, recipient="+2", body="hello",
        timestamp=datetime(2024, 6, 1), is_read=True,
    ))
    result = await call_tool("store_stats", {})
    stats = json.loads(result[0].text)
    unread_list = _store_mod.get_unread_messages(own_number=own)
    assert stats["unread_messages"] == len(unread_list) == 1
