"""
Reuse-Yield Ledger. Most cost tools only ever count money SPENT. This one also books money NOT spent -
the avoided cost of re-solving a problem the org already solved, captured the moment the collaboration
broker surfaces a reusable pattern (or a live duplicate) to a developer who was about to start it.

The ledger persists only the *fact* of an avoided re-solve (an opportunity), never a frozen dollar
figure. The money is computed at READ time from the current derived data, so it is deterministic
(independent of ingest order) and it tracks the cohort as it grows - a re-solve that was below k
developers when first seen is credited later, automatically, once enough people have solved that work.

  * opportunity - one content-free row per unique (tenant, developer-pair, objective, work_type). The
    dedup key is exactly those stable ids (NOT the display label), so re-polling a live match - or the
    same match arriving under a re-redacted objective label - never double-books. A confirmed
    solved-reuse atomically upgrades a prior live-duplication for the same opportunity (higher value).

  * cost-to-solve - for an objective x work_type, take each developer's total spend on it and compute a
    WINSORIZED MEAN across developers (trim the extremes, average the rest). It is credited only when at
    least k developers have solved that work, so the figure is a k-anonymous aggregate (k>=5 by default),
    not attributable to any individual. Winsorization engages at >= 4 developers (the trimmed core has
    >= 2 members) and at the default k>=5 the core has >= 3, making it robust to one runaway session; a
    homogeneous cohort where every developer spent the same can still equal that common value, which
    discloses nothing new about any one of them.

  * savings line - reports surface a k-gated "reuse avoided ~$X this period" band, a SAVINGS estimate
    shown beside real spend, never summed into it. solved-reuse credits the full cost-to-solve;
    live-duplication a conservative fraction, since part of the second effort has usually happened.

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


def cost_to_solve(per_actor_costs: list[float]) -> float:
    """robust central estimate of the cost to solve a piece of work: winsorize the per-developer totals
    (trim ~the extremes) then average. a k-anonymous aggregate, gated by the caller on
    len(per_actor_costs) >= k. winsorization engages at n >= 4 (trimmed core has >= 2 members); at the
    default k >= 5 the core has >= 3 and is robust to a runaway session. below 4 it averages all (a
    blend of 2-3 developers); a lone value (n == 1) is only reachable if the operator sets k == 1."""
    s = sorted(c for c in per_actor_costs if c)
    n = len(s)
    if n == 0:
        return 0.0
    if n < 4:
        return sum(s) / n            # too few to symmetric-trim; average the blend (no single datapoint for n>=2)
    trim = max(1, n // 10)           # drop ~10% from each end
    core = s[trim:n - trim]
    return sum(core) / len(core)


@dataclass(frozen=True)
class AvoidedCostEvent:
    """one booked re-solve avoided (an opportunity). content-free: ids and slugs only, no prompt, no
    person. estimated_avoided_usd / actors are advisory only - summary() recomputes both at read time
    from the live derived data, so they never go stale and never depend on ingest order."""

    tenant_id: str
    objective_id: str
    work_type: str
    cluster_id: str                 # the topic label the match clustered on (display only, not keyed)
    estimated_avoided_usd: float
    mode: str                       # "solved_reuse" | "live_duplication"
    actors: int
    ts: float

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def estimate_avoided(per_actor_costs: list[float], mode: str) -> float:
    """winsorized cost-to-solve x a mode factor. empty -> 0 (nothing to credit yet)."""
    factor = _SOLVED_FACTOR if mode == "solved_reuse" else _LIVE_DUP_FACTOR
    return round(cost_to_solve(per_actor_costs) * factor, 6)


def _pair_key(a: str, b: str) -> str:
    # order-independent so (a,b) and (b,a) collapse to one booked opportunity
    return "|".join(sorted((a, b)))


def _dedup_key(tenant_id: str, pair: tuple[str, str], objective_id: str, work_type: str) -> str:
    # STABLE ids only - never the display label - so label drift on the same work can't re-book it.
    return f"{tenant_id}::{_pair_key(*pair)}::{objective_id}::{work_type}"


def _tenant_where(tenant: str | None, ph: str) -> tuple[str, tuple]:
    if tenant is None:
        return "", ()
    if tenant == "default":
        return f" WHERE (tenant_id={ph} OR tenant_id IS NULL)", (tenant,)
    return f" WHERE tenant_id={ph}", (tenant,)


class LedgerStore:
    """persisted avoided-cost OPPORTUNITIES. sqlite default, postgres via dsn. dedups a unique
    opportunity (tenant x pair x objective x work_type) so re-polling never double-books. the dollar
    value and k-gate are computed at READ time in summary() from the live derived store."""

    _ph = "?"

    def __init__(self, path: str | Path = "abenlux-ledger.db"):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._ddl()
        self._lock = threading.RLock()

    def _ddl(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS avoided ("
            " dedup_key TEXT PRIMARY KEY, tenant_id TEXT, objective_id TEXT, work_type TEXT,"
            " cluster_id TEXT, mode TEXT, ts REAL)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_tenant ON avoided(tenant_id)")
        self.conn.commit()

    def book(self, ev: AvoidedCostEvent, *, pair: tuple[str, str]) -> bool:
        """book an avoided-re-solve opportunity. returns True if newly booked (or upgraded), False if
        this opportunity was already booked at >= this value-tier. the mode upgrade
        (live_duplication -> solved_reuse) is atomic via the conditional upsert, so a concurrent
        live-duplication can never downgrade a confirmed solved-reuse."""
        key = _dedup_key(ev.tenant_id, pair, ev.objective_id, ev.work_type)
        with self._lock:
            # INSERT the opportunity; on conflict only the mode UPGRADE (live -> solved) updates, so the
            # higher-value classification wins regardless of arrival order. sqlite 3.24+ upsert.
            cur = self.conn.execute(
                "INSERT INTO avoided (dedup_key, tenant_id, objective_id, work_type, cluster_id, "
                "mode, ts) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(dedup_key) DO UPDATE SET mode=excluded.mode, cluster_id=excluded.cluster_id "
                "WHERE avoided.mode='live_duplication' AND excluded.mode='solved_reuse'",
                (key, ev.tenant_id, ev.objective_id, ev.work_type, ev.cluster_id, ev.mode, ev.ts),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def _opportunities(self, tenant: str | None) -> list[tuple]:
        where, params = _tenant_where(tenant, self._ph)
        return self.conn.execute(
            "SELECT tenant_id, objective_id, work_type, mode FROM avoided" + where, params
        ).fetchall()

    def summary(self, store, tenant: str | None = None, *, k: int = 5) -> dict:
        """k-gated avoided-cost rollup, recomputed from the LIVE derived store so it never goes stale
        and never depends on ingest order. an opportunity is credited only when its objective x
        work_type cost-to-solve is backed by >= k developers RIGHT NOW."""
        rows = self._opportunities(tenant)
        # cache the per (tenant, objective, work_type) cost vector so we hit the store once per group
        cohorts: dict[tuple, list[float]] = {}
        total = 0.0
        by_work_type: dict[str, float] = {}
        credited = suppressed = 0
        for tid, obj, wt, mode in rows:
            scope = tid if tid is not None else "default"
            ck = (scope, obj, wt)
            if ck not in cohorts:
                # wt is the stored work_type ('unknown' for unclassified). actor_costs_for matches the
                # unclassified bucket (NULL or 'unknown') uniformly, so it is never silently suppressed.
                cohorts[ck] = store.actor_costs_for(obj, wt, tenant=scope)
            costs = cohorts[ck]
            if len(costs) < k:                        # sub-k cost-to-solve -> never credited
                suppressed += 1
                continue
            value = estimate_avoided(costs, mode)
            total += value
            by_work_type[wt] = by_work_type.get(wt, 0.0) + value
            credited += 1
        return {
            "tenant": tenant,
            "reuse_avoided_usd": round(total, 2),
            "events_credited": credited,
            "events_suppressed": suppressed,        # below k developers, not credited
            "by_work_type": [{"work_type": w, "avoided_usd": round(v, 2)}
                             for w, v in sorted(by_work_type.items(), key=lambda x: -x[1])],
            "k": k,
            "note": "estimated avoided re-solves, k-anonymity gated, shown beside spend never inside it",
        }

    def close(self) -> None:
        self.conn.close()


class PostgresLedgerStore(LedgerStore):
    """Postgres backend (optional). Same opportunity model, same read-time recompute, BIGINT/double."""

    _ph = "%s"

    def __init__(self, dsn: str):
        import psycopg
        self.conn = psycopg.connect(dsn, autocommit=True)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS avoided ("
            " dedup_key TEXT PRIMARY KEY, tenant_id TEXT, objective_id TEXT, work_type TEXT,"
            " cluster_id TEXT, mode TEXT, ts DOUBLE PRECISION)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_tenant ON avoided(tenant_id)")
        self._lock = threading.RLock()

    def book(self, ev: AvoidedCostEvent, *, pair: tuple[str, str]) -> bool:
        key = _dedup_key(ev.tenant_id, pair, ev.objective_id, ev.work_type)
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO avoided (dedup_key, tenant_id, objective_id, work_type, cluster_id, "
                "mode, ts) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (dedup_key) DO UPDATE SET mode=EXCLUDED.mode, cluster_id=EXCLUDED.cluster_id "
                "WHERE avoided.mode='live_duplication' AND EXCLUDED.mode='solved_reuse'",
                (key, ev.tenant_id, ev.objective_id, ev.work_type, ev.cluster_id, ev.mode, ev.ts),
            )
            return cur.rowcount > 0


def open_ledger(dsn: str | Path) -> LedgerStore:
    s = str(dsn)
    if s.startswith("postgres://") or s.startswith("postgresql://"):
        return PostgresLedgerStore(s)
    return LedgerStore(s)
