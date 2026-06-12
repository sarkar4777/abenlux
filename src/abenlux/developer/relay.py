"""
The async help relay. The double-blind intro is careful but slow. Both sides must opt in before either
gets anything, which on a team spread across time zones means a multi-day back and forth before a single
question is asked. The relay flips the order. A developer who matches a peer can send one redacted
question right away, and the peer can answer when they wake up, all without either side seeing who the
other is. The intro is still there for going deeper, but the first useful exchange no longer waits on it.

Every message is redacted before it is stored, so a secret or an address a developer types by mistake
never lands here. Threads are keyed by pseudonym and topic, so the store never needs the raw identity,
and there is no management read path to it.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from abenlux.developer.storage import private_db_path, secure_file

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS threads ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, a TEXT, b TEXT, topic TEXT, messages TEXT, ts REAL)"
)


class RelayStore:
    def __init__(self, path: str | Path | None = None):
        path = str(path) if path is not None else private_db_path("relay.db")
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute(_SCHEMA)
        self.conn.commit()
        secure_file(path)

    def _find(self, a: str, b: str, topic: str):
        # a thread is shared by the pair on a topic regardless of who opened it
        return self.conn.execute(
            "SELECT id, messages FROM threads WHERE topic=? AND ((a=? AND b=?) OR (a=? AND b=?))",
            (topic, a, b, b, a)).fetchone()

    def ask(self, sender: str, recipient: str, topic: str, text: str) -> int:
        # open a thread or add to the existing one. text is already redacted by the caller.
        row = self._find(sender, recipient, topic)
        msg = {"from": sender, "text": text, "ts": time.time()}
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO threads (a, b, topic, messages, ts) VALUES (?,?,?,?,?)",
                (sender, recipient, topic, json.dumps([msg]), time.time()))
            self.conn.commit()
            return cur.lastrowid
        tid, raw = row
        msgs = json.loads(raw)
        msgs.append(msg)
        self.conn.execute("UPDATE threads SET messages=?, ts=? WHERE id=?",
                          (json.dumps(msgs), time.time(), tid))
        self.conn.commit()
        return tid

    def reply(self, thread_id: int, sender: str, text: str) -> bool:
        row = self.conn.execute("SELECT a, b, messages FROM threads WHERE id=?", (thread_id,)).fetchone()
        if row is None or sender not in (row[0], row[1]):
            return False                       # only a participant can reply
        msgs = json.loads(row[2])
        msgs.append({"from": sender, "text": text, "ts": time.time()})
        self.conn.execute("UPDATE threads SET messages=?, ts=? WHERE id=?",
                          (json.dumps(msgs), time.time(), thread_id))
        self.conn.commit()
        return True

    def for_participant(self, pseudonym: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, a, b, topic, messages, ts FROM threads WHERE a=? OR b=? ORDER BY ts DESC",
            (pseudonym, pseudonym)).fetchall()
        out = []
        for tid, a, b, topic, raw, ts in rows:
            peer = b if a == pseudonym else a
            msgs = [{"mine": m["from"] == pseudonym, "text": m["text"], "ts": m["ts"]}
                    for m in json.loads(raw)]
            out.append({"id": tid, "topic": topic, "peer": peer, "messages": msgs, "ts": ts})
        return out

    def close(self) -> None:
        self.conn.close()
