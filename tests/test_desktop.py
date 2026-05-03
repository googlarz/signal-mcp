"""Tests for Signal Desktop importer."""

import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from signal_mcp.desktop import (
    DesktopImportError,
    _decode_group_id,
    _decrypt_key,
    _read_messages_from_plain_db,
    import_from_desktop,
)


# ── Unit tests ─────────────────────────────────────────────────────────────────

def test_decode_group_id_none():
    assert _decode_group_id(None) is None


def test_decode_group_id_too_long():
    assert _decode_group_id("x" * 101) is None


def test_decode_group_id_blob_prefix():
    assert _decode_group_id("blob:something") is None


def test_decode_group_id_valid():
    assert _decode_group_id("abc123") == "abc123"


def test_decrypt_key_unknown_format():
    bad_hex = bytes(b"v99" + b"\x00" * 16).hex()
    with pytest.raises(DesktopImportError, match="Unknown encryptedKey format"):
        _decrypt_key(bad_hex, b"password")


# ── DB parsing test ─────────────────────────────────────────────────────────────

def _make_plain_db(tmp_path: Path) -> Path:
    """Create a minimal Signal-like plain SQLite DB for testing."""
    db_path = tmp_path / "plain.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            e164 TEXT,
            groupId TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            conversationId TEXT,
            type TEXT,
            body TEXT,
            sent_at INTEGER,
            received_at INTEGER,
            source TEXT,
            sourceUuid TEXT,
            hasAttachments INTEGER
        )
    """)
    conn.execute("INSERT INTO conversations VALUES ('conv1', '+49111', NULL)")
    conn.execute("INSERT INTO conversations VALUES ('grp1', NULL, 'group-abc')")
    conn.execute(
        "INSERT INTO messages VALUES ('m1', 'conv1', 'incoming', 'Hallo', 1717243200000, 1717243200000, '+49222', NULL, 0)"
    )
    conn.execute(
        "INSERT INTO messages VALUES ('m2', 'grp1', 'outgoing', 'Group msg', 1717243300000, 1717243300000, NULL, NULL, 0)"
    )
    conn.execute(
        "INSERT INTO messages VALUES ('m3', 'conv1', 'incoming', NULL, 1717243400000, 1717243400000, '+49222', NULL, 1)"
    )
    conn.commit()
    conn.close()
    return db_path


def test_read_messages_from_plain_db(tmp_path):
    db = _make_plain_db(tmp_path)
    messages = _read_messages_from_plain_db(db)
    assert len(messages) == 3
    bodies = [m.body for m in messages]
    assert "Hallo" in bodies
    assert "Group msg" in bodies
    # message with no body but hasAttachments=1 should be included
    assert "" in bodies


def test_read_messages_outgoing_sender(tmp_path):
    db = _make_plain_db(tmp_path)
    messages = _read_messages_from_plain_db(db, own_number="+49111")
    outgoing = [m for m in messages if m.body == "Group msg"]
    assert len(outgoing) == 1
    assert outgoing[0].sender == "+49111"


def test_read_messages_outgoing_sender_fallback(tmp_path):
    db = _make_plain_db(tmp_path)
    messages = _read_messages_from_plain_db(db, own_number="")
    outgoing = [m for m in messages if m.body == "Group msg"]
    assert outgoing[0].sender == "me"


def test_read_messages_timestamps(tmp_path):
    db = _make_plain_db(tmp_path)
    messages = _read_messages_from_plain_db(db)
    assert any(m.body == "Hallo" for m in messages)


# ── Integration-ish test (mocked) ───────────────────────────────────────────────

@patch("signal_mcp.desktop.detect_account", return_value="+49111")
@patch("signal_mcp.desktop._get_keychain_password")
@patch("signal_mcp.desktop._decrypt_key")
@patch("signal_mcp.desktop._decrypt_db_to_temp")
@patch("signal_mcp.desktop._read_messages_from_plain_db")
@patch("signal_mcp.desktop._store")
def test_import_from_desktop_success(
    mock_store, mock_read, mock_decrypt_db, mock_decrypt_key, mock_keychain,
    mock_detect, tmp_path
):
    # Set up fake Signal Desktop files
    signal_dir = tmp_path / "Signal"
    (signal_dir / "sql").mkdir(parents=True)
    (signal_dir / "sql" / "db.sqlite").write_bytes(b"fake")
    config = {"encryptedKey": "76313000" + "00" * 16}
    (signal_dir / "config.json").write_text(json.dumps(config))

    from signal_mcp import desktop as _desktop_module
    original_db = _desktop_module.SIGNAL_DB
    original_cfg = _desktop_module.SIGNAL_CONFIG
    _desktop_module.SIGNAL_DB = signal_dir / "sql" / "db.sqlite"
    _desktop_module.SIGNAL_CONFIG = signal_dir / "config.json"

    mock_keychain.return_value = b"testpassword"
    mock_decrypt_key.return_value = "aabbccdd" * 4
    fake_plain = tmp_path / "plain.db"
    fake_plain.write_bytes(b"x")
    mock_decrypt_db.return_value = fake_plain

    from signal_mcp.models import Message
    msg = Message(id="m1", sender="+1", body="hi", timestamp=datetime(2024, 1, 1))
    mock_read.return_value = [msg]
    mock_store.save_message.return_value = True

    result = import_from_desktop()

    assert result["total"] == 1
    assert result["imported"] == 1
    assert result["skipped"] == 0

    _desktop_module.SIGNAL_DB = original_db
    _desktop_module.SIGNAL_CONFIG = original_cfg


def test_import_from_desktop_no_db(tmp_path):
    from signal_mcp import desktop as _desktop_module
    original_db = _desktop_module.SIGNAL_DB
    _desktop_module.SIGNAL_DB = tmp_path / "nonexistent.db"

    with pytest.raises(DesktopImportError, match="not found"):
        import_from_desktop()

    _desktop_module.SIGNAL_DB = original_db


# ── Platform path detection ────────────────────────────────────────────────────

def test_signal_dir_linux(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with patch("signal_mcp.desktop.platform.system", return_value="Linux"):
        from signal_mcp.desktop import _signal_dir
        d = _signal_dir()
    assert "Signal" in str(d)
    assert str(tmp_path) in str(d)


def test_signal_dir_linux_default(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    with patch("signal_mcp.desktop.platform.system", return_value="Linux"):
        from signal_mcp.desktop import _signal_dir
        d = _signal_dir()
    assert d == Path.home() / ".config" / "Signal"


def test_signal_dir_windows(monkeypatch):
    monkeypatch.setenv("APPDATA", "/fake/appdata")
    with patch("signal_mcp.desktop.platform.system", return_value="Windows"):
        from signal_mcp.desktop import _signal_dir
        d = _signal_dir()
    assert "Signal" in str(d)


def test_signal_dir_macos():
    with patch("signal_mcp.desktop.platform.system", return_value="Darwin"):
        from signal_mcp.desktop import _signal_dir
        d = _signal_dir()
    assert "Signal" in str(d)


# ── _get_db_key_hex dispatches by platform ─────────────────────────────────────

def test_get_db_key_hex_windows_path(monkeypatch):
    from signal_mcp import desktop as _d
    with patch("signal_mcp.desktop.platform.system", return_value="Windows"), \
         patch.object(_d, "_decrypt_dpapi_key", return_value="aabbccdd" * 8) as mock_dpapi:
        result = _d._get_db_key_hex("some_encrypted_hex")
    mock_dpapi.assert_called_once_with("some_encrypted_hex")
    assert result == "aabbccdd" * 8


def test_get_db_key_hex_macos_path(monkeypatch):
    from signal_mcp import desktop as _d
    with patch("signal_mcp.desktop.platform.system", return_value="Darwin"), \
         patch.object(_d, "_get_keychain_password", return_value=b"pass") as mock_kc, \
         patch.object(_d, "_decrypt_key", return_value="deadbeef" * 8) as mock_dk:
        result = _d._get_db_key_hex("v10" + "00" * 16)
    mock_kc.assert_called_once()
    mock_dk.assert_called_once()


# ── Temp file cleanup on error ─────────────────────────────────────────────────

@patch("signal_mcp.desktop.detect_account", return_value="+49111")
@patch("signal_mcp.desktop._get_keychain_password")
@patch("signal_mcp.desktop._decrypt_key")
@patch("signal_mcp.desktop._decrypt_db_to_temp")
@patch("signal_mcp.desktop._store")
def test_import_cleanup_on_parse_error(
    mock_store, mock_decrypt_db, mock_decrypt_key, mock_keychain, mock_detect, tmp_path
):
    """Temp file must be deleted even if _read_messages_from_plain_db raises."""
    from signal_mcp import desktop as _desktop_module
    signal_dir = tmp_path / "Signal"
    (signal_dir / "sql").mkdir(parents=True)
    (signal_dir / "sql" / "db.sqlite").write_bytes(b"fake")
    config = {"encryptedKey": "76313000" + "00" * 16}
    (signal_dir / "config.json").write_text(json.dumps(config))

    original_db = _desktop_module.SIGNAL_DB
    original_cfg = _desktop_module.SIGNAL_CONFIG
    _desktop_module.SIGNAL_DB = signal_dir / "sql" / "db.sqlite"
    _desktop_module.SIGNAL_CONFIG = signal_dir / "config.json"

    mock_keychain.return_value = b"pass"
    mock_decrypt_key.return_value = "aa" * 32
    fake_tmp = tmp_path / "tmp_plain.db"
    fake_tmp.write_bytes(b"x")
    mock_decrypt_db.return_value = fake_tmp

    with patch("signal_mcp.desktop._read_messages_from_plain_db", side_effect=RuntimeError("parse error")):
        with pytest.raises(RuntimeError, match="parse error"):
            import_from_desktop()

    # Temp file must have been cleaned up
    assert not fake_tmp.exists()

    _desktop_module.SIGNAL_DB = original_db
    _desktop_module.SIGNAL_CONFIG = original_cfg


# ── Linux keychain fallback ────────────────────────────────────────────────────

def test_linux_keychain_fallback_to_peanuts(monkeypatch):
    """When secret-tool is unavailable on Linux, password should fall back to 'peanuts'."""
    from signal_mcp.desktop import _get_keychain_password
    with patch("signal_mcp.desktop.platform.system", return_value="Linux"), \
         patch("signal_mcp.desktop.subprocess.run", side_effect=FileNotFoundError):
        result = _get_keychain_password()
    assert result == b"peanuts"


def test_linux_keychain_secret_tool_success(monkeypatch):
    """When secret-tool succeeds on Linux, its output is returned."""
    from signal_mcp.desktop import _get_keychain_password
    mock_run = MagicMock()
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "my_secret\n"
    with patch("signal_mcp.desktop.platform.system", return_value="Linux"), \
         patch("signal_mcp.desktop.subprocess.run", mock_run):
        result = _get_keychain_password()
    assert result == b"my_secret"
