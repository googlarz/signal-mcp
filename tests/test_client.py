"""Tests for SignalClient using mocked signal-cli daemon."""

import pytest
import respx
import httpx

from signal_mcp.client import SignalClient, SignalError
from signal_mcp.config import DAEMON_URL
from signal_mcp.models import Contact, Group, GroupMember, Message


def rpc_ok(result) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def rpc_err(message: str, code: int = -1) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}}


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
async def test_react_to_message(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await client.react_to_message("+19999999999", "+11111111111", 1700000000000, "👍")


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
async def test_send_message_saves_to_store(client, tmp_path, monkeypatch):
    import signal_mcp.store as store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    import signal_mcp.client as client_mod
    monkeypatch.setattr(client_mod, "_store", store)
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 1700000000000})))
    await client.send_message("+19999999999", "saved!")
    msgs = store.get_conversation("+10000000000")  # own account
    assert any(m.body == "saved!" for m in msgs)


@respx.mock
@pytest.mark.asyncio
async def test_send_group_message_saves_to_store(client, tmp_path, monkeypatch):
    import signal_mcp.store as store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    import signal_mcp.client as client_mod
    monkeypatch.setattr(client_mod, "_store", store)
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 1700000000000})))
    await client.send_group_message("grp123", "group saved!")
    msgs = store.get_conversation("grp123")
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
