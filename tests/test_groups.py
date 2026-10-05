"""Group features: member labels, extended group info, update/quit/terminate params.

Expected JSON-RPC param names come from signal-cli's UpdateGroupCommand,
QuitGroupCommand, TerminateGroupCommand and ListGroupsCommand (JsonRpcNamespace
accepts camelCase for every dashed option name, and list options as-is).
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from click.testing import CliRunner

import signal_mcp.client as _client_mod
from signal_mcp.cli import cli
from signal_mcp.client import SignalClient, SignalError
from signal_mcp.config import DAEMON_URL
from signal_mcp.models import Group, GroupMember
from tests.conftest import TOOLS, call_tool
from tests.test_cli import _mock_client


def rpc_ok(result) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def _sent(route) -> dict:
    return json.loads(route.calls[0].request.read())


@pytest.fixture(autouse=True)
def reset_caches(monkeypatch):
    monkeypatch.setattr(_client_mod, "_contact_cache", {})
    monkeypatch.setattr(_client_mod, "_contact_cache_loaded", True)
    monkeypatch.setattr(_client_mod, "_contact_cache_at", 1e18)
    monkeypatch.setattr(_client_mod, "_group_cache", {})
    monkeypatch.setattr(_client_mod, "_group_cache_loaded", True)
    monkeypatch.setattr(_client_mod, "_group_cache_at", 1e18)


@pytest.fixture(autouse=True)
def reset_client(monkeypatch):
    test_client = SignalClient(account="+10000000000")
    monkeypatch.setattr("signal_mcp.server._client", test_client)
    monkeypatch.setattr(test_client, "ensure_daemon", AsyncMock())
    return test_client


LIVE_SHAPE_GROUP = {
    "id": "grp1==", "name": "Club", "description": "", "isMember": True, "isBlocked": False,
    "messageExpirationTime": 604800,
    "members": [
        {"number": "+1111", "uuid": "u1", "isAdmin": True, "label": "Anna", "labelEmoji": "⚽"},
        {"number": None, "uuid": "u2", "isAdmin": False},
    ],
    "pendingMembers": [{"number": "+3333", "uuid": "u3"}],
    "requestingMembers": [{"number": None, "uuid": "u4"}],
    "admins": [{"number": "+1111", "uuid": "u1"}],
    "banned": [{"number": None, "uuid": "u5"}],
    "permissionAddMember": "EVERY_MEMBER",
    "permissionEditDetails": "ONLY_ADMINS",
    "permissionSendMessage": "EVERY_MEMBER",
    "groupInviteLink": "https://signal.group/#x",
    "isTerminated": False,
}


# ── list_groups / models ──────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_list_groups_parses_labels_and_extended_fields(reset_client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([LIVE_SHAPE_GROUP])))
    [g] = await reset_client.list_groups()
    assert "params" not in _sent(route)
    assert g.members[0].label == "Anna" and g.members[0].label_emoji == "⚽"
    assert g.members[1].label is None
    d = g.to_dict()
    assert d["members"][0] == {"uuid": "u1", "number": "+1111", "is_admin": True, "label": "Anna", "label_emoji": "⚽"}
    assert d["members"][1] == {"uuid": "u2", "number": None, "is_admin": False}
    assert d["pending_members"] == [{"uuid": "u3", "number": "+3333"}]
    assert d["requesting_members"] == [{"uuid": "u4", "number": None}]
    assert d["banned"] == [{"uuid": "u5", "number": None}]
    assert d["permission_add_member"] == "EVERY_MEMBER"
    assert d["permission_edit_details"] == "ONLY_ADMINS"
    assert d["permission_send_message"] == "EVERY_MEMBER"
    assert d["message_expiration_time"] == 604800
    assert "is_terminated" not in d  # falsy values omitted
    assert d["invite_link"] == "https://signal.group/#x"


def test_group_to_dict_keeps_old_keys_and_omits_empty():
    d = Group(id="g", name="n", members=[GroupMember(uuid="u")]).to_dict()
    assert d == {
        "id": "g", "name": "n", "description": None, "member_count": 1,
        "members": [{"uuid": "u", "number": None, "is_admin": False}],
        "is_blocked": False, "is_member": True, "invite_link": None,
    }
    assert Group(id="g", is_terminated=True).to_dict()["is_terminated"] is True


@respx.mock
@pytest.mark.asyncio
async def test_list_groups_group_id_filter(reset_client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([LIVE_SHAPE_GROUP])))
    await reset_client.list_groups(group_id="grp1==")
    assert _sent(route)["params"] == {"groupId": ["grp1=="]}


@respx.mock
@pytest.mark.asyncio
async def test_tool_list_groups_label_round_trip():
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([LIVE_SHAPE_GROUP])))
    result = await call_tool("list_groups", {"group_id": "grp1=="})
    data = json.loads(result[0].text)
    assert data[0]["members"][0]["label"] == "Anna"
    assert _sent(route)["params"] == {"groupId": ["grp1=="]}


# ── update_group ──────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_update_group_new_params(reset_client, tmp_path):
    avatar = tmp_path / "a.png"
    avatar.write_bytes(b"x")
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await reset_client.update_group(
        "grp1==",
        avatar_path=str(avatar),
        ban_members=["+1"], unban_members=["+2"], reset_link=True,
        permission_add_member="only-admins",
        permission_edit_details="every-member",
        permission_send_messages="only-admins",
        member_label="Lars", member_label_emoji="⚽",
    )
    body = _sent(route)
    assert body["method"] == "updateGroup"
    assert body["params"] == {
        "groupId": "grp1==", "avatar": str(avatar.resolve()), "ban": ["+1"], "unban": ["+2"],
        "resetLink": True, "setPermissionAddMember": "only-admins",
        "setPermissionEditDetails": "every-member", "setPermissionSendMessages": "only-admins",
        "memberLabel": "Lars", "memberLabelEmoji": "⚽",
    }


@respx.mock
@pytest.mark.asyncio
async def test_update_group_link_mode_reset_maps_to_reset_link(reset_client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await reset_client.update_group("grp1==", link_mode="reset")
    assert _sent(route)["params"] == {"groupId": "grp1==", "resetLink": True}


@respx.mock
@pytest.mark.asyncio
async def test_update_group_avatar_outside_roots_rejected(reset_client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    with pytest.raises(SignalError, match="outside the allowed folders"):
        await reset_client.update_group("grp1==", avatar_path="/etc/passwd")
    assert not route.called


@respx.mock
@pytest.mark.asyncio
async def test_tool_update_group_member_label():
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("update_group", {
        "group_id": "grp1==", "member_label": "Lars", "member_label_emoji": "⚽",
        "ban_members": ["+1"], "unban_members": ["+2"], "reset_link": True,
        "permission_add_member": "only-admins", "permission_edit_details": "only-admins",
        "permission_send_messages": "every-member", "avatar": "/tmp/a.png",
    })
    assert "group updated" in result[0].text
    assert _sent(route)["params"] == {
        "groupId": "grp1==", "memberLabel": "Lars", "memberLabelEmoji": "⚽",
        "ban": ["+1"], "unban": ["+2"], "resetLink": True,
        "setPermissionAddMember": "only-admins", "setPermissionEditDetails": "only-admins",
        "setPermissionSendMessages": "every-member", "avatar": str(Path("/tmp/a.png").resolve()),
    }


@respx.mock
@pytest.mark.asyncio
async def test_tool_update_group_avatar_rejected():
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("update_group", {"group_id": "grp1==", "avatar": "/etc/passwd"})
    assert "outside the allowed folders" in result[0].text
    assert not route.called


def test_update_group_schema_documents_own_label_only():
    tool = next(t for t in TOOLS if t.name == "update_group")
    props = tool.input_schema["properties"]
    for key in ("member_label", "member_label_emoji", "avatar", "ban_members", "unban_members",
                "reset_link", "permission_add_member", "permission_edit_details",
                "permission_send_messages"):
        assert key in props
    assert props["permission_send_messages"]["enum"] == ["every-member", "only-admins"]
    assert "ONLY YOUR OWN label" in tool.description


# ── create_group ──────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_create_group_avatar(reset_client, tmp_path):
    avatar = tmp_path / "a.png"
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"groupId": "new=="})))
    await reset_client.create_group("Team", ["+1"], avatar_path=str(avatar))
    assert _sent(route)["params"] == {"name": "Team", "member": ["+1"], "avatar": str(avatar.resolve())}


@respx.mock
@pytest.mark.asyncio
async def test_tool_create_group_avatar_rejected():
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("create_group", {"name": "T", "members": ["+1"], "avatar": "/etc/passwd"})
    assert "outside the allowed folders" in result[0].text
    assert not route.called


# ── leave_group / terminate_group ─────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_tool_leave_group_admin_and_delete():
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("leave_group", {"group_id": "grp1==", "admins": ["+2"], "delete": True})
    assert "left group" in result[0].text
    body = _sent(route)
    assert body["method"] == "quitGroup"
    assert body["params"] == {"groupId": "grp1==", "admin": ["+2"], "delete": True}


@respx.mock
@pytest.mark.asyncio
async def test_leave_group_default_params(reset_client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await reset_client.leave_group("grp1==")
    assert _sent(route)["params"] == {"groupId": "grp1=="}


def test_leave_group_schema_mentions_sole_admin():
    tool = next(t for t in TOOLS if t.name == "leave_group")
    assert {"admins", "delete"} <= tool.input_schema["properties"].keys()
    assert "ONLY admin" in tool.description


@respx.mock
@pytest.mark.asyncio
async def test_tool_terminate_group():
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    result = await call_tool("terminate_group", {"group_id": "grp1==", "confirm": True})
    assert "group terminated" in result[0].text
    body = _sent(route)
    assert body["method"] == "terminateGroup"
    assert body["params"] == {"groupId": "grp1=="}


@respx.mock
@pytest.mark.asyncio
async def test_tool_terminate_group_requires_confirm():
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    for args in ({"group_id": "grp1==", "confirm": False}, {"group_id": "grp1==", "confirm": "true"}):
        result = await call_tool("terminate_group", args)
        assert "Error" in result[0].text
    result = await call_tool("terminate_group", {"group_id": "grp1=="})
    assert "Error" in result[0].text
    assert not route.called


def test_terminate_group_schema_and_not_read_only():
    import signal_mcp.server as _server_mod
    tool = next(t for t in TOOLS if t.name == "terminate_group")
    assert tool.input_schema["required"] == ["group_id", "confirm"]
    assert "IRREVERSIBLE" in tool.description
    assert "terminate_group" not in _server_mod._READ_ONLY_TOOLS


# ── CLI ───────────────────────────────────────────────────────────────────────

def test_cli_group_label():
    client = _mock_client()
    client.update_group = AsyncMock()
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = CliRunner().invoke(cli, ["group-label", "grp1==", "Lars", "--emoji", "⚽"])
    assert result.exit_code == 0
    client.update_group.assert_awaited_once_with("grp1==", member_label="Lars", member_label_emoji="⚽")


def test_cli_group_label_error():
    client = _mock_client()
    client.update_group = AsyncMock(side_effect=SignalError("fail"))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = CliRunner().invoke(cli, ["group-label", "grp1==", "Lars"])
    assert result.exit_code == 1
    assert "fail" in result.output
