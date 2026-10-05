"""MCP tool annotations (readOnlyHint / destructiveHint / idempotentHint / openWorldHint).

One table for every tool, kept out of server.py so tool descriptions can be edited
without touching it. tests/test_tool_annotations.py fails if a tool is added without
being classified here, and checks the table against the SIGNAL_MCP_READONLY allowlist.

Meaning of the hints (MCP spec):
  readOnly     the tool does not change anything (local store, files or the Signal account)
  destructive  it can delete or overwrite data that cannot simply be recreated
  idempotent   repeating the same call has no additional effect
  openWorld    it talks to Signal's servers / other people, not only local data
"""

from mcp.types import ToolAnnotations

# Changes nothing anywhere.
READ_ONLY = {
    "list_contacts", "find_contact", "list_groups", "list_conversations", "search_messages",
    "export_messages", "store_stats", "get_own_number", "list_identities", "list_accounts",
    "list_attachments", "list_sticker_packs", "list_scheduled_messages", "get_webhook",
    "get_profile", "get_user_status", "list_devices", "get_avatar", "get_sticker",
}

# Can delete or overwrite something that cannot simply be recreated.
DESTRUCTIVE = {
    "edit_message", "delete_message", "delete_group_message", "admin_delete_message",
    "delete_local_messages", "clear_local_store", "prune_store", "leave_group",
    "terminate_group", "terminate_poll", "remove_contact", "remove_device", "remove_pin",
    "update_group", "update_account", "cancel_scheduled_message",
}

# Repeating the identical call changes nothing further.
IDEMPOTENT = READ_ONLY | {
    "get_conversation", "get_attachment", "edit_message", "delete_message", "delete_group_message",
    "admin_delete_message", "delete_local_messages", "clear_local_store", "prune_store",
    "leave_group", "terminate_group", "remove_contact", "remove_device", "remove_pin",
    "update_group", "update_account", "update_profile", "update_contact", "update_device",
    "update_configuration", "block_contact", "unblock_contact", "mark_as_unread", "pin_message",
    "unpin_message", "react_to_message", "vote_poll", "set_expiration_timer", "trust_identity",
    "set_pin", "set_typing", "set_webhook", "send_read_receipt", "send_message_request_response",
    "send_contacts_sync", "send_sync_request", "import_desktop", "sync_desktop",
    "cancel_scheduled_message", "add_sticker_pack", "join_group",
}

# Local only: no request to Signal's servers and nobody else is contacted.
LOCAL_ONLY = {
    "list_contacts", "find_contact", "list_groups", "list_conversations", "search_messages",
    "export_messages", "store_stats", "get_own_number", "list_identities", "list_accounts",
    "list_attachments", "list_sticker_packs", "list_scheduled_messages", "get_webhook",
    "get_conversation", "get_attachment", "mark_as_unread", "delete_local_messages",
    "clear_local_store", "prune_store", "set_webhook", "schedule_message",
    "cancel_scheduled_message", "import_desktop", "sync_desktop", "trust_identity",
}


def annotations_for(name: str) -> ToolAnnotations:
    read_only = name in READ_ONLY
    return ToolAnnotations(
        title=name.replace("_", " ").capitalize(),
        read_only_hint=read_only,
        destructive_hint=False if read_only else name in DESTRUCTIVE,
        idempotent_hint=name in IDEMPOTENT,
        open_world_hint=name not in LOCAL_ONLY,
    )
