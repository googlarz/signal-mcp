"""CLI entrypoint for signal-mcp."""

import asyncio
import json
import plistlib
import shlex
import signal
import subprocess
import sys
from pathlib import Path

import click

from . import __version__, store as _store
from .client import _E164_RE, SignalClient, SignalError
from .config import (
    DAEMON_PORT,
    check_signal_cli_version,
    clear_daemon_pid,
    detect_account,
    get_account_data_dir,
    is_service_installed,
    save_daemon_pid,
)


def run(coro):
    return asyncio.run(coro)


@click.group()
@click.version_option(__version__, prog_name="signal-mcp")
def cli():
    """signal-mcp: Signal CLI and MCP server via signal-cli."""
    pass


# ── send ──────────────────────────────────────────────────────────────────────

def _send_flags(f):
    """Options shared by `send` and `send-group`, forwarded to the client's send options."""
    for opt in reversed([
        click.option("--quote-author", default=None, help="Author (E.164) of the message to reply to"),
        click.option("--quote-timestamp", type=int, default=None, help="Timestamp of the message to reply to"),
        click.option("--preview-url", default=None, help="Link preview URL (must also appear in MESSAGE)"),
        click.option("--preview-title", default=None, help="Link preview title"),
        click.option("--preview-description", default=None, help="Link preview description"),
        click.option("--no-urgent", is_flag=True, help="Send without a push notification"),
    ]):
        f = opt(f)
    return f


@cli.command()
@click.argument("recipient")
@click.argument("message")
@click.option("--format", "formatting", is_flag=True,
              help="Turn **bold**, *italic*, ~~strike~~, `mono` and ||spoiler|| into Signal formatting")
@_send_flags
def send(recipient: str, message: str, formatting: bool, **options):
    """Send a text message to RECIPIENT (E.164 phone number, or a username like alice.42)."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            if _E164_RE.match(recipient):
                result = await client.send_message(recipient, message, formatting=formatting, **options)
            else:
                result = await client.send_message(None, message, username=recipient, formatting=formatting, **options)
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
@click.option("--format", "formatting", is_flag=True,
              help="Turn **bold**, *italic*, ~~strike~~, `mono` and ||spoiler|| into Signal formatting")
@_send_flags
def send_group(group_id: str, message: str, formatting: bool, **options):
    """Send a text message to GROUP_ID (use 'groups' command to list IDs)."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            result = await client.send_group_message(group_id, message, formatting=formatting, **options)
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
@click.option("--interval", default=2, show_default=True, help="Poll interval for --watch mode (seconds)")
@click.option("--json", "as_json", is_flag=True, help="Output newline-delimited JSON (one object per message)")
@click.option("--webhook", "webhook_url", default=None,
              help="POST each message as JSON to this URL (overrides SIGNAL_MCP_WEBHOOK env var and saved config)")
def receive(watch: bool, timeout: int, interval: int, as_json: bool, webhook_url: str | None):
    """Receive incoming messages.

    \b
    Examples:
      signal-mcp receive                          # one-shot, human output
      signal-mcp receive --watch                  # continuous watch, human output
      signal-mcp receive --watch --json           # continuous JSON stream (newline-delimited)
      signal-mcp receive --watch --webhook http://localhost:8080/signal
      SIGNAL_MCP_WEBHOOK=http://... signal-mcp receive --watch
    """
    from .config import get_webhook_url
    from .webhook import post_webhook_batch

    # Resolve webhook URL: flag > env/config
    effective_webhook = webhook_url or get_webhook_url()

    def _emit(messages):
        """Print messages and/or POST to webhook."""
        for msg in messages:
            if as_json:
                click.echo(json.dumps(msg.to_dict(), default=str))
            else:
                _print_message(msg)
        if effective_webhook and messages:
            asyncio.ensure_future(post_webhook_batch(effective_webhook, messages))

    async def _run():
        async with SignalClient() as client:
            if effective_webhook and not as_json:
                click.echo(f"Webhook: {effective_webhook}", err=True)

            if not watch:
                messages = await client.receive_direct(timeout=timeout)
                if not messages:
                    if not as_json:
                        click.echo("No new messages.")
                    return
                for msg in messages:
                    if as_json:
                        click.echo(json.dumps(msg.to_dict(), default=str))
                    else:
                        _print_message(msg)
                if effective_webhook and messages:
                    await post_webhook_batch(effective_webhook, messages)
            else:
                from .desktop import sync_from_desktop, SIGNAL_DB, DesktopImportError
                use_desktop = SIGNAL_DB.exists()
                if not as_json:
                    if use_desktop:
                        click.echo("Watching for messages via Signal Desktop DB (Ctrl+C to stop)…", err=True)
                    else:
                        click.echo("Watching for messages via signal-cli (Ctrl+C to stop)…", err=True)
                while True:
                    try:
                        if use_desktop:
                            result = await asyncio.to_thread(sync_from_desktop)
                            if result["imported"] > 0 and not as_json:
                                click.echo(f"[watch] synced {result['imported']} new messages", err=True)
                        else:
                            messages = await client.receive_direct(timeout=timeout)
                            if messages:
                                for msg in messages:
                                    if as_json:
                                        click.echo(json.dumps(msg.to_dict(), default=str))
                                    else:
                                        _print_message(msg)
                                if effective_webhook:
                                    await post_webhook_batch(effective_webhook, messages)
                    except DesktopImportError as e:
                        if not as_json:
                            click.echo(f"[watch] desktop sync error: {e}", err=True)
                        use_desktop = False
                        if not as_json:
                            click.echo("[watch] falling back to signal-cli receive", err=True)
                    except Exception as e:
                        if not as_json:
                            click.echo(f"[watch] receive error: {e}", err=True)
                    await asyncio.sleep(interval)
    try:
        run(_run())
    except KeyboardInterrupt:
        if not as_json:
            click.echo("\nStopped.")
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def _print_message(msg, client=None):
    """Print one message; with *client*, resolve sender and group to names (cache must be loaded)."""
    if msg.receipt_type:
        click.echo(f"[{msg.timestamp.strftime('%Y-%m-%d %H:%M:%S')}] ← {msg.receipt_type} receipt from {msg.sender}")
        return
    ts = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    sender = client.resolve_name(msg.sender) if client else msg.sender
    group = f" [group:{msg.group_id[:8]}…]" if msg.group_id else ""
    if client and msg.group_id:
        group_name = client.resolve_group_name(msg.group_id)
        if group_name != msg.group_id:
            group = f" [{group_name}]"
    click.echo(f"[{ts}]{group} {sender}: {msg.body}")
    for att in msg.attachments:
        click.echo(f"  📎 {Path(att.filename).name} → {att.local_path}")


# ── contacts ──────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--all-recipients", is_flag=True,
              help="Also include non-contacts (e.g. group members) with profile names")
def contacts(as_json: bool, all_recipients: bool):
    """List all Signal contacts."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            items = await client.list_contacts(all_recipients=all_recipients)
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


@cli.command("group-label")
@click.argument("group_id")
@click.argument("label")
@click.option("--emoji", default=None, help="Emoji shown with the label")
def group_label(group_id: str, label: str, emoji: str | None):
    """Set YOUR OWN member label in GROUP_ID (cannot set other members' labels)."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            await client.update_group(group_id, member_label=label, member_label_emoji=emoji)
            click.echo(f"Label set to {label!r}")
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── history ───────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("recipient")
@click.option("--limit", default=50, show_default=True, help="Max messages")
@click.option("--offset", default=0, show_default=True, help="Skip N messages (for pagination)")
@click.option("--since", default=None, help="Only messages after this date (YYYY-MM-DD or ISO datetime)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def history(recipient: str, limit: int, offset: int, since: str | None, as_json: bool):
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
            messages = await client.get_conversation(recipient, limit=limit, offset=offset, since=since_dt)
            if not messages:
                click.echo("No messages found.")
                return
            if as_json:
                click.echo(json.dumps([m.to_dict() for m in messages], indent=2))
            else:
                await client._ensure_caches()
                for msg in messages:
                    _print_message(msg, client)
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── note ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("message")
def note(message: str):
    """Send a note to yourself (saved messages)."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            result = await client.send_note_to_self(message)
            click.echo(f"Note saved (timestamp: {result.timestamp})")
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── story ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("path")
@click.option("--group", "group_id", default=None, help="Post to this group's story instead of My Story")
@click.option("--no-replies", is_flag=True, help="Disable replies to the story")
def story(path: str, group_id: str | None, no_replies: bool):
    """Post the image or video at PATH as a story."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            result = await client.send_story(path, group_id=group_id, allow_replies=not no_replies)
            click.echo(f"Story posted (timestamp: {result.timestamp})")
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── edit ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("recipient")
@click.argument("timestamp", type=int)
@click.argument("message")
def edit(recipient: str, timestamp: int, message: str):
    """Edit a previously sent message. RECIPIENT is a phone number or group ID."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            if _E164_RE.match(recipient):
                await client.edit_message(timestamp, message, recipient=recipient)
            else:
                await client.edit_message(timestamp, message, group_id=recipient)
            click.echo("Message edited.")
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── react ────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("recipient")
@click.argument("timestamp", type=int)
@click.argument("author")
@click.argument("emoji")
@click.option("--remove", is_flag=True, help="Remove the reaction instead of adding it")
def react(recipient: str, timestamp: int, author: str, emoji: str, remove: bool):
    """React to a message with EMOJI. RECIPIENT is a phone number or group ID."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            if _E164_RE.match(recipient):
                await client.react_to_message(author, timestamp, emoji, remove=remove, recipient=recipient)
            else:
                await client.react_to_message(author, timestamp, emoji, remove=remove, group_id=recipient)
            action = "removed" if remove else "sent"
            click.echo(f"Reaction {emoji} {action}.")
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── delete ───────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("recipient")
@click.argument("timestamp", type=int)
def delete(recipient: str, timestamp: int):
    """Delete a message you sent. RECIPIENT is a phone number or group ID."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            if _E164_RE.match(recipient):
                await client.delete_message(recipient, timestamp)
            else:
                await client.delete_group_message(recipient, timestamp)
            click.echo("Message deleted.")
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── block / unblock ───────────────────────────────────────────────────────────

@cli.command()
@click.argument("number")
def block(number: str):
    """Block a contact or group by phone number or group ID."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            await client.block_contact(number)
            click.echo(f"Blocked {number}.")
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("number")
def unblock(number: str):
    """Unblock a contact or group."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            await client.unblock_contact(number)
            click.echo(f"Unblocked {number}.")
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── search ────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("query")
@click.option("--sender", default=None, help="Restrict to messages from this phone number (E.164)")
@click.option("--limit", default=50, show_default=True, help="Max results")
@click.option("--offset", default=0, show_default=True, help="Skip this many results (pagination)")
@click.option("--since", default=None, help="Only messages at or after this ISO datetime (e.g. 2024-01-01)")
@click.option("--until", default=None, help="Only messages before this ISO datetime (exclusive)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def search(query: str, sender: str | None, limit: int, offset: int,
           since: str | None, until: str | None, as_json: bool):
    """Search recent messages for QUERY."""
    from datetime import datetime as _dt
    bounds = {}
    for opt, value in (("since", since), ("until", until)):
        try:
            bounds[opt] = _dt.fromisoformat(value) if value else None
        except ValueError:
            click.echo(f"Error: invalid --{opt} date: {value!r}", err=True)
            sys.exit(1)

    async def _run():
        async with SignalClient() as client:
            messages = await client.search_messages(
                query, limit=limit, offset=offset, sender=sender,
                since=bounds["since"], until=bounds["until"],
            )
            if not messages:
                click.echo("No messages found.")
                return
            if as_json:
                click.echo(json.dumps([m.to_dict() for m in messages], indent=2))
            else:
                await client._ensure_caches()
                for msg in messages:
                    _print_message(msg, client)
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── conversations ─────────────────────────────────────────────────────────────

@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def conversations(as_json: bool):
    """List all conversations ordered by most recent message."""
    async def _run():
        async with SignalClient() as client:
            await client._ensure_contact_cache()
            await client._ensure_group_cache()
            convs = await client.list_conversations()
            if not convs:
                click.echo("No conversations found.")
                return
            if as_json:
                click.echo(json.dumps(convs, indent=2))
            else:
                for c in convs:
                    unread = f" ({c['unread_count']} unread)" if c.get("unread_count") else ""
                    name = c.get("name") or c["id"]
                    snippet = c.get("last_message", "")[:60]
                    click.echo(f"{name:<35} {c['type']:<7}{unread}")
                    if snippet:
                        click.echo(f"  {snippet}")
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


# ── doctor ────────────────────────────────────────────────────────────────────

@cli.command()
def doctor():
    """Run onboarding smoke checks: signal-cli, account, daemon, receive health."""
    ok = True

    def check(label: str, passed: bool, detail: str = ""):
        nonlocal ok
        ok = ok and passed
        mark = "✓" if passed else "✗"
        click.echo(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))

    try:
        check_signal_cli_version()
        check("signal-cli installed and recent enough", True)
    except Exception as e:
        check("signal-cli installed and recent enough", False, str(e))

    try:
        account = detect_account()
        check("Signal account detected", True, account)
    except Exception as e:
        check("Signal account detected", False, str(e))
        click.echo(f"\n{'OK' if ok else 'FAILED'} — stopping early, nothing else to check without an account.")
        sys.exit(0 if ok else 1)

    async def _run():
        nonlocal ok
        async with SignalClient(account=account) as client:
            alive = await client._daemon_alive()
            check("Daemon reachable", alive, f"port {DAEMON_PORT}" if alive else "run: signal-mcp daemon")
            if not alive:
                return

            try:
                devices = await client.list_devices()
                names = ", ".join(f"{d['id']}:{d.get('name') or '(unnamed)'}" for d in devices)
                check("Devices readable", True, f"{len(devices)} linked — {names}")
                if len(devices) > 1:
                    click.echo(
                        "    Multiple devices linked — identify which one is this account below. "
                        "Only device 1 is primary; write tools like update_configuration, "
                        "block_contact, set_pin, add_device fail on any other device with "
                        "\"This command doesn't work on linked devices\". This check can't tell "
                        "which device this signal-mcp instance is (signal-cli doesn't expose it "
                        "over JSON-RPC) — match by name/last-seen, or just try one of those tools."
                    )
            except Exception as e:
                check("Devices readable", False, f"{type(e).__name__}: {e}")

            if is_service_installed():
                # The background watcher (signal-mcp receive --watch) holds signal-cli's
                # receive lock continuously — calling receive here would only fail with
                # "Receive command cannot be used if messages are already being received",
                # not tell us anything real. Check the watcher's own liveness instead.
                check(
                    "Receive round-trip works", True,
                    "skipped — background watch service is installed and holds the "
                    "receive lock; check its own log instead (~/.local/share/signal-mcp/watch.err)",
                )
            else:
                try:
                    await client.receive_messages(timeout=3)
                    check("Receive round-trip works", True)
                except Exception as e:
                    check("Receive round-trip works", False, f"{type(e).__name__}: {e}")

        data_dir = get_account_data_dir(account)
        if data_dir is None:
            check("msg-cache readable", False, "could not locate account data dir")
        else:
            cache_dir = data_dir / "msg-cache"
            if not cache_dir.exists():
                check("msg-cache readable", True, "empty")
            else:
                stuck = [f for f in cache_dir.iterdir() if f.is_file()]
                if stuck:
                    names = ", ".join(f.name for f in stuck[:5])
                    check(
                        "msg-cache readable", False,
                        f"{len(stuck)} unprocessed entr{'y' if len(stuck) == 1 else 'ies'} "
                        f"({names}) — a malformed one can crash the receive thread on daemon "
                        f"startup (silently, no messages come in); move them out of {cache_dir} "
                        "if receive stops working after this",
                    )
                else:
                    check("msg-cache readable", True, "clean")

    run(_run())
    click.echo(f"\n{'OK' if ok else 'FAILED'}")
    sys.exit(0 if ok else 1)


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
    proc = subprocess.Popen([
        "signal-cli", "-u", account,
        "daemon", f"--http", f"localhost:{port}",
        "--no-receive-stdout",
    ])
    save_daemon_pid(proc.pid)

    def _handle_sigterm(signum, frame):  # pragma: no cover
        # Python's default SIGTERM disposition kills the process without
        # running `finally` blocks -- launchd sends exactly this signal on
        # every stop/restart, so without a handler the PID file below never
        # gets cleared on the realistic shutdown path (only on Ctrl+C).
        proc.terminate()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)
    try:  # pragma: no cover
        proc.wait()
    except KeyboardInterrupt:  # pragma: no cover
        proc.terminate()
        proc.wait()
        click.echo("\nDaemon stopped.")
    finally:
        clear_daemon_pid()


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
    db_kb = stats["db_size_bytes"] / 1024
    click.echo(f"Total messages : {stats['total_messages']}")
    click.echo(f"Unread         : {stats['unread_messages']}")
    click.echo(f"DB size        : {db_kb:.1f} KB")
    click.echo(f"Oldest         : {stats['oldest'] or 'n/a'}")
    click.echo(f"Newest         : {stats['newest'] or 'n/a'}")


# ── prune ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--days", default=180, show_default=True, help="Delete messages older than this many days")
@click.option("--yes", "confirmed", is_flag=True, help="Skip confirmation prompt")
def prune(days: int, confirmed: bool):
    """Delete locally stored messages older than N days."""
    if days <= 0:
        click.echo("Error: --days must be a positive integer.", err=True)
        sys.exit(1)
    if not confirmed:
        click.confirm(f"Delete all locally stored messages older than {days} days?", abort=True)
    deleted = _store.prune_old_messages(days)
    click.echo(f"Deleted {deleted} message(s) older than {days} days.")


# ── import-desktop ────────────────────────────────────────────────────────────

def _echo_keychain_note() -> None:
    import platform
    if platform.system() == "Darwin":
        click.echo("  Note: macOS may ask for Keychain access — click Allow.")


@cli.command("import-desktop")
def import_desktop():
    """Import ALL messages from Signal Desktop (requires sqlcipher and OS keychain/keyring access)."""
    from .desktop import import_from_desktop, DesktopImportError

    def progress(msg):
        click.echo(f"  {msg}")

    click.echo("Importing from Signal Desktop…")
    _echo_keychain_note()
    try:
        result = import_from_desktop(progress_cb=progress)
        click.echo(f"\nDone: {result['imported']} imported, {result['skipped']} already stored ({result['total']} total)")
    except DesktopImportError as e:
        click.echo(f"\nError: {e}", err=True)
        sys.exit(1)


# ── sync-desktop ───────────────────────────────────────────────────────────────

@cli.command("sync-desktop")
def sync_desktop():
    """Incremental sync from Signal Desktop (only new messages since last sync)."""
    from .desktop import sync_from_desktop, DesktopImportError

    def progress(msg):
        click.echo(f"  {msg}")

    click.echo("Syncing from Signal Desktop…")
    _echo_keychain_note()
    try:
        result = sync_from_desktop(progress_cb=progress)
        if result["incremental"]:
            since_str = f" since {result['since']}" if result["since"] else ""
            click.echo(f"\nDone: {result['imported']} imported, {result['skipped']} skipped ({result['total']} checked{since_str})")
        else:
            click.echo(f"\nFirst sync complete: {result['imported']} imported, {result['skipped']} already stored ({result['total']} total)")
    except DesktopImportError as e:
        click.echo(f"\nError: {e}", err=True)
        sys.exit(1)


# ── export ────────────────────────────────────────────────────────────────────

@cli.command("export")
@click.argument("output", type=click.Path(), default="-")
@click.option("--format", "fmt", type=click.Choice(["json", "csv"]), default="json", show_default=True, help="Output format")
@click.option("--recipient", default=None, help="Export only this conversation (phone number or group ID)")
@click.option("--since", default=None, help="Only messages at or after this ISO datetime (e.g. 2024-01-01)")
def export_cmd(output: str, fmt: str, recipient: str | None, since: str | None):
    """Export stored messages to a file (or stdout with OUTPUT=-)."""
    from datetime import datetime as _dt
    since_dt = None
    if since:
        try:
            since_dt = _dt.fromisoformat(since)
        except ValueError:
            click.echo(f"Error: invalid --since date: {since!r}", err=True)
            sys.exit(1)
    data = _store.export_messages(fmt=fmt, recipient=recipient, since=since_dt)
    if output == "-":
        click.echo(data, nl=False)
    else:
        Path(output).write_text(data)
        click.echo(f"Exported to {output}")


# ── pin / unpin ───────────────────────────────────────────────────────────────

@cli.command()
@click.argument("target")          # phone number or group_id
@click.argument("timestamp", type=int)
@click.argument("author")
def pin(target: str, timestamp: int, author: str):
    """Pin a message. TARGET is a phone number or group ID."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            is_group = not _E164_RE.match(target)
            await client.pin_message(
                author, timestamp,
                group_id=target if is_group else None,
                recipient=target if not is_group else None,
            )
            click.echo(f"Pinned message {timestamp} in {target}.")
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("target")
@click.argument("timestamp", type=int)
@click.argument("author")
def unpin(target: str, timestamp: int, author: str):
    """Unpin a message. TARGET is a phone number or group ID."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            is_group = not _E164_RE.match(target)
            await client.unpin_message(
                author, timestamp,
                group_id=target if is_group else None,
                recipient=target if not is_group else None,
            )
            click.echo(f"Unpinned message {timestamp} in {target}.")
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── admin-delete ──────────────────────────────────────────────────────────────

@cli.command("admin-delete")
@click.argument("group_id")
@click.argument("timestamp", type=int)
@click.argument("author")
def admin_delete(group_id: str, timestamp: int, author: str):
    """Admin-delete a message in a group you administer."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            await client.admin_delete_message(author, timestamp, group_id)
            click.echo(f"Admin-deleted message {timestamp} from {author} in {group_id}.")
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── update-device ─────────────────────────────────────────────────────────────

@cli.command("update-device")
@click.argument("device_id", type=int)
@click.argument("name")
def update_device_cmd(device_id: int, name: str):
    """Rename a linked device."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            await client.update_device(device_id, name)
            click.echo(f"Device {device_id} renamed to '{name}'.")
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
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


def _find_binary_args() -> list[str]:
    """Return an argv-safe command for background-service definitions."""
    import shutil
    binary = shutil.which("signal-mcp")
    if binary:
        return [binary]
    uv = shutil.which("uv") or "uv"
    return [uv, "run", "--directory", str(Path(__file__).parent.parent.parent), "signal-mcp"]


@cli.command("install-service")
def install_service():
    """Install a background service to auto-receive Signal messages (macOS LaunchAgent or Linux systemd)."""
    import platform
    binary_args = _find_binary_args()
    log_dir = Path.home() / ".local" / "share" / "signal-mcp"
    log_dir.mkdir(parents=True, exist_ok=True)

    if platform.system() == "Darwin":
        plist = plistlib.dumps({
            "Label": PLIST_LABEL,
            "ProgramArguments": [*binary_args, "receive", "--watch"],
            "EnvironmentVariables": {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            },
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(log_dir / "watch.log"),
            "StandardErrorPath": str(log_dir / "watch.err"),
        })
        PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        PLIST_PATH.write_bytes(plist)
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
ExecStart={shlex.join([*binary_args, "receive", "--watch"])}
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


# ── webhook config ───────────────────────────────────────────────────────────

@cli.command("set-webhook")
@click.argument("url", required=False, default=None)
def set_webhook(url: str | None):
    """Set (or clear) the webhook URL for incoming messages.

    \b
    Examples:
      signal-mcp set-webhook http://localhost:8080/signal  # set
      signal-mcp set-webhook                               # clear
    """
    from .config import set_webhook_url
    set_webhook_url(url)
    if url:
        click.echo(f"Webhook set: {url}")
    else:
        click.echo("Webhook cleared.")


@cli.command("get-webhook")
def get_webhook():
    """Show the currently configured webhook URL."""
    from .config import get_webhook_url
    url = get_webhook_url()
    if url:
        click.echo(url)
    else:
        click.echo("No webhook configured.")


# ── find-contact ─────────────────────────────────────────────────────────────

@cli.command("find-contact")
@click.argument("query")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--all-recipients", is_flag=True,
              help="Also include non-contacts (e.g. group members) with profile names")
def find_contact(query: str, as_json: bool, all_recipients: bool):
    """Search contacts by name or phone number fragment."""
    async def _run():
        async with SignalClient() as client:
            await client.ensure_daemon()
            contacts = await client.list_contacts(search=query, all_recipients=all_recipients)
            if not contacts:
                click.echo("No matching contacts.")
                return
            if as_json:
                click.echo(json.dumps([c.to_dict() for c in contacts], indent=2))
            else:
                for c in contacts:
                    blocked = " [BLOCKED]" if c.blocked else ""
                    click.echo(f"{c.display_name:<35} {c.number or '(no number)'}{blocked}")
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── schedule-send ─────────────────────────────────────────────────────────────

@cli.command("schedule-send")
@click.argument("recipient")
@click.argument("message")
@click.option("--at", "send_at", required=True,
              help="When to send (ISO datetime, e.g. '2024-06-01 09:00' or '2024-06-01T09:00:00')")
@click.option("--group", "is_group", is_flag=True,
              help="Treat RECIPIENT as a group ID instead of a phone number")
def schedule_send(recipient: str, message: str, send_at: str, is_group: bool):
    """Schedule a message to be sent at a specific time.

    Use 'signal-mcp scheduled' to list pending jobs and
    'signal-mcp cancel-scheduled ID' to cancel one.
    The background service (install-service) will deliver them automatically.
    """
    from datetime import datetime as _dt
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            dt = _dt.strptime(send_at, fmt)
            break
        except ValueError:
            continue
    else:
        click.echo(f"Error: invalid --at value '{send_at}'. Use ISO format e.g. '2024-06-01 09:00'", err=True)
        sys.exit(1)

    if dt <= _dt.now():
        click.echo("Error: --at must be in the future.", err=True)
        sys.exit(1)

    from . import store as _store
    if is_group:
        job_id = _store.add_scheduled_message(message, dt, group_id=recipient)
    else:
        job_id = _store.add_scheduled_message(message, dt, recipient=recipient)
    click.echo(f"Scheduled (id={job_id}): send to {recipient} at {dt.strftime('%Y-%m-%d %H:%M:%S')}")


@cli.command("scheduled")
@click.option("--all", "include_done", is_flag=True, help="Include sent/cancelled/failed messages")
@click.option("--json", "as_json", is_flag=True)
def list_scheduled(include_done: bool, as_json: bool):
    """List scheduled messages."""
    from . import store as _store
    jobs = _store.list_scheduled_messages(include_done=include_done)
    if not jobs:
        click.echo("No scheduled messages.")
        return
    if as_json:
        click.echo(json.dumps(jobs, indent=2))
    else:
        for j in jobs:
            status = j["status"]
            target = j.get("group_id") or j.get("recipient") or "?"
            click.echo(f"[{j['id']:>4}] {j['send_at']}  →  {target}  [{status}]")
            click.echo(f"       {j['message'][:80]}")


@cli.command("cancel-scheduled")
@click.argument("job_id", type=int)
def cancel_scheduled(job_id: int):
    """Cancel a pending scheduled message by ID (see 'scheduled' command)."""
    from . import store as _store
    if _store.cancel_scheduled_message(job_id):
        click.echo(f"Cancelled scheduled message {job_id}.")
    else:
        click.echo(f"No pending scheduled message with id={job_id}.", err=True)
        sys.exit(1)


@cli.command("run-scheduled")
def run_scheduled():
    """Process and send any scheduled messages that are due now."""
    async def _run():
        async with SignalClient() as client:
            results = await client.process_scheduled_messages()
            if not results:
                click.echo("No scheduled messages due.")
                return
            for r in results:
                if r["status"] == "sent":
                    click.echo(f"  Sent  id={r['id']} (ts={r['timestamp']})")
                else:
                    click.echo(f"  Failed id={r['id']}: {r['error']}", err=True)
    try:
        run(_run())
    except SignalError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── install (setup wizard) ────────────────────────────────────────────────────

@cli.command("install")
def install():
    """Interactive first-time setup wizard.

    Checks signal-cli, detects your linked account, and optionally installs
    the background message-capture service.
    """
    import platform
    from .config import check_signal_cli_version

    click.echo("=== signal-mcp setup ===\n")

    # 1. Check signal-cli
    click.echo("Checking signal-cli…", nl=False)
    try:
        check_signal_cli_version()
        import subprocess as _sp
        ver = _sp.run(["signal-cli", "--version"], capture_output=True, text=True).stdout.strip()
        click.echo(f" ✓  ({ver})")
    except RuntimeError as e:
        click.echo(f"\n✗  {e}")
        sys.exit(1)

    # 2. Check linked account
    click.echo("Detecting linked account…", nl=False)
    try:
        account = detect_account()
        click.echo(f" ✓  {account}")
    except RuntimeError:
        click.echo("\n✗  No account found.")
        click.echo("\nTo link signal-mcp to your Signal account, run:")
        click.echo("  signal-cli link --name 'MyComputer'")
        click.echo("Then scan the QR code shown, or open the URL on your phone.")
        sys.exit(1)

    # 3. Background service
    from .config import is_service_installed
    if is_service_installed():
        click.echo("Background service: ✓  already installed")
    else:
        click.echo()
        install_svc = click.confirm(
            "Install background service to auto-capture incoming messages?",
            default=True,
        )
        if install_svc:
            ctx = click.get_current_context()
            ctx.invoke(install_service)

    # 4. MCP config hint
    click.echo()
    click.echo("=== MCP configuration ===")
    click.echo("Add to your Claude Code MCP settings:")
    click.echo()
    binary = _find_binary()
    click.echo(json.dumps({
        "mcpServers": {
            "signal": {
                "command": binary,
                "args": ["serve"],
            }
        }
    }, indent=2))
    click.echo()
    click.echo("Setup complete. Run 'signal-mcp status' to verify.")


# ── serve (MCP) ───────────────────────────────────────────────────────────────

@cli.command()
def serve():
    """Start the MCP server (stdio transport, for Claude Code)."""
    from .server import serve as _serve  # pragma: no cover
    asyncio.run(_serve())  # pragma: no cover
