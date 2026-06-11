"""
Developer-private collaboration matches, server-side. The local feed (feed.py) lives on the
developer's device, the central API needs a place to surface matches in the dashboard too, but
WITHOUT becoming a management-readable "who duplicates whom" report - that artifact is an
efficiency-policing weapon and is exactly what the README forbids.

The mechanism that prevents it: every row is keyed by a single owner pseudonym, and the API only
ever queries `for_owner(caller.pseudonym)`. There is no list-all, no by-objective rollup, no
manager read path. A match between A and B writes one row for A and one for B, each seeing only
their own side. The peer's identity is stored as a pseudonym and revealed only after a mutual,
double-blind consent - and that consent is scoped to the SPECIFIC topic, so consenting to an intro
on one shared problem never auto-reveals identity on a different, later match between the same two.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from abenlux.developer.storage import private_db_path, secure_file

_SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner TEXT, peer TEXT, topic TEXT, similarity REAL, mode TEXT, ts REAL
);
CREATE INDEX IF NOT EXISTS idx_owner ON matches(owner);
CREATE TABLE IF NOT EXISTS consents (
  owner TEXT, peer TEXT, topic TEXT, ts REAL, PRIMARY KEY (owner, peer, topic)
);
"""


class MatchStore:
    def __init__(self, path: str | Path | None = None):
        # default to the developer's own ~/.abenlux dir (private), like the local feed - the storage
        # location IS part of the privacy guarantee. a shared collector sets ABEN_MATCH_DB explicitly.
        path = str(path) if path is not None else private_db_path("matches.db")
        # check_same_thread=False: written from the gateway's BackgroundTask thread (see store.py).
        # the lock serializes that concurrent access (one connection, many threads).
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")        # concurrent cross-connection access waits,
        self.conn.execute("PRAGMA busy_timeout=5000")       # instead of failing with 'database is locked'
        self.conn.executescript(_SCHEMA)
        self._migrate_consents()                            # add topic-scoping to an older consents table
        secure_file(path)                                   # 0600 so a co-tenant can't read it off disk
        self._lock = threading.Lock()

    def _migrate_consents(self) -> None:
        # consent used to be keyed (owner, peer); it is now (owner, peer, topic). an older db has no
        # topic column, so recreate the table - consent is transient state, re-granted on the next
        # intro, so dropping it is safe and avoids a stale pair-wide consent leaking on a new topic.
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(consents)").fetchall()}
        if "topic" not in cols:
            self.conn.execute("DROP TABLE IF EXISTS consents")
            self.conn.execute(
                "CREATE TABLE consents (owner TEXT, peer TEXT, topic TEXT, ts REAL, "
                "PRIMARY KEY (owner, peer, topic))")
            self.conn.commit()

    def record(self, owner: str, peer: str, topic: str, similarity: float, mode: str, *, ts: float | None = None) -> None:
        if owner == peer:
            return                                          # never store a self-match (defense in depth)
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

    def for_owner(self, owner: str, limit: int = 50, *, max_age_s: float | None = None) -> list[dict]:
        # max_age_s drops stale rows so a months-old "live_duplication" pairing isn't surfaced as
        # current. None = no age filter (the consent-ownership lookups want every owned row).
        with self._lock:
            if max_age_s is not None:
                cur = self.conn.execute(
                    "SELECT id, peer, topic, similarity, mode, ts FROM matches WHERE owner=? AND ts>=? "
                    "ORDER BY ts DESC LIMIT ?", (owner, time.time() - max_age_s, limit))
            else:
                cur = self.conn.execute(
                    "SELECT id, peer, topic, similarity, mode, ts FROM matches WHERE owner=? "
                    "ORDER BY ts DESC LIMIT ?", (owner, limit))
            return [dict(r) for r in cur.fetchall()]

    def record_consent(self, owner: str, peer: str, topic: str) -> None:
        if owner == peer:
            return
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO consents (owner, peer, topic, ts) VALUES (?,?,?,?)",
                (owner, peer, topic, time.time()))
            self.conn.commit()

    def has_consented(self, owner: str, peer: str, topic: str) -> bool:
        # whether `owner` has already requested an intro to `peer` ON THIS TOPIC (one direction)
        with self._lock:
            return self.conn.execute(
                "SELECT 1 FROM consents WHERE owner=? AND peer=? AND topic=?",
                (owner, peer, topic)).fetchone() is not None

    def mutually_consented(self, a: str, b: str, topic: str) -> bool:
        if a == b:
            return False
        with self._lock:
            cur = self.conn.execute(
                "SELECT (SELECT 1 FROM consents WHERE owner=? AND peer=? AND topic=?) AND "
                "(SELECT 1 FROM consents WHERE owner=? AND peer=? AND topic=?)",
                (a, b, topic, b, a, topic))
            return bool(cur.fetchone()[0])

    def close(self) -> None:
        self.conn.close()
