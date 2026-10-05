# Changelog

All notable changes to signal-mcp are documented here.

## [Unreleased]

Closes the gap between signal-cli and the MCP tools: a command-by-command and field-by-field comparison found that signal-mcp discarded most incoming-message data and exposed only part of several commands' options.

### Added

- **Incoming messages keep everything signal-cli reports**: `mentions` (plus `body_resolved` with `@name` in place of the placeholder; UTF-16 offsets handled), `text_styles`, link `previews`, full `quote` (author, text), `voice_note`, `sticker`, `payment`, shared contacts, polls (`poll_create` / `poll_vote` / `poll_terminate`), pins/unpins, story replies, and event flags. Stored in one new nullable `extras` JSON column, added by a guarded migration (existing databases keep working).
- **Group member labels**: `list_groups` returns each member's `label` / `label_emoji`; `update_group` sets your own (`member_label`, `member_label_emoji`). Also exposed: pending, requesting and banned members, permissions, invite link, termination state, and a `group_id` filter.
- **`update_group`**: avatar, ban/unban, reset invite link, the three permission settings. `create_group` takes an avatar.
- **`terminate_group`** (new, irreversible, requires `confirm: true`) and **`send_story`** (new).
- **Contacts and profile**: nickname, note, username, archived/hidden/unregistered and more on contacts; `update_contact` (given/family/nick names, note), `update_profile` (given/family name, about emoji, MobileCoin address), `remove_contact` (`forget`, `hide`), `get_user_status` by username, typing indicators in groups. `list_contacts` / `find_contact` take `all_recipients`, and the name cache now uses it, so group members who are not in your address book resolve to their profile names instead of a bare UUID.
- **Sending**: link previews, voice-note flag, quoted text/mentions/styles, `no_urgent`, `notify_self`, `end_session`, send by username, story replies; `max_messages` and `ignore_*` for `receive_*`.
- **`get_attachment`** falls back to signal-cli's `getAttachment` when the file is not in the local folder.

### Fixed

- **Received attachments were never copied.** signal-cli's `filename` is the sender's original name, not a path; the file lives in `~/.local/share/signal-cli/attachments/<id>`. The store held 0 attachment rows next to 60 files on disk. Now copied by id (path-traversal safe) and the id is kept.
- **Replies sent an empty quote.** signal-cli builds the quote text from `quoteMessage`; replies now look the original up in the local store.
- **`leave_group` could not work for a sole admin** (signal-cli requires naming a successor).
- **`update_group(link_mode="reset")`** sent a value signal-cli rejects; it now sends `resetLink`.
- **Typing "stop" never stopped typing**, `list_identities` ignored its number filter, and `update_profile`'s `name` was silently dropped (signal-cli only reads `givenName`).
- **Incoming remote/admin deletes were stored as empty messages.** They now flag the original (`remote_deleted`, `admin_deleted_by`) and keep your local copy.

### Changed

- A contact's display name prefers the Signal nickname, then the contact name, then the profile name.

## [1.39.0] — 2026-09-30

### Added

- **`search_messages` gained `since`/`until` date-range filtering** (also available as `--since`/`--until` on `signal-mcp search`). Works in both the full-text and fallback search paths. The most useful gap found after checking every signal-cli capability not yet wrapped — `sendStory` and call commands stay deliberately excluded (see README).

### Fixed

- **`send_attachment`, `send_group_attachment`, `update_profile`'s avatar, and `upload_sticker_pack` accepted any local file path with no restriction.** Since incoming message content is untrusted and reaches the AI client unmarked, a sender could talk the AI into sending back an arbitrary local file (e.g. "send me ~/.ssh/id_ed25519"). Added an allowlist of folders (`config.validate_send_path`, default: the attachments dir, `~/Downloads`, `~/Desktop`, `~/Documents`; override via `SIGNAL_MCP_SEND_ROOTS`), rejecting hidden files/folders and anything outside it.
- **A negative `limit` on `get_conversation`/`search_messages`/`get_unread` removed SQLite's row cap entirely** (`LIMIT -1` means "no limit"). Now clamped to 1–500.
- **`_conversation_where`'s direct-message clause forced a full scan of every direct message** before checking sender/recipient, bypassing their indexes. Rewritten as three independent branches so SQLite can use each index directly — measured ~50x faster on a 100k-message synthetic store.
- **The `attachments` table had no index on `message_id`**, scanned by every message-enrichment call and the conversation/prune delete paths. Added.
- **`tests/test_desktop.py`'s import-lock tests wrote to the real `~/.local/share/signal-mcp/desktop-import.lock`** on most tests rather than a `tmp_path`, risking a race with, or deleting the lock of, a real Desktop sync running on the machine. Added an autouse isolation fixture.

### Testing

- 734 tests, 98% coverage. Added coverage for the previously-untested scheduled-message send path and `receive_direct`'s line-parsing, plus 4 `cli.py` receive/watch fallback-path tests. Verified clean across 15 randomized test-order seeds, and re-confirmed (DAEMON_PORT pointed at an unreachable port) that nothing in the suite depends on a real daemon.

## [1.38.9] — 2026-09-28

### Fixed

- **A wrong password or corrupted/truncated Signal Desktop `encryptedKey` raised a raw `cryptography` `ValueError`** instead of the clear `DesktopImportError` every other import failure uses. `_decrypt_key` now wraps AES/PKCS7 decryption accordingly.
- **A `secret-tool` that never responds (e.g. stuck on an unanswered keyring-unlock prompt) escaped as a raw `subprocess.TimeoutExpired`.** `_require_linux_keyring_password` now catches it with a specific message.

### Testing

- `cli.py` coverage: 82% → 97% (set-webhook/get-webhook, find-contact, schedule-send/scheduled/cancel-scheduled/run-scheduled, the `install` wizard). Total project coverage: 93% → 97%, 715 tests.
- Found and fixed two more local-import-shadowing test bugs: `install()` re-imports `check_signal_cli_version` and `is_service_installed` from `.config` inside its own body, so patching `signal_mcp.cli.*` for them silently no-ops — one existing test was passing only by coincidence, because this machine's own background service happens to be installed.

## [1.38.8] — 2026-09-28

### Fixed

- **On Linux, a `v10`-format Signal Desktop key could try the OS keyring password before the correct hardcoded `peanuts` password.** Signal Desktop only ever encrypts `v10` keys on Linux with `peanuts` — `v11` is the only keyring-backed format. A keyring entry left over from an unrelated app under the same label (`Signal Safe Storage` / `Electron Safe Storage`) would pick the wrong password and fail decryption for a `v10` key. Fixed at the source in `_get_keychain_password`; the `v11` path (`_require_linux_keyring_password`) was already correct and is unaffected. Also corrected a comment and error message that implied KWallet support — only libsecret-backed keyrings (via `secret-tool`) are supported.
- **`tests/test_server.py` had real order-dependent test failures**, caused by a `reset_client` fixture that reset the local store but not the module-level contact/group caches or the daemon-alive/freshen cooldowns — several tests only passed because an earlier test happened to warm that state first. Fixed the fixture and the affected tests; verified clean across 29 randomized test-order seeds (5 previously failed).

## [1.38.7] — 2026-09-28

### Fixed

- **`import-desktop` / `sync-desktop` failed on Linux with `Unknown encryptedKey format (prefix=b'v11')`** whenever Signal Desktop stores its key through a keyring (libsecret / KWallet). `_decrypt_key` only handled the macOS variant of Chromium's format: prefix `v10` and 1003 PBKDF2 iterations. Linux uses `v11` for keyring-backed keys and a single iteration, so the `v10` / `peanuts` fallback was affected too. Both prefixes are accepted now and the iteration count follows the platform. (#8)
- **A missing `secret-tool` on Linux ended in an opaque decryption error.** The keyring lookup silently fell back to the hardcoded `peanuts` password, which can never decrypt a keyring-backed (`v11`) key. For `v11` keys the import now stops with a message that says whether `secret-tool` is missing or the keyring has no Signal entry. The `peanuts` fallback is unchanged for `v10` keys. (#8)
- **The "sqlcipher not found" hint suggested `brew install sqlcipher` on every platform.** It now names the install command for the current platform. (#8)

### Changed

- `import-desktop` / `sync-desktop` print the "macOS may ask for Keychain access" note on macOS only, and the `import-desktop` help text no longer says it requires the macOS Keychain. README setup steps for the Desktop import now cover Linux. (#8)

## [1.38.6] — 2026-09-25

### Fixed

- **The 1.38.4 daemon-PID fix only handled Ctrl+C (`KeyboardInterrupt`), not `SIGTERM`** — the actual signal `launchctl kickstart` and every LaunchAgent stop/restart sends. Python's default SIGTERM disposition kills the process without running `finally` blocks, so `clear_daemon_pid()` never fired on the realistic restart path, leaving the PID file stale or, after further daemon-lifecycle churn, pointing nowhere. Added an explicit SIGTERM handler. Verified against the real LaunchAgent with 3 consecutive `launchctl kickstart -k` restarts — the PID file matched the live process every time.

## [1.38.5] — 2026-09-25

### Performance

- **Signal Desktop import committed to SQLite once per message** — a multi-thousand-message history did thousands of individual commits. Added `store.save_messages_batch` and switched the import loop to commit in chunks of 500, cutting import time for large histories.
- **`webhook.post_webhook_batch` fired all outbound POSTs concurrently with no cap** — a large catch-up batch (e.g. after being offline) could open hundreds of simultaneous connections to the webhook endpoint at once. Capped concurrency to 10 in-flight requests via a semaphore.

## [1.38.4] — 2026-09-25

### Fixed

- **`signal-mcp daemon` (the CLI command the LaunchAgent runs) never wrote the daemon PID file** — only the separate auto-spawn path in `client.py` did. `signal-mcp stop` and the stale-PID cleanup in `ensure_daemon` had no way to find or kill a daemon started this way. Now writes the PID on start and clears it on exit.

## [1.38.3] — 2026-09-25

### Fixed

- **1.38.2's cache-refresh fix (narrowing `except Exception` to `except SignalError`) exposed an unwrapped `FileNotFoundError`** from `ensure_daemon`'s `subprocess.Popen` when `signal-cli` isn't on `PATH` — worked on a machine with signal-cli installed, broke CI and any environment without it. `ensure_daemon` now wraps a missing/unrunnable binary in `SignalError`. Caught by CI going red on the 1.38.2 release; verified locally by stripping `signal-cli` from `PATH`.

## [1.38.2] — 2026-09-25

### Fixed

- **A 200 response from signal-cli's daemon can still carry per-recipient send failures** (e.g. `UNREGISTERED_FAILURE`, `IDENTITY_FAILURE`) nested in a `results` array — previously returned as if the send succeeded. `_rpc` now raises `SignalError` when it finds one.
- **`receive_direct` never checked the signal-cli subprocess's exit code**, so a failed `receive` silently returned an empty/partial message list instead of raising.
- **`_ensure_contact_cache`/`_ensure_group_cache` caught bare `Exception`**, meant to tolerate "daemon not up yet" but actually swallowing any bug in the cache-refresh path. Narrowed to `SignalError` so real bugs propagate instead of silently leaving the cache permanently unpopulated.
- **Several RPC call sites** (`list_contacts`, `list_groups`, `list_sticker_packs`, `get_user_status`, `list_accounts`, `create_group`, `join_group`) **silently substituted an empty list/dict when signal-cli returned an unexpected shape**, which read to the caller as "you have no contacts/groups" instead of an error. Now raise `SignalError` naming the RPC method.
- **`get_webhook_url` returned `None` for a corrupt/unreadable `webhook.json`**, indistinguishable from "no webhook configured". Now raises `RuntimeError`.

Bugs identified via a diff against `faces-sh/signal-mcp`'s fork; fixed directly against our existing `SignalError`/`RuntimeError` types rather than adopting the fork's envelope architecture.

### Added

- **Read-only mode.** Set `SIGNAL_MCP_READONLY=1` to run the server with every state-mutating tool (send, edit, delete, react, group/account management, scheduling, desktop import, etc.) hidden from `list_tools` and rejected by `call_tool` if called directly. 24 read-only tools (contacts, groups, conversations, search, export, status) remain available.

### Hardened

- **Stale plaintext temp files are swept** at the start of every Desktop import — the only thing that can clean up after a `SIGKILL`, which no signal handler can catch.
- **SIGTERM/SIGINT now delete the in-flight plaintext temp file** before the process exits (main-thread only, per Python's `signal` module constraints; the sweep above is the backstop for the background-thread case).
- **A single-flight lock prevents two concurrent Desktop imports** from racing on the same local store.

### Testing

- `webhook.py` coverage: 39% → 100%.
- 8 previously-untested MCP tool handlers (`set_webhook`, `get_webhook`, `find_contact`, `schedule_message`, `list_scheduled_messages`, `cancel_scheduled_message`, `run_scheduled_messages`, `submit_rate_limit_challenge`) now covered.
- Fixed a long-standing order-dependent flaky test (`test_ensure_group_cache`) caused by unreset module-level cache state between tests.
- Overall coverage: 88% → 92%, 664 tests passing.

---

## [1.38.1] — 2026-09-25

### Fixed

- **Desktop-imported replies never got a `quote_id`**, even though live-received replies already do. Signal Desktop keeps a reply's quote in the message's json blob; now extracted the same way other optional-schema columns already are.
- **`_decrypt_db_to_temp` leaked a full plaintext copy of Signal message history to the shared system temp directory on any decrypt failure** (wrong key, timeout, non-zero exit, empty output) — never cleaned up. Now written to a private, `0700` app-owned directory (`~/.local/share/signal-mcp/tmp`) and unlinked on every failure path.

Both credited to `Culper-Project/signal-mcp`'s analysis.

---

## [1.38.0] — 2026-09-25

### Added

- **Contact names now fall back to Signal Desktop's own conversation names** for people signal-cli's own contact list doesn't have a name for. Signal Desktop typically knows far more people by name than have been pushed into signal-cli — this data was already being captured during `import_desktop`/`sync_desktop`, just never read back. signal-cli's own name (including one set manually via `update_contact`) always wins; Desktop only fills gaps. Also fixes `_read_conversation_names` dropping every contact with no phone number on file — falls back to the conversation's serviceId. This is a manual-refresh feature: names reflect the last `import_desktop`/`sync_desktop` run.

Idea credited to `faces-sh/signal-mcp`'s fork.

---

## [1.37.2] — 2026-09-25

### Fixed

- **`import_from_desktop` swallowed a failed own-number lookup**, permanently attributing every outgoing message's sender to the literal string `"me"` instead of the real account number. Now raises `DesktopImportError` with a clear message instead of silently corrupting the import.
- **`call_tool` started the signal-cli daemon before validating that the tool name exists or has its required arguments**, so an unknown tool or a missing argument reported "daemon failed to start" whenever the daemon itself couldn't start — masking the real, cheaper-to-diagnose problem. Validation now runs first.

Both surfaced while reviewing forks of this project — see `faces-sh/signal-mcp`'s uniform-error-envelope commit for the original analysis (its broader architecture wasn't adopted here, just these two fixes).

---

## [1.37.1] — 2026-09-25

### Fixed

- **Signal Desktop import split every direct conversation into two, and dropped the recipient on outgoing messages to contacts with no stored phone number.** `store.get_conversation` matches `sender = ? OR recipient = ?` against a single identifier, but incoming messages were keyed by the contact's uuid (Signal Desktop leaves `source` NULL and fills `sourceServiceId`) while outgoing messages were keyed by the conversation's e164 — a read by either identifier returned only half the conversation. Found independently by two forks of this project (`faces-sh/signal-mcp`, `Culper-Project/signal-mcp`) while diagnosing corrupted imported history; verified against this repo's own code before fixing. Both directions of a direct-conversation message now use the same identifier — the conversation's own e164, falling back to its serviceId for a contact with no phone number on file. Group messages are unaffected.

---

## [1.37.0] — 2026-09-22

### Added

- **`signal-mcp doctor`** — an onboarding smoke test that catches, in one run, the class of setup failures this project has hit in practice: signal-cli missing or too old, no account detected, daemon not running, `listDevices`/`receive` round-trips actually working (not just the port being open), and stale entries in signal-cli's `msg-cache` that can silently kill the receive thread on daemon startup. When the account has multiple linked devices, it also explains that only device 1 is primary and several write tools (`update_configuration`, `block_contact`, `set_pin`, `add_device`, ...) fail on any other device — signal-cli's JSON-RPC doesn't expose which device *this* instance is, so it can't check that automatically. Skips the direct receive probe (which would otherwise always fail) when the background watch service is installed and already holds signal-cli's receive lock. Exits non-zero if any check fails.

---

## [1.36.2] — 2026-09-21

### Changed

- **README:** `sendStory` (signal-cli 0.14.6) and `terminateGroup` (0.14.8) are now listed under "Not covered" as consciously not added — both are feasible, but stories have no use case yet and terminating a group is irreversible for every member. The section intro no longer claims everything listed there is infeasible. No code changes.

---

## [1.36.1] — 2026-09-21

### Fixed

- **`Dockerfile` could never build** — the signal-cli download step extracted `signal-cli-<ver>-Linux-native/bin/signal-cli`, but the `Linux-native` tarball contains only a single top-level `signal-cli` executable, so `tar` failed with "Not found in archive" (reproduced against the real 0.14.3 tarball). Now extracts `signal-cli` directly. Verified the extraction against the 0.14.8 tarball (x86-64 ELF); the Docker image itself was not built (no Docker daemon available). The native binary is x86-64 only.

### Changed

- **Bundled signal-cli bumped 0.14.3 → 0.14.8.** signal-cli's README notes that Signal's official clients expire after three months, after which the server can make incompatible changes; 0.14.3 (April) was past that.

---

## [1.36.0] — 2026-09-04

### Fixed

- **`send_group_message`'s `mentions` never worked** — sent a JSON array of `{start,length,author}` objects, but signal-cli's mention parser calls `Pattern.matcher()` on each element expecting a `"start:length:author"` string; an object throws `ClassCastException` on signal-cli's side. Fixed to format mentions into strings. Also documented that `start`/`length` are UTF-16 code units, not codepoints.
- **`create_poll`/`vote_poll`/`terminate_poll` never worked** — used entirely fictional param names (`poll-question`, `poll-options`, `poll-multi-select`, `targetAuthor`, `targetTimestamp`, `poll-id` — none of these exist in signal-cli). Real keys are `question`/`option`/`no-multi` (multi-select is signal-cli's *default*, disabled via `no-multi`) and `poll-author`/`poll-timestamp` (a poll has no separate ID — it's identified by its message author+timestamp). `vote_poll` was also missing signal-cli's required `vote-count` field; a new local `poll_votes` table now tracks and applies it automatically.

### Removed

- **`poll_id`** parameter from `vote_poll` and `terminate_poll` — it never corresponded to anything in signal-cli's protocol.

---

## [1.35.1] — 2026-09-03

### Changed

- **10 tools now document a primary-device-only requirement**: `block_contact`, `unblock_contact`, `add_device`, `remove_device`, `update_device`, `update_configuration`, `set_pin`, `remove_pin`, `start_change_number`, `finish_change_number`. Each maps to a signal-cli command that throws `NotPrimaryDeviceException` when signal-mcp runs as a linked device (the setup this repo's own docs recommend) — confirmed by reading signal-cli's command source. The tools are unchanged and work correctly when signal-mcp is the account's primary device; the description now says so upfront instead of only surfacing it as a runtime error.

---

## [1.35.0] — 2026-09-03

### Removed

- **`get_configuration`** — called signal-cli's `getConfiguration` RPC, which has never existed in any signal-cli version (confirmed against the pinned v0.14.3 and current master's command registry, and live against a real account: always "Method not implemented"). signal-cli has no way to read configuration back, only set it. `update_configuration`/`update_account`'s descriptions no longer reference the removed tool.

### Fixed

- **`add_sticker_pack`** returned no confirmation of what was installed. Now parses `pack_id` from the URI and returns it, so callers don't need a separate `list_sticker_packs` call for the ID they need next.
- **`get_unread`**'s description said "call again with a higher limit or paginate," which didn't match actual behavior (no `offset` param exists; returned messages are marked read, so a same-limit re-call naturally advances). Corrected.
- **`run_scheduled_messages`**'s description never documented its response shape or that it's safe to call when nothing is due. Added both.

---

## [1.34.1] — 2026-09-03

### Fixed

- **`__version__` was hardcoded and never bumped** — stuck at `1.33.0` across four releases, so `signal-mcp --version`/`status` lied about what was actually running. Now sourced from installed package metadata (`importlib.metadata.version`), never hand-maintained again.
- **MCP handshake reported an empty version string** — `Server("signal-mcp")` never passed a `version` kwarg. Now passes `version=__version__`.

---

## [1.34.0] — 2026-09-03

### Added

- **`send_note_to_self` now supports rich text, attachments, and threading.** Message text supports lightweight markdown — `**bold**`, `~~strikethrough~~`, `` `monospace` `` — parsed into Signal's native `textStyle` ranges (real rendered rich text, not emoji tricks). New `attachments` param sends files (e.g. a package QR code) alongside a styled caption in one call. New `quote_author`/`quote_timestamp` params thread a follow-up note under a previous one (e.g. package status updates staying grouped together).
- **New `signal_mcp.formatting` module** — `parse_styled_text()`, a small pure function converting markdown markers to UTF-16-offset style ranges, fully unit-tested including emoji/surrogate-pair edge cases.

---

## [1.33.3] — 2026-09-03

### Fixed

- **`tools/list` crashed on the real mcp 2.0 runner** — `_list_tools` and `call_tool` were registered as request handlers but only accepted `params`, not the leading `ctx` argument `RequestHandler = Callable[[ServerRequestContext, ParamsT], Awaitable[Result]]` requires. Every `tools/list` call failed with `TypeError: _list_tools() takes 1 positional argument but 2 were given` (confirmed via Glama's build log, since local tests called the handlers directly with one arg and never caught it).

---

## [1.33.2] — 2026-09-03

### Fixed

- **Self-conversation ("note to self") queries matched every outgoing message** — `get_conversation`, `count_conversation`, `delete_conversation_messages`, and `get_messages_for_export` all bound the own-account number to both `sender` and `recipient` with `OR`, so fetching or deleting the notes-to-self thread returned/deleted every outgoing message to anyone.
- **`list_conversations` showed raw numbers/IDs instead of names** — contact/group caches were never warmed before name resolution.
- **`clear_local_store` confirm gate bypassable** — truthiness check let a non-boolean truthy value (e.g. the string `"false"`) through; now requires `confirm is True`.
- **`get_conversation` pagination crash on string offset** — `limit`/`offset` weren't cast to `int`, so a numeric-string offset raised `TypeError`.
- **Scheduled-message sends blocked the event loop** — synchronous SQLite calls in `process_scheduled_messages` now run via `asyncio.to_thread`.

---

## [1.33.1] — 2026-09-03

### Fixed

- **Docker build failure on Glama** — base image `eclipse-temurin:21-jre-bookworm` ships Python 3.11, but `pyproject.toml` requires `>=3.12`, so `pip install -e .` failed. Switched to the `-noble` tag (Ubuntu 24.04), which ships Python 3.12 by default.

---

## [1.8.0] — 2026-05-03

### UX

- **`list_conversations` now includes `unread_count` and `last_message`** — Claude can tell at a glance which conversations need attention and what the last message was, without a second round-trip
- **`get_unread` auto-marks as read** — consistent with `get_conversation`; fetching unread messages marks them read in the local store

### Tests

- **25 CLI tests** (`test_cli.py`) — covers `send`, `note`, `contacts`, `groups`, `history`, `search`, `store-stats`, `export`, `edit`, `status`; cli.py now has test coverage for the first time

### Stats
- 50 MCP tools total
- 229 tests

---

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
