"""
Persistence for the derived layer ONLY. No prompt/response text ever lands here - by the time a
DerivedRecord exists, content has been redacted and discarded upstream. Identity and raw content
are absent by construction, so this file is safe to back up and query without a content review.

Two backends, one query surface (`_BaseStore`):

  * SQLite (default, zero-config) - perfect for the demo, a solo developer, or a pilot of a few
    hundred. Opened in WAL mode with a busy timeout so the gateway's concurrent BackgroundTask
    writers don't serialize or trip "database is locked".
  * Postgres (optional, `pip install abenlux[postgres]`) - the backend for thousands of developers.
    Same logical schema and queries, `open_store("postgresql://…")` selects it.

`open_store(dsn)` picks the backend from the DSN, so the gateway/collector/CLI are backend-agnostic.
The report/drift/budget aggregates live here as plain SQL, the k-anonymity gate that decides what an
aggregate is *allowed* to show lives in analytics, so the privacy rule stays testable in isolation.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from abenlux.schema import DerivedRecord


class _Result:
    """a materialized query result. one store connection is shared across the gateway's BackgroundTask
    threadpool, and a lazy DB-API cursor held across threads corrupts state - so _exec fetches under the
    lock and hands back a fully-read result that exposes the small cursor surface the helpers use."""

    def __init__(self, rows: list, description):
        self._rows = rows
        self.description = description

    def fetchall(self) -> list:
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

_COLUMNS = [
    "event_id", "ts", "tier", "provider", "actor_pseudonym", "request_model",
    "input_tokens", "output_tokens", "duplicate_history_tokens",
    "cache_read_tokens", "cache_creation_tokens", "tokens_estimated",
    "cost_usd", "cost_priced",
    "tool", "app_category", "repo", "host_os",
    "embedding", "quality_score", "acceptance",
    "is_retry_loop", "retry_similarity",
    "objective_id", "objective_label", "is_orphan",
    "attribution_method", "attribution_confidence",
    "ticket_id", "work_type", "work_type_source", "residency", "tenant_id",
    "saved_input_tokens", "compression", "served_from_cache",
]

# portable DDL (works on both engines), booleans are stored 0/1 for parity.
def _ddl(ts_type: str, int_type: str, real_type: str) -> list[str]:
    return [
        f"""CREATE TABLE IF NOT EXISTS derived (
          event_id TEXT PRIMARY KEY,
          ts {ts_type}, tier TEXT, provider TEXT,
          actor_pseudonym TEXT, request_model TEXT,
          input_tokens {int_type}, output_tokens {int_type}, duplicate_history_tokens {int_type},
          cache_read_tokens {int_type}, cache_creation_tokens {int_type}, tokens_estimated {int_type},
          cost_usd {real_type}, cost_priced {int_type},
          tool TEXT, app_category TEXT, repo TEXT, host_os TEXT,
          embedding TEXT, quality_score {real_type}, acceptance {real_type},
          is_retry_loop {int_type}, retry_similarity {real_type},
          objective_id TEXT, objective_label TEXT, is_orphan {int_type},
          attribution_method TEXT, attribution_confidence {real_type},
          ticket_id TEXT, work_type TEXT, work_type_source TEXT, residency TEXT, tenant_id TEXT,
          saved_input_tokens {int_type}, compression TEXT, served_from_cache {int_type}
        )""",
        "CREATE INDEX IF NOT EXISTS idx_obj ON derived(objective_id)",
        "CREATE INDEX IF NOT EXISTS idx_actor ON derived(actor_pseudonym)",
        "CREATE INDEX IF NOT EXISTS idx_tool ON derived(tool)",
        "CREATE INDEX IF NOT EXISTS idx_ts ON derived(ts)",
        "CREATE INDEX IF NOT EXISTS idx_wt ON derived(work_type)",
    ]


def _coltypes(int_type: str, real_type: str) -> dict:
    # column -> sql type, used to add any missing columns when opening an older db
    ints = {"input_tokens", "output_tokens", "duplicate_history_tokens", "cache_read_tokens",
            "cache_creation_tokens", "tokens_estimated", "cost_priced", "is_retry_loop", "is_orphan",
            "saved_input_tokens", "served_from_cache"}
    reals = {"ts", "cost_usd", "quality_score", "acceptance", "retry_similarity", "attribution_confidence"}
    out = {}
    for c in _COLUMNS:
        out[c] = int_type if c in ints else (real_type if c in reals else "TEXT")
    return out


def _values(r: DerivedRecord) -> tuple:
    return (
        r.event_id, r.ts, r.tier, r.provider, r.actor_pseudonym, r.request_model,
        r.input_tokens, r.output_tokens, r.duplicate_history_tokens,
        r.cache_read_tokens, r.cache_creation_tokens, int(r.tokens_estimated),
        r.cost_usd, int(r.cost_priced),
        r.tool, r.app_category, r.repo, r.host_os,
        json.dumps(r.embedding) if r.embedding else None, r.quality_score, r.acceptance,
        int(r.is_retry_loop), r.retry_similarity,
        r.objective_id, r.objective_label, int(r.is_orphan),
        r.attribution_method, r.attribution_confidence,
        r.ticket_id, r.work_type, r.work_type_source, r.residency, r.tenant_id,
        r.saved_input_tokens, r.compression, int(r.served_from_cache),
    )


class _BaseStore:
    """shared query logic. subclasses provide a DB-API connection, a placeholder, and an upsert."""

    _ph = "?"

    def _q(self, sql: str) -> str:
        return sql if self._ph == "?" else sql.replace("?", self._ph)

    def _lock(self) -> threading.RLock:
        lk = self.__dict__.get("_rlock")
        if lk is None:
            lk = self.__dict__["_rlock"] = threading.RLock()
        return lk

    def _exec(self, sql: str, params: tuple = ()):
        # serialize all connection access (the gateway writes from many threads) and materialize the
        # result so the connection is never pinned by a lazy cursor handed to another thread.
        with self._lock():
            cur = self.conn.execute(self._q(sql), params)
            try:
                rows = cur.fetchall()
            except Exception:
                rows = []                          # INSERT/DDL/upsert without a result set
            return _Result(rows, cur.description)

    @staticmethod
    def _tenant_pred(tenant: str | None, col: str = "tenant_id") -> tuple[str, tuple]:
        # build a WHERE fragment scoping to one tenant. "default" also matches NULL so rows written
        # before the tenant column existed (migrated dbs) still belong to the default tenant. None ->
        # no scoping (org-wide / legacy single-tenant behavior, the report default).
        if tenant is None:
            return "", ()
        if tenant == "default":
            return f"({col}=? OR {col} IS NULL)", (tenant,)
        return f"{col}=?", (tenant,)

    @staticmethod
    def _and(pred: str, lead: str = "WHERE") -> str:
        # splice a tenant predicate into a query: lead is WHERE for a query that has none yet, AND for
        # one that already filters. empty pred collapses to nothing.
        if not pred:
            return ""
        return f" {lead} {pred}"

    @staticmethod
    def _rows(cur) -> list[dict]:
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    @staticmethod
    def _row(cur) -> dict | None:
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([d[0] for d in cur.description], row))

    # ----- write -----
    def insert(self, r: DerivedRecord) -> None:
        raise NotImplementedError

    # ----- aggregate reads (gated by k-anonymity in analytics) -----
    def orphan_token_share(self, tenant: str | None = None) -> float:
        pred, params = self._tenant_pred(tenant)
        cur = self._exec(
            "SELECT COALESCE(SUM(input_tokens+output_tokens),0), "
            "COALESCE(SUM(CASE WHEN is_orphan=1 THEN input_tokens+output_tokens ELSE 0 END),0) FROM derived"
            + self._and(pred), params
        )
        total, orphan = cur.fetchone()
        return (orphan / total) if total else 0.0

    def totals(self, tenant: str | None = None) -> dict:
        pred, params = self._tenant_pred(tenant)
        return self._row(self._exec(
            "SELECT COUNT(*) n, COALESCE(SUM(input_tokens+output_tokens),0) tokens, "
            "COALESCE(SUM(cost_usd),0) cost, COUNT(DISTINCT actor_pseudonym) actors, "
            "COALESCE(SUM(duplicate_history_tokens),0) dup_tokens, "
            "COALESCE(SUM(cache_read_tokens),0) cache_read, "
            "COALESCE(SUM(cache_creation_tokens),0) cache_creation, "
            "COALESCE(SUM(input_tokens),0) input_tokens, "
            "COALESCE(SUM(CASE WHEN is_retry_loop=1 THEN 1 ELSE 0 END),0) retries, "
            "COALESCE(SUM(CASE WHEN cost_priced=0 THEN 1 ELSE 0 END),0) unpriced "
            "FROM derived" + self._and(pred), params
        ))

    def rollup(self, dimension: str, tenant: str | None = None) -> list[dict]:
        allowed = {
            "objective": "objective_label", "tool": "tool", "model": "request_model",
            "provider": "provider", "tier": "tier", "work_type": "work_type",
        }
        if dimension not in allowed:
            raise ValueError(f"unknown rollup dimension {dimension!r}")
        col = allowed[dimension]
        pred, params = self._tenant_pred(tenant)
        return self._rows(self._exec(
            f"SELECT COALESCE({col},'(unattributed)') AS label, COUNT(*) AS calls, "
            "COALESCE(SUM(input_tokens+output_tokens),0) AS tokens, "
            "COALESCE(SUM(cost_usd),0) AS cost, COUNT(DISTINCT actor_pseudonym) AS actors "
            "FROM derived" + self._and(pred) + " GROUP BY label ORDER BY cost DESC", params
        ))

    def ticket_rollup(self) -> list[dict]:
        # spend per ticket with its work type - the trace from dollars to the specific piece of work
        return self._rows(self._exec(
            "SELECT ticket_id, COALESCE(work_type,'?') AS work_type, COUNT(*) AS calls, "
            "COALESCE(SUM(cost_usd),0) AS cost, COALESCE(objective_label,'') AS objective "
            "FROM derived WHERE ticket_id IS NOT NULL "
            "GROUP BY ticket_id, work_type, objective ORDER BY cost DESC"
        ))

    def new_objectives(self, since_ts: float, tenant: str | None = None) -> list[dict]:
        """objectives whose FIRST-EVER activity is at/after since_ts - i.e. new things being built
        this period. each row carries spend, devs, and the dominant work type, for traceability."""
        pred, params = self._tenant_pred(tenant)
        inner, _ = self._tenant_pred(tenant, "d2.tenant_id")
        return self._rows(self._exec(
            "SELECT objective_id, objective_label, MIN(ts) AS first_ts, "
            "COUNT(DISTINCT actor_pseudonym) AS actors, COALESCE(SUM(cost_usd),0) AS cost, "
            "(SELECT work_type FROM derived d2 WHERE d2.objective_id=d.objective_id "
            f" AND work_type IS NOT NULL{self._and(inner, 'AND')} "
            " GROUP BY work_type ORDER BY SUM(cost_usd) DESC LIMIT 1) AS work_type "
            "FROM derived d WHERE objective_id IS NOT NULL" + self._and(pred, "AND") + " "
            "GROUP BY objective_id, objective_label HAVING MIN(ts) >= ? ORDER BY cost DESC",
            params + params + (since_ts,),
        ))

    def time_bounds(self, tenant: str | None = None) -> tuple[float, float]:
        pred, params = self._tenant_pred(tenant)
        row = self._exec("SELECT MIN(ts), MAX(ts) FROM derived" + self._and(pred), params).fetchone()
        return (row[0] or 0.0, row[1] or 0.0)

    def window_stats(self, start_ts: float, end_ts: float, tenant: str | None = None) -> dict:
        pred, params = self._tenant_pred(tenant)
        d = self._row(self._exec(
            "SELECT COUNT(*) events, COUNT(DISTINCT actor_pseudonym) actors, "
            "COALESCE(SUM(input_tokens+output_tokens),0) tokens, COALESCE(SUM(cost_usd),0) cost, "
            "COALESCE(SUM(CASE WHEN is_orphan=1 THEN input_tokens+output_tokens ELSE 0 END),0) orphan_tokens "
            "FROM derived WHERE ts >= ? AND ts < ?" + self._and(pred, "AND"),
            (start_ts, end_ts) + params,
        ))
        d["orphan_share"] = (d["orphan_tokens"] / d["tokens"]) if d["tokens"] else 0.0
        return d

    def objective_window_cost(self, objective_id: str, start_ts: float, end_ts: float,
                              tenant: str | None = None) -> float:
        pred, params = self._tenant_pred(tenant)
        cur = self._exec(
            "SELECT COALESCE(SUM(cost_usd),0) FROM derived WHERE objective_id=? AND ts >= ? AND ts < ?"
            + self._and(pred, "AND"),
            (objective_id, start_ts, end_ts) + params,
        )
        return cur.fetchone()[0] or 0.0

    def objective_window_actors(self, objective_id: str, start_ts: float, end_ts: float,
                                tenant: str | None = None) -> int:
        # distinct developers on one objective WITHIN the budget period. the budget k-gate must use this
        # (not all-time actors): an objective worked by many people historically but by only 1-2 THIS
        # period would otherwise leak that period's near-individual spend through the budget line.
        pred, params = self._tenant_pred(tenant)
        cur = self._exec(
            "SELECT COUNT(DISTINCT actor_pseudonym) FROM derived WHERE objective_id=? AND ts >= ? AND ts < ?"
            + self._and(pred, "AND"),
            (objective_id, start_ts, end_ts) + params,
        )
        return cur.fetchone()[0] or 0

    def actor_costs_for(self, objective_id: str, work_type: str | None,
                        tenant: str | None = None) -> list[float]:
        """per-actor total cost on one objective x work_type - the basis for the reuse-yield
        cost-to-solve. the UNCLASSIFIED bucket is requested as None or the 'unknown' sentinel and matches
        BOTH a NULL work_type and the literal string 'unknown' (the classifier emits the string, but
        other ingest paths may leave NULL), so an unclassified opportunity is never silently dropped."""
        pred, params = self._tenant_pred(tenant)
        if work_type is None or work_type == "unknown":
            wt_clause = "(work_type IS NULL OR work_type='unknown')"
            wt_params: tuple = ()
        else:
            wt_clause = "work_type=?"
            wt_params = (work_type,)
        rows = self._exec(
            "SELECT actor_pseudonym, COALESCE(SUM(cost_usd),0) c FROM derived "
            f"WHERE objective_id=? AND {wt_clause}" + self._and(pred, "AND") +
            " GROUP BY actor_pseudonym",
            (objective_id,) + wt_params + params,
        ).fetchall()
        return [r[1] for r in rows if r[1]]

    def distinct_tenants(self) -> list[str]:
        """tenant_ids present in the derived data. NULL (pre-tenant rows) is reported as 'default'."""
        rows = self._exec(
            "SELECT DISTINCT COALESCE(tenant_id,'default') FROM derived ORDER BY 1"
        ).fetchall()
        return [r[0] for r in rows]

    def actor_work_types(self, actor_pseudonym: str) -> list[dict]:
        # one developer's own purpose mix (feature/fix/...) for their private view
        return self._rows(self._exec(
            "SELECT COALESCE(work_type,'unknown') AS label, COALESCE(SUM(cost_usd),0) AS cost, "
            "COUNT(*) AS calls FROM derived WHERE actor_pseudonym=? GROUP BY label ORDER BY cost DESC",
            (actor_pseudonym,),
        ))

    def actor_summary(self, actor_pseudonym: str, *, start_ts: float | None = None,
                      end_ts: float | None = None) -> dict:
        """one developer's OWN view. private to them, never a management surface. an optional time window
        powers a 'today' / since-midnight view and a burn-rate projection."""
        clause, params = "", (actor_pseudonym,)
        if start_ts is not None:
            clause += " AND ts >= ?"
            params += (start_ts,)
        if end_ts is not None:
            clause += " AND ts < ?"
            params += (end_ts,)
        return self._row(self._exec(
            "SELECT COUNT(*) calls, COALESCE(SUM(input_tokens+output_tokens),0) tokens, "
            "COALESCE(SUM(cost_usd),0) cost, COALESCE(SUM(duplicate_history_tokens),0) dup_tokens, "
            "COALESCE(SUM(cache_read_tokens),0) cache_read, COALESCE(SUM(input_tokens),0) input_tokens, "
            "COALESCE(SUM(CASE WHEN is_retry_loop=1 THEN 1 ELSE 0 END),0) retries "
            "FROM derived WHERE actor_pseudonym=?" + clause,
            params,
        ))

    def recent_records(self, actor_pseudonym: str, n: int = 20, *, since_ts: float | None = None,
                       objective: str | None = None, order: str = "ts") -> list[dict]:
        """one developer's OWN recent calls (per-call drill-down). private to them - scoped to their
        pseudonym, never a management surface. order by 'ts' (recent first) or 'cost' (most expensive)."""
        clause, params = "", (actor_pseudonym,)
        if since_ts is not None:
            clause += " AND ts >= ?"
            params += (since_ts,)
        if objective is not None:
            clause += " AND objective_label = ?"
            params += (objective,)
        order_col = "cost_usd" if order == "cost" else "ts"
        return self._rows(self._exec(
            "SELECT ts, tool, request_model, input_tokens, output_tokens, cache_read_tokens, "
            "cost_usd, cost_priced, COALESCE(objective_label,'(unattributed)') AS objective, "
            "COALESCE(work_type,'?') AS work_type, ticket_id, is_retry_loop "
            "FROM derived WHERE actor_pseudonym=?" + clause +
            f" ORDER BY {order_col} DESC LIMIT ?",
            params + (n,),
        ))

    def claim_null_tenant(self, tenant: str) -> int:
        """assign legacy pre-tenant rows (tenant_id IS NULL) to a concrete tenant. an edge/single-tenant
        deployment upgraded in place (the column was added by ALTER TABLE with no backfill) and then
        pointed at a named tenant would otherwise orphan its whole history out of tenant-scoped reports,
        since a named-tenant predicate excludes NULL. called on the edge where the tenant is known."""
        if tenant == "default":
            return 0                       # 'default' already absorbs NULL via the tenant predicate
        with self._lock():
            cur = self.conn.execute(
                self._q("UPDATE derived SET tenant_id=? WHERE tenant_id IS NULL"), (tenant,))
            try:
                self.conn.commit()
            except Exception:
                pass                       # autocommit backends (postgres) have nothing to commit
            return cur.rowcount or 0

    def _ensure_columns(self, coltypes: dict) -> None:
        # add any columns missing from an older db, so opening it upgrades the schema in place
        have = self._existing_columns()
        for col, typ in coltypes.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE derived ADD COLUMN {col} {typ}")

    def _existing_columns(self) -> set:
        raise NotImplementedError

    def close(self) -> None:
        self.conn.close()


class DerivedStore(_BaseStore):
    """SQLite backend (default). WAL + busy timeout for concurrent BackgroundTask writers."""

    _ph = "?"

    def __init__(self, path: str | Path = "abenlux.db"):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        stmts = _ddl("REAL", "INTEGER", "REAL")
        self.conn.execute(stmts[0])                       # create table if absent
        self._ensure_columns(_coltypes("INTEGER", "REAL"))  # add missing columns on older dbs
        for stmt in stmts[1:]:                            # then the indexes
            self.conn.execute(stmt)
        self.conn.commit()

    def _existing_columns(self) -> set:
        return {r[1] for r in self.conn.execute("PRAGMA table_info(derived)").fetchall()}

    def insert(self, r: DerivedRecord) -> None:
        # hold the lock across execute+commit so concurrent BackgroundTask writers don't interleave
        with self._lock():
            self.conn.execute(
                f"INSERT OR REPLACE INTO derived ({','.join(_COLUMNS)}) "
                f"VALUES ({','.join('?' for _ in _COLUMNS)})",
                _values(r),
            )
            self.conn.commit()


class PostgresDerivedStore(_BaseStore):
    """Postgres backend (optional: pip install abenlux[postgres]). For thousands of developers."""

    _ph = "%s"

    def __init__(self, dsn: str):
        import psycopg
        self.conn = psycopg.connect(dsn, autocommit=True)
        stmts = _ddl("DOUBLE PRECISION", "BIGINT", "DOUBLE PRECISION")
        self.conn.execute(stmts[0])
        self._ensure_columns(_coltypes("BIGINT", "DOUBLE PRECISION"))
        for stmt in stmts[1:]:
            self.conn.execute(stmt)

    def _existing_columns(self) -> set:
        cur = self.conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='derived'")
        return {r[0] for r in cur.fetchall()}

    def insert(self, r: DerivedRecord) -> None:
        cols = ",".join(_COLUMNS)
        ph = ",".join("%s" for _ in _COLUMNS)
        updates = ",".join(f"{c}=EXCLUDED.{c}" for c in _COLUMNS if c != "event_id")
        self._exec(
            f"INSERT INTO derived ({cols}) VALUES ({ph}) "
            f"ON CONFLICT (event_id) DO UPDATE SET {updates}",
            _values(r),
        )


def open_store(dsn: str | Path) -> _BaseStore:
    """select the backend from the DSN. postgres:// -> Postgres, otherwise SQLite (default)."""
    s = str(dsn)
    if s.startswith("postgres://") or s.startswith("postgresql://"):
        return PostgresDerivedStore(s)
    return DerivedStore(s)
