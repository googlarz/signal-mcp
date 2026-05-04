"""Local SQLite message store — persists received messages for history and search."""

import csv
import io
import json
import sqlite3
import stat
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .models import Attachment, Message

DB_PATH = Path.home() / ".local" / "share" / "signal-mcp" / "messages.db"

_initialized_paths: set[str] = set()  # paths already schema-initialized
_thread_local = threading.local()  # per-thread connection cache


def _connect() -> sqlite3.Connection:
    """Return the cached per-thread connection, creating it if needed."""
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        return conn
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-16000")   # 16 MB page cache
    conn.execute("PRAGMA temp_store=memory")
    conn.execute("PRAGMA mmap_size=134217728")  # 128 MB memory-mapped I/O
    _thread_local.conn = conn
    # Restrict permissions to owner-only on first creation
    try:
        DB_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return conn


@contextmanager
def _db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    # Connection is kept open for reuse — not closed here


def init_db() -> None:
    global _initialized_paths
    db_key = str(DB_PATH)
    if db_key in _initialized_paths:
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_unread "
            "ON messages(is_read, sender, timestamp) WHERE is_read = 0"
        )
    _initialized_paths.add(db_key)


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
        return _rows_to_messages(conn, list(reversed(rows)))


def _safe_fts_query(query: str) -> str:
    """Escape FTS5 special characters so plain-text searches never error."""
    tokens = query.split()
    return " ".join(f'"{t.replace(chr(34), "")}"' for t in tokens if t)


def search_messages(
    query: str, limit: int = 50, offset: int = 0, sender: str | None = None
) -> list[Message]:
    """Full-text search across all stored messages. Falls back to LIKE on FTS error.

    sender: if given, restrict results to messages from this phone number.
    offset: skip this many results (for pagination).
    """
    if not query or not query.strip():
        return []
    init_db()
    with _db() as conn:
        fts_sender_clause  = "AND m.sender = ?" if sender else ""
        like_sender_clause = "AND sender = ?"   if sender else ""
        sender_args = [sender] if sender else []
        try:
            rows = conn.execute(
                f"""SELECT m.* FROM messages m
                   JOIN messages_fts f ON m.rowid = f.rowid
                   WHERE messages_fts MATCH ?
                   {fts_sender_clause}
                   ORDER BY m.timestamp DESC LIMIT ? OFFSET ?""",
                [_safe_fts_query(query)] + sender_args + [limit, offset],
            ).fetchall()
        except Exception:
            # Escape LIKE wildcards so literal % and _ in query don't over-match
            like_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = conn.execute(
                f"SELECT * FROM messages WHERE body LIKE ? ESCAPE '\\' {like_sender_clause}"
                " ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                [f"%{like_query}%"] + sender_args + [limit, offset],
            ).fetchall()
        return _rows_to_messages(conn, rows)


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
        return _rows_to_messages(conn, list(reversed(rows)))


def update_message_body(target_timestamp_ms: int, new_body: str, sender: str | None = None) -> None:
    """Update a stored message's body after an edit. Also syncs FTS index.

    sender: if provided, restricts the update to messages from this sender, preventing
    accidental collision when two messages have the same millisecond timestamp.
    """
    init_db()
    with _db() as conn:
        if sender:
            row = conn.execute(
                "SELECT rowid, id, sender, body FROM messages WHERE timestamp = ? AND sender = ?",
                (target_timestamp_ms, sender),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT rowid, id, sender, body FROM messages WHERE timestamp = ? LIMIT 1",
                (target_timestamp_ms,),
            ).fetchone()
        if not row:
            return
        conn.execute("UPDATE messages SET body = ? WHERE id = ?", (new_body, row["id"]))
        # Sync FTS: remove stale entry, insert updated
        conn.execute(
            "INSERT INTO messages_fts(messages_fts, rowid, id, body, sender) VALUES ('delete', ?, ?, ?, ?)",
            (row["rowid"], row["id"], row["body"], row["sender"]),
        )
        conn.execute(
            "INSERT INTO messages_fts(rowid, id, body, sender) VALUES (?, ?, ?, ?)",
            (row["rowid"], row["id"], new_body, row["sender"]),
        )


_SQLITE_MAX_VARS = 500  # well under SQLite's 999-variable limit


def _chunked(lst: list, size: int):
    """Yield successive chunks of `size` from `lst`."""
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def mark_as_read(message_ids: list[str]) -> None:
    """Mark specific messages as read in the store."""
    if not message_ids:
        return
    init_db()
    with _db() as conn:
        for chunk in _chunked(message_ids, _SQLITE_MAX_VARS):
            placeholders = ",".join("?" * len(chunk))
            conn.execute(
                f"UPDATE messages SET is_read = 1 WHERE id IN ({placeholders})",
                chunk,
            )


def mark_as_unread(message_ids: list[str]) -> None:
    """Mark specific messages as unread in the store."""
    if not message_ids:
        return
    init_db()
    with _db() as conn:
        for chunk in _chunked(message_ids, _SQLITE_MAX_VARS):
            placeholders = ",".join("?" * len(chunk))
            conn.execute(
                f"UPDATE messages SET is_read = 0 WHERE id IN ({placeholders})",
                chunk,
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
                COUNT(*) AS message_count,
                SUM(CASE WHEN is_read = 0 AND sender != ? THEN 1 ELSE 0 END) AS unread_count
               FROM messages
               WHERE COALESCE(group_id,
                    CASE WHEN sender = ? THEN recipient ELSE sender END
               ) IS NOT NULL
               GROUP BY COALESCE(group_id,
                    CASE WHEN sender = ? THEN recipient ELSE sender END
               )
               ORDER BY last_message_at DESC""",
            (own_number, own_number, own_number, own_number),
        ).fetchall()
        # Fetch last message body for each conversation in one query
        conv_ids = [r["id"] for r in rows]
        last_body: dict[str, str] = {}
        if conv_ids:
            ph = ",".join("?" * len(conv_ids))
            snippet_rows = conn.execute(
                f"""SELECT
                        COALESCE(group_id,
                            CASE WHEN sender = ? THEN recipient ELSE sender END
                        ) AS conv_id,
                        body
                    FROM messages
                    WHERE COALESCE(group_id,
                            CASE WHEN sender = ? THEN recipient ELSE sender END
                    ) IN ({ph})
                    AND timestamp IN (
                        SELECT MAX(timestamp) FROM messages
                        WHERE COALESCE(group_id,
                            CASE WHEN sender = ? THEN recipient ELSE sender END
                        ) IN ({ph})
                        GROUP BY COALESCE(group_id,
                            CASE WHEN sender = ? THEN recipient ELSE sender END
                        )
                    )""",
                [own_number, own_number] + conv_ids + [own_number] + conv_ids + [own_number],
            ).fetchall()
            for s in snippet_rows:
                last_body[s["conv_id"]] = s["body"]
        return [
            {
                "id": r["id"],
                "type": r["type"],
                "last_message_at": datetime.fromtimestamp(r["last_message_at"] / 1000).isoformat(),
                "message_count": r["message_count"],
                "unread_count": r["unread_count"] or 0,
                "last_message": last_body.get(r["id"], ""),
            }
            for r in rows
        ]


def count_conversation(
    recipient: str, since: datetime | None = None
) -> int:
    """Return total message count matching get_conversation's filter — used for has_more."""
    init_db()
    with _db() as conn:
        params: list = [recipient, recipient, recipient]
        since_clause = ""
        if since:
            since_clause = "AND timestamp >= ?"
            params.append(int(since.timestamp() * 1000))
        row = conn.execute(
            f"""SELECT COUNT(*) FROM messages
               WHERE (group_id = ?
                  OR (group_id IS NULL AND (sender = ? OR recipient = ?)))
               {since_clause}""",
            params,
        ).fetchone()
        return row[0] if row else 0


def clear_store() -> int:
    """Delete ALL locally stored messages and attachments. Returns count deleted."""
    init_db()
    with _db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.execute("DELETE FROM attachments")
        conn.execute("DELETE FROM messages")
        # Rebuild FTS index (content table is now empty)
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    return count


def delete_conversation_messages(recipient: str) -> int:
    """Delete all locally stored messages for one contact or group. Returns count deleted."""
    init_db()
    _where = "group_id = ? OR (group_id IS NULL AND (sender = ? OR recipient = ?))"
    params = (recipient, recipient, recipient)
    with _db() as conn:
        count = conn.execute(f"SELECT COUNT(*) FROM messages WHERE {_where}", params).fetchone()[0]
        if count == 0:
            return 0
        conn.execute(
            f"DELETE FROM attachments WHERE message_id IN (SELECT id FROM messages WHERE {_where})",
            params,
        )
        conn.execute(f"DELETE FROM messages WHERE {_where}", params)
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    return count


def export_messages(
    fmt: str = "json",
    recipient: str | None = None,
    since: datetime | None = None,
) -> str:
    """Export messages as JSON or CSV text.

    recipient: if given, export only that conversation (number or group_id).
    since: if given, only messages at or after this datetime.
    fmt: "json" or "csv".
    """
    init_db()
    with _db() as conn:
        params: list = []
        clauses: list[str] = []
        if recipient:
            clauses.append(
                "(group_id = ? OR (group_id IS NULL AND (sender = ? OR recipient = ?)))"
            )
            params.extend([recipient, recipient, recipient])
        if since:
            clauses.append("timestamp >= ?")
            params.append(int(since.timestamp() * 1000))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM messages {where} ORDER BY timestamp ASC",
            params,
        ).fetchall()
        messages = _rows_to_messages(conn, rows)

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "timestamp", "sender", "recipient", "group_id", "body", "quote_id", "is_read"])
        for m in messages:
            writer.writerow([
                m.id,
                m.timestamp.isoformat(),
                m.sender,
                m.recipient or "",
                m.group_id or "",
                m.body,
                m.quote_id or "",
                int(m.is_read),
            ])
        return buf.getvalue()

    # JSON (default)
    return json.dumps(
        [
            {
                "id": m.id,
                "timestamp": m.timestamp.isoformat(),
                "sender": m.sender,
                "recipient": m.recipient,
                "group_id": m.group_id,
                "body": m.body,
                "quote_id": m.quote_id,
                "is_read": m.is_read,
                "attachments": [
                    {
                        "content_type": a.content_type,
                        "filename": a.filename,
                        "local_path": a.local_path,
                        "size": a.size,
                    }
                    for a in m.attachments
                ],
            }
            for m in messages
        ],
        indent=2,
    )


def prune_old_messages(days: int = 180) -> int:
    """Delete messages older than *days* days. Returns count deleted.

    FTS and attachments are cleaned up too.  Default is 180 days (6 months).
    Uses subqueries instead of IN (?, ?, ...) to avoid SQLite's variable limit.
    """
    if days <= 0:
        raise ValueError("days must be a positive integer")
    init_db()
    cutoff_ms = int((datetime.now().timestamp() - days * 86400) * 1000)
    with _db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE timestamp < ?", (cutoff_ms,)
        ).fetchone()[0]
        if count == 0:
            return 0
        conn.execute(
            "DELETE FROM attachments WHERE message_id IN (SELECT id FROM messages WHERE timestamp < ?)",
            (cutoff_ms,),
        )
        conn.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff_ms,))
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    return count


def get_stats(own_number: str = "") -> dict:
    init_db()
    with _db() as conn:
        total   = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        # Mirror get_unread_messages: unread = not read AND not sent by us
        unread  = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE is_read = 0 AND sender != ?", (own_number,)
        ).fetchone()[0]
        oldest  = conn.execute("SELECT MIN(timestamp) FROM messages").fetchone()[0]
        newest  = conn.execute("SELECT MAX(timestamp) FROM messages").fetchone()[0]
        db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    return {
        "total_messages": total,
        "unread_messages": unread,
        "db_size_bytes": db_size,
        "oldest": datetime.fromtimestamp(oldest / 1000).isoformat() if oldest else None,
        "newest": datetime.fromtimestamp(newest / 1000).isoformat() if newest else None,
    }


def _rows_to_messages(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[Message]:
    """Convert a batch of message rows to Message objects in 2 queries (not N+1)."""
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    ph = ",".join("?" * len(ids))
    att_rows = conn.execute(
        f"SELECT * FROM attachments WHERE message_id IN ({ph})", ids
    ).fetchall()
    # Group attachments by message_id
    att_map: dict[str, list[Attachment]] = {}
    for a in att_rows:
        att_map.setdefault(a["message_id"], []).append(
            Attachment(
                content_type=a["content_type"],
                filename=a["filename"],
                local_path=a["local_path"],
                size=a["size"],
            )
        )
    cols = rows[0].keys()
    has_recipient = "recipient" in cols
    return [
        Message(
            id=r["id"],
            sender=r["sender"],
            recipient=r["recipient"] if has_recipient else None,
            body=r["body"],
            timestamp=datetime.fromtimestamp(r["timestamp"] / 1000),
            group_id=r["group_id"],
            quote_id=r["quote_id"],
            is_read=bool(r["is_read"]),
            attachments=att_map.get(r["id"], []),
        )
        for r in rows
    ]


def _row_to_message(conn: sqlite3.Connection, row: sqlite3.Row) -> Message:
    """Single-row convenience wrapper — uses batch loader internally."""
    return _rows_to_messages(conn, [row])[0]
