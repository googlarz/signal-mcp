"""Configuration: auto-detect Signal account, daemon URL, attachment dir."""

import subprocess
from pathlib import Path

DAEMON_PORT = 7583
DAEMON_URL = f"http://localhost:{DAEMON_PORT}/api/v1/rpc"
ATTACHMENT_DIR = Path.home() / "Downloads" / "signal-attachments"
DAEMON_PID_FILE = Path.home() / ".local" / "share" / "signal-mcp" / "daemon.pid"


_account_cache: str | None = None


def detect_account() -> str:
    """Auto-detect linked Signal account number from signal-cli (result is cached)."""
    global _account_cache
    if _account_cache is not None:
        return _account_cache

    result = subprocess.run(
        ["signal-cli", "listAccounts"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"signal-cli listAccounts failed: {result.stderr.strip()}")

    for line in result.stdout.splitlines():
        line = line.strip()
        # signal-cli output is either "Number: +E164" or bare "+E164"
        if line.startswith("Number:"):
            _account_cache = line.split(":", 1)[1].strip()
            return _account_cache
        if line.startswith("+"):
            _account_cache = line.split()[0]
            return _account_cache

    raise RuntimeError("No Signal account found. Run: signal-cli link --name 'MyDevice'")


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
