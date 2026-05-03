"""Tests for Claude-powered translation."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from signal_mcp.models import Message
from signal_mcp.translation import translate_messages, translate_text


def make_msg(id="1", sender="+1", body="Hallo Welt", group_id=None):
    return Message(
        id=id,
        sender=sender,
        body=body,
        timestamp=datetime(2024, 6, 1, 12, 0, 0),
        group_id=group_id,
    )


def _mock_response(text: str):
    content = MagicMock()
    content.text = text
    resp = MagicMock()
    resp.content = [content]
    return resp


@patch("signal_mcp.translation._get_client")
def test_translate_messages_returns_results(mock_get_client):
    client = MagicMock()
    mock_get_client.return_value = client
    client.messages.create.return_value = _mock_response(
        "[2024-06-01 12:00] +1: Hello World"
    )

    results = translate_messages([make_msg()], target_language="English")

    assert len(results) == 1
    assert results[0]["translated"] == "Hello World"
    assert results[0]["original"] == "Hallo Welt"
    assert results[0]["sender"] == "+1"
    assert "timestamp" in results[0]


@patch("signal_mcp.translation._get_client")
def test_translate_messages_empty(mock_get_client):
    assert translate_messages([]) == []
    mock_get_client.assert_not_called()


@patch("signal_mcp.translation._get_client")
def test_translate_text(mock_get_client):
    client = MagicMock()
    mock_get_client.return_value = client
    client.messages.create.return_value = _mock_response("Hello World")

    result = translate_text("Hallo Welt")
    assert result == "Hello World"


@patch("signal_mcp.translation._get_client")
def test_translate_messages_preserves_group_id(mock_get_client):
    client = MagicMock()
    mock_get_client.return_value = client
    client.messages.create.return_value = _mock_response(
        "[2024-06-01 12:00] +1: Hello"
    )

    msg = make_msg(body="Hallo", group_id="group-123")
    results = translate_messages([msg])
    assert results[0]["group_id"] == "group-123"


def test_translate_missing_api_key(monkeypatch):
    import signal_mcp.translation as t
    monkeypatch.setattr(t, "_client", None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(t.TranslationError, match="ANTHROPIC_API_KEY"):
        t._get_client()


@patch("signal_mcp.translation._get_client")
def test_translate_messages_unchanged_if_already_english(mock_get_client):
    client = MagicMock()
    mock_get_client.return_value = client
    client.messages.create.return_value = _mock_response(
        "[2024-06-01 12:00] +1: Hello"
    )

    msg = make_msg(body="Hello")
    results = translate_messages([msg])
    assert results[0]["original"] == "Hello"
    assert results[0]["translated"] == "Hello"
