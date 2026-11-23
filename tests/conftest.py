"""Shared test helpers."""
from pathlib import Path

import pytest
from mcp.types import CallToolRequestParams
from signal_mcp.server import call_tool as _call_tool, get_client, TOOLS  # re-export for tests


@pytest.fixture(autouse=True)
def widen_send_roots(monkeypatch, tmp_path_factory):
    """Tests across this suite use throwaway paths -- literal "/tmp/..." or
    pytest's own tmp_path -- as stand-ins for attachment/avatar/sticker-pack
    paths; none of them are actually read from disk by the code under test
    (the RPC call itself is always mocked), so widen the SIGNAL_MCP_SEND_ROOTS
    allowlist (see config.validate_send_path) to cover both for the whole
    suite rather than rewriting every test's fixture path."""
    import signal_mcp.config as _config_mod
    monkeypatch.setattr(_config_mod, "SEND_ROOTS", [
        *_config_mod.SEND_ROOTS, Path("/tmp"), tmp_path_factory.getbasetemp(),
    ])


@pytest.fixture(autouse=True)
def isolate_live_service_state(monkeypatch, tmp_path_factory):
    """The suite must never touch the developer's running signal-mcp.

    An unmocked daemon test once started a real signal-cli and its cleanup deleted the live
    ~/.local/share/signal-mcp/daemon.pid. Point every state file at a temp dir and refuse to
    start a real signal-cli; tests that need either patch it themselves, as before."""
    import asyncio
    import subprocess

    import signal_mcp.client as _client
    import signal_mcp.config as _config
    import signal_mcp.desktop as _desktop

    state = tmp_path_factory.mktemp("signal-mcp-state")
    monkeypatch.setattr(_config, "DAEMON_PID_FILE", state / "daemon.pid")
    monkeypatch.setattr(_config, "WEBHOOK_CONFIG_FILE", state / "webhook.json")
    monkeypatch.setattr(_client, "RECEIVE_LOCK_FILE", state / "receive.lock")
    monkeypatch.setattr(_desktop, "DESKTOP_IMPORT_LOCK_FILE", state / "desktop-import.lock")

    real_popen, real_exec = subprocess.Popen, asyncio.create_subprocess_exec

    def _no_real_signal_cli(real):
        def guarded(*args, **kwargs):
            first = args[0] if args else None
            argv = first if isinstance(first, (list, tuple)) else args
            if argv and str(argv[0]).endswith("signal-cli"):
                raise AssertionError("a test tried to run the real signal-cli; mock it")
            return real(*args, **kwargs)
        return guarded

    monkeypatch.setattr(subprocess, "Popen", _no_real_signal_cli(real_popen))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_real_signal_cli(real_exec))


async def call_tool(name: str, arguments: dict):
    """Thin shim so existing tests keep their (name, args) call signature."""
    return (await _call_tool(None, CallToolRequestParams(name=name, arguments=arguments))).content
