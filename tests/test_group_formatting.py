"""Formatting (bold/italic/strike/mono/spoiler) for group messages, including how @mention
offsets are remapped once the markers are stripped."""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from click.testing import CliRunner

import signal_mcp.client as _client_mod
import signal_mcp.server as _server_mod
import signal_mcp.store as _store_mod
from signal_mcp.cli import cli
from signal_mcp.client import SignalClient
from signal_mcp.config import DAEMON_URL
from signal_mcp.formatting import _utf16_len, parse_styled_text_mapped
from signal_mcp.models import SendResult
from tests.conftest import call_tool


def rpc_ok(result) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(_store_mod, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(_store_mod, "_initialized_paths", set())
    if getattr(_store_mod._thread_local, "conn", None) is not None:
        _store_mod._thread_local.conn.close()
        _store_mod._thread_local.conn = None
    monkeypatch.setattr(_client_mod, "_contact_cache", {})
    monkeypatch.setattr(_client_mod, "_contact_cache_loaded", True)
    monkeypatch.setattr(_client_mod, "_contact_cache_at", time.monotonic())
    monkeypatch.setattr(_client_mod, "_group_cache", {})
    monkeypatch.setattr(_client_mod, "_group_cache_loaded", True)
    monkeypatch.setattr(_client_mod, "_group_cache_at", time.monotonic())


@pytest.fixture
def client(monkeypatch):
    c = SignalClient(account="+10000000000")
    monkeypatch.setattr(c, "ensure_daemon", AsyncMock())
    monkeypatch.setattr(_server_mod, "_client", c)
    monkeypatch.setattr(_server_mod, "_READONLY", False)
    return c


# ── offset remapping ──────────────────────────────────────────────────────────

def test_remap_moves_offsets_past_stripped_markers():
    plain, ranges, remap = parse_styled_text_mapped("**Hi** @Anna")
    assert plain == "Hi @Anna" and ranges == ["0:2:BOLD"]
    assert remap(7) == 3          # the "@" of @Anna
    assert remap(12) - remap(7) == 5


def test_remap_counts_emoji_as_two_utf16_units():
    plain, _, remap = parse_styled_text_mapped("🎉 **b** @A")
    assert plain == "🎉 b @A"
    assert remap(9) == 5


def test_remap_is_identity_without_markers():
    plain, ranges, remap = parse_styled_text_mapped("no markers here")
    assert (plain, ranges) == ("no markers here", [])
    assert all(remap(i) == i for i in range(len("no markers here") + 1))


@pytest.mark.parametrize("text", [
    "**a** *b* ~~c~~ `d` ||e||",
    "🎉 **bold** 🎉 *it* 🎉 `m` @Name tail",
    "plain **x**",
    "*start* and end **bold**",
    "snake_case **bold** 2 * 3",
])
def test_every_range_and_remapped_offset_stays_inside_plain_text(text):
    plain, ranges, remap = parse_styled_text_mapped(text)
    total = _utf16_len(plain)
    for r in ranges:
        start, length, _ = r.split(":")
        assert 0 <= int(start) and int(length) > 0 and int(start) + int(length) <= total
    orig_total = _utf16_len(text)
    mapped = [remap(i) for i in range(orig_total + 1)]
    assert mapped == sorted(mapped) and 0 <= min(mapped) and max(mapped) <= total


# ── client ────────────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_group_message_formatting_sends_plain_text_and_ranges(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 11})))
    await client.send_group_message("grp==", "**Hi** *all* ||x||", formatting=True)
    p = sent(route)["params"]
    assert p["message"] == "Hi all x"
    assert p["textStyle"] == ["0:2:BOLD", "3:3:ITALIC", "7:1:SPOILER"]
    assert [m.body for m in _store_mod.get_conversation("grp==")] == ["Hi all x"]


@respx.mock
@pytest.mark.asyncio
async def test_group_message_formatting_remaps_mentions(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 12})))
    await client.send_group_message(
        "grp==", "**Hi** @Anna", mentions=[{"start": 7, "length": 5, "author": "+15550101"}], formatting=True,
    )
    p = sent(route)["params"]
    assert p["message"] == "Hi @Anna"
    assert p["mention"] == ["3:5:+15550101"]
    assert p["textStyle"] == ["0:2:BOLD"]


@respx.mock
@pytest.mark.asyncio
async def test_group_message_without_formatting_flag_is_untouched(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 13})))
    await client.send_group_message(
        "grp==", "**keep** `me`", mentions=[{"start": 0, "length": 2, "author": "+15550101"}],
    )
    p = sent(route)["params"]
    assert p["message"] == "**keep** `me`"
    assert "textStyle" not in p
    assert p["mention"] == ["0:2:+15550101"]


@respx.mock
@pytest.mark.asyncio
async def test_group_message_formatting_without_markers_sends_no_textstyle(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 14})))
    await client.send_group_message("grp==", "just text, snake_case_name", formatting=True)
    p = sent(route)["params"]
    assert p["message"] == "just text, snake_case_name" and "textStyle" not in p


# ── tool + CLI ────────────────────────────────────────────────────────────────

def test_tool_schema_documents_formatting():
    tool = next(t for t in _server_mod.TOOLS if t.name == "send_group_message")
    assert tool.input_schema["properties"]["formatting"]["type"] == "boolean"
    assert "formatting=true" in tool.description


@respx.mock
@pytest.mark.asyncio
async def test_tool_forwards_formatting(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 15})))
    await call_tool("send_group_message", {"group_id": "grp==", "message": "**x**", "formatting": True})
    assert sent(route)["params"]["message"] == "x"
    assert sent(route)["params"]["textStyle"] == ["0:1:BOLD"]


def test_cli_send_group_format_flag():
    fake = MagicMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    fake.ensure_daemon = AsyncMock()
    fake.send_group_message = AsyncMock(return_value=SendResult(timestamp=1, recipient="grp==", success=True))
    with patch("signal_mcp.cli.SignalClient", return_value=fake):
        result = CliRunner().invoke(cli, ["send-group", "grp==", "**hi**", "--format"])
    assert result.exit_code == 0
    assert fake.send_group_message.call_args.kwargs["formatting"] is True
