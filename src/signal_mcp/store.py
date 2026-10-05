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
            CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id);
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
            CREATE TABLE IF NOT EXISTS conversations (
                id   TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'direct'
            );
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient   TEXT,
                group_id    TEXT,
                message     TEXT NOT NULL,
                send_at     TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
                status      TEXT NOT NULL DEFAULT 'pending',
                error       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_scheduled_status ON scheduled_messages(status, send_at);
            -- signal-cli's sendPollVote requires a "vote-count" that must increase by 1
            -- each time this account votes on a given poll (identified by the poll
            -- message's author+timestamp, not a separate poll ID — signal-cli has none).
            CREATE TABLE IF NOT EXISTS poll_votes (
                poll_author    TEXT NOT NULL,
                poll_timestamp INTEGER NOT NULL,
                vote_count     INTEGER NOT NULL,
                PRIMARY KEY (poll_author, poll_timestamp)
            );
        """)
        # Migrate: add recipient column if upgrading from pre-1.1 schema
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN recipient TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        # Migrate: optional incoming-message data (mentions, previews, polls, ...) as JSON
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        if "extras" not in cols:
            conn.execute("ALTER TABLE messages ADD COLUMN extras TEXT")
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


def _extras_json(msg: Message) -> str | None:
    return json.dumps(msg.extras) if msg.extras else None


def save_message(msg: Message) -> bool:
    """Save a message. Returns True if new, False if already stored (duplicate id)."""
    init_db()
    with _db() as conn:
        # Outgoing messages are always read; incoming start as unread
        is_read = 1 if msg.recipient is not None else int(msg.is_read)
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages"
            " (id, sender, recipient, body, timestamp, group_id, quote_id, is_read, extras)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (msg.id, msg.sender, msg.recipient, msg.body,
             int(msg.timestamp.timestamp() * 1000),
             msg.group_id, msg.quote_id, is_read, _extras_json(msg)),
        )
        if cur.rowcount == 0:
            return False  # duplicate — AFTER INSERT trigger did NOT fire, FTS unchanged
        if msg.attachments:
            conn.executemany(
                "INSERT INTO attachments"
                " (message_id, content_type, filename, local_path, size) VALUES (?,?,?,?,?)",
                [
                    (msg.id, att.content_type, att.filename, att.local_path, att.size)
                    for att in msg.attachments
                ],
            )
    return True


def save_messages_batch(messages: list[Message]) -> tuple[int, int]:
    """Save multiple messages in a single transaction. Returns (imported, skipped) counts.

    Same INSERT OR IGNORE dedup semantics as save_message(), but avoids one
    commit per message — used by bulk imports (e.g. Signal Desktop import)
    where thousands of per-message commits would dominate runtime.
    """
    if not messages:
        return 0, 0
    init_db()
    imported = 0
    skipped = 0
    with _db() as conn:
        for msg in messages:
            is_read = 1 if msg.recipient is not None else int(msg.is_read)
            cur = conn.execute(
                "INSERT OR IGNORE INTO messages"
                " (id, sender, recipient, body, timestamp, group_id, quote_id, is_read, extras)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (msg.id, msg.sender, msg.recipient, msg.body,
                 int(msg.timestamp.timestamp() * 1000),
                 msg.group_id, msg.quote_id, is_read, _extras_json(msg)),
            )
            if cur.rowcount == 0:
                skipped += 1
                continue
            imported += 1
            if msg.attachments:
                conn.executemany(
                    "INSERT INTO attachments"
                    " (message_id, content_type, filename, local_path, size) VALUES (?,?,?,?,?)",
                    [
                        (msg.id, att.content_type, att.filename, att.local_path, att.size)
                        for att in msg.attachments
                    ],
                )
    return imported, skipped


def _conversation_where(recipient: str, own_number: str = "") -> tuple[str, list]:
    """Build the WHERE clause + params matching messages exchanged with *recipient*.

    A plain "sender = ? OR recipient = ?" (both bound to *recipient*) breaks for the
    self-conversation ("note to self"): outgoing messages always have sender=own_number,
    so binding own_number to that OR would match every outgoing message to anyone, not
    just notes to self. When recipient is our own number, require both sides to match.
    """
    if own_number and recipient == own_number:
        return "group_id IS NULL AND sender = ? AND recipient = ?", [recipient, recipient]
    # Written as three independent branches rather than
    # "group_id = ? OR (group_id IS NULL AND (sender = ? OR recipient = ?))" so SQLite
    # can use the sender/recipient/group_id indexes directly instead of first scanning
    # every direct message via "group_id IS NULL" -- same result, ~50x faster measured
    # against a 100k-message store.
    return (
        "group_id = ? OR (sender = ? AND group_id IS NULL) OR (recipient = ? AND group_id IS NULL)",
        [recipient, recipient, recipient],
    )


def get_conversation(
    recipient: str, limit: int = 50, offset: int = 0, since: datetime | None = None,
    own_number: str = "",
) -> list[Message]:
    """Get message history with a contact (by number) or group (by group_id)."""
    init_db()
    with _db() as conn:
        where, params = _conversation_where(recipient, own_number)
        since_clause = ""
        if since:
            since_clause = "AND timestamp >= ?"
            params.append(int(since.timestamp() * 1000))
        params.extend([limit, offset])
        rows = conn.execute(
            f"""SELECT * FROM messages
               WHERE ({where})
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
    query: str, limit: int = 50, offset: int = 0, sender: str | None = None,
    since: datetime | None = None, until: datetime | None = None,
) -> list[Message]:
    """Full-text search across all stored messages. Falls back to LIKE on FTS error.

    sender: if given, restrict results to messages from this phone number.
    since / until: restrict to messages with since <= timestamp < until.
    offset: skip this many results (for pagination).
    """
    if not query or not query.strip():
        return []
    init_db()
    with _db() as conn:
        filters: list[str] = []
        filter_args: list = []
        if sender:
            filters.append("sender = ?")
            filter_args.append(sender)
        if since:
            filters.append("timestamp >= ?")
            filter_args.append(int(since.timestamp() * 1000))
        if until:
            filters.append("timestamp < ?")
            filter_args.append(int(until.timestamp() * 1000))
        fts_filter_clause  = "".join(f" AND m.{f}" for f in filters)
        like_filter_clause = "".join(f" AND {f}" for f in filters)
        try:
            rows = conn.execute(
                f"""SELECT m.* FROM messages m
                   JOIN messages_fts f ON m.rowid = f.rowid
                   WHERE messages_fts MATCH ?
                   {fts_filter_clause}
                   ORDER BY m.timestamp DESC LIMIT ? OFFSET ?""",
                [_safe_fts_query(query)] + filter_args + [limit, offset],
            ).fetchall()
        except Exception:
            # Escape LIKE wildcards so literal % and _ in query don't over-match
            like_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = conn.execute(
                f"SELECT * FROM messages WHERE body LIKE ? ESCAPE '\\' {like_filter_clause}"
                " ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                [f"%{like_query}%"] + filter_args + [limit, offset],
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


def get_message_body(timestamp_ms: int, sender: str) -> str | None:
    """Body of the stored message sent by *sender* at *timestamp_ms*, or None."""
    init_db()
    with _db() as conn:
        row = conn.execute(
            "SELECT body FROM messages WHERE timestamp = ? AND sender = ? LIMIT 1",
            (timestamp_ms, sender),
        ).fetchone()
    return row["body"] if row else None


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
            # No sender to disambiguate: only proceed if exactly one message shares
            # this millisecond timestamp. Two candidates means we can't tell which
            # one the edit applies to — updating an arbitrary row could silently
            # overwrite an unrelated message, so skip rather than guess.
            rows = conn.execute(
                "SELECT rowid, id, sender, body FROM messages WHERE timestamp = ? LIMIT 2",
                (target_timestamp_ms,),
            ).fetchall()
            row = rows[0] if len(rows) == 1 else None
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


def mark_deleted(target_timestamp_ms: int | None, senders: list, flag: dict) -> bool:
    """Merge *flag* into the extras of the message (timestamp, any of *senders*).

    Used for incoming remote/admin deletes. The body is left intact on purpose
    (see SignalClient._apply_delete). Returns True if a message was flagged.
    """
    senders = [s for s in senders if s]
    if not target_timestamp_ms or not senders:
        return False
    init_db()
    with _db() as conn:
        ph = ",".join("?" * len(senders))
        row = conn.execute(
            f"SELECT id, extras FROM messages WHERE timestamp = ? AND sender IN ({ph})",
            [target_timestamp_ms, *senders],
        ).fetchone()
        if not row:
            return False
        extras = json.loads(row["extras"]) if row["extras"] else {}
        conn.execute(
            "UPDATE messages SET extras = ? WHERE id = ?",
            (json.dumps(extras | flag), row["id"]),
        )
    return True


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
    """Return all distinct conversations ordered by most recent message.

    Uses a single CTE + window function so the messages table is scanned once,
    replacing the previous two-query approach.
    """
    init_db()
    with _db() as conn:
        rows = conn.execute(
            """WITH numbered AS (
                   SELECT *,
                       COALESCE(group_id,
                           CASE WHEN sender = ? THEN recipient ELSE sender END
                       ) AS conv_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY COALESCE(group_id,
                               CASE WHEN sender = ? THEN recipient ELSE sender END
                           )
                           ORDER BY timestamp DESC
                       ) AS rn
                   FROM messages
                   WHERE COALESCE(group_id,
                       CASE WHEN sender = ? THEN recipient ELSE sender END
                   ) IS NOT NULL
               )
               SELECT
                   conv_id AS id,
                   CASE WHEN MAX(group_id) IS NOT NULL THEN 'group' ELSE 'direct' END AS type,
                   MAX(timestamp) AS last_message_at,
                   COUNT(*) AS message_count,
                   SUM(CASE WHEN is_read = 0 AND sender != ? THEN 1 ELSE 0 END) AS unread_count,
                   MAX(CASE WHEN rn = 1 THEN body END) AS last_message
               FROM numbered
               GROUP BY conv_id
               ORDER BY last_message_at DESC""",
            (own_number, own_number, own_number, own_number),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "type": r["type"],
                "last_message_at": datetime.fromtimestamp(r["last_message_at"] / 1000).isoformat(),
                "message_count": r["message_count"],
                "unread_count": r["unread_count"] or 0,
                "last_message": r["last_message"] or "",
            }
            for r in rows
        ]


def count_conversation(
    recipient: str, since: datetime | None = None, own_number: str = ""
) -> int:
    """Return total message count matching get_conversation's filter — used for has_more."""
    init_db()
    with _db() as conn:
        where, params = _conversation_where(recipient, own_number)
        since_clause = ""
        if since:
            since_clause = "AND timestamp >= ?"
            params.append(int(since.timestamp() * 1000))
        row = conn.execute(
            f"""SELECT COUNT(*) FROM messages
               WHERE ({where})
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


def delete_conversation_messages(recipient: str, own_number: str = "") -> int:
    """Delete all locally stored messages for one contact or group. Returns count deleted."""
    init_db()
    _where, params = _conversation_where(recipient, own_number)
    params = tuple(params)
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


def get_messages_for_export(
    recipient: str | None = None,
    since: datetime | None = None,
    own_number: str = "",
) -> list[Message]:
    """Return Message objects matching the given filters (used by the client for enriched export)."""
    init_db()
    with _db() as conn:
        params: list = []
        clauses: list[str] = []
        if recipient:
            where, where_params = _conversation_where(recipient, own_number)
            clauses.append(f"({where})")
            params.extend(where_params)
        if since:
            clauses.append("timestamp >= ?")
            params.append(int(since.timestamp() * 1000))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM messages {where} ORDER BY timestamp ASC",
            params,
        ).fetchall()
        return _rows_to_messages(conn, rows)


def export_messages(
    fmt: str = "json",
    recipient: str | None = None,
    since: datetime | None = None,
    enriched: list[dict] | None = None,
) -> str:
    """Serialise messages as JSON or CSV text.

    If *enriched* is provided (pre-resolved dicts from the client layer), those
    are serialised directly.  Otherwise messages are fetched without name resolution.
    """
    if enriched is not None:
        messages_data = enriched
    else:
        messages_data = [
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
            } | m.extras
            for m in get_messages_for_export(recipient, since)
        ]

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "timestamp", "sender", "sender_name", "recipient", "group_id", "group_name", "body", "quote_id", "is_read"])
        for m in messages_data:
            writer.writerow([
                m.get("id", ""),
                m.get("timestamp", ""),
                m.get("sender", ""),
                m.get("sender_name") or m.get("sender", ""),
                m.get("recipient") or "",
                m.get("group_id") or "",
                m.get("group_name") or "",
                m.get("body", ""),
                m.get("quote_id") or "",
                int(m.get("is_read", False)),
            ])
        return buf.getvalue()

    return json.dumps(messages_data, indent=2)


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
        # Single scan: total, unread (not sent by us), oldest, newest
        row = conn.execute(
            """SELECT
                   COUNT(*) AS total,
                   COUNT(CASE WHEN is_read = 0 AND sender != ? THEN 1 END) AS unread,
                   MIN(timestamp) AS oldest,
                   MAX(timestamp) AS newest
               FROM messages""",
            (own_number,),
        ).fetchone()
        db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    return {
        "total_messages": row["total"],
        "unread_messages": row["unread"],
        "db_size_bytes": db_size,
        "oldest": datetime.fromtimestamp(row["oldest"] / 1000).isoformat() if row["oldest"] else None,
        "newest": datetime.fromtimestamp(row["newest"] / 1000).isoformat() if row["newest"] else None,
    }


def _rows_to_messages(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[Message]:
    """Convert a batch of message rows to Message objects without N+1 queries.

    Attachment lookup is chunked to stay under SQLite's variable limit so that
    export_messages() with thousands of rows never raises OperationalError.
    """
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    att_rows: list[sqlite3.Row] = []
    for chunk in _chunked(ids, _SQLITE_MAX_VARS):
        ph = ",".join("?" * len(chunk))
        att_rows.extend(
            conn.execute(
                f"SELECT * FROM attachments WHERE message_id IN ({ph})", chunk
            ).fetchall()
        )
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
    has_extras = "extras" in cols
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
            extras=json.loads(r["extras"]) if has_extras and r["extras"] else {},
        )
        for r in rows
    ]


# ── conversation name store ───────────────────────────────────────────────────

def save_conversation(conv_id: str, name: str, conv_type: str = "direct") -> None:
    """Upsert a conversation's display name."""
    if not conv_id or not name:
        return
    init_db()
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO conversations (id, name, type) VALUES (?, ?, ?)",
            (conv_id, name, conv_type),
        )


def get_conversation_names(conv_type: str | None = None) -> dict[str, str]:
    """Return {conversation_id: display_name} for known conversations.

    conv_type: filter to 'direct' or 'group'; None returns both.
    """
    init_db()
    with _db() as conn:
        if conv_type:
            rows = conn.execute(
                "SELECT id, name FROM conversations WHERE type = ?", (conv_type,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT id, name FROM conversations").fetchall()
        return {r["id"]: r["name"] for r in rows}


# ── scheduled messages ───────────────────────────────────────────────────────

def add_scheduled_message(
    message: str,
    send_at: datetime,
    recipient: str | None = None,
    group_id: str | None = None,
) -> int:
    """Schedule a message to be sent at *send_at*. Returns the new row id."""
    if not recipient and not group_id:
        raise ValueError("Either recipient or group_id is required")
    init_db()
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO scheduled_messages (recipient, group_id, message, send_at) VALUES (?,?,?,?)",
            (recipient, group_id, message, send_at.isoformat()),
        )
        return cur.lastrowid


def get_pending_scheduled(now: datetime | None = None) -> list[dict]:
    """Return scheduled messages that are due (send_at <= now) and still pending."""
    init_db()
    cutoff = (now or datetime.now()).isoformat()
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_messages WHERE status = 'pending' AND send_at <= ? ORDER BY send_at ASC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_scheduled_messages(include_done: bool = False) -> list[dict]:
    """Return all scheduled messages, optionally including sent/cancelled ones."""
    init_db()
    with _db() as conn:
        if include_done:
            rows = conn.execute(
                "SELECT * FROM scheduled_messages ORDER BY send_at ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM scheduled_messages WHERE status = 'pending' ORDER BY send_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]


def mark_scheduled_sent(row_id: int) -> None:
    init_db()
    with _db() as conn:
        conn.execute(
            "UPDATE scheduled_messages SET status = 'sent' WHERE id = ?", (row_id,)
        )


def mark_scheduled_failed(row_id: int, error: str) -> None:
    init_db()
    with _db() as conn:
        conn.execute(
            "UPDATE scheduled_messages SET status = 'failed', error = ? WHERE id = ?",
            (error, row_id),
        )


def cancel_scheduled_message(row_id: int) -> bool:
    """Cancel a pending scheduled message. Returns True if it was pending, False otherwise."""
    init_db()
    with _db() as conn:
        cur = conn.execute(
            "UPDATE scheduled_messages SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (row_id,),
        )
        return cur.rowcount > 0


# ── poll votes ───────────────────────────────────────────────────────────────

def get_and_increment_vote_count(poll_author: str, poll_timestamp: int) -> int:
    """Return the next vote-count for a poll (1 on first vote, +1 each re-vote)."""
    init_db()
    with _db() as conn:
        row = conn.execute(
            "SELECT vote_count FROM poll_votes WHERE poll_author = ? AND poll_timestamp = ?",
            (poll_author, poll_timestamp),
        ).fetchone()
        next_count = (row["vote_count"] + 1) if row else 1
        conn.execute(
            "INSERT INTO poll_votes (poll_author, poll_timestamp, vote_count) VALUES (?, ?, ?)"
            " ON CONFLICT (poll_author, poll_timestamp) DO UPDATE SET vote_count = excluded.vote_count",
            (poll_author, poll_timestamp, next_count),
        )
        return next_count


# ── meta key-value store ───────────────────────────────────────────────────────

def get_meta(key: str) -> str | None:
    """Return a stored metadata value, or None if not set."""
    init_db()
    with _db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    """Persist a metadata key-value pair (upsert)."""
    init_db()
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )

