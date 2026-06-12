"""
Deep end-to-end collaboration: two developers on two machines forward content-free records to the
collector, which matches them double-blind, surfaces a hidden match to each, and reveals identities
only after BOTH request an intro. Plus the broker's latest-signal/bounding behavior, the Chinese
wall, and owner scoping. Multi-turn, driven through the real collector API.
"""
import pytest
from fastapi.testclient import TestClient

from abenlux.api import server
from abenlux.attribution.attributor import KnowledgeGraph, Objective
from abenlux.collaborate.broker import CollaborationBroker, TopicSignal
from abenlux.developer.matches import MatchStore
from abenlux.schema import DerivedRecord
from abenlux.store import DerivedStore

EMB = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]  # identical embedding -> cosine 1.0, a clear match


def _rec(actor, objective="obj-x", label="Acme - Checkout Platform", embedding=EMB):
    return DerivedRecord(
        event_id=f"{actor}:{objective}", ts=1.0, tier="tier2_gateway", provider="anthropic",
        actor_pseudonym=actor, request_model="claude-opus-4-8",
        input_tokens=1000, output_tokens=100, duplicate_history_tokens=0,
        cost_usd=1.0, cost_priced=True, embedding=embedding,
        objective_id=objective, objective_label=label, is_orphan=False, attribution_method="ticket_join")


@pytest.fixture
def collector(tmp_path, monkeypatch):
    db, mdb = str(tmp_path / "c.db"), str(tmp_path / "m.db")
    monkeypatch.setattr(server, "_store", lambda: DerivedStore(db))
    monkeypatch.setattr(server, "_matches", lambda: MatchStore(mdb))
    monkeypatch.setattr(server, "_broker", CollaborationBroker(threshold=0.8))
    return TestClient(server.app), db, mdb


def _ingest(client, rec):
    return client.post("/v1/derived", json=rec.to_dict(),
                       headers={"Authorization": "Bearer dev-ingest-token"})


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_collector_matches_two_developers_double_blind(collector):
    client, db, mdb = collector
    dev = server._principals.resolve("dev-token").pseudonym
    fin = server._principals.resolve("fin-token").pseudonym

    # turn 1: developer A forwards a record. no peer yet -> no match.
    assert _ingest(client, _rec(dev)).json()["ingested"] == 1
    # turn 2: developer B forwards a record on the same topic -> the collector matches them.
    assert _ingest(client, _rec(fin)).json()["ingested"] == 1

    # both see a match, peer hidden, BEFORE anyone consents
    me_dev = client.get("/api/me", headers=_auth("dev-token")).json()
    me_fin = client.get("/api/me", headers=_auth("fin-token")).json()
    assert len(me_dev["collaboration_matches"]) == 1
    assert len(me_fin["collaboration_matches"]) == 1
    assert me_dev["collaboration_matches"][0]["peer_revealed"] is None
    assert me_fin["collaboration_matches"][0]["peer_revealed"] is None
    dev_match = me_dev["collaboration_matches"][0]["id"]
    fin_match = me_fin["collaboration_matches"][0]["id"]

    # turn 3: A requests an intro -> not mutual yet, still hidden for everyone
    r = client.post(f"/api/collab/{dev_match}/consent", headers=_auth("dev-token")).json()
    assert r["consented"] is True and r["mutual"] is False and r["peer_revealed"] is None
    assert client.get("/api/me", headers=_auth("dev-token")).json()["collaboration_matches"][0]["peer_revealed"] is None

    # turn 4: B requests an intro back -> mutual consent, identities revealed to BOTH
    r2 = client.post(f"/api/collab/{fin_match}/consent", headers=_auth("fin-token")).json()
    assert r2["mutual"] is True and r2["peer_revealed"] == "Dev Developer"
    dev_view = client.get("/api/me", headers=_auth("dev-token")).json()["collaboration_matches"][0]
    assert dev_view["peer_revealed"] == "Finn Finance"
    # contact handles are exchanged so they can actually reach each other (Slack/email)
    assert dev_view["peer_contact"]["slack"] == "@finn"
    assert dev_view["peer_contact"]["email"] == "finance@example.com"


def test_contact_card_set_and_revealed_only_on_mutual_consent(collector, monkeypatch):
    client, db, mdb = collector
    cdb = mdb.replace("m.db", "contacts.db")
    monkeypatch.setattr(server, "_contacts", lambda: __import__("abenlux.developer.contacts", fromlist=["ContactStore"]).ContactStore(cdb))
    dev = server._principals.resolve("dev-token").pseudonym
    fin = server._principals.resolve("fin-token").pseudonym

    # finance sets a custom contact card (overrides the static one)
    r = client.post("/api/contact", json={"slack": "@finn-custom", "teams": "Finn F", "junk": "x"},
                    headers=_auth("fin-token")).json()
    assert r["contact"]["slack"] == "@finn-custom" and "junk" not in r["contact"]

    _ingest(client, _rec(dev))
    _ingest(client, _rec(fin))
    dev_id = client.get("/api/me", headers=_auth("dev-token")).json()["collaboration_matches"][0]["id"]
    fin_id = client.get("/api/me", headers=_auth("fin-token")).json()["collaboration_matches"][0]["id"]

    # before mutual consent: no contact leaks
    me_dev = client.get("/api/me", headers=_auth("dev-token")).json()["collaboration_matches"][0]
    assert me_dev["peer_contact"] is None

    client.post(f"/api/collab/{dev_id}/consent", headers=_auth("dev-token"))
    client.post(f"/api/collab/{fin_id}/consent", headers=_auth("fin-token"))
    # after mutual: dev sees finance's CUSTOM handle
    me_dev = client.get("/api/me", headers=_auth("dev-token")).json()["collaboration_matches"][0]
    assert me_dev["peer_contact"]["slack"] == "@finn-custom"
    assert me_dev["peer_contact"]["teams"] == "Finn F"


def test_collab_is_owner_scoped(collector):
    client, _, _ = collector
    dev = server._principals.resolve("dev-token").pseudonym
    fin = server._principals.resolve("fin-token").pseudonym
    _ingest(client, _rec(dev))
    _ingest(client, _rec(fin))
    # the developer view returns ONLY the caller's own match rows, never the peer's side
    me_dev = client.get("/api/me", headers=_auth("dev-token")).json()
    assert all(m.get("peer_revealed") is None for m in me_dev["collaboration_matches"])
    # a manager has no collaboration matches of their own here, and cannot see others'
    me_mgr = client.get("/api/me", headers=_auth("mgr-token")).json()
    assert me_mgr["collaboration_matches"] == []


def test_collector_respects_chinese_wall(tmp_path, monkeypatch):
    db, mdb = str(tmp_path / "c.db"), str(tmp_path / "m.db")
    kg = KnowledgeGraph()
    kg.add_objective(Objective("obj-a", "Acme platform", client="acme"))
    kg.add_objective(Objective("obj-b", "Globex platform", client="globex"))
    monkeypatch.setattr(server, "_store", lambda: DerivedStore(db))
    monkeypatch.setattr(server, "_matches", lambda: MatchStore(mdb))
    monkeypatch.setattr(server, "_broker", CollaborationBroker(threshold=0.8))
    monkeypatch.setattr(server, "_kg", kg)
    client = TestClient(server.app)
    dev = server._principals.resolve("dev-token").pseudonym
    fin = server._principals.resolve("fin-token").pseudonym
    # identical topic embedding but different CLIENTS -> the wall blocks the match
    _ingest(client, _rec(dev, objective="obj-a", label="Acme platform"))
    _ingest(client, _rec(fin, objective="obj-b", label="Globex platform"))
    assert client.get("/api/me", headers=_auth("dev-token")).json()["collaboration_matches"] == []


# --------------------------------------------------------------------------- #
# broker internals: latest-signal-per-actor and memory bound                   #
# --------------------------------------------------------------------------- #
def test_broker_keeps_only_latest_signal_per_actor():
    b = CollaborationBroker(threshold=0.8)
    b.submit(TopicSignal("alice", EMB, "topic-1"))
    b.submit(TopicSignal("alice", EMB, "topic-2"))   # alice re-submits, replaces her prior signal
    assert sum(1 for s in b.signals if s.actor_pseudonym == "alice") == 1
    assert b.signals[-1].topic_label == "topic-2"


def test_broker_bounds_memory():
    b = CollaborationBroker(threshold=0.99, max_signals=10)
    for i in range(50):
        b.submit(TopicSignal(f"dev{i}", [float(i)] * 4, f"t{i}"))
    assert len(b.signals) <= 10


def test_solved_reuse_mode_at_collector(collector):
    # mark one side solved by setting is_solved via a second broker submit path is internal,
    # so verify the broker directly produces solved_reuse and the collector path matches generally
    b = CollaborationBroker(threshold=0.8)
    b.submit(TopicSignal("expert", EMB, "idempotent retries", is_solved=True))
    out = b.submit(TopicSignal("newbie", EMB, "idempotent retries"))
    assert out and out[0].mode == "solved_reuse"


def test_chinese_wall_blocks_client_vs_no_client_signal():
    # a client-tagged signal must not match a signal with no client tag (it could be any client's work)
    b = CollaborationBroker(threshold=0.5)
    b.submit(TopicSignal("alice", EMB, "topic", client="acme", org="o", residency="eu"))
    matches = b.submit(TopicSignal("bob", EMB, "topic", client=None, org="o", residency="eu"))
    assert matches == []


def test_semantic_threshold_out_of_range_is_ignored(tmp_path):
    # a threshold of 0 would disguise all orphan spend as attributed; the loader must reject it
    p = tmp_path / "kg.yaml"
    p.write_text("objectives:\n  - {id: o1, label: One}\nsemantic_threshold: 0\n", encoding="utf-8")
    kg = KnowledgeGraph.from_yaml(str(p))
    assert kg.semantic_threshold == 0.55     # kept the safe default, ignored the invalid 0


def test_capsule_store_records_content_free_solution_facts(tmp_path):
    from abenlux.developer.capsules import CapsuleStore, cost_band
    assert cost_band(0) == "unknown" and cost_band(0.4) == "under $1" and cost_band(9) == "$5 to $20"
    cs = CapsuleStore(tmp_path / "cap.db")
    facts = cs.record_solved("px-alice", "Checkout retry", work_type="feature",
                             model="claude-haiku-4-5", tool="claude-code", retry_loops=2, usd=3.0)
    assert facts["cost_band"] == "$1 to $5" and facts["model"] == "claude-haiku-4-5"
    got = cs.get("px-alice", "Checkout retry")
    assert got == facts and cs.get("px-bob", "Checkout retry") is None   # keyed per solver + topic
    cs.close()


def test_relay_async_thread_is_double_blind_and_participant_scoped(tmp_path):
    from abenlux.developer.relay import RelayStore
    r = RelayStore(tmp_path / "relay.db")
    tid = r.ask("px-alice", "px-bob", "Checkout retry", "how did you key the idempotency token?")
    assert r.reply(tid, "px-bob", "on the order id") is True
    assert r.reply(tid, "px-eve", "i am not in this thread") is False    # only a participant can reply
    alice = r.for_participant("px-alice")[0]
    assert alice["messages"][0]["mine"] is True and alice["messages"][1]["mine"] is False
    assert "peer" in alice                                               # store returns peer, API strips it
    assert r.for_participant("px-eve") == []                            # outsiders see nothing
    r.close()
