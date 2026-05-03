"""Signal Desktop database importer.

Reads ALL historical messages from Signal Desktop's local SQLCipher database
and imports them into the signal-mcp store.

Signal Desktop stores:
  macOS:   ~/Library/Application Support/Signal/
  Linux:   ~/.config/Signal/
  Windows: %APPDATA%/Signal/

  DB:  <dir>/sql/db.sqlite  (SQLCipher 4)
  Key: <dir>/config.json    (encryptedKey, Chromium v10)

The encryptedKey is AES-128-CBC encrypted with a password from the OS keychain:
  macOS:   Keychain service "Signal Safe Storage"
  Linux:   libsecret / GNOME Keyring ("Signal Safe Storage"), fallback "peanuts"
  Windows: DPAPI (not yet supported — use manual key)
"""

import json
import os
import platform
import subprocess
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes, padding

from .models import Attachment, Message
from .config import detect_account
from . import store as _store


def _signal_dir() -> Path:
    """Return the Signal Desktop data directory for the current platform."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Signal"
    elif system == "Linux":
        return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Signal"
    elif system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "Signal"
    # Fallback
    return Path.home() / ".config" / "Signal"


SIGNAL_DIR = _signal_dir()
SIGNAL_DB = SIGNAL_DIR / "sql" / "db.sqlite"
SIGNAL_CONFIG = SIGNAL_DIR / "config.json"


class DesktopImportError(Exception):
    pass


def _get_keychain_password() -> bytes:
    """Retrieve the Signal Safe Storage password from the OS keychain."""
    system = platform.system()

    if system == "Darwin":
        for service in ("Signal Safe Storage", "Signal Keys", "Electron Keys"):
            result = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().encode()
        raise DesktopImportError(
            "Could not find Signal password in macOS Keychain.\n"
            "macOS may have shown an access dialog — click 'Allow' and try again."
        )

    elif system == "Linux":
        # Try secret-tool (GNOME Keyring / libsecret)
        for label in ("Signal Safe Storage", "Electron Safe Storage"):
            try:
                result = subprocess.run(
                    ["secret-tool", "lookup", "application", "Signal"],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip().encode()
            except FileNotFoundError:
                pass
            try:
                result = subprocess.run(
                    ["secret-tool", "lookup", "label", label],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip().encode()
            except FileNotFoundError:
                break
        # Signal Desktop on Linux falls back to hardcoded password when no keyring is available
        return b"peanuts"

    else:
        raise DesktopImportError(
            f"Platform '{system}' is not supported for automatic keychain access.\n"
            "Use signal-mcp import-desktop --key <hex-key> to provide the DB key manually."
        )


def _decrypt_key(encrypted_hex: str, password: bytes) -> str:
    """Decrypt Signal Desktop's encryptedKey (Chromium v10 AES-CBC format)."""
    raw = bytes.fromhex(encrypted_hex)
    if not raw.startswith(b"v10"):
        raise DesktopImportError(f"Unknown encryptedKey format (prefix={raw[:3]!r})")

    ciphertext = raw[3:]

    # Chromium key derivation: PBKDF2-SHA1, salt="saltysalt", 1003 iterations, 16 bytes
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),  # noqa: S303  (Chromium's choice, not ours)
        length=16,
        salt=b"saltysalt",
        iterations=1003,
    )
    aes_key = kdf.derive(password)

    # Decrypt AES-128-CBC, IV = 0x20 * 16 (space character)
    iv = b"\x20" * 16
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()

    # Remove PKCS7 padding
    unpadder = padding.PKCS7(128).unpadder()
    db_key_bytes = unpadder.update(plaintext) + unpadder.finalize()

    return db_key_bytes.hex()


def _decrypt_db_to_temp(db_key_hex: str, db_path: Path | None = None) -> Path:
    """Use sqlcipher CLI to export the encrypted DB to a plain SQLite file."""
    sqlcipher = _find_sqlcipher()
    source = db_path or SIGNAL_DB
    fd, tmp_str = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tmp = Path(tmp_str)

    script = (
        f"PRAGMA key = \"x'{db_key_hex}'\";\n"
        f"PRAGMA cipher_page_size = 4096;\n"
        f"PRAGMA kdf_iter = 1;\n"
        f"PRAGMA cipher_hmac_algorithm = HMAC_SHA1;\n"
        f"PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA1;\n"
        f"ATTACH DATABASE '{tmp}' AS plaintext KEY '';\n"
        f"SELECT sqlcipher_export('plaintext');\n"
        f"DETACH DATABASE plaintext;\n"
        f".quit\n"
    )

    result = subprocess.run(
        [sqlcipher, str(source)],
        input=script, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise DesktopImportError(f"sqlcipher failed: {result.stderr.strip()}")
    if not tmp.exists() or tmp.stat().st_size == 0:
        raise DesktopImportError("sqlcipher produced empty output — wrong key?")

    return tmp


def _find_sqlcipher() -> str:
    for path in ["/opt/homebrew/bin/sqlcipher", "/usr/local/bin/sqlcipher"]:
        if Path(path).exists():
            return path
    result = subprocess.run(["which", "sqlcipher"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    raise DesktopImportError(
        "sqlcipher not found. Install it: brew install sqlcipher"
    )


def _read_messages_from_plain_db(plain_db: Path, own_number: str = "") -> list[Message]:
    """Parse Signal Desktop's messages table into our Message model."""
    conn = sqlite3.connect(str(plain_db))
    conn.row_factory = sqlite3.Row
    messages = []

    try:
        rows = conn.execute(
            """SELECT
                m.id,
                m.conversationId,
                m.type,
                m.body,
                m.sent_at,
                m.received_at,
                m.source,
                m.sourceUuid,
                m.hasAttachments,
                c.e164    AS conv_e164,
                c.groupId AS conv_group_id
            FROM messages m
            LEFT JOIN conversations c ON c.id = m.conversationId
            WHERE m.type IN ('incoming', 'outgoing')
              AND (m.body IS NOT NULL OR m.hasAttachments = 1)
            ORDER BY m.sent_at ASC"""
        ).fetchall()

        for row in rows:
            ts_ms = row["sent_at"] or row["received_at"] or 0
            if not ts_ms:
                continue

            # Outgoing: source is NULL in Signal Desktop — use own account number
            if row["type"] == "outgoing":
                sender = own_number or "me"
            else:
                sender = row["source"] or row["sourceUuid"] or row["conv_e164"] or ""

            messages.append(Message(
                id=f"desktop_{row['id']}",
                sender=sender,
                body=row["body"] or "",
                timestamp=datetime.fromtimestamp(ts_ms / 1000),
                group_id=_decode_group_id(row["conv_group_id"]),
            ))
    finally:
        conn.close()

    return messages


def _decode_group_id(raw: str | None) -> str | None:
    """Signal Desktop stores group IDs as base64; convert to the format signal-cli uses."""
    if not raw:
        return None
    # Strip any Blob prefix Signal Desktop adds
    if raw.startswith("blob:") or len(raw) > 100:
        return None
    return raw


def import_from_desktop(progress_cb=None, signal_dir: Path | None = None) -> dict:
    """
    Full import pipeline: decrypt DB → parse → store.
    Returns {"imported": N, "skipped": N, "total": N, "platform": str, "source": str}.

    signal_dir: override the auto-detected Signal Desktop directory.
    """
    if signal_dir is not None:
        # Explicit override — construct paths from it
        db_path = signal_dir / "sql" / "db.sqlite"
        config_path = signal_dir / "config.json"
    else:
        # Use module-level constants (patchable in tests, reflect current platform)
        db_path = SIGNAL_DB
        config_path = SIGNAL_CONFIG

    if not db_path.exists():
        raise DesktopImportError(
            f"Signal Desktop DB not found at {db_path}\n"
            f"Platform: {platform.system()}  Expected dir: {db_path.parent.parent}\n"
            "Make sure Signal Desktop is installed and has been opened at least once."
        )
    if not config_path.exists():
        raise DesktopImportError(f"Signal Desktop config not found at {config_path}")

    # 1. Read encrypted key from config
    config = json.loads(config_path.read_text())
    encrypted_key_hex = config.get("encryptedKey")
    if not encrypted_key_hex:
        raise DesktopImportError("No encryptedKey in Signal Desktop config.json")

    if progress_cb:
        progress_cb("Unlocking macOS Keychain…")

    # 2. Get keychain password and decrypt the DB key
    password = _get_keychain_password()
    db_key_hex = _decrypt_key(encrypted_key_hex, password)

    if progress_cb:
        progress_cb("Decrypting Signal Desktop database…")

    # 3. Export encrypted DB to plain SQLite temp file
    plain_db = None
    try:
        plain_db = _decrypt_db_to_temp(db_key_hex, db_path)

        if progress_cb:
            progress_cb("Importing messages…")

        # 4. Parse messages — resolve own number for outgoing sender attribution
        try:
            own_number = detect_account()
        except Exception:
            own_number = ""
        messages = _read_messages_from_plain_db(plain_db, own_number=own_number)
        total = len(messages)
        imported = 0
        skipped = 0

        for i, msg in enumerate(messages):
            if _store.save_message(msg):
                imported += 1
            else:
                skipped += 1
            if progress_cb and i % 500 == 0:
                progress_cb(f"  {i}/{total} messages…")

        return {
            "imported": imported,
            "skipped": skipped,
            "total": total,
            "platform": platform.system(),
            "source": str(db_path.parent.parent),
        }
    finally:
        if plain_db is not None:
            plain_db.unlink(missing_ok=True)
