"""Async signal-cli JSON-RPC client. Single backend for all reads and writes."""

import asyncio
import os
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

import httpx

from .config import (
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


class SignalClient:
    def __init__(self, account: str | None = None, daemon_url: str = DAEMON_URL):
        self._account = account
        self._daemon_url = daemon_url
        self._http = httpx.AsyncClient(timeout=10.0)

    @property
    def account(self) -> str:
        if self._account is None:
            self._account = detect_account()
        return self._account

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    # ── Daemon management ─────────────────────────────────────────────────────

    async def ensure_daemon(self) -> None:
        """Start signal-cli daemon if not already running."""
        if await self._daemon_alive():
            return

        # Kill stale PID if present
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
                f"--http", f"localhost:{DAEMON_PORT}",
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
        # Also try killing any signal-cli daemon on our port
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

    # ── JSON-RPC core ─────────────────────────────────────────────────────────

    async def _rpc(self, method: str, params: dict | None = None) -> dict:
        payload: dict = {
            "jsonrpc": "2.0",
            "method": method,
            "id": int(time.time() * 1000),
        }
        if params:
            payload["params"] = params

        try:
            r = await self._http.post(self._daemon_url, json=payload)
            r.raise_for_status()
        except httpx.ConnectError:
            raise SignalError("signal-cli daemon not running. Run: signal-mcp daemon")

        body = r.json()
        if "error" in body:
            raise SignalError(f"signal-cli error: {body['error'].get('message', body['error'])}")
        return body.get("result", {})

    # ── Messaging ─────────────────────────────────────────────────────────────

    async def send_message(self, recipient: str, message: str) -> SendResult:
        result = await self._rpc("send", {
            "recipient": [recipient],
            "message": message,
        })
        ts = result.get("timestamp", int(time.time() * 1000))
        _store.save_message(Message(
            id=f"sent_{ts}_{recipient}",
            sender=self.account,
            body=message,
            timestamp=datetime.fromtimestamp(ts / 1000),
        ))
        return SendResult(timestamp=ts, recipient=recipient, success=True)

    async def send_group_message(self, group_id: str, message: str) -> SendResult:
        result = await self._rpc("send", {
            "groupId": group_id,
            "message": message,
        })
        ts = result.get("timestamp", int(time.time() * 1000))
        _store.save_message(Message(
            id=f"sent_{ts}_{group_id}",
            sender=self.account,
            body=message,
            timestamp=datetime.fromtimestamp(ts / 1000),
            group_id=group_id,
        ))
        return SendResult(timestamp=ts, recipient=group_id, success=True)

    async def send_attachment(self, recipient: str, path: str, caption: str = "") -> SendResult:
        params: dict = {"recipient": [recipient], "attachment": [path]}
        if caption:
            params["message"] = caption
        result = await self._rpc("send", params)
        ts = result.get("timestamp", int(time.time() * 1000))
        return SendResult(timestamp=ts, recipient=recipient, success=True)

    async def send_group_attachment(self, group_id: str, path: str, caption: str = "") -> SendResult:
        params: dict = {"groupId": [group_id], "attachment": [path]}
        if caption:
            params["message"] = caption
        result = await self._rpc("sendGroupMessage", params)
        ts = result.get("timestamp", int(time.time() * 1000))
        return SendResult(timestamp=ts, recipient=group_id, success=True)

    async def set_typing(self, recipient: str, stop: bool = False) -> None:
        action = "STOPPED" if stop else "STARTED"
        await self._rpc("sendTyping", {"recipient": [recipient], "action": action})

    async def react_to_message(
        self, recipient: str, target_author: str, target_timestamp: int, emoji: str
    ) -> None:
        await self._rpc("sendReaction", {
            "recipient": [recipient],
            "emoji": emoji,
            "targetAuthor": target_author,
            "targetTimestamp": target_timestamp,
        })

    async def receive_messages(self, timeout: int = 5) -> list[Message]:
        """Poll for new messages and persist them to local store."""
        result = await self._rpc("receive", {"timeout": timeout})
        messages = []
        for envelope in result if isinstance(result, list) else []:
            msg = self._parse_envelope(envelope)
            if msg:
                _store.save_message(msg)
                messages.append(msg)
        return messages

    def _parse_envelope(self, envelope: dict) -> Message | None:
        data = envelope.get("envelope", envelope)
        data_message = data.get("dataMessage")
        if not data_message:
            return None

        attachments = []
        for att in data_message.get("attachments", []):
            local_path = att.get("filename")
            if local_path:
                dest = ensure_attachment_dir() / Path(local_path).name
                try:
                    import shutil
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

        ts_ms = data_message.get("timestamp", 0)
        return Message(
            id=str(ts_ms),
            sender=data.get("source", "") or data.get("sourceNumber", ""),
            body=data_message.get("message", "") or "",
            timestamp=datetime.fromtimestamp(ts_ms / 1000),
            attachments=attachments,
            group_id=data_message.get("groupInfo", {}).get("groupId"),
        )

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

    # ── History & Search ──────────────────────────────────────────────────────

    async def get_conversation(self, recipient: str, limit: int = 50) -> list[Message]:
        """Get message history from local store.
        Note: messages are stored as they arrive via receive_messages().
        Run 'signal-mcp receive --watch' to continuously populate history.
        """
        return _store.get_conversation(recipient, limit=limit)

    async def search_messages(self, query: str, limit: int = 50) -> list[Message]:
        """Full-text search across all stored messages."""
        return _store.search_messages(query, limit=limit)

    def list_conversations(self) -> list[dict]:
        """Return all distinct conversations from local store, newest first."""
        return _store.list_conversations(own_number=self.account)

    # ── Message actions ───────────────────────────────────────────────────────

    async def delete_message(self, recipient: str, target_timestamp: int) -> None:
        """Remote-delete (unsend) a message."""
        await self._rpc("remoteDelete", {
            "recipient": [recipient],
            "targetTimestamp": target_timestamp,
        })

    async def delete_group_message(self, group_id: str, target_timestamp: int) -> None:
        """Remote-delete a message from a group."""
        await self._rpc("remoteDelete", {
            "groupId": group_id,
            "targetTimestamp": target_timestamp,
        })

    async def send_read_receipt(self, sender: str, timestamps: list[int]) -> None:
        """Mark one or more messages as read."""
        await self._rpc("sendReadReceipt", {
            "recipient": sender,
            "targetTimestamps": timestamps,
        })

    async def update_contact(self, number: str, name: str) -> None:
        """Set a local display name for a contact."""
        await self._rpc("updateContact", {
            "recipient": number,
            "name": name,
        })

    async def leave_group(self, group_id: str) -> None:
        """Leave a Signal group."""
        await self._rpc("quitGroup", {"groupId": group_id})

    # ── Identity / safety numbers ─────────────────────────────────────────────

    async def list_identities(self, number: str | None = None) -> list[dict]:
        """List identity keys and trust levels. Pass number to filter to one contact."""
        params = {"recipient": number} if number else {}
        result = await self._rpc("listIdentities", params or None)
        return result if isinstance(result, list) else [result] if result else []

    async def trust_identity(self, number: str, trust_all_known: bool = False, safety_number: str | None = None) -> None:
        """Trust a contact's identity key (after verifying safety number)."""
        params: dict = {"recipient": number}
        if safety_number:
            params["verifiedSafetyNumber"] = safety_number
        else:
            params["trustAllKnownKeys"] = trust_all_known
        await self._rpc("trust", params)

