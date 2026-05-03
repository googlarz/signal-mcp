"""Local SQLite message store — persists received messages for history and search."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .models import Attachment, Message

DB_PATH = Path.home() / ".local" / "share" / "signal-mcp" / "messages.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id          TEXT PRIMARY KEY,
                sender      TEXT NOT NULL,
                body        TEXT NOT NULL DEFAULT '',
                timestamp   INTEGER NOT NULL,
                group_id    TEXT,
                quote_id    TEXT,
                is_read     INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS attachments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id   TEXT NOT NULL REFERENCES messages(id),
                content_type TEXT NOT NULL,
                filename     TEXT NOT NULL,
                local_path   TEXT,
                size         INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_messages_sender    ON messages(sender);
            CREATE INDEX IF NOT EXISTS idx_messages_group     ON messages(group_id);
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                id UNINDEXED,
                body,
                sender,
                content=messages,
                content_rowid=rowid
            );
            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, id, body, sender)
                VALUES (new.rowid, new.id, new.body, new.sender);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, id, body, sender)
                VALUES ('delete', old.rowid, old.id, old.body, old.sender);
            END;
        """)


def save_message(msg: Message) -> bool:
    """Save a message. Returns True if new, False if already stored."""
    init_db()
    with _db() as conn:
        existing = conn.execute("SELECT id FROM messages WHERE id = ?", (msg.id,)).fetchone()
        if existing:
            return False
        conn.execute(
            "INSERT INTO messages (id, sender, body, timestamp, group_id, quote_id) VALUES (?,?,?,?,?,?)",
            (msg.id, msg.sender, msg.body, int(msg.timestamp.timestamp() * 1000),
             msg.group_id, msg.quote_id),
        )
        for att in msg.attachments:
            conn.execute(
                "INSERT INTO attachments (message_id, content_type, filename, local_path, size) VALUES (?,?,?,?,?)",
                (msg.id, att.content_type, att.filename, att.local_path, att.size),
            )
    return True


def get_conversation(recipient: str, limit: int = 50) -> list[Message]:
    """Get message history with a contact (by number) or group (by group_id)."""
    init_db()
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM messages
               WHERE sender = ? OR group_id = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (recipient, recipient, limit),
        ).fetchall()
        return [_row_to_message(conn, r) for r in reversed(rows)]


def _safe_fts_query(query: str) -> str:
    """Escape FTS5 special characters so plain-text searches never error."""
    # Wrap each token in double-quotes so FTS5 treats them as literals
    tokens = query.split()
    return " ".join(f'"{t.replace(chr(34), "")}"' for t in tokens if t)


def search_messages(query: str, limit: int = 50) -> list[Message]:
    """Full-text search across all stored messages. Falls back to LIKE on FTS error."""
    init_db()
    with _db() as conn:
        try:
            rows = conn.execute(
                """SELECT m.* FROM messages m
                   JOIN messages_fts f ON m.id = f.id
                   WHERE messages_fts MATCH ?
                   ORDER BY m.timestamp DESC LIMIT ?""",
                (_safe_fts_query(query), limit),
            ).fetchall()
        except Exception:
            # Fallback: case-insensitive LIKE search
            rows = conn.execute(
                "SELECT * FROM messages WHERE body LIKE ? ORDER BY timestamp DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
        return [_row_to_message(conn, r) for r in rows]


def list_conversations(own_number: str = "") -> list[dict]:
    """Return all distinct conversations ordered by most recent message."""
    init_db()
    with _db() as conn:
        rows = conn.execute(
            """SELECT
                COALESCE(group_id, sender) AS id,
                CASE WHEN group_id IS NOT NULL THEN 'group' ELSE 'direct' END AS type,
                MAX(timestamp) AS last_message_at,
                COUNT(*) AS message_count
               FROM messages
               WHERE NOT (group_id IS NULL AND sender = ?)
               GROUP BY COALESCE(group_id, sender)
               ORDER BY last_message_at DESC""",
            (own_number,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "type": r["type"],
                "last_message_at": datetime.fromtimestamp(r["last_message_at"] / 1000).isoformat(),
                "message_count": r["message_count"],
            }
            for r in rows
        ]


def get_stats() -> dict:
    init_db()
    with _db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        oldest = conn.execute("SELECT MIN(timestamp) FROM messages").fetchone()[0]
        newest = conn.execute("SELECT MAX(timestamp) FROM messages").fetchone()[0]
    return {
        "total_messages": total,
        "oldest": datetime.fromtimestamp(oldest / 1000).isoformat() if oldest else None,
        "newest": datetime.fromtimestamp(newest / 1000).isoformat() if newest else None,
    }


def _row_to_message(conn: sqlite3.Connection, row: sqlite3.Row) -> Message:
    att_rows = conn.execute(
        "SELECT * FROM attachments WHERE message_id = ?", (row["id"],)
    ).fetchall()
    return Message(
        id=row["id"],
        sender=row["sender"],
        body=row["body"],
        timestamp=datetime.fromtimestamp(row["timestamp"] / 1000),
        group_id=row["group_id"],
        quote_id=row["quote_id"],
        is_read=bool(row["is_read"]),
        attachments=[
            Attachment(
                content_type=a["content_type"],
                filename=a["filename"],
                local_path=a["local_path"],
                size=a["size"],
            )
            for a in att_rows
        ],
    )
