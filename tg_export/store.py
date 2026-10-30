"""SQLite archive of one chat.

Resume position is MAX(id) of rows that are already committed, so it can never
run ahead of the data (the original script saved its offset before flushing
the batch and could skip up to 99 messages after a crash).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

COLUMNS = (
    "id", "date", "edit_date", "sender_id", "sender_name", "sender_username",
    "text", "reply_to", "fwd_from", "views", "forwards", "post_author",
    "grouped_id", "topic_id", "service", "media_type", "media_name", "media_size",
    "media_path", "media_error", "raw",
)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    edit_date TEXT,
    sender_id INTEGER,
    sender_name TEXT,
    sender_username TEXT,
    text TEXT,
    reply_to INTEGER,
    fwd_from TEXT,
    views INTEGER,
    forwards INTEGER,
    post_author TEXT,
    grouped_id INTEGER,
    topic_id INTEGER,
    service TEXT,
    media_type TEXT,
    media_name TEXT,
    media_size INTEGER,
    media_path TEXT,
    media_error TEXT,
    raw TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS topics (id INTEGER PRIMARY KEY, title TEXT);
"""

_COLUMN_TYPES = {"date": "TEXT", "edit_date": "TEXT", "sender_name": "TEXT", "sender_username": "TEXT",
                 "text": "TEXT", "fwd_from": "TEXT", "post_author": "TEXT", "service": "TEXT",
                 "media_type": "TEXT", "media_name": "TEXT", "media_path": "TEXT", "media_error": "TEXT",
                 "raw": "TEXT"}

# Fields refreshed when a message is seen again (edits, view counters).
# media_path / media_error are never overwritten by a re-scan.
_UPDATABLE = [c for c in COLUMNS if c not in ("id", "media_path", "media_error")]

DOWNLOADABLE = ("MessageMediaPhoto", "MessageMediaDocument")


class Archive:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        # archives made by older versions lack newer columns
        have = {r[1] for r in self.db.execute("PRAGMA table_info(messages)")}
        for col in COLUMNS:
            if col not in have:
                self.db.execute(f"ALTER TABLE messages ADD COLUMN {col} {_COLUMN_TYPES.get(col, 'INTEGER')}")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def set_meta(self, **values) -> None:
        self.db.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                            [(k, None if v is None else str(v)) for k, v in values.items()])
        self.db.commit()

    def meta(self) -> dict:
        return {r["key"]: r["value"] for r in self.db.execute("SELECT key, value FROM meta")}

    def set_topics(self, topics: dict[int, str]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO topics(id, title) VALUES (?, ?)", list(topics.items()))
        self.db.commit()

    def topics(self) -> dict[int, str]:
        return {r[0]: r[1] for r in self.db.execute("SELECT id, title FROM topics ORDER BY id")}

    def last_id(self) -> int:
        return self.db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def upsert(self, rows: list[dict]) -> None:
        if not rows:
            return
        cols = ", ".join(COLUMNS)
        marks = ", ".join("?" for _ in COLUMNS)
        updates = ", ".join(f"{c}=excluded.{c}" for c in _UPDATABLE)
        self.db.executemany(
            f"INSERT INTO messages ({cols}) VALUES ({marks}) ON CONFLICT(id) DO UPDATE SET {updates}",
            [tuple(r.get(c) for c in COLUMNS) for r in rows],
        )
        self.db.commit()

    def pending_media(self, retry_failed: bool = False) -> list[int]:
        q = (f"SELECT id FROM messages WHERE media_type IN ({', '.join('?' for _ in DOWNLOADABLE)}) "
             "AND media_path IS NULL" + ("" if retry_failed else " AND media_error IS NULL") + " ORDER BY id")
        return [r[0] for r in self.db.execute(q, DOWNLOADABLE)]

    def set_media(self, msg_id: int, path: str | None, error: str | None = None) -> None:
        self.db.execute("UPDATE messages SET media_path=?, media_error=? WHERE id=?", (path, error, msg_id))
        self.db.commit()

    def rows(self):
        cur = self.db.execute(f"SELECT {', '.join(COLUMNS)} FROM messages ORDER BY id")
        for r in cur:
            yield dict(r)
