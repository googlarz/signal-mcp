"""Tests for the signal-mcp CLI commands."""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

import signal_mcp.store as _store_mod
from signal_mcp.cli import cli
from signal_mcp.models import Contact, Group, GroupMember, Message, SendResult


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(_store_mod, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(_store_mod, "_initialized_paths", set())
    if getattr(_store_mod._thread_local, "conn", None) is not None:
        _store_mod._thread_local.conn.close()
        _store_mod._thread_local.conn = None


@pytest.fixture
def runner():
    return CliRunner()


def _msg(id="1", sender="+1", body="hello", ts=None, recipient=None, group_id=None):
    return Message(
        id=id, sender=sender, recipient=recipient, body=body,
        timestamp=ts or datetime(2024, 6, 1, 12, 0, 0),
        group_id=group_id,
    )


def _mock_client(**overrides):
    """Return a context-manager mock of SignalClient with sane defaults."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.ensure_daemon = AsyncMock()
    client._daemon_alive = AsyncMock(return_value=True)
    client._ensure_contact_cache = AsyncMock()
    client._ensure_group_cache = AsyncMock()
    client.account = "+10000000000"
    for k, v in overrides.items():
        setattr(client, k, v)
    return client


# ── --version ─────────────────────────────────────────────────────────────────

def test_version(runner):
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "signal-mcp" in result.output


# ── status ────────────────────────────────────────────────────────────────────

def test_status_running(runner):
    client = _mock_client()
    with patch("signal_mcp.cli.detect_account", return_value="+10000000000"), \
         patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "Account" in result.output
    assert "running" in result.output


def test_status_stopped(runner):
    client = _mock_client()
    client._daemon_alive = AsyncMock(return_value=False)
    with patch("signal_mcp.cli.detect_account", return_value="+10000000000"), \
         patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "stopped" in result.output


# ── send ──────────────────────────────────────────────────────────────────────

def test_send_message(runner):
    client = _mock_client()
    client.send_message = AsyncMock(return_value=SendResult(timestamp=1234567890, recipient="+19999999999", success=True))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["send", "+19999999999", "Hello!"])
    assert result.exit_code == 0
    assert "Sent" in result.output
    assert "1234567890" in result.output


def test_send_message_error(runner):
    from signal_mcp.client import SignalError
    client = _mock_client()
    client.send_message = AsyncMock(side_effect=SignalError("invalid number"))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["send", "badnumber", "Hi"])
    assert result.exit_code == 1
    assert "Error" in result.output


# ── note ──────────────────────────────────────────────────────────────────────

def test_note(runner):
    client = _mock_client()
    client.send_note_to_self = AsyncMock(return_value=SendResult(timestamp=999, recipient="+10000000000", success=True))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["note", "remember this"])
    assert result.exit_code == 0
    assert "Note saved" in result.output


# ── contacts ──────────────────────────────────────────────────────────────────

def test_contacts_table(runner):
    contacts = [
        Contact(number="+11111111111", name="Alice"),
        Contact(number="+12222222222", name="Bob", blocked=True),
    ]
    client = _mock_client()
    client.list_contacts = AsyncMock(return_value=contacts)
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["contacts"])
    assert result.exit_code == 0
    assert "Alice" in result.output
    assert "Bob" in result.output
    assert "BLOCKED" in result.output


def test_contacts_json(runner):
    contacts = [Contact(number="+11111111111", name="Alice")]
    client = _mock_client()
    client.list_contacts = AsyncMock(return_value=contacts)
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["contacts", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["number"] == "+11111111111"


# ── groups ────────────────────────────────────────────────────────────────────

def test_groups_table(runner):
    groups = [Group(id="grp1==", name="Team", members=[
        GroupMember(uuid="u1", number="+1"), GroupMember(uuid="u2", number="+2"),
    ])]
    client = _mock_client()
    client.list_groups = AsyncMock(return_value=groups)
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["groups"])
    assert result.exit_code == 0
    assert "Team" in result.output
    assert "2" in result.output  # member count


def test_groups_json(runner):
    groups = [Group(id="grp1==", name="Team", members=[])]
    client = _mock_client()
    client.list_groups = AsyncMock(return_value=groups)
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["groups", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["id"] == "grp1=="


# ── history ───────────────────────────────────────────────────────────────────

def test_history(runner):
    msgs = [_msg(id="1", sender="+1", body="hey there")]
    client = _mock_client()
    client.get_conversation = AsyncMock(return_value=msgs)
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["history", "+19999999999"])
    assert result.exit_code == 0
    assert "hey there" in result.output


def test_history_empty(runner):
    client = _mock_client()
    client.get_conversation = AsyncMock(return_value=[])
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["history", "+19999999999"])
    assert result.exit_code == 0
    assert "No messages" in result.output


def test_history_json(runner):
    msgs = [_msg(id="1", sender="+1", body="json msg")]
    client = _mock_client()
    client.get_conversation = AsyncMock(return_value=msgs)
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["history", "+19999999999", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["body"] == "json msg"


def test_history_invalid_since(runner):
    client = _mock_client()
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["history", "+19999999999", "--since", "not-a-date"])
    assert result.exit_code == 1
    assert "invalid" in result.output.lower()


def test_history_since_date(runner):
    msgs = [_msg()]
    client = _mock_client()
    client.get_conversation = AsyncMock(return_value=msgs)
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["history", "+1", "--since", "2024-01-01"])
    assert result.exit_code == 0
    # Verify since was parsed and passed (get_conversation was called with a since kwarg)
    call_kwargs = client.get_conversation.call_args.kwargs
    assert call_kwargs["since"] is not None


# ── search ────────────────────────────────────────────────────────────────────

def test_search(runner):
    msgs = [_msg(body="found it")]
    client = _mock_client()
    client.search_messages = AsyncMock(return_value=msgs)
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["search", "found"])
    assert result.exit_code == 0
    assert "found it" in result.output


def test_search_no_results(runner):
    client = _mock_client()
    client.search_messages = AsyncMock(return_value=[])
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["search", "nothing"])
    assert result.exit_code == 0
    assert "No messages" in result.output


# ── store-stats ───────────────────────────────────────────────────────────────

def test_store_stats_empty(runner):
    result = runner.invoke(cli, ["store-stats"])
    assert result.exit_code == 0
    assert "Total messages" in result.output
    assert "0" in result.output


def test_store_stats_with_data(runner):
    _store_mod.init_db()
    _store_mod.save_message(_msg(id="s1", sender="+1", ts=datetime(2024, 1, 1)))
    result = runner.invoke(cli, ["store-stats"])
    assert result.exit_code == 0
    assert "1" in result.output


# ── export ────────────────────────────────────────────────────────────────────

def test_export_stdout_json(runner):
    _store_mod.init_db()
    _store_mod.save_message(_msg(id="e1", sender="+1", body="export me"))
    result = runner.invoke(cli, ["export"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["body"] == "export me"


def test_export_stdout_csv(runner):
    _store_mod.init_db()
    _store_mod.save_message(_msg(id="e2", sender="+1", body="csv row"))
    result = runner.invoke(cli, ["export", "--format", "csv"])
    assert result.exit_code == 0
    assert "csv row" in result.output
    assert "id,timestamp" in result.output


def test_export_to_file(runner, tmp_path):
    _store_mod.init_db()
    _store_mod.save_message(_msg(id="e3", sender="+1", body="file msg"))
    out = str(tmp_path / "out.json")
    result = runner.invoke(cli, ["export", out])
    assert result.exit_code == 0
    assert "Exported to" in result.output
    data = json.loads(Path(out).read_text())
    assert data[0]["body"] == "file msg"


def test_export_invalid_since(runner):
    result = runner.invoke(cli, ["export", "--since", "bad-date"])
    assert result.exit_code == 1
    assert "invalid" in result.output.lower()


# ── edit ──────────────────────────────────────────────────────────────────────

def test_edit_dm(runner):
    client = _mock_client()
    client.edit_message = AsyncMock()
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["edit", "+19999999999", "1234567890", "corrected"])
    assert result.exit_code == 0
    assert "edited" in result.output.lower()
    client.edit_message.assert_called_once_with(1234567890, "corrected", recipient="+19999999999")


def test_edit_group(runner):
    client = _mock_client()
    client.edit_message = AsyncMock()
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["edit", "grp123==", "1234567890", "corrected"])
    assert result.exit_code == 0
    client.edit_message.assert_called_once_with(1234567890, "corrected", group_id="grp123==")


# ── send-group ────────────────────────────────────────────────────────────────

def test_send_group(runner):
    from signal_mcp.models import SendResult
    client = _mock_client()
    client.send_group_message = AsyncMock(return_value=SendResult(timestamp=555, recipient="grp==", success=True))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["send-group", "grp==", "hello group"])
    assert result.exit_code == 0
    assert "Sent" in result.output


def test_send_group_error(runner):
    from signal_mcp.client import SignalError
    client = _mock_client()
    client.send_group_message = AsyncMock(side_effect=SignalError("not a member"))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["send-group", "grp==", "hi"])
    assert result.exit_code == 1


# ── conversations ─────────────────────────────────────────────────────────────

def test_conversations_table(runner):
    client = _mock_client()
    client.list_conversations = AsyncMock(return_value=[
        {"id": "+11111111111", "type": "direct", "name": "Alice",
         "unread_count": 3, "last_message": "hey!", "last_message_at": "2024-06-01T12:00:00"},
        {"id": "grp==", "type": "group", "name": "Team",
         "unread_count": 0, "last_message": "ok", "last_message_at": "2024-06-01T11:00:00"},
    ])
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["conversations"])
    assert result.exit_code == 0
    assert "Alice" in result.output
    assert "3 unread" in result.output
    assert "Team" in result.output


def test_conversations_json(runner):
    client = _mock_client()
    convs = [{"id": "+1", "type": "direct", "unread_count": 0, "last_message": "hi"}]
    client.list_conversations = AsyncMock(return_value=convs)
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["conversations", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["id"] == "+1"


def test_conversations_empty(runner):
    client = _mock_client()
    client.list_conversations = AsyncMock(return_value=[])
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["conversations"])
    assert result.exit_code == 0
    assert "No conversations" in result.output


# ── search --sender / --limit ─────────────────────────────────────────────────

def test_search_with_sender(runner):
    msgs = [_msg(body="filtered")]
    client = _mock_client()
    client.search_messages = AsyncMock(return_value=msgs)
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["search", "filtered", "--sender", "+11111111111"])
    assert result.exit_code == 0
    assert "filtered" in result.output
    client.search_messages.assert_called_once_with("filtered", limit=50, offset=0, sender="+11111111111")


def test_search_with_limit(runner):
    client = _mock_client()
    client.search_messages = AsyncMock(return_value=[])
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["search", "x", "--limit", "10"])
    client.search_messages.assert_called_once_with("x", limit=10, offset=0, sender=None)


# ── receive ───────────────────────────────────────────────────────────────────

def test_receive_once(runner):
    client = _mock_client()
    client.receive_messages = AsyncMock(return_value=[_msg(body="incoming")])
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["receive"])
    assert result.exit_code == 0
    assert "incoming" in result.output


def test_receive_empty(runner):
    client = _mock_client()
    client.receive_messages = AsyncMock(return_value=[])
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["receive"])
    assert result.exit_code == 0


# ── pin / unpin ───────────────────────────────────────────────────────────────

def test_pin_group(runner):
    client = _mock_client()
    client.pin_message = AsyncMock()
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["pin", "grp==", "1234567890", "+1"])
    assert result.exit_code == 0
    assert "Pinned" in result.output
    client.pin_message.assert_called_once_with("+1", 1234567890, group_id="grp==", recipient=None)


def test_unpin_dm(runner):
    client = _mock_client()
    client.unpin_message = AsyncMock()
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["unpin", "+19999999999", "1234567890", "+1"])
    assert result.exit_code == 0
    assert "Unpinned" in result.output
    client.unpin_message.assert_called_once_with("+1", 1234567890, group_id=None, recipient="+19999999999")


# ── admin-delete ──────────────────────────────────────────────────────────────

def test_admin_delete(runner):
    client = _mock_client()
    client.admin_delete_message = AsyncMock()
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["admin-delete", "grp==", "1234567890", "+1"])
    assert result.exit_code == 0
    assert "Admin-deleted" in result.output
    client.admin_delete_message.assert_called_once_with("+1", 1234567890, "grp==")


# ── update-device ─────────────────────────────────────────────────────────────

def test_update_device_cmd(runner):
    client = _mock_client()
    client.update_device = AsyncMock()
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["update-device", "2", "My Mac"])
    assert result.exit_code == 0
    assert "renamed" in result.output
    client.update_device.assert_called_once_with(2, "My Mac")


# ── react ─────────────────────────────────────────────────────────────────────

def test_react_dm(runner):
    client = _mock_client()
    client.react_to_message = AsyncMock()
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["react", "+19999999999", "1234567890", "+1sender", "👍"])
    assert result.exit_code == 0
    assert "👍" in result.output
    client.react_to_message.assert_called_once_with(
        "+1sender", 1234567890, "👍", remove=False, recipient="+19999999999"
    )


def test_react_group(runner):
    client = _mock_client()
    client.react_to_message = AsyncMock()
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["react", "grp==", "1234567890", "+1sender", "❤️"])
    assert result.exit_code == 0
    client.react_to_message.assert_called_once_with(
        "+1sender", 1234567890, "❤️", remove=False, group_id="grp=="
    )


def test_react_remove(runner):
    client = _mock_client()
    client.react_to_message = AsyncMock()
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["react", "+19999999999", "1234567890", "+1sender", "👍", "--remove"])
    assert result.exit_code == 0
    assert "removed" in result.output
    client.react_to_message.assert_called_once_with(
        "+1sender", 1234567890, "👍", remove=True, recipient="+19999999999"
    )


# ── delete ────────────────────────────────────────────────────────────────────

def test_delete_dm(runner):
    client = _mock_client()
    client.delete_message = AsyncMock()
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["delete", "+19999999999", "1234567890"])
    assert result.exit_code == 0
    assert "deleted" in result.output.lower()
    client.delete_message.assert_called_once_with("+19999999999", 1234567890)


def test_delete_group(runner):
    client = _mock_client()
    client.delete_group_message = AsyncMock()
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["delete", "grp==", "1234567890"])
    assert result.exit_code == 0
    client.delete_group_message.assert_called_once_with("grp==", 1234567890)


# ── block / unblock ───────────────────────────────────────────────────────────

def test_block(runner):
    client = _mock_client()
    client.block_contact = AsyncMock()
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["block", "+19999999999"])
    assert result.exit_code == 0
    assert "Blocked" in result.output
    client.block_contact.assert_called_once_with("+19999999999")


def test_unblock(runner):
    client = _mock_client()
    client.unblock_contact = AsyncMock()
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["unblock", "+19999999999"])
    assert result.exit_code == 0
    assert "Unblocked" in result.output
    client.unblock_contact.assert_called_once_with("+19999999999")


# ── prune ─────────────────────────────────────────────────────────────────────

def test_prune_with_yes(monkeypatch, runner):
    monkeypatch.setattr(_store_mod, "prune_old_messages", lambda days: 12)
    result = runner.invoke(cli, ["prune", "--days", "90", "--yes"])
    assert result.exit_code == 0
    assert "12" in result.output
    assert "90" in result.output


def test_prune_default_days(monkeypatch, runner):
    captured = {}
    def fake_prune(days):
        captured["days"] = days
        return 0
    monkeypatch.setattr(_store_mod, "prune_old_messages", fake_prune)
    result = runner.invoke(cli, ["prune", "--yes"])
    assert result.exit_code == 0
    assert captured["days"] == 180


def test_prune_prompts_without_yes(monkeypatch, runner):
    monkeypatch.setattr(_store_mod, "prune_old_messages", lambda days: 0)
    # Simulate user confirming
    result = runner.invoke(cli, ["prune", "--days", "30"], input="y\n")
    assert result.exit_code == 0


def test_prune_aborts_on_no(monkeypatch, runner):
    monkeypatch.setattr(_store_mod, "prune_old_messages", lambda days: 0)
    result = runner.invoke(cli, ["prune", "--days", "30"], input="n\n")
    assert result.exit_code != 0


def test_prune_rejects_zero_days(runner):
    result = runner.invoke(cli, ["prune", "--days", "0", "--yes"])
    assert result.exit_code != 0
