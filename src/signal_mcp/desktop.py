"""Signal Desktop database importer.

Reads ALL historical messages from Signal Desktop's local SQLCipher database
and imports them into the signal-mcp store.

Signal Desktop stores:
  macOS:   ~/Library/Application Support/Signal/
  Linux:   ~/.config/Signal/
  Windows: %APPDATA%/Signal/

  DB:  <dir>/sql/db.sqlite  (SQLCipher 4)
  Key: <dir>/config.json    (encryptedKey, Chromium v10 / v11)

The encryptedKey is AES-128-CBC encrypted with a password from the OS keychain:
  macOS:   Keychain service "Signal Safe Storage"
  Linux:   libsecret / GNOME Keyring ("Signal Safe Storage"), fallback "peanuts"
  Windows: DPAPI (not yet supported — use manual key)
"""

import json
import os
import platform
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import store as _store
from .config import detect_account
from .models import Attachment, Message


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
        # Signal Desktop always encrypts a v10 key on Linux with this hardcoded
        # password. v11 keys (keyring-backed) never reach this function --
        # _get_db_key_hex routes those to _require_linux_keyring_password()
        # instead, since trying the keyring here could pick up an unrelated
        # app's entry under the same label and derive the wrong AES key.
        return b"peanuts"

    elif system == "Windows":
        # On Windows, Electron's safeStorage uses DPAPI to encrypt the key directly.
        # We call CryptUnprotectData via ctypes to decrypt — no PBKDF2 involved.
        # The decrypted bytes ARE the raw DB key (32 bytes → 64 hex chars).
        raise DesktopImportError(
            "Windows: use _decrypt_dpapi_key() instead of _get_keychain_password() + _decrypt_key()"
        )

    else:
        raise DesktopImportError(
            f"Platform '{system}' is not supported for automatic keychain access."
        )


def _linux_keyring_password() -> bytes | None:
    """Look up the Signal Safe Storage password via secret-tool (GNOME Keyring / libsecret).

    Returns None when secret-tool is not installed or has no matching entry.
    """
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
    return None


def _require_linux_keyring_password() -> bytes:
    """Return the keyring password, or explain what is missing.

    Used for v11 keys, which are always encrypted with a keyring password — the
    hardcoded "peanuts" fallback can never decrypt them.
    """
    try:
        password = _linux_keyring_password()
    except subprocess.TimeoutExpired as exc:
        raise DesktopImportError(
            "Signal Desktop's key is protected by your keyring, but secret-tool "
            "did not respond in time.\n"
            "It may be waiting on an unlock prompt — check for one and try again."
        ) from exc
    if password is not None:
        return password
    if shutil.which("secret-tool") is None:
        raise DesktopImportError(
            "Signal Desktop's key is protected by your keyring, but secret-tool is not installed.\n"
            "Install it (Debian/Ubuntu: sudo apt install libsecret-tools) and try again."
        )
    raise DesktopImportError(
        "Signal Desktop's key is protected by your keyring, but secret-tool found no Signal entry.\n"
        "Make sure the keyring is unlocked and that you are the user who runs Signal Desktop.\n"
        "Only libsecret-backed keyrings (GNOME Keyring) are supported — KWallet-only setups "
        "will hit this error even when unlocked."
    )


def _decrypt_dpapi_key(encrypted_hex: str) -> str:
    """Windows only: DPAPI-decrypt Signal Desktop's encryptedKey → raw DB key hex."""
    try:
        import ctypes
        import ctypes.wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        raw = bytes.fromhex(encrypted_hex)
        # Strip known Electron prefixes (v10, v11) if present
        for prefix in (b"v10", b"v11"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
                break

        p = ctypes.create_string_buffer(raw, len(raw))
        blobin = DATA_BLOB(len(raw), p)
        blobout = DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blobin), None, None, None, None, 0, ctypes.byref(blobout)
        )
        if not ok:
            raise DesktopImportError(
                "DPAPI decryption failed — run as the same Windows user that installed Signal Desktop"
            )
        result = ctypes.string_at(blobout.pbData, blobout.cbData)
        ctypes.windll.kernel32.LocalFree(blobout.pbData)
        return result.hex()
    except DesktopImportError:
        raise
    except Exception as exc:
        raise DesktopImportError(f"Windows keychain error: {exc}") from exc


def _get_db_key_hex(encrypted_hex: str) -> str:
    """Return the raw SQLCipher DB key hex, handling all platforms."""
    if platform.system() == "Windows":
        return _decrypt_dpapi_key(encrypted_hex)
    if platform.system() == "Linux" and encrypted_hex[:6].lower() == b"v11".hex():
        password = _require_linux_keyring_password()
    else:
        password = _get_keychain_password()
    return _decrypt_key(encrypted_hex, password)


def _decrypt_key(encrypted_hex: str, password: bytes) -> str:
    """Decrypt Signal Desktop's encryptedKey (Chromium v10/v11 AES-CBC format)."""
    raw = bytes.fromhex(encrypted_hex)
    # v10: macOS Keychain, or the hardcoded "peanuts" password on Linux.
    # v11: Linux only — password comes from a libsecret keyring (secret-tool).
    # KWallet-only setups (no libsecret) aren't supported: secret-tool finds
    # nothing and _require_linux_keyring_password's error names the wrong cause.
    if raw[:3] not in (b"v10", b"v11"):
        raise DesktopImportError(f"Unknown encryptedKey format (prefix={raw[:3]!r})")

    ciphertext = raw[3:]

    # Chromium key derivation: PBKDF2-SHA1, salt="saltysalt", 16 bytes.
    # 1003 iterations on macOS, but only 1 on Linux (os_crypt_linux.cc).
    iterations = 1003 if platform.system() == "Darwin" else 1
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),  # noqa: S303  (Chromium's choice, not ours)
        length=16,
        salt=b"saltysalt",
        iterations=iterations,
    )
    aes_key = kdf.derive(password)

    # Decrypt AES-128-CBC, IV = 0x20 * 16 (space character)
    iv = b"\x20" * 16
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    try:
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        # Remove PKCS7 padding
        unpadder = padding.PKCS7(128).unpadder()
        db_key_bytes = unpadder.update(plaintext) + unpadder.finalize()
    except ValueError as exc:
        # Wrong password derives the wrong AES key, which almost always fails
        # PKCS7 unpadding; a truncated/corrupt ciphertext fails block-aligned
        # AES-CBC decryption the same way. Either way this is not a bug in the
        # server -- it's bad input, so it gets DesktopImportError like every
        # other "this import cannot proceed" case rather than a raw ValueError.
        raise DesktopImportError(
            "Could not decrypt Signal Desktop's key — the password is wrong "
            "or the encryptedKey is corrupted."
        ) from exc

    # Signal Desktop stores the SQLCipher key as a hex-encoded ASCII string
    # (e.g. b'3a0aaac0...'), not raw binary bytes.  Decode it directly instead
    # of calling .hex() which would double-encode and produce the wrong key.
    try:
        candidate = db_key_bytes.decode("ascii")
        if len(candidate) in (64, 128) and all(c in "0123456789abcdefABCDEF" for c in candidate):
            return candidate.lower()
    except (UnicodeDecodeError, ValueError):
        pass
    # Fallback for platforms that do store raw bytes: hex-encode them
    return db_key_bytes.hex()


_PLAINTEXT_TMP_DIR = Path.home() / ".local" / "share" / "signal-mcp" / "tmp"
_STALE_PLAINTEXT_MAX_AGE_SECONDS = 60 * 60

DESKTOP_IMPORT_LOCK_FILE = Path.home() / ".local" / "share" / "signal-mcp" / "desktop-import.lock"


def _sweep_stale_plaintext_files() -> None:
    """Delete plaintext DB copies left behind by a process that was SIGKILLed.

    No signal handler can intercept SIGKILL, so a hard-killed import leaves its
    plaintext file on disk forever unless something else cleans it up. This runs
    at the start of every import and removes anything older than the threshold.
    """
    if not _PLAINTEXT_TMP_DIR.exists():
        return
    cutoff = time.time() - _STALE_PLAINTEXT_MAX_AGE_SECONDS
    for entry in _PLAINTEXT_TMP_DIR.iterdir():
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
        except OSError:
            pass


def _decrypt_db_to_temp(db_key_hex: str, db_path: Path | None = None) -> Path:
    """Use sqlcipher CLI to export the encrypted DB to a plain SQLite file.

    The export is a full plaintext copy of the user's Signal message history,
    so it goes in a private (0700), app-owned directory rather than the shared
    system temp dir, and is unlinked on every failure path — a leaked copy
    left behind on a decrypt failure would otherwise sit there indefinitely.
    """
    sqlcipher = _find_sqlcipher()
    source = db_path or SIGNAL_DB
    _PLAINTEXT_TMP_DIR.mkdir(parents=True, exist_ok=True)
    _PLAINTEXT_TMP_DIR.chmod(0o700)
    _sweep_stale_plaintext_files()
    fd, tmp_str = tempfile.mkstemp(suffix=".db", dir=str(_PLAINTEXT_TMP_DIR))
    os.close(fd)
    tmp = Path(tmp_str)

    script = (
        f"PRAGMA key = \"x'{db_key_hex}'\";\n"
        f"PRAGMA cipher_page_size = 4096;\n"
        f"PRAGMA kdf_iter = 1;\n"
        f"PRAGMA cipher_hmac_algorithm = HMAC_SHA512;\n"
        f"PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;\n"
        f"ATTACH DATABASE '{tmp}' AS plaintext KEY '';\n"
        f"SELECT sqlcipher_export('plaintext');\n"
        f"DETACH DATABASE plaintext;\n"
        f".quit\n"
    )

    try:
        result = subprocess.run(
            [sqlcipher, str(source)],
            input=script, capture_output=True, text=True, timeout=60,
        )
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise DesktopImportError(f"sqlcipher failed: {result.stderr.strip()}")
    if not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise DesktopImportError("sqlcipher produced empty output — wrong key?")

    return tmp


def _find_sqlcipher() -> str:
    for path in ["/opt/homebrew/bin/sqlcipher", "/usr/local/bin/sqlcipher"]:
        if Path(path).exists():
            return path
    result = subprocess.run(["which", "sqlcipher"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    system = platform.system()
    if system == "Darwin":
        hint = "Install it: brew install sqlcipher"
    elif system == "Linux":
        hint = "Install it: sudo apt install sqlcipher (Debian/Ubuntu) or your distribution's sqlcipher package"
    else:
        hint = "Install the sqlcipher command-line tool and put it on your PATH"
    raise DesktopImportError(f"sqlcipher not found. {hint}")


def _read_messages_from_plain_db(plain_db: Path, own_number: str = "", since_ms: int = 0) -> list[Message]:
    """Parse Signal Desktop's messages table into our Message model.

    since_ms: if > 0, only return messages with sent_at or received_at > since_ms.
    """
    conn = sqlite3.connect(str(plain_db))
    conn.row_factory = sqlite3.Row
    messages = []

    try:
        # Signal Desktop renamed sourceUuid → sourceServiceId in newer versions.
        # Detect whichever column is present and alias it to a stable name.
        msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "sourceServiceId" in msg_cols:
            source_col = "m.sourceServiceId AS sourceUuid"
        elif "sourceUuid" in msg_cols:
            source_col = "m.sourceUuid AS sourceUuid"
        else:
            source_col = "NULL AS sourceUuid"

        # readStatus column present in Signal Desktop schema v39+
        # (renamed from "unread"). 0=read, 1=unread, NULL=unknown.
        if "readStatus" in msg_cols:
            read_col = "m.readStatus"
        else:
            read_col = "NULL AS readStatus"

        # A contact with no phone number is still one conversation, and older
        # Signal Desktop schemas have no serviceId column at all — detected
        # rather than assumed, same as sourceServiceId/readStatus above.
        conv_cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()}
        conv_service_col = (
            "c.serviceId AS conv_service_id" if "serviceId" in conv_cols
            else "NULL AS conv_service_id"
        )

        # A reply's quote lives in the message's json blob; older or synthetic
        # databases may not have the column at all.
        json_col = "m.json AS msg_json" if "json" in msg_cols else "NULL AS msg_json"

        rows = conn.execute(
            f"""SELECT
                m.id,
                m.conversationId,
                m.type,
                m.body,
                m.sent_at,
                m.received_at,
                m.source,
                {source_col},
                m.hasAttachments,
                {json_col},
                {read_col},
                c.e164    AS conv_e164,
                {conv_service_col},
                c.groupId AS conv_group_id
            FROM messages m
            LEFT JOIN conversations c ON c.id = m.conversationId
            WHERE m.type IN ('incoming', 'outgoing')
              AND (m.body IS NOT NULL OR m.hasAttachments = 1)
              AND COALESCE(m.sent_at, m.received_at, 0) > ?
            ORDER BY m.sent_at ASC""",
            (since_ms,),
        ).fetchall()

        for row in rows:
            ts_ms = row["sent_at"] or row["received_at"] or 0
            if not ts_ms:
                continue

            is_group = bool(row["conv_group_id"])
            # Both halves of a direct conversation must be keyed by the SAME
            # identifier, since store.get_conversation matches "sender = ? OR
            # recipient = ?" against a single value. Signal Desktop leaves
            # `source` NULL on incoming messages and fills sourceServiceId, so
            # keying incoming by that uuid while keying outgoing by the
            # conversation's phone number split one conversation into two —
            # a read by either identifier returned only half the messages.
            # Use the conversation's own e164 (falling back to its serviceId
            # for a contact who shares no phone number) for both directions.
            dm_identity = None if is_group else (row["conv_e164"] or row["conv_service_id"])
            if row["type"] == "outgoing":
                sender = own_number or "me"
                recipient = dm_identity
            else:
                recipient = None
                sender = dm_identity or row["source"] or row["sourceUuid"] or ""

            # Signal Desktop: readStatus=0 means read, 1=unread, NULL=unknown.
            # Default unknown/old messages to read (safer than false unread counts)
            read_status = row["readStatus"]
            is_read = read_status == 0 if read_status is not None else True

            messages.append(Message(
                # Same id scheme as live-received messages (str(ts_ms)) so INSERT OR
                # IGNORE in store.save_message() actually dedups a message that was
                # captured live and later re-seen via desktop import/sync.
                id=str(ts_ms),
                sender=sender,
                recipient=recipient,
                body=row["body"] or "",
                timestamp=datetime.fromtimestamp(ts_ms / 1000),
                group_id=_decode_group_id(row["conv_group_id"]),
                is_read=is_read,
                quote_id=_quote_id(row["msg_json"]),
            ))
    finally:
        conn.close()

    return messages


def _read_conversation_names(plain_db: Path) -> list[tuple[str, str, str]]:
    """Read (id, name, type) tuples from Signal Desktop's conversations table."""
    conn = sqlite3.connect(str(plain_db))
    conn.row_factory = sqlite3.Row
    try:
        conv_cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()}
        # profileName / profileFullName for DMs, name for groups
        name_expr_parts = []
        if "name" in conv_cols:
            name_expr_parts.append("c.name")
        if "profileFullName" in conv_cols:
            name_expr_parts.append("c.profileFullName")
        if "profileName" in conv_cols:
            name_expr_parts.append("c.profileName")
        if "e164" in conv_cols:
            name_expr_parts.append("c.e164")
        if not name_expr_parts:
            return []
        name_expr = f"COALESCE({', '.join(f'NULLIF({p}, \"\")' for p in name_expr_parts)})"
        # A contact with no phone number stored is still someone worth naming —
        # older schemas have no serviceId column at all, so detect rather than assume.
        service_col = "c.serviceId" if "serviceId" in conv_cols else "NULL"

        rows = conn.execute(
            f"""SELECT
                c.id,
                c.type,
                c.groupId,
                c.e164,
                {service_col} AS service_id,
                {name_expr} AS display_name
            FROM conversations c
            WHERE display_name IS NOT NULL AND display_name != ''"""
        ).fetchall()

        result = []
        for row in rows:
            conv_type = row["type"] or "private"
            group_id = _decode_group_id(row["groupId"])
            if conv_type == "group" and group_id:
                result.append((group_id, row["display_name"], "group"))
            elif row["e164"]:
                result.append((row["e164"], row["display_name"], "direct"))
            elif row["service_id"]:
                result.append((row["service_id"], row["display_name"], "direct"))
        return result
    finally:
        conn.close()


def _decode_group_id(raw: str | None) -> str | None:
    """Signal Desktop stores group IDs as base64; convert to the format signal-cli uses."""
    if not raw:
        return None
    # Strip any Blob prefix Signal Desktop adds — the id itself is still valid,
    # so keep it rather than discarding the whole value.
    if raw.startswith("blob:"):
        raw = raw[len("blob:"):]
    return raw or None


def _quote_id(msg_json: str | None) -> str | None:
    """The sent_at of the message this one replies to, as a string, or None.

    Signal Desktop keeps a reply's quote inside the message's json blob as
    {"quote": {"id": <sent_at of the quoted message>, ...}}. `id` is the quoted
    message's own sent_at, matching the str(sent_at) id scheme used elsewhere
    in this module, so it joins back to another imported message's own id.
    """
    if not msg_json:
        return None
    try:
        quote = json.loads(msg_json).get("quote")
    except (ValueError, AttributeError):
        return None
    if not isinstance(quote, dict):
        return None
    qid = quote.get("id")
    if isinstance(qid, bool) or not isinstance(qid, (int, str)):
        return None
    qid = str(qid).strip()
    return qid if qid.isdigit() else None


_active_plain_db: Path | None = None


def _handle_termination_signal(signum, frame) -> None:
    """Delete the in-flight plaintext DB copy, then resume default signal behavior.

    Registered only for the window during which import_from_desktop has a
    plaintext file on disk. SIGKILL can't be caught (that's what the stale-file
    sweep is for) — this covers SIGTERM (e.g. launchd stopping the process) and
    SIGINT (Ctrl+C).
    """
    if _active_plain_db is not None:
        _active_plain_db.unlink(missing_ok=True)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def import_from_desktop(progress_cb=None, signal_dir: Path | None = None, since_ms: int = 0) -> dict:
    """
    Full import pipeline: decrypt DB → parse → store.
    Returns {"imported": N, "skipped": N, "total": N, "platform": str, "source": str}.

    signal_dir: override the auto-detected Signal Desktop directory.
    since_ms: if > 0, only import messages newer than this epoch-millisecond timestamp.
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

    if not _acquire_import_lock():
        raise DesktopImportError(
            "A Signal Desktop import is already in progress "
            f"(lock file: {DESKTOP_IMPORT_LOCK_FILE})"
        )

    try:
        return _run_desktop_import(db_path, config_path, progress_cb, since_ms)
    finally:
        DESKTOP_IMPORT_LOCK_FILE.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire_import_lock() -> bool:
    """Take the import lock; False if a live process holds it.

    A kill (launchd restarting the watch service) skips the `finally` that removes the lock, so
    a lock whose process is gone, or whose content is not a pid, is taken over instead of
    blocking every later import.
    """
    DESKTOP_IMPORT_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if DESKTOP_IMPORT_LOCK_FILE.exists():
        try:
            holder = int(DESKTOP_IMPORT_LOCK_FILE.read_text().strip())
        except (ValueError, OSError):
            holder = 0
        if holder > 0 and _pid_alive(holder):
            return False
    DESKTOP_IMPORT_LOCK_FILE.write_text(str(os.getpid()))
    return True


def _run_desktop_import(db_path: Path, config_path: Path, progress_cb, since_ms: int) -> dict:
    global _active_plain_db

    # 1. Read encrypted key from config
    config = json.loads(config_path.read_text())
    encrypted_key_hex = config.get("encryptedKey")
    if not encrypted_key_hex:
        raise DesktopImportError("No encryptedKey in Signal Desktop config.json")

    if progress_cb:
        system = platform.system()
        if system == "Darwin":
            progress_cb("Unlocking macOS Keychain…")
        elif system == "Linux":
            progress_cb("Unlocking Linux keychain (GNOME Keyring / libsecret)…")
        else:
            progress_cb("Decrypting Signal Desktop key…")

    # 2. Derive the raw DB key (platform-specific)
    db_key_hex = _get_db_key_hex(encrypted_key_hex)

    if progress_cb:
        progress_cb("Decrypting Signal Desktop database…")

    # 3. Export encrypted DB to plain SQLite temp file
    plain_db = None
    is_main_thread = threading.current_thread() is threading.main_thread()
    prev_sigterm = prev_sigint = None
    try:
        plain_db = _decrypt_db_to_temp(db_key_hex, db_path)
        _active_plain_db = plain_db
        if is_main_thread:
            prev_sigterm = signal.signal(signal.SIGTERM, _handle_termination_signal)
            prev_sigint = signal.signal(signal.SIGINT, _handle_termination_signal)

        if progress_cb:
            progress_cb("Importing messages…")

        # 4. Parse messages — resolve own number for outgoing sender attribution.
        # A silent fallback to "" here would make every outgoing message's sender
        # fall back to the literal string "me" (see _read_messages_from_plain_db),
        # permanently misattributing every message the user ever sent.
        try:
            own_number = detect_account()
        except Exception as e:
            raise DesktopImportError(f"Could not detect your Signal account: {e}") from e
        messages = _read_messages_from_plain_db(plain_db, own_number=own_number, since_ms=since_ms)
        total = len(messages)
        imported = 0
        skipped = 0
        max_ts_ms = 0

        # Batch commits (one per 500 messages) instead of one commit per message —
        # a multi-thousand-message history otherwise dominates runtime in fsync overhead.
        _IMPORT_CHUNK = 500
        for start in range(0, total, _IMPORT_CHUNK):
            chunk = messages[start:start + _IMPORT_CHUNK]
            n_imported, n_skipped = _store.save_messages_batch(chunk)
            imported += n_imported
            skipped += n_skipped
            for msg in chunk:
                ts = int(msg.timestamp.timestamp() * 1000)
                if ts > max_ts_ms:
                    max_ts_ms = ts
            if progress_cb:
                progress_cb(f"  {start + len(chunk)}/{total} messages…")

        # 5. Extract and store conversation names (groups + contacts)
        try:
            conv_names = _read_conversation_names(plain_db)
            for conv_id, name, conv_type in conv_names:
                _store.save_conversation(conv_id, name, conv_type)
        except Exception:
            pass  # non-fatal — messages still imported

        return {
            "imported": imported,
            "skipped": skipped,
            "total": total,
            "max_ts_ms": max_ts_ms,
            "platform": platform.system(),
            "source": str(db_path.parent.parent),
        }
    finally:
        if is_main_thread and prev_sigterm is not None:
            signal.signal(signal.SIGTERM, prev_sigterm)
            signal.signal(signal.SIGINT, prev_sigint)
        _active_plain_db = None
        if plain_db is not None:
            plain_db.unlink(missing_ok=True)


def sync_from_desktop(progress_cb=None, signal_dir: Path | None = None) -> dict:
    """
    Incremental sync from Signal Desktop: imports only messages newer than the last sync.

    On the first call (no prior sync recorded) it imports everything — equivalent to
    import_from_desktop.  Subsequent calls are fast because they skip already-seen messages.
    Deduplication is handled atomically by INSERT OR IGNORE in the store, so messages
    already imported by import_from_desktop are silently skipped.

    Returns the import_from_desktop result dict plus:
      "since":       ISO datetime of the lower-bound filter (None on first run)
      "incremental": True if this was a delta sync, False if it was the first run
    """
    last_sync = _store.get_meta("desktop_last_sync")
    # Subtract a 60-second overlap so messages right at the boundary are never missed;
    # INSERT OR IGNORE deduplicates anything we've already stored.
    since_ms = max(0, int(last_sync) - 60_000) if last_sync else 0

    result = import_from_desktop(progress_cb=progress_cb, signal_dir=signal_dir, since_ms=since_ms)

    # Advance the watermark to the latest message timestamp seen (not wall-clock now).
    # Using wall-clock time caused messages to be lost whenever the Mac clock jumped
    # forward (sleep/wake, NTP, DST) — the watermark would leap past sent_at values.
    max_ts_ms = result.get("max_ts_ms", 0)
    if max_ts_ms > 0:
        new_mark = str(max_ts_ms)
        _store.set_meta("desktop_last_sync", new_mark)
    # If no messages were seen at all, leave the watermark unchanged so the next
    # sync re-scans from the same position rather than skipping forward.

    result["since"] = datetime.fromtimestamp(since_ms / 1000).isoformat() if since_ms else None
    result["incremental"] = last_sync is not None
    return result
