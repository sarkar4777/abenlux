"""
Developer-private collaboration matches, server-side. The local feed (feed.py) lives on the
developer's device, the central API needs a place to surface matches in the dashboard too, but
WITHOUT becoming a management-readable "who duplicates whom" report - that artifact is an
efficiency-policing weapon and is exactly what the README forbids.

The mechanism that prevents it: every row is keyed by a single owner pseudonym, and the API only
ever queries `for_owner(caller.pseudonym)`. There is no list-all, no by-objective rollup, no
manager read path. A match between A and B writes one row for A and one for B, each seeing only
their own side. The peer's identity is stored as a pseudonym and revealed only after a mutual,
double-blind consent - never by default.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner TEXT, peer TEXT, topic TEXT, similarity REAL, mode TEXT, ts REAL
);
CREATE INDEX IF NOT EXISTS idx_owner ON matches(owner);
CREATE TABLE IF NOT EXISTS consents (
  owner TEXT, peer TEXT, ts REAL, PRIMARY KEY (owner, peer)
);
"""


class MatchStore:
    def __init__(self, path: str | Path = "abenlux-matches.db"):
        # check_same_thread=False: written from the gateway's BackgroundTask thread (see store.py).
        # the lock serializes that concurrent access (one connection, many threads).
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def record(self, owner: str, peer: str, topic: str, similarity: float, mode: str, *, ts: float | None = None) -> None:
        # dedup on (owner, peer, topic): a colleague repeatedly hitting the same problem must refresh
        # the existing row, not pile up identical rows in the developer's feed.
        ts = ts if ts is not None else time.time()
        with self._lock:
            row = self.conn.execute(
                "SELECT id FROM matches WHERE owner=? AND peer=? AND topic=?", (owner, peer, topic)).fetchone()
            if row:
                self.conn.execute("UPDATE matches SET similarity=?, mode=?, ts=? WHERE id=?",
                                  (similarity, mode, ts, row[0]))
            else:
                self.conn.execute(
                    "INSERT INTO matches (owner, peer, topic, similarity, mode, ts) VALUES (?,?,?,?,?,?)",
                    (owner, peer, topic, similarity, mode, ts))
            self.conn.commit()

    def for_owner(self, owner: str, limit: int = 50) -> list[dict]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT id, peer, topic, similarity, mode, ts FROM matches WHERE owner=? ORDER BY ts DESC LIMIT ?",
                (owner, limit))
            return [dict(r) for r in cur.fetchall()]

    def record_consent(self, owner: str, peer: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO consents (owner, peer, ts) VALUES (?,?,?)", (owner, peer, time.time()))
            self.conn.commit()

    def has_consented(self, owner: str, peer: str) -> bool:
        # whether `owner` has already requested an intro to `peer` (one direction)
        with self._lock:
            return self.conn.execute(
                "SELECT 1 FROM consents WHERE owner=? AND peer=?", (owner, peer)).fetchone() is not None

    def mutually_consented(self, a: str, b: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "SELECT (SELECT 1 FROM consents WHERE owner=? AND peer=?) AND "
                "(SELECT 1 FROM consents WHERE owner=? AND peer=?)", (a, b, b, a))
            return bool(cur.fetchone()[0])

    def close(self) -> None:
        self.conn.close()
