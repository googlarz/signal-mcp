"""Coverage tests for signal_mcp/cli.py — uncovered lines."""

import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

import signal_mcp.store as _store_mod
from signal_mcp.cli import cli
from signal_mcp.client import SignalError
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


def _msg(id="1", sender="+1", body="hello", ts=None, recipient=None, group_id=None,
         receipt_type=None):
    return Message(
        id=id, sender=sender, recipient=recipient, body=body,
        timestamp=ts or datetime(2024, 6, 1, 12, 0, 0),
        group_id=group_id,
        receipt_type=receipt_type,
    )


def _mock_client(**overrides):
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


# ── receive --watch ───────────────────────────────────────────────────────────

def test_receive_watch_mode(runner):
    """--watch mode iterates receive_stream and prints messages."""
    msg = _msg(body="watched message")
    client = _mock_client()

    async def _stream(**kwargs):
        yield msg

    client.receive_stream = _stream
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["receive", "--watch"])
    assert result.exit_code == 0
    assert "watched message" in result.output


def test_receive_keyboard_interrupt(runner):
    """KeyboardInterrupt during receive prints 'Stopped.' and exits 0."""
    client = _mock_client()

    async def _bad_receive(**kwargs):
        raise KeyboardInterrupt()

    client.receive_messages = _bad_receive
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["receive"])
    assert "Stopped" in result.output
    assert result.exit_code == 0


def test_receive_signal_error(runner):
    """SignalError during receive exits 1 with error message."""
    client = _mock_client()

    async def _bad_receive(**kwargs):
        raise SignalError("daemon dead")

    client.receive_messages = _bad_receive
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["receive"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── _print_message receipt_type ──────────────────────────────────────────────

def test_print_message_receipt_type(runner):
    """Messages with receipt_type show 'receipt' in output."""
    msg = _msg(receipt_type="DELIVERY")
    client = _mock_client()
    client.receive_messages = AsyncMock(return_value=[msg])
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["receive"])
    assert "receipt" in result.output


# ── contacts SignalError ──────────────────────────────────────────────────────

def test_contacts_signal_error(runner):
    client = _mock_client()
    client.list_contacts = AsyncMock(side_effect=SignalError("fail"))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["contacts"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── groups SignalError ────────────────────────────────────────────────────────

def test_groups_signal_error(runner):
    client = _mock_client()
    client.list_groups = AsyncMock(side_effect=SignalError("fail"))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["groups"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── history SignalError ───────────────────────────────────────────────────────

def test_history_signal_error(runner):
    client = _mock_client()
    client.get_conversation = AsyncMock(side_effect=SignalError("fail"))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["history", "+1999"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── note SignalError ──────────────────────────────────────────────────────────

def test_note_signal_error(runner):
    client = _mock_client()
    client.send_note_to_self = AsyncMock(side_effect=SignalError("fail"))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["note", "test message"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── edit SignalError ──────────────────────────────────────────────────────────

def test_edit_signal_error(runner):
    client = _mock_client()
    client.edit_message = AsyncMock(side_effect=SignalError("fail"))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["edit", "+1999", "1234567890", "new text"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── react SignalError ─────────────────────────────────────────────────────────

def test_react_signal_error(runner):
    client = _mock_client()
    client.react_to_message = AsyncMock(side_effect=SignalError("fail"))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["react", "+1999", "12345", "+1", "👍"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── delete SignalError ────────────────────────────────────────────────────────

def test_delete_signal_error(runner):
    client = _mock_client()
    client.delete_message = AsyncMock(side_effect=SignalError("fail"))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["delete", "+1999", "12345"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── block SignalError ─────────────────────────────────────────────────────────

def test_block_signal_error(runner):
    client = _mock_client()
    client.block_contact = AsyncMock(side_effect=SignalError("fail"))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["block", "+1999"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── unblock SignalError ───────────────────────────────────────────────────────

def test_unblock_signal_error(runner):
    client = _mock_client()
    client.unblock_contact = AsyncMock(side_effect=SignalError("fail"))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["unblock", "+1999"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── _print_message with attachment (line 103) ─────────────────────────────────

def test_print_message_with_attachment(runner):
    """Message with attachment shows attachment info (line 103)."""
    from signal_mcp.models import Attachment
    att = Attachment(content_type="image/jpeg", filename="photo.jpg", local_path="/tmp/photo.jpg")
    msg = _msg()
    msg = Message(
        id="1", sender="+1", body="check this",
        timestamp=datetime(2024, 6, 1, 12, 0, 0),
        attachments=[att],
    )
    client = _mock_client()
    client.receive_messages = AsyncMock(return_value=[msg])
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["receive"])
    assert "photo.jpg" in result.output


# ── search --json output (line 335) ─────────────────────────────────────────

def test_search_json_output(runner):
    """search --json flag outputs JSON array (line 335)."""
    msg = _msg(body="found it")
    client = _mock_client()
    client.search_messages = AsyncMock(return_value=[msg])
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["search", "--json", "found"])
    assert result.exit_code == 0
    import json as _json
    data = _json.loads(result.output)
    assert isinstance(data, list)
    assert len(data) == 1


# ── search empty results ──────────────────────────────────────────────────────

def test_search_empty_results(runner):
    client = _mock_client()
    client.search_messages = AsyncMock(return_value=[])
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["search", "nomatch"])
    assert result.exit_code == 0


# ── search SignalError ────────────────────────────────────────────────────────

def test_search_signal_error(runner):
    client = _mock_client()
    client.search_messages = AsyncMock(side_effect=SignalError("fail"))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["search", "query"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── conversations SignalError ─────────────────────────────────────────────────

def test_conversations_signal_error(runner):
    client = _mock_client()
    client.list_conversations = AsyncMock(side_effect=SignalError("fail"))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["conversations"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── status detect_account fails ──────────────────────────────────────────────

def test_status_detect_account_failure(runner):
    with patch("signal_mcp.cli.detect_account", side_effect=RuntimeError("no account")):
        result = runner.invoke(cli, ["status"])
    assert "ERROR" in result.output


# ── daemon command ────────────────────────────────────────────────────────────

def test_daemon_detect_account_success(runner):
    """daemon command: detect_account succeeds, prints startup message, subprocess is mocked."""
    with patch("signal_mcp.cli.detect_account", return_value="+1test"), \
         patch("signal_mcp.cli.subprocess.run", side_effect=KeyboardInterrupt()):
        result = runner.invoke(cli, ["daemon"])
    assert "Starting signal-cli daemon" in result.output


def test_daemon_detect_account_failure(runner):
    """daemon command: detect_account fails → exits 1 with error."""
    with patch("signal_mcp.cli.detect_account", side_effect=RuntimeError("no account")):
        result = runner.invoke(cli, ["daemon"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── stop command ─────────────────────────────────────────────────────────────

def test_stop_daemon_stopped(runner):
    client = _mock_client()
    client.stop_daemon = AsyncMock(return_value=True)
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["stop"])
    assert "Daemon stopped" in result.output


def test_stop_daemon_not_running(runner):
    client = _mock_client()
    client.stop_daemon = AsyncMock(return_value=False)
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["stop"])
    assert "not running" in result.output


# ── import-desktop ────────────────────────────────────────────────────────────

def test_import_desktop_success(runner):
    """Successful import calls progress_cb (line 475) and shows summary."""
    def fake_import(progress_cb=None):
        if progress_cb:
            progress_cb("Processing 7 messages...")
        return {"imported": 5, "skipped": 2, "total": 7}

    with patch("signal_mcp.desktop.import_from_desktop", side_effect=fake_import):
        result = runner.invoke(cli, ["import-desktop"])
    assert "5 imported" in result.output
    assert "Processing" in result.output  # progress_cb was called
    assert result.exit_code == 0


def test_import_desktop_error(runner):
    from signal_mcp.desktop import DesktopImportError
    with patch("signal_mcp.desktop.import_from_desktop",
               side_effect=DesktopImportError("no DB")):
        result = runner.invoke(cli, ["import-desktop"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── pin SignalError ───────────────────────────────────────────────────────────

def test_pin_signal_error(runner):
    client = _mock_client()
    client.pin_message = AsyncMock(side_effect=SignalError("fail"))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["pin", "grp123==", "12345", "+1"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── unpin SignalError ─────────────────────────────────────────────────────────

def test_unpin_signal_error(runner):
    client = _mock_client()
    client.unpin_message = AsyncMock(side_effect=SignalError("fail"))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["unpin", "grp123==", "12345", "+1"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── admin-delete SignalError ──────────────────────────────────────────────────

def test_admin_delete_signal_error(runner):
    client = _mock_client()
    client.admin_delete_message = AsyncMock(side_effect=SignalError("fail"))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["admin-delete", "grp123==", "12345", "+1"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── update-device SignalError ─────────────────────────────────────────────────

def test_update_device_signal_error(runner):
    client = _mock_client()
    client.update_device = AsyncMock(side_effect=SignalError("fail"))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["update-device", "1", "MyPhone"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── _find_binary ──────────────────────────────────────────────────────────────

def test_find_binary_found():
    import signal_mcp.cli as cli_mod
    with patch("shutil.which", return_value="/usr/local/bin/signal-mcp"):
        result = cli_mod._find_binary()
    assert result == "/usr/local/bin/signal-mcp"


def test_find_binary_not_found():
    import signal_mcp.cli as cli_mod
    with patch("shutil.which", return_value=None):
        result = cli_mod._find_binary()
    assert "uv run" in result
    assert "signal-mcp" in result


# ── install-service (Darwin) ──────────────────────────────────────────────────

def test_install_service_darwin_success(runner, tmp_path, monkeypatch):
    import signal_mcp.cli as cli_mod
    plist_path = tmp_path / "com.signal-mcp.watch.plist"
    monkeypatch.setattr(cli_mod, "PLIST_PATH", plist_path)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stderr = ""

    with patch("platform.system", return_value="Darwin"), \
         patch("signal_mcp.cli._find_binary", return_value="/usr/bin/signal-mcp"), \
         patch("subprocess.run", return_value=mock_result):
        result = runner.invoke(cli, ["install-service"])

    assert result.exit_code == 0
    assert "installed" in result.output.lower()


def test_install_service_darwin_launchctl_warn(runner, tmp_path, monkeypatch):
    import signal_mcp.cli as cli_mod
    plist_path = tmp_path / "com.signal-mcp.watch.plist"
    monkeypatch.setattr(cli_mod, "PLIST_PATH", plist_path)

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "load failed"

    with patch("platform.system", return_value="Darwin"), \
         patch("signal_mcp.cli._find_binary", return_value="/usr/bin/signal-mcp"), \
         patch("subprocess.run", return_value=mock_result):
        result = runner.invoke(cli, ["install-service"])

    assert "Warning" in result.output


def test_install_service_linux_systemctl_warn(runner, tmp_path, monkeypatch):
    """Linux systemctl enable fails → Warning path (lines 683-685)."""
    import signal_mcp.cli as cli_mod
    service_path = tmp_path / "signal-mcp-watch.service"
    monkeypatch.setattr(cli_mod, "SYSTEMD_SERVICE_PATH", service_path)

    daemon_reload = MagicMock(returncode=0)
    enable_fail = MagicMock(returncode=1, stderr="enable failed")

    call_count = [0]

    def mock_run(cmd, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return daemon_reload
        return enable_fail

    with patch("platform.system", return_value="Linux"), \
         patch("signal_mcp.cli._find_binary", return_value="/usr/bin/signal-mcp"), \
         patch("subprocess.run", side_effect=mock_run):
        result = runner.invoke(cli, ["install-service"])

    assert "Warning" in result.output


def test_install_service_linux_success(runner, tmp_path, monkeypatch):
    import signal_mcp.cli as cli_mod
    service_path = tmp_path / "signal-mcp-watch.service"
    monkeypatch.setattr(cli_mod, "SYSTEMD_SERVICE_PATH", service_path)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stderr = ""

    with patch("platform.system", return_value="Linux"), \
         patch("signal_mcp.cli._find_binary", return_value="/usr/bin/signal-mcp"), \
         patch("subprocess.run", return_value=mock_result):
        result = runner.invoke(cli, ["install-service"])

    assert result.exit_code == 0
    assert "installed" in result.output.lower()


def test_install_service_unsupported_platform(runner):
    with patch("platform.system", return_value="Windows"), \
         patch("signal_mcp.cli._find_binary", return_value="/bin/signal-mcp"):
        result = runner.invoke(cli, ["install-service"])
    assert result.exit_code == 1


# ── uninstall-service ─────────────────────────────────────────────────────────

def test_uninstall_service_darwin_not_installed(runner, tmp_path, monkeypatch):
    import signal_mcp.cli as cli_mod
    plist_path = tmp_path / "nonexistent.plist"
    monkeypatch.setattr(cli_mod, "PLIST_PATH", plist_path)

    with patch("platform.system", return_value="Darwin"):
        result = runner.invoke(cli, ["uninstall-service"])

    assert "not installed" in result.output
    assert result.exit_code == 0


def test_uninstall_service_darwin_installed(runner, tmp_path, monkeypatch):
    import signal_mcp.cli as cli_mod
    plist_path = tmp_path / "com.signal-mcp.watch.plist"
    plist_path.write_text("<?xml?>")
    monkeypatch.setattr(cli_mod, "PLIST_PATH", plist_path)

    with patch("platform.system", return_value="Darwin"), \
         patch("subprocess.run", return_value=MagicMock(returncode=0)):
        result = runner.invoke(cli, ["uninstall-service"])

    assert "uninstalled" in result.output
    assert result.exit_code == 0
    assert not plist_path.exists()


def test_uninstall_service_linux_not_installed(runner, tmp_path, monkeypatch):
    import signal_mcp.cli as cli_mod
    service_path = tmp_path / "nonexistent.service"
    monkeypatch.setattr(cli_mod, "SYSTEMD_SERVICE_PATH", service_path)

    with patch("platform.system", return_value="Linux"):
        result = runner.invoke(cli, ["uninstall-service"])

    assert "not installed" in result.output


def test_uninstall_service_linux_installed(runner, tmp_path, monkeypatch):
    import signal_mcp.cli as cli_mod
    service_path = tmp_path / "signal-mcp-watch.service"
    service_path.write_text("[Unit]\n")
    monkeypatch.setattr(cli_mod, "SYSTEMD_SERVICE_PATH", service_path)

    with patch("platform.system", return_value="Linux"), \
         patch("subprocess.run", return_value=MagicMock(returncode=0)):
        result = runner.invoke(cli, ["uninstall-service"])

    assert "uninstalled" in result.output
    assert not service_path.exists()


def test_uninstall_service_unsupported_platform(runner):
    with patch("platform.system", return_value="FreeBSD"):
        result = runner.invoke(cli, ["uninstall-service"])
    assert result.exit_code == 1
