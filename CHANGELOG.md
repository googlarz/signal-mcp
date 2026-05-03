# Changelog

All notable changes to signal-mcp are documented here.

## [1.3.1] — 2026-05-03

### Bug fixes
- `edit_message` now updates the local SQLite store (body + FTS index) — history was showing stale text after edits
- `delete_group_message` — added missing server-level test

### CLI additions
- `signal-mcp note "message"` — send a note to yourself
- `signal-mcp edit <recipient> <timestamp> <message>` — edit a sent message
- `signal-mcp history` — new `--offset` option for pagination

### PyPI
- README documents the one-time trusted publisher setup on pypi.org

---

## [1.3.0] — 2026-05-03

### New tools (2)
- `send_note_to_self` — save a note to yourself (Signal's saved messages)
- `edit_message` — edit a previously sent message (DM or group)

### New capabilities on existing tools
- `send_message` / `send_group_message` — quoted replies (`quote_author` + `quote_timestamp`)
- `send_group_message` — @mention support (`mentions` array)
- `send_attachment` / `send_group_attachment` — view-once flag (`view_once: true`)
- `get_conversation` — pagination via `offset` parameter
- `update_group` — group admin management (`add_admins`, `remove_admins`)

### Reliability
- JSON-RPC client retries once on `ConnectError` before raising (handles transient daemon restarts)

### Delivery receipts
- `receive_messages` now surfaces delivery and read receipts as messages with `receipt_type: "DELIVERY"` or `"READ"` — receipts are not stored locally

### PyPI
- Added GitHub Actions trusted publisher workflow — `pip install signal-mcp` once configured on PyPI

### Stats
- 38 MCP tools total
- 131 tests

---

## [1.2.0] — 2026-05-03

### New tools (9)
- `unblock_contact` — unblock a previously blocked contact
- `remove_contact` — remove a contact from local list
- `update_profile` — update your own Signal name, about text, or avatar
- `create_group` — create a new Signal group
- `join_group` — join a group via invite link
- `list_devices` — list all devices linked to your account
- `add_device` — link a new device
- `remove_device` — unlink a device by ID
- `get_own_number` — get the Signal number this server is running as

### Improvements
- `send_read_receipt` now also marks messages as read in local store
- `send_attachment` now saves a sent record to local store (conversation history)
- `install-service` / `uninstall-service` now work on both macOS (LaunchAgent) and Linux (systemd user unit)
- 36 MCP tools total

---

## [1.1.0] — 2026-05-03

### Bug fixes
- `send_group_attachment` — was using wrong RPC method (`sendGroupMessage` instead of `send`) and passing `groupId` as a list instead of a string
- `get_conversation` — outgoing DMs were invisible; added `recipient` column to store with auto-migration for existing databases
- `get_unread` — was polling the network and filtering by a flag that was never set; now queries `is_read=0` messages from local store
- `react_to_message` — silently failed for groups; now accepts `group_id` parameter
- `send_read_receipt` — `recipient` must be an array per signal-cli JSON-RPC spec
- Desktop import — temp plaintext DB file could leak if message parsing raised an exception
- `history` / `search` CLI — were calling `ensure_daemon()` for store-only reads (slow, unnecessary)
- `send_attachment` — `~` and relative paths not expanded before sending to signal-cli

### New tools (2)
- `update_group` — rename a group, add/remove members, set disappearing message timer
- `set_expiration_timer` — set or disable disappearing messages for any DM or group

### New features
- `get_conversation` and `signal-mcp history` — new `since` parameter (ISO datetime or `YYYY-MM-DD`) to filter messages by date
- 27 MCP tools total

### Performance
- `init_db()` now runs only once per process (guarded by `_initialized` flag); no-op on repeat calls
- `detect_account()` result cached at module level — no repeated subprocess spawns
- `get_conversation` and `search_messages` no longer start the daemon (store-only reads)
- JSON-RPC request IDs now use an incrementing counter — no collision risk under concurrent calls

### Tests
- 99 tests total (was 74)
- Full `store.py` coverage: save, get_conversation (both directions), get_unread, mark_as_read, list_conversations, search, stats, schema migration
- RPC param shape tests for all fixed methods
- All tests use isolated temp databases — no global state leakage between tests

---

## [1.0.0] — 2026-05-03

Initial release.

### Features
- 25 MCP tools: send, receive, contacts, groups, conversations, history, search, attachments, reactions, typing, delete (unsend), read receipts, contact rename, leave group, identity/trust management
- Local SQLite store with FTS5 full-text search
- Sent messages saved to store for two-sided conversation history
- Signal Desktop import — decrypt and import full message history from macOS Signal Desktop app
- macOS LaunchAgent background service for continuous message capture
- Full CLI: send, receive, contacts, groups, history, search, daemon management
- Auto-starts signal-cli daemon on first use
- 74 tests, fully mocked — no signal-cli or Signal account required to run tests
