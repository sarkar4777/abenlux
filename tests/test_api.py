import pytest
from fastapi.testclient import TestClient

from abenlux.api import server
from abenlux.developer.matches import MatchStore
from abenlux.schema import DerivedRecord
from abenlux.store import DerivedStore


def _rec(eid, actor, objective, cost, orphan=False, tool="aider"):
    return DerivedRecord(
        event_id=eid, ts=0.0, tier="tier2_gateway", provider="anthropic",
        actor_pseudonym=actor, request_model="claude-opus-4-8",
        input_tokens=1000, output_tokens=100, duplicate_history_tokens=50,
        cost_usd=cost, cost_priced=True, tool=tool,
        objective_id=objective, objective_label=objective, is_orphan=orphan,
        attribution_method="none" if orphan else "ticket_join",
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = str(tmp_path / "api.db")
    mdb = str(tmp_path / "m.db")
    dev = server._principals.resolve("dev-token").pseudonym
    mgr = server._principals.resolve("mgr-token").pseudonym

    s = DerivedStore(db)
    # ObjA: 6 distinct devs (>= k) so it renders, includes the dev caller's own row
    s.insert(_rec("d0", dev, "ObjA", 2.0))
    for i in range(1, 6):
        s.insert(_rec(f"d{i}", f"dev{i}", "ObjA", 1.0))
    # ObjSecret: only 2 devs (< k) -> must be suppressed in the management view
    s.insert(_rec("s0", "sx", "ObjSecret", 50.0))
    s.insert(_rec("s1", "sy", "ObjSecret", 50.0))
    s.close()

    ms = MatchStore(mdb)
    ms.record(dev, mgr, "approval workflow saga", 0.9, "live_duplication")
    ms.close()

    monkeypatch.setattr(server, "_store", lambda: DerivedStore(db))
    monkeypatch.setattr(server, "_matches", lambda: MatchStore(mdb))
    return TestClient(server.app)


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_no_token_is_401(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/report").status_code == 401


def test_developer_cannot_see_aggregates(client):
    assert client.get("/api/report", headers=_auth("dev-token")).status_code == 403
    assert client.get("/api/rollup/tool", headers=_auth("dev-token")).status_code == 403


def test_manager_sees_aggregates_but_subk_group_suppressed(client):
    r = client.get("/api/report", headers=_auth("mgr-token"))
    assert r.status_code == 200
    body = r.json()
    by_obj = {row["label"]: row for row in body["by_objective"]}
    assert by_obj["ObjA"]["suppressed"] is False
    assert by_obj["ObjSecret"]["suppressed"] is True
    assert by_obj["ObjSecret"]["cost"] == 0.0  # the $100 sub-k group is never revealed


def test_developer_me_is_scoped_to_self(client):
    r = client.get("/api/me", headers=_auth("dev-token"))
    assert r.status_code == 200
    body = r.json()
    assert body["calls"] == 1                 # only the caller's own row
    assert round(body["cost_usd"], 2) == 2.0  # not the org total
    assert "collaboration_matches" in body


def test_only_admin_manages_objectives(client):
    assert client.get("/api/objectives", headers=_auth("admin-token")).status_code == 200
    assert client.get("/api/objectives", headers=_auth("mgr-token")).status_code == 403
    assert client.get("/api/objectives", headers=_auth("dev-token")).status_code == 403


def test_whoami_reports_role_and_permissions(client):
    body = client.get("/api/whoami", headers=_auth("fin-token")).json()
    assert body["role"] == "finance"
    assert "view_aggregates" in body["permissions"]


def test_collab_double_blind_consent_reveals_only_on_mutual(client, tmp_path):
    # dev sees a match but the peer is hidden until BOTH consent
    me = client.get("/api/me", headers=_auth("dev-token")).json()
    match = me["collaboration_matches"][0]
    assert match["peer_revealed"] is None
    mid = match["id"]
    # dev consents -> still not mutual (manager hasn't)
    r = client.post(f"/api/collab/{mid}/consent", headers=_auth("dev-token")).json()
    assert r["consented"] is True and r["mutual"] is False and r["peer_revealed"] is None
    # simulate the peer (manager) consenting back, then dev sees the reveal
    dev = server._principals.resolve("dev-token").pseudonym
    mgr = server._principals.resolve("mgr-token").pseudonym
    ms = server._matches()
    ms.record_consent(mgr, dev)
    ms.close()
    me2 = client.get("/api/me", headers=_auth("dev-token")).json()
    assert me2["collaboration_matches"][0]["peer_revealed"] == "Morgan Manager"


def test_rollup_rejects_unknown_dimension(client):
    r = client.get("/api/rollup/secret_drilldown", headers=_auth("mgr-token"))
    assert r.status_code == 400


def test_drift_endpoint_gated_and_reports_trend(tmp_path, monkeypatch):
    db = str(tmp_path / "drift.db")
    s = DerivedStore(db)
    for i in range(5):  # prior window: attributed (>= k distinct devs so the window clears k-anon)
        s.insert(_rec(f"p{i}", f"d{i}", "ObjA", 1.0))
    for i in range(5):  # recent window: orphan spend up
        r = _rec(f"o{i}", f"d{i}", None, 1.0, orphan=True)
        r.ts = 1000.0 + i
        s.insert(r)
    # backdate the prior window so there are two comparable windows
    s.conn.execute("UPDATE derived SET ts=100 WHERE event_id LIKE 'p%'")
    s.conn.commit()
    s.close()
    monkeypatch.setattr(server, "_store", lambda: DerivedStore(db))
    c = TestClient(server.app)
    assert c.get("/api/drift", headers=_auth("dev-token")).status_code == 403  # developer denied
    body = c.get("/api/drift", headers=_auth("mgr-token")).json()
    assert body["trend"] is not None
    assert body["trend"]["orphan_share"]["direction"] == "up"
    assert body["trend"]["orphan_share"]["alert"] is True
