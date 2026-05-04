"""Coverage tests for signal_mcp/client.py — uncovered lines."""

import asyncio
import shutil
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

import signal_mcp.client as client_mod
import signal_mcp.store as _store_mod
from signal_mcp.client import SignalClient, SignalError
from signal_mcp.config import DAEMON_URL
from signal_mcp.models import Contact, Group, Message, SendResult


def rpc_ok(result) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def rpc_err(message: str, code: int = -1) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}}


@pytest.fixture(autouse=True)
def reset_store(monkeypatch, tmp_path):
    monkeypatch.setattr(_store_mod, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(_store_mod, "_initialized_paths", set())
    if getattr(_store_mod._thread_local, "conn", None) is not None:
        _store_mod._thread_local.conn.close()
        _store_mod._thread_local.conn = None


@pytest.fixture(autouse=True)
def reset_caches(monkeypatch):
    monkeypatch.setattr(client_mod, "_contact_cache", {})
    monkeypatch.setattr(client_mod, "_contact_cache_loaded", False)
    monkeypatch.setattr(client_mod, "_contact_cache_at", 0.0)
    monkeypatch.setattr(client_mod, "_group_cache", {})
    monkeypatch.setattr(client_mod, "_group_cache_loaded", False)
    monkeypatch.setattr(client_mod, "_group_cache_at", 0.0)


@pytest.fixture
def client():
    return SignalClient(account="+10000000000")


# ── account property lazy init ────────────────────────────────────────────────

def test_account_lazy_init():
    """SignalClient without account calls detect_account on first access."""
    c = SignalClient()
    with patch("signal_mcp.client.detect_account", return_value="+49test"):
        assert c.account == "+49test"


# ── close() / __aenter__ / __aexit__ ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_close_cancels_background_tasks():
    c = SignalClient(account="+10000000000")
    task = asyncio.create_task(asyncio.sleep(100))
    c._background_tasks.append(task)
    await c.close()
    assert task.cancelled()
    assert c._background_tasks == []


@pytest.mark.asyncio
async def test_context_manager():
    async with SignalClient(account="+1test") as c:
        assert c is not None


# ── _daemon_alive ─────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_daemon_alive_true(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200))
    result = await client._daemon_alive()
    assert result is True


@respx.mock
@pytest.mark.asyncio
async def test_daemon_alive_false_on_connect_error(client):
    respx.post(DAEMON_URL).mock(side_effect=httpx.ConnectError("refused"))
    result = await client._daemon_alive()
    assert result is False


# ── prewarm / _start_watchdog ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prewarm_starts_watchdog(client):
    with patch.object(client, "ensure_daemon", new_callable=AsyncMock) as mock_ed, \
         patch.object(client, "_start_watchdog") as mock_sw:
        await client.prewarm()
        mock_sw.assert_called_once()


@pytest.mark.asyncio
async def test_start_watchdog_idempotent(client):
    """Calling _start_watchdog twice only creates one watchdog task."""
    async def _run():
        client._start_watchdog()
        initial_count = len([t for t in client._background_tasks
                              if getattr(t, "_is_watchdog", False)])
        client._start_watchdog()
        second_count = len([t for t in client._background_tasks
                            if getattr(t, "_is_watchdog", False)])
        assert initial_count == 1
        assert second_count == 1
        # Cancel so we don't leak
        for t in list(client._background_tasks):
            t.cancel()
        if client._background_tasks:
            await asyncio.gather(*client._background_tasks, return_exceptions=True)

    await _run()


# ── watchdog ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_watchdog_returns_on_cancelled():
    """watchdog() returns cleanly when CancelledError is raised."""
    c = SignalClient(account="+1test")

    async def _sleep_raise(*args, **kwargs):
        raise asyncio.CancelledError()

    with patch("signal_mcp.client.asyncio.sleep", side_effect=_sleep_raise):
        await c.watchdog()  # should return without raising


@pytest.mark.asyncio
async def test_watchdog_calls_ensure_daemon_when_dead():
    """watchdog calls ensure_daemon when daemon is not alive."""
    c = SignalClient(account="+1test")
    sleep_calls = []

    async def _sleep(secs):
        sleep_calls.append(secs)
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError()

    with patch("signal_mcp.client.asyncio.sleep", side_effect=_sleep), \
         patch.object(c, "_daemon_alive", new_callable=AsyncMock, return_value=False), \
         patch.object(c, "ensure_daemon", new_callable=AsyncMock) as mock_ed:
        await c.watchdog()
        mock_ed.assert_called()


@pytest.mark.asyncio
async def test_watchdog_swallows_exception():
    """watchdog swallows non-CancelledError exceptions and continues."""
    c = SignalClient(account="+1test")
    call_count = [0]

    async def _sleep(secs):
        call_count[0] += 1
        if call_count[0] >= 2:
            raise asyncio.CancelledError()

    async def _alive():
        raise RuntimeError("boom")

    with patch("signal_mcp.client.asyncio.sleep", side_effect=_sleep), \
         patch.object(c, "_daemon_alive", side_effect=_alive):
        await c.watchdog()  # should not raise RuntimeError


# ── send_group_message with quote ────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_send_group_message_with_quote(client):
    captured = {}

    def capture_request(request, route):
        captured["body"] = request.content
        return httpx.Response(200, json=rpc_ok({"timestamp": 5555}))

    respx.post(DAEMON_URL).mock(side_effect=capture_request)
    await client.send_group_message(
        "grp123==", "hi",
        quote_author="+1999", quote_timestamp=9999
    )
    import json as _json
    body = _json.loads(captured["body"])
    params = body["params"]
    assert params.get("quoteAuthor") == "+1999"
    assert params.get("quoteTimestamp") == 9999


# ── send_group_attachment with view_once ────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_send_group_attachment_view_once(client, tmp_path):
    att_file = tmp_path / "img.jpg"
    att_file.write_bytes(b"fake-image")

    captured = {}

    def capture_request(request, route):
        import json as _json
        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, json=rpc_ok({"timestamp": 7777}))

    respx.post(DAEMON_URL).mock(side_effect=capture_request)
    await client.send_group_attachment("grp==", str(att_file), view_once=True)
    assert captured["body"]["params"].get("viewOnce") is True


# ── _parse_envelope without dataMessage ──────────────────────────────────────

def test_parse_envelope_no_data_message(client):
    """Envelope with no dataMessage (and no other known message types) returns None at line 569."""
    envelope = {
        "envelope": {
            "source": "+1999",
            "sourceNumber": "+1999",
            "timestamp": 1000000,
            # No dataMessage, no receiptMessage, no typingMessage, no syncMessage
        }
    }
    result = client._parse_envelope(envelope)
    assert result is None


def test_parse_envelope_typing_message(client):
    """Typing indicator envelope returns None at line 541."""
    envelope = {
        "envelope": {
            "source": "+1999",
            "timestamp": 1000000,
            "typingMessage": {"action": "STARTED"},
        }
    }
    result = client._parse_envelope(envelope)
    assert result is None


# ── _parse_attachments shutil.copy2 success ──────────────────────────────────

def test_parse_attachments_copy_success(client, tmp_path):
    """When source file exists, copy2 succeeds and local_path is updated to dest (line 599)."""
    src = tmp_path / "signal_attachment_abc123"
    src.write_bytes(b"binary-data")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    with patch("signal_mcp.client.ensure_attachment_dir", return_value=dest_dir):
        attachments = client._parse_attachments({
            "attachments": [
                {
                    "filename": str(src),
                    "contentType": "image/jpeg",
                }
            ]
        })

    assert len(attachments) == 1
    # local_path should be updated to dest (line 599 was reached)
    assert attachments[0].local_path == str(dest_dir / src.name)


# ── _ensure_contact_cache happy path ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_ensure_contact_cache_populates(client):
    contact = Contact(number="+1999", name="Alice")
    with patch.object(client, "list_contacts", new_callable=AsyncMock,
                      return_value=[contact]):
        await client._ensure_contact_cache()
    assert client_mod._contact_cache.get("+1999") == "Alice"
    assert client_mod._contact_cache_loaded is True


# ── _ensure_group_cache failure ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ensure_group_cache_failure_leaves_unloaded(client):
    with patch.object(client, "list_groups", new_callable=AsyncMock,
                      side_effect=Exception("daemon down")):
        await client._ensure_group_cache()
    assert client_mod._group_cache_loaded is False


# ── _enrich_message with recipient and group_id ──────────────────────────────

def test_enrich_message_with_recipient(client):
    msg = Message(
        id="1", sender="+1", recipient="+2", body="hi",
        timestamp=datetime.now()
    )
    result = client._enrich_message(msg)
    assert "recipient_name" in result


def test_enrich_message_with_group_id(client):
    msg = Message(
        id="1", sender="+1", group_id="grp==", body="hi",
        timestamp=datetime.now()
    )
    result = client._enrich_message(msg)
    assert "group_name" in result


# ── get_profile fallback ──────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_get_profile_fallback_returns_bare_contact(client):
    """When RPC returns empty list, get_profile returns Contact with just number."""
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([])))
    result = await client.get_profile("+1999")
    assert result.number == "+1999"


# ── update_profile with avatar_path and remove_avatar ────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_update_profile_avatar_path(client, tmp_path):
    avatar = tmp_path / "avatar.jpg"
    avatar.write_bytes(b"img")
    captured = {}

    def capture(request, route):
        import json as _j
        captured["body"] = _j.loads(request.content)
        return httpx.Response(200, json=rpc_ok(None))

    respx.post(DAEMON_URL).mock(side_effect=capture)
    await client.update_profile(avatar_path=str(avatar))
    assert "avatarPath" in captured["body"]["params"]


@respx.mock
@pytest.mark.asyncio
async def test_update_profile_remove_avatar(client):
    captured = {}

    def capture(request, route):
        import json as _j
        captured["body"] = _j.loads(request.content)
        return httpx.Response(200, json=rpc_ok(None))

    respx.post(DAEMON_URL).mock(side_effect=capture)
    await client.update_profile(remove_avatar=True)
    assert captured["body"]["params"].get("removeAvatar") is True


# ── create_group with description ────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_create_group_with_description(client):
    captured = {}

    def capture(request, route):
        import json as _j
        captured["body"] = _j.loads(request.content)
        return httpx.Response(200, json=rpc_ok({"groupId": "abc=="}))

    respx.post(DAEMON_URL).mock(side_effect=capture)
    await client.create_group("My Group", ["+1999"], description="A test group")
    assert captured["body"]["params"].get("description") == "A test group"


# ── update_group optional params ─────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_update_group_optional_params(client):
    captured = {}

    def capture(request, route):
        import json as _j
        captured["body"] = _j.loads(request.content)
        return httpx.Response(200, json=rpc_ok(None))

    respx.post(DAEMON_URL).mock(side_effect=capture)
    await client.update_group(
        "grp==",
        description="Updated desc",
        remove_members=["+1888"],
        link_mode="enabled",
    )
    params = captured["body"]["params"]
    assert params.get("description") == "Updated desc"
    assert params.get("removeMember") == ["+1888"]
    assert params.get("link") == "enabled"


# ── update_configuration with linkPreviews / unidentifiedDeliveryIndicators ──

@respx.mock
@pytest.mark.asyncio
async def test_update_configuration_link_previews(client):
    captured = {}

    def capture(request, route):
        import json as _j
        captured["body"] = _j.loads(request.content)
        return httpx.Response(200, json=rpc_ok(None))

    respx.post(DAEMON_URL).mock(side_effect=capture)
    await client.update_configuration(link_previews=True, unidentified_delivery_indicators=False)
    params = captured["body"]["params"]
    assert params.get("linkPreviews") is True
    assert params.get("unidentifiedDeliveryIndicators") is False


# ── get_sticker / upload_sticker_pack fallback (non-dict result) ──────────────

@respx.mock
@pytest.mark.asyncio
async def test_get_sticker_string_result(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok("base64data")))
    result = await client.get_sticker("packid", 1)
    assert result == "base64data"


@respx.mock
@pytest.mark.asyncio
async def test_upload_sticker_pack_string_result(client, tmp_path):
    fake_path = tmp_path / "manifest.json"
    fake_path.write_text("{}")
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok("https://signal.art/pack/abc")))
    result = await client.upload_sticker_pack(str(fake_path))
    assert result == "https://signal.art/pack/abc"


# ── list_accounts non-list fallback ──────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_list_accounts_non_list(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok(None)))
    result = await client.list_accounts()
    assert result == []


# ── update_account with unrestricted_unidentified_sender ─────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_update_account_unrestricted_unidentified_sender(client):
    captured = {}

    def capture(request, route):
        import json as _j
        captured["body"] = _j.loads(request.content)
        return httpx.Response(200, json=rpc_ok(None))

    respx.post(DAEMON_URL).mock(side_effect=capture)
    await client.update_account(unrestricted_unidentified_sender=True)
    assert captured["body"]["params"].get("unrestrictedUnidentifiedSender") is True


# ── receive_stream exception handler ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_receive_stream_swallows_exception():
    """receive_stream swallows non-Cancelled exceptions and retries.
    CancelledError causes clean return (no re-raise) from receive_stream."""
    c = SignalClient(account="+1test")
    call_count = [0]

    async def mock_receive(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise Exception("daemon hiccup")
        # Second call: raise CancelledError — receive_stream should return cleanly
        raise asyncio.CancelledError()

    async def mock_sleep(secs):
        pass  # fast

    msgs = []
    with patch.object(c, "receive_messages", side_effect=mock_receive), \
         patch("signal_mcp.client.asyncio.sleep", side_effect=mock_sleep):
        # receive_stream catches CancelledError and returns — no exception propagates
        async for msg in c.receive_stream(poll_interval=0):
            msgs.append(msg)

    # No messages yielded, first exception swallowed, second (CancelledError) caused return
    assert call_count[0] == 2
    assert msgs == []


# ── set_expiration_timer neither recipient nor group_id ───────────────────────

@pytest.mark.asyncio
async def test_set_expiration_timer_raises_without_target(client):
    with pytest.raises(SignalError, match="Either"):
        await client.set_expiration_timer(expiration=60)


# ── pin_message recipient path ────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_pin_message_with_recipient(client):
    captured = {}

    def capture(request, route):
        import json as _j
        captured["body"] = _j.loads(request.content)
        return httpx.Response(200, json=rpc_ok(None))

    respx.post(DAEMON_URL).mock(side_effect=capture)
    await client.pin_message("+1author", 12345, recipient="+1target")
    assert captured["body"]["params"].get("recipient") == ["+1target"]


# ── unpin_message neither / group_id ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_unpin_message_raises_without_target(client):
    with pytest.raises(SignalError, match="Either"):
        await client.unpin_message("+1author", 12345)


@respx.mock
@pytest.mark.asyncio
async def test_unpin_message_with_group_id(client):
    captured = {}

    def capture(request, route):
        import json as _j
        captured["body"] = _j.loads(request.content)
        return httpx.Response(200, json=rpc_ok(None))

    respx.post(DAEMON_URL).mock(side_effect=capture)
    await client.unpin_message("+1author", 12345, group_id="grp==")
    assert captured["body"]["params"].get("groupId") == "grp=="


# ── get_avatar fallback ───────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_get_avatar_string_result(client):
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok("base64avatardata")))
    result = await client.get_avatar("+1test")
    assert result == "base64avatardata"


# ── create_poll multi_select and recipient ───────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_create_poll_multi_select(client):
    captured = {}

    def capture(request, route):
        import json as _j
        captured["body"] = _j.loads(request.content)
        return httpx.Response(200, json=rpc_ok({"timestamp": 111}))

    respx.post(DAEMON_URL).mock(side_effect=capture)
    await client.create_poll("Q?", ["A", "B"], group_id="grp==", multi_select=True)
    assert captured["body"]["params"].get("poll-multi-select") is True


@respx.mock
@pytest.mark.asyncio
async def test_create_poll_with_recipient(client):
    captured = {}

    def capture(request, route):
        import json as _j
        captured["body"] = _j.loads(request.content)
        return httpx.Response(200, json=rpc_ok({"timestamp": 222}))

    respx.post(DAEMON_URL).mock(side_effect=capture)
    result = await client.create_poll("Q?", ["A", "B"], recipient="+1999")
    assert captured["body"]["params"].get("recipient") == ["+1999"]
    assert result.recipient == "+1999"


# ── vote_poll neither / recipient ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_vote_poll_raises_without_target(client):
    with pytest.raises(SignalError, match="Either"):
        await client.vote_poll("+1author", 111, 1, [0])


@respx.mock
@pytest.mark.asyncio
async def test_vote_poll_with_recipient(client):
    captured = {}

    def capture(request, route):
        import json as _j
        captured["body"] = _j.loads(request.content)
        return httpx.Response(200, json=rpc_ok(None))

    respx.post(DAEMON_URL).mock(side_effect=capture)
    await client.vote_poll("+1author", 111, 1, [0], recipient="+1target")
    assert captured["body"]["params"].get("recipient") == ["+1target"]


# ── terminate_poll neither / recipient ────────────────────────────────────────

@pytest.mark.asyncio
async def test_terminate_poll_raises_without_target(client):
    with pytest.raises(SignalError, match="Either"):
        await client.terminate_poll("+1author", 111, 1)


@respx.mock
@pytest.mark.asyncio
async def test_terminate_poll_with_recipient(client):
    captured = {}

    def capture(request, route):
        import json as _j
        captured["body"] = _j.loads(request.content)
        return httpx.Response(200, json=rpc_ok(None))

    respx.post(DAEMON_URL).mock(side_effect=capture)
    await client.terminate_poll("+1author", 111, 1, recipient="+1target")
    assert captured["body"]["params"].get("recipient") == ["+1target"]


# ── trust_identity with safety_number ────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_trust_identity_with_safety_number(client):
    captured = {}

    def capture(request, route):
        import json as _j
        captured["body"] = _j.loads(request.content)
        return httpx.Response(200, json=rpc_ok(None))

    respx.post(DAEMON_URL).mock(side_effect=capture)
    await client.trust_identity("+1test", safety_number="abc123")
    assert captured["body"]["params"].get("verifiedSafetyNumber") == "abc123"


# ── ensure_daemon ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ensure_daemon_fast_path_already_alive(client, monkeypatch):
    """ensure_daemon returns immediately if daemon is already alive."""
    monkeypatch.setattr(client, "_daemon_alive", AsyncMock(return_value=True))
    popen_mock = MagicMock()
    with patch("signal_mcp.client.subprocess.Popen", popen_mock):
        await client.ensure_daemon()
    popen_mock.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_daemon_starts_when_dead(client, monkeypatch, tmp_path):
    """ensure_daemon spawns signal-cli and polls until alive."""
    import signal_mcp.config as _conf
    monkeypatch.setattr(_conf, "DAEMON_PID_FILE", tmp_path / "daemon.pid")

    # Daemon not alive initially, starts after first poll
    alive_calls = [False, False, True]
    alive_iter = iter(alive_calls)
    monkeypatch.setattr(client, "_daemon_alive", AsyncMock(side_effect=alive_iter))
    monkeypatch.setattr("signal_mcp.client.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("signal_mcp.client.read_daemon_pid", lambda: None)
    monkeypatch.setattr("signal_mcp.client.clear_daemon_pid", lambda: None)
    monkeypatch.setattr("signal_mcp.client.save_daemon_pid", lambda pid: None)

    proc_mock = MagicMock()
    proc_mock.pid = 99999
    with patch("signal_mcp.client.subprocess.Popen", return_value=proc_mock):
        await client.ensure_daemon()


@pytest.mark.asyncio
async def test_ensure_daemon_kills_stale_pid(client, monkeypatch, tmp_path):
    """ensure_daemon terminates stale PID before starting new daemon."""
    import os
    import signal as _signal

    killed = {}

    def fake_kill(pid, sig):
        killed["pid"] = pid
        killed["sig"] = sig

    # Daemon not alive; stale pid 55555 exists; daemon alive after first poll
    alive_iter = iter([False, False, True])
    monkeypatch.setattr(client, "_daemon_alive", AsyncMock(side_effect=alive_iter))
    monkeypatch.setattr("signal_mcp.client.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("signal_mcp.client.read_daemon_pid", lambda: 55555)
    monkeypatch.setattr("signal_mcp.client.clear_daemon_pid", lambda: None)
    monkeypatch.setattr("signal_mcp.client.save_daemon_pid", lambda pid: None)
    monkeypatch.setattr("signal_mcp.client.os.kill", fake_kill)

    proc_mock = MagicMock()
    proc_mock.pid = 99998
    with patch("signal_mcp.client.subprocess.Popen", return_value=proc_mock):
        await client.ensure_daemon()

    assert killed.get("pid") == 55555


@pytest.mark.asyncio
async def test_ensure_daemon_stale_pid_already_gone(client, monkeypatch):
    """ensure_daemon handles ProcessLookupError when killing stale PID."""
    def fake_kill(pid, sig):
        raise ProcessLookupError

    alive_iter = iter([False, False, True])
    monkeypatch.setattr(client, "_daemon_alive", AsyncMock(side_effect=alive_iter))
    monkeypatch.setattr("signal_mcp.client.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("signal_mcp.client.read_daemon_pid", lambda: 55556)
    monkeypatch.setattr("signal_mcp.client.clear_daemon_pid", lambda: None)
    monkeypatch.setattr("signal_mcp.client.save_daemon_pid", lambda pid: None)
    monkeypatch.setattr("signal_mcp.client.os.kill", fake_kill)

    proc_mock = MagicMock()
    proc_mock.pid = 99997
    with patch("signal_mcp.client.subprocess.Popen", return_value=proc_mock):
        await client.ensure_daemon()  # must not raise


@pytest.mark.asyncio
async def test_ensure_daemon_timeout_raises(client, monkeypatch):
    """ensure_daemon raises SignalError when daemon never starts within 20 polls."""
    monkeypatch.setattr(client, "_daemon_alive", AsyncMock(return_value=False))
    monkeypatch.setattr("signal_mcp.client.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("signal_mcp.client.read_daemon_pid", lambda: None)
    monkeypatch.setattr("signal_mcp.client.clear_daemon_pid", lambda: None)
    monkeypatch.setattr("signal_mcp.client.save_daemon_pid", lambda pid: None)

    proc_mock = MagicMock()
    proc_mock.pid = 99996
    with patch("signal_mcp.client.subprocess.Popen", return_value=proc_mock):
        with pytest.raises(SignalError, match="daemon failed to start"):
            await client.ensure_daemon()


# ── stop_daemon ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stop_daemon_with_pid_file(client, monkeypatch):
    """stop_daemon sends SIGTERM to pid from file and returns True."""
    killed = {}

    def fake_kill(pid, sig):
        killed["pid"] = pid

    monkeypatch.setattr("signal_mcp.client.read_daemon_pid", lambda: 77777)
    monkeypatch.setattr("signal_mcp.client.clear_daemon_pid", lambda: None)
    monkeypatch.setattr("signal_mcp.client.os.kill", fake_kill)

    result = await client.stop_daemon()
    assert result is True
    assert killed["pid"] == 77777


@pytest.mark.asyncio
async def test_stop_daemon_pid_already_gone(client, monkeypatch):
    """stop_daemon handles ProcessLookupError on pid from file, falls through to lsof."""
    def fake_kill(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr("signal_mcp.client.read_daemon_pid", lambda: 77778)
    monkeypatch.setattr("signal_mcp.client.clear_daemon_pid", lambda: None)
    monkeypatch.setattr("signal_mcp.client.os.kill", fake_kill)
    # lsof returns empty — no port listeners
    lsof_result = MagicMock()
    lsof_result.stdout = ""
    with patch("signal_mcp.client.subprocess.run", return_value=lsof_result):
        result = await client.stop_daemon()
    assert result is False


@pytest.mark.asyncio
async def test_stop_daemon_no_pid_lsof_finds_process(client, monkeypatch):
    """stop_daemon falls back to lsof when no pid file, kills found pid."""
    killed = {}
    kill_calls = []

    def fake_kill(pid, sig):
        kill_calls.append(pid)
        killed["pid"] = pid

    monkeypatch.setattr("signal_mcp.client.read_daemon_pid", lambda: None)
    monkeypatch.setattr("signal_mcp.client.os.kill", fake_kill)

    lsof_result = MagicMock()
    lsof_result.stdout = "88888\n"
    with patch("signal_mcp.client.subprocess.run", return_value=lsof_result):
        result = await client.stop_daemon()
    assert result is True
    assert 88888 in kill_calls


@pytest.mark.asyncio
async def test_stop_daemon_lsof_not_found(client, monkeypatch):
    """stop_daemon returns False when lsof is not installed."""
    monkeypatch.setattr("signal_mcp.client.read_daemon_pid", lambda: None)

    with patch("signal_mcp.client.subprocess.run", side_effect=FileNotFoundError):
        result = await client.stop_daemon()
    assert result is False


@pytest.mark.asyncio
async def test_stop_daemon_lsof_invalid_pid(client, monkeypatch):
    """stop_daemon skips non-integer lines in lsof output."""
    killed = []

    def fake_kill(pid, sig):
        killed.append(pid)

    monkeypatch.setattr("signal_mcp.client.read_daemon_pid", lambda: None)
    monkeypatch.setattr("signal_mcp.client.os.kill", fake_kill)

    lsof_result = MagicMock()
    lsof_result.stdout = "notanumber\n"
    with patch("signal_mcp.client.subprocess.run", return_value=lsof_result):
        result = await client.stop_daemon()
    assert result is False
    assert killed == []


@pytest.mark.asyncio
async def test_ensure_daemon_second_check_inside_lock(client, monkeypatch):
    """ensure_daemon returns when second _daemon_alive check (inside lock) is True.

    Simulates the double-checked locking path: outer check False (daemon dead),
    then another coroutine starts the daemon while we wait for the lock,
    so the inner re-check returns True.
    """
    # First call: outer fast-path check → False (need to acquire lock)
    # Second call: inner re-check after acquiring lock → True (already started)
    alive_results = iter([False, True])
    monkeypatch.setattr(client, "_daemon_alive", AsyncMock(side_effect=alive_results))

    popen_mock = MagicMock()
    with patch("signal_mcp.client.subprocess.Popen", popen_mock):
        await client.ensure_daemon()

    # Popen must NOT have been called — the inner re-check found daemon alive
    popen_mock.assert_not_called()
