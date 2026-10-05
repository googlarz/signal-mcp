"""Incoming dataMessage fields beyond the body (mentions, previews, polls, deletes, ...).

Envelope fixtures follow signal-cli's JSON records (src/main/java/org/asamk/signal/json/
JsonDataMessage.java and the Json* classes it references).
"""

import json
import sqlite3
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

import signal_mcp.client as _client_mod
import signal_mcp.store as _store_mod
from signal_mcp.client import SignalClient, _resolve_mentions
from signal_mcp.config import DAEMON_URL
from signal_mcp.models import Message
from tests.conftest import call_tool

OWN = "+10000000000"
ALICE = "+13333333333"
ALICE_UUID = "aaaaaaaa-0000-0000-0000-000000000001"
BOB = "+14444444444"
BOB_UUID = "bbbbbbbb-0000-0000-0000-000000000002"
TS = 1700000000000


def rpc_ok(result) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def envelope(data_message: dict, source: str = ALICE) -> dict:
    return {"envelope": {
        "source": source, "sourceNumber": source, "sourceUuid": ALICE_UUID,
        "timestamp": data_message.get("timestamp", TS),
        "dataMessage": {"timestamp": TS, "message": None, "expiresInSeconds": 0,
                        "attachments": [], **data_message},
    }}


@pytest.fixture(autouse=True)
def reset_store(monkeypatch, tmp_path):
    monkeypatch.setattr(_store_mod, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(_store_mod, "_initialized_paths", set())
    if getattr(_store_mod._thread_local, "conn", None) is not None:
        _store_mod._thread_local.conn.close()
        _store_mod._thread_local.conn = None


@pytest.fixture(autouse=True)
def reset_caches(monkeypatch):
    monkeypatch.setattr(_client_mod, "_contact_cache", {BOB: "Bob"})
    monkeypatch.setattr(_client_mod, "_contact_cache_loaded", True)
    monkeypatch.setattr(_client_mod, "_contact_cache_at", time.monotonic())
    monkeypatch.setattr(_client_mod, "_group_cache", {})
    monkeypatch.setattr(_client_mod, "_group_cache_loaded", True)
    monkeypatch.setattr(_client_mod, "_group_cache_at", time.monotonic())
    monkeypatch.setattr(_client_mod, "_daemon_last_ok_at", 0.0)


@pytest.fixture
def client(monkeypatch):
    c = SignalClient(account=OWN)
    monkeypatch.setattr("signal_mcp.server._client", c)

    async def noop():
        pass
    monkeypatch.setattr(c, "ensure_daemon", noop)
    return c


# ── parsing ───────────────────────────────────────────────────────────────────

def test_mentions_parsed_body_unchanged(client):
    msg = client._parse_envelope(envelope({
        "message": "hi ￼!",
        "mentions": [{"name": BOB, "number": BOB, "uuid": BOB_UUID, "start": 3, "length": 1}],
    }))
    assert msg.body == "hi ￼!"
    assert msg.extras["mentions"] == [{"number": BOB, "uuid": BOB_UUID, "start": 3, "length": 1}]


def test_mention_at_offset_zero_keeps_start(client):
    msg = client._parse_envelope(envelope({
        "message": "￼ hi", "mentions": [{"uuid": BOB_UUID, "start": 0, "length": 1}],
    }))
    assert msg.extras["mentions"] == [{"uuid": BOB_UUID, "start": 0, "length": 1}]


def test_resolve_mentions_utf16_offsets_after_emoji():
    # "😀" is 2 UTF-16 units but 1 codepoint: the mention sits at UTF-16 offset 3.
    body = "😀 ￼ and ￼"
    mentions = [{"number": BOB, "start": 3, "length": 1},
                {"uuid": ALICE_UUID, "start": 9, "length": 1}]
    names = {BOB: "Bob", ALICE_UUID: "Alice"}
    assert _resolve_mentions(body, mentions, lambda k: names.get(k, k)) == "😀 @Bob and @Alice"


def test_resolve_mentions_skips_out_of_range():
    assert _resolve_mentions("x", [{"number": BOB, "start": 5, "length": 1}], str) == "x"


def test_text_styles_previews_quote_voice_note(client):
    msg = client._parse_envelope(envelope({
        "message": "look https://example.com",
        "textStyles": [{"style": "BOLD", "start": 0, "length": 4}],
        "previews": [{"url": "https://example.com", "title": "Example", "description": "",
                      "image": None}],
        "quote": {"id": 1699999999999, "author": ALICE, "authorNumber": ALICE,
                  "authorUuid": ALICE_UUID, "text": "original ￼",
                  "mentions": [{"number": BOB, "uuid": BOB_UUID, "start": 9, "length": 1}],
                  "attachments": [{"contentType": "image/jpeg", "filename": "a.jpg",
                                   "thumbnail": None}]},
        "attachments": [{"contentType": "audio/aac", "filename": None, "id": "x.aac",
                         "size": 10, "width": None, "height": None, "caption": None,
                         "uploadTimestamp": TS, "isVoiceNote": True}],
    }))
    assert msg.quote_id == "1699999999999"
    assert msg.extras["text_styles"] == [{"style": "BOLD", "start": 0, "length": 4}]
    assert msg.extras["previews"] == [{"url": "https://example.com", "title": "Example"}]
    assert msg.extras["quote"] == {
        "author_number": ALICE, "author_uuid": ALICE_UUID, "text": "original ￼",
        "mentions": [{"number": BOB, "uuid": BOB_UUID, "start": 9, "length": 1}],
        "attachments": [{"content_type": "image/jpeg", "filename": "a.jpg"}],
    }
    assert msg.extras["voice_note"] is True


def test_sticker_payment_contacts_story(client):
    msg = client._parse_envelope(envelope({
        "sticker": {"packId": "abcd", "stickerId": 3},
        "payment": {"note": "lunch", "receipt": "AAEC"},
        "contacts": [{"name": {"nickname": None, "given": "Carol", "family": "Doe",
                               "prefix": None, "suffix": None, "middle": None},
                      "avatar": None,
                      "phone": [{"value": "+15555555555", "type": "MOBILE", "label": None}],
                      "email": [{"value": "c@example.com", "type": "HOME", "label": None}],
                      "address": [], "organization": "ACME"}],
        "storyContext": {"authorNumber": BOB, "authorUuid": BOB_UUID, "sentTimestamp": TS - 5},
    }))
    assert msg.extras["sticker"] == {"pack_id": "abcd", "sticker_id": 3}
    assert msg.extras["payment"] == {"note": "lunch"}
    assert msg.extras["shared_contacts"] == [{
        "name": "Carol Doe", "phones": ["+15555555555"], "emails": ["c@example.com"],
        "organization": "ACME"}]
    assert msg.extras["story_context"] == {
        "author_number": BOB, "author_uuid": BOB_UUID, "sent_timestamp": TS - 5}


def test_polls(client):
    create = client._parse_envelope(envelope({
        "pollCreate": {"question": "Lunch?", "allowMultiple": False, "options": ["A", "B"]}}))
    assert create.extras["poll_create"] == {"question": "Lunch?", "options": ["A", "B"]}
    vote = client._parse_envelope(envelope({
        "pollVote": {"author": BOB, "authorNumber": BOB, "authorUuid": BOB_UUID,
                     "targetSentTimestamp": TS - 1, "optionIndexes": [0, 1], "voteCount": 2}}))
    assert vote.extras["poll_vote"] == {
        "poll_author_number": BOB, "poll_author_uuid": BOB_UUID, "poll_timestamp": TS - 1,
        "option_indexes": [0, 1], "vote_count": 2}
    term = client._parse_envelope(envelope({"pollTerminate": {"targetSentTimestamp": TS - 1}}))
    assert term.extras["poll_terminate"] == {"poll_timestamp": TS - 1}


def test_pin_unpin(client):
    target = {"targetAuthor": BOB, "targetAuthorNumber": BOB, "targetAuthorUuid": BOB_UUID,
              "targetSentTimestamp": TS - 1}
    pin = client._parse_envelope(envelope({"pinMessage": target | {"pinDurationSeconds": 3600}}))
    assert pin.extras["pin_message"] == {
        "target_author_number": BOB, "target_author_uuid": BOB_UUID,
        "target_timestamp": TS - 1, "duration_seconds": 3600}
    unpin = client._parse_envelope(envelope({"unpinMessage": target}))
    assert unpin.extras["unpin_message"] == {
        "target_author_number": BOB, "target_author_uuid": BOB_UUID, "target_timestamp": TS - 1}


def test_event_flags(client):
    msg = client._parse_envelope(envelope({
        "expiresInSeconds": 3600, "isExpirationUpdate": True, "isEndSession": True,
        "isProfileKeyUpdate": True, "groupCallUpdate": {"eraId": "era1"}}))
    assert msg.extras == {"is_expiration_update": True, "is_end_session": True,
                          "is_profile_key_update": True, "group_call_update": {"era_id": "era1"}}
    bare = client._parse_envelope(envelope({"groupCallUpdate": {"eraId": None}}))
    assert bare.extras == {"group_call_update": True}


def test_plain_message_has_no_extras(client):
    msg = client._parse_envelope(envelope({"message": "hello"}))
    assert msg.extras == {}
    assert set(msg.to_dict()) == {
        "id", "sender", "recipient", "body", "timestamp", "attachments", "group_id",
        "quote_id", "reactions", "is_read", "receipt_type", "expires_in_seconds", "view_once"}


def test_sync_sent_message_extras(client):
    msg = client._parse_envelope({"envelope": {"source": OWN, "timestamp": TS, "syncMessage": {
        "sentMessage": {"timestamp": TS, "message": "￼", "destination": BOB,
                        "mentions": [{"number": BOB, "start": 0, "length": 1}]}}}})
    assert msg.recipient == BOB
    assert msg.extras["mentions"] == [{"number": BOB, "start": 0, "length": 1}]


def test_delete_envelopes_not_parsed_as_messages(client):
    assert client._parse_envelope(envelope({"remoteDelete": {"timestamp": TS - 1}})) is None
    assert client._parse_envelope({"envelope": {"syncMessage": {"sentMessage": {
        "timestamp": TS, "remoteDelete": {"timestamp": TS - 1}}}}}) is None


# ── remote / admin delete via receive ─────────────────────────────────────────

def _stored(id_, sender, ts_ms):
    _store_mod.save_message(Message(id=id_, sender=sender, body="secret",
                                    timestamp=datetime.fromtimestamp(ts_ms / 1000)))


@respx.mock
@pytest.mark.asyncio
async def test_remote_delete_flags_target_keeps_body(client):
    _stored("t1", ALICE, TS - 1)
    _stored("other", BOB, TS - 1)  # same timestamp, different sender: untouched
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([
        envelope({"remoteDelete": {"timestamp": TS - 1}})])))
    assert await client.receive_messages(timeout=1) == []
    msgs = {m.id: m for m in _store_mod.search_messages("secret")}
    assert msgs["t1"].extras == {"remote_deleted": True}
    assert msgs["t1"].body == "secret"
    assert msgs["other"].extras == {}
    assert len(msgs) == 2  # the delete envelope itself was not stored


@respx.mock
@pytest.mark.asyncio
async def test_admin_delete_flags_target(client):
    _stored("t2", BOB, TS - 1)
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([
        envelope({"adminDelete": {"targetAuthor": BOB, "targetAuthorNumber": BOB,
                                  "targetAuthorUuid": BOB_UUID,
                                  "targetSentTimestamp": TS - 1}})])))
    await client.receive_messages(timeout=1)
    assert _store_mod.get_conversation(BOB)[0].extras == {"admin_deleted_by": ALICE}


@respx.mock
@pytest.mark.asyncio
async def test_sync_remote_delete_flags_own_message(client):
    _stored("mine", OWN, TS - 1)
    respx.post(DAEMON_URL).mock(return_value=httpx.Response(200, json=rpc_ok([
        {"envelope": {"source": OWN, "timestamp": TS, "syncMessage": {"sentMessage": {
            "timestamp": TS, "destination": BOB, "remoteDelete": {"timestamp": TS - 1}}}}}])))
    await client.receive_messages(timeout=1)
    assert _store_mod.search_messages("secret")[0].extras == {"remote_deleted": True}


@pytest.mark.asyncio
async def test_receive_direct_applies_remote_delete(client, monkeypatch):
    _stored("t3", ALICE, TS - 1)
    monkeypatch.setattr(client, "stop_daemon", AsyncMock(return_value=True))
    proc = MagicMock()
    line = json.dumps(envelope({"remoteDelete": {"timestamp": TS - 1}})).encode()
    proc.communicate = AsyncMock(return_value=(line + b"\n", b""))
    proc.returncode = 0

    async def fake_sleep(*_a, **_kw):
        return None

    with patch("signal_mcp.client.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), \
         patch("signal_mcp.client.asyncio.sleep", fake_sleep):
        assert await client.receive_direct(timeout=1) == []
    assert _store_mod.get_conversation(ALICE)[0].extras == {"remote_deleted": True}


def test_mark_deleted_no_target():
    assert _store_mod.mark_deleted(None, [ALICE], {"remote_deleted": True}) is False
    assert _store_mod.mark_deleted(TS, [None], {"remote_deleted": True}) is False
    assert _store_mod.mark_deleted(TS, [ALICE], {"remote_deleted": True}) is False


def test_mark_deleted_merges_existing_extras():
    _store_mod.save_message(Message(id="p", sender=ALICE, body="",
                                    timestamp=datetime.fromtimestamp(TS / 1000),
                                    extras={"poll_create": {"question": "Q"}}))
    assert _store_mod.mark_deleted(TS, [ALICE], {"remote_deleted": True}) is True
    assert _store_mod.get_conversation(ALICE)[0].extras == {
        "poll_create": {"question": "Q"}, "remote_deleted": True}


# ── persistence / migration ───────────────────────────────────────────────────

EXTRAS = {"mentions": [{"number": BOB, "start": 0, "length": 1}],
          "previews": [{"url": "https://example.com"}], "voice_note": True}


def test_extras_round_trip_single_and_batch():
    ts = datetime(2024, 1, 1)
    _store_mod.save_message(Message(id="a", sender=ALICE, body="￼ hi", timestamp=ts,
                                    extras=EXTRAS))
    _store_mod.save_messages_batch([Message(id="b", sender=ALICE, body="plain", timestamp=ts,
                                            extras={"sticker": {"pack_id": "p"}})])
    got = {m.id: m.extras for m in _store_mod.get_conversation(ALICE)}
    assert got == {"a": EXTRAS, "b": {"sticker": {"pack_id": "p"}}}
    assert [m.id for m in _store_mod.search_messages("hi")] == ["a"]  # FTS still works
    assert _store_mod.get_unread_messages()[0].extras in (EXTRAS, {"sticker": {"pack_id": "p"}})
    exported = json.loads(_store_mod.export_messages("json"))
    assert {e["id"]: e.get("mentions") for e in exported}["a"] == EXTRAS["mentions"]


def test_migration_from_old_schema(tmp_path, monkeypatch):
    """A DB created by v1.39.0 (no extras column) keeps its rows and gains the column."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT,
            body TEXT NOT NULL DEFAULT '', timestamp INTEGER NOT NULL,
            group_id TEXT, quote_id TEXT, is_read INTEGER NOT NULL DEFAULT 0);
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            id UNINDEXED, body, sender, content=messages, content_rowid=rowid);
        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, id, body, sender)
            VALUES (new.rowid, new.id, new.body, new.sender);
        END;
        INSERT INTO messages (id, sender, body, timestamp) VALUES ('old', '+13333333333', 'legacy row', 1700000000000);
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(_store_mod, "DB_PATH", db)

    old = _store_mod.get_conversation(ALICE)
    assert [(m.id, m.body, m.extras) for m in old] == [("old", "legacy row", {})]
    assert [m.id for m in _store_mod.search_messages("legacy")] == ["old"]
    _store_mod.save_message(Message(id="new", sender=ALICE, body="fresh", timestamp=datetime(2024, 1, 1),
                                    extras=EXTRAS))
    assert {m.id: m.extras for m in _store_mod.get_conversation(ALICE)} == {"old": {}, "new": EXTRAS}

    # Re-running the migration on an already-migrated DB is a no-op
    monkeypatch.setattr(_store_mod, "_initialized_paths", set())
    _store_mod.init_db()
    cols = [r[1] for r in _store_mod._connect().execute("PRAGMA table_info(messages)")]
    assert cols.count("extras") == 1


# ── tool output ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_conversation_tool_shows_mentions_and_body_resolved(client):
    _store_mod.save_message(Message(
        id="m", sender=ALICE, body="😀 ￼ ok", timestamp=datetime(2024, 1, 1),
        extras={"mentions": [{"number": BOB, "uuid": BOB_UUID, "start": 3, "length": 1}],
                "text_styles": [{"style": "BOLD", "start": 0, "length": 2}]}))
    result = await call_tool("get_conversation", {"recipient": ALICE})
    msg = json.loads(result[0].text)["messages"][0]
    assert msg["body"] == "😀 ￼ ok"
    assert msg["body_resolved"] == "😀 @Bob ok"
    assert msg["mentions"] == [{"number": BOB, "uuid": BOB_UUID, "start": 3, "length": 1,
                                "name": "Bob"}]
    assert msg["text_styles"] == [{"style": "BOLD", "start": 0, "length": 2}]


@pytest.mark.asyncio
async def test_search_tool_omits_empty_extras(client):
    _store_mod.save_message(Message(id="p", sender=ALICE, body="plain words",
                                    timestamp=datetime(2024, 1, 1)))
    result = await call_tool("search_messages", {"query": "plain"})
    data = json.loads(result[0].text)
    msg = data[0] if isinstance(data, list) else data["messages"][0]
    assert "mentions" not in msg and "body_resolved" not in msg
