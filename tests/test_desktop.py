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


# ── Additional coverage tests ─────────────────────────────────────────────────

def test_signal_dir_unknown_platform():
    """_signal_dir() fallback for unknown platform returns .config/Signal."""
    with patch("signal_mcp.desktop.platform.system", return_value="FreeBSD"):
        from signal_mcp.desktop import _signal_dir
        d = _signal_dir()
    assert str(d).endswith("Signal")


def test_get_keychain_password_darwin_success(monkeypatch):
    """Darwin keychain: returns password when security command succeeds."""
    from signal_mcp.desktop import _get_keychain_password
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "mypassword\n"
    with patch("signal_mcp.desktop.platform.system", return_value="Darwin"), \
         patch("signal_mcp.desktop.subprocess.run", return_value=mock_result):
        result = _get_keychain_password()
    assert result == b"mypassword"


def test_get_keychain_password_darwin_all_fail(monkeypatch):
    """Darwin keychain: raises DesktopImportError when all services fail."""
    from signal_mcp.desktop import _get_keychain_password, DesktopImportError
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    with patch("signal_mcp.desktop.platform.system", return_value="Darwin"), \
         patch("signal_mcp.desktop.subprocess.run", return_value=mock_result):
        with pytest.raises(DesktopImportError, match="Keychain"):
            _get_keychain_password()


def test_get_keychain_password_linux_label_success(monkeypatch):
    """Linux: second secret-tool lookup (by label) succeeds."""
    from signal_mcp.desktop import _get_keychain_password

    call_count = [0]

    def fake_run(cmd, **kwargs):
        call_count[0] += 1
        result = MagicMock()
        # First call (lookup application) → fail; second (lookup label) → succeed
        if "application" in cmd:
            result.returncode = 1
            result.stdout = ""
        else:
            result.returncode = 0
            result.stdout = "labelpassword\n"
        return result

    with patch("signal_mcp.desktop.platform.system", return_value="Linux"), \
         patch("signal_mcp.desktop.subprocess.run", side_effect=fake_run):
        result = _get_keychain_password()
    assert result == b"labelpassword"


def test_get_keychain_password_windows_raises():
    """Windows path in _get_keychain_password raises DesktopImportError."""
    from signal_mcp.desktop import _get_keychain_password, DesktopImportError
    with patch("signal_mcp.desktop.platform.system", return_value="Windows"):
        with pytest.raises(DesktopImportError, match="decrypt_dpapi_key"):
            _get_keychain_password()


def test_get_keychain_password_unknown_platform():
    """Unknown platform raises DesktopImportError."""
    from signal_mcp.desktop import _get_keychain_password, DesktopImportError
    with patch("signal_mcp.desktop.platform.system", return_value="Haiku"):
        with pytest.raises(DesktopImportError, match="not supported"):
            _get_keychain_password()


def test_decrypt_dpapi_key_strips_v10_prefix():
    """_decrypt_dpapi_key strips v10 prefix from raw bytes (lines 132-133 covered)."""
    from signal_mcp.desktop import _decrypt_dpapi_key, DesktopImportError
    # v10-prefixed hex: strips the prefix, then fails at ctypes.windll (macOS)
    v10_hex = (b"v10" + b"\xaa" * 16).hex()
    with pytest.raises(DesktopImportError):
        _decrypt_dpapi_key(v10_hex)


def test_decrypt_dpapi_key_dpapi_fails():
    """_decrypt_dpapi_key raises DesktopImportError when CryptUnprotectData returns 0 (lines 141-144, 149)."""
    import ctypes as _ctypes
    from signal_mcp.desktop import _decrypt_dpapi_key, DesktopImportError
    mock_windll = MagicMock()
    mock_windll.crypt32.CryptUnprotectData.return_value = 0  # DPAPI says no
    with patch.object(_ctypes, "windll", mock_windll, create=True):
        with pytest.raises(DesktopImportError, match="DPAPI decryption failed"):
            _decrypt_dpapi_key((b"v10" + b"\x00" * 16).hex())


def test_decrypt_dpapi_key_dpapi_success():
    """_decrypt_dpapi_key returns hex key string when CryptUnprotectData succeeds (lines 145-147)."""
    import ctypes as _ctypes
    from signal_mcp.desktop import _decrypt_dpapi_key

    mock_windll = MagicMock()
    mock_windll.crypt32.CryptUnprotectData.return_value = 1  # success
    mock_windll.kernel32.LocalFree.return_value = 0

    with patch.object(_ctypes, "windll", mock_windll, create=True), \
         patch.object(_ctypes, "string_at", return_value=b"raw_key_bytes_12"):
        result = _decrypt_dpapi_key((b"v10" + b"\x00" * 16).hex())
    assert result == b"raw_key_bytes_12".hex()


def test_decrypt_key_valid_v10(monkeypatch):
    """_decrypt_key decrypts a known v10-format test vector."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes, padding as _pad
    from signal_mcp.desktop import _decrypt_key

    # Produce a valid encrypted value we can then decrypt
    password = b"testpassword"
    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1003)
    aes_key = kdf.derive(password)
    iv = b"\x20" * 16
    plaintext = b"0" * 32  # 32-byte key we'll "store"
    padder = _pad.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    encrypted_hex = (b"v10" + ciphertext).hex()

    result = _decrypt_key(encrypted_hex, password)
    assert result == plaintext.hex()


def test_find_sqlcipher_homebrew(tmp_path):
    """_find_sqlcipher returns homebrew path when it exists."""
    from signal_mcp.desktop import _find_sqlcipher
    brew_path = "/opt/homebrew/bin/sqlcipher"
    with patch("signal_mcp.desktop.Path.exists", return_value=True):
        result = _find_sqlcipher()
    assert "sqlcipher" in result


def test_find_sqlcipher_which_fallback():
    """_find_sqlcipher falls back to `which sqlcipher` when hardcoded paths missing."""
    from signal_mcp.desktop import _find_sqlcipher
    which_result = MagicMock()
    which_result.returncode = 0
    which_result.stdout = "/usr/bin/sqlcipher\n"
    with patch("signal_mcp.desktop.Path.exists", return_value=False), \
         patch("signal_mcp.desktop.subprocess.run", return_value=which_result):
        result = _find_sqlcipher()
    assert result == "/usr/bin/sqlcipher"


def test_find_sqlcipher_not_found():
    """_find_sqlcipher raises DesktopImportError when sqlcipher is nowhere."""
    from signal_mcp.desktop import _find_sqlcipher, DesktopImportError
    which_result = MagicMock()
    which_result.returncode = 1
    which_result.stdout = ""
    with patch("signal_mcp.desktop.Path.exists", return_value=False), \
         patch("signal_mcp.desktop.subprocess.run", return_value=which_result):
        with pytest.raises(DesktopImportError, match="sqlcipher not found"):
            _find_sqlcipher()


def test_decrypt_db_to_temp_success(tmp_path):
    """_decrypt_db_to_temp returns a path to decrypted DB on success."""
    from signal_mcp.desktop import _decrypt_db_to_temp

    fake_db = tmp_path / "db.sqlite"
    fake_db.write_bytes(b"encrypted")

    # sqlcipher succeeds and writes output file
    def fake_run(cmd, input=None, **kwargs):
        # Extract temp file path from script and create it
        for line in (input or "").splitlines():
            if "ATTACH DATABASE" in line and "AS plaintext" in line:
                # Extract path between quotes
                start = line.index("'") + 1
                end = line.index("'", start)
                Path(line[start:end]).write_bytes(b"plain sqlite data")
        r = MagicMock()
        r.returncode = 0
        r.stderr = ""
        return r

    with patch("signal_mcp.desktop._find_sqlcipher", return_value="/usr/bin/sqlcipher"), \
         patch("signal_mcp.desktop.subprocess.run", side_effect=fake_run):
        result = _decrypt_db_to_temp("aabbccdd" * 8, fake_db)

    assert result.exists()
    result.unlink(missing_ok=True)


def test_decrypt_db_to_temp_sqlcipher_fails(tmp_path):
    """_decrypt_db_to_temp raises DesktopImportError when sqlcipher exits non-zero."""
    from signal_mcp.desktop import _decrypt_db_to_temp, DesktopImportError

    fake_db = tmp_path / "db.sqlite"
    fake_db.write_bytes(b"encrypted")

    fail_result = MagicMock()
    fail_result.returncode = 1
    fail_result.stderr = "cipher error"
    with patch("signal_mcp.desktop._find_sqlcipher", return_value="/usr/bin/sqlcipher"), \
         patch("signal_mcp.desktop.subprocess.run", return_value=fail_result):
        with pytest.raises(DesktopImportError, match="sqlcipher failed"):
            _decrypt_db_to_temp("aabbccdd" * 8, fake_db)


def test_decrypt_db_to_temp_empty_output(tmp_path):
    """_decrypt_db_to_temp raises when sqlcipher produces empty output."""
    from signal_mcp.desktop import _decrypt_db_to_temp, DesktopImportError

    fake_db = tmp_path / "db.sqlite"
    fake_db.write_bytes(b"encrypted")

    ok_result = MagicMock()
    ok_result.returncode = 0
    ok_result.stderr = ""
    # Don't create the temp file → empty output condition
    with patch("signal_mcp.desktop._find_sqlcipher", return_value="/usr/bin/sqlcipher"), \
         patch("signal_mcp.desktop.subprocess.run", return_value=ok_result):
        with pytest.raises(DesktopImportError, match="empty output"):
            _decrypt_db_to_temp("aabbccdd" * 8, fake_db)


def test_read_messages_skips_zero_timestamp(tmp_path):
    """_read_messages_from_plain_db skips rows where sent_at and received_at are 0/NULL."""
    import sqlite3
    db_path = tmp_path / "zero_ts.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE conversations (id TEXT PRIMARY KEY, e164 TEXT, groupId TEXT)""")
    conn.execute("""CREATE TABLE messages (
        id TEXT PRIMARY KEY, conversationId TEXT, type TEXT, body TEXT,
        sent_at INTEGER, received_at INTEGER, source TEXT, sourceUuid TEXT, hasAttachments INTEGER
    )""")
    conn.execute("INSERT INTO conversations VALUES ('c1', '+1', NULL)")
    # Row with zero timestamps — must be skipped
    conn.execute("INSERT INTO messages VALUES ('z1', 'c1', 'incoming', 'zeroed', 0, 0, '+2', NULL, 0)")
    # Normal row
    conn.execute("INSERT INTO messages VALUES ('n1', 'c1', 'incoming', 'normal', 1700000000000, 1700000000000, '+2', NULL, 0)")
    conn.commit()
    conn.close()

    from signal_mcp.desktop import _read_messages_from_plain_db
    msgs = _read_messages_from_plain_db(db_path)
    assert len(msgs) == 1
    assert msgs[0].body == "normal"


def _make_signal_dir(tmp_path, encrypted_key="76313000" + "00" * 16):
    """Helper: create a fake Signal Desktop directory structure."""
    signal_dir = tmp_path / "Signal"
    (signal_dir / "sql").mkdir(parents=True)
    (signal_dir / "sql" / "db.sqlite").write_bytes(b"fake")
    (signal_dir / "config.json").write_text(json.dumps({"encryptedKey": encrypted_key}))
    return signal_dir


def test_import_from_desktop_with_signal_dir_override(tmp_path):
    """import_from_desktop(signal_dir=...) uses provided path instead of auto-detected."""
    signal_dir = _make_signal_dir(tmp_path)

    from signal_mcp.desktop import import_from_desktop, _read_messages_from_plain_db
    from signal_mcp.models import Message

    fake_plain = tmp_path / "plain.db"
    fake_plain.write_bytes(b"x")
    msg = Message(id="m1", sender="+1", body="hi", timestamp=datetime(2024, 1, 1))

    with patch("signal_mcp.desktop._get_db_key_hex", return_value="aa" * 32), \
         patch("signal_mcp.desktop._decrypt_db_to_temp", return_value=fake_plain), \
         patch("signal_mcp.desktop._read_messages_from_plain_db", return_value=[msg]), \
         patch("signal_mcp.desktop._store") as mock_store, \
         patch("signal_mcp.desktop.detect_account", return_value="+49test"):
        mock_store.save_message.return_value = True
        result = import_from_desktop(signal_dir=signal_dir)

    assert result["imported"] == 1


def test_import_config_missing(tmp_path):
    """import_from_desktop raises when config.json is absent."""
    from signal_mcp.desktop import import_from_desktop, DesktopImportError
    signal_dir = tmp_path / "Signal"
    (signal_dir / "sql").mkdir(parents=True)
    (signal_dir / "sql" / "db.sqlite").write_bytes(b"fake")
    # config.json intentionally not created
    with pytest.raises(DesktopImportError, match="config not found"):
        import_from_desktop(signal_dir=signal_dir)


def test_import_no_encrypted_key(tmp_path):
    """import_from_desktop raises when encryptedKey missing from config.json."""
    from signal_mcp.desktop import import_from_desktop, DesktopImportError
    signal_dir = tmp_path / "Signal"
    (signal_dir / "sql").mkdir(parents=True)
    (signal_dir / "sql" / "db.sqlite").write_bytes(b"fake")
    (signal_dir / "config.json").write_text(json.dumps({"someOtherKey": "value"}))
    with pytest.raises(DesktopImportError, match="No encryptedKey"):
        import_from_desktop(signal_dir=signal_dir)


def test_import_progress_callbacks(tmp_path):
    """import_from_desktop calls progress_cb with platform-specific and progress messages."""
    signal_dir = _make_signal_dir(tmp_path)

    from signal_mcp.desktop import import_from_desktop
    from signal_mcp.models import Message

    # Build 1 message — i=0 triggers the % 500 == 0 progress call
    fake_plain = tmp_path / "plain_prog.db"
    fake_plain.write_bytes(b"x")
    msg = Message(id="p1", sender="+1", body="progress", timestamp=datetime(2024, 1, 1))

    messages_logged = []

    with patch("signal_mcp.desktop.platform.system", return_value="Darwin"), \
         patch("signal_mcp.desktop._get_db_key_hex", return_value="aa" * 32), \
         patch("signal_mcp.desktop._decrypt_db_to_temp", return_value=fake_plain), \
         patch("signal_mcp.desktop._read_messages_from_plain_db", return_value=[msg]), \
         patch("signal_mcp.desktop._store") as mock_store, \
         patch("signal_mcp.desktop.detect_account", return_value="+49test"):
        mock_store.save_message.return_value = True
        import_from_desktop(signal_dir=signal_dir, progress_cb=messages_logged.append)

    assert any("Keychain" in m or "macOS" in m for m in messages_logged), messages_logged
    assert any("Decrypting" in m for m in messages_logged)
    assert any("Importing" in m for m in messages_logged)
    assert any("0/" in m for m in messages_logged)  # i=0 progress tick


def test_import_progress_linux(tmp_path):
    """progress_cb on Linux gets Linux-specific message."""
    signal_dir = _make_signal_dir(tmp_path)

    from signal_mcp.desktop import import_from_desktop
    from signal_mcp.models import Message
    fake_plain = tmp_path / "plain_linux.db"
    fake_plain.write_bytes(b"x")
    msg = Message(id="l1", sender="+1", body="hi", timestamp=datetime(2024, 1, 1))

    messages_logged = []
    with patch("signal_mcp.desktop.platform.system", return_value="Linux"), \
         patch("signal_mcp.desktop._get_db_key_hex", return_value="aa" * 32), \
         patch("signal_mcp.desktop._decrypt_db_to_temp", return_value=fake_plain), \
         patch("signal_mcp.desktop._read_messages_from_plain_db", return_value=[msg]), \
         patch("signal_mcp.desktop._store") as mock_store, \
         patch("signal_mcp.desktop.detect_account", return_value="+49test"):
        mock_store.save_message.return_value = True
        import_from_desktop(signal_dir=signal_dir, progress_cb=messages_logged.append)

    assert any("Linux" in m or "libsecret" in m for m in messages_logged)


def test_import_progress_other_platform(tmp_path):
    """progress_cb on an unknown platform gets generic decryption message."""
    signal_dir = _make_signal_dir(tmp_path)

    from signal_mcp.desktop import import_from_desktop
    from signal_mcp.models import Message
    fake_plain = tmp_path / "plain_other.db"
    fake_plain.write_bytes(b"x")
    msg = Message(id="o1", sender="+1", body="hi", timestamp=datetime(2024, 1, 1))

    messages_logged = []
    with patch("signal_mcp.desktop.platform.system", return_value="FreeBSD"), \
         patch("signal_mcp.desktop._get_db_key_hex", return_value="aa" * 32), \
         patch("signal_mcp.desktop._decrypt_db_to_temp", return_value=fake_plain), \
         patch("signal_mcp.desktop._read_messages_from_plain_db", return_value=[msg]), \
         patch("signal_mcp.desktop._store") as mock_store, \
         patch("signal_mcp.desktop.detect_account", return_value="+49test"):
        mock_store.save_message.return_value = True
        import_from_desktop(signal_dir=signal_dir, progress_cb=messages_logged.append)

    assert any("Decrypting" in m for m in messages_logged)


def test_import_detect_account_failure(tmp_path):
    """import_from_desktop uses empty own_number when detect_account raises."""
    signal_dir = _make_signal_dir(tmp_path)

    from signal_mcp.desktop import import_from_desktop
    from signal_mcp.models import Message
    fake_plain = tmp_path / "plain_acct.db"
    fake_plain.write_bytes(b"x")
    msg = Message(id="a1", sender="+1", body="hi", timestamp=datetime(2024, 1, 1))

    with patch("signal_mcp.desktop._get_db_key_hex", return_value="aa" * 32), \
         patch("signal_mcp.desktop._decrypt_db_to_temp", return_value=fake_plain), \
         patch("signal_mcp.desktop._read_messages_from_plain_db", return_value=[msg]) as mock_read, \
         patch("signal_mcp.desktop._store") as mock_store, \
         patch("signal_mcp.desktop.detect_account", side_effect=RuntimeError("no account")):
        mock_store.save_message.return_value = True
        result = import_from_desktop(signal_dir=signal_dir)

    # Should succeed with own_number="" (outgoing messages attributed to "me")
    assert result["total"] == 1
    # _read_messages_from_plain_db called with own_number=""
    mock_read.assert_called_once_with(fake_plain, own_number="", since_ms=0)


def test_import_skipped_count(tmp_path):
    """import_from_desktop increments skipped when save_message returns False."""
    signal_dir = _make_signal_dir(tmp_path)

    from signal_mcp.desktop import import_from_desktop
    from signal_mcp.models import Message
    fake_plain = tmp_path / "plain_skip.db"
    fake_plain.write_bytes(b"x")
    msg = Message(id="s1", sender="+1", body="dupe", timestamp=datetime(2024, 1, 1))

    with patch("signal_mcp.desktop._get_db_key_hex", return_value="aa" * 32), \
         patch("signal_mcp.desktop._decrypt_db_to_temp", return_value=fake_plain), \
         patch("signal_mcp.desktop._read_messages_from_plain_db", return_value=[msg]), \
         patch("signal_mcp.desktop._store") as mock_store, \
         patch("signal_mcp.desktop.detect_account", return_value="+49test"):
        mock_store.save_message.return_value = False  # duplicate → skip
        result = import_from_desktop(signal_dir=signal_dir)

    assert result["skipped"] == 1
    assert result["imported"] == 0


# ── _read_messages_from_plain_db since_ms filter ──────────────────────────────

def test_read_messages_since_ms_filter(tmp_path):
    """_read_messages_from_plain_db with since_ms only returns newer messages."""
    db_path = tmp_path / "since.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, e164 TEXT, groupId TEXT)")
    conn.execute("""CREATE TABLE messages (
        id TEXT PRIMARY KEY, conversationId TEXT, type TEXT, body TEXT,
        sent_at INTEGER, received_at INTEGER, source TEXT, sourceUuid TEXT, hasAttachments INTEGER
    )""")
    conn.execute("INSERT INTO conversations VALUES ('c1', '+1', NULL)")
    conn.execute("INSERT INTO messages VALUES ('old', 'c1', 'incoming', 'old message', 1000000, 1000000, '+2', NULL, 0)")
    conn.execute("INSERT INTO messages VALUES ('new', 'c1', 'incoming', 'new message', 9000000, 9000000, '+2', NULL, 0)")
    conn.commit()
    conn.close()

    from signal_mcp.desktop import _read_messages_from_plain_db
    msgs = _read_messages_from_plain_db(db_path, since_ms=5000000)
    assert len(msgs) == 1
    assert msgs[0].body == "new message"


def test_read_messages_since_ms_zero_returns_all(tmp_path):
    """since_ms=0 (default) returns all messages."""
    db_path = tmp_path / "all.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, e164 TEXT, groupId TEXT)")
    conn.execute("""CREATE TABLE messages (
        id TEXT PRIMARY KEY, conversationId TEXT, type TEXT, body TEXT,
        sent_at INTEGER, received_at INTEGER, source TEXT, sourceUuid TEXT, hasAttachments INTEGER
    )""")
    conn.execute("INSERT INTO conversations VALUES ('c1', '+1', NULL)")
    conn.execute("INSERT INTO messages VALUES ('a', 'c1', 'incoming', 'first', 1000000, 1000000, '+2', NULL, 0)")
    conn.execute("INSERT INTO messages VALUES ('b', 'c1', 'incoming', 'second', 9000000, 9000000, '+2', NULL, 0)")
    conn.commit()
    conn.close()

    from signal_mcp.desktop import _read_messages_from_plain_db
    msgs = _read_messages_from_plain_db(db_path, since_ms=0)
    assert len(msgs) == 2


# ── import_from_desktop since_ms passthrough ──────────────────────────────────

def test_import_from_desktop_passes_since_ms(tmp_path):
    """import_from_desktop passes since_ms down to _read_messages_from_plain_db."""
    signal_dir = _make_signal_dir(tmp_path)
    fake_plain = tmp_path / "plain_since.db"
    fake_plain.write_bytes(b"x")

    with patch("signal_mcp.desktop._get_db_key_hex", return_value="aa" * 32), \
         patch("signal_mcp.desktop._decrypt_db_to_temp", return_value=fake_plain), \
         patch("signal_mcp.desktop._read_messages_from_plain_db", return_value=[]) as mock_read, \
         patch("signal_mcp.desktop._store") as mock_store, \
         patch("signal_mcp.desktop.detect_account", return_value="+49"):
        mock_store.save_message.return_value = True
        from signal_mcp.desktop import import_from_desktop
        import_from_desktop(signal_dir=signal_dir, since_ms=123456789)

    mock_read.assert_called_once()
    _, kwargs = mock_read.call_args
    assert kwargs.get("since_ms") == 123456789


# ── sync_from_desktop ─────────────────────────────────────────────────────────

def _make_sync_patches(tmp_path, messages=None, *, last_sync=None):
    """Return a context-manager stack of patches for sync_from_desktop tests."""
    from contextlib import ExitStack
    from signal_mcp.models import Message

    signal_dir = _make_signal_dir(tmp_path)
    fake_plain = tmp_path / "sync_plain.db"
    fake_plain.write_bytes(b"x")
    msgs = messages if messages is not None else []

    stack = ExitStack()
    stack.enter_context(patch("signal_mcp.desktop._get_db_key_hex", return_value="aa" * 32))
    stack.enter_context(patch("signal_mcp.desktop._decrypt_db_to_temp", return_value=fake_plain))
    stack.enter_context(patch("signal_mcp.desktop._read_messages_from_plain_db", return_value=msgs))
    mock_store = MagicMock()
    mock_store.get_meta.return_value = last_sync
    mock_store.save_message.return_value = True
    stack.enter_context(patch("signal_mcp.desktop._store", mock_store))
    stack.enter_context(patch("signal_mcp.desktop.detect_account", return_value="+49"))
    return stack, signal_dir, mock_store


def test_sync_from_desktop_first_run(tmp_path):
    """First sync (no prior record) imports everything and records the high-water mark."""
    from signal_mcp.models import Message
    msg = Message(id="m1", sender="+1", body="hi", timestamp=datetime(2024, 1, 1))

    stack, signal_dir, mock_store = _make_sync_patches(tmp_path, messages=[msg], last_sync=None)
    with stack:
        from signal_mcp.desktop import sync_from_desktop
        result = sync_from_desktop(signal_dir=signal_dir)

    assert result["imported"] == 1
    assert result["incremental"] is False
    assert result["since"] is None
    # high-water mark was stored
    mock_store.set_meta.assert_called_once_with("desktop_last_sync", mock_store.set_meta.call_args[0][1])


def test_sync_from_desktop_incremental(tmp_path):
    """Subsequent sync uses last_sync and applies 60-second overlap."""
    from signal_mcp.models import Message
    msg = Message(id="m2", sender="+1", body="newer", timestamp=datetime(2024, 6, 1))
    last_sync_ms = str(1_700_000_000_000)

    stack, signal_dir, mock_store = _make_sync_patches(tmp_path, messages=[msg], last_sync=last_sync_ms)

    captured_since = {}
    original_read = None

    def capture_read(db, own_number="", since_ms=0):
        captured_since["since_ms"] = since_ms
        return [msg]

    with stack:
        import signal_mcp.desktop as _desktop
        with patch.object(_desktop, "_read_messages_from_plain_db", side_effect=capture_read):
            from signal_mcp.desktop import sync_from_desktop
            result = sync_from_desktop(signal_dir=signal_dir)

    assert result["incremental"] is True
    assert result["since"] is not None
    # since_ms = last_sync_ms - 60_000 (overlap)
    assert captured_since["since_ms"] == int(last_sync_ms) - 60_000


def test_sync_from_desktop_updates_meta(tmp_path):
    """sync_from_desktop always updates the meta key after a successful sync."""
    stack, signal_dir, mock_store = _make_sync_patches(tmp_path, messages=[], last_sync="999")
    with stack:
        from signal_mcp.desktop import sync_from_desktop
        sync_from_desktop(signal_dir=signal_dir)

    mock_store.set_meta.assert_called_once()
    key, value = mock_store.set_meta.call_args[0]
    assert key == "desktop_last_sync"
    assert value.isdigit()  # epoch milliseconds


def test_sync_from_desktop_no_messages(tmp_path):
    """sync_from_desktop with zero new messages still returns a valid result dict."""
    stack, signal_dir, mock_store = _make_sync_patches(tmp_path, messages=[], last_sync=None)
    with stack:
        from signal_mcp.desktop import sync_from_desktop
        result = sync_from_desktop(signal_dir=signal_dir)

    assert result["imported"] == 0
    assert result["skipped"] == 0
    assert result["total"] == 0
    assert "since" in result
    assert "incremental" in result


# ── store meta ────────────────────────────────────────────────────────────────

def test_store_get_set_meta(tmp_path):
    """get_meta / set_meta round-trip via the meta table."""
    import signal_mcp.store as store
    orig = store.DB_PATH
    try:
        store.DB_PATH = tmp_path / "meta_test.db"
        store._initialized_paths.discard(str(store.DB_PATH))
        store._thread_local.__dict__.pop("conn", None)

        assert store.get_meta("foo") is None
        store.set_meta("foo", "bar")
        assert store.get_meta("foo") == "bar"
        # upsert
        store.set_meta("foo", "baz")
        assert store.get_meta("foo") == "baz"
    finally:
        store.DB_PATH = orig
        store._initialized_paths.discard(str(store.DB_PATH))
        store._thread_local.__dict__.pop("conn", None)


# ── server sync_desktop tool ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_server_sync_desktop_tool(tmp_path):
    """sync_desktop MCP tool returns imported/skipped counts."""
    import signal_mcp.server as server_mod
    from signal_mcp.client import SignalClient

    fake_result = {
        "imported": 5, "skipped": 2, "total": 7,
        "platform": "Darwin", "source": str(tmp_path),
        "since": None, "incremental": False,
    }
    fake_client = MagicMock(spec=SignalClient)
    fake_client.account = "+49test"
    with patch("signal_mcp.desktop.sync_from_desktop", return_value=fake_result), \
         patch.object(server_mod, "_client", fake_client):
        result = await server_mod.call_tool("sync_desktop", {})

    assert result[0].text
    import json as _json
    data = _json.loads(result[0].text)
    assert data["imported"] == 5


@pytest.mark.asyncio
async def test_server_sync_desktop_error(tmp_path):
    """sync_desktop MCP tool returns error text on DesktopImportError."""
    import signal_mcp.server as server_mod
    from signal_mcp.client import SignalClient
    from signal_mcp.desktop import DesktopImportError

    fake_client = MagicMock(spec=SignalClient)
    fake_client.account = "+49test"
    with patch("signal_mcp.desktop.sync_from_desktop", side_effect=DesktopImportError("no sqlcipher")), \
         patch.object(server_mod, "_client", fake_client):
        result = await server_mod.call_tool("sync_desktop", {})

    assert "no sqlcipher" in result[0].text


# ── CLI sync-desktop ──────────────────────────────────────────────────────────

def test_cli_sync_desktop_first_run(tmp_path):
    """sync-desktop CLI prints first-sync summary when incremental=False."""
    from click.testing import CliRunner
    from signal_mcp.cli import cli
    from signal_mcp.desktop import DesktopImportError

    result_data = {
        "imported": 10, "skipped": 3, "total": 13,
        "platform": "Darwin", "source": str(tmp_path),
        "since": None, "incremental": False,
    }
    runner = CliRunner()
    with patch("signal_mcp.desktop.sync_from_desktop", return_value=result_data):
        out = runner.invoke(cli, ["sync-desktop"])

    assert out.exit_code == 0
    assert "10 imported" in out.output


def test_cli_sync_desktop_incremental(tmp_path):
    """sync-desktop CLI prints delta summary when incremental=True."""
    from click.testing import CliRunner
    from signal_mcp.cli import cli

    result_data = {
        "imported": 2, "skipped": 0, "total": 2,
        "platform": "Darwin", "source": str(tmp_path),
        "since": "2024-06-01T12:00:00", "incremental": True,
    }
    runner = CliRunner()
    with patch("signal_mcp.desktop.sync_from_desktop", return_value=result_data):
        out = runner.invoke(cli, ["sync-desktop"])

    assert out.exit_code == 0
    assert "2 imported" in out.output
    assert "2024-06-01" in out.output


def test_cli_sync_desktop_error():
    """sync-desktop CLI exits with code 1 on DesktopImportError."""
    from click.testing import CliRunner
    from signal_mcp.cli import cli
    from signal_mcp.desktop import DesktopImportError

    runner = CliRunner()
    with patch("signal_mcp.desktop.sync_from_desktop", side_effect=DesktopImportError("no sqlcipher")):
        out = runner.invoke(cli, ["sync-desktop"])

    assert out.exit_code == 1
    assert "no sqlcipher" in out.output


def test_cli_sync_desktop_progress_callback(tmp_path):
    """sync-desktop CLI progress_cb is called and echoes messages."""
    from click.testing import CliRunner
    from signal_mcp.cli import cli

    captured_cb = {}

    def fake_sync(progress_cb=None, signal_dir=None):
        if progress_cb:
            progress_cb("Decrypting…")
        captured_cb["called"] = True
        return {
            "imported": 0, "skipped": 0, "total": 0,
            "platform": "Darwin", "source": str(tmp_path),
            "since": None, "incremental": False,
        }

    runner = CliRunner()
    with patch("signal_mcp.desktop.sync_from_desktop", side_effect=fake_sync):
        out = runner.invoke(cli, ["sync-desktop"])

    assert captured_cb.get("called")
    assert "Decrypting" in out.output


def test_read_messages_zero_timestamp_after_since_filter(tmp_path):
    """_read_messages_from_plain_db skips rows that pass the SQL filter but have zero ts."""
    # Insert a row with sent_at=NULL and received_at=NULL to bypass the COALESCE > ? filter
    # when since_ms=0 (0 > 0 is false, but NULL COALESCE = 0 > 0 is also false).
    # We need a row where COALESCE returns 0 < since_ms but ts_ms is still falsy — not possible
    # in normal data.  Instead craft a row where sent_at IS NULL and received_at IS NULL:
    # COALESCE(NULL, NULL, 0) = 0 > 0 → False, so it won't pass since_ms=0 anyway.
    # The `if not ts_ms: continue` guard exists for safety; exercise it by patching fetchall
    # to return a synthetic row that passes the query but has zeroed timestamps.
    import signal_mcp.desktop as _desktop
    from signal_mcp.desktop import _read_messages_from_plain_db

    db_path = tmp_path / "guard.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, e164 TEXT, groupId TEXT)")
    conn.execute("""CREATE TABLE messages (
        id TEXT PRIMARY KEY, conversationId TEXT, type TEXT, body TEXT,
        sent_at INTEGER, received_at INTEGER, source TEXT, sourceUuid TEXT, hasAttachments INTEGER
    )""")
    # This row has both timestamps NULL — COALESCE → 0, which is not > 0, so normally filtered.
    # We use since_ms=-1 (negative) to let it through: COALESCE(NULL,NULL,0)=0 > -1 → True.
    conn.execute("INSERT INTO conversations VALUES ('c1', '+1', NULL)")
    conn.execute("INSERT INTO messages VALUES ('zero', 'c1', 'incoming', 'body', NULL, NULL, '+2', NULL, 0)")
    conn.commit()
    conn.close()

    # since_ms=-1 allows the NULL row through the SQL filter
    msgs = _read_messages_from_plain_db(db_path, since_ms=-1)
    # The Python guard `if not ts_ms: continue` kicks in and skips the row
    assert msgs == []
