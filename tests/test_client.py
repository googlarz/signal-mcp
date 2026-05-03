"""Tests for SignalClient using mocked signal-cli daemon."""

import pytest
import respx
import httpx

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
    with pytest.raises(SignalError, match="daemon not running"):
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
    with pytest.raises(SignalError, match="daemon not running"):
        await client.send_message("+19999999999", "hi")
    assert call_count == 2  # initial + 1 retry
