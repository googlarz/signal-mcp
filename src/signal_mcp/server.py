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
            "Send a text message to a Signal contact. The message is delivered end-to-end encrypted. "
            "Returns the sent timestamp, which can be used as target_timestamp for react_to_message or edit_message. "
            "To reply/quote a specific message, provide quote_author and quote_timestamp (get timestamps from get_conversation). "
            "Address the contact by recipient (phone number) or username — exactly one. "
            "Optional: a link preview card (preview_*), a story reply (story_*), no_urgent to skip the push notification. "
            "Set formatting=true to turn **bold**, *italic*, ~~strikethrough~~, `monospace` and ||spoiler|| in the text into real "
            "Signal formatting (markers are removed from the sent text); leave it off for text with literal asterisks or backticks. "
            "end_session=true instead resets the encrypted session with the contact (message is ignored; troubleshooting only). "
            "Use send_group_message for group chats, send_attachment for files/images."
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
            "Send a text message to a Signal group. The message is delivered end-to-end encrypted to all group members. "
            "Returns the sent timestamp, which can be used as target_timestamp for react_to_message or edit_message. "
            "To @mention a member, include their name in the message text and pass a mentions list where each entry has "
            "start (character index of the mention in the text), length (character count), and author (E.164 phone number). "
            "start/length are UTF-16 code units, not Unicode codepoints — an emoji before the mention shifts the offset by 2, not 1. "
            "Set formatting=true to turn **bold**, *italic*, ~~strikethrough~~, `monospace` and ||spoiler|| in the text into real Signal "
            "formatting (markers are removed from the sent text); with it, mention offsets refer to the text as you wrote it, markers "
            "included, and are adjusted for you. Leave it off for text that contains literal asterisks or backticks. "
            "To reply/quote a message, provide quote_author (sender's phone number) and quote_timestamp (from get_conversation). "
            "Use list_groups to get group_id values. "
            "Use send_group_attachment to send files or images to a group. "
            "Do NOT use for direct messages to a contact — use send_message instead."
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
            "Send a note to yourself via Signal's 'Note to Self' / saved messages feature. "
            "The note is synced across all your linked Signal devices. "
            "Useful for saving reminders, bookmarks, or drafts that sync to your phone. "
            "message supports lightweight markdown for Signal's native rich text: "
            "**bold**, *italic*, ~~strikethrough~~, `monospace`, ||spoiler|| — use it to visually distinguish "
            "different kinds of notes (e.g. a bold title per note) instead of plain text blobs. "
            "Pass attachments (e.g. a QR code image) and quote_author/quote_timestamp "
            "(to thread a follow-up under a previous note, from a prior send_note_to_self result) "
            "to combine content in one message instead of separate calls."
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
            "Edit the text of a previously sent message. "
            "Sends the edit via signal-cli to all original recipients; they see the updated text inline with an '(edited)' label. "
            "Only the message text can be modified — attachments, quoted replies, and reactions are immutable. "
            "The edit must reference the exact timestamp of the original message as returned by send_message or get_conversation. "
            "Edits can only be made to messages you sent; editing someone else's message returns an error. "
            "There is no enforced time limit, but Signal clients may ignore edits on very old messages. "
            "Provide recipient for a DM edit or group_id for a group edit; exactly one is required. "
            "Use when correcting a typo or updating information in a message you already sent. "
            "Do NOT use to change who a message was sent to — send a new message instead."
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
            "Manually poll signal-cli for new messages and store them. "
            "Prefer get_unread — it does this automatically and returns results in one call. "
            "Use receive_messages only if you want to poll without reading results."
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
            "Receive messages by calling signal-cli directly, bypassing the daemon. "
            "Use this as a fallback when the daemon is stuck or unresponsive — it stops the daemon, "
            "calls signal-cli receive directly, then lets the daemon restart. "
            "Prefer receive_messages (daemon mode) for normal use; use this only for troubleshooting."
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
            "List all Signal contacts known to this account, including names and phone numbers. "
            "Use the optional search parameter to filter by name or number substring. "
            "Returns contacts from signal-cli's local contact store. "
            "Use get_profile to fetch the current Signal profile for a specific contact."
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
            "List all Signal groups this account belongs to, including group name, ID, members, and admin list. "
            "The group_id returned here is required for send_group_message, send_group_attachment, and update_group. "
            "Each member includes their group 'label' (the tag shown next to their name, e.g. a child's name) when set. "
            "Also returns, when non-empty: pending_members (invited), requesting_members (join requests awaiting approval), "
            "banned, permission_* settings (EVERY_MEMBER/ONLY_ADMINS), message_expiration_time, is_terminated. "
            "Use update_group to modify a group, or leave_group to exit."
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
        description="Get recent message history with a contact or group from local store. Automatically marks returned messages as read in the local store (does NOT send a Signal read receipt — call send_read_receipt for that).",
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Phone number or group ID"},
                "limit": {"type": "integer", "description": "Max messages to return (default: 50)", "default": 50},
                "offset": {"type": "integer", "description": "Number of messages to skip for pagination (default: 0)", "default": 0},
                "since": {"type": "string", "description": "Only messages after this ISO datetime (e.g. 2024-01-01T00:00:00)"},
            },
            "required": ["recipient"],
        },
    ),
    Tool(
        name="search_messages",
        description=(
            "Full-text search across all locally stored messages by keyword or phrase. "
            "Searches message bodies using SQLite FTS — results are ranked by relevance. "
            "Only messages in the local store are searchable; messages never received on this device are excluded. "
            "Use sender to narrow results to a specific conversation. "
            "Use since and/or until (ISO 8601) to restrict to a time window, e.g. 'last week' or a specific day. "
            "Use limit and offset to paginate through large result sets. "
            "Use when looking for a specific message or topic across all Signal conversations. "
            "Do NOT use to browse a conversation chronologically — use get_conversation for that."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword or phrase to search for"},
                "sender": {"type": "string", "description": "Filter results to messages from this phone number (E.164)"},
                "since": {"type": "string", "description": "Only messages at or after this ISO datetime (e.g. 2024-01-01 or 2024-01-01T09:00:00)"},
                "until": {"type": "string", "description": "Only messages strictly before this ISO datetime (exclusive; until=2024-01-02 includes all of Jan 1)"},
                "limit": {"type": "integer", "description": "Maximum results to return (default 50)"},
                "offset": {"type": "integer", "description": "Skip this many results for pagination (default 0)", "default": 0},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="send_attachment",
        description=(
            "Send one or more files or images to a Signal contact. "
            "Supports photos, videos, documents, and audio files. "
            "Use path for a single file or paths to send multiple files in one message. "
            "Set view_once=true to send media that auto-deletes after the recipient views it once. "
            "For groups use send_group_attachment instead."
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
            "Send one or more files (photos, videos, documents, audio) to a Signal group in a single message. "
            "All current group members receive the attachment via the normal Signal encrypted delivery pipeline. "
            "Provide path for a single file or paths for multiple files sent together in one message. "
            "Set view_once=true so each member can only open the media once before it disappears — "
            "ideal for sensitive images; does not apply to document types. "
            "The file must exist and be readable on the local filesystem; non-existent paths return an error. "
            "Use list_groups to obtain the group_id. "
            "Use when sharing a file with a group chat. "
            "Do NOT use for direct messages — use send_attachment instead. "
            "Do NOT use when you only want to send text — use send_group_message instead."
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
            "Add or remove an emoji reaction on a Signal message in a direct or group conversation. "
            "target_author is the phone number of the person who sent the original message. "
            "target_timestamp is the sent_at timestamp of that message (from get_conversation). "
            "Supply recipient for a DM conversation or group_id for a group conversation — exactly one is required. "
            "Each account can have at most one reaction per message; calling again with a different emoji replaces the previous one. "
            "Set remove=true to retract an existing reaction without adding a new one (emoji is still required as the key). "
            "Use when you want to react to or acknowledge a specific message without sending a reply. "
            "Do NOT use to send a text reply — use send_message or send_group_message for that."
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
            "Send a 'typing…' indicator to a Signal contact to show you are composing a message. "
            "The indicator appears immediately in the recipient's conversation and auto-expires after ~15 seconds "
            "if no message is sent — you do not need to call stop=true after sending the message. "
            "Call with stop=true to cancel an in-progress typing indicator early (e.g. if the user abandons the message). "
            "signal-cli relays the indicator via the Signal protocol; if the recipient has typing indicators "
            "disabled in their settings, it is silently ignored on their end — no error is returned. "
            "Provide recipient for a one-to-one chat or group_id for a group (at least one is required). "
            "Use before send_message to create a realistic 'typing' effect in an automated workflow. "
            "Do NOT call repeatedly in a tight loop; one call per composing session is sufficient."
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
            "Fetch the Signal profile for a contact, including their display name, about text, and avatar. "
            "Profile data is fetched live from the Signal network (not local cache). "
            "Use this to verify a contact's current name or check if they have a profile set up. "
            "Use update_profile to update your own profile."
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
            "Block a Signal contact so they can no longer send you messages or call you. "
            "Only works when signal-mcp is the account's primary device — fails with "
            "'This command doesn't work on linked devices' if signal-mcp was set up via signal-cli link. "
            "The block is applied locally via signal-cli and propagated to the Signal network. "
            "The blocked contact receives NO notification — from their perspective, messages appear sent "
            "but are silently discarded before reaching you; delivery receipts are suppressed. "
            "Blocking does not delete existing message history; prior conversations remain in your local store. "
            "The block persists across restarts and is reversible — call unblock_contact to lift it. "
            "Use when you want to permanently stop receiving messages from a contact. "
            "Use unblock_contact to reverse the block. "
            "Do NOT use as a temporary mute — blocking hides the contact from normal message flow entirely. "
            "Do NOT use to remove a contact from your list — use remove_contact for that."
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
            "Unblock a previously blocked Signal contact, restoring their ability to send you messages and calls. "
            "Only works when signal-mcp is the account's primary device — fails with "
            "'This command doesn't work on linked devices' if signal-mcp was set up via signal-cli link. "
            "The contact is NOT notified that they were unblocked. "
            "Use block_contact to re-block, or list_contacts to see which contacts are blocked."
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
            "Remove a contact from the local signal-cli contact list on this device. "
            "This only removes the local record — it does NOT block the contact, delete message history, "
            "or affect the contact's ability to message you. "
            "To prevent incoming messages, use block_contact instead. "
            "Set hide=true to only hide the contact from the list (data kept), or forget=true to delete "
            "all data for the recipient including identity keys and sessions (mutually exclusive). "
            "Use update_contact to set a local display name without removing."
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
            "Update your own Signal profile visible to all contacts. "
            "name sets your display name shown to contacts who have not saved your number. "
            "about sets the bio text shown on your profile page. "
            "avatar_path sets a new profile photo from a local image file (JPEG or PNG). "
            "Set remove_avatar=true to clear your current photo without setting a new one. "
            "All parameters are optional — only include what you want to change. "
            "Changes are propagated to the Signal network immediately. "
            "Use get_profile to read a contact's current profile. "
            "Do NOT use to rename a linked device — use update_device for that. "
            "Do NOT use to change messaging settings — use update_configuration for that."
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
                "avatar_path": {"type": "string", "description": "Path to avatar image file"},
                "remove_avatar": {"type": "boolean", "description": "Remove current avatar", "default": False},
            },
        },
    ),
    Tool(
        name="create_group",
        description=(
            "Create a new Signal group with specified members. "
            "You are automatically added as the group admin. All listed members receive an invitation notification. "
            "Returns the new group's ID and invite link. "
            "Use update_group to modify the group after creation (name, description, members, link settings). "
            "Use send_group_message to post messages to the group."
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
            "Join a Signal group using an invite link (https://signal.group/#...). "
            "If the group requires admin approval, your join request will be pending until approved. "
            "After joining, use list_groups to find the group_id for sending messages."
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
            "List all devices currently linked to your Signal account, including the primary device and any linked secondaries. "
            "Returns each device's ID, name, and last-seen timestamp. "
            "Device ID 1 is always the primary device (your registered phone). "
            "Use the returned device_id values with update_device (rename), remove_device (unlink). "
            "Use when auditing which devices have access to your Signal account, "
            "or to find the ID of a device you want to rename or remove."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="add_device",
        description=(
            "Link a new secondary device to your Signal account using a device-link URI. "
            "Only works when signal-mcp is the account's primary device — fails with "
            "'This command doesn't work on linked devices' if signal-mcp was set up via signal-cli link. "
            "The URI is generated on the new device by running 'signal-cli link' or by scanning the QR code "
            "in Signal Desktop's Settings → Linked Devices → Link New Device. "
            "After linking, the new device receives future messages but not historical ones. "
            "Use list_devices to confirm the device was linked successfully. "
            "Use remove_device to unlink a device you no longer use. "
            "Do NOT share the device-link URI — it grants full Signal account access to whoever uses it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Device link URI (from signal-cli link output)"},
            },
            "required": ["uri"],
        },
    ),
    Tool(
        name="remove_device",
        description=(
            "Permanently unlink a secondary device from your Signal account. "
            "Only works when signal-mcp is the account's primary device — fails with "
            "'This command doesn't work on linked devices' if signal-mcp was set up via signal-cli link. "
            "The device loses access to send and receive messages immediately. "
            "device_id must be a secondary device (ID ≥ 2) — you cannot unlink your primary device. "
            "The removed device is not notified; it simply stops receiving messages. "
            "This action is irreversible — the device must re-link via add_device to regain access. "
            "Use list_devices to find the device_id you want to remove. "
            "Use update_device to rename a device without removing it."
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
        description=(
            "Return this server's own Signal account number as {number} (E.164, e.g. +4915112345678). "
            "Local lookup: no network call, no daemon needed, no side effects. "
            "Use it to know which messages are your own (sender == number) or to address send_note_to_self; "
            "use list_accounts instead to see every account registered in signal-cli on this machine."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="store_stats",
        description=(
            "Report statistics about signal-mcp's local message database (~/.local/share/signal-mcp/messages.db). "
            "Returns {total_messages, unread_messages (incoming only), db_size_bytes, oldest, newest} with oldest/newest as ISO datetimes or null when empty. "
            "Read-only, local only, no daemon needed. "
            "Use it to check whether history has been imported (import_desktop / sync_desktop) or before cleaning up with prune_store, delete_local_messages or clear_local_store."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_unread",
        description=(
            "Get new unread messages. If the background service (signal-mcp install-service) is running, "
            "reads directly from the local store. Otherwise polls signal-cli first to fetch any messages "
            "that arrived since the last check, then returns unread. Always use this to check for new messages. "
            "Messages are marked as read after retrieval, so a call with has_more=true in the response "
            "should be followed by calling get_unread again with the same limit — the just-returned "
            "messages are no longer unread, so the next call naturally returns the next batch."
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
        description=(
            "Import the full message history from Signal Desktop on this machine into signal-mcp's local store, so get_conversation, search_messages and export_messages can see older messages. "
            "No parameters. Reads Signal Desktop's encrypted database, which needs sqlcipher installed and the database key: on macOS read from the Keychain ('Signal Safe Storage', may prompt for access), on Linux via secret-tool (libsecret / GNOME Keyring). "
            "Signal Desktop must be installed and opened at least once. Only writes to the local store; nothing is sent to Signal. "
            "Already stored messages are skipped, so re-running is safe but slow. Only one import can run at a time. "
            "Returns {imported, skipped, total, max_ts_ms, platform, source}. "
            "Use sync_desktop for later updates; it only reads messages newer than the last sync."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="sync_desktop",
        description=(
            "Incrementally import new messages from Signal Desktop into signal-mcp's local store: only messages newer than the last sync (with a 60-second overlap) are read, so repeat calls are fast. "
            "No parameters. The first call imports everything, like import_desktop. "
            "Same requirements as import_desktop: sqlcipher, Signal Desktop installed, and Keychain (macOS) or secret-tool (Linux) access to its key. "
            "Only writes to the local store; duplicates are skipped. "
            "Returns the import_desktop fields {imported, skipped, total, max_ts_ms, platform, source} plus since (ISO datetime of the lower bound, null on first run) and incremental (boolean). "
            "Use import_desktop only for a deliberate full re-scan."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_conversations",
        description=(
            "List all conversations (both direct and group) ordered by most recent message. "
            "Returns contact/group name, phone number or group_id, last message preview, timestamp, and unread count. "
            "Use this to get an inbox overview before reading specific conversations with get_conversation. "
            "Contact and group names are resolved from local signal-cli contacts and groups. "
            "Use get_unread to fetch only unread messages across all conversations. "
            "Do NOT use this to read message history — use get_conversation for that."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_user_status",
        description=(
            "Check whether one or more phone numbers or usernames are registered Signal users. "
            "Queries Signal's servers for each number and returns a registered/unregistered status. "
            "Accepts a list so you can batch-check multiple numbers in a single call. "
            "Useful before sending to an unknown number to avoid 'unregistered user' delivery failures. "
            "Note: privacy-mode accounts or numbers that have opted out of discoverability may show as unregistered "
            "even if they actively use Signal. "
            "Use before sending to a new contact to confirm they are reachable on Signal. "
            "Do NOT use to look up contact profile details — use get_profile for that."
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
            "Request a full sync of messages, contacts, and groups from your primary Signal device to this linked device. "
            "Signal's linked-device architecture stores history on the primary device; a sync pulls that data here. "
            "Use when list_conversations shows no history, list_contacts returns fewer contacts than expected, "
            "or list_groups is missing groups that exist on your phone. "
            "The sync is asynchronous — data arrives in the background over the next few seconds. "
            "Do NOT use to receive new incoming messages — use receive_messages for that."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="delete_message",
        description=(
            "Remote-delete (unsend) a message you previously sent to a Signal contact. "
            "Delivers a delete request to the recipient's device; the message disappears from their "
            "conversation view on Signal 5.0+ clients. "
            "You can only delete messages you sent — you cannot delete messages received from others. "
            "target_timestamp is the sent_at timestamp of the message (from get_conversation). "
            "Deletion may fail silently if the recipient is on an older Signal client. "
            "Remote deletion does not remove the message from the local signal-mcp store — "
            "use delete_local_messages to remove it locally. "
            "Use when you want to retract a sent message from the recipient's device. "
            "Do NOT use for group messages — use delete_group_message instead. "
            "Do NOT use to delete a message you received — only senders can remotely delete."
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
            "Remote-delete (unsend) a message you previously sent to a Signal group. "
            "Delivers a delete request to all group members' devices; the message disappears from "
            "their conversation view on Signal 5.0+ clients. "
            "You can only delete messages you sent — for admin deletion of any member's message use admin_delete_message. "
            "target_timestamp is the sent_at timestamp of the message (from get_conversation). "
            "Deletion may fail silently on older Signal clients. "
            "Remote deletion does not remove the message from the local signal-mcp store. "
            "Use when you want to retract a message you sent in a group. "
            "Do NOT use for direct messages — use delete_message instead."
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
            "Send a read receipt to a contact, notifying them that you have read their messages. "
            "The sender sees a 'Read' indicator under their messages in their Signal app. "
            "Pass all timestamps you want to mark as read in a single call to batch the receipts. "
            "Timestamps come from the received_at or sent_at fields in get_conversation. "
            "Note: read receipts are only delivered if the sender has read receipts enabled in their Signal settings. "
            "Use after reading a conversation with get_conversation to acknowledge the messages. "
            "Do NOT use to mark messages as read in the local store — get_conversation does that automatically. "
            "Do NOT use for group messages — Signal does not support per-sender read receipts in groups."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sender": {"type": "string", "description": "Phone number (E.164) of the contact whose messages you are acknowledging"},
                "timestamps": {"type": "array", "items": {"type": "integer"}, "description": "Timestamps of the messages to mark as read (from get_conversation sent_at/received_at fields)"},
            },
            "required": ["sender", "timestamps"],
        },
    ),
    Tool(
        name="update_contact",
        description=(
            "Set or update the local display name, nickname or note for a Signal contact "
            "(at least one of name, given_name, family_name, nick_given_name, nick_family_name, note). "
            "The name is stored only in signal-cli's local contact database — it is never sent to or visible by the contact. "
            "Overrides the contact's own profile name in list_contacts and conversation displays. "
            "Useful for adding a human-readable label to a number that has no Signal profile name. "
            "Use list_contacts to see current names before updating. "
            "Use when you want to assign or correct a contact's display name locally. "
            "Do NOT use to change your own profile name — use update_profile for that. "
            "Do NOT use to block or remove a contact — use block_contact or remove_contact for those."
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
            "Modify a Signal group's settings, membership, or permissions. "
            "All parameters except group_id are optional — include only what you want to change. "
            "add_members sends invitations; remove_members removes members immediately. "
            "add_admins promotes members to admin; remove_admins demotes them. "
            "expiration_seconds sets the disappearing-messages timer (0 to disable). "
            "link_mode controls the invite link: 'enabled' (anyone with link can join), "
            "'enabled-with-approval' (admin must approve), 'disabled' (no link), "
            "or 'reset' (same as reset_link=true). "
            "member_label / member_label_emoji set ONLY YOUR OWN label in this group (the tag shown next to your name) — "
            "it is impossible to set another member's label; each member sets their own. Any member can set their own label. "
            "ban_members / unban_members manage the ban list; permission_* take 'every-member' or 'only-admins'. "
            "Changes are applied instantly and all members receive an update notification. "
            "You must be a group admin to change membership, admin list, or invite link. "
            "Use list_groups to get the group_id and confirm your admin status. "
            "Do NOT use to send a message — use send_group_message for that."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "Group ID to update"},
                "name": {"type": "string", "description": "New group name"},
                "description": {"type": "string", "description": "New group description"},
                "add_members": {"type": "array", "items": {"type": "string"}, "description": "Phone numbers to add"},
                "remove_members": {"type": "array", "items": {"type": "string"}, "description": "Phone numbers to remove"},
                "add_admins": {"type": "array", "items": {"type": "string"}, "description": "Phone numbers to promote to admin"},
                "remove_admins": {"type": "array", "items": {"type": "string"}, "description": "Phone numbers to demote from admin"},
                "expiration_seconds": {"type": "integer", "description": "Disappearing message timer in seconds (0 to disable)"},
                "link_mode": {"type": "string", "description": "Invite link mode: 'disabled', 'enabled', 'enabled-with-approval', or 'reset' to generate a new link"},
                "reset_link": {"type": "boolean", "description": "Generate a new invite link, invalidating the old one"},
                "avatar": {"type": "string", "description": "Local image file path for the new group avatar"},
                "ban_members": {"type": "array", "items": {"type": "string"}, "description": "Members to ban from (re)joining the group"},
                "unban_members": {"type": "array", "items": {"type": "string"}, "description": "Members to remove from the ban list"},
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
            "Leave a Signal group. After leaving, you will no longer receive messages from the group "
            "and will be removed from the member list. Other members are notified that you left. "
            "This action is irreversible without being re-invited. "
            "If you are the group's ONLY admin you must name a successor in 'admins', otherwise signal-cli refuses. "
            "Use list_groups to find the group_id."
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
            "DESTRUCTIVE AND IRREVERSIBLE: permanently terminate a Signal group FOR ALL MEMBERS. "
            "Afterwards nobody can send messages or start calls in it, and it cannot be undone. "
            "Requires admin privileges. To just exit a group yourself, use leave_group instead. "
            "Requires confirm=true; only call after the user explicitly asked to end the group for everyone."
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
            "Pin a message in a DM or group conversation so it appears prominently in the conversation header. "
            "Pinning delivers a system-level pin notification to all participants via signal-cli; "
            "they see the pinned message highlighted at the top of the thread. "
            "Any participant can pin any message — admin privileges are not required. "
            "Only one message can be pinned per conversation at a time; pinning a new message "
            "automatically replaces the previous pin. "
            "Provide exactly one of recipient (for a DM) or group_id (for a group). "
            "Get target_author and target_timestamp from get_conversation — both are required to identify the message. "
            "Use unpin_message to remove a pinned message without replacing it. "
            "Use when you want to highlight an important message for all participants. "
            "Do NOT use if you only want to bookmark a message for yourself — pinning is visible to everyone."
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
            "Unpin a previously pinned message in a DM or group conversation, removing it from the "
            "conversation header. Provide either recipient (for DMs) or group_id (for groups). "
            "Get target_author and target_timestamp from get_conversation. "
            "Use pin_message to pin a message."
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
            "As a group admin, delete any message posted in a group you administer, regardless of who sent it. "
            "The message is removed for all participants immediately. "
            "Only works if you are an admin of the specified group — use list_groups to confirm admin status. "
            "For deleting your own messages use delete_message (DM) or delete_group_message (group) instead."
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
            "Push your local contacts list to all linked Signal devices (e.g., phone, desktop). "
            "Useful when contacts added via signal-cli are not showing up on other devices. "
            "This is a one-way sync from this device outward."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="update_device",
        description=(
            "Rename a linked secondary device on your Signal account. "
            "Only works when signal-mcp is the account's primary device — fails with "
            "'This command doesn't work on linked devices' if signal-mcp was set up via signal-cli link. "
            "The updated name is synced to the Signal network and appears immediately in your Signal app's "
            "Settings → Linked Devices list across all your devices. "
            "Only secondary (linked) devices can be renamed; the primary device name is set during registration. "
            "Use list_devices to find all linked device IDs and their current names. "
            "The device_id is a small integer (e.g. 2, 3); device 1 is always the primary. "
            "Renaming does not affect the device's ability to send or receive messages. "
            "Use when you want to distinguish between multiple linked devices by a meaningful label. "
            "Use remove_device to unlink a device entirely. "
            "Do NOT use to rename your own primary account — that is done via update_profile."
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
            "Mark one or more messages as unread in the local signal-mcp store. "
            "This updates only the local database — it does not affect read receipts already sent "
            "to the sender, nor does it change how messages appear on other devices. "
            "message_ids are the internal signal-mcp IDs returned by get_conversation or search_messages. "
            "Messages marked unread are returned by get_unread on the next call. "
            "Use when you want to flag a message for follow-up later."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "message_ids": {"type": "array", "items": {"type": "string"}, "description": "List of message IDs to mark as unread"},
            },
            "required": ["message_ids"],
        },
    ),
    Tool(
        name="get_avatar",
        description=(
            "Retrieve the profile photo for a contact or group as base64-encoded image data. "
            "Pass a phone number (E.164) for contacts or a group ID (from list_groups) for groups. "
            "Returns raw image bytes encoded as base64 — decode to get a JPEG or PNG. "
            "Returns an error if no avatar is set for the identifier. "
            "Use get_profile to also read name and about text alongside the avatar. "
            "Use update_profile with avatar_path to set your own profile photo."
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
        description="Accept or decline a message request from an unknown contact (required before replying to strangers)",
        inputSchema={
            "type": "object",
            "properties": {
                "sender": {"type": "string", "description": "Phone number of the contact who sent the message request"},
                "accept": {"type": "boolean", "description": "true to accept and start chatting, false to decline/block"},
            },
            "required": ["sender", "accept"],
        },
    ),
    Tool(
        name="create_poll",
        description=(
            "Create a poll and send it to a Signal contact or group. "
            "Provide at least 2 options. Set multi_select=true to allow voters to pick multiple answers. "
            "Provide either recipient (DM) or group_id (group) — exactly one is required. "
            "Returns the poll timestamp needed for vote_poll and terminate_poll. "
            "Use terminate_poll to close the poll and stop accepting votes."
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
            "Cast your vote on an active Signal poll in a DM or group conversation. "
            "Your vote is delivered via signal-cli and is visible to all participants in real time. "
            "Each participant can vote once; re-voting overwrites the previous selection. "
            "For single-choice polls, provide exactly one option index in votes. "
            "For multi-select polls, provide all chosen indices in a single call — partial updates are not supported. "
            "votes are 0-based indices corresponding to the options array from the original create_poll call. "
            "Get target_author and target_timestamp from the poll message returned by get_conversation — "
            "a poll has no separate ID, it's identified by its author + message timestamp. "
            "Provide exactly one of recipient (for a DM poll) or group_id (for a group poll). "
            "Voting on a terminated poll returns an error. "
            "Use terminate_poll to close a poll you created and freeze the results. "
            "Use when responding to an open poll in a conversation. "
            "Do NOT use to create a poll — use create_poll instead."
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
            "Close (terminate) a poll you created, stopping any further votes. "
            "All participants are notified that the poll has ended and can see the final results. "
            "Get target_timestamp from the original poll message in get_conversation — a poll has no "
            "separate ID, it's identified by its message timestamp. "
            "Only the poll creator can terminate their own poll. "
            "Provide either recipient (DM poll) or group_id (group poll)."
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
            "Set or disable the disappearing-messages timer for a direct or group conversation. "
            "Once set, all new messages auto-delete after expiration_seconds on both sides. "
            "Common values: 3600 (1h), 86400 (1d), 604800 (1w), 2592000 (30d). "
            "Set expiration_seconds=0 to disable disappearing messages entirely. "
            "Provide recipient for a direct conversation or group_id for a group — exactly one is required. "
            "The change is delivered to all participants and takes effect on new messages immediately; "
            "existing messages already sent are not affected. "
            "Use when you want automatic privacy for a sensitive conversation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "expiration_seconds": {"type": "integer", "description": "Timer in seconds (0 to disable). Common: 3600=1h, 86400=1d, 604800=1w"},
                "recipient": {"type": "string", "description": "Phone number for a direct conversation"},
                "group_id": {"type": "string", "description": "Group ID for a group conversation"},
            },
            "required": ["expiration_seconds"],
        },
    ),
    Tool(
        name="list_identities",
        description=(
            "List the Signal identity keys (safety numbers) and trust levels for one or all contacts. "
            "Each contact has a unique identity key; Signal uses these to verify end-to-end encryption integrity. "
            "Trust levels: TRUSTED_VERIFIED (manually verified), TRUSTED_UNVERIFIED (trusted on first use, TOFU), "
            "or UNTRUSTED (key changed — sending is blocked until re-trusted). "
            "Omit number to inspect all stored identities; provide number to filter to a specific contact. "
            "Use before calling trust_identity to check the current trust state and key fingerprint. "
            "Use when Signal reports 'safety number changed' to identify which contact needs re-verification. "
            "Do NOT use to trust or change trust levels — use trust_identity for that."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "Filter to a specific contact (optional)"},
            },
        },
    ),
    Tool(
        name="trust_identity",
        description=(
            "Trust a contact's Signal identity key after verifying their safety number out-of-band. "
            "Signal uses identity keys (safety numbers) to verify end-to-end encryption. "
            "When a contact's safety number changes (e.g. they reinstalled Signal), sending fails "
            "until you explicitly trust the new key — this tool resolves that block. "
            "Provide safety_number to trust only that specific verified key; leave it blank to trust "
            "all known keys for the number (less secure but unblocks delivery immediately). "
            "Use list_identities to inspect the current trust level and key fingerprint before calling. "
            "Use when Signal blocks delivery with 'untrusted identity' or 'safety number changed' errors. "
            "Do NOT trust without first verifying the safety number via a trusted channel (in-person, phone call). "
            "Trusting an unverified key bypasses Signal's TOFU identity verification."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "Phone number to trust"},
                "safety_number": {"type": "string", "description": "Verified safety number (leave blank to trust all known keys)"},
            },
            "required": ["number"],
        },
    ),
]


TOOLS += [
    Tool(
        name="clear_local_store",
        description=(
            "Delete ALL messages and attachment records from signal-mcp's local database. "
            "confirm (boolean, required) must be exactly true, otherwise nothing is deleted and an error is returned. "
            "Local only: nothing is deleted from Signal, your phone or other devices, and downloaded attachment files on disk are left in place. "
            "Irreversible except by re-importing (import_desktop) or receiving again. "
            "Returns {deleted: count, status: 'cleared'}. "
            "Use delete_local_messages to clear one conversation, prune_store to drop only old messages; delete_message unsends a message in Signal."
        ),
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
        description=(
            "Delete the locally stored messages of one conversation from signal-mcp's database. "
            "recipient (required): the contact's E.164 phone number or the group ID; your own number deletes only your note-to-self messages. "
            "Local only: nothing is unsent from Signal or removed from other devices; irreversible locally. "
            "Returns {deleted: count, status: 'deleted'} (0 if nothing matched). "
            "Use clear_local_store to wipe everything, prune_store to drop messages by age, delete_message to unsend in Signal."
        ),
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
            "Export messages from signal-mcp's local store as one JSON or CSV string, for archiving or analysis. "
            "format: 'json' (default; full message objects with resolved sender_name/group_name, attachments and extras) or 'csv' (flat columns id, timestamp, sender, sender_name, recipient, group_id, group_name, body, quote_id, is_read). "
            "recipient (optional): limit to one conversation, an E.164 phone number or a group ID. "
            "since (optional, ISO 8601 datetime, e.g. '2026-01-01T00:00:00'): only messages at or after it; an invalid value returns an error. "
            "Read-only. Only messages already in the local store are included (run sync_desktop or receive_messages first). "
            "Returns {format, data}. Use get_conversation or search_messages to read messages interactively."
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
            "Change account-wide messaging settings and sync them to your linked devices. All four booleans are optional; omit any you do not want to change, and a call with none is a no-op: "
            "read_receipts whether senders are told when you have read their messages; "
            "typing_indicators whether contacts see you typing; "
            "link_previews whether URLs in outgoing messages get previews; "
            "unidentified_delivery_indicators whether sealed-sender delivery icons are shown. "
            "Primary device only: on a linked signal-cli setup it fails with 'This command doesn't work on linked devices'. "
            "Returns {status: 'updated'}. signal-cli cannot read the current values back, so track what you set. "
            "Use update_account for discoverability, number sharing and username; update_profile for name and avatar."
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
            "List the sticker packs installed on this Signal account. "
            "No parameters. Returns signal-cli's array of packs: {packId (hex), url, installed, title, author, cover, stickers: [{id, emoji, contentType}]}. "
            "Read-only. Use packId and a sticker id with send_sticker, send_group_sticker or get_sticker. "
            "Use add_sticker_pack to install a pack from a signal.art link."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="add_sticker_pack",
        description=(
            "Install an existing sticker pack on this Signal account from its signal.art link. "
            "uri (required): https://signal.art/addstickers/#pack_id=<hex>&pack_key=<hex>; both pack_id and pack_key are needed. "
            "Installing the same pack again has no further effect. "
            "Returns {status: 'installed', pack_id} with pack_id parsed from the uri, ready for send_sticker, send_group_sticker or get_sticker; "
            "call list_sticker_packs to see the sticker ids and emoji in it. "
            "Use upload_sticker_pack instead to publish a new pack of your own images."
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
            "Send a single sticker to a Signal contact in a direct message. "
            "Stickers are small images from installed packs delivered as a distinct message type — "
            "they appear rendered in the conversation, not as a file attachment. "
            "Both pack_id (a hex string) and sticker_id (a 0-based integer) must match an installed pack; "
            "referencing an uninstalled pack or an invalid sticker_id returns an error. "
            "Use list_sticker_packs to browse all installed packs and retrieve valid pack_id and sticker_id values. "
            "If no packs are installed, call add_sticker_pack first with a signal.art URL to install one. "
            "Use when you want to send an expressive image reaction or decoration to a contact. "
            "Use send_group_sticker to send a sticker to a group instead of a DM. "
            "Do NOT use to send a regular image file — use send_attachment for that."
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
            "Send a single sticker to a Signal group so all members receive it. "
            "Stickers are small images from installed packs delivered as a distinct message type — "
            "they appear rendered in the group conversation, not as a file attachment. "
            "Both pack_id (a hex string) and sticker_id (a 0-based integer) must match an installed pack; "
            "referencing an uninstalled pack or invalid sticker_id returns an error. "
            "Use list_sticker_packs to browse installed packs and retrieve valid pack_id and sticker_id values. "
            "If no packs are installed, call add_sticker_pack first with a signal.art URL to install one. "
            "Use list_groups to obtain the group_id. "
            "Use when sending an expressive image reaction or decoration to a group chat. "
            "Use send_sticker for direct messages instead of group chats. "
            "Do NOT use to send a regular image file — use send_group_attachment for that."
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
            "Post an image or video as a Signal story — to My Story by default, or to a group's story with group_id. "
            "Stories are visible to the audience for 24 hours. "
            "Do NOT use to message someone — use send_message or send_attachment."
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
            "List the attachment files saved in the local attachments folder (~/Downloads/signal-attachments). "
            "No parameters. Returns an array of {filename, path, size (bytes), modified (ISO datetime)}, sorted by filename; empty if the folder does not exist. "
            "Read-only and local; nothing is downloaded. "
            "Pass a filename to get_attachment for its details. Attachments of incoming messages land here when messages are received (receive_messages or the background service)."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_attachment",
        description=(
            "Look up one received attachment and make sure it is saved locally. "
            "filename (required): a filename from list_attachments, or a signal-cli attachment id from a message's attachments; path components such as '../' are rejected. "
            "If the file is not in ~/Downloads/signal-attachments, it is copied there from signal-cli's own attachment store (only attachments signal-cli already downloaded; otherwise 'Attachment not found'). "
            "Returns {filename, path, size (bytes), modified (ISO datetime)}: metadata and the local path, not the file content. Nothing is sent to Signal. "
            "Use send_attachment or send_group_attachment to send a file."
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
        description=(
            "Fetch one sticker image from an installed pack as base64. "
            "pack_id (required, hex string) and sticker_id (required, integer) come from list_sticker_packs (packId and stickers[].id) or add_sticker_pack. "
            "Read-only. Returns {base64}; the image format is the sticker's contentType from list_sticker_packs (usually image/webp). "
            "Use send_sticker or send_group_sticker to send it instead of downloading it."
        ),
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
            "Publish a new sticker pack made from your own images to Signal's servers and get a shareable signal.art link. "
            "path (required): local path to a manifest.json (with the sticker images next to it) or to a zip containing the manifest and images. "
            "The file must be inside an allowed folder (~/Downloads/signal-attachments, ~/Downloads, ~/Desktop, ~/Documents, or the SIGNAL_MCP_SEND_ROOTS list) and not in a hidden folder. "
            "The pack is public to anyone with the link and cannot be deleted through this tool. "
            "Invalid packs or oversized images return an error. Returns {url}. "
            "Use add_sticker_pack to install an existing pack."
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
            "List every Signal account registered in signal-cli on this machine. "
            "Returns a JSON array of E.164 phone numbers (e.g. [\"+4915112345678\"]); no registration status or other fields. "
            "Read-only, asks the running signal-cli daemon. Most setups have exactly one account. "
            "Use get_own_number instead to get the single account this server sends and receives as."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="update_account",
        description=(
            "Change account attributes stored on Signal's servers for this account. All six parameters are optional and only the ones you pass are sent: "
            "device_name (string) renames this device as shown in list_devices on your other devices; "
            "discoverable_by_number (boolean) whether people who have your number can find you on Signal; "
            "number_sharing (boolean) whether your phone number is shown to people you message; "
            "unrestricted_unidentified_sender (boolean) true lets anyone, not only contacts, send you sealed-sender messages; "
            "username (string, without @) claims a Signal username; delete_username (boolean) removes the current one and takes precedence over username if both are given. "
            "Takes effect immediately on the real account; setting a username that is taken or invalid fails with an error. "
            "Returns {status: 'account updated'} (the resulting username is not echoed). "
            "Use update_configuration for read receipts, typing indicators and link previews, update_profile for your name, about text and avatar."
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
            "Set or replace the Registration Lock PIN on your Signal account, so re-registering your phone number elsewhere (e.g. after a SIM swap) requires this PIN. "
            "pin (string, required): the new PIN, numeric, e.g. '123456'. "
            "Primary device only: on a linked signal-cli setup it fails with 'This command doesn't work on linked devices'. "
            "Affects the real account immediately; calling again with a new pin replaces the old one. "
            "If you forget it, re-registration is blocked until the lock lapses after 7 days of inactivity. "
            "Returns {status: 'PIN set'}. Use remove_pin to turn the lock off; finish_change_number needs this PIN when changing numbers."
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
            "Remove the Registration Lock PIN from your Signal account, so anyone who controls your phone number can re-register it without a PIN. "
            "No parameters. "
            "Primary device only: on a linked signal-cli setup it fails with 'This command doesn't work on linked devices'. "
            "Affects the real account immediately; undo it by calling set_pin again. "
            "Returns {status: 'PIN removed'}. Use set_pin to change the PIN instead of removing it."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="start_change_number",
        description=(
            "Step 1 of moving this Signal account to a new phone number: asks Signal to send a verification code to the new number. "
            "number (required): the new number in E.164 format, e.g. +12025551234; "
            "voice (boolean, default false): deliver the code by voice call instead of SMS; "
            "captcha (optional): a captcha token, needed only when a previous attempt failed with a captcha-required error; solve one at https://signalcaptchas.org/registration/generate.html and pass the resulting signalcaptcha:// token. "
            "Primary device only: on a linked signal-cli setup it fails with 'This command doesn't work on linked devices'. "
            "The account stays on the old number until finish_change_number succeeds. Rate limits are returned as errors. "
            "Returns {status: 'verification code sent', number}. Then call finish_change_number with the same number and the received code."
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
            "Step 2 of moving this Signal account to a new phone number: submits the verification code and completes the change. "
            "Call start_change_number first; without it there is no code to verify. "
            "number (required): the same new E.164 number given to start_change_number; "
            "verification_code (required): the 6-digit code received by SMS or voice call; "
            "pin (optional): your Registration Lock PIN, needed only if one is set (see set_pin). "
            "Primary device only: on a linked signal-cli setup it fails with 'This command doesn't work on linked devices'. "
            "On success the real account is moved to the new number; this cannot be undone except by another change-number round. "
            "A wrong pin fails with the number of tries remaining. Returns {status: 'number changed', number}."
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
            "Lift a Signal rate limit on sending by submitting a proof-of-humanity challenge. "
            "Use it when a send fails with a rate-limit / proof-required error that includes a challenge token. "
            "challenge (required): the challenge token from that error; "
            "captcha (required): the token from solving the captcha at https://signalcaptchas.org/challenge/generate.html. "
            "Talks to Signal's servers; a rejected captcha returns an error and you need a fresh one. "
            "Returns {status: 'challenge submitted'}; then retry the failed send. "
            "For a captcha needed while changing number, pass it to start_change_number instead."
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
            "Delete locally stored messages older than a number of days from signal-mcp's database, together with their attachment records and search index entries. "
            "days (integer, default 180, must be positive): messages with a timestamp older than now minus this many days are deleted. "
            "Local only: nothing is deleted from Signal; irreversible locally. "
            "Returns {deleted: count, older_than_days}. "
            "Use it to keep the store small; use delete_local_messages for one conversation or clear_local_store to wipe everything."
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
            "Save or clear the webhook URL that signal-mcp's background receiver (signal-mcp receive --watch, run by install-service) POSTs every incoming message to. "
            "url (optional): an http(s) URL such as 'http://localhost:5678/webhook/signal'; omit it or pass null/empty to clear. "
            "Stored in ~/.local/share/signal-mcp/webhook.json; the receiver reads it at startup, so restart the service for a change to apply. The SIGNAL_MCP_WEBHOOK environment variable overrides it. "
            "Payload: JSON {event: 'message', timestamp, sender, recipient, group_id, body, quote_id, attachments, is_read, receipt_type, expires_in_seconds, view_once}. "
            "Returns {status: 'webhook set', url} or {status: 'webhook cleared'}. Use get_webhook to check the value in effect."
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
        description=(
            "Return the webhook URL in effect as {url}, or {url: null} if none is configured. "
            "No parameters. The SIGNAL_MCP_WEBHOOK environment variable wins over the URL saved with set_webhook. "
            "Read-only, local only. Use set_webhook to change or clear it."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="find_contact",
        description=(
            "Search contacts by name or phone number fragment. "
            "Returns all contacts whose name or number contains the query string (case-insensitive). "
            "Use this to look up a phone number when you only know a name, or to verify a contact exists."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Name or phone number fragment to search for"},
                "all_recipients": {"type": "boolean", "description": "Also include recipients that are not in your address book (e.g. members of your groups), with their profile names", "default": False},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="schedule_message",
        description=(
            "Save a text message in the local store to be sent at a future time. "
            "Give exactly one target: recipient (E.164 phone number, for a direct message) or group_id (for a group). "
            "message (required): the text. "
            "send_at (required): local time as 'YYYY-MM-DDTHH:MM[:SS]' or 'YYYY-MM-DD HH:MM[:SS]' (no timezone); must be in the future. "
            "Nothing is sent at that time by itself: a due message goes out only when run_scheduled_messages (or the `signal-mcp run-scheduled` CLI, e.g. from cron) runs. "
            "Returns {job_id, send_at, status: 'scheduled'}. "
            "Use list_scheduled_messages to review jobs and cancel_scheduled_message to cancel one; use send_message or send_group_message to send now."
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
            "List messages scheduled with schedule_message, ordered by send time. "
            "include_done (boolean, default false): also include jobs that were sent, cancelled or failed; by default only pending jobs are listed. "
            "Read-only, local only. Returns an array of {id, recipient, group_id, message, send_at, created_at, status ('pending'|'sent'|'cancelled'|'failed'), error}. "
            "Use the id with cancel_scheduled_message; run_scheduled_messages sends the pending jobs that are due."
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
            "Cancel one pending scheduled message so it will never be sent. "
            "job_id (required, integer): the id from schedule_message or list_scheduled_messages. "
            "Local only; the job stays in the list with status 'cancelled' and cannot be re-activated (schedule it again instead). "
            "Returns {status: 'cancelled', job_id}, or an error if no pending job has that id (already sent, failed, cancelled or unknown)."
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
            "Send every scheduled message whose send_at time has passed and is still pending. "
            "No parameters. Nothing else delivers scheduled messages, so call this (or run `signal-mcp run-scheduled` from cron) after their time; due messages are sent late, never dropped. "
            "Each due job is sent once as a real Signal message and marked 'sent' or 'failed' (failed jobs are not retried). Safe to call when nothing is due. "
            "Returns {processed: count, results: [{id, status: 'sent', timestamp} or {id, status: 'failed', error}]}. "
            "Use schedule_message to add jobs and list_scheduled_messages to inspect them."
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
