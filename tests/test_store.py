"""Tests for local SQLite message store."""

import pytest
from datetime import datetime
from pathlib import Path

from signal_mcp.models import Message, Attachment
from signal_mcp import store


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Redirect DB to a temp file and reset init flag for each test."""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test_messages.db")
    monkeypatch.setattr(store, "_initialized", False)
    if getattr(store._thread_local, "conn", None) is not None:
        store._thread_local.conn.close()
        store._thread_local.conn = None


def make_msg(id="1", sender="+1", body="hello", group_id=None, recipient=None,
             ts=None):
    return Message(
        id=id,
        sender=sender,
        recipient=recipient,
        body=body,
        timestamp=ts or datetime(2024, 6, 1, 12, 0, 0),
        group_id=group_id,
    )


# ── save / dedup ──────────────────────────────────────────────────────────────

def test_save_and_retrieve():
    msg = make_msg(id="100", sender="+11111", body="test message")
    assert store.save_message(msg) is True
    results = store.get_conversation("+11111")
    assert len(results) == 1
    assert results[0].body == "test message"


def test_duplicate_not_saved():
    msg = make_msg(id="200")
    assert store.save_message(msg) is True
    assert store.save_message(msg) is False
    results = store.get_conversation("+1")
    assert len(results) == 1


def test_outgoing_message_marked_read():
    """Messages saved with recipient (outgoing) should be is_read=1."""
    store.save_message(make_msg(id="out1", sender="+me", recipient="+other"))
    msgs = store.get_conversation("+other")
    assert msgs[0].is_read is True


def test_incoming_message_unread():
    store.save_message(make_msg(id="in1", sender="+other"))
    msgs = store.get_conversation("+other")
    assert msgs[0].is_read is False


# ── get_conversation ──────────────────────────────────────────────────────────

def test_get_conversation_by_sender():
    store.save_message(make_msg(id="100", sender="+2", body="hi"))
    results = store.get_conversation("+2")
    assert len(results) == 1


def test_get_conversation_outgoing_dm_included():
    """Outgoing DMs must appear in the conversation with the recipient."""
    store.save_message(make_msg(id="out1", sender="+me", recipient="+other", body="hey"))
    results = store.get_conversation("+other")
    assert len(results) == 1
    assert results[0].body == "hey"


def test_get_conversation_both_directions():
    store.save_message(make_msg(id="in1", sender="+2", body="yo"))
    store.save_message(make_msg(id="out1", sender="+1", recipient="+2", body="sup"))
    results = store.get_conversation("+2")
    assert len(results) == 2


def test_get_conversation_by_group():
    msg = make_msg(id="300", sender="+2", group_id="group-abc")
    store.save_message(msg)
    results = store.get_conversation("group-abc")
    assert len(results) == 1
    assert results[0].group_id == "group-abc"


def test_conversation_limit():
    for i in range(10):
        store.save_message(make_msg(id=str(800 + i), sender="+4", body=f"msg {i}",
                                   ts=datetime(2024, 1, i + 1)))
    results = store.get_conversation("+4", limit=5)
    assert len(results) == 5


def test_get_conversation_since():
    cutoff = datetime(2024, 6, 1)
    store.save_message(make_msg(id="old", sender="+2", ts=datetime(2024, 1, 1)))
    store.save_message(make_msg(id="new", sender="+2", ts=datetime(2024, 7, 1)))
    results = store.get_conversation("+2", since=cutoff)
    assert len(results) == 1
    assert results[0].id == "new"


def test_get_conversation_oldest_first():
    store.save_message(make_msg(id="m1", sender="+2", ts=datetime(2024, 1, 1)))
    store.save_message(make_msg(id="m2", sender="+2", ts=datetime(2024, 1, 2)))
    msgs = store.get_conversation("+2")
    assert msgs[0].id == "m1"
    assert msgs[1].id == "m2"


# ── search_messages ───────────────────────────────────────────────────────────

def test_search_messages():
    store.save_message(make_msg(id="400", body="hello world"))
    store.save_message(make_msg(id="401", body="goodbye moon"))
    results = store.search_messages("hello")
    assert len(results) == 1
    assert results[0].body == "hello world"


def test_search_no_results():
    store.save_message(make_msg(id="500", body="nothing here"))
    results = store.search_messages("xyz")
    assert results == []


def test_search_special_chars_dont_crash():
    store.save_message(make_msg(id="501", body="call +491234567890 tomorrow"))
    for query in ["+491234567890", "OR", "AND NOT", "hello AND"]:
        results = store.search_messages(query)
        assert isinstance(results, list)


# ── get_unread_messages ───────────────────────────────────────────────────────

def test_get_unread_returns_incoming():
    store.save_message(make_msg(id="in1", sender="+2", body="unread"))
    msgs = store.get_unread_messages(own_number="+1")
    assert len(msgs) == 1


def test_get_unread_excludes_own_sent():
    store.save_message(make_msg(id="out1", sender="+1", recipient="+2"))
    assert store.get_unread_messages(own_number="+1") == []


def test_get_unread_excludes_already_read():
    store.save_message(make_msg(id="in1", sender="+2"))
    store.mark_as_read(["in1"])
    assert store.get_unread_messages(own_number="+1") == []


# ── mark_as_read ──────────────────────────────────────────────────────────────

def test_mark_as_read():
    store.save_message(make_msg(id="in1", sender="+2"))
    store.mark_as_read(["in1"])
    assert store.get_unread_messages(own_number="+1") == []


def test_mark_as_read_empty_list_no_error():
    store.mark_as_read([])


# ── attachments ───────────────────────────────────────────────────────────────

def test_save_with_attachment():
    msg = Message(
        id="600",
        sender="+3",
        body="check this out",
        timestamp=datetime(2024, 6, 1),
        attachments=[Attachment(content_type="image/jpeg", filename="photo.jpg", size=12345)],
    )
    store.save_message(msg)
    results = store.get_conversation("+3")
    assert len(results[0].attachments) == 1
    assert results[0].attachments[0].filename == "photo.jpg"


# ── list_conversations ────────────────────────────────────────────────────────

def test_list_conversations_empty():
    assert store.list_conversations() == []


def test_list_conversations_direct():
    store.save_message(make_msg(id="900", sender="+5", body="hello"))
    store.save_message(make_msg(id="901", sender="+6", body="hi"))
    results = store.list_conversations()
    ids = [r["id"] for r in results]
    assert "+5" in ids
    assert "+6" in ids
    assert all(r["type"] == "direct" for r in results)


def test_list_conversations_outgoing_dm_appears():
    """Outgoing DMs must create a conversation entry."""
    store.save_message(make_msg(id="out1", sender="+me", recipient="+other"))
    results = store.list_conversations(own_number="+me")
    assert len(results) == 1
    assert results[0]["id"] == "+other"


def test_list_conversations_group():
    store.save_message(make_msg(id="910", sender="+7", group_id="grp-1"))
    store.save_message(make_msg(id="911", sender="+8", group_id="grp-1"))
    results = store.list_conversations()
    groups = [r for r in results if r["type"] == "group"]
    assert len(groups) == 1
    assert groups[0]["id"] == "grp-1"
    assert groups[0]["message_count"] == 2


def test_list_conversations_excludes_own_number():
    store.save_message(make_msg(id="920", sender="+me", body="I sent this"))
    store.save_message(make_msg(id="921", sender="+other", body="they replied"))
    results = store.list_conversations(own_number="+me")
    ids = [r["id"] for r in results]
    assert "+me" not in ids
    assert "+other" in ids


def test_list_conversations_ordered_by_recency():
    store.save_message(Message(id="930", sender="+a", body="old",
                               timestamp=datetime(2024, 1, 1)))
    store.save_message(Message(id="931", sender="+b", body="new",
                               timestamp=datetime(2024, 6, 1)))
    results = store.list_conversations()
    ids = [r["id"] for r in results]
    assert ids.index("+b") < ids.index("+a")


def test_list_conversations_deduped():
    store.save_message(make_msg(id="m1", sender="+2", ts=datetime(2024, 1, 1)))
    store.save_message(make_msg(id="m2", sender="+2", ts=datetime(2024, 1, 2)))
    results = store.list_conversations()
    assert len(results) == 1
    assert results[0]["message_count"] == 2


# ── get_stats ─────────────────────────────────────────────────────────────────

def test_get_stats_empty():
    stats = store.get_stats()
    assert stats["total_messages"] == 0
    assert stats["oldest"] is None


def test_get_stats_with_data():
    store.save_message(make_msg(id="700"))
    stats = store.get_stats()
    assert stats["total_messages"] == 1
    assert stats["oldest"] is not None
    assert stats["newest"] is not None


# ── init_db migration ─────────────────────────────────────────────────────────

def test_init_db_idempotent():
    """Calling init_db twice must not raise."""
    store.init_db()
    store.init_db()


def test_migration_adds_recipient_column(tmp_path, monkeypatch):
    """Simulate upgrading from pre-1.1 schema (no recipient column)."""
    import sqlite3
    db_path = tmp_path / "old.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    monkeypatch.setattr(store, "_initialized", False)
    if getattr(store._thread_local, "conn", None) is not None:
        store._thread_local.conn.close()
        store._thread_local.conn = None

    # Create old-style schema without recipient column
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE messages (
        id TEXT PRIMARY KEY, sender TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
        timestamp INTEGER NOT NULL, group_id TEXT, quote_id TEXT, is_read INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute("INSERT INTO messages VALUES ('m1','+1','hi',1700000000000,NULL,NULL,0)")
    conn.commit()
    conn.close()

    # init_db should run migration without error
    monkeypatch.setattr(store, "_initialized", False)
    if getattr(store._thread_local, "conn", None) is not None:
        store._thread_local.conn.close()
        store._thread_local.conn = None
    store.init_db()

    # Old data still readable
    msgs = store.get_conversation("+1")
    assert len(msgs) == 1


# ── update_message_body ───────────────────────────────────────────────────────

def test_update_message_body_changes_body():
    ts = datetime(2024, 6, 1, 12, 0, 0)
    ts_ms = int(ts.timestamp() * 1000)
    store.save_message(make_msg(id="edit1", sender="+1", body="original", ts=ts))
    store.update_message_body(ts_ms, "edited")
    msgs = store.get_conversation("+1")
    assert msgs[0].body == "edited"


def test_update_message_body_noop_on_unknown_ts():
    # Should not raise even if timestamp doesn't match any message
    store.update_message_body(9999999999999, "ghost")


def test_update_message_body_syncs_fts():
    ts = datetime(2024, 6, 1, 12, 0, 0)
    ts_ms = int(ts.timestamp() * 1000)
    store.save_message(make_msg(id="fts1", sender="+1", body="findme original", ts=ts))
    store.update_message_body(ts_ms, "findme edited")
    # FTS should find new text, not old
    results = store.search_messages("edited")
    assert len(results) == 1
    assert results[0].body == "findme edited"
    results_old = store.search_messages("original")
    assert results_old == []


# ── search_messages sender filter ────────────────────────────────────────────

def test_search_messages_sender_filter():
    store.save_message(make_msg(id="sf1", sender="+1", body="hello from one"))
    store.save_message(make_msg(id="sf2", sender="+2", body="hello from two"))
    results = store.search_messages("hello", sender="+1")
    assert len(results) == 1
    assert results[0].sender == "+1"


def test_search_messages_sender_filter_no_match():
    store.save_message(make_msg(id="sf3", sender="+1", body="hello"))
    results = store.search_messages("hello", sender="+9")
    assert results == []


def test_search_messages_sender_none_returns_all():
    store.save_message(make_msg(id="sf4", sender="+1", body="hello"))
    store.save_message(make_msg(id="sf5", sender="+2", body="hello"))
    results = store.search_messages("hello")
    assert len(results) == 2


# ── export_messages ───────────────────────────────────────────────────────────

def test_export_messages_json_all():
    import json
    store.save_message(make_msg(id="ex1", sender="+1", body="exported"))
    data = store.export_messages(fmt="json")
    parsed = json.loads(data)
    assert len(parsed) == 1
    assert parsed[0]["body"] == "exported"
    assert parsed[0]["sender"] == "+1"


def test_export_messages_csv_all():
    store.save_message(make_msg(id="ex2", sender="+1", body="csv export"))
    data = store.export_messages(fmt="csv")
    assert "csv export" in data
    assert "id,timestamp,sender" in data


def test_export_messages_filter_recipient():
    import json
    store.save_message(make_msg(id="ex3", sender="+1", body="msg from 1"))
    store.save_message(make_msg(id="ex4", sender="+2", body="msg from 2"))
    data = store.export_messages(fmt="json", recipient="+1")
    parsed = json.loads(data)
    assert len(parsed) == 1
    assert parsed[0]["sender"] == "+1"


def test_export_messages_filter_since():
    import json
    ts_early = datetime(2024, 1, 1, 0, 0, 0)
    ts_late = datetime(2024, 6, 1, 0, 0, 0)
    store.save_message(make_msg(id="ex5", sender="+1", body="early", ts=ts_early))
    store.save_message(make_msg(id="ex6", sender="+1", body="late", ts=ts_late))
    data = store.export_messages(fmt="json", since=datetime(2024, 3, 1))
    parsed = json.loads(data)
    assert len(parsed) == 1
    assert parsed[0]["body"] == "late"


def test_export_messages_empty_store():
    import json
    data = store.export_messages(fmt="json")
    assert json.loads(data) == []
