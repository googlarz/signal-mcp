"""Tests for SignalClient using mocked signal-cli daemon."""

import json
import pytest
import respx
import httpx
from unittest.mock import patch

import signal_mcp.store as _store_mod
from signal_mcp.client import SignalClient, SignalError
from signal_mcp.config import DAEMON_URL
from signal_mcp.models import Contact, Group, GroupMember, Message


def rpc_ok(result) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def rpc_err(message: str, code: int = -1) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}}


@pytest.fixture(autouse=True)
def reset_store(monkeypatch, tmp_path):
    """Redirect store to temp DB for each test."""
    monkeypatch.setattr(_store_mod, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(_store_mod, "_initialized", False)
    # Close any cached thread-local connection so next call reconnects to the new path
    if getattr(_store_mod._thread_local, "conn", None) is not None:
        _store_mod._thread_local.conn.close()
        _store_mod._thread_local.conn = None


@pytest.fixture
def client():
    return SignalClient(account="+10000000000")


@respx.mock
@pytest.mark.asyncio
async def test_send_message(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 1234567890})))
    result = await client.send_message("+19999999999", "Hello!")
    assert result.success is True
    assert result.timestamp == 1234567890
    assert result.recipient == "+19999999999"


@respx.mock
@pytest.mark.asyncio
async def test_send_group_message(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 999})))
    result = await client.send_group_message("group123==", "Hi group!")
    assert result.success is True
    assert result.recipient == "group123=="


@respx.mock
@pytest.mark.asyncio
async def test_send_message_rpc_error(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_err("User not registered")))
    with pytest.raises(SignalError, match="User not registered"):
        await client.send_message("+19999999999", "Hi")


@respx.mock
@pytest.mark.asyncio
async def test_send_message_connection_error(client):
    respx.post(DAEMON_URL).mock(side_effect=httpx.ConnectError("refused"))
    # With auto-restart, ensure_daemon is called on first ConnectError; mock it out
    with patch.object(client, "ensure_daemon", return_value=None):
        with pytest.raises(SignalError):
            await client.send_message("+19999999999", "Hi")


@respx.mock
@pytest.mark.asyncio
async def test_list_contacts(client):
    contacts_data = [
        {
            "number": "+11111111111", "uuid": "uuid-1",
            "name": "Alice", "givenName": None, "familyName": None,
            "about": "Hey", "isBlocked": False,
            "profile": {"givenName": "Alice", "familyName": "Smith", "about": "Hey"},
        },
        {
            "number": "+12222222222", "uuid": "uuid-2",
            "name": "", "givenName": None, "familyName": None,
            "about": None, "isBlocked": True,
            "profile": {"givenName": "Bob", "familyName": None, "about": None},
        },
    ]
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok(contacts_data)))
    contacts = await client.list_contacts()
    assert len(contacts) == 2
    assert contacts[0].number == "+11111111111"
    assert contacts[0].display_name == "Alice"
    assert contacts[1].blocked is True
    assert contacts[1].display_name == "Bob"  # falls back to profile given name


@respx.mock
@pytest.mark.asyncio
async def test_list_groups(client):
    groups_data = [
        {
            "id": "abc123==",
            "name": "Family",
            "description": "Our family group",
            "isMember": True,
            "isBlocked": False,
            "members": [
                {"uuid": "uuid-1", "number": "+11111111111", "isAdmin": False},
                {"uuid": "uuid-2", "number": None, "isAdmin": True},
            ],
            "pendingMembers": [],
            "requestingMembers": [],
            "admins": [{"uuid": "uuid-2"}],
            "banned": [],
            "permissionAddMember": "ONLY_ADMINS",
            "permissionEditDetails": "ONLY_ADMINS",
            "permissionSendMessage": "EVERY_MEMBER",
            "groupInviteLink": None,
        }
    ]
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok(groups_data)))
    groups = await client.list_groups()
    assert len(groups) == 1
    assert groups[0].name == "Family"
    assert groups[0].member_count == 2
    assert groups[0].members[0].number == "+11111111111"
    assert groups[0].members[1].is_admin is True


@respx.mock
@pytest.mark.asyncio
async def test_receive_messages_empty(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([])))
    messages = await client.receive_messages(timeout=1)
    assert messages == []


@respx.mock
@pytest.mark.asyncio
async def test_receive_messages_with_data(client):
    envelopes = [
        {
            "envelope": {
                "source": "+13333333333",
                "dataMessage": {
                    "timestamp": 1700000000000,
                    "message": "Test message",
                    "attachments": [],
                },
            }
        }
    ]
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok(envelopes)))
    messages = await client.receive_messages(timeout=1)
    assert len(messages) == 1
    assert messages[0].sender == "+13333333333"
    assert messages[0].body == "Test message"


@respx.mock
@pytest.mark.asyncio
async def test_set_typing(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.set_typing("+19999999999")


@respx.mock
@pytest.mark.asyncio
async def test_set_typing_stop(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.set_typing("+19999999999", stop=True)


@respx.mock
@pytest.mark.asyncio
async def test_react_to_message_dm(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.react_to_message(
        target_author="+11111111111",
        target_timestamp=1700000000000,
        emoji="👍",
        recipient="+19999999999",
    )
    body = route.calls[0].request.read()
    import json
    params = json.loads(body)["params"]
    assert params["recipient"] == ["+19999999999"]
    assert "groupId" not in params


@respx.mock
@pytest.mark.asyncio
async def test_react_to_message_group(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.react_to_message(
        target_author="+11111111111",
        target_timestamp=1700000000000,
        emoji="❤️",
        group_id="grp123==",
    )
    body = route.calls[0].request.read()
    import json
    params = json.loads(body)["params"]
    assert params["groupId"] == "grp123=="
    assert "recipient" not in params


@respx.mock
@pytest.mark.asyncio
async def test_react_to_message_requires_target(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    from signal_mcp.client import SignalError
    with pytest.raises(SignalError):
        await client.react_to_message(
            target_author="+11111111111",
            target_timestamp=1700000000000,
            emoji="👍",
        )


@respx.mock
@pytest.mark.asyncio
async def test_block_contact(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.block_contact("+19999999999")


@respx.mock
@pytest.mark.asyncio
async def test_send_attachment(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 5555})))
    result = await client.send_attachment("+19999999999", "/tmp/photo.jpg", caption="Look!")
    assert result.success is True


def test_contact_display_name_prefers_name():
    c = Contact(number="+1", name="Alice", given_name="A", family_name="Smith")
    assert c.display_name == "Alice"


def test_contact_display_name_uses_profile_full_name():
    c = Contact(number="+1", name=None, given_name="Bob", family_name="Jones")
    assert c.display_name == "Bob Jones"


def test_contact_display_name_given_only():
    c = Contact(number="+1", name=None, given_name="Bob", family_name=None)
    assert c.display_name == "Bob"


def test_contact_display_name_falls_back_to_profile_name():
    c = Contact(number="+1", name=None, given_name=None, family_name=None, profile_name="alice99")
    assert c.display_name == "alice99"


def test_contact_display_name_falls_back_to_number():
    c = Contact(number="+1", name=None, given_name=None, family_name=None)
    assert c.display_name == "+1"


def test_group_member_count():
    g = Group(id="x", name="Test", members=[
        GroupMember(uuid="a"), GroupMember(uuid="b"),
    ])
    assert g.member_count == 2


def test_message_to_dict():
    from datetime import datetime
    msg = Message(id="1", sender="+1", body="hi", timestamp=datetime(2024, 1, 1))
    d = msg.to_dict()
    assert d["sender"] == "+1"
    assert d["body"] == "hi"
    assert d["attachments"] == []


@respx.mock
@pytest.mark.asyncio
async def test_send_message_saves_to_store(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 1700000000000})))
    await client.send_message("+19999999999", "saved!")
    msgs = _store_mod.get_conversation("+19999999999")
    assert any(m.body == "saved!" for m in msgs)


@respx.mock
@pytest.mark.asyncio
async def test_send_group_message_saves_to_store(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 1700000000000})))
    await client.send_group_message("grp123", "group saved!")
    msgs = _store_mod.get_conversation("grp123")
    assert any(m.body == "group saved!" for m in msgs)


@respx.mock
@pytest.mark.asyncio
async def test_delete_message(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.delete_message("+19999999999", 1700000000000)


@respx.mock
@pytest.mark.asyncio
async def test_send_read_receipt(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.send_read_receipt("+19999999999", [1700000000000, 1700000001000])


@respx.mock
@pytest.mark.asyncio
async def test_update_contact(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.update_contact("+19999999999", "Alice")


@respx.mock
@pytest.mark.asyncio
async def test_leave_group(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.leave_group("grp123==")


@respx.mock
@pytest.mark.asyncio
async def test_list_identities(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([
        {"number": "+19999999999", "trustLevel": "TRUSTED_VERIFIED", "safetyNumber": "12345"}
    ])))
    result = await client.list_identities()
    assert len(result) == 1
    assert result[0]["number"] == "+19999999999"


@respx.mock
@pytest.mark.asyncio
async def test_trust_identity(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.trust_identity("+19999999999", trust_all_known=True)


# ── RPC param shape tests ─────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_send_message_rpc_params(client, tmp_path, monkeypatch):
    """send_message must pass recipient as a list."""
    import signal_mcp.store as s
    monkeypatch.setattr(s, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(s, "_initialized", False)
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 1})))
    await client.send_message("+19999999999", "hi")
    import json
    params = json.loads(route.calls[0].request.read())["params"]
    assert params["recipient"] == ["+19999999999"]
    assert params["message"] == "hi"


@respx.mock
@pytest.mark.asyncio
async def test_send_group_attachment_rpc_params(client):
    """send_group_attachment must use 'send' method and groupId as a string."""
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 1})))
    await client.send_group_attachment("grp123==", "/tmp/file.jpg")
    import json
    body = json.loads(route.calls[0].request.read())
    assert body["method"] == "send"
    assert body["params"]["groupId"] == "grp123=="
    assert isinstance(body["params"]["attachment"], list)


@respx.mock
@pytest.mark.asyncio
async def test_send_read_receipt_rpc_params(client):
    """send_read_receipt must pass recipient as a list."""
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.send_read_receipt("+19999999999", [111, 222])
    import json
    params = json.loads(route.calls[0].request.read())["params"]
    assert params["recipient"] == ["+19999999999"]
    assert params["targetTimestamps"] == [111, 222]


@respx.mock
@pytest.mark.asyncio
async def test_send_attachment_expands_tilde(client):
    """send_attachment must expand ~ in paths."""
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 1})))
    await client.send_attachment("+19999999999", "~/Downloads/file.jpg")
    import json
    params = json.loads(route.calls[0].request.read())["params"]
    assert "~" not in params["attachment"][0]
    assert params["attachment"][0].startswith("/")


@respx.mock
@pytest.mark.asyncio
async def test_update_group_rpc_params(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.update_group("grp1==", name="New Name", add_members=["+1999"])
    import json
    params = json.loads(route.calls[0].request.read())["params"]
    assert params["groupId"] == "grp1=="
    assert params["name"] == "New Name"
    assert params["member"] == ["+1999"]


@respx.mock
@pytest.mark.asyncio
async def test_set_expiration_timer_dm(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.set_expiration_timer(recipient="+1999", expiration=86400)
    import json
    params = json.loads(route.calls[0].request.read())["params"]
    assert params["recipient"] == "+1999"
    assert params["expiration"] == 86400


@respx.mock
@pytest.mark.asyncio
async def test_set_expiration_timer_group(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.set_expiration_timer(group_id="grp1==", expiration=3600)
    import json
    params = json.loads(route.calls[0].request.read())["params"]
    assert params["groupId"] == "grp1=="
    assert params["expiration"] == 3600


@respx.mock
@pytest.mark.asyncio
async def test_send_message_with_quote_rpc_params(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 1})))
    await client.send_message("+19999999999", "reply!", quote_author="+11111111111", quote_timestamp=1700000000000)
    import json
    params = json.loads(route.calls[0].request.read())["params"]
    assert params["quoteAuthor"] == "+11111111111"
    assert params["quoteTimestamp"] == 1700000000000


@respx.mock
@pytest.mark.asyncio
async def test_send_group_message_with_mentions_rpc_params(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 2})))
    mentions = [{"start": 0, "length": 12, "author": "+19999999999"}]
    await client.send_group_message("grp1==", "hello!", mentions=mentions)
    import json
    params = json.loads(route.calls[0].request.read())["params"]
    assert params["mention"] == mentions


@respx.mock
@pytest.mark.asyncio
async def test_send_attachment_view_once_rpc_params(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 3})))
    await client.send_attachment("+19999999999", "/tmp/photo.jpg", view_once=True)
    import json
    params = json.loads(route.calls[0].request.read())["params"]
    assert params["viewOnce"] is True


@respx.mock
@pytest.mark.asyncio
async def test_update_group_admin_rpc_params(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.update_group("grp1==", add_admins=["+1111"], remove_admins=["+2222"])
    import json
    params = json.loads(route.calls[0].request.read())["params"]
    assert params["admin"] == ["+1111"]
    assert params["removeAdmin"] == ["+2222"]


@respx.mock
@pytest.mark.asyncio
async def test_edit_message_dm_rpc_params(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.edit_message(1700000000000, "new text", recipient="+19999999999")
    import json
    body = json.loads(route.calls[0].request.read())
    assert body["method"] == "editMessage"
    params = body["params"]
    assert params["targetTimestamp"] == 1700000000000
    assert params["message"] == "new text"
    assert params["recipient"] == ["+19999999999"]


@respx.mock
@pytest.mark.asyncio
async def test_edit_message_group_rpc_params(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.edit_message(1700000000000, "fixed", group_id="grp1==")
    import json
    params = json.loads(route.calls[0].request.read())["params"]
    assert params["groupId"] == "grp1=="


@respx.mock
@pytest.mark.asyncio
async def test_edit_message_requires_target(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    with pytest.raises(SignalError):
        await client.edit_message(1700000000000, "oops")


@respx.mock
@pytest.mark.asyncio
async def test_send_note_to_self(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 99})))
    result = await client.send_note_to_self("reminder")
    import json
    params = json.loads(route.calls[0].request.read())["params"]
    assert params["recipient"] == ["+10000000000"]  # own account
    assert result.success is True


@respx.mock
@pytest.mark.asyncio
async def test_receive_delivery_receipt_not_saved_to_store(client):
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
    messages = await client.receive_messages(timeout=1)
    assert len(messages) == 1
    assert messages[0].receipt_type == "DELIVERY"
    # Receipt must not be stored
    stored = _store_mod.get_conversation("+13333333333")
    assert stored == []


@respx.mock
@pytest.mark.asyncio
async def test_receive_message_parses_quote(client):
    envelopes = [
        {
            "envelope": {
                "source": "+13333333333",
                "dataMessage": {
                    "timestamp": 1700000000001,
                    "message": "replying",
                    "attachments": [],
                    "quote": {"id": 1700000000000, "author": "+19999999999", "text": "original"},
                },
            }
        }
    ]
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok(envelopes)))
    messages = await client.receive_messages(timeout=1)
    assert messages[0].quote_id == "1700000000000"


@respx.mock
@pytest.mark.asyncio
async def test_send_group_attachment_saves_to_store(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 1700000000000})))
    await client.send_group_attachment("grp123", "/tmp/photo.jpg", caption="look!")
    msgs = _store_mod.get_conversation("grp123")
    assert any(m.group_id == "grp123" for m in msgs)


@respx.mock
@pytest.mark.asyncio
async def test_edit_message_updates_store(client):
    """edit_message must update the local store body."""
    from datetime import datetime as _dt
    ts = _dt(2024, 6, 1, 12, 0, 0)
    ts_ms = int(ts.timestamp() * 1000)
    _store_mod.save_message(Message(id=f"sent_{ts_ms}_+19999999999", sender="+10000000000",
                                    recipient="+19999999999", body="original", timestamp=ts))
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.edit_message(ts_ms, "edited", recipient="+19999999999")
    msgs = _store_mod.get_conversation("+19999999999")
    assert msgs[0].body == "edited"


@respx.mock
@pytest.mark.asyncio
async def test_rpc_retries_on_connect_error(client):
    """_rpc retries once on ConnectError before giving up."""
    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("refused")

    respx.post(DAEMON_URL).mock(side_effect=side_effect)
    with patch.object(client, "ensure_daemon", return_value=None):
        with pytest.raises(SignalError):
            await client.send_message("+19999999999", "hi")
    assert call_count == 2  # initial + 1 retry


# ── New features ──────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_send_sticker_rpc_params(client):
    """send_sticker passes pack_id:sticker_id as sticker param."""
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 5})))
    result = await client.send_sticker("+19999999999", "aabbcc", 3)
    body = route.calls[0].request.read()
    import json
    params = json.loads(body)["params"]
    assert params["sticker"] == "aabbcc:3"
    assert params["recipient"] == ["+19999999999"]
    assert result.success is True


@respx.mock
@pytest.mark.asyncio
async def test_send_group_sticker_rpc_params(client):
    """send_group_sticker passes groupId and sticker param."""
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 6})))
    await client.send_group_sticker("grp1==", "aabbcc", 0)
    body = route.calls[0].request.read()
    import json
    params = json.loads(body)["params"]
    assert params["sticker"] == "aabbcc:0"
    assert params["groupId"] == "grp1=="


@pytest.mark.asyncio
async def test_list_attachments_empty(client, tmp_path, monkeypatch):
    import signal_mcp.client as _client_mod
    monkeypatch.setattr(_client_mod, "ATTACHMENT_DIR", tmp_path / "no-such-dir")
    assert client.list_attachments() == []


@pytest.mark.asyncio
async def test_list_attachments_lists_files(client, tmp_path, monkeypatch):
    import signal_mcp.client as _client_mod
    att_dir = tmp_path / "att"
    att_dir.mkdir()
    (att_dir / "img.png").write_bytes(b"z" * 50)
    monkeypatch.setattr(_client_mod, "ATTACHMENT_DIR", att_dir)
    files = client.list_attachments()
    assert len(files) == 1
    assert files[0]["filename"] == "img.png"
    assert files[0]["size"] == 50


@pytest.mark.asyncio
async def test_get_attachment_not_found_raises(client, tmp_path, monkeypatch):
    import signal_mcp.client as _client_mod
    att_dir = tmp_path / "att"
    att_dir.mkdir()
    monkeypatch.setattr(_client_mod, "ATTACHMENT_DIR", att_dir)
    with pytest.raises(SignalError, match="not found"):
        client.get_attachment("ghost.jpg")


@pytest.mark.asyncio
async def test_get_conversation_auto_marks_read(client):
    """get_conversation marks received messages as read."""
    from datetime import datetime
    _store_mod.init_db()
    _store_mod.save_message(Message(
        id="rx1", sender="+19999999999", body="hey",
        timestamp=datetime(2024, 6, 1), is_read=False,
    ))
    msgs_before = _store_mod.get_conversation("+19999999999")
    assert not msgs_before[0].is_read
    await client.get_conversation("+19999999999")
    msgs_after = _store_mod.get_conversation("+19999999999")
    assert msgs_after[0].is_read


@pytest.mark.asyncio
async def test_resolve_name_returns_display_name(client, monkeypatch):
    import signal_mcp.client as _client_mod
    monkeypatch.setattr(_client_mod, "_contact_cache", {"+19999999999": "Bob"})
    assert client.resolve_name("+19999999999") == "Bob"
    assert client.resolve_name("+10000000001") == "+10000000001"  # unknown → number


@pytest.mark.asyncio
async def test_enrich_message_adds_sender_name(client, monkeypatch):
    from datetime import datetime
    import signal_mcp.client as _client_mod
    monkeypatch.setattr(_client_mod, "_contact_cache", {"+19999999999": "Carol"})
    msg = Message(id="x", sender="+19999999999", body="hi", timestamp=datetime(2024, 1, 1))
    d = client._enrich_message(msg)
    assert d["sender_name"] == "Carol"
    assert d["body"] == "hi"


# ── Codex-flagged gap tests ────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_send_sticker_saves_to_store(client):
    """send_sticker persists a record to the local store."""
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 7000})))
    _store_mod.init_db()
    await client.send_sticker("+19999999999", "aabb", 2)
    msgs = _store_mod.get_conversation("+19999999999")
    assert len(msgs) == 1
    assert "sticker" in msgs[0].body


@respx.mock
@pytest.mark.asyncio
async def test_send_group_sticker_saves_to_store(client):
    """send_group_sticker persists a record to the local store."""
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 8000})))
    _store_mod.init_db()
    await client.send_group_sticker("grp1==", "aabb", 0)
    msgs = _store_mod.get_conversation("grp1==")
    assert len(msgs) == 1
    assert "sticker" in msgs[0].body


@pytest.mark.asyncio
async def test_get_attachment_path_traversal_blocked(client, tmp_path, monkeypatch):
    """get_attachment rejects path traversal attempts."""
    import signal_mcp.client as _client_mod
    att_dir = tmp_path / "att"
    att_dir.mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    monkeypatch.setattr(_client_mod, "ATTACHMENT_DIR", att_dir)
    with pytest.raises(SignalError):
        client.get_attachment("../secret.txt")


@pytest.mark.asyncio
async def test_contact_cache_retries_after_failure(client, monkeypatch):
    """_ensure_contact_cache retries on RPC failure (cache_loaded stays False)."""
    import signal_mcp.client as _client_mod
    monkeypatch.setattr(_client_mod, "_contact_cache_loaded", False)
    call_count = 0

    async def failing_list_contacts():
        nonlocal call_count
        call_count += 1
        raise Exception("daemon not ready")

    monkeypatch.setattr(client, "list_contacts", failing_list_contacts)
    await client._ensure_contact_cache()
    # Cache should NOT be marked loaded on failure — allows retry
    assert not _client_mod._contact_cache_loaded
    assert call_count == 1
    # Second call retries
    await client._ensure_contact_cache()
    assert call_count == 2


@pytest.mark.asyncio
async def test_get_conversation_does_not_mark_own_messages_read(client):
    """get_conversation auto-mark-as-read skips outgoing messages."""
    from datetime import datetime
    _store_mod.init_db()
    # Outgoing message (sender == own account)
    _store_mod.save_message(Message(
        id="sent_out", sender="+10000000000", recipient="+19999999999",
        body="hi", timestamp=datetime(2024, 6, 1),
    ))
    await client.get_conversation("+19999999999")
    msgs = _store_mod.get_conversation("+19999999999")
    # Outgoing messages are stored as is_read=1 already; verify they still are
    outgoing = [m for m in msgs if m.sender == "+10000000000"]
    assert all(m.is_read for m in outgoing)


def test_check_signal_cli_version_timeout(monkeypatch):
    """check_signal_cli_version raises RuntimeError on timeout."""
    import subprocess
    import signal_mcp.config as _config_mod

    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="signal-cli", timeout=10)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="timed out"):
        _config_mod.check_signal_cli_version()


def test_check_signal_cli_version_nonzero_exit(monkeypatch):
    """check_signal_cli_version raises RuntimeError on non-zero exit code."""
    import subprocess
    import signal_mcp.config as _config_mod

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: type(
        "R", (), {"returncode": 1, "stdout": "", "stderr": "bad"}
    )())
    with pytest.raises(RuntimeError, match="exited with code 1"):
        _config_mod.check_signal_cli_version()


def test_check_signal_cli_version_not_found(monkeypatch):
    """check_signal_cli_version raises RuntimeError when signal-cli missing."""
    import subprocess
    import signal_mcp.config as _config_mod

    def fake_run(*a, **kw):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="not found"):
        _config_mod.check_signal_cli_version()


# ── Configuration ─────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_get_configuration(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({
        "readReceipts": True, "typingIndicators": True, "linkPreviews": False,
    })))
    result = await client.get_configuration()
    assert result["readReceipts"] is True
    assert result["linkPreviews"] is False


@respx.mock
@pytest.mark.asyncio
async def test_update_configuration(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.update_configuration(read_receipts=False, typing_indicators=True)
    body = respx.calls[0].request.content
    import json as _json
    params = _json.loads(body)["params"]
    assert params["readReceipts"] is False
    assert params["typingIndicators"] is True


@respx.mock
@pytest.mark.asyncio
async def test_update_configuration_no_params(client):
    """update_configuration with no args should not make any RPC call."""
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.update_configuration()
    assert len(respx.calls) == 0


# ── Sticker packs ─────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_list_sticker_packs(client):
    packs = [{"packId": "abc123", "title": "Cool Stickers", "stickers": []}]
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok(packs)))
    result = await client.list_sticker_packs()
    assert len(result) == 1
    assert result[0]["packId"] == "abc123"


@respx.mock
@pytest.mark.asyncio
async def test_add_sticker_pack(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.add_sticker_pack("https://signal.art/addstickers/#pack_id=abc&pack_key=def")
    import json as _json
    params = _json.loads(respx.calls[0].request.content)["params"]
    assert "signal.art" in params["uri"]


# ── Streaming receive ─────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_receive_stream_yields_messages(client):
    """receive_stream should yield messages from repeated polls."""
    import asyncio as _asyncio
    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json=rpc_ok([{
                "envelope": {
                    "source": "+19999999999",
                    "timestamp": 111000,
                    "dataMessage": {"message": "hello", "timestamp": 111000, "attachments": []},
                },
            }]))
        # After first call, raise CancelledError to end the stream
        raise _asyncio.CancelledError()

    respx.post(DAEMON_URL).mock(side_effect=side_effect)

    received = []
    try:
        async for msg in client.receive_stream(poll_interval=1):
            received.append(msg)
    except _asyncio.CancelledError:
        pass

    assert len(received) >= 1
    assert received[0].body == "hello"


# ── Rate limiter ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rate_limiter_allows_burst(client):
    """First N calls within the burst should not sleep."""
    import time
    from signal_mcp.client import _RateLimiter
    rl = _RateLimiter(rate=5, per=60.0)
    start = time.monotonic()
    for _ in range(5):
        await rl.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.1  # burst — should be near-instant


@pytest.mark.asyncio
async def test_rate_limiter_throttles_after_burst():
    """Calls beyond the burst should wait."""
    from signal_mcp.client import _RateLimiter
    rl = _RateLimiter(rate=2, per=1.0)  # 2/sec
    await rl.acquire()
    await rl.acquire()
    # Third call should have to wait ~0.5s
    import time
    start = time.monotonic()
    await rl.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.4  # waited at least 0.4s


# ── E.164 validation ──────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_send_message_rejects_invalid_number(client):
    with pytest.raises(SignalError, match="E.164"):
        await client.send_message("notanumber", "hi")


@respx.mock
@pytest.mark.asyncio
async def test_send_message_accepts_valid_e164(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 999})))
    result = await client.send_message("+12125551234", "hi")
    assert result.success


# ── Identity error hints ───────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_identity_error_includes_hint(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json={
        "jsonrpc": "2.0", "id": 1,
        "error": {"code": -1, "message": "Untrusted identity key for +19999999999"},
    }))
    with pytest.raises(SignalError, match="trust_identity"):
        await client._rpc("send", {})


# ── Enriched list_conversations ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_conversations_enriches_name(monkeypatch, client):
    import signal_mcp.client as _client_mod
    monkeypatch.setattr(_client_mod, "_contact_cache", {"+19999999999": "Alice"})
    monkeypatch.setattr(_client_mod, "_contact_cache_loaded", True)
    _store_mod.init_db()
    _store_mod.save_message(Message(
        id="m1", sender="+19999999999", body="hi",
        timestamp=__import__("datetime").datetime(2024, 1, 1),
    ))
    convs = await client.list_conversations()
    assert convs[0]["name"] == "Alice"


# ── Store management ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clear_local_store(client):
    _store_mod.init_db()
    from datetime import datetime as _dt
    _store_mod.save_message(Message(id="m1", sender="+1", body="a", timestamp=_dt(2024,1,1)))
    _store_mod.save_message(Message(id="m2", sender="+2", body="b", timestamp=_dt(2024,1,2)))
    count = await client.clear_local_store()
    assert count == 2
    assert _store_mod.get_stats()["total_messages"] == 0


@pytest.mark.asyncio
async def test_delete_local_messages(client):
    _store_mod.init_db()
    from datetime import datetime as _dt
    _store_mod.save_message(Message(id="m1", sender="+19999999999", body="a", timestamp=_dt(2024,1,1)))
    _store_mod.save_message(Message(id="m2", sender="+18888888888", body="b", timestamp=_dt(2024,1,2)))
    count = await client.delete_local_messages("+19999999999")
    assert count == 1
    assert _store_mod.get_stats()["total_messages"] == 1


# ── get_unread auto-marks as read ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_unread_messages_marks_as_read(client):
    from datetime import datetime as _dt
    _store_mod.init_db()
    _store_mod.save_message(Message(
        id="unread1", sender="+12223334444", body="hi",
        timestamp=_dt(2024, 1, 1), is_read=False,
    ))
    msgs = await client.get_unread_messages()
    assert len(msgs) == 1
    assert msgs[0].is_read is True
    # Store should now show zero unread
    assert _store_mod.get_unread_messages(own_number=client.account) == []


@pytest.mark.asyncio
async def test_get_unread_messages_empty(client):
    msgs = await client.get_unread_messages()
    assert msgs == []


# ── get_user_status ───────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_get_user_status(client):
    status_payload = [{"recipient": "+19999999999", "isRegistered": True}]
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok(status_payload)))
    result = await client.get_user_status(["+19999999999"])
    assert result[0]["isRegistered"] is True


# ── send_sync_request ─────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_send_sync_request(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.send_sync_request()  # should not raise


# ── _parse_envelope: syncMessage ──────────────────────────────────────────────

def test_parse_envelope_sync_sent_dm(client):
    """syncMessage.sentMessage for a DM is stored as outgoing message."""
    envelope = {
        "envelope": {
            "source": "+19999999999",
            "timestamp": 1717200000000,
            "syncMessage": {
                "sentMessage": {
                    "destination": "+15555555555",
                    "timestamp": 1717200000000,
                    "message": "I sent this from another device",
                    "attachments": [],
                }
            }
        }
    }
    msg = client._parse_envelope(envelope)
    assert msg is not None
    assert msg.body == "I sent this from another device"
    assert msg.recipient == "+15555555555"
    assert msg.is_read is True  # outgoing = already read


def test_parse_envelope_sync_other_skipped(client):
    """syncMessage without sentMessage (e.g. read sync) is skipped."""
    envelope = {
        "envelope": {
            "source": "+19999999999",
            "timestamp": 1717200000000,
            "syncMessage": {"readMessages": [{"timestamp": 123}]}
        }
    }
    msg = client._parse_envelope(envelope)
    assert msg is None


def test_parse_envelope_typing_skipped(client):
    """typingMessage envelopes return None — no crash."""
    envelope = {
        "envelope": {
            "source": "+19999999999",
            "timestamp": 1717200000000,
            "typingMessage": {"action": "STARTED", "timestamp": 1717200000000},
        }
    }
    msg = client._parse_envelope(envelope)
    assert msg is None


def test_parse_envelope_call_skipped(client):
    """callMessage envelopes return None — no crash."""
    envelope = {
        "envelope": {
            "source": "+19999999999",
            "timestamp": 1717200000000,
            "callMessage": {"offerMessage": {"id": 1}},
        }
    }
    msg = client._parse_envelope(envelope)
    assert msg is None


# ── list_contacts search filter ───────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_list_contacts_search_filter(client):
    contacts_payload = [
        {"number": "+11111111111", "name": "Alice"},
        {"number": "+12222222222", "name": "Bob"},
        {"number": "+13333333333", "name": "Alice Smith"},
    ]
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok(contacts_payload)))
    result = await client.list_contacts(search="alice")
    assert len(result) == 2
    assert all("alice" in (c.name or "").lower() for c in result)


@respx.mock
@pytest.mark.asyncio
async def test_list_contacts_no_filter_returns_all(client):
    contacts_payload = [{"number": "+1", "name": "X"}, {"number": "+2", "name": "Y"}]
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok(contacts_payload)))
    result = await client.list_contacts()
    assert len(result) == 2


# ── react_to_message remove ───────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_react_to_message_remove(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.react_to_message("+1", 123, "👍", recipient="+2", remove=True)
    req_body = json.loads(respx.calls[-1].request.content)
    assert req_body["params"]["remove"] is True


# ── pin / unpin message ───────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_pin_message_group(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.pin_message("+1", 123, group_id="grp==")
    req_body = json.loads(respx.calls[-1].request.content)
    assert req_body["method"] == "sendPinMessage"


@respx.mock
@pytest.mark.asyncio
async def test_unpin_message_dm(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.unpin_message("+1", 123, recipient="+2")
    req_body = json.loads(respx.calls[-1].request.content)
    assert req_body["method"] == "sendUnpinMessage"


@pytest.mark.asyncio
async def test_pin_message_missing_conversation(client):
    from signal_mcp.client import SignalError
    with pytest.raises(SignalError):
        await client.pin_message("+1", 123)


# ── admin_delete_message ──────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_admin_delete_message(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.admin_delete_message("+1", 123, "grp==")
    req_body = json.loads(respx.calls[-1].request.content)
    assert req_body["method"] == "sendAdminDelete"
    assert req_body["params"]["groupId"] == "grp=="


# ── send_contacts_sync ────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_send_contacts_sync(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.send_contacts_sync()
    req_body = json.loads(respx.calls[-1].request.content)
    assert req_body["method"] == "sendContacts"


# ── update_device ─────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_update_device(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.update_device(2, "My Mac")
    req_body = json.loads(respx.calls[-1].request.content)
    assert req_body["method"] == "updateDevice"
    assert req_body["params"]["name"] == "My Mac"


# ── reaction envelope skip ────────────────────────────────────────────────────

def test_parse_envelope_reaction_skipped(client):
    """dataMessage.reaction envelopes are not stored as messages."""
    envelope = {
        "envelope": {
            "source": "+19999999999",
            "timestamp": 1717200000000,
            "dataMessage": {
                "timestamp": 1717200000000,
                "reaction": {
                    "emoji": "👍",
                    "targetAuthor": "+10000000000",
                    "targetTimestamp": 1717100000000,
                    "isRemove": False,
                }
            }
        }
    }
    msg = client._parse_envelope(envelope)
    assert msg is None


# ── expiresInSeconds + viewOnce captured ─────────────────────────────────────

def test_parse_envelope_expiry_and_view_once(client):
    envelope = {
        "envelope": {
            "source": "+19999999999",
            "timestamp": 1717200000000,
            "dataMessage": {
                "timestamp": 1717200000000,
                "message": "disappearing",
                "expiresInSeconds": 86400,
                "viewOnce": True,
                "attachments": [],
            }
        }
    }
    msg = client._parse_envelope(envelope)
    assert msg is not None
    assert msg.expires_in_seconds == 86400
    assert msg.view_once is True


# ── attachment width/height/caption ──────────────────────────────────────────

def test_parse_attachments_metadata(client):
    data_message = {
        "attachments": [{
            "contentType": "image/jpeg",
            "filename": "/tmp/photo.jpg",
            "size": 12345,
            "width": 1920,
            "height": 1080,
            "caption": "Look at this!",
        }]
    }
    atts = client._parse_attachments(data_message)
    assert len(atts) == 1
    assert atts[0].width == 1920
    assert atts[0].height == 1080
    assert atts[0].caption == "Look at this!"


# ── send_attachment multiple paths ───────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_send_attachment_multiple_paths(client, tmp_path):
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("x")
    f2.write_text("y")
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 999})))
    result = await client.send_attachment("+19999999999", [str(f1), str(f2)])
    req_body = json.loads(respx.calls[-1].request.content)
    assert len(req_body["params"]["attachment"]) == 2
    assert result.timestamp == 999


# ── mark_as_unread ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_as_unread_client(client):
    import signal_mcp.store as _store_mod
    from signal_mcp.models import Message
    from datetime import datetime
    _store_mod.init_db()
    _store_mod.save_message(Message(id="mu1", sender="+1", body="hi",
                                    timestamp=datetime(2024, 1, 1)))
    _store_mod.mark_as_read(["mu1"])
    await client.mark_as_unread(["mu1"])
    msgs = _store_mod.get_unread_messages(own_number="+99")
    assert any(m.id == "mu1" for m in msgs)


# ── get_avatar ────────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_get_avatar_contact(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"base64": "abc123"})))
    result = await client.get_avatar("+19999999999")
    assert result == "abc123"
    req_body = json.loads(respx.calls[-1].request.content)
    assert req_body["method"] == "getAvatar"
    assert "recipient" in req_body["params"]


@respx.mock
@pytest.mark.asyncio
async def test_get_avatar_group(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"base64": "xyz"})))
    result = await client.get_avatar("grp==")
    req_body = json.loads(respx.calls[-1].request.content)
    assert "groupId" in req_body["params"]


# ── send_message_request_response ────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_send_message_request_accept(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.send_message_request_response("+19999999999", accept=True)
    req_body = json.loads(respx.calls[-1].request.content)
    assert req_body["method"] == "sendMessageRequestResponse"
    assert req_body["params"]["type"] == "accept"


@respx.mock
@pytest.mark.asyncio
async def test_send_message_request_decline(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.send_message_request_response("+19999999999", accept=False)
    req_body = json.loads(respx.calls[-1].request.content)
    assert req_body["params"]["type"] == "delete"


# ── polls ─────────────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_create_poll_group(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 555})))
    result = await client.create_poll("Best day?", ["Mon", "Fri"], group_id="grp==")
    req_body = json.loads(respx.calls[-1].request.content)
    assert req_body["method"] == "sendPollCreate"
    assert req_body["params"]["poll-question"] == "Best day?"
    assert result.timestamp == 555


@respx.mock
@pytest.mark.asyncio
async def test_vote_poll(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.vote_poll("+1", 123, poll_id=1, votes=[0], group_id="grp==")
    req_body = json.loads(respx.calls[-1].request.content)
    assert req_body["method"] == "sendPollVote"
    assert req_body["params"]["poll-answer"] == [0]


@respx.mock
@pytest.mark.asyncio
async def test_terminate_poll(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.terminate_poll("+1", 123, poll_id=1, group_id="grp==")
    req_body = json.loads(respx.calls[-1].request.content)
    assert req_body["method"] == "sendPollTerminate"


@pytest.mark.asyncio
async def test_create_poll_missing_conversation(client):
    with pytest.raises(SignalError):
        await client.create_poll("Q?", ["A", "B"])


# ── group cache ───────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_ensure_group_cache(client):
    groups_payload = [
        {"id": "grpAAA==", "name": "Weekend plans", "members": [], "admins": []},
    ]
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok(groups_payload)))
    await client._ensure_group_cache()
    assert client.resolve_group_name("grpAAA==") == "Weekend plans"
    assert client.resolve_group_name("unknown==") == "unknown=="
