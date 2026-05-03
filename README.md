# signal-mcp

[![Tests](https://github.com/googlarz/signal-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/googlarz/signal-mcp/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/signal-mcp)](https://pypi.org/project/Signal-MCP/)
[![Python](https://img.shields.io/pypi/pyversions/signal-mcp)](https://pypi.org/project/Signal-MCP/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**The most complete Signal MCP server and CLI.** Let Claude send and receive Signal messages, manage contacts and groups, search history, and more — all running 100% locally via [signal-cli](https://github.com/AsamK/signal-cli).

## What Claude can do

Once connected, just ask Claude naturally:

> *"Check my Signal messages and summarize what I missed"*
> *"Send Anna a message saying I'll be 10 minutes late"*
> *"Reply to Marco's last message and quote what he said"*
> *"Search my Signal history for anything about the project deadline"*
> *"Show me all my conversations and who messaged me most recently"*
> *"Create a group with Alice and Bob called 'Weekend plans'"*
> *"Save a note to myself: pick up dry cleaning Thursday"*
> *"Import all my old messages from Signal Desktop"*

## Features

- **42 MCP tools** — complete coverage of everything signal-cli exposes
- **Quoted replies & @mentions** — reply to specific messages, mention group members
- **Edit & delete messages** — fix typos, unsend mistakes
- **View-once attachments** — send photos that disappear after viewing
- **Complete conversation history** — sent and received messages stored locally in SQLite
- **Full-text search** — FTS5 index across all messages
- **Signal Desktop import** — pull your entire message history in one command (macOS)
- **Background service** — macOS LaunchAgent or Linux systemd unit captures messages automatically
- **Full CLI** — use Signal from your terminal without Claude
- **100% local** — no cloud, no third-party services, your data stays on your machine
- **Auto-starts daemon** — no manual process management needed

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
| `edit_message` | Edit a previously sent message (DM or group). Updates local store. |
| `delete_message` | Remote-delete (unsend) a sent DM. |
| `delete_group_message` | Remote-delete a sent group message. |
| `react_to_message` | React to a message with an emoji (DM or group). |
| `set_typing` | Send a typing indicator to a contact. |
| `send_read_receipt` | Mark messages as read. Also updates local store. |
| `send_sticker` | Send a sticker to a contact. |
| `send_group_sticker` | Send a sticker to a group. |

### Contacts

| Tool | Description |
|---|---|
| `list_contacts` | All contacts with names and numbers. |
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
| `search_messages` | Full-text search (FTS5) across all stored messages. |
| `store_stats` | Total message count, oldest and newest message dates. |

### Security & Devices

| Tool | Description |
|---|---|
| `list_identities` | List identity keys and trust levels (safety numbers). |
| `trust_identity` | Trust a contact's identity key after verifying their safety number. |
| `list_devices` | List all devices linked to your account. |
| `add_device` | Link a new device using a device link URI. |
| `remove_device` | Unlink a device by ID. |

### Disappearing Messages

| Tool | Description |
|---|---|
| `set_expiration_timer` | Set or disable disappearing messages for any DM or group. |

### Data & Import

| Tool | Description |
|---|---|
| `import_desktop` | Import full message history from Signal Desktop (macOS). Requires sqlcipher. |
| `list_attachments` | List all locally downloaded attachments (photos, files received via Signal). |
| `get_attachment` | Get details about a specific downloaded attachment by filename. |

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

# Contacts & groups
signal-mcp contacts
signal-mcp contacts --json
signal-mcp groups

# History & search
signal-mcp history +1234567890
signal-mcp history +1234567890 --limit 20
signal-mcp history +1234567890 --limit 20 --offset 20   # page 2
signal-mcp history +1234567890 --since 2024-01-01
signal-mcp search "keyword"
signal-mcp store-stats

# Signal Desktop import (macOS)
signal-mcp import-desktop

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
Claude Code
    │  MCP (stdio transport)
    ▼
signal-mcp serve
    ├── SQLite store  (~/.local/share/signal-mcp/messages.db)
    │     FTS5 full-text search, sent + received messages
    │
    └── signal-cli daemon  (JSON-RPC on localhost:7583)
            │  Signal protocol (libsignal)
            ▼
        Signal network
```

The signal-cli daemon starts automatically on first use and stays alive across tool calls. Received attachments are saved to `~/Downloads/signal-attachments/`.

## Development

```bash
git clone https://github.com/googlarz/signal-mcp
cd signal-mcp
uv sync --dev
uv run pytest
uv run pytest --cov --cov-report=term-missing
```

All tests are fully mocked — no signal-cli installation or Signal account required to run them.

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add new tools.

## License

MIT — see [LICENSE](LICENSE).
