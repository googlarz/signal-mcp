"""CLI entrypoint for signal-mcp."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import click

from . import __version__, store as _store
from .client import SignalClient, SignalError
from .config import DAEMON_PORT, detect_account


def run(coro):
    return asyncio.run(coro)


@click.group()
@click.version_option(__version__, prog_name="signal-mcp")
def cli():
    """signal-mcp: Signal CLI and MCP server via signal-cli."""
    pass


# ── send ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("recipient")
@click.argument("message")
def send(recipient: str, message: str):
    """Send a text message to RECIPIENT (phone number in E.164 format)."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            result = await client.send_message(recipient, message)
            click.echo(f"Sent (timestamp: {result.timestamp})")
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── send-group ────────────────────────────────────────────────────────────────

@cli.command("send-group")
@click.argument("group_id")
@click.argument("message")
def send_group(group_id: str, message: str):
    """Send a text message to GROUP_ID (use 'groups' command to list IDs)."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            result = await client.send_group_message(group_id, message)
            click.echo(f"Sent (timestamp: {result.timestamp})")
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── receive ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--watch", is_flag=True, help="Keep watching for new messages")
@click.option("--timeout", default=5, show_default=True, help="Seconds to wait per poll")
def receive(watch: bool, timeout: int):
    """Receive incoming messages."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            if not watch:
                messages = await client.receive_messages(timeout=timeout)
                if not messages:
                    click.echo("No new messages.")
                for msg in messages:
                    _print_message(msg)
            else:
                click.echo("Watching for messages (Ctrl+C to stop)…")
                while True:
                    messages = await client.receive_messages(timeout=timeout)
                    for msg in messages:
                        _print_message(msg)
    try:
        run(_run())
    except KeyboardInterrupt:
        click.echo("\nStopped.")
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def _print_message(msg):
    ts = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    group = f" [group:{msg.group_id[:8]}…]" if msg.group_id else ""
    click.echo(f"[{ts}]{group} {msg.sender}: {msg.body}")
    for att in msg.attachments:
        click.echo(f"  📎 {Path(att.filename).name} → {att.local_path}")


# ── contacts ──────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def contacts(as_json: bool):
    """List all Signal contacts."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            items = await client.list_contacts()
            if as_json:
                click.echo(json.dumps([c.to_dict() for c in items], indent=2))
            else:
                for c in items:
                    blocked = " [BLOCKED]" if c.blocked else ""
                    num = c.number or "(no number)"
                    click.echo(f"{c.display_name:<35} {num}{blocked}")
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── groups ────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def groups(as_json: bool):
    """List all Signal groups."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            items = await client.list_groups()
            if as_json:
                click.echo(json.dumps([g.to_dict() for g in items], indent=2))
            else:
                for g in items:
                    name = g.name or "(unnamed)"
                    desc = f"  {g.description}" if g.description else ""
                    click.echo(f"{name:<35} {g.member_count:>3} members  {g.id[:20]}…{desc}")
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── history ───────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("recipient")
@click.option("--limit", default=50, show_default=True, help="Max messages")
@click.option("--since", default=None, help="Only messages after this date (YYYY-MM-DD or ISO datetime)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def history(recipient: str, limit: int, since: str | None, as_json: bool):
    """Show message history with RECIPIENT (phone number or group ID). Reads local store."""
    from datetime import datetime as _dt
    since_dt = None
    if since:
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                since_dt = _dt.strptime(since, fmt)
                break
            except ValueError:
                continue
        if since_dt is None:
            click.echo(f"Error: invalid --since date '{since}' (use YYYY-MM-DD)", err=True)
            sys.exit(1)

    async def _run():
        async with SignalClient() as client:
            messages = await client.get_conversation(recipient, limit=limit, since=since_dt)
            if not messages:
                click.echo("No messages found.")
                return
            if as_json:
                click.echo(json.dumps([m.to_dict() for m in messages], indent=2))
            else:
                for msg in messages:
                    _print_message(msg)
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── search ────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("query")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def search(query: str, as_json: bool):
    """Search recent messages for QUERY."""
    async def _run():
        async with SignalClient() as client:
            messages = await client.search_messages(query)
            if not messages:
                click.echo("No messages found.")
                return
            if as_json:
                click.echo(json.dumps([m.to_dict() for m in messages], indent=2))
            else:
                for msg in messages:
                    _print_message(msg)
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── status ────────────────────────────────────────────────────────────────────

@cli.command()
def status():
    """Show account and daemon status."""
    async def _run():
        try:
            account = detect_account()
            click.echo(f"Account : {account}")
        except Exception as e:
            click.echo(f"Account : ERROR — {e}")
            return

        async with SignalClient(account=account) as client:
            alive = await client._daemon_alive()
            state = "running" if alive else "stopped"
            click.echo(f"Daemon  : {state} (port {DAEMON_PORT})")
            click.echo(f"Version : signal-mcp {__version__}")
    run(_run())


# ── daemon ────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--port", default=DAEMON_PORT, show_default=True)
def daemon(port: int):
    """Start the signal-cli JSON-RPC daemon in the foreground."""
    try:
        account = detect_account()
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    click.echo(f"Starting signal-cli daemon for {account} on port {port}…")
    click.echo("Press Ctrl+C to stop.")
    try:
        subprocess.run([
            "signal-cli", "-u", account,
            "daemon", f"--http", f"localhost:{port}",
            "--no-receive-stdout",
        ])
    except KeyboardInterrupt:
        click.echo("\nDaemon stopped.")


# ── stop ──────────────────────────────────────────────────────────────────────

@cli.command()
def stop():
    """Stop the running signal-cli daemon."""
    async def _run():
        async with SignalClient() as client:
            stopped = await client.stop_daemon()
            if stopped:
                click.echo("Daemon stopped.")
            else:
                click.echo("Daemon was not running.")
    run(_run())


# ── store-stats ───────────────────────────────────────────────────────────────

@cli.command("store-stats")
def store_stats():
    """Show stats about locally stored messages."""
    stats = _store.get_stats()
    click.echo(f"Total messages : {stats['total_messages']}")
    click.echo(f"Oldest         : {stats['oldest'] or 'n/a'}")
    click.echo(f"Newest         : {stats['newest'] or 'n/a'}")


# ── import-desktop ────────────────────────────────────────────────────────────

@cli.command("import-desktop")
def import_desktop():
    """Import ALL messages from Signal Desktop (requires macOS Keychain access)."""
    from .desktop import import_from_desktop, DesktopImportError

    def progress(msg):
        click.echo(f"  {msg}")

    click.echo("Importing from Signal Desktop…")
    click.echo("  Note: macOS may ask for Keychain access — click Allow.")
    try:
        result = import_from_desktop(progress_cb=progress)
        click.echo(f"\nDone: {result['imported']} imported, {result['skipped']} already stored ({result['total']} total)")
    except DesktopImportError as e:
        click.echo(f"\nError: {e}", err=True)
        sys.exit(1)


# ── install-service ───────────────────────────────────────────────────────────

PLIST_LABEL = "com.signal-mcp.watch"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"
SYSTEMD_SERVICE_NAME = "signal-mcp-watch"
SYSTEMD_SERVICE_PATH = Path.home() / ".config" / "systemd" / "user" / f"{SYSTEMD_SERVICE_NAME}.service"


def _find_binary() -> str:
    import shutil
    binary = shutil.which("signal-mcp")
    if binary:
        return binary
    return f"uv run --directory {Path(__file__).parent.parent.parent} signal-mcp"


@cli.command("install-service")
def install_service():
    """Install a background service to auto-receive Signal messages (macOS LaunchAgent or Linux systemd)."""
    import platform
    binary = _find_binary()
    log_dir = Path.home() / ".local" / "share" / "signal-mcp"
    log_dir.mkdir(parents=True, exist_ok=True)

    if platform.system() == "Darwin":
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{binary}</string>
        <string>receive</string>
        <string>--watch</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir}/watch.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/watch.err</string>
</dict>
</plist>"""
        PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        PLIST_PATH.write_text(plist)
        result = subprocess.run(
            ["launchctl", "load", "-w", str(PLIST_PATH)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            click.echo(f"Warning: launchctl load failed: {result.stderr.strip()}")
        else:
            click.echo("Service installed and started.")
            click.echo(f"  Plist : {PLIST_PATH}")
            click.echo(f"  Log   : {log_dir}/watch.log")
            click.echo("  Messages will be captured automatically on login.")

    elif platform.system() == "Linux":
        unit = f"""[Unit]
Description=signal-mcp message watcher
After=network.target

[Service]
ExecStart={binary} receive --watch
Restart=always
RestartSec=5
StandardOutput=append:{log_dir}/watch.log
StandardError=append:{log_dir}/watch.err

[Install]
WantedBy=default.target
"""
        SYSTEMD_SERVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
        SYSTEMD_SERVICE_PATH.write_text(unit)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        result = subprocess.run(
            ["systemctl", "--user", "enable", "--now", SYSTEMD_SERVICE_NAME],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            click.echo(f"Warning: systemctl enable failed: {result.stderr.strip()}")
            click.echo(f"  Unit file written to {SYSTEMD_SERVICE_PATH}")
            click.echo("  Run manually: systemctl --user enable --now signal-mcp-watch")
        else:
            click.echo("Service installed and started.")
            click.echo(f"  Unit  : {SYSTEMD_SERVICE_PATH}")
            click.echo(f"  Log   : {log_dir}/watch.log")
            click.echo("  Messages will be captured automatically on login.")
    else:
        click.echo(f"Unsupported platform: {platform.system()}", err=True)
        sys.exit(1)


@cli.command("uninstall-service")
def uninstall_service():
    """Remove the background Signal message watcher service."""
    import platform
    if platform.system() == "Darwin":
        if not PLIST_PATH.exists():
            click.echo("Service not installed.")
            return
        subprocess.run(["launchctl", "unload", "-w", str(PLIST_PATH)], capture_output=True)
        PLIST_PATH.unlink(missing_ok=True)
        click.echo("Service uninstalled.")
    elif platform.system() == "Linux":
        if not SYSTEMD_SERVICE_PATH.exists():
            click.echo("Service not installed.")
            return
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", SYSTEMD_SERVICE_NAME],
            capture_output=True,
        )
        SYSTEMD_SERVICE_PATH.unlink(missing_ok=True)
        click.echo("Service uninstalled.")
    else:
        click.echo(f"Unsupported platform: {platform.system()}", err=True)
        sys.exit(1)


# ── serve (MCP) ───────────────────────────────────────────────────────────────

@cli.command()
def serve():
    """Start the MCP server (stdio transport, for Claude Code)."""
    from .server import serve as _serve
    asyncio.run(_serve())
