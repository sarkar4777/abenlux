"""
Edge -> central forwarding: the topology that makes the privacy model hold at org scale. The
edge agent derives on-device and ships only content-free DerivedRecords to the collector. These
tests prove the sink selection, the forward payload, the central ingest auth, and a full loop.
"""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from abenlux.api import server
from abenlux.schema import DerivedRecord
from abenlux.sink import HttpSink, SqliteSink, build_sink
from abenlux.store import DerivedStore


def _rec(eid="e1", actor="px_a", objective="ObjA", cost=1.23):
    return DerivedRecord(
        event_id=eid, ts=1.0, tier="tier2_gateway", provider="anthropic",
        actor_pseudonym=actor, request_model="claude-opus-4-8",
        input_tokens=1000, output_tokens=100, duplicate_history_tokens=0,
        cost_usd=cost, cost_priced=True, tool="aider",
        objective_id=objective, objective_label=objective, is_orphan=False,
        attribution_method="ticket_join",
    )


def test_sink_selection_local_vs_forward(tmp_path):
    store = DerivedStore(tmp_path / "s.db")
    local = build_sink(SimpleNamespace(collector_url=None, ingest_token="t"), local_store=store)
    assert isinstance(local, SqliteSink)
    fwd = build_sink(SimpleNamespace(collector_url="https://collector", ingest_token="t"), local_store=store)
    assert isinstance(fwd, HttpSink)
    assert fwd.endpoint == "https://collector/v1/derived"


def test_http_sink_batches_and_payload_is_content_free(monkeypatch):
    sent = {}

    def capture_post(url, batch, token, timeout):
        sent.update(url=url, batch=batch, token=token)
        return True

    # batch_size=1 forces an immediate flush so the test is deterministic
    sink = HttpSink("https://collector", "device-token", batch_size=1, post=capture_post)
    sink.insert(_rec())
    assert sent["url"].endswith("/v1/derived")
    assert sent["token"] == "device-token"
    assert isinstance(sent["batch"], list) and len(sent["batch"]) == 1
    rec = sent["batch"][0]
    assert "messages" not in rec and "content" not in rec   # only derived fields cross
    assert rec["actor_pseudonym"] == "px_a"


def test_http_sink_batches_multiple_records_into_one_post():
    posts = []
    sink = HttpSink("https://c", "t", batch_size=3, post=lambda u, b, tok, to: posts.append(b) or True)
    for i in range(3):
        sink.insert(_rec(eid=f"e{i}"))
    assert len(posts) == 1 and len(posts[0]) == 3   # 3 records, 1 HTTP call


def test_http_sink_spools_on_outage_then_retries(monkeypatch):
    state = {"up": False}
    posts = []

    def post(u, b, tok, to):
        if not state["up"]:
            raise ConnectionError("collector down")
        posts.append(b)
        return True

    sink = HttpSink("https://c", "t", batch_size=1, post=post)
    sink.insert(_rec(eid="e0"))     # collector down -> spooled, no raise
    assert posts == []
    state["up"] = True
    sink.flush()                    # recovers and delivers the spooled record
    assert len(posts) == 1 and posts[0][0]["event_id"] == "e0"


def test_http_sink_never_raises_on_outage():
    sink = HttpSink("https://c", "t", batch_size=1, post=lambda *a: (_ for _ in ()).throw(ConnectionError()))
    sink.insert(_rec())  # must not raise


def test_central_ingest_requires_token_and_stores(tmp_path, monkeypatch):
    db = str(tmp_path / "central.db")
    monkeypatch.setattr(server, "_store", lambda: DerivedStore(db))
    client = TestClient(server.app)  # default ingest token is "dev-ingest-token"

    rec = _rec().to_dict()
    assert client.post("/v1/derived", json=rec).status_code == 401  # no token
    bad = client.post("/v1/derived", json=rec, headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 401
    ok = client.post("/v1/derived", json=rec, headers={"Authorization": "Bearer dev-ingest-token"})
    assert ok.status_code == 200 and ok.json()["ingested"] == 1

    s = DerivedStore(db)
    assert s.totals()["n"] == 1
    s.close()


def test_central_ingest_strips_unknown_smuggled_fields(tmp_path, monkeypatch):
    db = str(tmp_path / "central2.db")
    monkeypatch.setattr(server, "_store", lambda: DerivedStore(db))
    client = TestClient(server.app)
    rec = _rec().to_dict()
    rec["messages"] = [{"role": "user", "content": "this must never persist"}]  # smuggling attempt
    r = client.post("/v1/derived", json=rec, headers={"Authorization": "Bearer dev-ingest-token"})
    assert r.status_code == 200
    raw = open(db, "rb").read()
    assert b"this must never persist" not in raw  # rejected at the schema boundary


def test_full_edge_to_collector_loop(tmp_path, monkeypatch):
    # the edge HttpSink's httpx.post is routed to the in-process collector, a manager then sees it
    db = str(tmp_path / "loop.db")
    monkeypatch.setattr(server, "_store", lambda: DerivedStore(db))
    collector = TestClient(server.app)

    def post(url, batch, token, timeout):
        r = collector.post("/v1/derived", json=batch, headers={"Authorization": f"Bearer {token}"})
        return r.status_code < 300

    sink = HttpSink("http://collector", "dev-ingest-token", batch_size=1, post=post)
    for i in range(5):  # five distinct devs so the objective clears k=5
        sink.insert(_rec(eid=f"e{i}", actor=f"px_{i}"))
    sink.flush()

    rep = collector.get("/api/report", headers={"Authorization": "Bearer mgr-token"}).json()
    by_obj = {r["label"]: r for r in rep["by_objective"]}
    assert by_obj["ObjA"]["suppressed"] is False
    # the collector re-prices authoritatively from the (content-free) token facts and IGNORES the edge's
    # claimed cost_usd of 1.23 - opus 1000 input + 100 output is $0.0075/record, not $1.23
    assert round(by_obj["ObjA"]["cost"], 4) == round(0.0075 * 5, 4)
