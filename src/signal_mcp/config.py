"""Configuration: auto-detect Signal account, daemon URL, attachment dir."""

import json
import re
import subprocess
from pathlib import Path

DAEMON_PORT = 7583
DAEMON_URL = f"http://localhost:{DAEMON_PORT}/api/v1/rpc"
ATTACHMENT_DIR = Path.home() / "Downloads" / "signal-attachments"
DAEMON_PID_FILE = Path.home() / ".local" / "share" / "signal-mcp" / "daemon.pid"

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
