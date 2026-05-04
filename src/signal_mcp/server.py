"""MCP server exposing all Signal tools to Claude."""

import asyncio
import json
from datetime import datetime

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .client import SignalClient, SignalError
from .config import check_signal_cli_version, is_service_installed
from . import store as _store

app = Server("signal-mcp")

_client: SignalClient | None = None

# Tools that don't need the signal-cli daemon (read from local store only)
_DAEMON_FREE = {
    "import_desktop", "sync_desktop", "store_stats",
    "get_conversation", "search_messages", "get_own_number",
    "list_attachments", "get_attachment",
    "clear_local_store", "delete_local_messages", "export_messages",
    "prune_store", "mark_as_unread",
}
# Tools NOT in _DAEMON_FREE call ensure_daemon() automatically before executing.
# get_unread calls _freshen_store() (which may call receive_messages) if no
# background service is running.
# list_accounts, list_conversations, get_configuration etc. call signal-cli JSON-RPC.


def get_client() -> SignalClient:
    global _client
    if _client is None:
        _client = SignalClient()
    return _client


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=f"Error: {msg}")]


def _require(arguments: dict, *keys: str) -> str | None:
    """Return an error string if any required key is missing, else None."""
    missing = [k for k in keys if k not in arguments]
    if missing:
        return f"Missing required parameter(s): {', '.join(missing)}"
    return None


# ── Tool definitions ───────────────────────────────────────────────────────────

TOOLS = [
    Tool(
        name="send_message",
        description=(
            "Send a text message to a Signal contact. The message is delivered end-to-end encrypted. "
            "Returns the sent timestamp, which can be used as target_timestamp for react_to_message or edit_message. "
            "To reply/quote a specific message, provide quote_author and quote_timestamp (get timestamps from get_conversation). "
            "Use send_group_message for group chats, send_attachment for files/images."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Phone number in E.164 format (e.g. +1234567890)"},
                "message": {"type": "string", "description": "Message text to send"},
                "quote_author": {"type": "string", "description": "Phone number of the author of the message being quoted/replied to"},
                "quote_timestamp": {"type": "integer", "description": "Timestamp of the message being quoted/replied to (from get_conversation)"},
            },
            "required": ["recipient", "message"],
        },
    ),
    Tool(
        name="send_group_message",
        description=(
            "Send a text message to a Signal group. All group members receive the message. "
            "Returns the sent timestamp for use with react_to_message or edit_message. "
            "To @mention specific members, provide a mentions list with character offsets into the message text. "
            "To reply to a message, provide quote_author and quote_timestamp. "
            "Use list_groups to find the group_id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "Group ID (get from list_groups)"},
                "message": {"type": "string", "description": "Message text to send"},
                "mentions": {
                    "type": "array",
                    "description": "List of @mentions: each item is {start, length, author} where start/length are character offsets into the message and author is a phone number",
                    "items": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "integer"},
                            "length": {"type": "integer"},
                            "author": {"type": "string"},
                        },
                    },
                },
                "quote_author": {"type": "string", "description": "Phone number of the author of the message being quoted/replied to"},
                "quote_timestamp": {"type": "integer", "description": "Timestamp of the message being quoted/replied to (from get_conversation)"},
            },
            "required": ["group_id", "message"],
        },
    ),
    Tool(
        name="send_note_to_self",
        description=(
            "Send a note to yourself via Signal's 'Note to Self' / saved messages feature. "
            "The note is synced across all your linked Signal devices. "
            "Useful for saving reminders, bookmarks, or drafts that sync to your phone."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Note text to save"},
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
            },
        },
    ),
    Tool(
        name="list_groups",
        description=(
            "List all Signal groups this account belongs to, including group name, ID, members, and admin list. "
            "The group_id returned here is required for send_group_message, send_group_attachment, and update_group. "
            "Use update_group to modify a group, or leave_group to exit."
        ),
        inputSchema={"type": "object", "properties": {}},
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
        description="Search for messages containing a keyword",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword or phrase to search for"},
                "sender": {"type": "string", "description": "Filter results to messages from this phone number (E.164)"},
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
                "path": {"type": "string", "description": "Single file path (absolute, relative, or ~/path)"},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Multiple file paths to send as one message"},
                "caption": {"type": "string", "description": "Optional caption text shown below the attachment", "default": ""},
                "view_once": {"type": "boolean", "description": "Send as view-once media — recipient can only view it once before it disappears", "default": False},
            },
            "required": ["recipient"],
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
            },
            "required": ["group_id"],
        },
    ),
    Tool(
        name="react_to_message",
        description="React to a Signal message with an emoji (DM or group). Set remove=true to remove a reaction.",
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
            "Typing indicators are only supported for one-to-one DMs; passing a group_id is not valid. "
            "Use before send_message to create a realistic 'typing' effect in an automated workflow. "
            "Do NOT use for groups — group typing indicators are not supported by Signal. "
            "Do NOT call repeatedly in a tight loop; one call per composing session is sufficient."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Phone number in E.164 format"},
                "stop": {"type": "boolean", "description": "Set to true to cancel an active typing indicator (default: false = start typing)", "default": False},
            },
            "required": ["recipient"],
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
            "Use update_contact to set a local display name without removing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "Phone number to remove (E.164 format)"},
            },
            "required": ["number"],
        },
    ),
    Tool(
        name="update_profile",
        description="Update your own Signal profile (name, about text, avatar)",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Display name to set"},
                "about": {"type": "string", "description": "About/bio text"},
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
        description="List all devices linked to your Signal account",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="add_device",
        description="Link a new device to your Signal account using a device link URI",
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
        description="Unlink a device from your Signal account",
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
            "Get new unread messages. If the background service (signal-mcp install-service) is running, "
            "reads directly from the local store. Otherwise polls signal-cli first to fetch any messages "
            "that arrived since the last check, then returns unread. Always use this to check for new messages. "
            "Messages are marked as read after retrieval. Response includes has_more=true if more unread messages "
            "exist beyond the limit — call again with a higher limit or paginate."
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
        description="List all conversations (direct and group) ordered by most recent message",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_user_status",
        description="Check whether one or more phone numbers are registered Signal users",
        inputSchema={
            "type": "object",
            "properties": {
                "recipients": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of phone numbers (E.164) to check",
                },
            },
            "required": ["recipients"],
        },
    ),
    Tool(
        name="send_sync_request",
        description="Request a sync of messages, contacts, and groups from your primary Signal device. Useful if history is missing on this linked device.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="delete_message",
        description="Remote-delete (unsend) a message you sent to a contact",
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
        description="Remote-delete (unsend) a message you sent to a group",
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
        description="Mark one or more messages as read (sends read receipts to sender)",
        inputSchema={
            "type": "object",
            "properties": {
                "sender": {"type": "string", "description": "Phone number of the message sender"},
                "timestamps": {"type": "array", "items": {"type": "integer"}, "description": "List of message timestamps to mark read"},
            },
            "required": ["sender", "timestamps"],
        },
    ),
    Tool(
        name="update_contact",
        description="Set a local display name for a contact",
        inputSchema={
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "Phone number in E.164 format"},
                "name": {"type": "string", "description": "Display name to set"},
            },
            "required": ["number", "name"],
        },
    ),
    Tool(
        name="update_group",
        description="Update a group's name, description, members, admins, or expiration timer",
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
            "Use list_groups to find the group_id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "Group ID to leave (get from list_groups)"},
            },
            "required": ["group_id"],
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
        description="Mark messages as unread in the local store",
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
        description="Get the avatar image for a contact or group as base64-encoded data",
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
            "Get target_author, target_timestamp, and poll_id from the poll message returned by get_conversation. "
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
                "poll_id": {"type": "integer", "description": "Poll ID from the poll message data"},
                "votes": {"type": "array", "items": {"type": "integer"}, "description": "Option indices to vote for (0-based). Single item for single-choice polls."},
                "recipient": {"type": "string", "description": "Phone number for a DM poll — provide this OR group_id"},
                "group_id": {"type": "string", "description": "Group ID for a group poll — provide this OR recipient"},
            },
            "required": ["target_author", "target_timestamp", "poll_id", "votes"],
        },
    ),
    Tool(
        name="terminate_poll",
        description=(
            "Close (terminate) a poll you created, stopping any further votes. "
            "All participants are notified that the poll has ended and can see the final results. "
            "Get target_timestamp and poll_id from the original poll message in get_conversation. "
            "Only the poll creator can terminate their own poll. "
            "Provide either recipient (DM poll) or group_id (group poll)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_author": {"type": "string", "description": "Phone number of the poll creator — must be your own number"},
                "target_timestamp": {"type": "integer", "description": "Timestamp of the poll message (from get_conversation)"},
                "poll_id": {"type": "integer", "description": "Poll ID from the original poll message data"},
                "recipient": {"type": "string", "description": "Phone number for a DM poll — provide this OR group_id"},
                "group_id": {"type": "string", "description": "Group ID for a group poll — provide this OR recipient"},
            },
            "required": ["target_author", "target_timestamp", "poll_id"],
        },
    ),
    Tool(
        name="set_expiration_timer",
        description="Set or disable the disappearing message timer for a conversation",
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
        description="List identity keys and trust levels for contacts (safety number verification)",
        inputSchema={
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "Filter to a specific contact (optional)"},
            },
        },
    ),
    Tool(
        name="trust_identity",
        description="Trust a contact's identity key after verifying their safety number",
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
        description="Export stored messages as JSON or CSV text. Optionally filter by conversation or date.",
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
        name="get_configuration",
        description="Get current Signal account configuration (read receipts, typing indicators, link previews)",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="update_configuration",
        description="Toggle Signal account settings: read receipts, typing indicators, link previews",
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
            "Once installed, use list_sticker_packs to browse pack contents, then send_sticker or "
            "send_group_sticker to send individual stickers. "
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
        name="list_attachments",
        description="List all downloaded attachments saved locally (photos, files received via Signal)",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_attachment",
        description="Get details about a specific downloaded attachment by filename",
        inputSchema={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Attachment filename (get from list_attachments)"},
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
        description="Upload and publish a sticker pack from a local manifest.json or zip file. Returns the signal.art URL.",
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
        description="List all Signal accounts (phone numbers) configured in signal-cli on this machine.",
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
            "Use get_configuration for messaging settings (read receipts, typing indicators)."
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
        description="Set the Signal registration lock PIN (protects your account if your SIM is stolen).",
        inputSchema={
            "type": "object",
            "properties": {
                "pin": {"type": "string", "description": "4–20 digit PIN"},
            },
            "required": ["pin"],
        },
    ),
    Tool(
        name="remove_pin",
        description="Remove the Signal registration lock PIN.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="start_change_number",
        description=(
            "Begin a phone number change. Signal sends a verification code to the new number via SMS "
            "(or voice if voice=true). Call finish_change_number to complete. Requires a captcha token "
            "if Signal rejects the request — solve it at https://signalcaptchas.org/challenge/generate.html"
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
        description="Complete a phone number change using the verification code received via SMS or voice.",
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
]


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    client = get_client()

    try:
        if name not in _DAEMON_FREE:
            await client.ensure_daemon()

        # Validate required parameters up front (gives clean error instead of KeyError)
        _REQUIRED: dict[str, list[str]] = {
            "send_message":         ["recipient", "message"],
            "send_group_message":   ["group_id", "message"],
            "send_note_to_self":    ["message"],
            "send_attachment":      ["recipient"],
            "send_group_attachment":["group_id"],
            "send_sticker":         ["recipient", "pack_id", "sticker_id"],
            "send_group_sticker":   ["group_id", "pack_id", "sticker_id"],
            "get_conversation":     ["recipient"],
            "search_messages":      ["query"],
            "react_to_message":     ["target_author", "target_timestamp", "emoji"],
            "set_typing":           ["recipient"],
            "get_profile":          ["number"],
            "block_contact":        ["number"],
            "unblock_contact":      ["number"],
            "remove_contact":       ["number"],
            "update_contact":       ["number", "name"],
            "create_group":         ["name", "members"],
            "join_group":           ["uri"],
            "add_device":           ["uri"],
            "remove_device":        ["device_id"],
            "delete_message":       ["recipient", "target_timestamp"],
            "delete_group_message": ["group_id", "target_timestamp"],
            "send_read_receipt":    ["sender", "timestamps"],
            "update_group":         ["group_id"],
            "leave_group":          ["group_id"],
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
            "get_user_status":      ["recipients"],
            "pin_message":                    ["target_author", "target_timestamp"],
            "unpin_message":                  ["target_author", "target_timestamp"],
            "admin_delete_message":           ["group_id", "target_author", "target_timestamp"],
            "update_device":                  ["device_id", "name"],
            "mark_as_unread":                 ["message_ids"],
            "get_avatar":                     ["identifier"],
            "send_message_request_response":  ["sender", "accept"],
            "create_poll":                    ["question", "options"],
            "vote_poll":                      ["target_author", "target_timestamp", "poll_id", "votes"],
            "terminate_poll":                 ["target_author", "target_timestamp", "poll_id"],
            "start_change_number":            ["number"],
            "finish_change_number":           ["number", "verification_code"],
            "submit_rate_limit_challenge":    ["challenge", "captcha"],
        }
        if name in _REQUIRED:
            err = _require(arguments, *_REQUIRED[name])
            if err:
                return _err(err)

        if name == "send_message":
            result = await client.send_message(
                arguments["recipient"], arguments["message"],
                quote_author=arguments.get("quote_author"),
                quote_timestamp=arguments.get("quote_timestamp"),
            )
            return _ok({"status": "sent", "timestamp": result.timestamp, "recipient": result.recipient})

        elif name == "send_group_message":
            result = await client.send_group_message(
                arguments["group_id"], arguments["message"],
                mentions=arguments.get("mentions"),
                quote_author=arguments.get("quote_author"),
                quote_timestamp=arguments.get("quote_timestamp"),
            )
            return _ok({"status": "sent", "timestamp": result.timestamp, "group_id": result.recipient})

        elif name == "send_note_to_self":
            result = await client.send_note_to_self(arguments["message"])
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
            return _ok(client.list_attachments())

        elif name == "get_attachment":
            return _ok(client.get_attachment(arguments["filename"]))

        elif name == "receive_messages":
            await client._ensure_contact_cache()
            await client._ensure_group_cache()
            try:
                timeout = int(arguments.get("timeout", 5))
            except (TypeError, ValueError):
                return _err("timeout must be an integer number of seconds")
            try:
                messages = await client.receive_messages(timeout=timeout)
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

        elif name == "list_contacts":
            contacts = await client.list_contacts(search=arguments.get("search"))
            return _ok([c.to_dict() for c in contacts])

        elif name == "list_groups":
            groups = await client.list_groups()
            return _ok([g.to_dict() for g in groups])

        elif name == "get_conversation":
            since = None
            if arguments.get("since"):
                try:
                    since = datetime.fromisoformat(arguments["since"])
                except ValueError:
                    return _err(f"Invalid since date: {arguments['since']}")
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            await client._ensure_contact_cache()
            await client._ensure_group_cache()
            messages = await client.get_conversation(
                arguments["recipient"], limit=limit, offset=offset, since=since,
            )
            total = await asyncio.to_thread(
                _store.count_conversation, arguments["recipient"], since=since
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
            await client._ensure_contact_cache()
            await client._ensure_group_cache()
            messages = await client.search_messages(
                arguments["query"],
                limit=int(arguments.get("limit", 50)),
                offset=int(arguments.get("offset", 0)),
                sender=arguments.get("sender"),
            )
            return _ok([client._enrich_message(m) for m in messages])

        elif name == "send_attachment":
            path_arg = arguments.get("paths") or arguments.get("path")
            if not path_arg:
                return _err("Either path or paths is required")
            result = await client.send_attachment(
                arguments["recipient"],
                path_arg,
                caption=arguments.get("caption", ""),
                view_once=arguments.get("view_once", False),
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
            await client.set_typing(arguments["recipient"], stop=arguments.get("stop", False))
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
            await client.remove_contact(arguments["number"])
            return _ok({"status": "removed", "number": arguments["number"]})

        elif name == "update_profile":
            await client.update_profile(
                name=arguments.get("name"),
                about=arguments.get("about"),
                avatar_path=arguments.get("avatar_path"),
                remove_avatar=arguments.get("remove_avatar", False),
            )
            return _ok({"status": "profile updated"})

        elif name == "create_group":
            result = await client.create_group(
                arguments["name"],
                arguments["members"],
                description=arguments.get("description"),
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
            await client._ensure_contact_cache()
            await client._ensure_group_cache()
            warning = await _freshen_store(client)
            limit = int(arguments.get("limit", 50))
            # Fetch one extra to detect whether more exist without a COUNT query
            messages = await client.get_unread_messages(limit=limit + 1)
            has_more = len(messages) > limit
            messages = messages[:limit]
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
            await client._ensure_contact_cache()
            await client._ensure_group_cache()
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
            await client.update_contact(arguments["number"], arguments["name"])
            return _ok({"status": "contact updated", "number": arguments["number"], "name": arguments["name"]})

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
            )
            return _ok({"status": "group updated", "group_id": arguments["group_id"]})

        elif name == "leave_group":
            await client.leave_group(arguments["group_id"])
            return _ok({"status": "left group", "group_id": arguments["group_id"]})

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

        elif name == "get_configuration":
            return _ok(await client.get_configuration())

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
            await client.add_sticker_pack(arguments["uri"])
            return _ok({"status": "installed"})

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
            if not arguments.get("confirm"):
                return _err("confirm must be true to delete all local messages")
            count = await client.clear_local_store()
            return _ok({"deleted": count, "status": "cleared"})

        elif name == "delete_local_messages":
            count = await client.delete_local_messages(arguments["recipient"])
            return _ok({"deleted": count, "status": "deleted"})

        elif name == "get_user_status":
            statuses = await client.get_user_status(arguments["recipients"])
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
                poll_id=arguments["poll_id"],
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
                poll_id=arguments["poll_id"],
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


async def serve() -> None:  # pragma: no cover
    _store.init_db()
    try:
        check_signal_cli_version()
        client = get_client()
        # Pre-warm: start daemon in background so first tool call doesn't cold-start
        await client.prewarm()
        # Pre-load contact + group names in background
        for _cache_coro in (client._ensure_contact_cache(), client._ensure_group_cache()):
            _t = asyncio.create_task(_cache_coro)
            client._background_tasks.append(_t)
        # Watchdog is already started by prewarm() via _start_watchdog() (idempotent)
    except RuntimeError as exc:
        import sys
        print(f"[signal-mcp] WARNING: {exc}", file=sys.stderr)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())
