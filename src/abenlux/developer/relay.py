"""
The async help relay. The double-blind intro is careful but slow. Both sides must opt in before either
gets anything, which on a team spread across time zones means a multi-day back and forth before a single
question is asked. The relay flips the order. A developer who matches a peer can send one redacted
question right away, and the peer can answer when they wake up, all without either side seeing who the
other is until they both opt in through the existing consent.

Each message is its OWN row, appended atomically, so two people writing at once never overwrite each
other. Every message is also cleaned of secrets here in the store, so the guarantee holds no matter who
calls it. Threads are keyed by pseudonym and topic, so the store never needs the raw identity, and there
is no management read path to it.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from abenlux.developer.storage import private_db_path, secure_file

_SCHEMA = [
    "CREATE TABLE IF NOT EXISTS threads (id INTEGER PRIMARY KEY AUTOINCREMENT, a TEXT, b TEXT, topic TEXT, ts REAL)",
    "CREATE TABLE IF NOT EXISTS messages (thread_id INTEGER, sender TEXT, text TEXT, ts REAL)",
    "CREATE INDEX IF NOT EXISTS idx_threads_a ON threads(a)",
    "CREATE INDEX IF NOT EXISTS idx_threads_b ON threads(b)",
    "CREATE INDEX IF NOT EXISTS idx_threads_pair ON threads(topic, a, b)",
    "CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread_id)",
]


def _clean(text: str) -> str:
    # strip any secret or address the developer typed by mistake, here in the store as well as the API
    from abenlux.processing.redact import redact
    return redact(str(text)[:4000]).text.strip()


class RelayStore:
    def __init__(self, path: str | Path | None = None):
        path = str(path) if path is not None else private_db_path("relay.db")
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        for stmt in _SCHEMA:
            self.conn.execute(stmt)
        self.conn.commit()
        secure_file(path)

    def _thread_id(self, a: str, b: str, topic: str) -> int:
        row = self.conn.execute(
            "SELECT id FROM threads WHERE topic=? AND ((a=? AND b=?) OR (a=? AND b=?))",
            (topic, a, b, b, a)).fetchone()
        if row is not None:
            return row[0]
        cur = self.conn.execute("INSERT INTO threads (a, b, topic, ts) VALUES (?,?,?,?)",
                                (a, b, topic, time.time()))
        self.conn.commit()
        return cur.lastrowid

    def _append(self, thread_id: int, sender: str, text: str) -> None:
        # one row per message, so concurrent writers never clobber each other
        self.conn.execute("INSERT INTO messages (thread_id, sender, text, ts) VALUES (?,?,?,?)",
                          (thread_id, sender, text, time.time()))
        self.conn.execute("UPDATE threads SET ts=? WHERE id=?", (time.time(), thread_id))
        self.conn.commit()

    def ask(self, sender: str, recipient: str, topic: str, text: str) -> int:
        tid = self._thread_id(sender, recipient, topic)
        self._append(tid, sender, _clean(text))
        return tid

    def reply(self, thread_id: int, sender: str, text: str) -> bool:
        row = self.conn.execute("SELECT a, b FROM threads WHERE id=?", (thread_id,)).fetchone()
        if row is None or sender not in (row[0], row[1]):
            return False                       # only a participant can reply
        self._append(thread_id, sender, _clean(text))
        return True

    def for_participant(self, pseudonym: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, a, b, topic FROM threads WHERE a=? OR b=? ORDER BY ts DESC",
            (pseudonym, pseudonym)).fetchall()
        out = []
        for tid, a, b, topic in rows:
            peer = b if a == pseudonym else a
            msgs = [{"mine": s == pseudonym, "text": t, "ts": ts} for s, t, ts in self.conn.execute(
                "SELECT sender, text, ts FROM messages WHERE thread_id=? ORDER BY ts", (tid,)).fetchall()]
            out.append({"id": tid, "topic": topic, "peer": peer, "messages": msgs})
        return out

    def close(self) -> None:
        self.conn.close()
