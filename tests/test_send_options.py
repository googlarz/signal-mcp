"""Send options (quotes, previews, username, story replies, …), send_story,
get_attachment fetch and receive options — exact signal-cli JSON-RPC params.

Param names follow signal-cli's JsonRpcNamespace: a JSON-RPC key is looked up
as the dashed CLI dest first, then as its camelCase form, so camelCase keys
("quoteMessage") map onto the CLI options ("--quote-message")."""

import base64
import json
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from click.testing import CliRunner

import signal_mcp.client as _client_mod
import signal_mcp.server as _server_mod
import signal_mcp.store as _store_mod
from signal_mcp.cli import cli
from signal_mcp.client import SignalClient, SignalError
from signal_mcp.config import DAEMON_URL
from signal_mcp.models import Message, SendResult
from tests.conftest import call_tool


def rpc_ok(result) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


@pytest.fixture(autouse=True)
def reset_store(monkeypatch, tmp_path):
    monkeypatch.setattr(_store_mod, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(_store_mod, "_initialized_paths", set())
    if getattr(_store_mod._thread_local, "conn", None) is not None:
        _store_mod._thread_local.conn.close()
        _store_mod._thread_local.conn = None


@pytest.fixture(autouse=True)
def reset_caches(monkeypatch):
    # Pre-warmed empty caches: no unmocked listContacts/listGroups RPCs.
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
    return c


@pytest.fixture
def server_client(monkeypatch, client):
    monkeypatch.setattr(_server_mod, "_client", client)
    monkeypatch.setattr(_server_mod, "_READONLY", False)
    return client


@pytest.fixture
def allowed(tmp_path_factory):
    """A real file inside the (test-widened) SEND_ROOTS allowlist."""
    p = tmp_path_factory.getbasetemp() / "pic.jpg"
    p.write_bytes(b"jpg")
    return p


# ── send options ──────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_send_message_all_options_exact_params(client, allowed):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 5})))
    await client.send_message(
        "+19999999999", "see https://x.org",
        quote_author="+18888888888", quote_timestamp=111, quote_message="orig",
        quote_mentions=[{"start": 0, "length": 1, "author": "+17777777777"}],
        quote_text_styles=["0:4:BOLD"],
        quote_attachments=["image/png:a.png", f"image/jpeg:b.jpg:{allowed}"],
        preview_url="https://x.org", preview_title="X", preview_description="desc",
        preview_image=str(allowed),
        story_author="+16666666666", story_timestamp=222,
        no_urgent=True, notify_self=True,
    )
    body = sent(route)
    assert body["method"] == "send"
    assert body["params"] == {
        "recipient": ["+19999999999"],
        "message": "see https://x.org",
        "quoteAuthor": "+18888888888",
        "quoteTimestamp": 111,
        "quoteMessage": "orig",
        "quoteMention": ["0:1:+17777777777"],
        "quoteTextStyle": ["0:4:BOLD"],
        "quoteAttachment": ["image/png:a.png", f"image/jpeg:b.jpg:{allowed.resolve()}"],
        "previewUrl": "https://x.org",
        "previewTitle": "X",
        "previewDescription": "desc",
        "previewImage": str(allowed.resolve()),
        "storyAuthor": "+16666666666",
        "storyTimestamp": 222,
        "noUrgent": True,
        "notifySelf": True,
    }


@respx.mock
@pytest.mark.asyncio
async def test_plain_send_params_unchanged(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 5})))
    await client.send_message("+19999999999", "hi")
    assert sent(route)["params"] == {"recipient": ["+19999999999"], "message": "hi"}


@respx.mock
@pytest.mark.asyncio
async def test_quote_text_looked_up_from_store(client):
    """signal-cli sends quote text "" when quoteMessage is absent — fill it from the store."""
    _store_mod.save_message(Message(
        id="m1", sender="+18888888888", body="original text",
        timestamp=datetime.fromtimestamp(1_700_000_000_123 / 1000),
    ))
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 5})))
    await client.send_message(
        "+18888888888", "reply", quote_author="+18888888888", quote_timestamp=1_700_000_000_123,
    )
    params = sent(route)["params"]
    assert params["quoteMessage"] == "original text"
    assert params["quoteTimestamp"] == 1_700_000_000_123


@respx.mock
@pytest.mark.asyncio
async def test_quote_text_lookup_requires_matching_author(client):
    _store_mod.save_message(Message(
        id="m1", sender="+17777777777", body="someone else",
        timestamp=datetime.fromtimestamp(1_700_000_000_123 / 1000),
    ))
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 5})))
    await client.send_group_message(
        "grp==", "reply", quote_author="+18888888888", quote_timestamp=1_700_000_000_123,
    )
    params = sent(route)["params"]
    assert "quoteMessage" not in params
    assert params["quoteAuthor"] == "+18888888888"


@respx.mock
@pytest.mark.asyncio
async def test_preview_image_outside_allowlist_rejected(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    with pytest.raises(SignalError, match="outside|allowed|not"):
        await client.send_message(
            "+19999999999", "https://x.org", preview_url="https://x.org",
            preview_image="/etc/passwd",
        )
    assert not route.called


@respx.mock
@pytest.mark.asyncio
async def test_quote_attachment_preview_file_outside_allowlist_rejected(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    with pytest.raises(SignalError):
        await client.send_message(
            "+19999999999", "hi", quote_author="+18888888888", quote_timestamp=1,
            quote_attachments=["image/png:x.png:/etc/passwd"],
        )
    assert not route.called


@pytest.mark.asyncio
async def test_story_timestamp_requires_author(client):
    with pytest.raises(SignalError, match="story_author"):
        await client.send_message("+19999999999", "nice", story_timestamp=5)


@respx.mock
@pytest.mark.asyncio
async def test_send_by_username(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 9})))
    result = await client.send_message(None, "hi", username="alice.42")
    assert sent(route)["params"] == {"username": ["alice.42"], "message": "hi"}
    assert result.recipient == "alice.42"


@respx.mock
@pytest.mark.asyncio
async def test_send_by_username_link(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 9})))
    link = "https://signal.me/#eu/abcDEF123"
    await client.send_message(None, "hi", username=link)
    assert sent(route)["params"]["username"] == [link]


@pytest.mark.asyncio
@pytest.mark.parametrize("recipient,username,match", [
    (None, None, "exactly one"),
    ("+19999999999", "alice.42", "exactly one"),
    (None, "alice", "Invalid username"),
    (None, "+19999999999", "Invalid username"),
    ("12345", None, "E.164"),
])
async def test_direct_target_validation(client, recipient, username, match):
    with pytest.raises(SignalError, match=match):
        await client.send_message(recipient, "hi", username=username)


@respx.mock
@pytest.mark.asyncio
async def test_end_session(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok(None)))
    result = await client.send_message("+19999999999", "", end_session=True)
    assert sent(route)["params"] == {"recipient": ["+19999999999"], "endSession": True}
    assert result.success is True
    assert _store_mod.get_stats()["total_messages"] == 0


@respx.mock
@pytest.mark.asyncio
async def test_attachments_voice_note_and_username(client, allowed):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 1})))
    await client.send_attachment(None, str(allowed), username="bob.01", voice_note=True)
    assert sent(route)["params"] == {
        "username": ["bob.01"], "attachment": [str(allowed.resolve())], "voiceNote": True,
    }
    await client.send_group_attachment("grp==", str(allowed), voice_note=True, no_urgent=True)
    assert sent(route)["params"] == {
        "groupId": "grp==", "attachment": [str(allowed.resolve())], "voiceNote": True, "noUrgent": True,
    }


@respx.mock
@pytest.mark.asyncio
async def test_note_to_self_notify_self(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 1})))
    await client.send_note_to_self("ping", notify_self=True)
    assert sent(route)["params"] == {"recipient": ["+10000000000"], "message": "ping", "notifySelf": True}


@pytest.mark.asyncio
async def test_unknown_send_option_is_a_type_error(client):
    with pytest.raises(TypeError):
        await client.send_group_message("grp==", "hi", bogus=1)


# ── send_story ────────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_send_story_params(client, allowed):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 77})))
    result = await client.send_story(str(allowed))
    assert sent(route)["method"] == "sendStory"
    assert sent(route)["params"] == {"attachment": str(allowed.resolve())}
    assert result.timestamp == 77
    await client.send_story(str(allowed), group_id="grp==", allow_replies=False)
    assert sent(route)["params"] == {
        "attachment": str(allowed.resolve()), "groupId": "grp==", "noReplies": True,
    }


@respx.mock
@pytest.mark.asyncio
async def test_send_story_path_outside_allowlist_rejected(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    with pytest.raises(SignalError):
        await client.send_story("/etc/passwd")
    assert not route.called


# ── get_attachment fetch ──────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_get_attachment_fetches_via_rpc(client, tmp_path, monkeypatch):
    att_dir = tmp_path / "att"
    monkeypatch.setattr(_client_mod, "ATTACHMENT_DIR", att_dir)
    monkeypatch.setattr(_client_mod, "ensure_attachment_dir", lambda: att_dir.mkdir(exist_ok=True))
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(
        200, json=rpc_ok({"data": base64.b64encode(b"\x89PNG").decode()})))
    info = await client.get_attachment("abc123.png")
    assert sent(route)["method"] == "getAttachment"
    assert sent(route)["params"] == {"id": "abc123.png"}
    assert (att_dir / "abc123.png").read_bytes() == b"\x89PNG"
    assert info["filename"] == "abc123.png"
    assert info["size"] == 4


@respx.mock
@pytest.mark.asyncio
async def test_get_attachment_local_file_skips_rpc(client, tmp_path, monkeypatch):
    att_dir = tmp_path / "att"
    att_dir.mkdir()
    (att_dir / "here.jpg").write_bytes(b"xx")
    monkeypatch.setattr(_client_mod, "ATTACHMENT_DIR", att_dir)
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    info = await client.get_attachment("here.jpg")
    assert info["size"] == 2
    assert not route.called


@respx.mock
@pytest.mark.asyncio
async def test_get_attachment_empty_rpc_result_is_not_found(client, tmp_path, monkeypatch):
    att_dir = tmp_path / "att"
    att_dir.mkdir()
    monkeypatch.setattr(_client_mod, "ATTACHMENT_DIR", att_dir)
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    with pytest.raises(SignalError, match="not found"):
        await client.get_attachment("nothing.bin")
    assert not (att_dir / "nothing.bin").exists()


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["../escape.txt", "sub/../../escape.txt", "/etc/passwd"])
async def test_get_attachment_traversal_never_fetches_or_writes(client, tmp_path, monkeypatch, name):
    att_dir = tmp_path / "att"
    att_dir.mkdir()
    monkeypatch.setattr(_client_mod, "ATTACHMENT_DIR", att_dir)
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(
        200, json=rpc_ok({"data": base64.b64encode(b"evil").decode()})))
    with pytest.raises(SignalError, match="Invalid attachment filename"):
        await client.get_attachment(name)
    assert not route.called
    assert not (tmp_path / "escape.txt").exists()


# ── receive options ───────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_receive_messages_max_messages(client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([])))
    await client.receive_messages(timeout=2, max_messages=3)
    assert sent(route)["params"] == {"timeout": 2, "maxMessages": 3}
    await client.receive_messages(timeout=2)
    assert sent(route)["params"] == {"timeout": 2}


@pytest.mark.asyncio
async def test_receive_direct_flags(client, monkeypatch, tmp_path):
    monkeypatch.setattr(_client_mod, "RECEIVE_LOCK_FILE", tmp_path / "receive.lock")
    monkeypatch.setattr(client, "stop_daemon", AsyncMock(return_value=True))
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = 0
    exec_mock = AsyncMock(return_value=proc)

    async def no_sleep(*_a, **_kw):
        return None

    with patch("signal_mcp.client.asyncio.create_subprocess_exec", exec_mock), \
         patch("signal_mcp.client.asyncio.sleep", no_sleep):
        await client.receive_direct(
            timeout=1, max_messages=4, ignore_attachments=True, ignore_stories=True,
            ignore_avatars=True, ignore_stickers=True,
        )
    assert exec_mock.call_args.args == (
        "signal-cli", "-u", "+10000000000", "-o", "json",
        "receive", "--timeout", "1", "--max-messages", "4",
        "--ignore-attachments", "--ignore-stories", "--ignore-avatars", "--ignore-stickers",
    )


# ── server: schemas + dispatch ────────────────────────────────────────────────

def _tool(name):
    return next(t for t in _server_mod.TOOLS if t.name == name)


def test_tool_schemas():
    sm = _tool("send_message").input_schema
    assert sm["required"] == ["message"]
    for key in ("username", "end_session", "quote_message", "quote_mentions", "preview_url",
                "preview_image", "story_author", "story_timestamp", "no_urgent", "notify_self"):
        assert key in sm["properties"], key
    assert "voice_note" in _tool("send_attachment").input_schema["properties"]
    assert "username" in _tool("send_attachment").input_schema["properties"]
    assert "voice_note" in _tool("send_group_attachment").input_schema["properties"]
    assert "preview_url" in _tool("send_group_message").input_schema["properties"]
    assert "notify_self" in _tool("send_note_to_self").input_schema["properties"]
    assert set(_tool("receive_direct").input_schema["properties"]) == {
        "timeout", "max_messages", "ignore_attachments", "ignore_stories",
        "ignore_avatars", "ignore_stickers",
    }
    assert set(_tool("receive_messages").input_schema["properties"]) == {"timeout", "max_messages"}
    assert _tool("send_story").input_schema["required"] == ["path"]
    assert "send_story" not in _server_mod._READ_ONLY_TOOLS


@pytest.mark.asyncio
async def test_send_story_blocked_in_readonly_mode(server_client, monkeypatch):
    monkeypatch.setattr(_server_mod, "_READONLY", True)
    result = await call_tool("send_story", {"path": "/tmp/x.jpg"})
    assert "read-only" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_send_message_username_and_preview(server_client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 3})))
    result = await call_tool("send_message", {
        "username": "alice.42", "message": "https://x.org",
        "preview_url": "https://x.org", "preview_title": "X", "no_urgent": True,
    })
    assert '"recipient": "alice.42"' in result[0].text
    assert sent(route)["params"] == {
        "username": ["alice.42"], "message": "https://x.org",
        "previewUrl": "https://x.org", "previewTitle": "X", "noUrgent": True,
    }


@pytest.mark.asyncio
async def test_tool_send_message_needs_recipient_or_username(server_client):
    result = await call_tool("send_message", {"message": "hi"})
    assert "exactly one of recipient or username" in result[0].text


@respx.mock
@pytest.mark.asyncio
async def test_tool_send_message_end_session(server_client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({})))
    await call_tool("send_message", {"recipient": "+19999999999", "message": "", "end_session": True})
    assert sent(route)["params"] == {"recipient": ["+19999999999"], "endSession": True}


@respx.mock
@pytest.mark.asyncio
async def test_tool_send_group_and_note_forward_options(server_client):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 3})))
    await call_tool("send_group_message", {
        "group_id": "grp==", "message": "hi", "story_author": "+16666666666", "story_timestamp": 8,
    })
    assert sent(route)["params"] == {
        "groupId": "grp==", "message": "hi", "storyTimestamp": 8, "storyAuthor": "+16666666666",
    }
    await call_tool("send_note_to_self", {"message": "n", "notify_self": True})
    assert sent(route)["params"]["notifySelf"] is True


@respx.mock
@pytest.mark.asyncio
async def test_tool_attachments_forward_options(server_client, allowed):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 3})))
    await call_tool("send_attachment", {"username": "bob.01", "path": str(allowed), "voice_note": True})
    assert sent(route)["params"]["username"] == ["bob.01"]
    assert sent(route)["params"]["voiceNote"] is True
    await call_tool("send_group_attachment", {"group_id": "grp==", "path": str(allowed), "no_urgent": True})
    assert sent(route)["params"]["noUrgent"] is True


@respx.mock
@pytest.mark.asyncio
async def test_tool_send_story(server_client, allowed):
    route = respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok({"timestamp": 4})))
    result = await call_tool("send_story", {"path": str(allowed), "allow_replies": False})
    assert '"posted"' in result[0].text
    assert sent(route)["params"] == {"attachment": str(allowed.resolve()), "noReplies": True}


@respx.mock
@pytest.mark.asyncio
async def test_tool_get_attachment_fetch(server_client, tmp_path, monkeypatch):
    att_dir = tmp_path / "att"
    monkeypatch.setattr(_client_mod, "ATTACHMENT_DIR", att_dir)
    monkeypatch.setattr(_client_mod, "ensure_attachment_dir", lambda: att_dir.mkdir(exist_ok=True))
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(
        200, json=rpc_ok({"data": base64.b64encode(b"abc").decode()})))
    result = await call_tool("get_attachment", {"filename": "id1.bin"})
    assert '"size": 3' in result[0].text


@pytest.mark.asyncio
async def test_tool_receive_options_forwarded(server_client, monkeypatch):
    rm = AsyncMock(return_value=[])
    rd = AsyncMock(return_value=[])
    monkeypatch.setattr(server_client, "receive_messages", rm)
    monkeypatch.setattr(server_client, "receive_direct", rd)
    await call_tool("receive_messages", {"timeout": 1, "max_messages": 2})
    rm.assert_awaited_once_with(timeout=1, max_messages=2)
    await call_tool("receive_direct", {"timeout": 1, "ignore_stories": True})
    rd.assert_awaited_once_with(
        timeout=1, max_messages=None, ignore_attachments=False, ignore_stories=True,
        ignore_avatars=False, ignore_stickers=False,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def _mock_cli_client(**methods):
    c = MagicMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    c.ensure_daemon = AsyncMock()
    for k, v in methods.items():
        setattr(c, k, v)
    return c


def test_cli_send_username_and_flags():
    send = AsyncMock(return_value=SendResult(timestamp=1, recipient="alice.42", success=True))
    with patch("signal_mcp.cli.SignalClient", return_value=_mock_cli_client(send_message=send)):
        result = CliRunner().invoke(cli, [
            "send", "alice.42", "hi", "--quote-author", "+18888888888",
            "--quote-timestamp", "5", "--no-urgent",
        ])
    assert result.exit_code == 0, result.output
    args, kwargs = send.call_args
    assert args == (None, "hi")
    assert kwargs["username"] == "alice.42"
    assert kwargs["quote_timestamp"] == 5
    assert kwargs["no_urgent"] is True


def test_cli_send_number_passes_recipient():
    send = AsyncMock(return_value=SendResult(timestamp=1, recipient="+19999999999", success=True))
    with patch("signal_mcp.cli.SignalClient", return_value=_mock_cli_client(send_message=send)):
        result = CliRunner().invoke(cli, ["send", "+19999999999", "hi", "--preview-url", "https://x.org"])
    assert result.exit_code == 0, result.output
    assert send.call_args.args == ("+19999999999", "hi")
    assert send.call_args.kwargs["preview_url"] == "https://x.org"


def test_cli_send_group_flags():
    send = AsyncMock(return_value=SendResult(timestamp=1, recipient="grp==", success=True))
    with patch("signal_mcp.cli.SignalClient", return_value=_mock_cli_client(send_group_message=send)):
        result = CliRunner().invoke(cli, ["send-group", "grp==", "hi", "--preview-title", "T"])
    assert result.exit_code == 0, result.output
    assert send.call_args.kwargs["preview_title"] == "T"


def test_cli_story():
    story = AsyncMock(return_value=SendResult(timestamp=42, recipient="my_story", success=True))
    with patch("signal_mcp.cli.SignalClient", return_value=_mock_cli_client(send_story=story)):
        result = CliRunner().invoke(cli, ["story", "/tmp/p.jpg", "--group", "grp==", "--no-replies"])
    assert result.exit_code == 0, result.output
    assert "42" in result.output
    story.assert_awaited_once_with("/tmp/p.jpg", group_id="grp==", allow_replies=False)


def test_cli_story_error():
    story = AsyncMock(side_effect=SignalError("bad path"))
    with patch("signal_mcp.cli.SignalClient", return_value=_mock_cli_client(send_story=story)):
        result = CliRunner().invoke(cli, ["story", "/etc/passwd"])
    assert result.exit_code == 1
    assert "bad path" in result.output
