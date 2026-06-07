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
from pathlib import Path

from abenlux.schema import DerivedRecord

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
          attribution_method TEXT, attribution_confidence {real_type}
        )""",
        "CREATE INDEX IF NOT EXISTS idx_obj ON derived(objective_id)",
        "CREATE INDEX IF NOT EXISTS idx_actor ON derived(actor_pseudonym)",
        "CREATE INDEX IF NOT EXISTS idx_tool ON derived(tool)",
        "CREATE INDEX IF NOT EXISTS idx_ts ON derived(ts)",
    ]


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
    )


class _BaseStore:
    """shared query logic. subclasses provide a DB-API connection, a placeholder, and an upsert."""

    _ph = "?"

    def _q(self, sql: str) -> str:
        return sql if self._ph == "?" else sql.replace("?", self._ph)

    def _exec(self, sql: str, params: tuple = ()):
        return self.conn.execute(self._q(sql), params)

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
    def orphan_token_share(self) -> float:
        cur = self._exec(
            "SELECT COALESCE(SUM(input_tokens+output_tokens),0), "
            "COALESCE(SUM(CASE WHEN is_orphan=1 THEN input_tokens+output_tokens ELSE 0 END),0) FROM derived"
        )
        total, orphan = cur.fetchone()
        return (orphan / total) if total else 0.0

    def totals(self) -> dict:
        return self._row(self._exec(
            "SELECT COUNT(*) n, COALESCE(SUM(input_tokens+output_tokens),0) tokens, "
            "COALESCE(SUM(cost_usd),0) cost, COUNT(DISTINCT actor_pseudonym) actors, "
            "COALESCE(SUM(duplicate_history_tokens),0) dup_tokens, "
            "COALESCE(SUM(CASE WHEN is_retry_loop=1 THEN 1 ELSE 0 END),0) retries, "
            "COALESCE(SUM(CASE WHEN cost_priced=0 THEN 1 ELSE 0 END),0) unpriced "
            "FROM derived"
        ))

    def rollup(self, dimension: str) -> list[dict]:
        allowed = {
            "objective": "objective_label", "tool": "tool", "model": "request_model",
            "provider": "provider", "tier": "tier",
        }
        if dimension not in allowed:
            raise ValueError(f"unknown rollup dimension {dimension!r}")
        col = allowed[dimension]
        return self._rows(self._exec(
            f"SELECT COALESCE({col},'(unattributed)') AS label, COUNT(*) AS calls, "
            "COALESCE(SUM(input_tokens+output_tokens),0) AS tokens, "
            "COALESCE(SUM(cost_usd),0) AS cost, COUNT(DISTINCT actor_pseudonym) AS actors "
            "FROM derived GROUP BY label ORDER BY cost DESC"
        ))

    def time_bounds(self) -> tuple[float, float]:
        row = self._exec("SELECT MIN(ts), MAX(ts) FROM derived").fetchone()
        return (row[0] or 0.0, row[1] or 0.0)

    def window_stats(self, start_ts: float, end_ts: float) -> dict:
        d = self._row(self._exec(
            "SELECT COUNT(*) events, COUNT(DISTINCT actor_pseudonym) actors, "
            "COALESCE(SUM(input_tokens+output_tokens),0) tokens, COALESCE(SUM(cost_usd),0) cost, "
            "COALESCE(SUM(CASE WHEN is_orphan=1 THEN input_tokens+output_tokens ELSE 0 END),0) orphan_tokens "
            "FROM derived WHERE ts >= ? AND ts < ?",
            (start_ts, end_ts),
        ))
        d["orphan_share"] = (d["orphan_tokens"] / d["tokens"]) if d["tokens"] else 0.0
        return d

    def objective_window_cost(self, objective_id: str, start_ts: float, end_ts: float) -> float:
        cur = self._exec(
            "SELECT COALESCE(SUM(cost_usd),0) FROM derived WHERE objective_id=? AND ts >= ? AND ts < ?",
            (objective_id, start_ts, end_ts),
        )
        return cur.fetchone()[0] or 0.0

    def actor_summary(self, actor_pseudonym: str) -> dict:
        """one developer's OWN view. private to them, never a management surface."""
        return self._row(self._exec(
            "SELECT COUNT(*) calls, COALESCE(SUM(input_tokens+output_tokens),0) tokens, "
            "COALESCE(SUM(cost_usd),0) cost, COALESCE(SUM(duplicate_history_tokens),0) dup_tokens, "
            "COALESCE(SUM(CASE WHEN is_retry_loop=1 THEN 1 ELSE 0 END),0) retries "
            "FROM derived WHERE actor_pseudonym=?",
            (actor_pseudonym,),
        ))

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
        for stmt in _ddl("REAL", "INTEGER", "REAL"):
            self.conn.execute(stmt)
        self.conn.commit()

    def insert(self, r: DerivedRecord) -> None:
        self._exec(
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
        for stmt in _ddl("DOUBLE PRECISION", "BIGINT", "DOUBLE PRECISION"):
            self.conn.execute(stmt)

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
