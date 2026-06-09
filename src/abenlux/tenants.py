"""
Tenant registry. A tenant is an org unit or geography (acme-eu, acme-us, acme-apac) that belongs to
an org. Tenants of one org are the unit the benchmark compares - "how is our US region doing versus
EU on cache efficiency, net-new share, reuse" - so a tenant carries an org and a residency region.

This registry is deliberately tiny and content-free: it maps tenant_id -> {org, display_name,
residency}. The edge stamps tenant_id on every derived record (settings.tenant_id), and this registry
is what lets the collector group a tenant's records, scope a report to one tenant, and assemble the
set of tenants in an org for the cross-tenant benchmark cohort. No spend, no identities live here.

Same two-backend story as the derived store (sqlite default, postgres for scale), same open by DSN.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    org: str
    display_name: str
    residency: str = "eu"
    created_ts: float = 0.0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class TenantStore:
    """registry of tenants. sqlite by default, postgres via a postgresql:// dsn."""

    _ph = "?"

    def __init__(self, path: str | Path = "abenlux-tenants.db"):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS tenants ("
            " tenant_id TEXT PRIMARY KEY, org TEXT, display_name TEXT,"
            " residency TEXT, created_ts REAL)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_tenant_org ON tenants(org)")
        self.conn.commit()
        self._lock = threading.RLock()

    def upsert(self, t: Tenant) -> Tenant:
        # creating a tenant is idempotent on tenant_id - re-running the same create updates its metadata.
        # tenant_id is a GLOBAL key (the derived table scopes on tenant_id alone, no org column), so an
        # id, once owned by an org, can never be reassigned to another org here - otherwise a second org
        # could re-create a rival's tenant_id, flip its org, and read its reports through the org gate.
        with self._lock:
            existing = self.get(t.tenant_id)
            if existing is not None and existing.org != t.org:
                raise ValueError(
                    f"tenant_id {t.tenant_id!r} already belongs to org {existing.org!r}")
            self.conn.execute(
                "INSERT OR REPLACE INTO tenants (tenant_id, org, display_name, residency, created_ts) "
                "VALUES (?,?,?,?,?)",
                (t.tenant_id, t.org, t.display_name, t.residency, t.created_ts),
            )
            self.conn.commit()
        return t

    def get(self, tenant_id: str) -> Tenant | None:
        row = self.conn.execute(
            "SELECT tenant_id, org, display_name, residency, created_ts FROM tenants WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        return Tenant(*row) if row else None

    def list(self, org: str | None = None) -> list[Tenant]:
        if org is None:
            rows = self.conn.execute(
                "SELECT tenant_id, org, display_name, residency, created_ts FROM tenants "
                "ORDER BY org, tenant_id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT tenant_id, org, display_name, residency, created_ts FROM tenants "
                "WHERE org=? ORDER BY tenant_id",
                (org,),
            ).fetchall()
        return [Tenant(*r) for r in rows]

    def org_of(self, tenant_id: str) -> str | None:
        t = self.get(tenant_id)
        return t.org if t else None

    def close(self) -> None:
        self.conn.close()


class PostgresTenantStore(TenantStore):
    """Postgres backend (optional: pip install abenlux[postgres]). For an org running the collector
    at scale, the tenant registry lives next to the derived table."""

    _ph = "%s"

    def __init__(self, dsn: str):
        import psycopg
        self.conn = psycopg.connect(dsn, autocommit=True)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS tenants ("
            " tenant_id TEXT PRIMARY KEY, org TEXT, display_name TEXT,"
            " residency TEXT, created_ts DOUBLE PRECISION)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_tenant_org ON tenants(org)")
        self._lock = threading.RLock()

    def upsert(self, t: Tenant) -> Tenant:
        with self._lock:
            existing = self.get(t.tenant_id)
            if existing is not None and existing.org != t.org:
                raise ValueError(
                    f"tenant_id {t.tenant_id!r} already belongs to org {existing.org!r}")
            # the WHERE clause is a second, atomic guard against a concurrent cross-org reassignment:
            # the conflict update only ever fires when the owning org is unchanged.
            self.conn.execute(
                "INSERT INTO tenants (tenant_id, org, display_name, residency, created_ts) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (tenant_id) DO UPDATE SET "
                "display_name=EXCLUDED.display_name, residency=EXCLUDED.residency "
                "WHERE tenants.org=EXCLUDED.org",
                (t.tenant_id, t.org, t.display_name, t.residency, t.created_ts),
            )
        return t

    def get(self, tenant_id: str) -> Tenant | None:
        row = self.conn.execute(
            "SELECT tenant_id, org, display_name, residency, created_ts FROM tenants WHERE tenant_id=%s",
            (tenant_id,),
        ).fetchone()
        return Tenant(*row) if row else None

    def list(self, org: str | None = None) -> list[Tenant]:
        if org is None:
            rows = self.conn.execute(
                "SELECT tenant_id, org, display_name, residency, created_ts FROM tenants "
                "ORDER BY org, tenant_id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT tenant_id, org, display_name, residency, created_ts FROM tenants "
                "WHERE org=%s ORDER BY tenant_id",
                (org,),
            ).fetchall()
        return [Tenant(*r) for r in rows]


def open_tenant_store(dsn: str | Path) -> TenantStore:
    """select the backend from the dsn. postgres support mirrors the derived store - the demo and
    pilots run on sqlite with zero config."""
    s = str(dsn)
    if s.startswith("postgres://") or s.startswith("postgresql://"):
        return PostgresTenantStore(s)
    return TenantStore(s)
