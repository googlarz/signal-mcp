"""MCP server exposing all Signal tools to Claude."""

import json
from datetime import datetime

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .client import SignalClient, SignalError
from . import store as _store

app = Server("signal-mcp")

_client: SignalClient | None = None

# Tools that don't need the signal-cli daemon (read from local store only)
_DAEMON_FREE = {
    "import_desktop", "store_stats", "list_conversations",
    "get_conversation", "search_messages", "get_unread",
}


def get_client() -> SignalClient:
    global _client
    if _client is None:
        _client = SignalClient()
    return _client


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=f"Error: {msg}")]


# ── Tool definitions ───────────────────────────────────────────────────────────

TOOLS = [
    Tool(
        name="send_message",
        description="Send a text message to a Signal contact",
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Phone number in E.164 format (e.g. +1234567890)"},
                "message": {"type": "string", "description": "Message text to send"},
            },
            "required": ["recipient", "message"],
        },
    ),
    Tool(
        name="send_group_message",
        description="Send a text message to a Signal group",
        inputSchema={
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "Group ID (get from list_groups)"},
                "message": {"type": "string", "description": "Message text to send"},
            },
            "required": ["group_id", "message"],
        },
    ),
    Tool(
        name="receive_messages",
        description="Poll for new incoming Signal messages",
        inputSchema={
            "type": "object",
            "properties": {
                "timeout": {"type": "integer", "description": "Seconds to wait for messages (default: 5)", "default": 5},
            },
        },
    ),
    Tool(
        name="list_contacts",
        description="List all Signal contacts with names and phone numbers",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_groups",
        description="List all Signal groups with members",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_conversation",
        description="Get recent message history with a contact or group from local store",
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Phone number or group ID"},
                "limit": {"type": "integer", "description": "Max messages to return (default: 50)", "default": 50},
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
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="send_attachment",
        description="Send a file or image to a Signal contact",
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Phone number in E.164 format"},
                "path": {"type": "string", "description": "File path (absolute, relative, or ~/path)"},
                "caption": {"type": "string", "description": "Optional caption text", "default": ""},
            },
            "required": ["recipient", "path"],
        },
    ),
    Tool(
        name="send_group_attachment",
        description="Send a file or image to a Signal group",
        inputSchema={
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "Group ID (get from list_groups)"},
                "path": {"type": "string", "description": "File path (absolute, relative, or ~/path)"},
                "caption": {"type": "string", "description": "Optional caption text", "default": ""},
            },
            "required": ["group_id", "path"],
        },
    ),
    Tool(
        name="react_to_message",
        description="React to a Signal message with an emoji (DM or group)",
        inputSchema={
            "type": "object",
            "properties": {
                "target_author": {"type": "string", "description": "Phone number of the message author"},
                "target_timestamp": {"type": "integer", "description": "Timestamp of the message to react to"},
                "emoji": {"type": "string", "description": "Emoji to react with (e.g. '👍')"},
                "recipient": {"type": "string", "description": "Phone number for DM reactions"},
                "group_id": {"type": "string", "description": "Group ID for group reactions"},
            },
            "required": ["target_author", "target_timestamp", "emoji"],
        },
    ),
    Tool(
        name="set_typing",
        description="Send a typing indicator to a contact",
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Phone number in E.164 format"},
                "stop": {"type": "boolean", "description": "True to stop typing indicator (default: False)", "default": False},
            },
            "required": ["recipient"],
        },
    ),
    Tool(
        name="get_profile",
        description="Get profile information for a Signal contact",
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
        description="Block a Signal contact",
        inputSchema={
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "Phone number to block"},
            },
            "required": ["number"],
        },
    ),
    Tool(
        name="store_stats",
        description="Get statistics about locally stored messages (count, date range)",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_unread",
        description="Get messages not yet marked as read from local store",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max messages to return (default: 50)", "default": 50},
            },
        },
    ),
    Tool(
        name="import_desktop",
        description="Import all historical messages from Signal Desktop app (macOS). Requires sqlcipher and Keychain access.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_conversations",
        description="List all conversations (direct and group) ordered by most recent message",
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
        description="Update a group's name, description, members, or expiration timer",
        inputSchema={
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "Group ID to update"},
                "name": {"type": "string", "description": "New group name"},
                "description": {"type": "string", "description": "New group description"},
                "add_members": {"type": "array", "items": {"type": "string"}, "description": "Phone numbers to add"},
                "remove_members": {"type": "array", "items": {"type": "string"}, "description": "Phone numbers to remove"},
                "expiration_seconds": {"type": "integer", "description": "Disappearing message timer in seconds (0 to disable)"},
            },
            "required": ["group_id"],
        },
    ),
    Tool(
        name="leave_group",
        description="Leave a Signal group",
        inputSchema={
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "Group ID to leave"},
            },
            "required": ["group_id"],
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


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    client = get_client()

    try:
        if name not in _DAEMON_FREE:
            await client.ensure_daemon()

        if name == "send_message":
            result = await client.send_message(arguments["recipient"], arguments["message"])
            return _ok({"status": "sent", "timestamp": result.timestamp, "recipient": result.recipient})

        elif name == "send_group_message":
            result = await client.send_group_message(arguments["group_id"], arguments["message"])
            return _ok({"status": "sent", "timestamp": result.timestamp, "group_id": result.recipient})

        elif name == "receive_messages":
            messages = await client.receive_messages(timeout=arguments.get("timeout", 5))
            return _ok([m.to_dict() for m in messages])

        elif name == "list_contacts":
            contacts = await client.list_contacts()
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
            messages = await client.get_conversation(
                arguments["recipient"],
                limit=arguments.get("limit", 50),
                since=since,
            )
            return _ok([m.to_dict() for m in messages])

        elif name == "search_messages":
            messages = await client.search_messages(arguments["query"])
            return _ok([m.to_dict() for m in messages])

        elif name == "send_attachment":
            result = await client.send_attachment(
                arguments["recipient"],
                arguments["path"],
                caption=arguments.get("caption", ""),
            )
            return _ok({"status": "sent", "timestamp": result.timestamp})

        elif name == "send_group_attachment":
            result = await client.send_group_attachment(
                arguments["group_id"],
                arguments["path"],
                caption=arguments.get("caption", ""),
            )
            return _ok({"status": "sent", "timestamp": result.timestamp})

        elif name == "react_to_message":
            await client.react_to_message(
                target_author=arguments["target_author"],
                target_timestamp=arguments["target_timestamp"],
                emoji=arguments["emoji"],
                recipient=arguments.get("recipient"),
                group_id=arguments.get("group_id"),
            )
            return _ok({"status": "reaction sent"})

        elif name == "set_typing":
            await client.set_typing(arguments["recipient"], stop=arguments.get("stop", False))
            return _ok({"status": "typing indicator sent"})

        elif name == "get_profile":
            contact = await client.get_profile(arguments["number"])
            return _ok(contact.to_dict())

        elif name == "block_contact":
            await client.block_contact(arguments["number"])
            return _ok({"status": "blocked", "number": arguments["number"]})

        elif name == "get_unread":
            messages = client.get_unread_messages(limit=arguments.get("limit", 50))
            return _ok([m.to_dict() for m in messages])

        elif name == "store_stats":
            return _ok(_store.get_stats())

        elif name == "import_desktop":
            from .desktop import import_from_desktop, DesktopImportError
            try:
                result = import_from_desktop()
                return _ok(result)
            except DesktopImportError as e:
                return _err(str(e))

        elif name == "list_conversations":
            return _ok(client.list_conversations())

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
            )
            return _ok({"status": "group updated", "group_id": arguments["group_id"]})

        elif name == "leave_group":
            await client.leave_group(arguments["group_id"])
            return _ok({"status": "left group", "group_id": arguments["group_id"]})

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

        else:
            return _err(f"Unknown tool: {name}")

    except SignalError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"Unexpected error: {e}")


async def serve() -> None:
    _store.init_db()
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())
