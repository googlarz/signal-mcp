"""Coverage tests for signal_mcp/config.py."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import signal_mcp.config as config_mod
from signal_mcp.config import (
    check_signal_cli_version,
    clear_daemon_pid,
    detect_account,
    read_daemon_pid,
    save_daemon_pid,
)


@pytest.fixture(autouse=True)
def reset_account_cache(monkeypatch):
    """Reset the module-level account cache before each test."""
    monkeypatch.setattr(config_mod, "_account_cache", None)


# ── detect_account ────────────────────────────────────────────────────────────

def test_detect_account_cache_hit(monkeypatch):
    monkeypatch.setattr(config_mod, "_account_cache", "+49999888777")
    result = detect_account()
    assert result == "+49999888777"


def test_detect_account_accounts_json_fast_path(monkeypatch, tmp_path):
    accounts_json = tmp_path / "accounts.json"
    accounts_json.write_text(json.dumps({"accounts": [{"number": "+49123456789"}]}))
    monkeypatch.setattr(config_mod, "_ACCOUNTS_JSON", accounts_json)
    result = detect_account()
    assert result == "+49123456789"
    # Cache should now be populated
    assert config_mod._account_cache == "+49123456789"


def test_detect_account_accounts_json_bad_json_falls_through(monkeypatch, tmp_path):
    """Invalid JSON in accounts.json triggers except block and falls through to subprocess."""
    accounts_json = tmp_path / "accounts.json"
    accounts_json.write_text("NOT VALID JSON }{")
    monkeypatch.setattr(config_mod, "_ACCOUNTS_JSON", accounts_json)
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "Number: +49fallback\n"
    mock_result.stderr = ""
    with patch("signal_mcp.config.subprocess.run", return_value=mock_result):
        result = detect_account()
    assert result == "+49fallback"


def test_detect_account_accounts_json_skips_no_plus(monkeypatch, tmp_path):
    """Accounts without leading '+' are skipped; falls through to subprocess."""
    accounts_json = tmp_path / "accounts.json"
    accounts_json.write_text(json.dumps({"accounts": [{"number": "4912345"}]}))
    monkeypatch.setattr(config_mod, "_ACCOUNTS_JSON", accounts_json)
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "Number: +49111222333\n"
    mock_result.stderr = ""
    with patch("signal_mcp.config.subprocess.run", return_value=mock_result):
        result = detect_account()
    assert result == "+49111222333"


def test_detect_account_subprocess_number_line(monkeypatch, tmp_path):
    """Subprocess fallback: 'Number: +49...' line parsed correctly."""
    monkeypatch.setattr(config_mod, "_ACCOUNTS_JSON", tmp_path / "nonexistent.json")
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "Number: +49123000000\n"
    mock_result.stderr = ""
    with patch("signal_mcp.config.subprocess.run", return_value=mock_result):
        result = detect_account()
    assert result == "+49123000000"


def test_detect_account_subprocess_plus_line(monkeypatch, tmp_path):
    """Subprocess fallback: line starting with '+' (older signal-cli format)."""
    monkeypatch.setattr(config_mod, "_ACCOUNTS_JSON", tmp_path / "nonexistent.json")
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "+49555000111 (registered)\n"
    mock_result.stderr = ""
    with patch("signal_mcp.config.subprocess.run", return_value=mock_result):
        result = detect_account()
    assert result == "+49555000111"


def test_detect_account_subprocess_nonzero_exit(monkeypatch, tmp_path):
    """Non-zero exit from signal-cli raises RuntimeError."""
    monkeypatch.setattr(config_mod, "_ACCOUNTS_JSON", tmp_path / "nonexistent.json")
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "some error"
    with patch("signal_mcp.config.subprocess.run", return_value=mock_result):
        with pytest.raises(RuntimeError, match="listAccounts failed"):
            detect_account()


def test_detect_account_subprocess_no_matching_line(monkeypatch, tmp_path):
    """Zero exit but no parseable account line raises RuntimeError."""
    monkeypatch.setattr(config_mod, "_ACCOUNTS_JSON", tmp_path / "nonexistent.json")
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "No accounts configured.\n"
    mock_result.stderr = ""
    with patch("signal_mcp.config.subprocess.run", return_value=mock_result):
        with pytest.raises(RuntimeError, match="No Signal account found"):
            detect_account()


# ── check_signal_cli_version ──────────────────────────────────────────────────

def test_check_signal_cli_version_success():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "signal-cli 0.14.3\n"
    mock_result.stderr = ""
    with patch("signal_mcp.config.subprocess.run", return_value=mock_result):
        check_signal_cli_version()  # should not raise


def test_check_signal_cli_version_too_old():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "signal-cli 0.12.0\n"
    mock_result.stderr = ""
    with patch("signal_mcp.config.subprocess.run", return_value=mock_result):
        with pytest.raises(RuntimeError, match="too old"):
            check_signal_cli_version()


def test_check_signal_cli_version_not_found():
    with patch("signal_mcp.config.subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(RuntimeError, match="not found"):
            check_signal_cli_version()


def test_check_signal_cli_version_timeout():
    with patch("signal_mcp.config.subprocess.run", side_effect=subprocess.TimeoutExpired("signal-cli", 10)):
        with pytest.raises(RuntimeError, match="timed out"):
            check_signal_cli_version()


def test_check_signal_cli_version_nonzero_exit():
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "crashed"
    with patch("signal_mcp.config.subprocess.run", return_value=mock_result):
        with pytest.raises(RuntimeError, match="exited with code"):
            check_signal_cli_version()


def test_check_signal_cli_version_unparseable():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "no version here\n"
    mock_result.stderr = ""
    with patch("signal_mcp.config.subprocess.run", return_value=mock_result):
        with pytest.raises(RuntimeError, match="Could not parse"):
            check_signal_cli_version()


# ── save_daemon_pid / read_daemon_pid / clear_daemon_pid ──────────────────────

def test_save_and_read_daemon_pid(monkeypatch, tmp_path):
    pid_file = tmp_path / "daemon.pid"
    monkeypatch.setattr(config_mod, "DAEMON_PID_FILE", pid_file)
    save_daemon_pid(12345)
    assert pid_file.exists()
    assert read_daemon_pid() == 12345


def test_read_daemon_pid_missing(monkeypatch, tmp_path):
    pid_file = tmp_path / "nonexistent.pid"
    monkeypatch.setattr(config_mod, "DAEMON_PID_FILE", pid_file)
    assert read_daemon_pid() is None


def test_read_daemon_pid_non_int(monkeypatch, tmp_path):
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text("not-a-number")
    monkeypatch.setattr(config_mod, "DAEMON_PID_FILE", pid_file)
    assert read_daemon_pid() is None


def test_clear_daemon_pid_removes_file(monkeypatch, tmp_path):
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text("9999")
    monkeypatch.setattr(config_mod, "DAEMON_PID_FILE", pid_file)
    clear_daemon_pid()
    assert not pid_file.exists()


def test_clear_daemon_pid_safe_when_absent(monkeypatch, tmp_path):
    pid_file = tmp_path / "no_such_file.pid"
    monkeypatch.setattr(config_mod, "DAEMON_PID_FILE", pid_file)
    clear_daemon_pid()  # should not raise
