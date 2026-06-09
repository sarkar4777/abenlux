"""
Reuse-Yield Ledger. Most cost tools only ever count money SPENT. This one also books money NOT spent -
the avoided cost of re-solving a problem the org already solved, captured the moment the collaboration
broker surfaces a reusable pattern (or a live duplicate) to a developer who was about to start it.

The mechanism is honest and content-free:

  * cost-to-solve - for an objective x work_type, take each developer's total spend on it and use the
    MEDIAN across developers as the org's typical cost to solve that piece of work (median, not mean,
    so one runaway session can't inflate the estimate). this number is itself k-anonymity gated: it is
    only credited when at least k developers have solved that work, so it is never one person's figure.

  * avoided-cost event - when the broker matches two developers on the same topic, ONE content-free
    AvoidedCostEvent is booked: {tenant, objective, work_type, cluster, estimated_avoided_usd, mode}.
    solved-reuse (re-using an already-solved pattern) is credited at the full median cost-to-solve;
    live-duplication (caught while both are still working) is credited at a conservative fraction,
    because part of the second effort has usually already happened. each unique pair x objective x
    work_type is booked once - re-polling the same live match never double-counts.

  * savings line - reports surface a k-gated "reuse avoided ~$X this period" band. it is a SAVINGS
    estimate, presented as such, next to real spend - never mixed into the spend total.

Nothing here is a per-person figure or a management drill-down: it is an org/tenant aggregate of
avoided re-solves, the positive mirror of the orphan-waste line.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

# live-duplication is credited at a fraction of a full re-solve: the two developers overlap while both
# are still in flight, so only part of the second effort is actually avoided by catching it early.
_LIVE_DUP_FACTOR = 0.5
_SOLVED_FACTOR = 1.0


def median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


@dataclass(frozen=True)
class AvoidedCostEvent:
    """one booked re-solve avoided. content-free: ids and slugs only, no prompt, no person."""

    tenant_id: str
    objective_id: str
    work_type: str
    cluster_id: str                 # the topic label the match clustered on
    estimated_avoided_usd: float
    mode: str                       # "solved_reuse" | "live_duplication"
    actors: int                     # how many developers backed the cost-to-solve median (for k-gating)
    ts: float

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def estimate_avoided(per_actor_costs: list[float], mode: str) -> float:
    """median cost-to-solve x a mode factor. empty -> 0 (nothing to credit yet)."""
    factor = _SOLVED_FACTOR if mode == "solved_reuse" else _LIVE_DUP_FACTOR
    return round(median(per_actor_costs) * factor, 6)


def _pair_key(a: str, b: str) -> str:
    # order-independent so (a,b) and (b,a) collapse to one booked opportunity
    return "|".join(sorted((a, b)))


class LedgerStore:
    """persisted avoided-cost events. sqlite default, postgres via dsn. dedups a unique collaboration
    opportunity (pair x objective x work_type x cluster) so re-polling a live match can't double-book."""

    _ph = "?"

    def __init__(self, path: str | Path = "abenlux-ledger.db"):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._ddl("REAL")
        self._lock = threading.RLock()

    def _ddl(self, real: str) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS avoided ("
            " dedup_key TEXT PRIMARY KEY, tenant_id TEXT, objective_id TEXT, work_type TEXT,"
            f" cluster_id TEXT, estimated_avoided_usd {real}, mode TEXT, actors INTEGER, ts {real})"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_tenant ON avoided(tenant_id)")
        self.conn.commit()

    def book(self, ev: AvoidedCostEvent, *, pair: tuple[str, str]) -> bool:
        """book an avoided-cost event for a unique opportunity. returns True if newly booked, False if
        this pair already booked this objective x work_type x cluster (idempotent, no double-count).
        a solved_reuse upgrade over a prior live_duplication for the same key is allowed to overwrite,
        because the avoided value is higher once the reuse is confirmed."""
        key = f"{ev.tenant_id}::{_pair_key(*pair)}::{ev.objective_id}::{ev.work_type}::{ev.cluster_id}"
        with self._lock:
            row = self.conn.execute(
                "SELECT mode FROM avoided WHERE dedup_key=?", (key,)
            ).fetchone()
            if row is not None and not (row[0] == "live_duplication" and ev.mode == "solved_reuse"):
                return False
            self.conn.execute(
                "INSERT OR REPLACE INTO avoided "
                "(dedup_key, tenant_id, objective_id, work_type, cluster_id, estimated_avoided_usd, "
                " mode, actors, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (key, ev.tenant_id, ev.objective_id, ev.work_type, ev.cluster_id,
                 ev.estimated_avoided_usd, ev.mode, ev.actors, ev.ts),
            )
            self.conn.commit()
        return True

    def summary(self, tenant: str | None = None, *, k: int = 5) -> dict:
        """k-gated avoided-cost rollup. only events whose cost-to-solve median was backed by >= k
        developers are credited, so the savings line can never expose a sub-k group's spend."""
        params: tuple = ()
        where = ""
        if tenant is not None:
            if tenant == "default":
                where = " WHERE (tenant_id=? OR tenant_id IS NULL)"
                params = (tenant,)
            else:
                where = " WHERE tenant_id=?"
                params = (tenant,)
        rows = self.conn.execute(
            "SELECT mode, work_type, cluster_id, estimated_avoided_usd, actors FROM avoided" + where,
            params,
        ).fetchall()
        credited = [r for r in rows if r[4] >= k]
        total = round(sum(r[3] for r in credited), 2)
        by_work_type: dict[str, float] = {}
        for r in credited:
            # accumulate raw then round ONCE below - rounding each step drops sub-cent reuses to zero
            by_work_type[r[1]] = by_work_type.get(r[1], 0.0) + r[3]
        by_work_type = {w: round(v, 2) for w, v in by_work_type.items()}
        return {
            "tenant": tenant,
            "reuse_avoided_usd": total,
            "events_credited": len(credited),
            "events_suppressed": len(rows) - len(credited),  # below k, not credited
            "by_work_type": [{"work_type": w, "avoided_usd": v}
                             for w, v in sorted(by_work_type.items(), key=lambda x: -x[1])],
            "k": k,
            "note": "estimated avoided re-solves, k-anonymity gated, shown beside spend never inside it",
        }

    def close(self) -> None:
        self.conn.close()


class PostgresLedgerStore(LedgerStore):
    """Postgres backend (optional). Same dedup + k-gated summary, BIGINT/double types."""

    _ph = "%s"

    def __init__(self, dsn: str):
        import psycopg
        self.conn = psycopg.connect(dsn, autocommit=True)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS avoided ("
            " dedup_key TEXT PRIMARY KEY, tenant_id TEXT, objective_id TEXT, work_type TEXT,"
            " cluster_id TEXT, estimated_avoided_usd DOUBLE PRECISION, mode TEXT,"
            " actors BIGINT, ts DOUBLE PRECISION)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_tenant ON avoided(tenant_id)")
        self._lock = threading.RLock()

    def book(self, ev: AvoidedCostEvent, *, pair: tuple[str, str]) -> bool:
        key = f"{ev.tenant_id}::{_pair_key(*pair)}::{ev.objective_id}::{ev.work_type}::{ev.cluster_id}"
        with self._lock:
            row = self.conn.execute("SELECT mode FROM avoided WHERE dedup_key=%s", (key,)).fetchone()
            if row is not None and not (row[0] == "live_duplication" and ev.mode == "solved_reuse"):
                return False
            self.conn.execute(
                "INSERT INTO avoided (dedup_key, tenant_id, objective_id, work_type, cluster_id, "
                "estimated_avoided_usd, mode, actors, ts) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (dedup_key) DO UPDATE SET mode=EXCLUDED.mode, "
                "estimated_avoided_usd=EXCLUDED.estimated_avoided_usd, actors=EXCLUDED.actors",
                (key, ev.tenant_id, ev.objective_id, ev.work_type, ev.cluster_id,
                 ev.estimated_avoided_usd, ev.mode, ev.actors, ev.ts),
            )
        return True

    def summary(self, tenant: str | None = None, *, k: int = 5) -> dict:
        params: tuple = ()
        where = ""
        if tenant is not None:
            where = " WHERE (tenant_id=%s OR tenant_id IS NULL)" if tenant == "default" else " WHERE tenant_id=%s"
            params = (tenant,)
        rows = self.conn.execute(
            "SELECT mode, work_type, cluster_id, estimated_avoided_usd, actors FROM avoided" + where,
            params,
        ).fetchall()
        credited = [r for r in rows if r[4] >= k]
        total = round(sum(r[3] for r in credited), 2)
        by_work_type: dict[str, float] = {}
        for r in credited:
            # accumulate raw then round ONCE below - rounding each step drops sub-cent reuses to zero
            by_work_type[r[1]] = by_work_type.get(r[1], 0.0) + r[3]
        by_work_type = {w: round(v, 2) for w, v in by_work_type.items()}
        return {
            "tenant": tenant, "reuse_avoided_usd": total, "events_credited": len(credited),
            "events_suppressed": len(rows) - len(credited),
            "by_work_type": [{"work_type": w, "avoided_usd": v}
                             for w, v in sorted(by_work_type.items(), key=lambda x: -x[1])],
            "k": k,
            "note": "estimated avoided re-solves, k-anonymity gated, shown beside spend never inside it",
        }


def open_ledger(dsn: str | Path) -> LedgerStore:
    s = str(dsn)
    if s.startswith("postgres://") or s.startswith("postgresql://"):
        return PostgresLedgerStore(s)
    return LedgerStore(s)
