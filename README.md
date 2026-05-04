# signal-mcp

[![Tests](https://github.com/googlarz/signal-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/googlarz/signal-mcp/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/signal-mcp)](https://pypi.org/project/Signal-MCP/)
[![Python](https://img.shields.io/pypi/pyversions/signal-mcp)](https://pypi.org/project/Signal-MCP/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![signal-mcp MCP server](https://glama.ai/mcp/servers/googlarz/signal-mcp/badges/card.svg)](https://glama.ai/mcp/servers/googlarz/signal-mcp)

signal-mcp is a layer on top of [signal-cli](https://github.com/AsamK/signal-cli) that adds everything it's missing: persistent message history, full-text search, a usable conversation view, contact name resolution, and an MCP server so Claude can read, search, and act on your Signal messages. 100% local — no cloud, no third-party services.

> **v1.29.0** — New `sync_desktop` / `sync-desktop`: incremental sync from Signal Desktop — fast on repeat calls, imports only new messages since the last run. Plus: incoming edits update the stored copy in-place, fixes for 500+ conversation crashes, improved store reliability.

## What signal-cli is missing — and what signal-mcp adds

**No history.** signal-cli delivers a message and forgets it. signal-mcp stores every sent and received message — including messages sent from your phone — in a local SQLite database. It stays there. You can search it, browse it, export it, and ask Claude about it.

**No search.** signal-mcp adds an FTS5 full-text index. Find any message by keyword, filter by sender, page through results.

**No conversation view.** signal-cli has no concept of a thread. signal-mcp gives you paginated history per contact or group, unread counts, and last-message previews across all your chats.

**Raw phone numbers everywhere.** signal-mcp resolves `+12025551234` → `Anna Schmidt` in all output.

**Manual daemon management.** signal-mcp auto-starts the daemon on first use, restarts it if it crashes, and can install a background service (macOS LaunchAgent or Linux systemd) that captures every incoming message — even when you're not at your computer.

## CLI and MCP: use whichever fits the task

The CLI and MCP server share the same store and daemon. You don't have to choose one:

| Task | How |
|---|---|
| Quick send from terminal or a cron job | `signal-mcp send +49... "done"` |
| Set up background message capture (once) | `signal-mcp install-service` |
| Import full history from Signal Desktop (once) | `signal-mcp import-desktop` |
| Export a conversation to CSV | `signal-mcp export --recipient +49... --format csv` |
| Check unread messages and conversations | `signal-mcp conversations` |
| "What did I miss while I was offline?" | Claude via MCP |
| "Find every message mentioning the invoice" | Claude via MCP |
| "Summarize my week with Marco and draft a reply" | Claude via MCP |
| "Who in my contacts hasn't messaged me in a month?" | Claude via MCP |
| "Send the team: standup in 5 minutes" | Claude via MCP or CLI |

## Features

- **Persistent SQLite store** — every message saved locally, survives restarts
- **FTS5 full-text search** — instant search across entire history
- **Signal Desktop import** — migrate your complete history in one command (macOS/Linux/Windows)
- **Background service** — captures messages automatically, even when Claude isn't running
- **Export** — JSON or CSV with recipient and date filters
- **Contact name resolution** — phone numbers resolved to names everywhere
- **72 MCP tools** — complete signal-cli coverage (see [coverage matrix](#signal-cli-coverage))
- **Incoming edit handling** — when contacts edit their messages, the stored copy updates in-place
- **Full CLI** — all the above without Claude, from your terminal
- **100% local** — your data never leaves your machine

## Setup

### Step 1 — Install signal-cli

signal-mcp is a front-end for [signal-cli](https://github.com/AsamK/signal-cli), which handles the Signal protocol.

**macOS**
```bash
brew install signal-cli
```

**Linux**
Download the latest release from [signal-cli releases](https://github.com/AsamK/signal-cli/releases), extract it, and put the `signal-cli` binary on your `$PATH`.

### Step 2 — Link your Signal account

signal-cli needs to be linked to your existing Signal account (the same way you'd add a linked device in Signal mobile).

```bash
signal-cli link --name "MyMac"
```

This prints a QR code in your terminal. On your phone:

> **Signal** → Settings → Linked Devices → **+** → scan the QR code

Once scanned, signal-cli is linked and ready.

### Step 3 — Install signal-mcp

**With uv** (recommended):
```bash
uv tool install signal-mcp
```

**With pip or pipx:**
```bash
pip install signal-mcp
# or
pipx install signal-mcp
```

**From source:**
```bash
git clone https://github.com/googlarz/signal-mcp
cd signal-mcp
uv tool install .
```

Verify it works:
```bash
signal-mcp status
# → Account : +1234567890
# → Daemon  : stopped (port 7583)
```

### Step 4 — Connect to Claude Code

```bash
claude mcp add signal -- signal-mcp serve
```

Restart Claude Code. Signal tools appear automatically — ask Claude *"check my Signal messages"* to confirm.

<details>
<summary>Manual config alternatives</summary>

**Claude Code** — global (`~/.claude.json`):
```json
{
  "mcpServers": {
    "signal": {
      "command": "uvx",
      "args": ["signal-mcp", "serve"]
    }
  }
}
```

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "signal": {
      "command": "uvx",
      "args": ["signal-mcp", "serve"]
    }
  }
}
```

> Claude Desktop uses a restricted PATH — `uvx` resolves the tool without needing `signal-mcp` on your shell's PATH.

**Per-project** (`.mcp.json`):
```json
{
  "mcpServers": {
    "signal": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/signal-mcp", "signal-mcp", "serve"]
    }
  }
}
```
</details>

### Step 5 — (Optional) Import Signal Desktop history

If you use Signal Desktop on macOS, import your full message history in one command:

```bash
brew install sqlcipher       # required for decryption
signal-mcp import-desktop    # macOS will prompt for Keychain access — click Allow
```

### Step 6 — (Optional) Enable background message capture

signal-cli only delivers messages when polled. Install the background service so nothing is missed:

```bash
signal-mcp install-service   # starts on login, works on macOS and Linux
```

## MCP Tools

### Messaging

| Tool | Description |
|---|---|
| `send_message` | Send a text message to a contact. Supports quoted replies (`quote_author`, `quote_timestamp`). |
| `send_group_message` | Send a text message to a group. Supports quoted replies and `@mentions`. |
| `send_attachment` | Send a file or image to a contact. Supports captions and view-once. |
| `send_group_attachment` | Send a file or image to a group. Supports captions and view-once. |
| `send_note_to_self` | Save a note to yourself (Signal's saved messages). |
| `receive_messages` | Poll for new incoming messages and delivery receipts. |
| `get_unread` | Get messages not yet marked as read from local store. |
| `edit_message` | Edit a previously sent message (DM or group). Updates local store. Incoming edits from contacts also update the stored copy in-place. |
| `delete_message` | Remote-delete (unsend) a sent DM. |
| `delete_group_message` | Remote-delete a sent group message. |
| `react_to_message` | React to a message with an emoji (DM or group). Set `remove=true` to unreact. |
| `pin_message` | Pin a message in a DM or group conversation. |
| `unpin_message` | Unpin a message in a DM or group conversation. |
| `admin_delete_message` | Group admin: delete any message in a group you administer. |
| `set_typing` | Send a typing indicator to a contact. |
| `send_read_receipt` | Mark messages as read. Also updates local store. |
| `send_sticker` | Send a sticker to a contact. |
| `send_group_sticker` | Send a sticker to a group. |

### Configuration

| Tool | Description |
|---|---|
| `get_configuration` | Read current account settings (read receipts, typing indicators, link previews). |
| `update_configuration` | Toggle read receipts, typing indicators, link previews, or sealed sender indicators. |

### Sticker Packs

| Tool | Description |
|---|---|
| `list_sticker_packs` | List all installed sticker packs with `pack_id` and sticker IDs for `send_sticker`. |
| `add_sticker_pack` | Install a sticker pack from a `signal.art` URL. |
| `get_sticker` | Retrieve a single sticker image as base64. |
| `upload_sticker_pack` | Upload and publish a sticker pack from a local manifest.json or zip. Returns the signal.art URL. |

### Contacts

| Tool | Description |
|---|---|
| `list_contacts` | All contacts with names and numbers. Supports optional `search` filter. |
| `get_profile` | Get profile info for a contact. |
| `update_contact` | Set a local display name for a contact. |
| `block_contact` | Block a contact. |
| `unblock_contact` | Unblock a contact. |
| `remove_contact` | Remove a contact from the local list. |
| `update_profile` | Update your own name, about text, or avatar. |
| `get_own_number` | Get the Signal number this server is running as. |

### Groups

| Tool | Description |
|---|---|
| `list_groups` | All groups with members and metadata. |
| `create_group` | Create a new Signal group. |
| `join_group` | Join a group via invite link. |
| `update_group` | Rename, add/remove members, promote/demote admins, set expiry timer. |
| `leave_group` | Leave a group. |

### History & Search

| Tool | Description |
|---|---|
| `list_conversations` | All conversations ordered by most recent message. |
| `get_conversation` | Message history with a contact or group. Supports `since`, `limit`, and `offset` for pagination. |
| `search_messages` | Full-text search (FTS5) across all stored messages. Supports `sender`, `limit`, and `offset`. |
| `store_stats` | Total message count, oldest and newest message dates. |
| `mark_as_unread` | Mark one or more stored messages as unread. |
| `get_user_status` | Check whether phone numbers are registered Signal users. |
| `send_sync_request` | Request sync of messages/contacts/groups from your primary device. |
| `send_contacts_sync` | Push your contacts list to all linked devices. |
| `send_message_request_response` | Accept or decline a message request from an unknown sender. |

### Security & Devices

| Tool | Description |
|---|---|
| `list_identities` | List identity keys and trust levels (safety numbers). |
| `trust_identity` | Trust a contact's identity key after verifying their safety number. |
| `list_devices` | List all devices linked to your account. |
| `add_device` | Link a new device using a device link URI. |
| `remove_device` | Unlink a device by ID. |
| `update_device` | Rename a linked device. |
| `list_accounts` | List all Signal accounts configured in signal-cli on this machine. |
| `update_account` | Update account settings: device name, discoverability, number sharing, username. |
| `set_pin` | Set the Signal registration lock PIN. |
| `remove_pin` | Remove the Signal registration lock PIN. |
| `get_avatar` | Retrieve the avatar image for a contact or group as base64. |

### Polls

| Tool | Description |
|---|---|
| `create_poll` | Create a poll in a group conversation. |
| `vote_poll` | Cast a vote on an existing poll. |
| `terminate_poll` | End a poll and prevent further votes. |

### Disappearing Messages

| Tool | Description |
|---|---|
| `set_expiration_timer` | Set or disable disappearing messages for any DM or group. |

### Data & Import

| Tool | Description |
|---|---|
| `import_desktop` | One-time full import of all historical messages from Signal Desktop. Requires sqlcipher. |
| `sync_desktop` | Incremental sync from Signal Desktop — imports only messages newer than the last sync. Fast on repeat calls. First call behaves like `import_desktop`. |
| `list_attachments` | List all locally downloaded attachments (photos, files received via Signal). |
| `get_attachment` | Get details about a specific downloaded attachment by filename. |
| `clear_local_store` | Delete ALL locally stored messages (requires `confirm: true`). Does not unsend from Signal. |
| `delete_local_messages` | Delete locally stored messages for one contact or group. |
| `export_messages` | Export stored messages as JSON or CSV. Supports `recipient` and `since` filters. |

## CLI Usage

```bash
# Status & daemon
signal-mcp status                          # account + daemon info
signal-mcp daemon                          # start daemon in foreground
signal-mcp stop                            # stop the daemon

# Send & receive
signal-mcp send +1234567890 "Hello!"
signal-mcp send-group <group_id> "Hey!"
signal-mcp note "Remember to buy milk"     # save a note to yourself
signal-mcp receive                         # poll once
signal-mcp receive --watch                 # keep watching (saves to store)

# Edit
signal-mcp edit +1234567890 <timestamp> "corrected text"
signal-mcp edit <group_id> <timestamp> "corrected text"

# Pin / unpin / admin-delete messages
signal-mcp pin +1234567890 <timestamp> +1234567890
signal-mcp unpin +1234567890 <timestamp> +1234567890
signal-mcp admin-delete <group_id> <timestamp> +1234567890

# Devices
signal-mcp update-device <device_id> "My Laptop"

# Contacts & groups
signal-mcp contacts
signal-mcp contacts --json
signal-mcp groups
signal-mcp conversations                   # list all chats with unread count + last message

# History & search
signal-mcp history +1234567890
signal-mcp history +1234567890 --limit 20
signal-mcp history +1234567890 --limit 20 --offset 20   # page 2
signal-mcp history +1234567890 --since 2024-01-01
signal-mcp search "keyword"
signal-mcp search "keyword" --sender +1234567890   # restrict to one contact
signal-mcp search "keyword" --limit 20
signal-mcp store-stats

# Export
signal-mcp export                                          # all messages as JSON to stdout
signal-mcp export messages.json                            # save to file
signal-mcp export messages.csv --format csv                # CSV format
signal-mcp export --recipient +1234567890 --format csv     # one conversation
signal-mcp export --since 2024-01-01                       # messages from date

# Signal Desktop import (macOS) — one-time full import
signal-mcp import-desktop
signal-mcp sync-desktop                    # incremental: only new messages since last sync

# Background service (macOS LaunchAgent or Linux systemd)
signal-mcp install-service    # auto-starts on login, captures all messages
signal-mcp uninstall-service

# MCP server (for Claude Code)
signal-mcp serve
```

## Getting full message history

signal-cli only delivers new messages — it has no history API. Two ways to get history:

**Going forward** (captures everything from now on):
```bash
signal-mcp install-service   # background watcher, auto-starts on login
```

**Retroactively** (imports everything from Signal Desktop):
```bash
signal-mcp import-desktop    # macOS will prompt for Keychain access — click Allow
```

Run both for complete coverage.

## Architecture

```
                    ┌─────────────────────────────────┐
                    │  signal-cli daemon  (:7583)      │
                    │  Signal protocol / libsignal     │
                    └──────────┬──────────────┬────────┘
                               │ sends/receives│
                    ┌──────────▼──────────┐   │ (when no service)
                    │  background service  │   │
                    │  LaunchAgent/systemd │   │
                    └──────────┬──────────┘   │
                               │ writes        │
                    ┌──────────▼──────────────▼────────┐
                    │  SQLite store                     │
                    │  ~/.local/share/signal-mcp/       │
                    │  messages.db  (FTS5 indexed)      │
                    └────────────────┬─────────────────┘
                                     │ reads/writes
                   ┌─────────────────┼─────────────────┐
                   │                 │                  │
        ┌──────────▼──────┐ ┌────────▼───────┐ ┌──────▼──────────┐
        │  Claude Code /  │ │  signal-mcp    │ │  signal-mcp CLI │
        │  Claude Desktop │ │  serve (MCP)   │ │  (terminal)     │
        │  (asks Claude)  │ └────────────────┘ └─────────────────┘
        └─────────────────┘
```

**How it works:**

`get_unread` is the primary "check for new messages" tool. If the background service is installed, it reads straight from the store (the service keeps it up to date). Otherwise it polls signal-cli first (debounced to once every 30 seconds) and includes a `_warning` suggesting `signal-mcp install-service`. `list_conversations` is a pure store read — fast, no polling.

The daemon starts automatically on first use. Attachments are saved to `~/Downloads/signal-attachments/`.

## signal-cli Coverage

signal-mcp wraps the [signal-cli JSON-RPC daemon](https://github.com/AsamK/signal-cli/blob/master/man/signal-cli.1.adoc). Here's what is and isn't covered:

### Covered (72 tools)

| signal-cli command | signal-mcp tool |
|---|---|
| `send` | `send_message`, `send_group_message`, `send_note_to_self`, `send_attachment`, `send_group_attachment`, `send_sticker`, `send_group_sticker` |
| `receive` | `receive_messages` (streaming), `get_unread` |
| `listContacts` | `list_contacts` |
| `listGroups` | `list_groups` |
| `listDevices` | `list_devices` |
| `listIdentities` | `list_identities` |
| `listStickerPacks` | `list_sticker_packs` |
| `getUserStatus` | `get_user_status` |
| `getAttachment` | `get_attachment`, `list_attachments` |
| `getAvatar` | `get_avatar` |
| `block` / `unblock` | `block_contact` / `unblock_contact` |
| `removeContact` | `remove_contact` |
| `updateContact` | `update_contact` |
| `trust` | `trust_identity` |
| `joinGroup` | `join_group` |
| `quitGroup` | `leave_group` |
| `updateGroup` | `update_group`, `create_group` |
| `addDevice` / `removeDevice` / `updateDevice` | `add_device` / `remove_device` / `update_device` |
| `sendReaction` | `react_to_message` |
| `sendTyping` | `set_typing` |
| `sendReceipt` | `send_read_receipt` |
| `sendSyncRequest` | `send_sync_request` |
| `sendContacts` | `send_contacts_sync` |
| `sendAdminDelete` | `admin_delete_message` |
| `sendPinMessage` / `sendUnpinMessage` | `pin_message` / `unpin_message` |
| `sendPollCreate` / `sendPollVote` / `sendPollTerminate` | `create_poll` / `vote_poll` / `terminate_poll` |
| `sendMessageRequestResponse` | `send_message_request_response` |
| `remoteDelete` | `delete_message`, `delete_group_message` |
| `editMessage` | `edit_message` |
| `updateProfile` | `update_profile` |
| `updateConfiguration` | `update_configuration`, `get_configuration` |
| `addStickerPack` | `add_sticker_pack` |
| `getSticker` | `get_sticker` |
| `uploadStickerPack` | `upload_sticker_pack` |
| `listAccounts` | `list_accounts` |
| `updateAccount` | `update_account` |
| `setPin` / `removePin` | `set_pin` / `remove_pin` |
| `startChangeNumber` / `finishChangeNumber` | `start_change_number` / `finish_change_number` |
| `submitRateLimitChallenge` | `submit_rate_limit_challenge` |

Plus tools with no direct signal-cli equivalent: `get_conversation`, `search_messages`, `list_conversations`, `store_stats`, `import_desktop`, `sync_desktop`, `export_messages`, `mark_as_unread`, `clear_local_store`, `delete_local_messages`, `prune_store`.

### Not covered

These commands are deliberately excluded — they are not feasible to implement as MCP tools:

| signal-cli command | Why |
|---|---|
| `acceptCall` / `hangupCall` / `rejectCall` / `startCall` / `listCalls` | Voice/video calls require WebRTC and an active media stack — not feasible via MCP |
| `register` / `verify` / `link` / `unregister` | One-time account setup; must be done before installing signal-mcp |
| `deleteLocalAccountData` | Irreversibly destroys all local Signal data; too destructive to expose |
| `sendPaymentNotification` | MobileCoin payments (requires a funded wallet; out of scope) |

## Development

```bash
git clone https://github.com/googlarz/signal-mcp
cd signal-mcp
uv sync --dev
uv run pytest
uv run pytest --cov --cov-report=term-missing
```

573 tests, 100% line coverage across all modules. All tests are fully mocked — no signal-cli installation or Signal account required to run them.

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add new tools.

## License

MIT — see [LICENSE](LICENSE).
