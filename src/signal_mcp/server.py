"""MCP server exposing all Signal tools to Claude."""

import asyncio
import json
import os
from datetime import datetime

from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    RequestParams,
    TextContent,
    Tool,
)

from .client import SignalClient, SignalError
from .config import check_signal_cli_version, is_service_installed
from .tool_annotations import annotations_for
from . import __version__, store as _store

app = Server("signal-mcp", version=__version__)

_client: SignalClient | None = None

# Tools that only read/list/search/export existing state — no side effect on the
# Signal account, contacts, groups, messages, files, or configuration.
_READ_ONLY_TOOLS = {
    "list_contacts", "list_groups", "get_conversation", "search_messages",
    "get_profile", "get_own_number", "store_stats", "get_unread",
    "list_conversations", "get_user_status", "list_identities", "export_messages",
    "list_sticker_packs", "list_attachments", "get_attachment", "get_sticker",
    "list_accounts", "get_webhook", "find_contact", "list_scheduled_messages",
    "list_devices", "get_avatar", "receive_messages", "receive_direct",
}

_READONLY = os.environ.get("SIGNAL_MCP_READONLY", "").lower() in ("1", "true", "yes")

# Tools that don't need the signal-cli daemon (read from local store only)
_DAEMON_FREE = {
    "import_desktop", "sync_desktop", "store_stats",
    "get_conversation", "search_messages", "get_own_number",
    "list_attachments", "get_attachment",
    "clear_local_store", "delete_local_messages", "export_messages",
    "prune_store", "mark_as_unread",
    "list_scheduled_messages", "cancel_scheduled_message", "schedule_message",
    "set_webhook", "get_webhook",
}
# Tools NOT in _DAEMON_FREE call ensure_daemon() automatically before executing.
# get_unread calls _freshen_store() (which may call receive_messages) if no
# background service is running.
# list_accounts, list_conversations, update_configuration etc. call signal-cli JSON-RPC.


def get_client() -> SignalClient:
    global _client
    if _client is None:
        _client = SignalClient()
    return _client


def _ok(data) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, indent=2, default=str))])


def _err(msg: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=f"Error: {msg}")], is_error=True)


def _require(arguments: dict, *keys: str) -> str | None:
    """Return an error string if any required key is missing, else None."""
    missing = [k for k in keys if k not in arguments]
    if missing:
        return f"Missing required parameter(s): {', '.join(missing)}"
    return None


def _paging(arguments: dict, default_limit: int = 50, max_limit: int = 500) -> tuple[int, int]:
    """Clamp limit/offset from tool arguments. A negative SQLite LIMIT means
    "no limit", so an unclamped value here would let a bad limit remove the
    cap entirely and return the whole store in one response."""
    limit = max(1, min(int(arguments.get("limit", default_limit)), max_limit))
    offset = max(0, int(arguments.get("offset", 0)))
    return limit, offset


# ── Tool definitions ───────────────────────────────────────────────────────────

_QUOTE_PROPS = {
    "quote_author": {"type": "string", "description": "Phone number (E.164) of the author of the message being quoted/replied to"},
    "quote_timestamp": {"type": "integer", "description": "Timestamp of the message being quoted/replied to (from get_conversation)"},
    "quote_message": {"type": "string", "description": "Text of the quoted message shown in the quote bubble. Default: looked up in the local store by quote_author + quote_timestamp"},
    "quote_mentions": {
        "type": "array",
        "description": "@mentions inside the quoted text, same shape as mentions: {start, length, author}",
        "items": {"type": "object", "properties": {
            "start": {"type": "integer"}, "length": {"type": "integer"}, "author": {"type": "string"},
        }},
    },
    "quote_text_styles": {"type": "array", "items": {"type": "string"}, "description": "Styles inside the quoted text as 'start:length:STYLE' (BOLD, ITALIC, SPOILER, STRIKETHROUGH, MONOSPACE)"},
    "quote_attachments": {"type": "array", "items": {"type": "string"}, "description": "Attachments of the quoted message as 'contentType[:filename[:previewFile]]', e.g. 'image/png:photo.png'"},
}
_PREVIEW_PROPS = {
    "preview_url": {"type": "string", "description": "URL for a link preview card; the same URL must also appear in the message text"},
    "preview_title": {"type": "string", "description": "Link preview title (needed for the card to render)"},
    "preview_description": {"type": "string", "description": "Link preview description"},
    "preview_image": {"type": "string", "description": "Local image file for the link preview thumbnail"},
}
_STORY_REPLY_PROPS = {
    "story_author": {"type": "string", "description": "Phone number of the story's author, to reply to a story"},
    "story_timestamp": {"type": "integer", "description": "Timestamp of the story being replied to"},
}
_DELIVERY_PROPS = {
    "no_urgent": {"type": "boolean", "description": "Send without the urgent flag, so the recipient gets no push notification", "default": False},
    "notify_self": {"type": "boolean", "description": "If you are among the recipients, deliver as a normal (notifying) message instead of a silent sync message", "default": False},
}
_VOICE_NOTE_PROPS = {
    "voice_note": {"type": "boolean", "description": "Mark audio attachments as voice notes (played inline in Signal)", "default": False},
}
_SEND_OPTION_KEYS = tuple({
    **_QUOTE_PROPS, **_PREVIEW_PROPS, **_STORY_REPLY_PROPS, **_DELIVERY_PROPS, **_VOICE_NOTE_PROPS,
})


def _send_options(arguments: dict) -> dict:
    return {k: arguments[k] for k in _SEND_OPTION_KEYS if k in arguments}


TOOLS = [
    Tool(
        name="send_message",
        description=(
            'Send a text message to one Signal contact (end-to-end encrypted). Use send_group_message for groups, '
            'send_attachment for files, send_note_to_self for your own Note to Self, schedule_message to send later. '
            'Address the contact by recipient (E.164, e.g. +4915112345678) or username (alice.42 or a username link) — '
            'exactly one, otherwise an error. message is the text; formatting=true turns **bold**, *italic*, '
            '~~strikethrough~~, `monospace` and ||spoiler|| into real Signal formatting (markers removed; leave off for '
            'literal asterisks/backticks). Reply/quote: quote_author (E.164) + quote_timestamp (ms) of the original; '
            'quote_message (quoted text; default: looked up in the local store), quote_mentions ({start, length, '
            "author}), quote_text_styles ('start:length:STYLE') and quote_attachments "
            "('contentType[:filename[:previewFile]]') only shape the quote bubble. Link preview card: preview_url (must "
            'also appear in the text), preview_title (needed for the card to render), preview_description, preview_image '
            '(local file). Story reply: story_author (E.164, required with story_timestamp) + story_timestamp (ms). '
            'no_urgent=true sends without a push notification; notify_self=true delivers a normal notifying message if '
            'you are among the recipients. end_session=true instead resets the encrypted session (message ignored; '
            "troubleshooting only). Contacts Signal's servers; not idempotent (repeating sends a duplicate); sends share "
            'a 20-per-minute rate limit (calls wait rather than fail). The sent message is saved to the local store. '
            'Returns {status, timestamp, recipient}; timestamp is the target_timestamp for edit_message, react_to_message'
            ' or delete_message.'
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Phone number in E.164 format (e.g. +1234567890)"},
                "username": {"type": "string", "description": "Signal username (e.g. alice.42) or username link, instead of recipient"},
                "message": {"type": "string", "description": "Message text to send"},
                "formatting": {"type": "boolean", "description": "Convert **bold**, *italic*, ~~strikethrough~~, `monospace`, ||spoiler|| to Signal text formatting. Default false."},
                **_QUOTE_PROPS,
                **_PREVIEW_PROPS,
                **_STORY_REPLY_PROPS,
                **_DELIVERY_PROPS,
                "end_session": {"type": "boolean", "description": "Reset the session with this contact instead of sending a message", "default": False},
            },
            "required": ["message"],
        },
    ),
    Tool(
        name="send_group_message",
        description=(
            'Send a text message to a Signal group (end-to-end encrypted to all members). Use send_message for a single '
            'contact, send_group_attachment for files. group_id comes from list_groups. message is the text. @mentions: '
            'put the name in the text and pass mentions [{start, length, author (E.164)}]; start/length are UTF-16 code '
            'units, not codepoints — an emoji before the mention shifts the offset by 2, not 1. formatting=true turns '
            '**bold**, *italic*, ~~strikethrough~~, `monospace` and ||spoiler|| into real Signal formatting (markers are '
            'removed from the sent text); with it, mention offsets refer to the text as you wrote it, markers included, '
            'and are adjusted for you. Leave it off for text with literal asterisks or backticks. Reply/quote: '
            'quote_author (E.164) + quote_timestamp (ms) of the original; quote_message (quoted text; default: looked up '
            "in the local store), quote_mentions ({start, length, author}), quote_text_styles ('start:length:STYLE') and "
            "quote_attachments ('contentType[:filename[:previewFile]]') only shape the quote bubble. Link preview card: "
            'preview_url (must also appear in the text), preview_title (needed for the card to render), '
            'preview_description, preview_image (local file). Story reply: story_author (E.164, required with '
            'story_timestamp) + story_timestamp (ms). no_urgent=true sends without a push notification; notify_self=true '
            "delivers a normal notifying message if you are among the recipients. Contacts Signal's servers; not "
            'idempotent (repeating sends a duplicate); sends share a 20-per-minute rate limit (calls wait rather than '
            'fail). The sent message is saved to the local store. Returns {status, timestamp, group_id}; timestamp is the'
            ' target_timestamp for edit_message, react_to_message or delete_group_message.'
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "Group ID (from list_groups)"},
                "message": {"type": "string", "description": "Message text to send"},
                "formatting": {"type": "boolean", "description": "Convert **bold**, *italic*, ~~strikethrough~~, `monospace`, ||spoiler|| to Signal text formatting. Default false."},
                "mentions": {
                    "type": "array",
                    "description": "List of @mentions. Each item: {start: offset of the mention in the message (UTF-16 code units, not codepoints), length: mention length (UTF-16 code units), author: E.164 phone number of the mentioned member}. Example: message='Hello @Alice', mentions=[{start:6,length:6,author:'+1234567890'}]",
                    "items": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "integer", "description": "Character offset of the mention in the message text"},
                            "length": {"type": "integer", "description": "Length of the mention text in characters"},
                            "author": {"type": "string", "description": "E.164 phone number of the mentioned group member"},
                        },
                    },
                },
                **_QUOTE_PROPS,
                **_PREVIEW_PROPS,
                **_STORY_REPLY_PROPS,
                **_DELIVERY_PROPS,
            },
            "required": ["group_id", "message"],
        },
    ),
    Tool(
        name="send_note_to_self",
        description=(
            'Send a message to your own Note to Self chat; it syncs to all your linked Signal devices. Use for reminders,'
            ' bookmarks or drafts; use send_message to message anyone else. message always supports **bold**, *italic*, '
            '~~strikethrough~~, `monospace`, ||spoiler|| (markers become Signal formatting) — e.g. a bold title per note.'
            ' attachments: list of local file paths (e.g. a QR code); voice_note=true marks audio as a voice note. To '
            'thread a follow-up under an earlier note pass quote_author (your own number) + quote_timestamp (from a prior'
            ' send_note_to_self result); quote_message, quote_mentions, quote_text_styles, quote_attachments optionally '
            'shape the quote bubble. Link preview card: preview_url (must also appear in the text), preview_title (needed'
            ' for the card to render), preview_description, preview_image (local file). no_urgent=true sends without a '
            'push notification; notify_self=true delivers a normal notifying message if you are among the recipients. '
            'Combine content in one call rather than several. Not idempotent; shares the 20-sends-per-minute rate limit. '
            'Saved to the local store. Returns {status, timestamp}.'
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Note text to save. Supports **bold**, *italic*, ~~strikethrough~~, `monospace`, ||spoiler||"},
                "attachments": {
                    "type": "array",
                    "description": "File paths to attach (e.g. a QR code or screenshot)",
                    "items": {"type": "string"},
                },
                "quote_author": {"type": "string", "description": "Your own account number, to thread this note under a previous one"},
                "quote_timestamp": {"type": "integer", "description": "Timestamp of the note being followed up on (from a prior send_note_to_self result)"},
                **_PREVIEW_PROPS,
                **_DELIVERY_PROPS,
                **_VOICE_NOTE_PROPS,
            },
            "required": ["message"],
        },
    ),
    Tool(
        name="edit_message",
        description=(
            "Replace the text of a message you already sent (DM or group); recipients see the new text with an '(edited)'"
            ' label. Use to fix typos or update information; use delete_message / delete_group_message to retract it '
            'instead, and send a new message to reach different people. Only the text changes — attachments, quotes and '
            'reactions stay. target_timestamp: ms timestamp of the original (from the send_* result or the message id in '
            'get_conversation). message: the new full text (no formatting conversion). Give recipient (E.164) for a DM or'
            ' group_id (from list_groups) for a group; neither is an error. Signal only accepts edits of your own '
            "messages. Contacts Signal's servers and overwrites the stored body locally (the old text is not kept); "
            'repeating the same edit has no further effect. Returns {status, target_timestamp}.'
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_timestamp": {"type": "integer", "description": "Timestamp of the message to edit (from get_conversation or send_message response)"},
                "message": {"type": "string", "description": "New message text to replace the original"},
                "recipient": {"type": "string", "description": "Phone number for a DM message edit"},
                "group_id": {"type": "string", "description": "Group ID for a group message edit"},
            },
            "required": ["target_timestamp", "message"],
        },
    ),
    Tool(
        name="receive_messages",
        description=(
            'Poll the signal-cli daemon once for newly arrived messages and save them to the local store. Normally use '
            'get_unread instead — it polls when needed and returns unread messages in one call; use receive_direct only '
            'if the daemon is stuck. timeout: seconds to wait (integer, default 5); max_messages: stop after this many '
            '(default: no limit). Incoming edits and remote deletes are applied to stored messages instead of being '
            'returned; receipts are returned but not stored. Does not mark anything read. Returns a list of messages (id,'
            ' sender, sender_name, recipient, group_id, group_name, body, timestamp, attachments, quote_id, reactions, '
            'is_read). If the background service already holds the receive lock, returns {note, messages} with up to 50 '
            'unread messages from the store instead.'
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "timeout": {"type": "integer", "description": "Seconds to wait for messages (default: 5)", "default": 5},
                "max_messages": {"type": "integer", "description": "Return after this many messages (default: no limit)"},
            },
        },
    ),
    Tool(
        name="receive_direct",
        description=(
            'Troubleshooting fallback: receive messages by running signal-cli receive directly, bypassing the daemon. Use'
            ' only when receive_messages / get_unread fail because the daemon is stuck; it stops the daemon (holding a '
            'lock file meanwhile), runs the receive, and the daemon restarts on the next call. timeout: seconds to wait '
            '(default 5); max_messages: stop after this many (default: no limit); ignore_attachments, ignore_stories, '
            'ignore_avatars, ignore_stickers (all default false) skip downloading those. Received messages are saved to '
            'the local store and remote deletes applied; nothing is marked read. Returns a list of messages in the same '
            'shape as receive_messages; errors if signal-cli exits non-zero.'
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "timeout": {"type": "integer", "description": "Seconds to wait for messages (default: 5)", "default": 5},
                "max_messages": {"type": "integer", "description": "Return after this many messages (default: no limit)"},
                "ignore_attachments": {"type": "boolean", "description": "Don't download attachments", "default": False},
                "ignore_stories": {"type": "boolean", "description": "Don't receive story messages", "default": False},
                "ignore_avatars": {"type": "boolean", "description": "Don't download avatars", "default": False},
                "ignore_stickers": {"type": "boolean", "description": "Don't download sticker packs", "default": False},
            },
        },
    ),
    Tool(
        name="list_contacts",
        description=(
            "List the contacts in signal-cli's local contact store (no network call). Use it to browse or audit contacts; "
            "to look up one person's number by name, find_contact is the shorter call. Parameters: search (optional) keeps "
            "only contacts whose number, name, given/family name, nickname or username contains it (case-insensitive); "
            "all_recipients (default false) also includes people not in your address book, e.g. members of your groups; "
            "blocked=true returns only blocked contacts, false only unblocked, omitted all. Returns a list of objects with "
            "number (E.164), uuid, name, given_name, family_name, about, blocked, display_name, plus username, nick_name, "
            "note, has_avatar, message_expiration_time etc. when set."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Filter contacts by name or number (case-insensitive substring match)"},
                "all_recipients": {"type": "boolean", "description": "Also include recipients that are not in your address book (e.g. members of your groups), with their profile names", "default": False},
                "blocked": {"type": "boolean", "description": "true = only blocked contacts, false = only unblocked (omit for all)"},
            },
        },
    ),
    Tool(
        name="list_groups",
        description=(
            "List the Signal groups this account knows, from signal-cli's local store (no network call). Call it first to "
            "get the group_id that send_group_message, send_group_attachment, update_group, leave_group, terminate_group "
            "and set_expiration_timer require, or to check your admin status. group_id (optional) returns only that group. "
            "Each group has id, name, description, member_count, members (uuid, number, is_admin, and label/label_emoji — "
            "the tag shown next to the name — when set), is_blocked, is_member and invite_link; when non-empty also "
            "pending_members (invited), requesting_members (awaiting approval), banned, "
            "permission_add_member/permission_edit_details/permission_send_message (EVERY_MEMBER or ONLY_ADMINS), "
            "message_expiration_time (seconds) and is_terminated."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "Optional: return only this group"},
            },
        },
    ),
    Tool(
        name="get_conversation",
        description=(
            'Read the message history of one conversation from the local store (no Signal server call). Use '
            'list_conversations to find conversations, search_messages to find text across all chats, get_unread for only'
            ' new messages. recipient: E.164 number for a DM or a group_id (from list_groups). limit: max messages '
            '(default 50, clamped 1-500); offset: skip the newest N for paging back (default 0); since: only messages at '
            'or after this ISO datetime (e.g. 2024-01-01T00:00:00; invalid values return an error). Side effect: incoming'
            ' messages returned are marked read in the local store only — no read receipt is sent (use '
            'send_read_receipt). Returns {messages (oldest first; id, sender, sender_name, body, timestamp, attachments, '
            "quote_id, reactions, is_read …), total, has_more, limit, offset}. Timestamps are milliseconds: a message's "
            'id in get_conversation is its timestamp (sent messages: sent_<ms>_<recipient or group_id>). '
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "E.164 phone number for a DM, or group ID (from list_groups)"},
                "limit": {"type": "integer", "description": "Max messages to return (default 50, clamped 1-500)", "default": 50},
                "offset": {"type": "integer", "description": "Number of newest messages to skip for pagination (default: 0)", "default": 0},
                "since": {"type": "string", "description": "Only messages after this ISO datetime (e.g. 2024-01-01T00:00:00)"},
            },
            "required": ["recipient"],
        },
    ),
    Tool(
        name="search_messages",
        description=(
            'Full-text search of message bodies across all conversations in the local store (SQLite FTS; no Signal server'
            ' call, nothing marked read). Only messages stored on this device are found. Use get_conversation to read a '
            'chat in order. query: words to find (all words must occur; falls back to substring match if FTS fails; empty'
            ' returns []). sender: only messages from this E.164 number. since (inclusive) / until (exclusive) ISO dates,'
            ' e.g. since=2024-01-01, until=2024-01-02 covers all of Jan 1; invalid dates return an error. limit: max '
            'results (default 50, clamped 1-500); offset: skip N results for paging (default 0). Returns a list of '
            'messages, newest first (id, sender, sender_name, recipient, group_id, group_name, body, timestamp, '
            'attachments …).'
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword or phrase to search for"},
                "sender": {"type": "string", "description": "Filter results to messages from this phone number (E.164)"},
                "since": {"type": "string", "description": "Only messages at or after this ISO datetime (e.g. 2024-01-01 or 2024-01-01T09:00:00)"},
                "until": {"type": "string", "description": "Only messages strictly before this ISO datetime (exclusive; until=2024-01-02 includes all of Jan 1)"},
                "limit": {"type": "integer", "description": "Maximum results to return (default 50, clamped 1-500)"},
                "offset": {"type": "integer", "description": "Skip this many results for pagination (default 0)", "default": 0},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="send_attachment",
        description=(
            'Send one or more files (photos, videos, documents, audio) to one Signal contact in a single message. Use '
            'send_group_attachment for groups, send_message for text only, send_sticker for stickers. Address by '
            'recipient (E.164) or username (alice.42 or username link) — exactly one. path: a single file; paths: several'
            ' files sent together (one of the two is required). Files must lie inside the allowed send folders (default: '
            'the signal-mcp attachments folder, ~/Downloads, ~/Desktop, ~/Documents; override with SIGNAL_MCP_SEND_ROOTS)'
            ' and not be hidden, else an error is returned. caption: text shown with the files (default empty); '
            'view_once=true lets the recipient open media only once; voice_note=true marks audio as a voice note. '
            'Reply/quote: quote_author (E.164) + quote_timestamp (ms) of the original; quote_message (quoted text; '
            'default: looked up in the local store), quote_mentions ({start, length, author}), quote_text_styles '
            "('start:length:STYLE') and quote_attachments ('contentType[:filename[:previewFile]]') only shape the quote "
            'bubble. no_urgent=true sends without a push notification; notify_self=true delivers a normal notifying '
            "message if you are among the recipients. Contacts Signal's servers; not idempotent (repeating sends a "
            'duplicate); sends share a 20-per-minute rate limit (calls wait rather than fail). Saved to the local store '
            '(caption as body). Returns {status, timestamp}.'
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Phone number in E.164 format"},
                "username": {"type": "string", "description": "Signal username (e.g. alice.42) or username link, instead of recipient"},
                "path": {"type": "string", "description": "Single file path (absolute, relative, or ~/path)"},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Multiple file paths to send as one message"},
                "caption": {"type": "string", "description": "Optional caption text shown below the attachment", "default": ""},
                "view_once": {"type": "boolean", "description": "Send as view-once media — recipient can only view it once before it disappears", "default": False},
                **_VOICE_NOTE_PROPS,
                **_QUOTE_PROPS,
                **_DELIVERY_PROPS,
            },
        },
    ),
    Tool(
        name="send_group_attachment",
        description=(
            'Send one or more files (photos, videos, documents, audio) to a Signal group in a single message. Use '
            'send_attachment for a single contact, send_group_message for text only. group_id comes from list_groups. '
            'path: a single file; paths: several files sent together (one of the two is required). Files must lie inside '
            'the allowed send folders (default: the signal-mcp attachments folder, ~/Downloads, ~/Desktop, ~/Documents; '
            'override with SIGNAL_MCP_SEND_ROOTS) and not be hidden, else an error is returned. caption: text shown with '
            'the files (default empty); view_once=true lets each member open media only once; voice_note=true marks audio'
            ' as a voice note. Reply/quote: quote_author (E.164) + quote_timestamp (ms) of the original; quote_message '
            '(quoted text; default: looked up in the local store), quote_mentions ({start, length, author}), '
            "quote_text_styles ('start:length:STYLE') and quote_attachments ('contentType[:filename[:previewFile]]') only"
            ' shape the quote bubble. no_urgent=true sends without a push notification; notify_self=true delivers a '
            "normal notifying message if you are among the recipients. Contacts Signal's servers; not idempotent "
            '(repeating sends a duplicate); sends share a 20-per-minute rate limit (calls wait rather than fail). Saved '
            'to the local store (caption as body). Returns {status, timestamp}.'
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "Group ID (get from list_groups)"},
                "path": {"type": "string", "description": "Single file path (absolute, relative, or ~/path)"},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Multiple file paths to send as one message"},
                "caption": {"type": "string", "description": "Optional caption text shown below the attachment", "default": ""},
                "view_once": {"type": "boolean", "description": "Send as view-once media — each recipient can only view it once", "default": False},
                **_VOICE_NOTE_PROPS,
                **_QUOTE_PROPS,
                **_DELIVERY_PROPS,
            },
            "required": ["group_id"],
        },
    ),
    Tool(
        name="react_to_message",
        description=(
            'Add or remove your emoji reaction on a message in a DM or group. Use to acknowledge without replying; use '
            "send_message / send_group_message for a text reply. target_author: E.164 number of the message's sender; "
            "target_timestamp: its ms timestamp (message id in get_conversation, or a send_* result). emoji: e.g. '👍'. "
            'Give recipient (E.164) for a DM or group_id for a group; neither is an error. You have at most one reaction '
            'per message: a new emoji replaces the old one, repeating the same one changes nothing. remove=true retracts '
            "that reaction (emoji still required; default false). Contacts Signal's servers; not stored locally. Returns "
            "{status: 'reaction sent' | 'reaction removed'}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_author": {"type": "string", "description": "Phone number of the message author"},
                "target_timestamp": {"type": "integer", "description": "Timestamp of the message to react to"},
                "emoji": {"type": "string", "description": "Emoji to react with (e.g. '👍')"},
                "recipient": {"type": "string", "description": "Phone number for DM reactions"},
                "group_id": {"type": "string", "description": "Group ID for group reactions"},
                "remove": {"type": "boolean", "description": "Remove an existing reaction (default false)", "default": False},
            },
            "required": ["target_author", "target_timestamp", "emoji"],
        },
    ),
    Tool(
        name="set_typing",
        description=(
            "Show or cancel the 'typing…' indicator in a DM or group. Purely cosmetic; use before send_message / "
            'send_group_message in an automated reply. recipient: E.164 number for a DM; group_id for a group (from '
            'list_groups); at least one is required. stop=true cancels an active indicator early (default false = start).'
            ' The indicator expires by itself after ~15 seconds and is cleared when you send, so stop is rarely needed; '
            "one call per message is enough — don't loop. If the recipient disabled typing indicators it is silently "
            "ignored. Contacts Signal's servers; nothing is stored. Returns {status: 'typing indicator sent'}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Phone number in E.164 format"},
                "group_id": {"type": "string", "description": "Group ID (base64) to show typing in a group"},
                "stop": {"type": "boolean", "description": "Set to true to cancel an active typing indicator (default: false = start typing)", "default": False},
            },
        },
    ),
    Tool(
        name="get_profile",
        description=(
            "Check one phone number against Signal's servers and return what that lookup provides. number (required, "
            "E.164). Returns a contact object whose number and uuid (the Signal account id; null if the number is not "
            "registered) are filled; the lookup carries no profile data, so name, given_name, family_name and about come "
            "back null. For a contact's profile name and about text use find_contact or list_contacts with "
            "all_recipients=true; for their photo use get_avatar; to check many numbers at once use get_user_status. To "
            "change your own profile use update_profile."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "Phone number in E.164 format"},
            },
            "required": ["number"],
        },
    ),
    Tool(
        name="block_contact",
        description=(
            "Block a contact so you stop receiving their messages and calls. number (required, E.164). Only works when "
            "signal-mcp is the account's primary device — fails with 'This command doesn't work on linked devices' if "
            "signal-mcp was set up via signal-cli link. The contact is not notified. The blocked list is synced to your "
            "linked devices, and if you share no group with them your profile key is rotated so they lose access to your "
            "profile updates. Message history is kept. Blocking an already blocked contact is a no-op. Reversible with "
            "unblock_contact. To only delete the local contact entry without blocking, use remove_contact. Returns status "
            "'blocked' and number."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "Phone number to block (E.164 format, e.g. +1234567890)"},
            },
            "required": ["number"],
        },
    ),
    Tool(
        name="unblock_contact",
        description=(
            "Unblock a previously blocked contact so their messages and calls reach you again. number (required, E.164). "
            "Only works when signal-mcp is the account's primary device — fails with 'This command doesn't work on linked "
            "devices' if signal-mcp was set up via signal-cli link. The contact is not notified; unblocking also accepts "
            "their message request (your profile is shared with them again) and syncs to your linked devices. Unblocking a "
            "contact that is not blocked is a no-op. Use list_contacts with blocked=true to see who is blocked, "
            "block_contact to block again. Returns status 'unblocked' and number."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "Phone number to unblock (E.164 format)"},
            },
            "required": ["number"],
        },
    ),
    Tool(
        name="remove_contact",
        description=(
            "Delete a contact's entry (name, nickname, note) from your contact list; synced to your linked devices. It does"
            " not block them, delete messages or stop them messaging you — use block_contact for that. number (required, "
            "E.164). hide (default false): only hide the contact from the list and keep its data. forget (default false): "
            "delete ALL data for this recipient, including identity keys and sessions — not reversible. hide and forget are"
            " mutually exclusive (error if both). Returns status 'removed' and number. To rename instead, use "
            "update_contact."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "Phone number to remove (E.164 format)"},
                "hide": {"type": "boolean", "description": "Hide the contact but keep its data", "default": False},
                "forget": {"type": "boolean", "description": "Delete all data for this recipient, including identity keys and sessions", "default": False},
            },
            "required": ["number"],
        },
    ),
    Tool(
        name="update_profile",
        description=(
            "Change your own Signal profile, which is uploaded to Signal's servers and seen by people you share your "
            "profile with. Only the fields you pass change. name: display name (alias of given_name; given_name wins if "
            "both are set). given_name / family_name: the two parts of your profile name. about: bio text; about_emoji: "
            "emoji shown next to it. mobilecoin_address: base64-encoded MobileCoin public address. avatar_path: local image"
            " file (JPEG or PNG) inside the allowed send folders (SIGNAL_MCP_SEND_ROOTS), no hidden paths. remove_avatar "
            "(default false): clear the current photo. Returns status 'profile updated'. To label someone else locally use "
            "update_contact; to rename a linked device use update_device; for account settings use update_account."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Display (given) name to set; alias of given_name"},
                "given_name": {"type": "string", "description": "Profile given name"},
                "family_name": {"type": "string", "description": "Profile family name"},
                "about": {"type": "string", "description": "About/bio text"},
                "about_emoji": {"type": "string", "description": "Emoji shown next to the about text"},
                "mobilecoin_address": {"type": "string", "description": "MobileCoin address (base64)"},
                "avatar_path": {"type": "string", "description": "Local JPEG/PNG path inside the allowed send folders (SIGNAL_MCP_SEND_ROOTS)"},
                "remove_avatar": {"type": "boolean", "description": "Remove current avatar", "default": False},
            },
        },
    ),
    Tool(
        name="create_group",
        description=(
            "Create a new Signal group with you as admin. name (required) is the group name shown to everyone; members "
            "(required) is a list of E.164 phone numbers to add; description (optional) is the group info text; avatar "
            "(optional) is a local image path, which must be inside the allowed send folders (SIGNAL_MCP_SEND_ROOTS). "
            "Contacts Signal's servers and notifies every member; not idempotent — calling twice creates two groups. "
            "Returns status, groupId (base64; use it as group_id elsewhere), timestamp and per-member send results. Use "
            "update_group to change an existing group and send_group_message to post in it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Group name visible to all members"},
                "members": {"type": "array", "items": {"type": "string"}, "description": "Phone numbers (E.164) of initial members to invite"},
                "description": {"type": "string", "description": "Optional group description shown in group info"},
                "avatar": {"type": "string", "description": "Optional local image file path for the group avatar"},
            },
            "required": ["name", "members"],
        },
    ),
    Tool(
        name="join_group",
        description=(
            "Join a Signal group from an invite link. uri (required) is the link, https://signal.group/#... Use this for "
            "groups you are not in; for groups you already belong to use list_groups. Contacts Signal's servers and "
            "notifies the group. If the group requires admin approval you become a requesting member and the result has "
            "onlyRequested=true (or the call fails with 'Pending admin approval'); an invalid or reset link fails with "
            "'Group link is invalid'. Returns status, groupId, timestamp and send results; use the groupId as group_id for "
            "send_group_message."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Group invite link starting with https://signal.group/#"},
            },
            "required": ["uri"],
        },
    ),
    Tool(
        name="list_devices",
        description=(
            "List all devices linked to your Signal account, queried from Signal's servers. Takes no parameters. Returns "
            "entries with id, name, createdTimestamp and lastSeenTimestamp (epoch ms); device id 1 is the primary phone. "
            "Use it to audit which devices have access, or to get the device id for update_device (rename) or remove_device"
            " (unlink)."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="add_device",
        description=(
            "Link a new device (e.g. Signal Desktop or another signal-cli) to your account. uri (required): the device-link"
            " URI (sgnl://linkdevice?...) shown by the new device as a QR code or printed by 'signal-cli link'. Only works "
            "when signal-mcp is the account's primary device — fails with 'This command doesn't work on linked devices' if "
            "signal-mcp was set up via signal-cli link. Also fails on a malformed or expired link or when the account "
            "already has the maximum number of linked devices. The linked device gets full access to the account; never use"
            " a URI from an untrusted source. Returns status 'device linked'; confirm with list_devices, undo with "
            "remove_device."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Device link URI (sgnl://linkdevice?...) from the new device's QR code or signal-cli link output"},
            },
            "required": ["uri"],
        },
    ),
    Tool(
        name="remove_device",
        description=(
            "Permanently unlink a device from your Signal account; it immediately stops sending and receiving and can only "
            "come back by linking again with add_device. device_id (required, integer from list_devices) must be a linked "
            "device, not 1 (the primary). Only works when signal-mcp is the account's primary device — fails with 'This "
            "command doesn't work on linked devices' if signal-mcp was set up via signal-cli link. Returns status 'device "
            "removed' and device_id. To only rename a device, use update_device."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "device_id": {"type": "integer", "description": "Device ID (get from list_devices)"},
            },
            "required": ["device_id"],
        },
    ),
    Tool(
        name="get_own_number",
        description="Get your own Signal phone number (the account this server is running as)",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="store_stats",
        description="Get statistics about locally stored messages (count, unread count, DB size on disk, date range)",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_unread",
        description=(
            'Return new unread incoming messages across all conversations — the default way to check for new messages. If'
            ' the background service (signal-mcp install-service) is running it reads the local store; otherwise it first'
            ' polls signal-cli (at most once per 30 s) and adds a _warning suggesting the service. Use get_conversation '
            "for a chat's full history, list_conversations for an inbox overview. limit: max messages (default 50, "
            'clamped 1-500); the newest are kept. Side effect: returned messages are marked read in the local store only '
            '(no read receipt — use send_read_receipt; mark_as_unread to undo). Returns {messages (oldest first; id, '
            'sender, sender_name, group_id, group_name, body, timestamp, attachments …), has_more}; if has_more is true '
            'call again with the same limit to get the next batch.'
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max messages to return (default: 50)", "default": 50},
            },
        },
    ),
    Tool(
        name="import_desktop",
        description="Full one-time import of all historical messages from Signal Desktop (macOS/Linux). Requires sqlcipher. On macOS prompts for Keychain access; on Linux uses libsecret/GNOME Keyring. For ongoing sync use sync_desktop instead.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="sync_desktop",
        description="Incremental sync from Signal Desktop: imports only messages newer than the last sync. Fast on repeat calls. On first call behaves like import_desktop (imports everything). Requires sqlcipher.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_conversations",
        description=(
            'List every conversation (direct and group) in the local store, most recent first — an inbox overview. Takes '
            'no parameters. Use get_conversation to read one, get_unread for only new messages, search_messages to find '
            'text. Reads only locally stored messages (no Signal server call; nothing marked read); names come from the '
            'cached signal-cli contacts and groups. Returns a list of {id (E.164 number or group_id — pass to '
            "get_conversation), type ('direct' | 'group'), name, last_message, last_message_at (ISO), message_count, "
            'unread_count}.'
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_user_status",
        description=(
            "Check whether phone numbers and/or usernames are registered on Signal, in one batch query to Signal's servers."
            " recipients: list of E.164 phone numbers; usernames: list of Signal usernames or username links; at least one "
            "of the two is required. Returns one entry per input with recipient, number or username, uuid (null if not "
            "registered) and isRegistered. Use it before messaging an unknown number; numbers that hide their "
            "discoverability can show as unregistered. For a contact's name or details use find_contact or get_profile."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "recipients": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of phone numbers (E.164) to check",
                },
                "usernames": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of Signal usernames or username links to check",
                },
            },
        },
    ),
    Tool(
        name="send_sync_request",
        description=(
            "Ask your primary Signal device to send this linked device its contacts, groups and settings. Takes no "
            "parameters. Use it when list_contacts or list_groups is missing entries that exist on your phone; it is meant "
            "for linked setups. Asynchronous: the call returns status 'sync requested' at once and the data arrives over "
            "the next seconds through the normal receive loop. It does not fetch new messages — use receive_messages for "
            "that. To push your contacts the other way, use send_contacts_sync."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="delete_message",
        description=(
            'Remote-delete (unsend) a message you sent to one contact, removing it for everyone in the chat on Signal '
            '5.0+ clients. Use delete_group_message for groups; delete_local_messages to remove messages only from the '
            'local store. Only your own messages can be deleted — you cannot delete what others sent. recipient: E.164 '
            'number of the contact; target_timestamp: ms timestamp of your message (from the send_message result or '
            'get_conversation). Irreversible; older clients may silently ignore it; repeating it has no further effect. '
            "The local store copy is kept. Returns {status: 'deleted'}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Phone number of the recipient"},
                "target_timestamp": {"type": "integer", "description": "Timestamp of the message to delete"},
            },
            "required": ["recipient", "target_timestamp"],
        },
    ),
    Tool(
        name="delete_group_message",
        description=(
            'Remote-delete (unsend) a message you sent to a group, removing it for all members on Signal 5.0+ clients. '
            "Use delete_message for DMs; admin_delete_message to delete another member's message as admin; "
            'delete_local_messages for local-only removal. group_id: from list_groups; target_timestamp: ms timestamp of '
            'your message (from the send_group_message result or get_conversation). Irreversible; older clients may '
            'silently ignore it; repeating it has no further effect. The local store copy is kept. Returns {status: '
            "'deleted'}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "Group ID"},
                "target_timestamp": {"type": "integer", "description": "Timestamp of the message to delete"},
            },
            "required": ["group_id", "target_timestamp"],
        },
    ),
    Tool(
        name="send_read_receipt",
        description=(
            "Send a read receipt to a contact so their Signal app shows your messages as 'Read', and mark those messages "
            'read in the local store. Use after reading a DM with get_conversation (which marks read locally but sends no'
            ' receipt). Not for groups. sender: E.164 number of the contact who sent the messages. timestamps: list of '
            "their messages' ms timestamps (the message id in get_conversation), batched in one call. Only shown if the "
            "sender has read receipts enabled. Contacts Signal's servers; repeating is harmless. Returns {status: 'read "
            "receipt sent'}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sender": {"type": "string", "description": "Phone number (E.164) of the contact whose messages you are acknowledging"},
                "timestamps": {"type": "array", "items": {"type": "integer"}, "description": "Millisecond timestamps of the messages to acknowledge (the message id in get_conversation)"},
            },
            "required": ["sender", "timestamps"],
        },
    ),
    Tool(
        name="update_contact",
        description=(
            "Set your private local name, nickname or note for another contact; it is synced to your own linked devices and"
            " never shown to the contact. To change your own public profile use update_profile; to block or delete a "
            "contact use block_contact or remove_contact. number (required, E.164). Pass at least one of: name (full "
            "display name; stored as given name and clears the family name unless family_name is also given), given_name, "
            "family_name, nick_given_name, nick_family_name (nickname shown instead of their profile name), note (private "
            "note). Fails if none is given or the number is not registered on Signal. Returns status, number and name. For "
            "a disappearing-message timer use set_expiration_timer."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "Phone number in E.164 format"},
                "name": {"type": "string", "description": "Display name to set"},
                "given_name": {"type": "string", "description": "Contact given name"},
                "family_name": {"type": "string", "description": "Contact family name"},
                "nick_given_name": {"type": "string", "description": "Nickname given name"},
                "nick_family_name": {"type": "string", "description": "Nickname family name"},
                "note": {"type": "string", "description": "Private note about the contact"},
            },
            "required": ["number"],
        },
    ),
    Tool(
        name="update_group",
        description=(
            "Change an existing group's details, membership, admins, invite link, permissions or timer; only the fields you"
            " pass change. group_id (required, from list_groups). name, description: new text. avatar: local image path "
            "inside the allowed send folders. add_members / remove_members / add_admins / remove_admins / ban_members / "
            "unban_members: lists of E.164 numbers. expiration_seconds: disappearing-message timer, 0 disables. link_mode: "
            "'enabled' (anyone with the link joins), 'enabled-with-approval', 'disabled', or 'reset' (same as "
            "reset_link=true, which issues a new link and invalidates the old one). permission_add_member, "
            "permission_edit_details, permission_send_messages: 'every-member' or 'only-admins' (only-admins sending = "
            "announcement group). member_label / member_label_emoji set ONLY YOUR OWN label in this group (the tag next to "
            "your name); you cannot set another member's label. Changes apply immediately and every member gets a group "
            "update; removals and bans are not undone automatically. Admin rights are needed for most changes, depending on"
            " the group's permissions. Returns status and group_id. Use send_group_message to post, leave_group to exit."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "Group ID to update (get from list_groups)"},
                "name": {"type": "string", "description": "New group name"},
                "description": {"type": "string", "description": "New group description"},
                "add_members": {"type": "array", "items": {"type": "string"}, "description": "Phone numbers (E.164) to add"},
                "remove_members": {"type": "array", "items": {"type": "string"}, "description": "Phone numbers (E.164) to remove"},
                "add_admins": {"type": "array", "items": {"type": "string"}, "description": "Phone numbers (E.164) to promote to admin"},
                "remove_admins": {"type": "array", "items": {"type": "string"}, "description": "Phone numbers (E.164) to demote from admin"},
                "expiration_seconds": {"type": "integer", "description": "Disappearing message timer in seconds (0 to disable)"},
                "link_mode": {"type": "string", "description": "Invite link mode: 'disabled', 'enabled', 'enabled-with-approval', or 'reset' to generate a new link"},
                "reset_link": {"type": "boolean", "description": "Generate a new invite link, invalidating the old one"},
                "avatar": {"type": "string", "description": "Local image file path for the new group avatar (must be inside the allowed send folders)"},
                "ban_members": {"type": "array", "items": {"type": "string"}, "description": "Phone numbers (E.164) to ban from (re)joining the group"},
                "unban_members": {"type": "array", "items": {"type": "string"}, "description": "Phone numbers (E.164) to remove from the ban list"},
                "permission_add_member": {"type": "string", "enum": ["every-member", "only-admins"], "description": "Who may add new members"},
                "permission_edit_details": {"type": "string", "enum": ["every-member", "only-admins"], "description": "Who may edit group name, description, avatar, timer"},
                "permission_send_messages": {"type": "string", "enum": ["every-member", "only-admins"], "description": "Who may send messages ('only-admins' = announcement group)"},
                "member_label": {"type": "string", "description": "YOUR OWN member label in this group (not other members')"},
                "member_label_emoji": {"type": "string", "description": "Emoji for YOUR OWN member label"},
            },
            "required": ["group_id"],
        },
    ),
    Tool(
        name="leave_group",
        description=(
            "Leave a Signal group yourself: sends a quit message to all members and removes you from the member list; the "
            "group continues for everyone else. To end the group for all members use terminate_group instead. group_id "
            "(required, from list_groups). admins: E.164 numbers of members to promote first — if you are the group's ONLY "
            "admin you must name one, otherwise signal-cli refuses. delete (default false): also delete the group's local "
            "data after leaving. You can only come back by being re-added or via an invite link. Returns status and "
            "group_id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "Group ID to leave (get from list_groups)"},
                "admins": {"type": "array", "items": {"type": "string"}, "description": "Members to make admin before leaving — required if you are the only admin"},
                "delete": {"type": "boolean", "description": "Also delete all local group data after leaving"},
            },
            "required": ["group_id"],
        },
    ),
    Tool(
        name="terminate_group",
        description=(
            "DESTRUCTIVE AND IRREVERSIBLE: permanently terminate a Signal group FOR ALL MEMBERS; afterwards nobody can send"
            " messages or start calls in it. Requires admin rights. To just exit the group yourself, use leave_group. "
            "group_id (required, from list_groups). confirm (required) must be true, otherwise nothing happens and an error"
            " is returned; only set it after the user explicitly asked to end the group for everyone. Returns status and "
            "group_id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "Group ID to terminate (get from list_groups)"},
                "confirm": {"type": "boolean", "description": "Must be true to proceed — prevents accidental termination"},
            },
            "required": ["group_id", "confirm"],
        },
    ),
    Tool(
        name="pin_message",
        description=(
            'Pin a message at the top of a DM or group conversation for all participants. Pinning is visible to everyone '
            "— don't use it as a private bookmark (send_note_to_self instead). Use unpin_message to remove a pin. "
            "target_author: E.164 number of the message's sender; target_timestamp: its ms timestamp (message id in "
            'get_conversation). Give recipient (E.164) for a DM or group_id for a group; neither is an error. Contacts '
            "Signal's servers; pinning an already pinned message changes nothing. Returns {status: 'message pinned'}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_author": {"type": "string", "description": "Phone number of the message author (E.164)"},
                "target_timestamp": {"type": "integer", "description": "Timestamp of the message to pin (from get_conversation)"},
                "recipient": {"type": "string", "description": "Phone number for DM conversations — provide this OR group_id"},
                "group_id": {"type": "string", "description": "Group ID for group conversations — provide this OR recipient"},
            },
            "required": ["target_author", "target_timestamp"],
        },
    ),
    Tool(
        name="unpin_message",
        description=(
            'Remove a pinned message from the top of a DM or group conversation for all participants; the message itself '
            "stays. Use pin_message to pin. target_author: E.164 number of the pinned message's sender; target_timestamp:"
            ' its ms timestamp (message id in get_conversation). Give recipient (E.164) for a DM or group_id for a group;'
            " neither is an error. Contacts Signal's servers; repeating has no further effect. Returns {status: 'message "
            "unpinned'}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_author": {"type": "string", "description": "Phone number of the message author (E.164)"},
                "target_timestamp": {"type": "integer", "description": "Timestamp of the pinned message (from get_conversation)"},
                "recipient": {"type": "string", "description": "Phone number for DM conversations — provide this OR group_id"},
                "group_id": {"type": "string", "description": "Group ID for group conversations — provide this OR recipient"},
            },
            "required": ["target_author", "target_timestamp"],
        },
    ),
    Tool(
        name="admin_delete_message",
        description=(
            "As a group admin, delete any member's message in that group for all participants. For your own messages use "
            'delete_group_message (group) or delete_message (DM); for local-only removal delete_local_messages. Requires '
            'admin rights — check is_admin in list_groups. group_id: from list_groups; target_author: E.164 number of the'
            " message's sender; target_timestamp: its ms timestamp (message id in get_conversation). Irreversible; "
            "contacts Signal's servers; the local store copy is kept. Returns {status: 'message deleted by admin'}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "Group ID where the message was sent (get from list_groups)"},
                "target_author": {"type": "string", "description": "Phone number of the user who sent the message"},
                "target_timestamp": {"type": "integer", "description": "Timestamp of the message to delete (from get_conversation)"},
            },
            "required": ["group_id", "target_author", "target_timestamp"],
        },
    ),
    Tool(
        name="send_contacts_sync",
        description=(
            "Send your local contact list to your other linked devices as a sync message, one-way from this device outward."
            " Takes no parameters. Use it when contacts added or renamed through signal-mcp do not appear on your phone or "
            "desktop. Contacts no one else and changes nothing locally. Returns status 'contacts synced to linked devices'."
            " To pull data from the primary device instead, use send_sync_request."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="update_device",
        description=(
            "Rename a device on your Signal account; the name shows in every device's Linked Devices list. device_id "
            "(required, integer from list_devices); name (required): the new label. Renaming the device signal-mcp itself "
            "runs on works everywhere; renaming any other device only works when signal-mcp is the account's primary device"
            " — otherwise it fails with 'This command doesn't work on linked devices'. Does not affect messaging. Returns "
            "status, device_id and name. To unlink a device use remove_device; to change your profile name use "
            "update_profile."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "device_id": {"type": "integer", "description": "Device ID (get from list_devices)"},
                "name": {"type": "string", "description": "New display name for the device"},
            },
            "required": ["device_id", "name"],
        },
    ),
    Tool(
        name="mark_as_unread",
        description=(
            'Mark messages as unread again in the local signal-mcp store, so get_unread returns them on its next call — '
            'e.g. to flag for follow-up. Local only: read receipts already sent and other devices are unaffected '
            '(send_read_receipt is the opposite, outward action). message_ids: list of message id strings exactly as '
            'returned by get_conversation, get_unread or search_messages; unknown ids are ignored. Idempotent. Returns '
            '{status, count} where count is the number of ids passed.'
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "message_ids": {"type": "array", "items": {"type": "string"}, "description": "Message id strings as returned by get_conversation, get_unread or search_messages"},
            },
            "required": ["message_ids"],
        },
    ),
    Tool(
        name="get_avatar",
        description=(
            "Return a contact's or group's avatar image as base64, from the copy signal-cli has stored. identifier "
            "(required): an E.164 phone number for a contact, or a group id from list_groups for a group (anything that is "
            "not a full E.164 number is treated as a group id). Returns identifier, base64 (decode to get the JPEG/PNG "
            "bytes) and has_avatar; fails with 'Could not find avatar' when none is stored. Use update_profile with "
            "avatar_path to set your own photo, update_group with avatar for a group's."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "Phone number (E.164) for a contact or group ID for a group"},
            },
            "required": ["identifier"],
        },
    ),
    Tool(
        name="send_message_request_response",
        description=(
            "Accept or decline a message request from someone not in your contacts. sender (required, E.164) is who sent "
            "the request; accept (required): true accepts, which shares your profile with them; false declines (deletes the"
            " request) and turns profile sharing off. Declining does not block them — use block_contact for that. The "
            "decision is recorded locally and synced to your linked devices; the sender is not sent a message. Returns "
            "status 'message request accepted' or 'message request declined' and sender."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sender": {"type": "string", "description": "Phone number (E.164) of the contact who sent the message request"},
                "accept": {"type": "boolean", "description": "true to accept (shares your profile), false to decline (does not block)"},
            },
            "required": ["sender", "accept"],
        },
    ),
    Tool(
        name="create_poll",
        description=(
            'Create a poll and send it to a contact or group. Use vote_poll to vote and terminate_poll to close it. '
            'question: the poll text; options: answer strings (at least 2, else an error); multi_select=true lets voters '
            'pick several answers (default false = single choice). Give recipient (E.164) for a DM or group_id (from '
            "list_groups) for a group; neither is an error. Contacts Signal's servers; not idempotent (each call sends a "
            "new poll); shares the 20-sends-per-minute rate limit; not saved to the local store. Returns {status: 'poll "
            "created', timestamp} — the timestamp (with your number as author) identifies the poll for vote_poll and "
            'terminate_poll.'
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The poll question text"},
                "options": {"type": "array", "items": {"type": "string"}, "description": "List of answer options (minimum 2 required)"},
                "recipient": {"type": "string", "description": "Phone number for a DM poll — provide this OR group_id"},
                "group_id": {"type": "string", "description": "Group ID for a group poll — provide this OR recipient"},
                "multi_select": {"type": "boolean", "description": "Allow voters to select multiple options (default: false = single choice only)", "default": False},
            },
            "required": ["question", "options"],
        },
    ),
    Tool(
        name="vote_poll",
        description=(
            'Cast or change your vote on an open Signal poll in a DM or group; the vote is visible to participants. Use '
            'create_poll to start a poll, terminate_poll to close your own. A poll has no separate id: identify it by '
            "target_author (E.164 number of the poll's creator) + target_timestamp (ms timestamp of the poll message, "
            "from create_poll or get_conversation). votes: 0-based indices into the poll's options — exactly one for a "
            'single-choice poll, all chosen indices at once for multi-select (each call replaces your previous vote). '
            'Give recipient (E.164) for a DM poll or group_id for a group poll; neither is an error. Voting on a '
            "terminated poll fails. Contacts Signal's servers; a local per-poll counter is incremented so re-votes "
            "supersede earlier ones. Returns {status: 'vote sent'}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_author": {"type": "string", "description": "Phone number of the poll creator (E.164)"},
                "target_timestamp": {"type": "integer", "description": "Timestamp of the poll message (from get_conversation)"},
                "votes": {"type": "array", "items": {"type": "integer"}, "description": "Option indices to vote for (0-based). Single item for single-choice polls."},
                "recipient": {"type": "string", "description": "Phone number for a DM poll — provide this OR group_id"},
                "group_id": {"type": "string", "description": "Group ID for a group poll — provide this OR recipient"},
            },
            "required": ["target_author", "target_timestamp", "votes"],
        },
    ),
    Tool(
        name="terminate_poll",
        description=(
            'Close a poll you created so no more votes are accepted; participants see it as ended with final results. '
            'Irreversible. Use vote_poll to vote, create_poll to start a new one. Only the creator can terminate. '
            'target_author: your own E.164 number (accepted for symmetry with vote_poll; only the timestamp is sent); '
            'target_timestamp: ms timestamp of the poll (from create_poll or get_conversation). Give recipient (E.164) '
            "for a DM poll or group_id for a group poll; neither is an error. Contacts Signal's servers. Returns {status:"
            " 'poll terminated'}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_author": {"type": "string", "description": "Phone number of the poll creator — must be your own number"},
                "target_timestamp": {"type": "integer", "description": "Timestamp of the poll message (from get_conversation)"},
                "recipient": {"type": "string", "description": "Phone number for a DM poll — provide this OR group_id"},
                "group_id": {"type": "string", "description": "Group ID for a group poll — provide this OR recipient"},
            },
            "required": ["target_author", "target_timestamp"],
        },
    ),
    Tool(
        name="set_expiration_timer",
        description=(
            "Set or turn off the disappearing-messages timer of a one-to-one or group chat. expiration_seconds (required): "
            "lifetime of new messages in seconds, 0 disables; common values 3600 (1h), 86400 (1d), 604800 (1w), 2592000 "
            "(30d). Pass recipient (E.164) for a direct chat or group_id (from list_groups) for a group; one is required, "
            "and group_id wins if both are given. Every participant is notified and the timer applies to new messages; "
            "already sent messages are unaffected. For a group this is the same as update_group with expiration_seconds. "
            "Returns status and seconds."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "expiration_seconds": {"type": "integer", "description": "Timer in seconds (0 to disable). Common: 3600=1h, 86400=1d, 604800=1w"},
                "recipient": {"type": "string", "description": "Phone number (E.164) for a direct conversation"},
                "group_id": {"type": "string", "description": "Group ID (from list_groups) for a group conversation; wins if recipient is also given"},
            },
            "required": ["expiration_seconds"],
        },
    ),
    Tool(
        name="list_identities",
        description=(
            "List stored Signal identity keys (safety numbers) and their trust levels, from the local store. number "
            "(optional, E.164) limits the result to one contact; omit it for all. Returns entries with number, uuid, "
            "fingerprint, safetyNumber, scannableSafetyNumber, trustLevel and addedTimestamp (epoch ms). trustLevel is "
            "TRUSTED_VERIFIED (manually verified), TRUSTED_UNVERIFIED (trusted on first use) or UNTRUSTED (key changed; "
            "sending is blocked until re-trusted). Use it when Signal reports 'safety number changed' or before "
            "trust_identity; it does not change trust itself."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "Only this contact's keys (E.164 phone number); omit for all"},
            },
        },
    ),
    Tool(
        name="trust_identity",
        description=(
            "Mark a contact's identity key as trusted so sending to them works again after their safety number changed "
            "(e.g. they reinstalled Signal). number (required, E.164). safety_number (optional): the safety number or "
            "fingerprint you verified in person or by call, as shown by list_identities; only that key is trusted and it "
            "becomes TRUSTED_VERIFIED. Without safety_number ALL known keys for the number are trusted unverified — this "
            "unblocks delivery but skips verification, so prefer passing it. Changes local trust only; the contact is not "
            "notified. Fails if the number or safety number does not match. Returns status 'trusted' and number."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "Phone number (E.164) whose identity key to trust"},
                "safety_number": {"type": "string", "description": "Safety number or fingerprint you verified (from list_identities); omit to trust all known keys unverified"},
            },
            "required": ["number"],
        },
    ),
]


TOOLS += [
    Tool(
        name="clear_local_store",
        description="Delete ALL locally stored messages from the signal-mcp database. This does NOT delete messages from Signal — only from the local store. Requires confirm=true.",
        inputSchema={
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "description": "Must be true to proceed — prevents accidental deletion"},
            },
            "required": ["confirm"],
        },
    ),
    Tool(
        name="delete_local_messages",
        description="Delete locally stored messages for one contact or group. Does NOT unsend from Signal — only removes from local store.",
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Phone number or group ID whose messages to delete"},
            },
            "required": ["recipient"],
        },
    ),
    Tool(
        name="export_messages",
        description=(
            "Export locally stored messages as a JSON or CSV string for archiving, analysis, or migration. "
            "Returns all messages in the local store by default; use recipient to restrict to one conversation. "
            "Use since (ISO 8601 datetime) to export only messages after a given point in time. "
            "JSON output preserves all fields (sender, timestamp, body, group_id); "
            "CSV output is flat and suitable for spreadsheets. "
            "Only messages already in the local store are included — messages never received on this device are absent. "
            "Use when you need a full or filtered dump of conversation history in machine-readable form. "
            "Do NOT use to read individual messages interactively — use get_conversation or search_messages for that."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["json", "csv"], "description": "Output format (default: json)"},
                "recipient": {"type": "string", "description": "Export only this conversation (phone number or group ID)"},
                "since": {"type": "string", "description": "Only include messages at or after this ISO datetime"},
            },
        },
    ),
    Tool(
        name="update_configuration",
        description=(
            "Update Signal account-wide messaging settings. "
            "Only works when signal-mcp is the account's primary device — fails with "
            "'This command doesn't work on linked devices' if signal-mcp was set up via signal-cli link. "
            "read_receipts controls whether Signal tells senders when you have read their messages. "
            "typing_indicators controls whether contacts see the '...' indicator when you are composing. "
            "link_previews controls whether URLs in outgoing messages generate inline previews. "
            "unidentified_delivery_indicators controls whether sealed-sender delivery icons are shown. "
            "All parameters are optional — omit any setting you do not want to change. "
            "Changes take effect immediately and persist across sessions. "
            "signal-cli has no way to read back current values — track what you've set yourself if needed. "
            "Use update_account for account-level privacy settings (discoverability, username). "
            "Do NOT use to change your profile name or photo — use update_profile for that."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "read_receipts": {"type": "boolean", "description": "Enable/disable sending read receipts"},
                "typing_indicators": {"type": "boolean", "description": "Enable/disable sending typing indicators"},
                "link_previews": {"type": "boolean", "description": "Enable/disable link previews in messages"},
                "unidentified_delivery_indicators": {"type": "boolean", "description": "Show/hide sealed sender indicators"},
            },
        },
    ),
    Tool(
        name="list_sticker_packs",
        description=(
            "List all sticker packs installed on this Signal account. "
            "Returns pack_id and sticker_id values needed for send_sticker and send_group_sticker. "
            "Use add_sticker_pack to install a new pack from a signal.art URL."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="add_sticker_pack",
        description=(
            "Install a Signal sticker pack from a signal.art URL. "
            "Returns {status, pack_id} — pack_id is parsed from the URI, ready to pass directly to "
            "get_sticker or send_sticker/send_group_sticker without a separate list_sticker_packs call. "
            "Use list_sticker_packs instead if you need to browse the pack's sticker_id/emoji contents first. "
            "The URI must be a signal.art URL in the format: https://signal.art/addstickers/#pack_id=...&pack_key=..."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Sticker pack URL (https://signal.art/addstickers/#pack_id=...&pack_key=...)"},
            },
            "required": ["uri"],
        },
    ),
    Tool(
        name="send_sticker",
        description=(
            'Send one sticker from an installed sticker pack to a single contact; it renders as a sticker, not a file. '
            'Use send_group_sticker for groups, send_attachment for ordinary images. recipient: E.164 number. pack_id: '
            'hex pack id and sticker_id: integer index within the pack, both from list_sticker_packs; an uninstalled pack'
            ' or invalid id returns an error — install packs first with add_sticker_pack (signal.art URL). Contacts '
            "Signal's servers; not idempotent (repeating sends a duplicate); sends share a 20-per-minute rate limit "
            "(calls wait rather than fail). Saved locally as '[sticker pack:id]'. Returns {status, timestamp}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Phone number in E.164 format"},
                "pack_id": {"type": "string", "description": "Sticker pack ID (hex string from list_sticker_packs)"},
                "sticker_id": {"type": "integer", "description": "Sticker ID within the pack (from list_sticker_packs)"},
            },
            "required": ["recipient", "pack_id", "sticker_id"],
        },
    ),
    Tool(
        name="send_group_sticker",
        description=(
            'Send one sticker from an installed sticker pack to a Signal group; it renders as a sticker, not a file. Use '
            'send_sticker for a single contact, send_group_attachment for ordinary images. group_id: from list_groups. '
            'pack_id: hex pack id and sticker_id: integer index within the pack, both from list_sticker_packs; an '
            'uninstalled pack or invalid id returns an error — install packs first with add_sticker_pack (signal.art '
            "URL). Contacts Signal's servers; not idempotent (repeating sends a duplicate); sends share a 20-per-minute "
            "rate limit (calls wait rather than fail). Saved locally as '[sticker pack:id]'. Returns {status, timestamp}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "Group ID (get from list_groups)"},
                "pack_id": {"type": "string", "description": "Sticker pack ID (hex string from list_sticker_packs)"},
                "sticker_id": {"type": "integer", "description": "Sticker ID within the pack (from list_sticker_packs)"},
            },
            "required": ["group_id", "pack_id", "sticker_id"],
        },
    ),
    Tool(
        name="send_story",
        description=(
            'Post an image or video as a Signal story, visible to its audience for 24 hours. Use send_message / '
            'send_attachment to message someone directly. path: one local image or video file; Files must lie inside the '
            'allowed send folders (default: the signal-mcp attachments folder, ~/Downloads, ~/Desktop, ~/Documents; '
            'override with SIGNAL_MCP_SEND_ROOTS) and not be hidden, else an error is returned. group_id (from '
            "list_groups) posts to that group's story instead of My Story. allow_replies (default true): false disables "
            "replies. Contacts Signal's servers; not idempotent (each call posts a new story); shares the "
            "20-sends-per-minute rate limit; not saved to the local store. Returns {status: 'posted', timestamp}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Image or video file to post"},
                "group_id": {"type": "string", "description": "Post to this group's story instead of My Story (from list_groups)"},
                "allow_replies": {"type": "boolean", "description": "Allow viewers to reply (default: true)", "default": True},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="list_attachments",
        description=(
            "List all Signal attachments that have been downloaded and saved to the local store. "
            "Returns filenames, MIME types, file sizes, and the associated message timestamp for each attachment. "
            "Only attachments explicitly downloaded (via receive_messages or import) appear here — "
            "attachments not yet fetched from Signal's servers are not listed. "
            "Use the returned filename with get_attachment to retrieve the actual file content. "
            "Use when you need to discover what media files are available locally before reading them. "
            "Do NOT use to download new attachments from Signal servers — use receive_messages for that."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_attachment",
        description=(
            "Retrieve metadata and the base64-encoded content of a locally saved Signal attachment by filename. "
            "Returns MIME type, file size, local path, and the raw bytes as base64 so the caller can read or display the file. "
            "If the file is not in the local attachments folder, it is fetched by attachment id from "
            "signal-cli's own attachment store (only attachments signal-cli already downloaded) and saved locally. "
            "Use list_attachments to discover available filenames before calling. "
            "Use when you need to read, display, or forward the contents of a received file or image. "
            "Do NOT use to send an attachment — use send_attachment or send_group_attachment for that."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Attachment filename (from list_attachments) or signal-cli attachment id"},
            },
            "required": ["filename"],
        },
    ),
    Tool(
        name="get_sticker",
        description="Retrieve a single sticker image as base64. Use list_sticker_packs to find pack_id and sticker_id values.",
        inputSchema={
            "type": "object",
            "properties": {
                "pack_id": {"type": "string", "description": "Sticker pack ID (hex string from list_sticker_packs)"},
                "sticker_id": {"type": "integer", "description": "Sticker ID within the pack"},
            },
            "required": ["pack_id", "sticker_id"],
        },
    ),
    Tool(
        name="upload_sticker_pack",
        description=(
            "Package and publish a sticker pack to Signal's CDN from local image files. "
            "Accepts a local manifest.json describing the pack, or a zip archive containing both the manifest and images. "
            "Signal's CDN stores the pack and returns a signal.art install URL you can share with others. "
            "Recipients call add_sticker_pack with the URL to install the pack and send its stickers. "
            "After publishing, the pack is available on Signal's network indefinitely. "
            "Use when you want to create and distribute a custom sticker pack. "
            "Use add_sticker_pack to install an existing pack for sending. "
            "Do NOT use to install a pack — use add_sticker_pack for that."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Local path to manifest.json or a zip containing the sticker pack"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="list_accounts",
        description=(
            "List all Signal accounts (phone numbers) registered in signal-cli on this machine. "
            "Returns each account's E.164 phone number and its registration status. "
            "Most setups have a single account; multiple accounts appear when signal-cli manages more than one number. "
            "Use get_own_number to get the active account's number in single-account setups. "
            "Use when you need to confirm which accounts are available before sending or receiving messages."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="update_account",
        description=(
            "Update Signal account-level privacy and identity settings. "
            "All parameters are optional — only provide the settings you want to change. "
            "discoverable_by_number controls whether others can find you by phone number. "
            "number_sharing controls whether your number is shared with contacts you message. "
            "username sets a @username alias; delete_username removes it. "
            "Use update_configuration for messaging settings (read receipts, typing indicators)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "device_name": {"type": "string", "description": "Name for this device shown in linked devices list"},
                "discoverable_by_number": {"type": "boolean", "description": "Allow others to find your account by phone number"},
                "number_sharing": {"type": "boolean", "description": "Share your phone number when sending messages"},
                "username": {"type": "string", "description": "Set a Signal username (without @) as an alias for your number"},
                "delete_username": {"type": "boolean", "description": "Delete your current Signal username"},
                "unrestricted_unidentified_sender": {"type": "boolean", "description": "Allow sealed-sender messages from anyone (not just contacts)"},
            },
        },
    ),
    Tool(
        name="set_pin",
        description=(
            "Set a Signal Registration Lock PIN to protect your account against SIM-swap and unauthorized re-registration. "
            "Only works when signal-mcp is the account's primary device — fails with "
            "'This command doesn't work on linked devices' if signal-mcp was set up via signal-cli link. "
            "Once set, anyone attempting to re-register your phone number on Signal must provide this PIN. "
            "The PIN must be 4–20 digits. Signal also uses the PIN to derive your storage encryption key. "
            "If you forget the PIN, you must wait 7 days for the lock to expire before re-registering. "
            "Use when you want to harden your account against SIM-swap attacks. "
            "Use remove_pin to disable the lock. "
            "Do NOT set a PIN you might forget — losing it locks you out of your account for 7 days."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pin": {"type": "string", "description": "4–20 digit numeric PIN (e.g. '123456')"},
            },
            "required": ["pin"],
        },
    ),
    Tool(
        name="remove_pin",
        description=(
            "Remove the Signal Registration Lock PIN, disabling re-registration protection on this account. "
            "Only works when signal-mcp is the account's primary device — fails with "
            "'This command doesn't work on linked devices' if signal-mcp was set up via signal-cli link. "
            "After removal, anyone who controls your phone number can re-register Signal without a PIN. "
            "Use only if you intentionally want to disable the registration lock. "
            "Use set_pin to set a new PIN instead of removing the existing one. "
            "Do NOT remove the PIN if you rely on it as a security measure against SIM-swap attacks."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="start_change_number",
        description=(
            "Begin migrating your Signal account to a new phone number. "
            "Only works when signal-mcp is the account's primary device — fails with "
            "'This command doesn't work on linked devices' if signal-mcp was set up via signal-cli link. "
            "Signal sends a 6-digit verification code to the new number via SMS (or voice call if voice=true). "
            "After calling this tool, call finish_change_number with the new number and received code to complete the migration. "
            "If Signal rejects the request due to rate limits, provide a captcha token obtained from "
            "https://signalcaptchas.org/challenge/generate.html. "
            "The account remains on the old number until finish_change_number succeeds. "
            "Use finish_change_number immediately after receiving the SMS code to complete the change. "
            "Do NOT call finish_change_number without first calling this tool — the verification code will not exist."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "number":  {"type": "string", "description": "New phone number in E.164 format (e.g. +12025551234)"},
                "voice":   {"type": "boolean", "description": "Request code via voice call instead of SMS (default: false)"},
                "captcha": {"type": "string",  "description": "Captcha token (required only if Signal demands it)"},
            },
            "required": ["number"],
        },
    ),
    Tool(
        name="finish_change_number",
        description=(
            "Complete the second step of a Signal phone number change by submitting the verification code. "
            "Only works when signal-mcp is the account's primary device — fails with "
            "'This command doesn't work on linked devices' if signal-mcp was set up via signal-cli link. "
            "Must be called after start_change_number, which initiates the number change and triggers the SMS/voice code. "
            "number is the new E.164 phone number you are migrating to. "
            "verification_code is the 6-digit code received via SMS or voice call to that number. "
            "pin is required only if your account has a Signal Registration Lock PIN set — omit otherwise. "
            "On success, the account is permanently migrated to the new number; all linked devices are updated. "
            "Use start_change_number first to request the verification code before calling this tool. "
            "Do NOT call this tool without first calling start_change_number — the code will not exist."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "number":            {"type": "string", "description": "The new phone number in E.164 format"},
                "verification_code": {"type": "string", "description": "6-digit verification code from SMS/voice"},
                "pin":               {"type": "string", "description": "Registration lock PIN (required if the account has a PIN set)"},
            },
            "required": ["number", "verification_code"],
        },
    ),
    Tool(
        name="submit_rate_limit_challenge",
        description=(
            "Unblock the account after Signal applies a rate limit. "
            "Provide the challenge token from the error and a solved captcha from "
            "https://signalcaptchas.org/challenge/generate.html"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "challenge": {"type": "string", "description": "Challenge token from the rate-limit error"},
                "captcha":   {"type": "string", "description": "Solved captcha token from the Signal captcha page"},
            },
            "required": ["challenge", "captcha"],
        },
    ),
    Tool(
        name="prune_store",
        description=(
            "Delete locally stored messages older than a given number of days (default: 180). "
            "Does NOT delete messages from Signal servers — only the local history cache. "
            "Useful for keeping the store from growing unbounded."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Delete messages older than this many days (default: 180)", "default": 180},
            },
        },
    ),
    Tool(
        name="set_webhook",
        description=(
            "Configure a webhook URL that receives a POST request for every incoming Signal message. "
            "The payload is a JSON object with fields: event, timestamp, sender, recipient, group_id, body, attachments, quote_id. "
            "Use this to connect signal-mcp to n8n, Make, Home Assistant, or any local HTTP endpoint. "
            "Pass url=null to disable webhooks. The URL is saved to disk and persists across restarts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Webhook URL to POST to (e.g. 'http://localhost:5678/webhook/signal'). Omit or pass null to clear."},
            },
        },
    ),
    Tool(
        name="get_webhook",
        description="Return the currently configured webhook URL, or null if none is set.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="find_contact",
        description=(
            "Find contacts whose number, name, given/family name, nickname or username contains query (case-insensitive "
            "substring; required). Use it to resolve a name to an E.164 number before send_message, or to check that a "
            "contact exists; use list_contacts instead to list everyone or filter by blocked status. all_recipients "
            "(default false) also searches people outside your address book, e.g. group members known only by their profile"
            " name. Reads the local contact store only. Returns a list (possibly empty) of contact objects with number, "
            "uuid, name, given_name, family_name, about, blocked and display_name."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Case-insensitive fragment of a name, nickname, username or phone number"},
                "all_recipients": {"type": "boolean", "description": "Also include recipients that are not in your address book (e.g. members of your groups), with their profile names", "default": False},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="schedule_message",
        description=(
            "Schedule a message to be sent at a specific future time. "
            "The message will be delivered when the background service runs (install-service) "
            "or when run_scheduled_messages is called manually. "
            "Returns the scheduled job ID — use cancel_scheduled_message to cancel it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Phone number in E.164 format (for DMs). Use group_id for group messages."},
                "group_id": {"type": "string", "description": "Group ID (for group messages). Mutually exclusive with recipient."},
                "message": {"type": "string", "description": "Message text to send"},
                "send_at": {"type": "string", "description": "When to send — ISO datetime string (e.g. '2024-06-01T09:00:00' or '2024-06-01 09:00')"},
            },
            "required": ["message", "send_at"],
        },
    ),
    Tool(
        name="list_scheduled_messages",
        description=(
            "List pending scheduled messages. "
            "Returns scheduled jobs with their ID, recipient, send time, and status. "
            "Use cancel_scheduled_message to cancel a pending job."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "include_done": {"type": "boolean", "description": "Include already-sent, cancelled, and failed messages (default: false)"},
            },
        },
    ),
    Tool(
        name="cancel_scheduled_message",
        description=(
            "Cancel a pending scheduled message by its job ID. "
            "Use list_scheduled_messages to find the ID. "
            "Returns an error if the message was already sent or does not exist."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "job_id": {"type": "integer", "description": "Scheduled message job ID from list_scheduled_messages"},
            },
            "required": ["job_id"],
        },
    ),
    Tool(
        name="run_scheduled_messages",
        description=(
            "Process and send any scheduled messages that are currently due. "
            "The background service calls this automatically, but you can also call it manually "
            "to deliver messages immediately without waiting for the next service run. "
            "Safe to call anytime, including when nothing is due (returns processed=0). "
            "Returns {processed: count, results: [{id, status: 'sent'|'failed', timestamp or error}, ...]} "
            "— one entry per job that was due, from list_scheduled_messages' job IDs."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
]

TOOLS = [t.model_copy(update={"annotations": annotations_for(t.name)}) for t in TOOLS]

_TOOL_NAMES = {t.name for t in TOOLS}


async def _list_tools(ctx: ServerRequestContext, params: RequestParams) -> ListToolsResult:
    if _READONLY:
        return ListToolsResult(tools=[t for t in TOOLS if t.name in _READ_ONLY_TOOLS])
    return ListToolsResult(tools=TOOLS)


async def call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
    name = params.name
    arguments = params.arguments or {}

    # Validate the tool exists and has its required parameters BEFORE touching the
    # daemon. Previously ensure_daemon() ran first, so an unknown tool name or a
    # missing argument reported "daemon failed to start" instead of the real problem
    # whenever the daemon itself couldn't start — masking the actual error.
    if name not in _TOOL_NAMES:
        return _err(f"Unknown tool: {name}")

    if _READONLY and name not in _READ_ONLY_TOOLS:
        return _err("This server is running in read-only mode (SIGNAL_MCP_READONLY).")

    client = get_client()  # noqa: F841 — used throughout the giant match below

    # Required parameters, per tool (gives a clean error instead of KeyError)
    _REQUIRED: dict[str, list[str]] = {
            "send_message":         ["message"],
            "send_group_message":   ["group_id", "message"],
            "send_note_to_self":    ["message"],
            "send_story":           ["path"],
            "send_group_attachment":["group_id"],
            "send_sticker":         ["recipient", "pack_id", "sticker_id"],
            "send_group_sticker":   ["group_id", "pack_id", "sticker_id"],
            "get_conversation":     ["recipient"],
            "search_messages":      ["query"],
            "react_to_message":     ["target_author", "target_timestamp", "emoji"],
            "get_profile":          ["number"],
            "block_contact":        ["number"],
            "unblock_contact":      ["number"],
            "remove_contact":       ["number"],
            "update_contact":       ["number"],
            "create_group":         ["name", "members"],
            "join_group":           ["uri"],
            "add_device":           ["uri"],
            "remove_device":        ["device_id"],
            "delete_message":       ["recipient", "target_timestamp"],
            "delete_group_message": ["group_id", "target_timestamp"],
            "send_read_receipt":    ["sender", "timestamps"],
            "update_group":         ["group_id"],
            "leave_group":          ["group_id"],
            "terminate_group":      ["group_id", "confirm"],
            "set_expiration_timer": ["expiration_seconds"],
            "trust_identity":       ["number"],
            "get_attachment":       ["filename"],
            "add_sticker_pack":     ["uri"],
            "get_sticker":          ["pack_id", "sticker_id"],
            "upload_sticker_pack":  ["path"],
            "set_pin":              ["pin"],
            "edit_message":         ["target_timestamp", "message"],
            "clear_local_store":    ["confirm"],
            "delete_local_messages":["recipient"],
            "pin_message":                    ["target_author", "target_timestamp"],
            "unpin_message":                  ["target_author", "target_timestamp"],
            "admin_delete_message":           ["group_id", "target_author", "target_timestamp"],
            "update_device":                  ["device_id", "name"],
            "mark_as_unread":                 ["message_ids"],
            "get_avatar":                     ["identifier"],
            "send_message_request_response":  ["sender", "accept"],
            "create_poll":                    ["question", "options"],
            "vote_poll":                      ["target_author", "target_timestamp", "votes"],
            "terminate_poll":                 ["target_author", "target_timestamp"],
            "start_change_number":            ["number"],
            "finish_change_number":           ["number", "verification_code"],
            "submit_rate_limit_challenge":    ["challenge", "captcha"],
        }
    if name in _REQUIRED:
        err = _require(arguments, *_REQUIRED[name])
        if err:
            return _err(err)

    try:
        if name not in _DAEMON_FREE:
            await client.ensure_daemon()

        if name == "send_message":
            result = await client.send_message(
                arguments.get("recipient"), arguments["message"],
                username=arguments.get("username"),
                end_session=arguments.get("end_session", False),
                formatting=bool(arguments.get("formatting", False)),
                **_send_options(arguments),
            )
            return _ok({"status": "sent", "timestamp": result.timestamp, "recipient": result.recipient})

        elif name == "send_group_message":
            result = await client.send_group_message(
                arguments["group_id"], arguments["message"],
                mentions=arguments.get("mentions"),
                formatting=bool(arguments.get("formatting", False)),
                **_send_options(arguments),
            )
            return _ok({"status": "sent", "timestamp": result.timestamp, "group_id": result.recipient})

        elif name == "send_note_to_self":
            result = await client.send_note_to_self(
                arguments["message"],
                attachments=arguments.get("attachments"),
                **_send_options(arguments),
            )
            return _ok({"status": "sent", "timestamp": result.timestamp})

        elif name == "edit_message":
            await client.edit_message(
                target_timestamp=arguments["target_timestamp"],
                message=arguments["message"],
                recipient=arguments.get("recipient"),
                group_id=arguments.get("group_id"),
            )
            return _ok({"status": "message edited", "target_timestamp": arguments["target_timestamp"]})

        elif name == "send_sticker":
            result = await client.send_sticker(
                arguments["recipient"], arguments["pack_id"], arguments["sticker_id"]
            )
            return _ok({"status": "sent", "timestamp": result.timestamp})

        elif name == "send_group_sticker":
            result = await client.send_group_sticker(
                arguments["group_id"], arguments["pack_id"], arguments["sticker_id"]
            )
            return _ok({"status": "sent", "timestamp": result.timestamp})

        elif name == "list_attachments":
            return _ok(await asyncio.to_thread(client.list_attachments))

        elif name == "get_attachment":
            return _ok(await client.get_attachment(arguments["filename"]))

        elif name == "send_story":
            result = await client.send_story(
                arguments["path"],
                group_id=arguments.get("group_id"),
                allow_replies=arguments.get("allow_replies", True),
            )
            return _ok({"status": "posted", "timestamp": result.timestamp})

        elif name == "receive_messages":
            await client._ensure_caches()
            try:
                timeout = int(arguments.get("timeout", 5))
            except (TypeError, ValueError):
                return _err("timeout must be an integer number of seconds")
            try:
                messages = await client.receive_messages(
                    timeout=timeout, max_messages=arguments.get("max_messages"),
                )
                return _ok([client._enrich_message(m) for m in messages])
            except Exception as e:
                if "already being received" in str(e):
                    # Background service is running — read from store instead
                    from signal_mcp.store import get_unread_messages as _get_unread
                    msgs = await asyncio.to_thread(_get_unread, client.account, 50)
                    return _ok({
                        "note": "Background service is running — returning unread messages from store instead.",
                        "messages": [client._enrich_message(m) for m in msgs],
                    })
                raise

        elif name == "receive_direct":
            await client._ensure_caches()
            try:
                timeout = int(arguments.get("timeout", 5))
            except (TypeError, ValueError):
                return _err("timeout must be an integer number of seconds")
            messages = await client.receive_direct(
                timeout=timeout,
                max_messages=arguments.get("max_messages"),
                ignore_attachments=arguments.get("ignore_attachments", False),
                ignore_stories=arguments.get("ignore_stories", False),
                ignore_avatars=arguments.get("ignore_avatars", False),
                ignore_stickers=arguments.get("ignore_stickers", False),
            )
            return _ok([client._enrich_message(m) for m in messages])

        elif name == "list_contacts":
            contacts = await client.list_contacts(
                search=arguments.get("search"),
                all_recipients=arguments.get("all_recipients", False),
                blocked=arguments.get("blocked"),
            )
            return _ok([c.to_dict() for c in contacts])

        elif name == "list_groups":
            groups = await client.list_groups(group_id=arguments.get("group_id"))
            return _ok([g.to_dict() for g in groups])

        elif name == "get_conversation":
            since = None
            if arguments.get("since"):
                try:
                    since = datetime.fromisoformat(arguments["since"])
                except ValueError:
                    return _err(f"Invalid since date: {arguments['since']}")
            limit, offset = _paging(arguments)
            await client._ensure_caches()
            messages, total = await asyncio.gather(
                client.get_conversation(
                    arguments["recipient"], limit=limit, offset=offset, since=since,
                ),
                asyncio.to_thread(
                    _store.count_conversation, arguments["recipient"], since=since,
                    own_number=client.account,
                ),
            )
            # client.get_conversation already marks incoming messages as read
            return _ok({
                "messages": [client._enrich_message(m) for m in messages],
                "total": total,
                "has_more": total > offset + len(messages),
                "limit": limit,
                "offset": offset,
            })

        elif name == "search_messages":
            bounds: dict[str, datetime | None] = {}
            for key in ("since", "until"):
                bounds[key] = None
                if arguments.get(key):
                    try:
                        bounds[key] = datetime.fromisoformat(arguments[key])
                    except ValueError:
                        return _err(f"Invalid {key} date: {arguments[key]}")
            limit, offset = _paging(arguments)
            await client._ensure_caches()
            messages = await client.search_messages(
                arguments["query"],
                limit=limit,
                offset=offset,
                sender=arguments.get("sender"),
                since=bounds["since"],
                until=bounds["until"],
            )
            return _ok([client._enrich_message(m) for m in messages])

        elif name == "send_attachment":
            path_arg = arguments.get("paths") or arguments.get("path")
            if not path_arg:
                return _err("Either path or paths is required")
            result = await client.send_attachment(
                arguments.get("recipient"),
                path_arg,
                caption=arguments.get("caption", ""),
                view_once=arguments.get("view_once", False),
                username=arguments.get("username"),
                **_send_options(arguments),
            )
            return _ok({"status": "sent", "timestamp": result.timestamp})

        elif name == "send_group_attachment":
            path_arg = arguments.get("paths") or arguments.get("path")
            if not path_arg:
                return _err("Either path or paths is required")
            result = await client.send_group_attachment(
                arguments["group_id"],
                path_arg,
                caption=arguments.get("caption", ""),
                view_once=arguments.get("view_once", False),
                **_send_options(arguments),
            )
            return _ok({"status": "sent", "timestamp": result.timestamp})

        elif name == "react_to_message":
            await client.react_to_message(
                target_author=arguments["target_author"],
                target_timestamp=arguments["target_timestamp"],
                emoji=arguments["emoji"],
                recipient=arguments.get("recipient"),
                group_id=arguments.get("group_id"),
                remove=arguments.get("remove", False),
            )
            action = "reaction removed" if arguments.get("remove") else "reaction sent"
            return _ok({"status": action})

        elif name == "set_typing":
            await client.set_typing(
                arguments.get("recipient"),
                stop=arguments.get("stop", False),
                group_id=arguments.get("group_id"),
            )
            return _ok({"status": "typing indicator sent"})

        elif name == "get_profile":
            contact = await client.get_profile(arguments["number"])
            return _ok(contact.to_dict())

        elif name == "block_contact":
            await client.block_contact(arguments["number"])
            return _ok({"status": "blocked", "number": arguments["number"]})

        elif name == "unblock_contact":
            await client.unblock_contact(arguments["number"])
            return _ok({"status": "unblocked", "number": arguments["number"]})

        elif name == "remove_contact":
            await client.remove_contact(
                arguments["number"],
                forget=arguments.get("forget", False),
                hide=arguments.get("hide", False),
            )
            return _ok({"status": "removed", "number": arguments["number"]})

        elif name == "update_profile":
            await client.update_profile(
                name=arguments.get("name"),
                about=arguments.get("about"),
                avatar_path=arguments.get("avatar_path"),
                remove_avatar=arguments.get("remove_avatar", False),
                given_name=arguments.get("given_name"),
                family_name=arguments.get("family_name"),
                about_emoji=arguments.get("about_emoji"),
                mobilecoin_address=arguments.get("mobilecoin_address"),
            )
            return _ok({"status": "profile updated"})

        elif name == "create_group":
            result = await client.create_group(
                arguments["name"],
                arguments["members"],
                description=arguments.get("description"),
                avatar_path=arguments.get("avatar"),
            )
            return _ok({"status": "group created", **result})

        elif name == "join_group":
            result = await client.join_group(arguments["uri"])
            return _ok({"status": "joined group", **result})

        elif name == "list_devices":
            devices = await client.list_devices()
            return _ok(devices)

        elif name == "add_device":
            await client.add_device(arguments["uri"])
            return _ok({"status": "device linked"})

        elif name == "remove_device":
            await client.remove_device(arguments["device_id"])
            return _ok({"status": "device removed", "device_id": arguments["device_id"]})

        elif name == "get_own_number":
            return _ok({"number": client.get_own_number()})

        elif name == "get_unread":
            await client._ensure_caches()
            warning = await _freshen_store(client)
            limit, _ = _paging(arguments)
            # Fetch one extra to detect whether more exist without a COUNT query
            messages = await client.get_unread_messages(limit=limit + 1)
            has_more = len(messages) > limit
            # messages are chronological (oldest first); the extra probe row, if
            # present, is the oldest of the batch — drop from the front, not the back,
            # so the newest unread message is never discarded.
            messages = messages[-limit:] if limit else []
            # Mark as read — Claude has now seen these messages
            unread_ids = [m.id for m in messages]
            if unread_ids:
                await asyncio.to_thread(_store.mark_as_read, unread_ids)
            result: dict = {
                "messages": [client._enrich_message(m) for m in messages],
                "has_more": has_more,
            }
            if warning:
                result["_warning"] = warning
            return _ok(result)

        elif name == "store_stats":
            return _ok(_store.get_stats(own_number=client.account))

        elif name == "import_desktop":
            from .desktop import import_from_desktop, DesktopImportError
            try:
                result = import_from_desktop()
                return _ok(result)
            except DesktopImportError as e:
                return _err(str(e))

        elif name == "sync_desktop":
            from .desktop import sync_from_desktop, DesktopImportError
            try:
                result = sync_from_desktop()
                return _ok(result)
            except DesktopImportError as e:
                return _err(str(e))

        elif name == "list_conversations":
            await client._ensure_caches()
            # client.list_conversations() already resolves names via resolve_name/resolve_group_name
            conversations = await client.list_conversations()
            return _ok(conversations)

        elif name == "delete_message":
            await client.delete_message(arguments["recipient"], arguments["target_timestamp"])
            return _ok({"status": "deleted"})

        elif name == "delete_group_message":
            await client.delete_group_message(arguments["group_id"], arguments["target_timestamp"])
            return _ok({"status": "deleted"})

        elif name == "send_read_receipt":
            await client.send_read_receipt(arguments["sender"], arguments["timestamps"])
            return _ok({"status": "read receipt sent"})

        elif name == "update_contact":
            await client.update_contact(
                arguments["number"],
                name=arguments.get("name"),
                given_name=arguments.get("given_name"),
                family_name=arguments.get("family_name"),
                nick_given_name=arguments.get("nick_given_name"),
                nick_family_name=arguments.get("nick_family_name"),
                note=arguments.get("note"),
            )
            return _ok({"status": "contact updated", "number": arguments["number"], "name": arguments.get("name")})

        elif name == "update_group":
            await client.update_group(
                arguments["group_id"],
                name=arguments.get("name"),
                description=arguments.get("description"),
                add_members=arguments.get("add_members"),
                remove_members=arguments.get("remove_members"),
                expiration_seconds=arguments.get("expiration_seconds"),
                add_admins=arguments.get("add_admins"),
                remove_admins=arguments.get("remove_admins"),
                link_mode=arguments.get("link_mode"),
                avatar_path=arguments.get("avatar"),
                ban_members=arguments.get("ban_members"),
                unban_members=arguments.get("unban_members"),
                reset_link=arguments.get("reset_link") is True,
                permission_add_member=arguments.get("permission_add_member"),
                permission_edit_details=arguments.get("permission_edit_details"),
                permission_send_messages=arguments.get("permission_send_messages"),
                member_label=arguments.get("member_label"),
                member_label_emoji=arguments.get("member_label_emoji"),
            )
            return _ok({"status": "group updated", "group_id": arguments["group_id"]})

        elif name == "leave_group":
            await client.leave_group(
                arguments["group_id"],
                admins=arguments.get("admins"),
                delete=arguments.get("delete") is True,
            )
            return _ok({"status": "left group", "group_id": arguments["group_id"]})

        elif name == "terminate_group":
            if arguments.get("confirm") is not True:
                return _err("confirm must be true to terminate the group for all members")
            await client.terminate_group(arguments["group_id"])
            return _ok({"status": "group terminated", "group_id": arguments["group_id"]})

        elif name == "pin_message":
            if not arguments.get("recipient") and not arguments.get("group_id"):
                return _err("Either recipient or group_id is required")
            await client.pin_message(
                target_author=arguments["target_author"],
                target_timestamp=arguments["target_timestamp"],
                recipient=arguments.get("recipient"),
                group_id=arguments.get("group_id"),
            )
            return _ok({"status": "message pinned"})

        elif name == "unpin_message":
            if not arguments.get("recipient") and not arguments.get("group_id"):
                return _err("Either recipient or group_id is required")
            await client.unpin_message(
                target_author=arguments["target_author"],
                target_timestamp=arguments["target_timestamp"],
                recipient=arguments.get("recipient"),
                group_id=arguments.get("group_id"),
            )
            return _ok({"status": "message unpinned"})

        elif name == "admin_delete_message":
            await client.admin_delete_message(
                target_author=arguments["target_author"],
                target_timestamp=arguments["target_timestamp"],
                group_id=arguments["group_id"],
            )
            return _ok({"status": "message deleted by admin"})

        elif name == "send_contacts_sync":
            await client.send_contacts_sync()
            return _ok({"status": "contacts synced to linked devices"})

        elif name == "update_device":
            await client.update_device(
                device_id=int(arguments["device_id"]),
                name=arguments["name"],
            )
            return _ok({"status": "device updated", "device_id": arguments["device_id"], "name": arguments["name"]})

        elif name == "set_expiration_timer":
            await client.set_expiration_timer(
                recipient=arguments.get("recipient"),
                group_id=arguments.get("group_id"),
                expiration=arguments["expiration_seconds"],
            )
            return _ok({"status": "expiration timer set", "seconds": arguments["expiration_seconds"]})

        elif name == "list_identities":
            identities = await client.list_identities(number=arguments.get("number"))
            return _ok(identities)

        elif name == "trust_identity":
            await client.trust_identity(
                arguments["number"],
                trust_all_known=not arguments.get("safety_number"),
                safety_number=arguments.get("safety_number"),
            )
            return _ok({"status": "trusted", "number": arguments["number"]})

        elif name == "update_configuration":
            await client.update_configuration(
                read_receipts=arguments.get("read_receipts"),
                typing_indicators=arguments.get("typing_indicators"),
                link_previews=arguments.get("link_previews"),
                unidentified_delivery_indicators=arguments.get("unidentified_delivery_indicators"),
            )
            return _ok({"status": "updated"})

        elif name == "list_sticker_packs":
            return _ok(await client.list_sticker_packs())

        elif name == "add_sticker_pack":
            install_result = await client.add_sticker_pack(arguments["uri"])
            return _ok({"status": "installed", **install_result})

        elif name == "get_sticker":
            data = await client.get_sticker(arguments["pack_id"], int(arguments["sticker_id"]))
            return _ok({"base64": data})

        elif name == "upload_sticker_pack":
            url = await client.upload_sticker_pack(arguments["path"])
            return _ok({"url": url})

        elif name == "list_accounts":
            accounts = await client.list_accounts()
            return _ok(accounts)

        elif name == "update_account":
            await client.update_account(
                device_name=arguments.get("device_name"),
                discoverable_by_number=arguments.get("discoverable_by_number"),
                number_sharing=arguments.get("number_sharing"),
                username=arguments.get("username"),
                delete_username=arguments.get("delete_username", False),
                unrestricted_unidentified_sender=arguments.get("unrestricted_unidentified_sender"),
            )
            return _ok({"status": "account updated"})

        elif name == "set_pin":
            await client.set_pin(arguments["pin"])
            return _ok({"status": "PIN set"})

        elif name == "remove_pin":
            await client.remove_pin()
            return _ok({"status": "PIN removed"})

        elif name == "clear_local_store":
            if arguments.get("confirm") is not True:
                return _err("confirm must be true to delete all local messages")
            count = await client.clear_local_store()
            return _ok({"deleted": count, "status": "cleared"})

        elif name == "delete_local_messages":
            count = await client.delete_local_messages(arguments["recipient"])
            return _ok({"deleted": count, "status": "deleted"})

        elif name == "get_user_status":
            statuses = await client.get_user_status(
                arguments.get("recipients"), usernames=arguments.get("usernames")
            )
            return _ok(statuses)

        elif name == "send_sync_request":
            await client.send_sync_request()
            return _ok({"status": "sync requested"})

        elif name == "mark_as_unread":
            await client.mark_as_unread(arguments["message_ids"])
            return _ok({"status": "marked as unread", "count": len(arguments["message_ids"])})

        elif name == "get_avatar":
            avatar_data = await client.get_avatar(arguments["identifier"])
            return _ok({"identifier": arguments["identifier"], "base64": avatar_data, "has_avatar": bool(avatar_data)})

        elif name == "send_message_request_response":
            await client.send_message_request_response(arguments["sender"], arguments["accept"])
            action = "accepted" if arguments["accept"] else "declined"
            return _ok({"status": f"message request {action}", "sender": arguments["sender"]})

        elif name == "create_poll":
            if not arguments.get("recipient") and not arguments.get("group_id"):
                return _err("Either recipient or group_id is required")
            options = arguments.get("options", [])
            if len(options) < 2:
                return _err("Poll requires at least 2 options")
            result = await client.create_poll(
                question=arguments["question"],
                options=options,
                recipient=arguments.get("recipient"),
                group_id=arguments.get("group_id"),
                multi_select=arguments.get("multi_select", False),
            )
            return _ok({"status": "poll created", "timestamp": result.timestamp})

        elif name == "vote_poll":
            if not arguments.get("recipient") and not arguments.get("group_id"):
                return _err("Either recipient or group_id is required")
            await client.vote_poll(
                target_author=arguments["target_author"],
                target_timestamp=arguments["target_timestamp"],
                votes=arguments["votes"],
                recipient=arguments.get("recipient"),
                group_id=arguments.get("group_id"),
            )
            return _ok({"status": "vote sent"})

        elif name == "terminate_poll":
            if not arguments.get("recipient") and not arguments.get("group_id"):
                return _err("Either recipient or group_id is required")
            await client.terminate_poll(
                target_author=arguments["target_author"],
                target_timestamp=arguments["target_timestamp"],
                recipient=arguments.get("recipient"),
                group_id=arguments.get("group_id"),
            )
            return _ok({"status": "poll terminated"})

        elif name == "export_messages":
            fmt = arguments.get("format", "json")
            if fmt not in ("json", "csv"):
                return _err("format must be 'json' or 'csv'")
            since_str = arguments.get("since")
            since = None
            if since_str:
                try:
                    since = datetime.fromisoformat(since_str)
                except ValueError:
                    return _err(f"Invalid since datetime: {since_str!r}")
            data = await client.export_messages(
                fmt=fmt,
                recipient=arguments.get("recipient"),
                since=since,
            )
            return _ok({"format": fmt, "data": data})

        elif name == "prune_store":
            days = int(arguments.get("days", 180))
            if days <= 0:
                return _err("days must be a positive integer")
            count = await asyncio.to_thread(_store.prune_old_messages, days)
            return _ok({"deleted": count, "older_than_days": days})

        elif name == "start_change_number":
            await client.start_change_number(
                number=arguments["number"],
                voice=arguments.get("voice", False),
                captcha=arguments.get("captcha"),
            )
            return _ok({"status": "verification code sent", "number": arguments["number"]})

        elif name == "finish_change_number":
            await client.finish_change_number(
                number=arguments["number"],
                verification_code=arguments["verification_code"],
                pin=arguments.get("pin"),
            )
            return _ok({"status": "number changed", "number": arguments["number"]})

        elif name == "submit_rate_limit_challenge":
            await client.submit_rate_limit_challenge(
                challenge=arguments["challenge"],
                captcha=arguments["captcha"],
            )
            return _ok({"status": "challenge submitted"})

        elif name == "set_webhook":
            from .config import set_webhook_url
            url = arguments.get("url") or None
            set_webhook_url(url)
            if url:
                return _ok({"status": "webhook set", "url": url})
            return _ok({"status": "webhook cleared"})

        elif name == "get_webhook":
            from .config import get_webhook_url
            url = get_webhook_url()
            return _ok({"url": url})

        elif name == "find_contact":
            err = _require(arguments, "query")
            if err:
                return _err(err)
            await client.ensure_daemon()
            contacts = await client.list_contacts(
                search=arguments["query"], all_recipients=arguments.get("all_recipients", False)
            )
            return _ok([c.to_dict() for c in contacts])

        elif name == "schedule_message":
            err = _require(arguments, "message", "send_at")
            if err:
                return _err(err)
            if not arguments.get("recipient") and not arguments.get("group_id"):
                return _err("Either 'recipient' or 'group_id' is required")
            from datetime import datetime as _dt
            send_at_str = arguments["send_at"]
            send_at = None
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
                try:
                    send_at = _dt.strptime(send_at_str, fmt)
                    break
                except ValueError:
                    continue
            if send_at is None:
                return _err(f"Invalid send_at format: '{send_at_str}'. Use ISO datetime e.g. '2024-06-01T09:00:00'")
            if send_at <= _dt.now():
                return _err("send_at must be in the future")
            job_id = _store.add_scheduled_message(
                message=arguments["message"],
                send_at=send_at,
                recipient=arguments.get("recipient"),
                group_id=arguments.get("group_id"),
            )
            return _ok({"job_id": job_id, "send_at": send_at.isoformat(), "status": "scheduled"})

        elif name == "list_scheduled_messages":
            jobs = _store.list_scheduled_messages(include_done=arguments.get("include_done", False))
            return _ok(jobs)

        elif name == "cancel_scheduled_message":
            err = _require(arguments, "job_id")
            if err:
                return _err(err)
            cancelled = _store.cancel_scheduled_message(int(arguments["job_id"]))
            if cancelled:
                return _ok({"status": "cancelled", "job_id": arguments["job_id"]})
            return _err(f"No pending scheduled message with id={arguments['job_id']}")

        elif name == "run_scheduled_messages":
            results = await client.process_scheduled_messages()
            return _ok({"processed": len(results), "results": results})

        else:
            return _err(f"Unknown tool: {name}")

    except SignalError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"Unexpected error: {e}")


_SERVICE_WARNING = (
    "Background service is not installed. Messages are only captured when this tool is called. "
    "Run 'signal-mcp install-service' to capture messages automatically in the background."
)

_FRESHEN_COOLDOWN = 30.0   # seconds — don't poll more than once per 30s
_last_freshen_at: float = 0.0


async def _freshen_store(client: SignalClient) -> str | None:
    """Poll signal-cli for new messages if no background service is running.

    Debounced: skips the poll if one completed within the last 30 seconds,
    so back-to-back tool calls (get_unread → list_conversations) only poll once.

    Returns a warning string when the service is absent, None when it is present.
    """
    global _last_freshen_at
    if is_service_installed():
        return None
    import time
    now = time.monotonic()
    if now - _last_freshen_at < _FRESHEN_COOLDOWN:
        return _SERVICE_WARNING  # still fresh from recent poll
    _last_freshen_at = now   # stamp BEFORE the await — concurrent calls see it as in-flight
    try:
        await client.receive_messages(timeout=2)
    except Exception:
        pass  # service just started receiving, or daemon not ready — best effort
    return _SERVICE_WARNING


app.add_request_handler("tools/list", RequestParams, _list_tools)
app.add_request_handler("tools/call", CallToolRequestParams, call_tool)


async def serve() -> None:  # pragma: no cover
    _store.init_db()
    try:
        check_signal_cli_version()
        client = get_client()
        # Pre-warm: start daemon in background so first tool call doesn't cold-start
        await client.prewarm()
        # Pre-load contact + group names concurrently in background
        _t = asyncio.create_task(client._ensure_caches())
        client._background_tasks.append(_t)
        # Watchdog is already started by prewarm() via _start_watchdog() (idempotent)
    except RuntimeError as exc:
        import sys
        print(f"[signal-mcp] WARNING: {exc}", file=sys.stderr)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())
