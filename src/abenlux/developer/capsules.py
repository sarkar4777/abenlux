"""
Solution capsules. When the broker spots that a developer is starting on something the org already
solved, a plain pointer to a person they cannot yet contact is weak help. A capsule makes that help
real and immediate. It carries only content-free facts about how the work was cracked, never the code
and never the prompt. The facts are which kind of work it was, which model and tool cracked it, how
many retry loops it took, and a coarse cost band. A developer who matches a solved problem sees those
facts at match time, so they can pick the right model first and skip the trial and error, with the
optional deeper intro reserved for going further.

The capsule lives keyed by the solver's pseudonym and the topic, so the store never needs the raw
identity, and there is no management read path to it.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from abenlux.developer.storage import private_db_path, secure_file

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS capsules ("
    " pseudonym TEXT, topic TEXT, facts TEXT, ts REAL,"
    " PRIMARY KEY (pseudonym, topic))"
)


def cost_band(usd: float) -> str:
    # a coarse band so the figure is never one developer's exact cost, only a rough scale
    if usd <= 0:
        return "unknown"
    if usd < 1:
        return "under $1"
    if usd < 5:
        return "$1 to $5"
    if usd < 20:
        return "$5 to $20"
    return "over $20"


class CapsuleStore:
    def __init__(self, path: str | Path | None = None):
        path = str(path) if path is not None else private_db_path("capsules.db")
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute(_SCHEMA)
        self.conn.commit()
        secure_file(path)

    def record_solved(self, pseudonym: str, topic: str, *, work_type: str, model: str, tool: str,
                      retry_loops: int, usd: float, ts: float | None = None) -> dict:
        # remember how this developer cracked this topic. content-free facts only.
        facts = {"work_type": work_type or "unknown", "model": model or "unknown",
                 "tool": tool or "unknown", "retry_loops": int(retry_loops or 0),
                 "cost_band": cost_band(usd)}
        self.conn.execute("INSERT OR REPLACE INTO capsules (pseudonym, topic, facts, ts) VALUES (?,?,?,?)",
                          (pseudonym, topic, json.dumps(facts), ts if ts is not None else time.time()))
        self.conn.commit()
        return facts

    def get(self, pseudonym: str, topic: str) -> dict | None:
        row = self.conn.execute("SELECT facts FROM capsules WHERE pseudonym=? AND topic=?",
                                (pseudonym, topic)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self) -> None:
        self.conn.close()
