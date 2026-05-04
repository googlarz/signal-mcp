"""Coverage tests for signal_mcp/store.py — uncovered lines 38-39, 49-51."""

import sqlite3
import stat
from unittest.mock import MagicMock, patch

import pytest

import signal_mcp.store as _store_mod
from signal_mcp.store import _connect, _db, init_db


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(_store_mod, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(_store_mod, "_initialized_paths", set())
    if getattr(_store_mod._thread_local, "conn", None) is not None:
        _store_mod._thread_local.conn.close()
        _store_mod._thread_local.conn = None


# ── _connect chmod OSError fallback (lines 38-39) ────────────────────────────

def test_connect_chmod_oserror_swallowed(monkeypatch, tmp_path):
    """OSError from chmod is silently swallowed; connection still returned."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(_store_mod, "DB_PATH", db_path)

    # Close any cached connection first
    if getattr(_store_mod._thread_local, "conn", None) is not None:
        _store_mod._thread_local.conn.close()
        _store_mod._thread_local.conn = None

    with patch.object(db_path.__class__, "chmod", side_effect=OSError("permission denied")):
        conn = _connect()

    assert conn is not None
    # Should still be a valid connection
    conn.execute("SELECT 1")


# ── _db() rollback on exception (lines 49-51) ────────────────────────────────

def test_db_context_manager_rollback_on_exception(monkeypatch, tmp_path):
    """Exception inside _db() context triggers rollback and re-raises."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(_store_mod, "DB_PATH", db_path)

    init_db()  # create schema

    # Use a mock connection that wraps the real one to track rollback calls
    rollback_called = []
    real_conn = _store_mod._connect()

    mock_conn = MagicMock(wraps=real_conn)
    mock_conn.rollback = MagicMock(side_effect=lambda: (rollback_called.append(True), real_conn.rollback())[1])

    with patch.object(_store_mod, "_connect", return_value=mock_conn):
        with pytest.raises(ValueError, match="intentional"):
            with _db():
                raise ValueError("intentional error")

    assert rollback_called, "rollback() should have been called on exception"
