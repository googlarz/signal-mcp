# signal-mcp

[![Tests](https://github.com/googlarz/signal-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/googlarz/signal-mcp/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/signal-mcp)](https://pypi.org/project/signal-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/signal-mcp)](https://pypi.org/project/signal-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**The most complete Signal MCP server and CLI.** Let Claude send and receive Signal messages, manage contacts and groups, search history, and more — all running 100% locally via [signal-cli](https://github.com/AsamK/signal-cli).

## What Claude can do

Once connected, just ask Claude naturally:

> *"Check my Signal messages and summarize what I missed"*
> *"Send Anna a message saying I'll be 10 minutes late"*
> *"Search my Signal history for anything about the meeting"*
> *"Show me all my conversations and who messaged me most recently"*
> *"Import all my old messages from Signal Desktop"*

## Features

- **38 MCP tools** — full coverage of everything signal-cli supports
- **Complete conversation history** — both sent and received messages stored locally
- **Full-text search** — FTS5 SQLite index across all messages
- **Signal Desktop import** — pull your entire message history in one command
- **Background service** — macOS LaunchAgent or Linux systemd unit captures messages automatically
- **Full CLI** — use Signal from your terminal without Claude
- **100% local** — no cloud, no third-party services, your data stays on your machine
- **Auto-starts daemon** — no manual process management

## Prerequisites

```bash
# 1. Install signal-cli
brew install signal-cli

# 2. Link to your existing Signal account
signal-cli link --name "MyMac"
# Scan the QR code in Signal mobile: Settings → Linked Devices → +

# 3. (Optional) For Signal Desktop history import
brew install sqlcipher
```

## Install

```bash
git clone https://github.com/googlarz/signal-mcp
cd signal-mcp
uv tool install .
```

## Connect to Claude Code

```bash
# Recommended — one command:
claude mcp add signal -- signal-mcp serve
```

Or manually add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "signal": {
      "command": "signal-mcp",
      "args": ["serve"]
    }
  }
}
```

Or per-project in `.mcp.json` (point to your clone):

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

Restart Claude Code. Signal tools will appear automatically.

## MCP Tools

| Tool | Description |
|---|---|
| `send_message` | Send text to a contact |
| `send_group_message` | Send text to a group |
| `receive_messages` | Poll for new messages |
| `list_contacts` | All contacts with names and numbers |
| `list_groups` | All groups with members |
| `list_conversations` | All conversations ordered by most recent |
| `get_conversation` | Message history with a contact or group (supports `since` filter) |
| `search_messages` | Full-text search across all stored messages |
| `send_attachment` | Send a file or image to a contact |
| `send_group_attachment` | Send a file or image to a group |
| `react_to_message` | React with an emoji |
| `set_typing` | Send a typing indicator |
| `get_profile` | Contact profile info |
| `get_unread` | Unread messages only |
| `block_contact` | Block a contact |
| `delete_message` | Remote-delete (unsend) a sent message |
| `delete_group_message` | Remote-delete a message from a group |
| `send_read_receipt` | Mark messages as read |
| `update_contact` | Set a local display name for a contact |
| `leave_group` | Leave a Signal group |
| `list_identities` | List identity keys and trust levels |
| `trust_identity` | Trust a contact's identity key |
| `update_group` | Rename group, add/remove members, set expiry timer |
| `create_group` | Create a new Signal group |
| `join_group` | Join a group via invite link |
| `set_expiration_timer` | Set or disable disappearing messages |
| `unblock_contact` | Unblock a previously blocked contact |
| `remove_contact` | Remove a contact from local list |
| `update_profile` | Update your own name, about text, or avatar |
| `list_devices` | List all linked devices on your account |
| `add_device` | Link a new device |
| `remove_device` | Unlink a device |
| `get_own_number` | Get your own Signal phone number |
| `store_stats` | Stored message count and date range |
| `import_desktop` | Import full history from Signal Desktop |
| `send_note_to_self` | Save a note to yourself (saved messages) |
| `edit_message` | Edit a previously sent message |

## CLI Usage

```bash
# Status
signal-mcp status                          # account + daemon info
signal-mcp daemon                          # start daemon in foreground
signal-mcp stop                            # stop the daemon

# Send & receive
signal-mcp send +1234567890 "Hello!"
signal-mcp send-group <group_id> "Hey!"
signal-mcp receive                         # poll once
signal-mcp receive --watch                 # keep watching (saves to store)

# Contacts & groups
signal-mcp contacts
signal-mcp contacts --json
signal-mcp groups

# History & search
signal-mcp history +1234567890
signal-mcp history +1234567890 --limit 20
signal-mcp search "keyword"
signal-mcp store-stats

# Signal Desktop import (macOS — full history)
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

The signal-cli daemon is started automatically on first use and kept alive across tool calls. Received attachments are saved to `~/Downloads/signal-attachments/`.

## Development

```bash
git clone https://github.com/googlarz/signal-mcp
cd signal-mcp
uv sync --dev
uv run pytest
uv run pytest --cov --cov-report=term-missing
```

All tests are fully mocked — no signal-cli installation or Signal account required.

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add new tools.

## License

MIT — see [LICENSE](LICENSE).
