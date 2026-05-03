# Contributing

## Setup

```bash
git clone https://github.com/yourname/signal-mcp
cd signal-mcp
uv sync --dev
```

## Running tests

```bash
uv run pytest          # all tests
uv run pytest -q       # quiet
uv run pytest --cov    # with coverage
```

Tests are fully mocked — no signal-cli or Signal account needed.

## Project structure

```
src/signal_mcp/
  models.py       — data classes (Message, Contact, Group, …)
  config.py       — account detection, paths, daemon PID
  client.py       — async JSON-RPC client wrapping signal-cli
  store.py        — SQLite message store (FTS5 search)
  server.py       — MCP server (tool definitions + handlers)
  desktop.py      — Signal Desktop DB importer
  translation.py  — Claude-powered translation (CLI only)
  cli.py          — Click CLI entrypoint
tests/
  test_client.py
  test_store.py
  test_server.py
  test_desktop.py
  test_translation.py
```

## Adding a new MCP tool

1. Add the method to `client.py` (calls `_rpc(...)`)
2. Add a `Tool(...)` entry to the `TOOLS` list in `server.py`
3. Add an `elif name == "..."` branch in `call_tool()` in `server.py`
4. If the tool doesn't need the daemon (read-only from store), add it to `_DAEMON_FREE`
5. Add tests in `tests/test_server.py` and `tests/test_client.py`
6. Update the tool table in `README.md`

## Principles

- All tests must pass before merging (`uv run pytest`)
- No external network calls in tests — mock at the HTTP layer with `respx`
- Keep `store.py` independent of `client.py` (no circular imports)
- `server.py` is the only file that imports from both

## Releasing

1. Bump `version` in `pyproject.toml`
2. Tag: `git tag v0.x.0 && git push --tags`
3. Publish: `uv build && uv publish`
