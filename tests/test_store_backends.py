"""
Both store backends are first-class: SQLite by default (zero-config), Postgres optional. These
prove the factory routing, the SQLite WAL setup, and that the Postgres adapter emits correct
upsert SQL with %s placeholders - exercised via an injected fake connection so no live Postgres or
psycopg install is needed in CI. (Integration against a real Postgres is a documented operator step.)
"""
from dataclasses import replace

import abenlux.store as store
from abenlux.schema import DerivedRecord
from abenlux.settings import SETTINGS
from abenlux.store import DerivedStore, PostgresDerivedStore, open_store


def _rec():
    return DerivedRecord(
        event_id="e1", ts=1.0, tier="t", provider="anthropic", actor_pseudonym="px",
        request_model="claude-opus-4-8", input_tokens=10, output_tokens=2,
        duplicate_history_tokens=0)


def test_open_store_defaults_to_sqlite(tmp_path):
    s = open_store(str(tmp_path / "x.db"))
    assert isinstance(s, DerivedStore)
    s.insert(_rec())
    assert s.totals()["n"] == 1
    s.close()


def test_sqlite_runs_in_wal_mode(tmp_path):
    s = DerivedStore(str(tmp_path / "wal.db"))
    mode = s.conn.execute("PRAGMA journal_mode").fetchone()[0]
    s.close()
    assert mode.lower() == "wal"   # concurrent BackgroundTask writers don't serialize


def test_open_store_routes_postgres_dsn(monkeypatch):
    seen = {}

    class FakePg:
        def __init__(self, dsn):
            seen["dsn"] = dsn

    monkeypatch.setattr(store, "PostgresDerivedStore", FakePg)
    s = store.open_store("postgresql://user@host:5432/abenlux")
    assert isinstance(s, FakePg) and seen["dsn"].startswith("postgresql://")
    s2 = store.open_store("postgres://user@host/abenlux")
    assert isinstance(s2, FakePg)


def test_placeholder_translation_per_backend():
    sqlite = DerivedStore.__new__(DerivedStore)   # no connect, just the _q logic
    pg = PostgresDerivedStore.__new__(PostgresDerivedStore)
    assert sqlite._q("WHERE a=? AND b=?") == "WHERE a=? AND b=?"
    assert pg._q("WHERE a=? AND b=?") == "WHERE a=%s AND b=%s"


def test_postgres_insert_emits_upsert_sql():
    calls = []

    class FakeCur:
        description = [("x",)]
        def fetchone(self): return (0,)
        def fetchall(self): return []

    class FakeConn:
        def execute(self, sql, params=()):
            calls.append((sql, params))
            return FakeCur()

    pg = PostgresDerivedStore.__new__(PostgresDerivedStore)
    pg.conn = FakeConn()
    pg.insert(_rec())
    sql, params = calls[-1]
    assert "INSERT INTO derived" in sql
    assert "ON CONFLICT (event_id) DO UPDATE SET" in sql   # idempotent upsert for at-least-once forward
    assert "%s" in sql and "?" not in sql
    assert len(params) == len(store._COLUMNS)


def test_multiple_ingest_tokens_accepted():
    s = replace(SETTINGS, ingest_token="primary", extra_ingest_tokens="dev-a, dev-b ,")
    assert s.ingest_tokens == {"primary", "dev-a", "dev-b"}
