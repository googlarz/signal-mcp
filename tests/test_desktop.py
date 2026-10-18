"""Tests for Signal Desktop importer."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from signal_mcp.desktop import (
    DesktopImportError,
    _decode_group_id,
    _decrypt_key,
    _quote_id,
    _read_conversation_names,
    _read_messages_from_plain_db,
    import_from_desktop,
)


@pytest.fixture(autouse=True)
def isolate_import_lock(tmp_path, monkeypatch):
    """Keep import_from_desktop off the real ~/.local/share lock file: concurrent
    pytest runs (or a live desktop sync) otherwise collide on it, and a test's
    cleanup would delete a real sync's lock."""
    monkeypatch.setattr("signal_mcp.desktop.DESKTOP_IMPORT_LOCK_FILE", tmp_path / "desktop-import.lock")


# ── Unit tests ─────────────────────────────────────────────────────────────────

def test_decode_group_id_none():
    assert _decode_group_id(None) is None


def test_decode_group_id_too_long():
    # Long group IDs are valid and must not be silently dropped.
    assert _decode_group_id("x" * 101) == "x" * 101


def test_decode_group_id_blob_prefix():
    # The "blob:" prefix is stripped, not treated as making the id invalid.
    assert _decode_group_id("blob:something") == "something"


def test_decode_group_id_valid():
    assert _decode_group_id("abc123") == "abc123"


def test_decrypt_key_unknown_format():
    bad_hex = bytes(b"v99" + b"\x00" * 16).hex()
    with pytest.raises(DesktopImportError, match="Unknown encryptedKey format"):
        _decrypt_key(bad_hex, b"password")


def _encrypt_key_like_chromium(db_key_hex: str, password: bytes, prefix: bytes, iterations: int) -> str:
    """Build an encryptedKey the way Chromium's OSCrypt does (test helper)."""
    from cryptography.hazmat.primitives import hashes, padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=iterations)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(db_key_hex.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(kdf.derive(password)), modes.CBC(b"\x20" * 16)).encryptor()
    return (prefix + encryptor.update(padded) + encryptor.finalize()).hex()


@pytest.mark.parametrize(
    "system, prefix, iterations, password",
    [
        ("Darwin", b"v10", 1003, b"keychain-password"),
        ("Linux", b"v11", 1, b"keyring-password"),  # libsecret / KWallet
        ("Linux", b"v10", 1, b"peanuts"),           # no keyring available
    ],
)
def test_decrypt_key_roundtrip_per_platform(system, prefix, iterations, password):
    db_key = "3a0aaac0" * 8
    encrypted = _encrypt_key_like_chromium(db_key, password, prefix, iterations)
    with patch("signal_mcp.desktop.platform.system", return_value=system):
        assert _decrypt_key(encrypted, password) == db_key


def test_decrypt_key_linux_does_not_use_macos_iterations():
    """A key encrypted with macOS parameters must not decrypt to the DB key on Linux."""
    db_key = "3a0aaac0" * 8
    encrypted = _encrypt_key_like_chromium(db_key, b"pw", b"v11", 1003)
    with patch("signal_mcp.desktop.platform.system", return_value="Linux"):
        try:
            result = _decrypt_key(encrypted, b"pw")
        except DesktopImportError:
            return  # wrong AES key → invalid padding, reported clearly
    assert result != db_key


def test_decrypt_key_wrong_password_raises_desktop_import_error():
    """A wrong password must not surface as a raw cryptography ValueError."""
    db_key = "3a0aaac0" * 8
    encrypted = _encrypt_key_like_chromium(db_key, b"correct-password", b"v10", 1003)
    with patch("signal_mcp.desktop.platform.system", return_value="Darwin"), \
         pytest.raises(DesktopImportError, match="password is wrong"):
        _decrypt_key(encrypted, b"wrong-password")


def test_decrypt_key_truncated_ciphertext_raises_desktop_import_error():
    """A truncated/corrupt ciphertext (not a multiple of the AES block size) must
    not surface as a raw cryptography ValueError."""
    bad_hex = (b"v10" + b"\x00" * 5).hex()  # 5 bytes: not block-aligned for AES-128
    with pytest.raises(DesktopImportError, match="password is wrong|corrupted"):
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
            hasAttachments INTEGER,
            readStatus INTEGER
        )
    """)
    conn.execute("INSERT INTO conversations VALUES ('conv1', '+49111', NULL)")
    conn.execute("INSERT INTO conversations VALUES ('grp1', NULL, 'group-abc')")
    conn.execute(
        "INSERT INTO messages VALUES ('m1', 'conv1', 'incoming', 'Hallo', 1717243200000, 1717243200000, '+49222', NULL, 0, 1)"
    )
    conn.execute(
        "INSERT INTO messages VALUES ('m2', 'grp1', 'outgoing', 'Group msg', 1717243300000, 1717243300000, NULL, NULL, 0, 0)"
    )
    conn.execute(
        "INSERT INTO messages VALUES ('m3', 'conv1', 'incoming', NULL, 1717243400000, 1717243400000, '+49222', NULL, 1, NULL)"
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


def test_read_messages_preserves_read_status(tmp_path):
    """readStatus=0 → is_read=True, 1 → False, NULL → True (safe default)."""
    db = _make_plain_db(tmp_path)
    messages = _read_messages_from_plain_db(db)
    by_id = {m.id: m for m in messages}
    # ids are the message timestamp (matches the live-message id scheme)
    # m1: readStatus=1 → unread
    assert by_id["1717243200000"].is_read is False
    # m2: readStatus=0 → read
    assert by_id["1717243300000"].is_read is True
    # m3: readStatus=NULL → default to read
    assert by_id["1717243400000"].is_read is True


def test_read_messages_without_readstatus_column(tmp_path):
    """DB without readStatus column defaults all messages to read."""
    db_path = tmp_path / "noread.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, e164 TEXT, groupId TEXT)")
    conn.execute("""CREATE TABLE messages (
        id TEXT PRIMARY KEY, conversationId TEXT, type TEXT, body TEXT,
        sent_at INTEGER, received_at INTEGER, source TEXT, sourceUuid TEXT, hasAttachments INTEGER
    )""")
    conn.execute("INSERT INTO conversations VALUES ('c1', '+1', NULL)")
    conn.execute("INSERT INTO messages VALUES ('x', 'c1', 'incoming', 'hi', 1000000, 1000000, '+2', NULL, 0)")
    conn.commit()
    conn.close()

    from signal_mcp.desktop import _read_messages_from_plain_db
    msgs = _read_messages_from_plain_db(db_path)
    assert len(msgs) == 1
    # Without readStatus column, default to read (safe fallback)
    assert msgs[0].is_read is True


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


# ── _quote_id ─────────────────────────────────────────────────────────────────

def test_quote_id_none_json():
    assert _quote_id(None) is None


def test_quote_id_no_quote_key():
    assert _quote_id('{"body": "hi"}') is None


def test_quote_id_invalid_json():
    assert _quote_id("not json") is None


def test_quote_id_extracts_sent_at():
    assert _quote_id('{"quote": {"id": 1717243200000, "authorAci": "x"}}') == "1717243200000"


def test_quote_id_rejects_non_numeric():
    assert _quote_id('{"quote": {"id": "not-a-number"}}') is None
    assert _quote_id('{"quote": {"id": true}}') is None
    assert _quote_id('{"quote": "not-a-dict"}') is None


def test_read_messages_sets_quote_id_from_json_column(tmp_path):
    """A reply imported from Signal Desktop must be joinable back to the
    message it quotes, the same way live-received replies already are."""
    db_path = tmp_path / "quotes.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, e164 TEXT, groupId TEXT)")
    conn.execute("""CREATE TABLE messages (
        id TEXT PRIMARY KEY, conversationId TEXT, type TEXT, body TEXT,
        sent_at INTEGER, received_at INTEGER, source TEXT, sourceUuid TEXT,
        hasAttachments INTEGER, json TEXT
    )""")
    conn.execute("INSERT INTO conversations VALUES ('c1', '+49222', NULL)")
    conn.execute(
        "INSERT INTO messages VALUES ('m1', 'c1', 'incoming', 'original', 1000, 1000, "
        "'+49222', NULL, 0, NULL)"
    )
    conn.execute(
        "INSERT INTO messages VALUES ('m2', 'c1', 'incoming', 'a reply', 2000, 2000, "
        "'+49222', NULL, 0, '{\"quote\": {\"id\": 1000}}')"
    )
    conn.commit()
    conn.close()

    messages = _read_messages_from_plain_db(db_path)
    by_body = {m.body: m for m in messages}
    assert by_body["a reply"].quote_id == "1000"
    assert by_body["original"].quote_id is None


# ── _read_conversation_names ─────────────────────────────────────────────────

def test_read_conversation_names_keys_direct_contact_by_e164(tmp_path):
    db = _make_plain_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE conversations ADD COLUMN name TEXT")
    conn.execute("ALTER TABLE conversations ADD COLUMN type TEXT")
    conn.execute("UPDATE conversations SET name = 'Alice', type = 'private' WHERE id = 'conv1'")
    conn.commit()
    conn.close()

    names = _read_conversation_names(db)
    assert ("+49111", "Alice", "direct") in names


def test_read_conversation_names_falls_back_to_service_id_without_e164(tmp_path):
    """A contact with no phone number stored is still someone worth naming."""
    db_path = tmp_path / "no-number.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, e164 TEXT, serviceId TEXT, "
        "groupId TEXT, type TEXT, name TEXT)"
    )
    conn.execute(
        "INSERT INTO conversations VALUES ('c1', NULL, 'uuid-only', NULL, 'private', 'Bob')"
    )
    conn.commit()
    conn.close()

    names = _read_conversation_names(db_path)
    assert ("uuid-only", "Bob", "direct") in names


def test_read_conversation_names_group(tmp_path):
    db = _make_plain_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE conversations ADD COLUMN name TEXT")
    conn.execute("ALTER TABLE conversations ADD COLUMN type TEXT")
    conn.execute("UPDATE conversations SET name = 'Team', type = 'group' WHERE id = 'grp1'")
    conn.commit()
    conn.close()

    names = _read_conversation_names(db)
    assert ("group-abc", "Team", "group") in names


def test_outgoing_direct_message_gets_a_recipient(tmp_path):
    """An outgoing DM must record who it went to, or list_conversations/
    get_conversation (which match on sender OR recipient) can never find it."""
    db = _make_plain_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO messages VALUES ('m4', 'conv1', 'outgoing', 'see you then', "
        "1717243500000, 1717243500000, NULL, NULL, 0, 0)"
    )
    conn.commit()
    conn.close()
    messages = _read_messages_from_plain_db(db, own_number="+49111")
    out = next(m for m in messages if m.body == "see you then")
    assert out.recipient == "+49111"  # conv1's own e164 in the fixture


def test_outgoing_direct_message_recipient_falls_back_to_service_id(tmp_path):
    """A contact with no phone number stored is still one conversation."""
    db_path = tmp_path / "no-number.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, e164 TEXT, serviceId TEXT, groupId TEXT)")
    conn.execute("""CREATE TABLE messages (
        id TEXT PRIMARY KEY, conversationId TEXT, type TEXT, body TEXT,
        sent_at INTEGER, received_at INTEGER, source TEXT, sourceServiceId TEXT, hasAttachments INTEGER
    )""")
    conn.execute("INSERT INTO conversations VALUES ('c1', NULL, 'uuid-only', NULL)")
    conn.execute("INSERT INTO messages VALUES ('out1', 'c1', 'outgoing', 'hi', 1000, 1000, NULL, NULL, 0)")
    conn.commit()
    conn.close()

    messages = _read_messages_from_plain_db(db_path, own_number="+15550001")
    assert messages[0].recipient == "uuid-only"


def test_both_halves_of_a_direct_conversation_share_one_identifier(tmp_path):
    """store.get_conversation matches sender = ? OR recipient = ? against a single
    value. If incoming (keyed by the sender's uuid, since Signal Desktop leaves
    `source` NULL and fills sourceServiceId) and outgoing (keyed by the
    conversation's e164) use different identifiers, a read by either one returns
    only half the conversation."""
    db_path = tmp_path / "one-identity.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, e164 TEXT, serviceId TEXT, groupId TEXT)")
    conn.execute("""CREATE TABLE messages (
        id TEXT PRIMARY KEY, conversationId TEXT, type TEXT, body TEXT,
        sent_at INTEGER, received_at INTEGER, source TEXT, sourceServiceId TEXT, hasAttachments INTEGER
    )""")
    conn.execute("INSERT INTO conversations VALUES ('c1', '+15550002', 'uuid-of-them', NULL)")
    # source NULL + sourceServiceId set is exactly how Signal Desktop stores an incoming message.
    conn.execute("INSERT INTO messages VALUES "
                  "('in1', 'c1', 'incoming', 'how are you', 1000, 1000, NULL, 'uuid-of-them', 0)")
    conn.execute("INSERT INTO messages VALUES "
                  "('out1', 'c1', 'outgoing', 'all good', 2000, 2000, NULL, NULL, 0)")
    conn.commit()
    conn.close()

    messages = _read_messages_from_plain_db(db_path, own_number="+15550001")
    by_id = {m.id: m for m in messages}  # id scheme is str(ts_ms), matching live-received messages
    identities = {by_id["1000"].sender, by_id["2000"].recipient}
    assert identities == {"+15550002"}, f"both halves must name the other party the same way, got {identities}"


def test_group_message_sender_is_still_the_member_not_the_group(tmp_path):
    """The one-identifier fix must not leak into group messages — a group's
    identity would otherwise name a member as "the group" instead of themselves."""
    db = _make_plain_db(tmp_path)
    messages = _read_messages_from_plain_db(db)
    incoming_group = None
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO messages VALUES ('m5', 'grp1', 'incoming', 'hi all', "
        "1717243600000, 1717243600000, NULL, 'member-uuid', 0, 0)"
    )
    conn.commit()
    conn.close()
    messages = _read_messages_from_plain_db(db)
    incoming_group = next(m for m in messages if m.body == "hi all")
    assert incoming_group.sender == "member-uuid"


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
    mock_store.save_messages_batch.return_value = (1, 0)

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


_V11_HEX = (b"v11" + b"\x00" * 16).hex()
_V10_HEX = (b"v10" + b"\x00" * 16).hex()


def test_get_db_key_hex_linux_v11_uses_keyring_password():
    from signal_mcp import desktop as _d
    with patch("signal_mcp.desktop.platform.system", return_value="Linux"), \
         patch.object(_d, "_linux_keyring_password", return_value=b"keyring-pw"), \
         patch.object(_d, "_decrypt_key", return_value="deadbeef" * 8) as mock_dk:
        result = _d._get_db_key_hex(_V11_HEX)
    mock_dk.assert_called_once_with(_V11_HEX, b"keyring-pw")
    assert result == "deadbeef" * 8


def test_get_db_key_hex_linux_v11_without_secret_tool_names_the_package():
    """v11 + no secret-tool must not fall back to 'peanuts' — it can never decrypt v11."""
    from signal_mcp import desktop as _d
    with patch("signal_mcp.desktop.platform.system", return_value="Linux"), \
         patch("signal_mcp.desktop.subprocess.run", side_effect=FileNotFoundError), \
         patch("signal_mcp.desktop.shutil.which", return_value=None), \
         patch.object(_d, "_decrypt_key") as mock_dk:
        with pytest.raises(DesktopImportError, match="secret-tool is not installed") as exc:
            _d._get_db_key_hex(_V11_HEX)
    assert "libsecret-tools" in str(exc.value)
    mock_dk.assert_not_called()


def test_get_db_key_hex_linux_v11_without_keyring_entry():
    from signal_mcp import desktop as _d
    no_entry = MagicMock()
    no_entry.return_value.returncode = 1
    no_entry.return_value.stdout = ""
    with patch("signal_mcp.desktop.platform.system", return_value="Linux"), \
         patch("signal_mcp.desktop.subprocess.run", no_entry), \
         patch("signal_mcp.desktop.shutil.which", return_value="/usr/bin/secret-tool"), \
         patch.object(_d, "_decrypt_key") as mock_dk:
        with pytest.raises(DesktopImportError, match="found no Signal entry"):
            _d._get_db_key_hex(_V11_HEX)
    mock_dk.assert_not_called()


def test_get_db_key_hex_linux_v11_secret_tool_timeout():
    """A secret-tool that never answers (e.g. stuck on an unlock prompt) must
    surface as a clear DesktopImportError, not a raw subprocess.TimeoutExpired."""
    import subprocess as _subprocess
    from signal_mcp import desktop as _d
    with patch("signal_mcp.desktop.platform.system", return_value="Linux"), \
         patch("signal_mcp.desktop.subprocess.run",
               side_effect=_subprocess.TimeoutExpired(cmd="secret-tool", timeout=10)), \
         patch.object(_d, "_decrypt_key") as mock_dk:
        with pytest.raises(DesktopImportError, match="did not respond in time"):
            _d._get_db_key_hex(_V11_HEX)
    mock_dk.assert_not_called()


def test_get_db_key_hex_linux_v10_keeps_peanuts_fallback():
    from signal_mcp import desktop as _d
    with patch("signal_mcp.desktop.platform.system", return_value="Linux"), \
         patch("signal_mcp.desktop.subprocess.run", side_effect=FileNotFoundError), \
         patch.object(_d, "_decrypt_key", return_value="deadbeef" * 8) as mock_dk:
        _d._get_db_key_hex(_V10_HEX)
    mock_dk.assert_called_once_with(_V10_HEX, b"peanuts")


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


def test_linux_keyring_password_application_lookup_success(monkeypatch):
    """When secret-tool's application lookup succeeds on Linux, its output is returned."""
    from signal_mcp.desktop import _linux_keyring_password
    mock_run = MagicMock()
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "my_secret\n"
    with patch("signal_mcp.desktop.subprocess.run", mock_run):
        result = _linux_keyring_password()
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


def test_linux_keyring_password_label_lookup_success(monkeypatch):
    """Linux: second secret-tool lookup (by label) succeeds."""
    from signal_mcp.desktop import _linux_keyring_password

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        # First call (lookup application) → fail; second (lookup label) → succeed
        if "application" in cmd:
            result.returncode = 1
            result.stdout = ""
        else:
            result.returncode = 0
            result.stdout = "labelpassword\n"
        return result

    with patch("signal_mcp.desktop.subprocess.run", side_effect=fake_run):
        result = _linux_keyring_password()
    assert result == b"labelpassword"


def test_get_keychain_password_linux_always_peanuts(monkeypatch):
    """Linux: _get_keychain_password is only used for v10 keys, which Signal
    Desktop always encrypts with this hardcoded password -- it must never try
    the keyring, since an unrelated app's entry under the same label would
    derive the wrong AES key (see _require_linux_keyring_password for v11)."""
    from signal_mcp.desktop import _get_keychain_password

    with patch("signal_mcp.desktop.platform.system", return_value="Linux"), \
         patch("signal_mcp.desktop.subprocess.run") as mock_run:
        result = _get_keychain_password()
    assert result == b"peanuts"
    mock_run.assert_not_called()


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
    """_decrypt_key decrypts a known v10-format test vector (macOS parameters)."""
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

    with patch("signal_mcp.desktop.platform.system", return_value="Darwin"):
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


@pytest.mark.parametrize(
    "system, expected, unexpected",
    [
        ("Darwin", "brew install sqlcipher", "apt"),
        ("Linux", "sudo apt install sqlcipher", "brew"),
        ("FreeBSD", "on your PATH", "brew"),
    ],
)
def test_find_sqlcipher_not_found_hint_matches_platform(system, expected, unexpected):
    from signal_mcp.desktop import _find_sqlcipher, DesktopImportError
    which_result = MagicMock()
    which_result.returncode = 1
    which_result.stdout = ""
    with patch("signal_mcp.desktop.Path.exists", return_value=False), \
         patch("signal_mcp.desktop.subprocess.run", return_value=which_result), \
         patch("signal_mcp.desktop.platform.system", return_value=system):
        with pytest.raises(DesktopImportError, match="sqlcipher not found") as exc:
            _find_sqlcipher()
    assert expected in str(exc.value)
    assert unexpected not in str(exc.value)


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
         patch("signal_mcp.desktop.subprocess.run", side_effect=fake_run), \
         patch("signal_mcp.desktop._PLAINTEXT_TMP_DIR", tmp_path / "plaintext-tmp"):
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
    tmp_dir = tmp_path / "plaintext-tmp"
    with patch("signal_mcp.desktop._find_sqlcipher", return_value="/usr/bin/sqlcipher"), \
         patch("signal_mcp.desktop.subprocess.run", return_value=fail_result), \
         patch("signal_mcp.desktop._PLAINTEXT_TMP_DIR", tmp_dir):
        with pytest.raises(DesktopImportError, match="sqlcipher failed"):
            _decrypt_db_to_temp("aabbccdd" * 8, fake_db)
    # A failed export must never leave the plaintext temp file behind.
    assert list(tmp_dir.iterdir()) == []


def test_decrypt_db_to_temp_cleans_up_on_subprocess_exception(tmp_path):
    """A subprocess.run exception (e.g. timeout) must not leave the plaintext
    temp file behind either — only the sqlcipher-failure paths did before."""
    from signal_mcp.desktop import _decrypt_db_to_temp

    fake_db = tmp_path / "db.sqlite"
    fake_db.write_bytes(b"encrypted")
    tmp_dir = tmp_path / "plaintext-tmp"

    with patch("signal_mcp.desktop._find_sqlcipher", return_value="/usr/bin/sqlcipher"), \
         patch("signal_mcp.desktop.subprocess.run", side_effect=TimeoutError("boom")), \
         patch("signal_mcp.desktop._PLAINTEXT_TMP_DIR", tmp_dir):
        with pytest.raises(TimeoutError):
            _decrypt_db_to_temp("aabbccdd" * 8, fake_db)
    assert list(tmp_dir.iterdir()) == []


def test_decrypt_db_to_temp_uses_a_private_directory(tmp_path):
    """The plaintext export directory must be created 0700 — it holds a full
    plaintext copy of the user's Signal message history."""
    from signal_mcp.desktop import _decrypt_db_to_temp
    import stat

    fake_db = tmp_path / "db.sqlite"
    fake_db.write_bytes(b"encrypted")
    tmp_dir = tmp_path / "plaintext-tmp"

    def fake_run(cmd, input=None, **kwargs):
        for line in (input or "").splitlines():
            if "ATTACH DATABASE" in line and "AS plaintext" in line:
                start = line.index("'") + 1
                end = line.index("'", start)
                Path(line[start:end]).write_bytes(b"plain sqlite data")
        r = MagicMock()
        r.returncode = 0
        r.stderr = ""
        return r

    with patch("signal_mcp.desktop._find_sqlcipher", return_value="/usr/bin/sqlcipher"), \
         patch("signal_mcp.desktop.subprocess.run", side_effect=fake_run), \
         patch("signal_mcp.desktop._PLAINTEXT_TMP_DIR", tmp_dir):
        result = _decrypt_db_to_temp("aabbccdd" * 8, fake_db)

    assert stat.S_IMODE(tmp_dir.stat().st_mode) == 0o700
    result.unlink(missing_ok=True)


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
         patch("signal_mcp.desktop.subprocess.run", return_value=ok_result), \
         patch("signal_mcp.desktop._PLAINTEXT_TMP_DIR", tmp_path / "plaintext-tmp"):
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
        sent_at INTEGER, received_at INTEGER, source TEXT, sourceUuid TEXT, hasAttachments INTEGER,
            readStatus INTEGER
    )""")
    conn.execute("INSERT INTO conversations VALUES ('c1', '+1', NULL)")
    # Row with zero timestamps — must be skipped
    conn.execute("INSERT INTO messages VALUES ('z1', 'c1', 'incoming', 'zeroed', 0, 0, '+2', NULL, 0, 1)")
    # Normal row
    conn.execute("INSERT INTO messages VALUES ('n1', 'c1', 'incoming', 'normal', 1700000000000, 1700000000000, '+2', NULL, 0, 1)")
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
        mock_store.save_messages_batch.return_value = (1, 0)
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
        mock_store.save_messages_batch.return_value = (1, 0)
        import_from_desktop(signal_dir=signal_dir, progress_cb=messages_logged.append)

    assert any("Keychain" in m or "macOS" in m for m in messages_logged), messages_logged
    assert any("Decrypting" in m for m in messages_logged)
    assert any("Importing" in m for m in messages_logged)
    assert any("/1 messages" in m for m in messages_logged)  # chunk progress tick


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
        mock_store.save_messages_batch.return_value = (1, 0)
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
        mock_store.save_messages_batch.return_value = (1, 0)
        import_from_desktop(signal_dir=signal_dir, progress_cb=messages_logged.append)

    assert any("Decrypting" in m for m in messages_logged)


def test_import_detect_account_failure(tmp_path):
    """import_from_desktop raises instead of silently falling back to own_number="" —
    a silent fallback would permanently misattribute every outgoing message's sender
    to the literal string "me" (see _read_messages_from_plain_db)."""
    signal_dir = _make_signal_dir(tmp_path)

    from signal_mcp.desktop import import_from_desktop, DesktopImportError
    fake_plain = tmp_path / "plain_acct.db"
    fake_plain.write_bytes(b"x")

    with patch("signal_mcp.desktop._get_db_key_hex", return_value="aa" * 32), \
         patch("signal_mcp.desktop._decrypt_db_to_temp", return_value=fake_plain), \
         patch("signal_mcp.desktop._store"), \
         patch("signal_mcp.desktop.detect_account", side_effect=RuntimeError("no account")):
        with pytest.raises(DesktopImportError, match="Could not detect your Signal account"):
            import_from_desktop(signal_dir=signal_dir)


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
        mock_store.save_message.return_value = False
        mock_store.save_messages_batch.return_value = (0, 1)
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
        sent_at INTEGER, received_at INTEGER, source TEXT, sourceUuid TEXT, hasAttachments INTEGER,
            readStatus INTEGER
    )""")
    conn.execute("INSERT INTO conversations VALUES ('c1', '+1', NULL)")
    conn.execute("INSERT INTO messages VALUES ('old', 'c1', 'incoming', 'old message', 1000000, 1000000, '+2', NULL, 0, 1)")
    conn.execute("INSERT INTO messages VALUES ('new', 'c1', 'incoming', 'new message', 9000000, 9000000, '+2', NULL, 0, 1)")
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
        sent_at INTEGER, received_at INTEGER, source TEXT, sourceUuid TEXT, hasAttachments INTEGER,
            readStatus INTEGER
    )""")
    conn.execute("INSERT INTO conversations VALUES ('c1', '+1', NULL)")
    conn.execute("INSERT INTO messages VALUES ('a', 'c1', 'incoming', 'first', 1000000, 1000000, '+2', NULL, 0, 1)")
    conn.execute("INSERT INTO messages VALUES ('b', 'c1', 'incoming', 'second', 9000000, 9000000, '+2', NULL, 0, 1)")
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
        mock_store.save_messages_batch.return_value = (1, 0)
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
    mock_store.save_messages_batch.return_value = (1, 0)
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
    """sync_from_desktop updates the watermark to the latest message timestamp when messages exist."""
    from signal_mcp.models import Message
    msg = Message(id="m1", sender="+1", body="hi", timestamp=datetime(2024, 6, 1, 12, 0, 0))
    stack, signal_dir, mock_store = _make_sync_patches(tmp_path, messages=[msg], last_sync="999")
    with stack:
        from signal_mcp.desktop import sync_from_desktop
        sync_from_desktop(signal_dir=signal_dir)

    mock_store.set_meta.assert_called_once()
    key, value = mock_store.set_meta.call_args[0]
    assert key == "desktop_last_sync"
    assert value.isdigit()  # epoch milliseconds


def test_sync_from_desktop_no_watermark_update_when_empty(tmp_path):
    """sync_from_desktop does NOT update the watermark when no messages are returned."""
    stack, signal_dir, mock_store = _make_sync_patches(tmp_path, messages=[], last_sync="999")
    with stack:
        from signal_mcp.desktop import sync_from_desktop
        sync_from_desktop(signal_dir=signal_dir)

    mock_store.set_meta.assert_not_called()


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
        from tests.conftest import call_tool as _ct
        result = await _ct("sync_desktop", {})

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
        from tests.conftest import call_tool as _ct
        result = await _ct("sync_desktop", {})

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
        sent_at INTEGER, received_at INTEGER, source TEXT, sourceUuid TEXT, hasAttachments INTEGER,
            readStatus INTEGER
    )""")
    # This row has both timestamps NULL — COALESCE → 0, which is not > 0, so normally filtered.
    # We use since_ms=-1 (negative) to let it through: COALESCE(NULL,NULL,0)=0 > -1 → True.
    conn.execute("INSERT INTO conversations VALUES ('c1', '+1', NULL)")
    conn.execute("INSERT INTO messages VALUES ('zero', 'c1', 'incoming', 'body', NULL, NULL, '+2', NULL, 0, 1)")
    conn.commit()
    conn.close()

    # since_ms=-1 allows the NULL row through the SQL filter
    msgs = _read_messages_from_plain_db(db_path, since_ms=-1)
    # The Python guard `if not ts_ms: continue` kicks in and skips the row
    assert msgs == []


# ── Stale plaintext file sweep ──────────────────────────────────────────────────

def test_sweep_removes_only_stale_files(tmp_path):
    """Files older than the threshold are deleted; fresh files are left alone."""
    import os as _os
    from signal_mcp.desktop import _sweep_stale_plaintext_files

    plaintext_dir = tmp_path / "plaintext-tmp"
    plaintext_dir.mkdir()
    old_file = plaintext_dir / "orphaned.db"
    old_file.write_bytes(b"leftover")
    fresh_file = plaintext_dir / "fresh.db"
    fresh_file.write_bytes(b"current")

    now = time.time()
    _os.utime(old_file, (now - 7200, now - 7200))  # 2 hours old
    _os.utime(fresh_file, (now, now))

    with patch("signal_mcp.desktop._PLAINTEXT_TMP_DIR", plaintext_dir):
        _sweep_stale_plaintext_files()

    assert not old_file.exists()
    assert fresh_file.exists()


def test_sweep_noop_when_dir_missing(tmp_path):
    from signal_mcp.desktop import _sweep_stale_plaintext_files

    with patch("signal_mcp.desktop._PLAINTEXT_TMP_DIR", tmp_path / "does-not-exist"):
        _sweep_stale_plaintext_files()  # must not raise


# ── SIGTERM/SIGINT cleanup handler ──────────────────────────────────────────────

def test_termination_handler_deletes_active_plain_db(tmp_path):
    """The registered handler deletes the in-flight plaintext file before
    resuming default signal behavior. True signal delivery isn't practical to
    test in pytest, so this calls the handler function directly and stubs
    os.kill to avoid actually terminating the test process."""
    import signal_mcp.desktop as _desktop

    plain_db = tmp_path / "plain.db"
    plain_db.write_bytes(b"data")
    _desktop._active_plain_db = plain_db

    with patch("signal_mcp.desktop.os.kill") as mock_kill, \
         patch("signal_mcp.desktop.signal.signal") as mock_signal:
        _desktop._handle_termination_signal(signal.SIGTERM, None)

    assert not plain_db.exists()
    mock_signal.assert_called_once_with(signal.SIGTERM, signal.SIG_DFL)
    mock_kill.assert_called_once()
    _desktop._active_plain_db = None


def test_termination_handler_noop_without_active_plain_db():
    import signal_mcp.desktop as _desktop

    _desktop._active_plain_db = None
    with patch("signal_mcp.desktop.os.kill"), patch("signal_mcp.desktop.signal.signal"):
        _desktop._handle_termination_signal(signal.SIGINT, None)  # must not raise


# ── Single-flight lock against concurrent Desktop imports ──────────────────────

@patch("signal_mcp.desktop.detect_account", return_value="+49111")
@patch("signal_mcp.desktop._get_keychain_password")
@patch("signal_mcp.desktop._decrypt_key")
@patch("signal_mcp.desktop._decrypt_db_to_temp")
@patch("signal_mcp.desktop._read_messages_from_plain_db")
@patch("signal_mcp.desktop._store")
def test_import_raises_when_lock_already_held(
    mock_store, mock_read, mock_decrypt_db, mock_decrypt_key, mock_keychain,
    mock_detect, tmp_path
):
    from signal_mcp import desktop as _d

    signal_dir = tmp_path / "Signal"
    (signal_dir / "sql").mkdir(parents=True)
    (signal_dir / "sql" / "db.sqlite").write_bytes(b"fake")
    config = {"encryptedKey": "76313000" + "00" * 16}
    (signal_dir / "config.json").write_text(json.dumps(config))
    original_db, original_cfg = _d.SIGNAL_DB, _d.SIGNAL_CONFIG
    _d.SIGNAL_DB = signal_dir / "sql" / "db.sqlite"
    _d.SIGNAL_CONFIG = signal_dir / "config.json"

    lock_file = tmp_path / "desktop-import.lock"
    lock_file.write_text(str(os.getpid()))  # a live process holding the lock

    try:
        with patch("signal_mcp.desktop.DESKTOP_IMPORT_LOCK_FILE", lock_file), \
             pytest.raises(DesktopImportError, match="already in progress"):
            import_from_desktop()
    finally:
        _d.SIGNAL_DB, _d.SIGNAL_CONFIG = original_db, original_cfg

    mock_decrypt_db.assert_not_called()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.mark.parametrize("content", [None, "not-a-pid", ""])
def test_import_lock_from_dead_process_is_taken_over(tmp_path, content):
    from signal_mcp import desktop as _d
    # A kill (launchd restart) skips the `finally`, leaving the lock behind; it must not block
    # every later import and force the watch service onto its fallback forever.
    lock_file = tmp_path / "desktop-import.lock"
    lock_file.write_text(str(_dead_pid()) if content is None else content)
    with patch("signal_mcp.desktop.DESKTOP_IMPORT_LOCK_FILE", lock_file):
        assert _d._acquire_import_lock() is True
        assert lock_file.read_text() == str(os.getpid())


def test_import_lock_from_live_process_is_respected(tmp_path):
    from signal_mcp import desktop as _d
    lock_file = tmp_path / "desktop-import.lock"
    lock_file.write_text(str(os.getpid()))
    with patch("signal_mcp.desktop.DESKTOP_IMPORT_LOCK_FILE", lock_file):
        assert _d._acquire_import_lock() is False


@patch("signal_mcp.desktop.detect_account", return_value="+49111")
@patch("signal_mcp.desktop._get_keychain_password")
@patch("signal_mcp.desktop._decrypt_key")
@patch("signal_mcp.desktop._decrypt_db_to_temp")
@patch("signal_mcp.desktop._read_messages_from_plain_db")
@patch("signal_mcp.desktop._store")
def test_import_releases_lock_after_success(
    mock_store, mock_read, mock_decrypt_db, mock_decrypt_key, mock_keychain,
    mock_detect, tmp_path
):
    from signal_mcp import desktop as _d
    from signal_mcp.models import Message

    signal_dir = tmp_path / "Signal"
    (signal_dir / "sql").mkdir(parents=True)
    (signal_dir / "sql" / "db.sqlite").write_bytes(b"fake")
    config = {"encryptedKey": "76313000" + "00" * 16}
    (signal_dir / "config.json").write_text(json.dumps(config))
    original_db, original_cfg = _d.SIGNAL_DB, _d.SIGNAL_CONFIG
    _d.SIGNAL_DB = signal_dir / "sql" / "db.sqlite"
    _d.SIGNAL_CONFIG = signal_dir / "config.json"

    mock_decrypt_key.return_value = "aabbccdd" * 4
    fake_plain = tmp_path / "plain.db"
    fake_plain.write_bytes(b"x")
    mock_decrypt_db.return_value = fake_plain
    mock_read.return_value = [Message(id="m1", sender="+1", body="hi", timestamp=datetime(2024, 1, 1))]
    mock_store.save_message.return_value = True
    mock_store.save_messages_batch.return_value = (1, 0)

    lock_file = tmp_path / "desktop-import.lock"
    try:
        with patch("signal_mcp.desktop.DESKTOP_IMPORT_LOCK_FILE", lock_file):
            import_from_desktop()
            assert not lock_file.exists()
    finally:
        _d.SIGNAL_DB, _d.SIGNAL_CONFIG = original_db, original_cfg


@patch("signal_mcp.desktop.detect_account", return_value="+49111")
@patch("signal_mcp.desktop._get_keychain_password")
@patch("signal_mcp.desktop._decrypt_key")
@patch("signal_mcp.desktop._decrypt_db_to_temp")
@patch("signal_mcp.desktop._store")
def test_import_releases_lock_after_failure(
    mock_store, mock_decrypt_db, mock_decrypt_key, mock_keychain, mock_detect, tmp_path
):
    from signal_mcp import desktop as _d

    signal_dir = tmp_path / "Signal"
    (signal_dir / "sql").mkdir(parents=True)
    (signal_dir / "sql" / "db.sqlite").write_bytes(b"fake")
    config = {"encryptedKey": "76313000" + "00" * 16}
    (signal_dir / "config.json").write_text(json.dumps(config))
    original_db, original_cfg = _d.SIGNAL_DB, _d.SIGNAL_CONFIG
    _d.SIGNAL_DB = signal_dir / "sql" / "db.sqlite"
    _d.SIGNAL_CONFIG = signal_dir / "config.json"

    mock_decrypt_key.return_value = "aabbccdd" * 4
    mock_decrypt_db.side_effect = DesktopImportError("sqlcipher failed: boom")

    lock_file = tmp_path / "desktop-import.lock"
    try:
        with patch("signal_mcp.desktop.DESKTOP_IMPORT_LOCK_FILE", lock_file):
            with pytest.raises(DesktopImportError, match="sqlcipher failed"):
                import_from_desktop()
            assert not lock_file.exists()
    finally:
        _d.SIGNAL_DB, _d.SIGNAL_CONFIG = original_db, original_cfg
