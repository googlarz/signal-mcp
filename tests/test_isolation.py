"""The autouse isolation fixture in conftest keeps the suite away from the live service."""

import subprocess
from pathlib import Path

import pytest

import signal_mcp.config as config


def test_real_signal_cli_cannot_be_started():
    with pytest.raises(FileNotFoundError):
        subprocess.Popen(["signal-cli", "-u", "+1", "daemon"])


def test_state_files_point_at_a_temp_dir():
    live = Path.home() / ".local" / "share" / "signal-mcp"
    assert live not in config.DAEMON_PID_FILE.parents
    assert live not in config.WEBHOOK_CONFIG_FILE.parents
