"""Async signal-cli JSON-RPC client. Single backend for all reads and writes."""

import asyncio
import itertools
import os
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

import httpx

from .config import (
    ATTACHMENT_DIR,
    DAEMON_PORT,
    DAEMON_URL,
    clear_daemon_pid,
    detect_account,
    ensure_attachment_dir,
    read_daemon_pid,
    save_daemon_pid,
)
from .models import Attachment, Contact, Group, GroupMember, Message, SendResult
from . import store as _store


class SignalError(Exception):
    pass


_rpc_id = itertools.count(1)

# Module-level contact name cache: number → display_name
_contact_cache: dict[str, str] = {}
_contact_cache_loaded: bool = False


class SignalClient:
    def __init__(self, account: str | None = None, daemon_url: str = DAEMON_URL):
        self._account = account
        self._daemon_url = daemon_url
        self._http = httpx.AsyncClient(timeout=10.0)
        self._rpc_lock = asyncio.Lock()
        self._daemon_lock = asyncio.Lock()  # single-flight guard for ensure_daemon
        self._background_tasks: list[asyncio.Task] = []

    @property
    def account(self) -> str:
        if self._account is None:
            self._account = detect_account()
        return self._account

    async def close(self) -> None:
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    # ── Daemon management ─────────────────────────────────────────────────────

    async def ensure_daemon(self) -> None:
        """Start signal-cli daemon if not already running (single-flight)."""
        # Fast path without lock
        if await self._daemon_alive():
            return

        async with self._daemon_lock:
            # Re-check after acquiring lock: another caller may have started it
            if await self._daemon_alive():
                return

            stale_pid = read_daemon_pid()
            if stale_pid:
                try:
                    os.kill(stale_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                clear_daemon_pid()
                await asyncio.sleep(0.5)

            proc = subprocess.Popen(
                [
                    "signal-cli", "-u", self.account,
                    "daemon",
                    "--http", f"localhost:{DAEMON_PORT}",
                    "--no-receive-stdout",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            save_daemon_pid(proc.pid)

            for _ in range(20):
                await asyncio.sleep(0.5)
                if await self._daemon_alive():
                    return

            raise SignalError(
                "signal-cli daemon failed to start within 10 seconds. "
                "Try running manually: signal-mcp daemon"
            )

    async def stop_daemon(self) -> bool:
        """Stop the running daemon. Returns True if stopped."""
        pid = read_daemon_pid()
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                clear_daemon_pid()
                return True
            except ProcessLookupError:
                clear_daemon_pid()
        try:
            result = subprocess.run(
                ["lsof", "-ti", f"tcp:{DAEMON_PORT}"],
                capture_output=True, text=True,
            )
            for pid_str in result.stdout.strip().splitlines():
                try:
                    os.kill(int(pid_str), signal.SIGTERM)
                    return True
                except (ValueError, ProcessLookupError):
                    pass
        except FileNotFoundError:
            pass
        return False

    async def _daemon_alive(self) -> bool:
        try:
            r = await self._http.post(
                self._daemon_url,
                json={"jsonrpc": "2.0", "method": "version", "id": 0},
            )
            return r.status_code == 200
        except Exception:
            return False

    async def prewarm(self) -> None:
        """Start daemon in background without blocking. Called at server startup."""
        task = asyncio.create_task(self.ensure_daemon())
        self._background_tasks.append(task)
        task.add_done_callback(lambda t: self._background_tasks.remove(t) if t in self._background_tasks else None)

    async def watchdog(self) -> None:
        """Periodically check daemon health and restart if dead. Runs forever."""
        while True:
            try:
                await asyncio.sleep(30)
                if not await self._daemon_alive():
                    await self.ensure_daemon()
            except asyncio.CancelledError:
                return
            except Exception:
                pass  # best-effort; never crash the server

    # ── JSON-RPC core ─────────────────────────────────────────────────────────

    async def _rpc(self, method: str, params: dict | None = None) -> dict:
        payload: dict = {
            "jsonrpc": "2.0",
            "method": method,
            "id": next(_rpc_id),
        }
        if params:
            payload["params"] = params

        async with self._rpc_lock:
            for attempt in range(2):
                try:
                    r = await self._http.post(self._daemon_url, json=payload)
                    r.raise_for_status()
                    break
                except httpx.ConnectError:
                    if attempt == 0:
                        await asyncio.sleep(0.5)
            else:
                raise SignalError("signal-cli daemon not running. Run: signal-mcp daemon")

        body = r.json()
        if "error" in body:
            raise SignalError(f"signal-cli error: {body['error'].get('message', body['error'])}")
        return body.get("result", {})

    # ── Messaging ─────────────────────────────────────────────────────────────

    async def send_message(
        self,
        recipient: str,
        message: str,
        quote_author: str | None = None,
        quote_timestamp: int | None = None,
    ) -> SendResult:
        params: dict = {"recipient": [recipient], "message": message}
        if quote_author and quote_timestamp:
            params["quoteAuthor"] = quote_author
            params["quoteTimestamp"] = quote_timestamp
        result = await self._rpc("send", params)
        ts = result.get("timestamp", int(time.time() * 1000))
        _store.save_message(Message(
            id=f"sent_{ts}_{recipient}",
            sender=self.account,
            recipient=recipient,
            body=message,
            timestamp=datetime.fromtimestamp(ts / 1000),
            quote_id=str(quote_timestamp) if quote_timestamp else None,
        ))
        return SendResult(timestamp=ts, recipient=recipient, success=True)

    async def send_group_message(
        self,
        group_id: str,
        message: str,
        mentions: list[dict] | None = None,
        quote_author: str | None = None,
        quote_timestamp: int | None = None,
    ) -> SendResult:
        params: dict = {"groupId": group_id, "message": message}
        if mentions:
            params["mention"] = mentions
        if quote_author and quote_timestamp:
            params["quoteAuthor"] = quote_author
            params["quoteTimestamp"] = quote_timestamp
        result = await self._rpc("send", params)
        ts = result.get("timestamp", int(time.time() * 1000))
        _store.save_message(Message(
            id=f"sent_{ts}_{group_id}",
            sender=self.account,
            body=message,
            timestamp=datetime.fromtimestamp(ts / 1000),
            group_id=group_id,
            quote_id=str(quote_timestamp) if quote_timestamp else None,
        ))
        return SendResult(timestamp=ts, recipient=group_id, success=True)

    async def send_note_to_self(self, message: str) -> SendResult:
        """Send a note to yourself (saved messages)."""
        return await self.send_message(self.account, message)

    async def send_attachment(
        self, recipient: str, path: str, caption: str = "", view_once: bool = False
    ) -> SendResult:
        resolved = str(Path(path).expanduser().resolve())
        params: dict = {"recipient": [recipient], "attachment": [resolved]}
        if caption:
            params["message"] = caption
        if view_once:
            params["viewOnce"] = True
        result = await self._rpc("send", params)
        ts = result.get("timestamp", int(time.time() * 1000))
        _store.save_message(Message(
            id=f"sent_{ts}_{recipient}",
            sender=self.account,
            recipient=recipient,
            body=caption,
            timestamp=datetime.fromtimestamp(ts / 1000),
        ))
        return SendResult(timestamp=ts, recipient=recipient, success=True)

    async def send_group_attachment(
        self, group_id: str, path: str, caption: str = "", view_once: bool = False
    ) -> SendResult:
        resolved = str(Path(path).expanduser().resolve())
        params: dict = {"groupId": group_id, "attachment": [resolved]}
        if caption:
            params["message"] = caption
        if view_once:
            params["viewOnce"] = True
        result = await self._rpc("send", params)
        ts = result.get("timestamp", int(time.time() * 1000))
        _store.save_message(Message(
            id=f"sent_{ts}_{group_id}",
            sender=self.account,
            body=caption,
            timestamp=datetime.fromtimestamp(ts / 1000),
            group_id=group_id,
        ))
        return SendResult(timestamp=ts, recipient=group_id, success=True)

    async def send_sticker(
        self, recipient: str, pack_id: str, sticker_id: int
    ) -> SendResult:
        """Send a sticker to a contact."""
        params: dict = {
            "recipient": [recipient],
            "sticker": f"{pack_id}:{sticker_id}",
        }
        result = await self._rpc("send", params)
        ts = result.get("timestamp", int(time.time() * 1000))
        _store.save_message(Message(
            id=f"sent_{ts}_{recipient}",
            sender=self.account,
            recipient=recipient,
            body=f"[sticker {pack_id}:{sticker_id}]",
            timestamp=datetime.fromtimestamp(ts / 1000),
        ))
        return SendResult(timestamp=ts, recipient=recipient, success=True)

    async def send_group_sticker(
        self, group_id: str, pack_id: str, sticker_id: int
    ) -> SendResult:
        """Send a sticker to a group."""
        params: dict = {
            "groupId": group_id,
            "sticker": f"{pack_id}:{sticker_id}",
        }
        result = await self._rpc("send", params)
        ts = result.get("timestamp", int(time.time() * 1000))
        _store.save_message(Message(
            id=f"sent_{ts}_{group_id}",
            sender=self.account,
            body=f"[sticker {pack_id}:{sticker_id}]",
            timestamp=datetime.fromtimestamp(ts / 1000),
            group_id=group_id,
        ))
        return SendResult(timestamp=ts, recipient=group_id, success=True)

    def list_attachments(self) -> list[dict]:
        """List all downloaded attachments in the attachments directory."""
        att_dir = ATTACHMENT_DIR
        if not att_dir.exists():
            return []
        files = []
        for p in sorted(att_dir.iterdir()):
            if p.is_file():
                stat = p.stat()
                files.append({
                    "filename": p.name,
                    "path": str(p),
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
        return files

    def get_attachment(self, filename: str) -> dict:
        """Get info about a specific downloaded attachment by filename."""
        att_dir = ATTACHMENT_DIR
        # Resolve to prevent path traversal (e.g. "../secret")
        path = (att_dir / filename).resolve()
        if path.parent != att_dir.resolve():
            raise SignalError(f"Invalid attachment filename: {filename}")
        if not path.exists() or not path.is_file():
            raise SignalError(f"Attachment not found: {filename}")
        stat = path.stat()
        return {
            "filename": path.name,
            "path": str(path),
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        }

    async def set_typing(self, recipient: str, stop: bool = False) -> None:
        action = "STOPPED" if stop else "STARTED"
        await self._rpc("sendTyping", {"recipient": [recipient], "action": action})

    async def react_to_message(
        self,
        target_author: str,
        target_timestamp: int,
        emoji: str,
        recipient: str | None = None,
        group_id: str | None = None,
    ) -> None:
        if not recipient and not group_id:
            raise SignalError("Either recipient or group_id must be provided")
        params: dict = {
            "emoji": emoji,
            "targetAuthor": target_author,
            "targetTimestamp": target_timestamp,
        }
        if group_id:
            params["groupId"] = group_id
        else:
            params["recipient"] = [recipient]
        await self._rpc("sendReaction", params)

    async def receive_messages(self, timeout: int = 5) -> list[Message]:
        """Poll for new messages and persist them to local store."""
        result = await self._rpc("receive", {"timeout": timeout})
        messages = []
        for envelope in result if isinstance(result, list) else []:
            msg = self._parse_envelope(envelope)
            if msg:
                if not msg.receipt_type:
                    _store.save_message(msg)
                messages.append(msg)
        return messages

    def _parse_envelope(self, envelope: dict) -> Message | None:
        data = envelope.get("envelope", envelope)
        sender = data.get("source", "") or data.get("sourceNumber", "")
        ts_ms = data.get("timestamp", 0)

        # Delivery/read receipts
        receipt = data.get("receiptMessage")
        if receipt:
            receipt_type = receipt.get("type", "DELIVERY")
            return Message(
                id=f"receipt_{ts_ms}_{sender}",
                sender=sender,
                body="",
                timestamp=datetime.fromtimestamp(ts_ms / 1000),
                receipt_type=receipt_type,
            )

        data_message = data.get("dataMessage")
        if not data_message:
            return None

        attachments = []
        for att in data_message.get("attachments", []):
            local_path = att.get("filename")
            if local_path:
                dest = ensure_attachment_dir() / Path(local_path).name
                try:
                    shutil.copy2(local_path, dest)
                    local_path = str(dest)
                except Exception:
                    pass
            attachments.append(Attachment(
                content_type=att.get("contentType", "application/octet-stream"),
                filename=att.get("filename", ""),
                local_path=local_path,
                size=att.get("size"),
            ))

        ts_ms = data_message.get("timestamp", ts_ms)
        quote = data_message.get("quote") or {}
        return Message(
            id=str(ts_ms),
            sender=sender,
            body=data_message.get("message", "") or "",
            timestamp=datetime.fromtimestamp(ts_ms / 1000),
            attachments=attachments,
            group_id=data_message.get("groupInfo", {}).get("groupId"),
            quote_id=str(quote["id"]) if quote.get("id") else None,
        )

    # ── Contact name resolution ───────────────────────────────────────────────

    async def _ensure_contact_cache(self) -> None:
        """Load contacts into module-level cache.

        Only marks the cache as loaded on success — failures allow retry on
        next call so a cold-start race (daemon not yet up) doesn't permanently
        freeze the cache empty.
        """
        global _contact_cache, _contact_cache_loaded
        if _contact_cache_loaded:
            return
        try:
            contacts = await self.list_contacts()
            for c in contacts:
                if c.number:
                    _contact_cache[c.number] = c.display_name
            _contact_cache_loaded = True  # only set on success
        except Exception:
            pass  # will retry on next call

    def resolve_name(self, number: str) -> str:
        """Return display name for a number, or the number itself if unknown."""
        return _contact_cache.get(number, number)

    def _enrich_message(self, msg: Message) -> dict:
        """Convert message to dict and replace numbers with display names."""
        d = msg.to_dict()
        d["sender_name"] = self.resolve_name(msg.sender)
        if msg.recipient:
            d["recipient_name"] = self.resolve_name(msg.recipient)
        return d

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def list_contacts(self) -> list[Contact]:
        result = await self._rpc("listContacts")
        contacts = []
        for c in result if isinstance(result, list) else []:
            profile = c.get("profile") or {}
            contacts.append(Contact(
                number=c.get("number") or "",
                uuid=c.get("uuid"),
                name=(c.get("name") or "").strip() or None,
                given_name=(profile.get("givenName") or c.get("givenName") or "").strip() or None,
                family_name=(profile.get("familyName") or c.get("familyName") or "").strip() or None,
                profile_name=None,
                about=(profile.get("about") or c.get("about") or "").strip() or None,
                blocked=c.get("isBlocked", False),
            ))
        return contacts

    async def get_profile(self, number: str) -> Contact:
        result = await self._rpc("getUserStatus", {"recipient": [number]})
        entries = result if isinstance(result, list) else [result]
        for entry in entries:
            if entry.get("number") == number or entry.get("uuid"):
                profile = entry.get("profile") or {}
                return Contact(
                    number=number,
                    uuid=entry.get("uuid"),
                    name=(entry.get("name") or "").strip() or None,
                    given_name=(profile.get("givenName") or "").strip() or None,
                    family_name=(profile.get("familyName") or "").strip() or None,
                )
        return Contact(number=number)

    async def block_contact(self, number: str) -> None:
        await self._rpc("block", {"recipient": [number]})

    async def unblock_contact(self, number: str) -> None:
        await self._rpc("unblock", {"recipient": [number]})

    async def remove_contact(self, number: str) -> None:
        await self._rpc("removeContact", {"recipient": number})

    async def update_profile(
        self,
        name: str | None = None,
        about: str | None = None,
        avatar_path: str | None = None,
        remove_avatar: bool = False,
    ) -> None:
        params: dict = {}
        if name is not None:
            params["name"] = name
        if about is not None:
            params["about"] = about
        if avatar_path is not None:
            params["avatarPath"] = str(Path(avatar_path).expanduser().resolve())
        if remove_avatar:
            params["removeAvatar"] = True
        await self._rpc("updateProfile", params or None)

    # ── Groups ────────────────────────────────────────────────────────────────

    async def list_groups(self) -> list[Group]:
        result = await self._rpc("listGroups")
        groups = []
        for g in result if isinstance(result, list) else []:
            members = [
                GroupMember(
                    uuid=m.get("uuid", ""),
                    number=m.get("number"),
                    is_admin=m.get("isAdmin", False),
                )
                for m in g.get("members", [])
                if m.get("uuid")
            ]
            admin_uuids = [a.get("uuid", "") for a in g.get("admins", [])]
            groups.append(Group(
                id=g.get("id") or "",
                name=g.get("name") or "",
                members=members,
                description=g.get("description") or None,
                is_blocked=g.get("isBlocked", False),
                is_member=g.get("isMember", True),
                admins=admin_uuids,
                invite_link=g.get("groupInviteLink") or None,
            ))
        return groups

    async def create_group(
        self,
        name: str,
        members: list[str],
        description: str | None = None,
    ) -> dict:
        """Create a new Signal group. Returns the new group info."""
        params: dict = {"name": name, "member": members}
        if description:
            params["description"] = description
        result = await self._rpc("updateGroup", params)
        return result if isinstance(result, dict) else {}

    async def update_group(
        self,
        group_id: str,
        name: str | None = None,
        description: str | None = None,
        add_members: list[str] | None = None,
        remove_members: list[str] | None = None,
        expiration_seconds: int | None = None,
        add_admins: list[str] | None = None,
        remove_admins: list[str] | None = None,
    ) -> None:
        """Update group properties (name, description, members, admins, expiry timer)."""
        params: dict = {"groupId": group_id}
        if name is not None:
            params["name"] = name
        if description is not None:
            params["description"] = description
        if add_members:
            params["member"] = add_members
        if remove_members:
            params["removeMember"] = remove_members
        if expiration_seconds is not None:
            params["expiration"] = expiration_seconds
        if add_admins:
            params["admin"] = add_admins
        if remove_admins:
            params["removeAdmin"] = remove_admins
        await self._rpc("updateGroup", params)

    async def join_group(self, uri: str) -> dict:
        """Join a group via invite link URI."""
        result = await self._rpc("joinGroup", {"uri": uri})
        return result if isinstance(result, dict) else {}

    async def list_devices(self) -> list[dict]:
        """List all linked devices on this account."""
        result = await self._rpc("listDevices")
        return result if isinstance(result, list) else [result] if result else []

    async def add_device(self, uri: str) -> None:
        """Link a new device using a device link URI (from signal-cli link output)."""
        await self._rpc("addDevice", {"uri": uri})

    async def remove_device(self, device_id: int) -> None:
        """Unlink a device by its ID (get IDs from list_devices)."""
        await self._rpc("removeDevice", {"deviceId": device_id})

    # ── History & Search ──────────────────────────────────────────────────────

    async def get_conversation(
        self, recipient: str, limit: int = 50, offset: int = 0, since: datetime | None = None
    ) -> list[Message]:
        messages = _store.get_conversation(recipient, limit=limit, offset=offset, since=since)
        # Auto-mark received messages as read (like every Signal client does)
        unread_ids = [m.id for m in messages if not m.is_read and m.sender != self.account]
        if unread_ids:
            _store.mark_as_read(unread_ids)
            for m in messages:
                if m.id in unread_ids:
                    m.is_read = True
        return messages

    async def search_messages(self, query: str, limit: int = 50) -> list[Message]:
        return _store.search_messages(query, limit=limit)

    def list_conversations(self) -> list[dict]:
        return _store.list_conversations(own_number=self.account)

    def get_unread_messages(self, limit: int = 50) -> list[Message]:
        return _store.get_unread_messages(own_number=self.account, limit=limit)

    def get_own_number(self) -> str:
        return self.account

    # ── Message actions ───────────────────────────────────────────────────────

    async def delete_message(self, recipient: str, target_timestamp: int) -> None:
        await self._rpc("remoteDelete", {
            "recipient": [recipient],
            "targetTimestamp": target_timestamp,
        })

    async def delete_group_message(self, group_id: str, target_timestamp: int) -> None:
        await self._rpc("remoteDelete", {
            "groupId": group_id,
            "targetTimestamp": target_timestamp,
        })

    async def edit_message(
        self,
        target_timestamp: int,
        message: str,
        recipient: str | None = None,
        group_id: str | None = None,
    ) -> None:
        """Edit a previously sent message and update the local store."""
        if not recipient and not group_id:
            raise SignalError("Either recipient or group_id must be provided")
        params: dict = {"targetTimestamp": target_timestamp, "message": message}
        if group_id:
            params["groupId"] = group_id
        else:
            params["recipient"] = [recipient]
        await self._rpc("editMessage", params)
        _store.update_message_body(target_timestamp, message)

    async def send_read_receipt(self, sender: str, timestamps: list[int]) -> None:
        await self._rpc("sendReadReceipt", {
            "recipient": [sender],
            "targetTimestamps": timestamps,
        })
        # Mark as read in local store — received message IDs are str(timestamp_ms)
        _store.mark_as_read([str(ts) for ts in timestamps])

    async def set_expiration_timer(
        self, recipient: str | None = None, group_id: str | None = None, expiration: int = 0
    ) -> None:
        """Set disappearing message timer (seconds). 0 disables."""
        if group_id:
            await self.update_group(group_id, expiration_seconds=expiration)
        elif recipient:
            await self._rpc("updateContact", {
                "recipient": recipient,
                "expiration": expiration,
            })
        else:
            raise SignalError("Either recipient or group_id must be provided")

    async def update_contact(self, number: str, name: str) -> None:
        await self._rpc("updateContact", {
            "recipient": number,
            "name": name,
        })

    async def leave_group(self, group_id: str) -> None:
        await self._rpc("quitGroup", {"groupId": group_id})

    # ── Identity / safety numbers ─────────────────────────────────────────────

    async def list_identities(self, number: str | None = None) -> list[dict]:
        params = {"recipient": number} if number else {}
        result = await self._rpc("listIdentities", params or None)
        return result if isinstance(result, list) else [result] if result else []

    async def trust_identity(self, number: str, trust_all_known: bool = False, safety_number: str | None = None) -> None:
        params: dict = {"recipient": number}
        if safety_number:
            params["verifiedSafetyNumber"] = safety_number
        else:
            params["trustAllKnownKeys"] = trust_all_known
        await self._rpc("trust", params)
