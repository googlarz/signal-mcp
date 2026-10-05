"""Contacts / profile tools: exact JSON-RPC params (verified against signal-cli
*Command.java; JsonRpcNamespace accepts the camelCase form of each dashed dest),
listContacts parsing, and contact-name cache behaviour with all-recipients."""
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

import signal_mcp.client as _client_mod
import signal_mcp.server as _server_mod
import signal_mcp.store as _store_mod
from signal_mcp.config import DAEMON_URL
from signal_mcp.models import Contact
from tests.conftest import call_tool
from tests.test_server import reset_caches, reset_client, rpc_ok  # noqa: F401 — autouse fixtures


def _sent(call_index: int = -1) -> dict:
    return json.loads(respx.calls[call_index].request.content)


# Realistic listContacts entry with every key the live daemon returns.
_FULL_CONTACT = {
    "number": "+11111111111", "uuid": "uuid-1", "username": "alice.01",
    "name": "Alice Smith", "givenName": "Alice", "familyName": "Smith",
    "nickName": "Ali", "nickGivenName": "Ali", "nickFamilyName": None,
    "note": "met at work", "color": "ULTRAMARINE",
    "isArchived": True, "isBlocked": False, "isHidden": False,
    "messageExpirationTime": 3600, "profileSharing": True, "unregistered": False,
    "profile": {
        "about": "hi", "aboutEmoji": "🙂", "familyName": "Smith", "givenName": "Alice",
        "hasAvatar": True, "lastUpdateTimestamp": 1700000000000, "mobileCoinAddress": None,
    },
}


@respx.mock
@pytest.mark.asyncio
async def test_list_contacts_full_payload_and_params():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([_FULL_CONTACT])))
    result = await call_tool("list_contacts", {"all_recipients": True, "blocked": False})
    assert _sent()["method"] == "listContacts"
    assert _sent()["params"] == {"allRecipients": True, "blocked": False}
    c = json.loads(result[0].text)[0]
    assert c["display_name"] == "Ali"  # nickname wins
    assert c["name"] == "Alice Smith" and c["about"] == "hi" and c["blocked"] is False
    assert c["username"] == "alice.01"
    assert c["nick_name"] == "Ali" and c["nick_given_name"] == "Ali"
    assert "nick_family_name" not in c and "is_hidden" not in c and "unregistered" not in c
    assert c["note"] == "met at work"
    assert c["about_emoji"] == "🙂" and c["has_avatar"] is True
    assert c["is_archived"] is True and c["profile_sharing"] is True
    assert c["message_expiration_time"] == 3600


@respx.mock
@pytest.mark.asyncio
async def test_list_contacts_default_sends_no_params_and_omits_empty_keys():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([{"number": "+1"}])))
    result = await call_tool("list_contacts", {})
    assert "params" not in _sent()
    c = json.loads(result[0].text)[0]
    assert set(c) == {"number", "uuid", "name", "given_name", "family_name",
                      "profile_name", "about", "blocked", "display_name"}


@respx.mock
@pytest.mark.asyncio
async def test_find_contact_all_recipients_matches_username_and_nickname():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([_FULL_CONTACT])))
    result = await call_tool("find_contact", {"query": "alice.0", "all_recipients": True})
    assert _sent()["params"] == {"allRecipients": True}
    assert json.loads(result[0].text)[0]["username"] == "alice.01"
    result = await call_tool("find_contact", {"query": "ali"})
    assert "params" not in _sent()
    assert len(json.loads(result[0].text)) == 1


@respx.mock
@pytest.mark.asyncio
async def test_update_contact_all_fields():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await call_tool("update_contact", {
        "number": "+1", "given_name": "A", "family_name": "B",
        "nick_given_name": "C", "nick_family_name": "D", "note": "E",
    })
    assert _sent()["method"] == "updateContact"
    assert _sent()["params"] == {
        "recipient": "+1", "givenName": "A", "familyName": "B",
        "nickGivenName": "C", "nickFamilyName": "D", "note": "E",
    }


@respx.mock
@pytest.mark.asyncio
async def test_update_contact_name_only_unchanged():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await call_tool("update_contact", {"number": "+1", "name": "Alice"})
    assert _sent()["params"] == {"recipient": "+1", "name": "Alice"}


@pytest.mark.asyncio
async def test_update_contact_requires_a_field():
    result = await call_tool("update_contact", {"number": "+1"})
    assert "at least one" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_update_profile_params():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await call_tool("update_profile", {
        "given_name": "A", "family_name": "B", "about": "x",
        "about_emoji": "🙂", "mobilecoin_address": "bW9i",
    })
    assert _sent()["method"] == "updateProfile"
    assert _sent()["params"] == {
        "givenName": "A", "familyName": "B", "about": "x",
        "aboutEmoji": "🙂", "mobileCoinAddress": "bW9i",
    }


@respx.mock
@pytest.mark.asyncio
async def test_update_profile_name_maps_to_given_name():
    # --name is only a CLI alias of --given-name; JSON-RPC reads givenName.
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await call_tool("update_profile", {"name": "Alice"})
    assert _sent()["params"] == {"givenName": "Alice"}
    await call_tool("update_profile", {"name": "Alice", "given_name": "Ally"})
    assert _sent()["params"] == {"givenName": "Ally"}


@pytest.mark.asyncio
async def test_update_profile_avatar_still_path_validated():
    result = await call_tool("update_profile", {"avatar_path": "/etc/passwd"})
    assert "Error" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_remove_contact_flags():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await call_tool("remove_contact", {"number": "+1"})
    assert _sent()["params"] == {"recipient": "+1"}
    await call_tool("remove_contact", {"number": "+1", "forget": True})
    assert _sent()["params"] == {"recipient": "+1", "forget": True}
    await call_tool("remove_contact", {"number": "+1", "hide": True})
    assert _sent()["params"] == {"recipient": "+1", "hide": True}


@pytest.mark.asyncio
async def test_remove_contact_forget_and_hide_exclusive():
    result = await call_tool("remove_contact", {"number": "+1", "forget": True, "hide": True})
    assert "mutually exclusive" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_get_user_status_usernames():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([])))
    await call_tool("get_user_status", {"usernames": ["alice.01"]})
    assert _sent()["params"] == {"username": ["alice.01"]}
    await call_tool("get_user_status", {"recipients": ["+1"], "usernames": ["alice.01"]})
    assert _sent()["params"] == {"recipient": ["+1"], "username": ["alice.01"]}


@respx.mock
@pytest.mark.asyncio
async def test_set_typing_params():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await call_tool("set_typing", {"recipient": "+1"})
    assert _sent()["method"] == "sendTyping"
    assert _sent()["params"] == {"recipient": ["+1"]}
    await call_tool("set_typing", {"recipient": "+1", "stop": True})
    assert _sent()["params"] == {"recipient": ["+1"], "stop": True}
    await call_tool("set_typing", {"group_id": "grp=="})
    assert _sent()["params"] == {"groupId": "grp=="}


@pytest.mark.asyncio
async def test_set_typing_requires_target():
    result = await call_tool("set_typing", {})
    assert "Either recipient or group_id" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_list_identities_filter_param():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([])))
    await call_tool("list_identities", {"number": "+1"})
    assert _sent()["params"] == {"number": "+1"}


@respx.mock
@pytest.mark.asyncio
async def test_trust_identity_safety_number_param():
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await call_tool("trust_identity", {"number": "+1", "safety_number": "1234"})
    assert _sent()["params"] == {"recipient": "+1", "verifiedSafetyNumber": "1234"}


def test_contact_tool_schemas():
    tools = {t.name: t.input_schema for t in _server_mod.TOOLS}
    assert {"all_recipients", "blocked"} <= set(tools["list_contacts"]["properties"])
    assert "all_recipients" in tools["find_contact"]["properties"]
    assert tools["update_contact"]["required"] == ["number"]
    assert {"given_name", "family_name", "nick_given_name", "nick_family_name", "note"} <= set(
        tools["update_contact"]["properties"])
    assert {"given_name", "family_name", "about_emoji", "mobilecoin_address"} <= set(
        tools["update_profile"]["properties"])
    assert {"forget", "hide"} <= set(tools["remove_contact"]["properties"])
    assert "usernames" in tools["get_user_status"]["properties"]
    assert "required" not in tools["get_user_status"]
    assert "group_id" in tools["set_typing"]["properties"]
    assert "required" not in tools["set_typing"]


# ── contact-name cache with all-recipients ────────────────────────────────────

@pytest.fixture
def cold_cache(monkeypatch):
    monkeypatch.setattr(_client_mod, "_contact_cache", {})
    monkeypatch.setattr(_client_mod, "_contact_cache_loaded", False)


@pytest.mark.asyncio
async def test_cache_resolves_non_contact_group_member(reset_client, cold_cache):
    contacts = [Contact(number="+1", uuid="u1", name="Alice")]
    everyone = contacts + [Contact(number="", uuid="u2", given_name="Bob", family_name="G")]
    mock = AsyncMock(side_effect=lambda **kw: everyone if kw.get("all_recipients") else contacts)
    with patch.object(reset_client, "list_contacts", mock):
        await reset_client._ensure_contact_cache()
    assert reset_client.resolve_name("u2") == "Bob G"
    assert reset_client.resolve_name("u1") == "Alice"
    assert mock.await_count == 2


@pytest.mark.asyncio
async def test_cache_precedence_contact_then_desktop_then_profile(reset_client, cold_cache):
    _store_mod.save_conversation("u2", "Bob (Desktop)", "direct")
    _store_mod.save_conversation("u1", "Alice (Desktop)", "direct")
    contacts = [Contact(number="+1", uuid="u1", name="Alice")]
    everyone = [Contact(number="+1", uuid="u1", given_name="Profile-Alice"),
                Contact(number="", uuid="u2", given_name="Profile-Bob"),
                Contact(number="", uuid="u3")]  # no name at all
    mock = AsyncMock(side_effect=lambda **kw: everyone if kw.get("all_recipients") else contacts)
    with patch.object(reset_client, "list_contacts", mock):
        await reset_client._ensure_contact_cache()
    assert reset_client.resolve_name("u1") == "Alice"          # contact name wins
    assert reset_client.resolve_name("u2") == "Bob (Desktop)"  # Desktop beats profile-only
    assert reset_client.resolve_name("u3") == "u3"             # unnamed stays unresolved


# ── CLI ───────────────────────────────────────────────────────────────────────

def test_cli_contacts_and_find_contact_all_recipients():
    from click.testing import CliRunner

    from signal_mcp.cli import cli
    from tests.test_cli_coverage import _mock_client

    client = _mock_client(list_contacts=AsyncMock(return_value=[Contact(number="+1", name="A")]))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        r1 = CliRunner().invoke(cli, ["contacts", "--all-recipients"])
        r2 = CliRunner().invoke(cli, ["find-contact", "a", "--all-recipients"])
    assert r1.exit_code == 0 and r2.exit_code == 0
    assert client.list_contacts.await_args_list[0].kwargs == {"all_recipients": True}
    assert client.list_contacts.await_args_list[1].kwargs == {"search": "a", "all_recipients": True}
