"""Local SQLite message store — persists received messages for history and search."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .models import Attachment, Message

DB_PATH = Path.home() / ".local" / "share" / "signal-mcp" / "messages.db"

_initialized = False


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
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
    global _initialized
    if _initialized:
        return
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id          TEXT PRIMARY KEY,
                sender      TEXT NOT NULL,
                recipient   TEXT,
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
            CREATE INDEX IF NOT EXISTS idx_messages_sender_ts    ON messages(sender, timestamp);
            CREATE INDEX IF NOT EXISTS idx_messages_group_ts     ON messages(group_id, timestamp);
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
        # Migrate: add recipient column if upgrading from pre-1.1 schema
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN recipient TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        # Indexes on recipient must be created after migration (column may have just been added)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_recipient_ts ON messages(recipient, timestamp)"
        )
    _initialized = True


def save_message(msg: Message) -> bool:
    """Save a message. Returns True if new, False if already stored."""
    init_db()
    with _db() as conn:
        existing = conn.execute("SELECT id FROM messages WHERE id = ?", (msg.id,)).fetchone()
        if existing:
            return False
        # Outgoing messages are always read; incoming start as unread
        is_read = 1 if msg.recipient is not None else int(msg.is_read)
        conn.execute(
            "INSERT INTO messages (id, sender, recipient, body, timestamp, group_id, quote_id, is_read)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (msg.id, msg.sender, msg.recipient, msg.body,
             int(msg.timestamp.timestamp() * 1000),
             msg.group_id, msg.quote_id, is_read),
        )
        for att in msg.attachments:
            conn.execute(
                "INSERT INTO attachments (message_id, content_type, filename, local_path, size) VALUES (?,?,?,?,?)",
                (msg.id, att.content_type, att.filename, att.local_path, att.size),
            )
    return True


def get_conversation(
    recipient: str, limit: int = 50, offset: int = 0, since: datetime | None = None
) -> list[Message]:
    """Get message history with a contact (by number) or group (by group_id)."""
    init_db()
    with _db() as conn:
        params: list = [recipient, recipient, recipient]
        since_clause = ""
        if since:
            since_clause = "AND timestamp >= ?"
            params.append(int(since.timestamp() * 1000))
        params.extend([limit, offset])
        rows = conn.execute(
            f"""SELECT * FROM messages
               WHERE (group_id = ?
                  OR (group_id IS NULL AND (sender = ? OR recipient = ?)))
               {since_clause}
               ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
        return [_row_to_message(conn, r) for r in reversed(rows)]


def _safe_fts_query(query: str) -> str:
    """Escape FTS5 special characters so plain-text searches never error."""
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
            rows = conn.execute(
                "SELECT * FROM messages WHERE body LIKE ? ORDER BY timestamp DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
        return [_row_to_message(conn, r) for r in rows]


def get_unread_messages(own_number: str = "", limit: int = 50) -> list[Message]:
    """Return stored messages not yet marked read (received, not sent)."""
    init_db()
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM messages
               WHERE is_read = 0 AND sender != ?
               ORDER BY timestamp DESC LIMIT ?""",
            (own_number, limit),
        ).fetchall()
        return [_row_to_message(conn, r) for r in reversed(rows)]


def update_message_body(target_timestamp_ms: int, new_body: str) -> None:
    """Update a stored message's body after an edit. Also syncs FTS index."""
    init_db()
    with _db() as conn:
        row = conn.execute(
            "SELECT rowid, id, sender, body FROM messages WHERE timestamp = ?",
            (target_timestamp_ms,),
        ).fetchone()
        if not row:
            return
        conn.execute("UPDATE messages SET body = ? WHERE timestamp = ?", (new_body, target_timestamp_ms))
        # Sync FTS: remove stale entry, insert updated
        conn.execute(
            "INSERT INTO messages_fts(messages_fts, rowid, id, body, sender) VALUES ('delete', ?, ?, ?, ?)",
            (row["rowid"], row["id"], row["body"], row["sender"]),
        )
        conn.execute(
            "INSERT INTO messages_fts(rowid, id, body, sender) VALUES (?, ?, ?, ?)",
            (row["rowid"], row["id"], new_body, row["sender"]),
        )


def mark_as_read(message_ids: list[str]) -> None:
    """Mark specific messages as read in the store."""
    if not message_ids:
        return
    init_db()
    with _db() as conn:
        placeholders = ",".join("?" * len(message_ids))
        conn.execute(
            f"UPDATE messages SET is_read = 1 WHERE id IN ({placeholders})",
            message_ids,
        )


def list_conversations(own_number: str = "") -> list[dict]:
    """Return all distinct conversations ordered by most recent message."""
    init_db()
    with _db() as conn:
        rows = conn.execute(
            """SELECT
                COALESCE(group_id,
                    CASE WHEN sender = ? THEN recipient ELSE sender END
                ) AS id,
                CASE WHEN group_id IS NOT NULL THEN 'group' ELSE 'direct' END AS type,
                MAX(timestamp) AS last_message_at,
                COUNT(*) AS message_count
               FROM messages
               WHERE COALESCE(group_id,
                    CASE WHEN sender = ? THEN recipient ELSE sender END
               ) IS NOT NULL
               GROUP BY COALESCE(group_id,
                    CASE WHEN sender = ? THEN recipient ELSE sender END
               )
               ORDER BY last_message_at DESC""",
            (own_number, own_number, own_number),
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
    cols = row.keys()
    return Message(
        id=row["id"],
        sender=row["sender"],
        recipient=row["recipient"] if "recipient" in cols else None,
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
