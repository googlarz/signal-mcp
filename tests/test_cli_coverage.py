"""Coverage tests for signal_mcp/cli.py — uncovered lines."""

import json
import plistlib
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
    """--watch mode calls receive_direct in a loop and prints messages."""
    msg = _msg(body="watched message")
    client = _mock_client()

    calls = [0]

    async def _receive(**kwargs):
        calls[0] += 1
        if calls[0] == 1:
            return [msg]
        raise KeyboardInterrupt()

    async def _fast_sleep(_):
        pass

    client.receive_direct = _receive
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        with patch("signal_mcp.desktop.SIGNAL_DB") as mock_db:
            mock_db.exists.return_value = False
            with patch("asyncio.sleep", side_effect=_fast_sleep):
                result = runner.invoke(cli, ["receive", "--watch"])
    assert result.exit_code == 0
    assert "watched message" in result.output


def test_receive_keyboard_interrupt(runner):
    """KeyboardInterrupt during receive prints 'Stopped.' and exits 0."""
    client = _mock_client()

    async def _bad_receive(**kwargs):
        raise KeyboardInterrupt()

    client.receive_direct = _bad_receive
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["receive"])
    assert "Stopped" in result.output
    assert result.exit_code == 0


def test_receive_signal_error(runner):
    """SignalError during receive exits 1 with error message."""
    client = _mock_client()

    async def _bad_receive(**kwargs):
        raise SignalError("daemon dead")

    client.receive_direct = _bad_receive
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["receive"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── _print_message receipt_type ──────────────────────────────────────────────

def test_print_message_receipt_type(runner):
    """Messages with receipt_type show 'receipt' in output."""
    msg = _msg(receipt_type="DELIVERY")
    client = _mock_client()
    client.receive_direct = AsyncMock(return_value=[msg])
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
        result = runner.invoke(cli, ["delete", "+12025551999", "12345"])
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
    client.receive_direct = AsyncMock(return_value=[msg])
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


@pytest.mark.parametrize("command, target", [
    ("import-desktop", "signal_mcp.desktop.import_from_desktop"),
    ("sync-desktop", "signal_mcp.desktop.sync_from_desktop"),
])
@pytest.mark.parametrize("system, shown", [("Darwin", True), ("Linux", False)])
def test_keychain_note_only_on_macos(runner, command, target, system, shown):
    result_data = {"imported": 1, "skipped": 0, "total": 1, "since": None, "incremental": False}
    with patch(target, return_value=result_data), \
         patch("platform.system", return_value=system):
        result = runner.invoke(cli, [command])
    assert result.exit_code == 0
    assert ("Keychain access" in result.output) is shown


def test_import_desktop_help_is_not_macos_only(runner):
    result = runner.invoke(cli, ["import-desktop", "--help"])
    assert "macOS" not in result.output
    assert "sqlcipher" in result.output


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


def test_find_binary_args_not_found():
    import signal_mcp.cli as cli_mod
    with patch("shutil.which", side_effect=lambda name: "/opt/homebrew/bin/uv" if name == "uv" else None):
        result = cli_mod._find_binary_args()
    assert result[:4] == ["/opt/homebrew/bin/uv", "run", "--directory", str(Path(cli_mod.__file__).parent.parent.parent)]
    assert result[-1] == "signal-mcp"


# ── install-service (Darwin) ──────────────────────────────────────────────────

def test_install_service_darwin_success(runner, tmp_path, monkeypatch):
    import signal_mcp.cli as cli_mod
    plist_path = tmp_path / "com.signal-mcp.watch.plist"
    monkeypatch.setattr(cli_mod, "PLIST_PATH", plist_path)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stderr = ""

    with patch("platform.system", return_value="Darwin"), \
         patch("signal_mcp.cli._find_binary_args", return_value=["/opt/homebrew/bin/uv", "run", "--directory", "/tmp/signal mcp", "signal-mcp"]), \
         patch("subprocess.run", return_value=mock_result):
        result = runner.invoke(cli, ["install-service"])

    assert result.exit_code == 0
    assert "installed" in result.output.lower()
    with plist_path.open("rb") as handle:
        plist = plistlib.load(handle)
    assert plist["ProgramArguments"] == [
        "/opt/homebrew/bin/uv", "run", "--directory", "/tmp/signal mcp",
        "signal-mcp", "receive", "--watch",
    ]
    assert plist["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin:")


def test_install_service_darwin_launchctl_warn(runner, tmp_path, monkeypatch):
    import signal_mcp.cli as cli_mod
    plist_path = tmp_path / "com.signal-mcp.watch.plist"
    monkeypatch.setattr(cli_mod, "PLIST_PATH", plist_path)

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "load failed"

    with patch("platform.system", return_value="Darwin"), \
         patch("signal_mcp.cli._find_binary_args", return_value=["/usr/bin/signal-mcp"]), \
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
         patch("signal_mcp.cli._find_binary_args", return_value=["/usr/bin/signal-mcp"]), \
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
         patch("signal_mcp.cli._find_binary_args", return_value=["/usr/bin/signal-mcp"]), \
         patch("subprocess.run", return_value=mock_result):
        result = runner.invoke(cli, ["install-service"])

    assert result.exit_code == 0
    assert "installed" in result.output.lower()


def test_install_service_unsupported_platform(runner):
    with patch("platform.system", return_value="Windows"), \
         patch("signal_mcp.cli._find_binary_args", return_value=["/bin/signal-mcp"]):
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


# ── set-webhook / get-webhook ────────────────────────────────────────────────

def test_set_webhook_sets_url(runner):
    with patch("signal_mcp.config.set_webhook_url") as mock_set:
        result = runner.invoke(cli, ["set-webhook", "http://localhost:8080/signal"])
    assert result.exit_code == 0
    assert "Webhook set: http://localhost:8080/signal" in result.output
    mock_set.assert_called_once_with("http://localhost:8080/signal")


def test_set_webhook_clears_url(runner):
    with patch("signal_mcp.config.set_webhook_url") as mock_set:
        result = runner.invoke(cli, ["set-webhook"])
    assert result.exit_code == 0
    assert "Webhook cleared." in result.output
    mock_set.assert_called_once_with(None)


def test_get_webhook_configured(runner):
    with patch("signal_mcp.config.get_webhook_url", return_value="http://localhost:8080/signal"):
        result = runner.invoke(cli, ["get-webhook"])
    assert result.exit_code == 0
    assert "http://localhost:8080/signal" in result.output


def test_get_webhook_not_configured(runner):
    with patch("signal_mcp.config.get_webhook_url", return_value=None):
        result = runner.invoke(cli, ["get-webhook"])
    assert result.exit_code == 0
    assert "No webhook configured." in result.output


# ── find-contact ─────────────────────────────────────────────────────────────

def test_find_contact_no_matches(runner):
    client = _mock_client(list_contacts=AsyncMock(return_value=[]))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["find-contact", "nobody"])
    assert result.exit_code == 0
    assert "No matching contacts." in result.output


def test_find_contact_table(runner):
    contacts = [Contact(number="+11111111111", name="Alice"),
                Contact(number="+12222222222", name="Bob", blocked=True)]
    client = _mock_client(list_contacts=AsyncMock(return_value=contacts))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["find-contact", "a"])
    assert result.exit_code == 0
    assert "Alice" in result.output
    assert "BLOCKED" in result.output
    client.list_contacts.assert_called_once_with(search="a", all_recipients=False)


def test_find_contact_json(runner):
    contacts = [Contact(number="+11111111111", name="Alice")]
    client = _mock_client(list_contacts=AsyncMock(return_value=contacts))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["find-contact", "a", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["number"] == "+11111111111"


def test_find_contact_signal_error(runner):
    client = _mock_client(list_contacts=AsyncMock(side_effect=SignalError("daemon down")))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["find-contact", "a"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── schedule-send / scheduled / cancel-scheduled / run-scheduled ─────────────

def test_schedule_send_invalid_format(runner):
    result = runner.invoke(cli, ["schedule-send", "+1", "hi", "--at", "not-a-date"])
    assert result.exit_code == 1
    assert "invalid --at value" in result.output


def test_schedule_send_in_past(runner):
    result = runner.invoke(cli, ["schedule-send", "+1", "hi", "--at", "2000-01-01 09:00"])
    assert result.exit_code == 1
    assert "must be in the future" in result.output


def test_schedule_send_recipient(runner):
    result = runner.invoke(cli, ["schedule-send", "+19999999999", "hi", "--at", "2999-01-01 09:00"])
    assert result.exit_code == 0
    assert "Scheduled" in result.output
    assert "+19999999999" in result.output


def test_schedule_send_group(runner):
    result = runner.invoke(cli, ["schedule-send", "grp==", "hi", "--at", "2999-01-01T09:00:00", "--group"])
    assert result.exit_code == 0
    assert "Scheduled" in result.output


def test_list_scheduled_empty(runner):
    result = runner.invoke(cli, ["scheduled"])
    assert result.exit_code == 0
    assert "No scheduled messages." in result.output


def test_list_scheduled_table(runner):
    runner.invoke(cli, ["schedule-send", "+19999999999", "hi there", "--at", "2999-01-01 09:00"])
    result = runner.invoke(cli, ["scheduled"])
    assert result.exit_code == 0
    assert "+19999999999" in result.output
    assert "hi there" in result.output


def test_list_scheduled_json(runner):
    runner.invoke(cli, ["schedule-send", "+19999999999", "hi", "--at", "2999-01-01 09:00"])
    result = runner.invoke(cli, ["scheduled", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["recipient"] == "+19999999999"


def test_cancel_scheduled_success(runner):
    schedule_result = runner.invoke(cli, ["schedule-send", "+1", "hi", "--at", "2999-01-01 09:00"])
    job_id = int(schedule_result.output.split("id=")[1].split(")")[0])
    result = runner.invoke(cli, ["cancel-scheduled", str(job_id)])
    assert result.exit_code == 0
    assert "Cancelled" in result.output


def test_cancel_scheduled_not_found(runner):
    result = runner.invoke(cli, ["cancel-scheduled", "999999"])
    assert result.exit_code == 1
    assert "No pending scheduled message" in result.output


def test_run_scheduled_none_due(runner):
    client = _mock_client(process_scheduled_messages=AsyncMock(return_value=[]))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["run-scheduled"])
    assert result.exit_code == 0
    assert "No scheduled messages due." in result.output


def test_run_scheduled_sent_and_failed(runner):
    results = [
        {"id": 1, "status": "sent", "timestamp": 123},
        {"id": 2, "status": "failed", "error": "not registered"},
    ]
    client = _mock_client(process_scheduled_messages=AsyncMock(return_value=results))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["run-scheduled"])
    assert result.exit_code == 0
    assert "Sent  id=1" in result.output
    assert "Failed id=2: not registered" in result.output


def test_run_scheduled_signal_error(runner):
    client = _mock_client(process_scheduled_messages=AsyncMock(side_effect=SignalError("daemon down")))
    with patch("signal_mcp.cli.SignalClient", return_value=client):
        result = runner.invoke(cli, ["run-scheduled"])
    assert result.exit_code == 1
    assert "Error:" in result.output


# ── install (setup wizard) ────────────────────────────────────────────────────

def test_install_signal_cli_missing(runner):
    with patch("signal_mcp.config.check_signal_cli_version",
               side_effect=RuntimeError("signal-cli not found")):
        result = runner.invoke(cli, ["install"])
    assert result.exit_code == 1
    assert "signal-cli not found" in result.output


def test_install_no_account(runner):
    with patch("signal_mcp.config.check_signal_cli_version"), \
         patch("signal_mcp.cli.detect_account", side_effect=RuntimeError("no account")), \
         patch("subprocess.run", return_value=MagicMock(stdout="0.13.0\n")):
        result = runner.invoke(cli, ["install"])
    assert result.exit_code == 1
    assert "No account found." in result.output
    assert "signal-cli link" in result.output


def test_install_service_already_installed(runner):
    with patch("signal_mcp.config.check_signal_cli_version"), \
         patch("signal_mcp.cli.detect_account", return_value="+10000000000"), \
         patch("signal_mcp.config.is_service_installed", return_value=True), \
         patch("subprocess.run", return_value=MagicMock(stdout="0.13.0\n")), \
         patch("signal_mcp.cli._find_binary", return_value="/usr/local/bin/signal-mcp"):
        result = runner.invoke(cli, ["install"])
    assert result.exit_code == 0
    assert "already installed" in result.output
    assert "Setup complete." in result.output


def test_install_service_declined(runner):
    with patch("signal_mcp.config.check_signal_cli_version"), \
         patch("signal_mcp.cli.detect_account", return_value="+10000000000"), \
         patch("signal_mcp.config.is_service_installed", return_value=False), \
         patch("subprocess.run", return_value=MagicMock(stdout="0.13.0\n")), \
         patch("signal_mcp.cli._find_binary", return_value="/usr/local/bin/signal-mcp"):
        result = runner.invoke(cli, ["install"], input="n\n")
    assert result.exit_code == 0
    assert "Setup complete." in result.output


def test_install_service_confirmed(runner, tmp_path, monkeypatch):
    import signal_mcp.cli as cli_mod
    plist_path = tmp_path / "com.signal-mcp.watch.plist"
    monkeypatch.setattr(cli_mod, "PLIST_PATH", plist_path)
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stderr = ""
    mock_result.stdout = "0.13.0\n"

    with patch("signal_mcp.config.check_signal_cli_version"), \
         patch("signal_mcp.cli.detect_account", return_value="+10000000000"), \
         patch("signal_mcp.config.is_service_installed", return_value=False), \
         patch("signal_mcp.cli._find_binary_args", return_value=["/usr/local/bin/signal-mcp"]), \
         patch("subprocess.run", return_value=mock_result), \
         patch("platform.system", return_value="Darwin"), \
         patch("signal_mcp.cli._find_binary", return_value="/usr/local/bin/signal-mcp"):
        result = runner.invoke(cli, ["install"], input="y\n")
    assert result.exit_code == 0
    assert plist_path.exists()
    assert "Setup complete." in result.output


# ── receive: watch fallback / error survival / webhook ───────────────────────

def test_receive_watch_desktop_error_falls_back_to_signal_cli(runner):
    """A DesktopImportError in watch mode switches permanently to signal-cli receive."""
    from signal_mcp.desktop import DesktopImportError
    msg = _msg(body="via cli fallback")
    client = _mock_client()
    calls = [0]

    async def _receive(**kwargs):
        calls[0] += 1
        if calls[0] == 1:
            return [msg]
        raise KeyboardInterrupt()

    async def _fast_sleep(_):
        pass

    client.receive_direct = _receive
    sync = MagicMock(side_effect=DesktopImportError("db locked"))
    with patch("signal_mcp.cli.SignalClient", return_value=client), \
         patch("signal_mcp.desktop.SIGNAL_DB") as mock_db, \
         patch("signal_mcp.desktop.sync_from_desktop", sync), \
         patch("asyncio.sleep", side_effect=_fast_sleep):
        mock_db.exists.return_value = True
        result = runner.invoke(cli, ["receive", "--watch"])
    assert result.exit_code == 0
    assert "via Signal Desktop DB" in result.output
    assert "desktop sync error: db locked" in result.output
    assert "falling back to signal-cli receive" in result.output
    assert "via cli fallback" in result.output
    sync.assert_called_once()  # desktop not retried after fallback


def test_receive_watch_survives_transient_error(runner):
    """A generic receive error is reported and the watch loop keeps going."""
    msg = _msg(body="after recovery")
    client = _mock_client()
    calls = [0]

    async def _receive(**kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("socket reset")
        if calls[0] == 2:
            return [msg]
        raise KeyboardInterrupt()

    async def _fast_sleep(_):
        pass

    client.receive_direct = _receive
    with patch("signal_mcp.cli.SignalClient", return_value=client), \
         patch("signal_mcp.desktop.SIGNAL_DB") as mock_db, \
         patch("asyncio.sleep", side_effect=_fast_sleep):
        mock_db.exists.return_value = False
        result = runner.invoke(cli, ["receive", "--watch"])
    assert result.exit_code == 0
    assert "[watch] receive error: socket reset" in result.output
    assert "after recovery" in result.output


def test_receive_posts_to_webhook(runner):
    """receive with --webhook POSTs the batch; --json output stays machine-readable."""
    msg = _msg(body="hooked")
    client = _mock_client()
    client.receive_direct = AsyncMock(return_value=[msg])
    post = AsyncMock()
    with patch("signal_mcp.cli.SignalClient", return_value=client), \
         patch("signal_mcp.webhook.post_webhook_batch", post):
        result = runner.invoke(cli, ["receive", "--json", "--webhook", "https://example.test/hook"])
    assert result.exit_code == 0
    post.assert_awaited_once_with("https://example.test/hook", [msg])
    assert json.loads(result.output.strip())["body"] == "hooked"


def test_receive_watch_posts_to_webhook(runner):
    """Watch mode (signal-cli path) POSTs each non-empty batch to the webhook."""
    msg = _msg(body="watch hooked")
    client = _mock_client()
    calls = [0]

    async def _receive(**kwargs):
        calls[0] += 1
        if calls[0] == 1:
            return [msg]
        raise KeyboardInterrupt()

    async def _fast_sleep(_):
        pass

    client.receive_direct = _receive
    post = AsyncMock()
    with patch("signal_mcp.cli.SignalClient", return_value=client), \
         patch("signal_mcp.desktop.SIGNAL_DB") as mock_db, \
         patch("signal_mcp.webhook.post_webhook_batch", post), \
         patch("asyncio.sleep", side_effect=_fast_sleep):
        mock_db.exists.return_value = False
        result = runner.invoke(cli, ["receive", "--watch", "--webhook", "https://example.test/hook"])
    assert result.exit_code == 0
    assert "Webhook: https://example.test/hook" in result.output
    post.assert_awaited_once_with("https://example.test/hook", [msg])
