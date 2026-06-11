"""Wave E flagship gaps: the finance cost-export endpoint, the developer per-call drill-down, and the
by-provider rollup for invoice reconciliation."""
import time

from fastapi.testclient import TestClient

from abenlux.analytics.reports import management_report
from abenlux.api import server
from abenlux.schema import DerivedRecord
from abenlux.store import DerivedStore


def _rec(eid, actor, obj="ObjA", cost=1.0, provider="anthropic", wt="feature", ts=None):
    return DerivedRecord(
        event_id=eid, ts=ts if ts is not None else time.time(), tier="t", provider=provider,
        actor_pseudonym=actor, request_model="claude-opus-4-8", input_tokens=1000, output_tokens=100,
        duplicate_history_tokens=0, cost_usd=cost, cost_priced=True, tool="aider",
        objective_id=obj, objective_label=obj, is_orphan=False, work_type=wt)


def test_recent_records_is_pseudonym_scoped_and_ordered(tmp_path):
    st = DerivedStore(tmp_path / "s.db")
    st.insert(_rec("e0", "me", cost=1.0))
    st.insert(_rec("e1", "me", cost=9.0))
    st.insert(_rec("e2", "other", cost=5.0))
    by_cost = st.recent_records("me", 10, order="cost")
    assert [r["cost_usd"] for r in by_cost] == [9.0, 1.0]          # only my rows, most expensive first
    assert all(r["objective"] == "ObjA" for r in by_cost)
    st.close()


def test_actor_summary_today_window(tmp_path):
    st = DerivedStore(tmp_path / "s.db")
    st.insert(_rec("old", "me", cost=4.0, ts=1000.0))             # ancient
    st.insert(_rec("new", "me", cost=2.0))                        # now
    today = st.actor_summary("me", start_ts=time.time() - 3600)   # last hour
    assert round(today["cost"], 2) == 2.0                         # excludes the ancient record
    allt = st.actor_summary("me")
    assert round(allt["cost"], 2) == 6.0
    st.close()


def test_report_has_by_provider(tmp_path):
    st = DerivedStore(tmp_path / "s.db")
    for i in range(5):
        st.insert(_rec(f"a{i}", f"a{i}", provider="anthropic", cost=2.0))
    for i in range(5):
        st.insert(_rec(f"o{i}", f"o{i}", provider="openai", cost=1.0))
    rep = management_report(st, k=5)
    provs = {r["label"]: r for r in rep["by_provider"]}
    assert "anthropic" in provs and "openai" in provs
    st.close()


def _wire(monkeypatch, tmp_path):
    db = str(tmp_path / "c.db")
    monkeypatch.setattr(server, "_store", lambda: DerivedStore(db))
    return db


def test_export_endpoint_rbac_and_k_gate(tmp_path, monkeypatch):
    db = _wire(monkeypatch, tmp_path)
    st = DerivedStore(db)
    for i in range(5):                       # ObjA clears k=5
        st.insert(_rec(f"a{i}", f"a{i}", obj="ObjA", cost=2.0))
    for i in range(2):                       # ObjTiny is sub-k
        st.insert(_rec(f"t{i}", f"t{i}", obj="ObjTiny", cost=3.0))
    st.close()
    c = TestClient(server.app)
    # finance can export; developer cannot (no VIEW_COST)
    assert c.get("/api/export?dimension=objective", headers={"Authorization": "Bearer dev-token"}).status_code == 403
    csv_resp = c.get("/api/export?dimension=objective&format=csv", headers={"Authorization": "Bearer fin-token"})
    assert csv_resp.status_code == 200 and "text/csv" in csv_resp.headers["content-type"]
    body = csv_resp.text
    assert "ObjA" in body and "ObjTiny" not in body            # sub-k group omitted from the export
    j = c.get("/api/export?dimension=objective&format=json", headers={"Authorization": "Bearer fin-token"}).json()
    assert any(r["label"] == "ObjA" for r in j["rows"]) and all(r["label"] != "ObjTiny" for r in j["rows"])
    # an unknown dimension is a clean 400
    assert c.get("/api/export?dimension=secret", headers={"Authorization": "Bearer fin-token"}).status_code == 400
