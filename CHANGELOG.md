# Changelog

All notable changes to signal-mcp are documented here.

## [1.7.0] — 2026-05-03

### New tools (2 → 50 total)

- `export_messages` — export stored messages as JSON or CSV; optionally filter by conversation or date
- `search_messages` gains `sender` parameter — restrict full-text search results to one phone number

### Reliability

- **Daemon auto-restart** — on `ConnectError` during an RPC call, signal-mcp now calls `ensure_daemon()` before retrying instead of sleeping; recovers from crashed daemons without user intervention

### CLI

- `signal-mcp export [OUTPUT]` — export all (or filtered) stored messages to a file or stdout; supports `--format json|csv`, `--recipient`, `--since`

### Stats
- 50 MCP tools total
- 201 tests

---

## [1.6.0] — 2026-05-03

### Security

- **Rate limiting** — all send operations (message, group message, attachment, sticker, note-to-self) share a token-bucket limiter of 20/minute; prevents a runaway session from spamming hundreds of messages
- **E.164 phone number validation** — `send_message`, `send_attachment`, `send_sticker` validate the recipient format upfront and return a clear error instead of passing garbage to signal-cli
- **DB file permissions** — `messages.db` is created with `0600` (owner read/write only); previously world-readable on default umask

### UX

- **`list_conversations` includes contact names** — every direct conversation now has a `"name"` field with the resolved display name; no need to cross-reference `list_contacts`
- **`get_conversation` returns `total`, `has_more`, `limit`, `offset`** — Claude can now tell users "showing 50 of 312 messages" and know when to paginate
- **Actionable identity-key error messages** — "Untrusted identity key" errors now include `→ Use trust_identity to resolve` guidance; rate-limit, not-a-member, invalid-number errors also get hints

### New tools (2 → 48 total)

- `clear_local_store` — delete ALL locally stored messages (requires `confirm: true`); does not unsend anything from Signal
- `delete_local_messages` — delete locally stored messages for one contact or group

### Performance

- **SQLite connection reuse** — `store.py` uses `threading.local()` to cache one connection per thread; eliminates open/close overhead on every DB call

### Cross-platform

- **Windows Signal Desktop import** — DPAPI key decryption via `ctypes.windll.crypt32.CryptUnprotectData`; handles v10/v11 Electron key prefixes

### Stats
- 48 MCP tools total
- 188 tests

---

## [1.5.0] — 2026-05-03

### New tools (4)
- `get_configuration` — read current account settings (read receipts, typing indicators, link previews)
- `update_configuration` — toggle account settings
- `list_sticker_packs` — list all installed sticker packs with pack_id/sticker_id values needed by `send_sticker`
- `add_sticker_pack` — install a sticker pack from a `signal.art` URL

### Input validation
- All tool handlers now validate required parameters up front; missing params return a clean `"Missing required parameter(s): ..."` error instead of a bare `KeyError`

### Cross-platform Signal Desktop import
- `import-desktop` now works on **Linux** as well as macOS
  - Linux path: `~/.config/Signal/` (respects `$XDG_CONFIG_HOME`)
  - Linux keychain: tries `secret-tool` (GNOME Keyring / libsecret), falls back to `"peanuts"` (Signal Desktop's hardcoded fallback password)
  - `import_from_desktop()` accepts a `signal_dir` override for custom install paths
  - Result dict now includes `"platform"` and `"source"` fields

### Streaming receive
- New `SignalClient.receive_stream(poll_interval)` async generator — yields messages continuously, handles errors with back-off; used by `receive --watch`
- CLI `receive --watch` now uses `receive_stream` with a configurable `--interval` option (default 2 s)

### Stats
- 46 MCP tools total
- 174 tests

---

## [1.4.1] — 2026-05-03

### Performance

- **SQLite WAL mode** — `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL` on every connection; eliminates writer-reader contention and makes concurrent reads non-blocking
- **Compound indexes** — added `(sender, timestamp)`, `(recipient, timestamp)`, `(group_id, timestamp)` indexes; `get_conversation` no longer needs a full table scan to sort messages
- **Concurrent RPC** — `asyncio.Lock` replaced with `asyncio.Semaphore(4)`; up to 4 RPCs can run simultaneously (e.g. a long `receive_messages` poll no longer blocks all other tools)
- **Per-call HTTP timeouts** — `_rpc()` now accepts a `timeout` parameter (default 10 s); `receive_messages` passes `poll_timeout + 5 s` so it never races against its own poll window; health-check pings use 3 s
- **Contact cache TTL** — cache now expires after 5 minutes (`_CACHE_TTL = 300 s`); contact name changes are reflected mid-session instead of being frozen for the entire server lifetime
- **Non-blocking SQLite** — all store calls inside async methods are now wrapped with `asyncio.to_thread`; the event loop is no longer blocked during DB reads/writes (`get_conversation`, `search_messages`, `save_message`, `mark_as_read`, …)

## [1.4.0] — 2026-05-03

### Performance
- **Pre-warm daemon at server start** — `signal-mcp serve` now starts signal-cli in the background immediately, eliminating the ~15s cold-start timeout on the first MCP tool call
- **Watchdog task** — auto-restarts the daemon if it crashes mid-session
- **Concurrent RPC safety** — `asyncio.Lock` prevents interleaved requests when multiple tools run concurrently (e.g. from Cowork scheduled tasks)
- **Single-flight daemon startup** — `asyncio.Lock` on `ensure_daemon` prevents concurrent callers from spawning multiple signal-cli processes

### UX
- **Contact name resolution** — `get_conversation`, `get_unread`, `search_messages`, and `receive_messages` now include `sender_name` / `recipient_name` fields with resolved display names
- **Auto-mark as read** — `get_conversation` now marks returned received messages as read in the local store (like every Signal client)
- **signal-cli version check** — server startup fails fast with a helpful message if signal-cli is missing, too old, or not working

### New tools (4)
- `send_sticker` — send a sticker to a DM contact
- `send_group_sticker` — send a sticker to a group
- `list_attachments` — list all locally downloaded attachments (photos, files received via Signal)
- `get_attachment` — get details about a specific downloaded attachment

### Bug fixes (from Codex review)
- Contact name cache now retries on RPC failure instead of permanently freezing empty
- `get_attachment` rejects path traversal filenames (`../secret`)
- Sticker sends now persist to local store (consistent with all other send paths)
- `check_signal_cli_version` properly handles timeout and non-zero exit codes
- Background tasks (watchdog, cache pre-load) are tracked and cancelled on server shutdown

### Stats
- 42 MCP tools total
- 161 tests

---

## [1.3.3] — 2026-05-03

### Changes
- Now available on PyPI: `pip install signal-mcp`

---

## [1.3.2] — 2026-05-03

### Bug fixes
- `send_group_attachment` now saves sent record to local store (was inconsistent with `send_attachment`)
- CLI `receive --watch` no longer prints blank lines for delivery/read receipts — shows a clean receipt indicator instead

### Documentation
- README CLI section updated with `note`, `edit`, and `--offset` / `--since` examples

---

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
