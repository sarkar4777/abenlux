"""
The value side of the ledger. Spend tells you what the AI cost. It does not tell you whether the work
paid off. The outcome store closes that gap. A small content-free feed from git or the CI system reports
plain facts about each piece of work. Did the change merge. Was it reverted soon after. How many lines
it added and removed. These facts join to spend on the same keys attribution already uses, the ticket
and the objective, so the report can answer a question no spend-only tool can, which spend turned into
shipped work.

Nothing here is a guess and nothing here is content. It is booleans and counts, keyed by objective, and
the dollars-per-merged-change number is computed at read time from the live spend store, so it tracks
the team as it grows.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS outcomes ("
    " outcome_id TEXT PRIMARY KEY, ts REAL, tenant_id TEXT, objective_id TEXT, ticket_id TEXT,"
    " merged INTEGER, reverted INTEGER, lines_added INTEGER, lines_removed INTEGER)"
)

_FIELDS = ("outcome_id", "ts", "tenant_id", "objective_id", "ticket_id",
           "merged", "reverted", "lines_added", "lines_removed")


class OutcomeStore:
    def __init__(self, path: str | Path = "abenlux-outcomes.db"):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute(_SCHEMA)
        self.conn.commit()

    def record(self, d: dict) -> bool:
        # accept only the known content-free fields. a missing outcome_id or objective is dropped.
        row = {k: d.get(k) for k in _FIELDS}
        if not row["outcome_id"] or not row["objective_id"]:
            return False
        row["ts"] = float(row["ts"] or time.time())
        for b in ("merged", "reverted"):
            row[b] = 1 if row.get(b) else 0
        for c in ("lines_added", "lines_removed"):
            try:
                row[c] = max(0, int(row.get(c) or 0))
            except (TypeError, ValueError):
                row[c] = 0
        self.conn.execute(
            "INSERT OR REPLACE INTO outcomes (outcome_id, ts, tenant_id, objective_id, ticket_id,"
            " merged, reverted, lines_added, lines_removed) VALUES (?,?,?,?,?,?,?,?,?)",
            tuple(row[k] for k in _FIELDS))
        self.conn.commit()
        return True

    def by_objective(self, tenant: str | None = None) -> dict:
        # roll the outcome facts up per objective so the report can join them to spend.
        where, params = "", ()
        if tenant is not None:
            where = " WHERE (tenant_id=? OR tenant_id IS NULL)" if tenant == "default" else " WHERE tenant_id=?"
            params = (tenant,)
        rows = self.conn.execute(
            "SELECT objective_id, COUNT(*) n, COALESCE(SUM(merged),0), COALESCE(SUM(reverted),0),"
            " COALESCE(SUM(lines_added),0), COALESCE(SUM(lines_removed),0)"
            " FROM outcomes" + where + " GROUP BY objective_id", params).fetchall()
        out = {}
        for oid, n, merged, reverted, added, removed in rows:
            out[oid] = {"changes": n, "merged": merged, "reverted": reverted,
                        "lines_added": added, "lines_removed": removed}
        return out

    def close(self) -> None:
        self.conn.close()
