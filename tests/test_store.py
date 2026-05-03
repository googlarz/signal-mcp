"""Tests for local SQLite message store."""

import pytest
from datetime import datetime
from pathlib import Path

from signal_mcp.models import Message, Attachment
from signal_mcp import store


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Redirect DB to a temp file for each test."""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test_messages.db")


def make_msg(id="1", sender="+1", body="hello", group_id=None):
    return Message(
        id=id,
        sender=sender,
        body=body,
        timestamp=datetime(2024, 6, 1, 12, 0, 0),
        group_id=group_id,
    )


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


def test_get_conversation_by_group():
    msg = make_msg(id="300", sender="+2", group_id="group-abc")
    store.save_message(msg)
    results = store.get_conversation("group-abc")
    assert len(results) == 1
    assert results[0].group_id == "group-abc"


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
    # These would crash raw FTS5 but should not raise
    for query in ["+491234567890", "OR", "AND NOT", "hello AND"]:
        results = store.search_messages(query)
        assert isinstance(results, list)


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
    assert len(results) == 1
    assert len(results[0].attachments) == 1
    assert results[0].attachments[0].filename == "photo.jpg"


def test_get_stats_empty():
    stats = store.get_stats()
    assert stats["total_messages"] == 0
    assert stats["oldest"] is None


def test_get_stats_with_data():
    store.save_message(make_msg(id="700"))
    stats = store.get_stats()
    assert stats["total_messages"] == 1
    assert stats["oldest"] is not None


def test_conversation_limit():
    for i in range(10):
        store.save_message(make_msg(id=str(800 + i), sender="+4", body=f"msg {i}"))
    results = store.get_conversation("+4", limit=5)
    assert len(results) == 5


def test_list_conversations_empty():
    results = store.list_conversations()
    assert results == []


def test_list_conversations_direct():
    store.save_message(make_msg(id="900", sender="+5", body="hello"))
    store.save_message(make_msg(id="901", sender="+6", body="hi"))
    results = store.list_conversations()
    ids = [r["id"] for r in results]
    assert "+5" in ids
    assert "+6" in ids
    assert all(r["type"] == "direct" for r in results)


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
