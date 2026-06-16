"""routing and team memory through the real collector ingest path, not just the modules."""
from fastapi.testclient import TestClient

from abenlux.api import server
from abenlux.schema import DerivedRecord
from abenlux.store import DerivedStore

V = [0.10, 0.20, 0.30, 0.40, 0.50]
V_FAR = [0.9, -0.4, 0.1, -0.7, 0.2]


def _rec(eid, actor, *, emb=None, lang="python", model="claude-opus-4-8", cost=1.0,
         original=None, target=None, route_saved=0.0):
    return DerivedRecord(
        event_id=eid, ts=1.0, tier="tier2_gateway", provider="anthropic",
        actor_pseudonym=actor, request_model=model, input_tokens=1000, output_tokens=100,
        duplicate_history_tokens=0, cost_usd=cost, cost_priced=True, tool="aider",
        objective_id="ObjA", objective_label="ObjA", is_orphan=False, attribution_method="ticket_join",
        embedding=emb, language=lang, original_model=original, route_target=target,
        route_saved_usd=route_saved,
    )


def _post(client, rec):
    return client.post("/v1/derived", json=[rec.to_dict()],
                       headers={"Authorization": "Bearer dev-ingest-token"})


def _fresh_collector(db, monkeypatch):
    monkeypatch.setenv("ABEN_TM", "shadow")
    monkeypatch.setattr(server, "_store", lambda: DerivedStore(db))
    server._TEAM_MEMORY = None              # a clean index per test
    return TestClient(server.app)


def test_a_teammate_reuse_is_marked_serve_and_a_different_language_is_a_warm_start(tmp_path, monkeypatch):
    db = str(tmp_path / "tm.db")
    client = _fresh_collector(db, monkeypatch)
    # alice solves the task, bob asks the same thing in the same language, carol the same in another one
    _post(client, _rec("e1", "alice", emb=V, lang="python"))
    _post(client, _rec("e2", "bob", emb=V, lang="python"))
    _post(client, _rec("e3", "carol", emb=V, lang="go"))
    _post(client, _rec("e4", "dave", emb=V_FAR, lang="python"))

    s = DerivedStore(db)
    rows = {r["event_id"]: r for r in s._rows(s._exec("SELECT event_id, tm_tier, tm_solver FROM derived", ()))}
    s.close()
    assert rows["e1"]["tm_tier"] is None            # nothing to match against yet
    assert rows["e2"]["tm_tier"] == "serve"
    assert rows["e2"]["tm_solver"] == "alice"
    assert rows["e3"]["tm_tier"] == "warm_start"
    assert rows["e4"]["tm_tier"] is None            # unrelated work


def test_team_memory_shows_up_in_the_report(tmp_path, monkeypatch):
    db = str(tmp_path / "tmrep.db")
    client = _fresh_collector(db, monkeypatch)
    for i in range(4):                              # four devs clear k, plus a serve and a warm start
        _post(client, _rec(f"p{i}", f"px_{i}", emb=V, lang="python"))
    _post(client, _rec("g0", "px_g", emb=V, lang="go"))
    rep = client.get("/api/report", headers={"Authorization": "Bearer mgr-token"}).json()
    tm = rep.get("team_memory") or {}
    assert tm.get("serve_hits", 0) >= 1
    assert tm.get("warm_starts", 0) >= 1
    assert tm.get("shadow_usd", 0) > 0


def test_collector_re_derives_routing_savings_and_ignores_a_forged_figure(tmp_path, monkeypatch):
    db = str(tmp_path / "route.db")
    monkeypatch.setattr(server, "_store", lambda: DerivedStore(db))
    client = TestClient(server.app)
    # a hostile edge claims a huge routing saving on a cheap haiku call. the collector must re-derive it.
    _post(client, _rec("r1", "px_a", model="claude-haiku-4-5", original="claude-opus-4-8",
                       target="claude-haiku-4-5", route_saved=999.0))
    s = DerivedStore(db)
    tot = s.routing_totals()
    s.close()
    assert tot["routed_calls"] == 1
    assert 0 < tot["saved_usd"] < 1.0               # the real opus-minus-haiku delta, not 999


def test_routing_in_shadow_books_a_would_save_not_a_realized_one(tmp_path, monkeypatch):
    db = str(tmp_path / "shadow.db")
    monkeypatch.setattr(server, "_store", lambda: DerivedStore(db))
    client = TestClient(server.app)
    # shadow means the call still went to opus (request_model stays opus), so it is a would-save
    _post(client, _rec("s1", "px_a", model="claude-opus-4-8", original="claude-opus-4-8",
                       target="claude-haiku-4-5"))
    s = DerivedStore(db)
    tot = s.routing_totals()
    s.close()
    assert tot["saved_usd"] == 0.0
    assert tot["shadow_usd"] > 0
