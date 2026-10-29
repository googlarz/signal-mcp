# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue:

**https://github.com/googlarz/signal-mcp/security/advisories/new**

(GitHub's private vulnerability reporting. Only the maintainer can see the report.)

Helpful to include: the signal-mcp version (`signal-mcp --version`), what you did, what happened, and what you expected. A reproducer using the mocked test suite style (`pytest`, `respx`) is ideal because the project has no live Signal account in CI.

This is a one-person project, so replies are best effort. I will confirm that I received the report and keep you updated on the fix.

## Supported versions

Only the latest release on [PyPI](https://pypi.org/project/signal-mcp/) gets fixes.

## What counts

In scope: reading or sending files outside the allowed send folders (`SIGNAL_MCP_SEND_ROOTS`), a state-changing tool that still runs in read-only mode (`SIGNAL_MCP_READONLY`), command or SQL injection from message content or tool arguments, leaking the local message history, and webhook or URL handling that lets a sender reach internal services.

Out of scope: anything that needs code execution as the same user or physical access to the machine, other local processes talking to the local signal-cli port (a documented trust boundary), and bugs in [signal-cli](https://github.com/AsamK/signal-cli) or libsignal themselves (please report those upstream).
