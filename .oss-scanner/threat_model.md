# signal-mcp threat model

## What it is
A local MCP server and CLI (Python) that sits on top of a locally running `signal-cli` daemon (JSON-RPC on `localhost:7583`) and keeps a local message history in SQLite. It is run by one user on their own machine. There is no network listener of its own, no multi-user mode and no authentication layer: whoever can talk to the MCP client or the local daemon port already acts as the account owner.

## Where untrusted input enters
1. **Incoming Signal messages** (text, sender names, group titles, attachment names, quoted text, mentions). Anyone who can message the user controls these. They are stored in SQLite, searched with FTS5, printed by the CLI and returned to the MCP client (an LLM).
2. **Arguments of MCP tool calls**, which may be influenced by prompt injection from message content. Treat the tool-call arguments as attacker-influenced.
3. **Attachment data and file names** received from Signal.
4. **URLs and links** passed to `join_group`, `add_device`, `add_sticker_pack` and webhook URLs.

## Protections the project intends to provide
- `SIGNAL_MCP_SEND_ROOTS`: any local file sent as attachment, avatar or sticker must resolve (after symlink resolution) inside an allowed folder. A bypass that reads or sends files outside the allowed roots is a real vulnerability.
- `SIGNAL_MCP_READONLY`: only the allowlisted read-only tools may run. Any tool that changes state and still runs in this mode is a real vulnerability.
- `confirm: true` is required for `clear_local_store` and `terminate_group`.
- Attachment copies use only the base name of an id, so path traversal through attachment ids must not escape the attachments directory.
- SQL is parameterised; FTS queries built from user text must not allow injection.
- No shell strings are built from message content.

## Severity guidance
- **Critical/High:** reading or writing files outside the allowed roots; executing commands from message content or tool arguments; SQL injection; read-only mode bypass; leaking message history to a third party; webhook or URL handling that lets a remote sender reach internal services.
- **Medium:** denial of service by a crafted incoming message (crash or unbounded growth of the local store); parsing bugs that corrupt stored data; formatting-marker parsing that sends wrong text ranges.
- **Low/out of scope:** anything that requires local code execution as the same user; physical access to the machine; weaknesses inside `signal-cli` or libsignal themselves (report upstream); prompt injection that only changes what the LLM decides to do with otherwise permitted tools; the local daemon port being reachable by other local processes (documented trust boundary).

## Reports
Please include a reproducer using the test suite style (`pytest`, mocked daemon via `respx`) and a minimal patch. The project has no live Signal account in CI, so reproducers must work against mocked JSON-RPC responses.
