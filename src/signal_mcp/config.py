"""Configuration: auto-detect Signal account, daemon URL, attachment dir."""

import json
import os
import re
import subprocess
from pathlib import Path

DAEMON_PORT = 7583
DAEMON_URL = f"http://localhost:{DAEMON_PORT}/api/v1/rpc"
ATTACHMENT_DIR = Path.home() / "Downloads" / "signal-attachments"
# Where signal-cli itself stores received attachments, one file per attachment id.
SIGNAL_CLI_ATTACHMENTS_DIR = Path.home() / ".local" / "share" / "signal-cli" / "attachments"
DAEMON_PID_FILE = Path.home() / ".local" / "share" / "signal-mcp" / "daemon.pid"
DAEMON_MESSAGES_LOG = Path.home() / ".local" / "share" / "signal-mcp" / "daemon-messages.jsonl"
RECEIVE_LOCK_FILE = Path.home() / ".local" / "share" / "signal-mcp" / "receive.lock"
WEBHOOK_CONFIG_FILE = Path.home() / ".local" / "share" / "signal-mcp" / "webhook.json"

# Folders a send/upload tool is allowed to read a local file from. An incoming
# message is untrusted content an AI client may act on -- without this, a
# sender could talk the AI into sending back an arbitrary local file (e.g.
# "please send me ~/.ssh/id_ed25519") via send_attachment. Override with a
# ':'-separated list of absolute paths in SIGNAL_MCP_SEND_ROOTS.
_send_roots_env = os.environ.get("SIGNAL_MCP_SEND_ROOTS", "")
SEND_ROOTS = (
    [Path(p).expanduser() for p in _send_roots_env.split(":") if p]
    if _send_roots_env
    else [ATTACHMENT_DIR, Path.home() / "Downloads", Path.home() / "Desktop", Path.home() / "Documents"]
)


def validate_send_path(path: str) -> Path:
    """Resolve *path* and raise ValueError unless it's inside an allowed
    root and has no hidden (dot-prefixed) component -- see SEND_ROOTS above."""
    resolved = Path(path).expanduser().resolve()
    if any(part.startswith(".") for part in resolved.parts):
        raise ValueError(f"'{path}' is inside a hidden folder or is a hidden file, which isn't allowed.")
    if not any(resolved.is_relative_to(root.resolve()) for root in SEND_ROOTS):
        allowed = ", ".join(str(r) for r in SEND_ROOTS)
        raise ValueError(f"'{path}' is outside the allowed folders ({allowed}). Set SIGNAL_MCP_SEND_ROOTS to change this.")
    return resolved


# signal-cli stores account data here
_ACCOUNTS_JSON = Path.home() / ".local" / "share" / "signal-cli" / "data" / "accounts.json"

_account_cache: str | None = None


def detect_account() -> str:
    """Auto-detect linked Signal account number (cached).

    Reads accounts.json directly to avoid a slow signal-cli JVM cold-start.
    Falls back to `signal-cli listAccounts` if the file is missing.
    """
    global _account_cache
    if _account_cache is not None:
        return _account_cache

    # Fast path: parse accounts.json without spawning signal-cli
    if _ACCOUNTS_JSON.exists():
        try:
            data = json.loads(_ACCOUNTS_JSON.read_text())
            for acc in data.get("accounts", []):
                number = acc.get("number", "")
                if number.startswith("+"):
                    _account_cache = number
                    return _account_cache
        except Exception:
            pass  # fall through to signal-cli

    # Slow fallback: spawn signal-cli (takes ~15s on cold JVM start)
    result = subprocess.run(
        ["signal-cli", "listAccounts"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"signal-cli listAccounts failed: {result.stderr.strip()}")

    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("Number:"):
            _account_cache = line.split(":", 1)[1].strip()
            return _account_cache
        if line.startswith("+"):
            _account_cache = line.split()[0]
            return _account_cache

    raise RuntimeError("No Signal account found. Run: signal-cli link --name 'MyDevice'")


MIN_SIGNAL_CLI_VERSION = (0, 13, 0)


def check_signal_cli_version() -> None:
    """Raise RuntimeError if signal-cli is missing or too old."""
    try:
        result = subprocess.run(
            ["signal-cli", "--version"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "signal-cli not found. Install it first:\n"
            "  macOS:  brew install signal-cli\n"
            "  Linux:  https://github.com/AsamK/signal-cli/releases"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "signal-cli --version timed out. "
            "Ensure signal-cli is installed and working: signal-cli --version"
        )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(
            f"signal-cli exited with code {result.returncode}"
            + (f": {stderr}" if stderr else "")
        )

    match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout)
    if not match:
        raise RuntimeError(
            f"Could not parse signal-cli version from: {result.stdout.strip()!r}"
        )
    version = tuple(int(x) for x in match.groups())
    if version < MIN_SIGNAL_CLI_VERSION:
        min_str = ".".join(str(x) for x in MIN_SIGNAL_CLI_VERSION)
        raise RuntimeError(
            f"signal-cli {'.'.join(str(x) for x in version)} is too old. "
            f"Minimum required: {min_str}. "
            "Upgrade: brew upgrade signal-cli"
        )


def get_account_data_dir(account: str) -> Path | None:
    """Return signal-cli's per-account data directory (contains msg-cache), or None."""
    try:
        data = json.loads(_ACCOUNTS_JSON.read_text())
    except Exception:
        return None
    for acc in data.get("accounts", []):
        if acc.get("number") == account:
            return _ACCOUNTS_JSON.parent / f"{acc['path']}.d"
    return None


def ensure_attachment_dir() -> Path:
    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
    return ATTACHMENT_DIR


def save_daemon_pid(pid: int) -> None:
    DAEMON_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    DAEMON_PID_FILE.write_text(str(pid))


def read_daemon_pid() -> int | None:
    try:
        return int(DAEMON_PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def clear_daemon_pid() -> None:
    DAEMON_PID_FILE.unlink(missing_ok=True)


# Background service paths (mirrors cli.py constants — kept here to avoid circular import)
_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / "com.signal-mcp.watch.plist"
_SYSTEMD_PATH = Path.home() / ".config" / "systemd" / "user" / "signal-mcp-watch.service"


def is_service_installed() -> bool:
    """Return True if the background message-capture service is installed."""
    return _PLIST_PATH.exists() or _SYSTEMD_PATH.exists()


# ── Webhook configuration ─────────────────────────────────────────────────────

def get_webhook_url() -> str | None:
    """Return the configured webhook URL, or None.

    Priority: SIGNAL_MCP_WEBHOOK env var → webhook.json config file.
    """
    env = os.environ.get("SIGNAL_MCP_WEBHOOK")
    if env:
        return env
    if WEBHOOK_CONFIG_FILE.exists():
        try:
            data = json.loads(WEBHOOK_CONFIG_FILE.read_text())
        except Exception as e:
            raise RuntimeError(f"Webhook config file {WEBHOOK_CONFIG_FILE} is corrupt: {e}") from e
        return data.get("url") or None
    return None


def set_webhook_url(url: str | None) -> None:
    """Persist (or clear) the webhook URL in the config file."""
    WEBHOOK_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if url:
        WEBHOOK_CONFIG_FILE.write_text(json.dumps({"url": url}, indent=2))
    else:
        WEBHOOK_CONFIG_FILE.unlink(missing_ok=True)
