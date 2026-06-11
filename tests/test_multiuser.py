"""
Multi-user emulation of the advanced features end to end: waste suggestions, collaboration
opportunities (live duplication + solved-reuse, with Chinese-wall / residency / double-blind
guarantees), spend drift, and fleet aggregation across many developers. Nothing here is mocked
away - it drives the real monitors, broker, pipeline, store, and drift detector with a simulated
team so the behaviors are exercised exactly as they would be in production.
"""
from __future__ import annotations

from abenlux.analytics.drift import spend_trend
from abenlux.analytics.reports import management_report
from abenlux.attribution.attributor import KnowledgeGraph, Objective
from abenlux.collaborate.broker import CollaborationBroker, TopicSignal
from abenlux.developer.matches import MatchStore
from abenlux.embedding import hashing_embed
from abenlux.pipeline import process
from abenlux.privacy.pseudonymize import pseudonymize
from abenlux.processing.waste import SessionWasteMonitor
from abenlux.schema import CanonicalEvent, Message, Usage, WorkContext
from abenlux.store import DerivedStore


# --------------------------------------------------------------------------- #
# suggestions: every waste kind, with per-developer session isolation          #
# --------------------------------------------------------------------------- #
def _ev(prompt, answer="ok", inp=500, out=50):
    return CanonicalEvent(messages=[Message("user", prompt)],
                          output_messages=[Message("assistant", answer)], usage=Usage(inp, out))


def test_retry_loop_suggestion_per_user():
    mon = SessionWasteMonitor()
    mon.observe(_ev("fix the failing auth integration test"))
    sigs = mon.observe(_ev("fix the failing auth integration test!!"))
    assert any(s.kind == "retry_loop" and s.severity == "warn" for s in sigs)


def test_uncached_resend_suggests_caching():
    mon = SessionWasteMonitor()
    e = _ev("one more small thing", inp=10000, out=50)
    e.duplicate_history_tokens = 9000  # 90% resent history, none of it cached
    sigs = mon.observe(e)
    cache = [s for s in sigs if s.kind == "cache_inefficiency"]
    assert cache and cache[0].recoverable_tokens == 9000
    assert "caching" in cache[0].suggestion.lower()


def test_answered_already_suggestion():
    mon = SessionWasteMonitor()
    mon.observe(_ev("how do I configure the Temporal worker poll interval", answer="set pollInterval"))
    sigs = mon.observe(_ev("how do I configure the Temporal worker poll interval again"))
    assert any(s.kind == "answered_already" for s in sigs)


def test_routing_hint_for_trivial_call():
    mon = SessionWasteMonitor()
    sigs = mon.observe(_ev("rename var x to count", inp=80, out=20))  # tiny, no other signals
    assert any(s.kind == "routing_hint" for s in sigs)


def test_sessions_are_isolated_between_developers():
    # user A retries, user B sending the same text must NOT be flagged (separate monitors)
    mon_a, mon_b = SessionWasteMonitor(), SessionWasteMonitor()
    mon_a.observe(_ev("optimize the nightly ETL job"))
    a_sigs = mon_a.observe(_ev("optimize the nightly ETL job"))
    b_sigs = mon_b.observe(_ev("optimize the nightly ETL job"))
    assert any(s.kind == "retry_loop" for s in a_sigs)
    assert not any(s.kind == "retry_loop" for s in b_sigs)


# --------------------------------------------------------------------------- #
# collaboration: live duplication, solved-reuse, walls, double-blind           #
# --------------------------------------------------------------------------- #
def _topic(actor, text, *, client=None, residency="eu", solved=False):
    return TopicSignal(actor_pseudonym=actor, topic_embedding=hashing_embed(text),
                       topic_label=text, client=client, residency=residency, is_solved=solved)


def test_live_duplication_match_between_two_developers():
    b = CollaborationBroker(threshold=0.8)
    assert b.submit(_topic("px_alice", "temporal saga for the approval workflow")) == []
    matches = b.submit(_topic("px_bob", "temporal saga for the approval workflow"))
    assert len(matches) == 1
    m = matches[0]
    assert m.mode == "live_duplication" and m.a == "px_bob" and m.b == "px_alice"


def test_solved_reuse_when_peer_already_solved():
    b = CollaborationBroker(threshold=0.8)
    b.submit(_topic("px_expert", "idempotent webhook retries", solved=True))
    matches = b.submit(_topic("px_newbie", "idempotent webhook retries"))
    assert matches and matches[0].mode == "solved_reuse"


def test_chinese_wall_blocks_cross_client_match():
    b = CollaborationBroker(threshold=0.8)
    b.submit(_topic("px_a", "data lakehouse ingestion", client="umbrella"))
    matches = b.submit(_topic("px_b", "data lakehouse ingestion", client="initech"))
    assert matches == []  # identical topic, different clients -> never matched


def test_residency_boundary_blocks_match():
    b = CollaborationBroker(threshold=0.8)
    b.submit(_topic("px_a", "kafka exactly-once", residency="eu"))
    matches = b.submit(_topic("px_b", "kafka exactly-once", residency="us"))
    assert matches == []


def test_dissimilar_topics_do_not_match():
    b = CollaborationBroker(threshold=0.8)
    b.submit(_topic("px_a", "react table virtualization performance"))
    matches = b.submit(_topic("px_b", "postgres vacuum autovacuum tuning"))
    assert matches == []


def test_double_blind_identity_hidden_until_mutual_consent():
    b = CollaborationBroker(threshold=0.8)
    b.submit(_topic("px_a", "saga compensation strategy"))
    b.submit(_topic("px_b", "saga compensation strategy"))
    assert b.mutually_consented("px_a", "px_b") is False
    b.record_consent("px_a", "px_b")
    assert b.mutually_consented("px_a", "px_b") is False  # one-sided is not enough
    b.record_consent("px_b", "px_a")
    assert b.mutually_consented("px_a", "px_b") is True


def test_collaboration_surfaced_per_owner_via_match_store(tmp_path):
    # a live-duplication match between alice and bob is written one-row-per-owner, each sees only
    # their own side, peer pseudonymous until both consent.
    b = CollaborationBroker(threshold=0.8)
    b.submit(_topic("px_alice", "feature flag rollout safety"))
    matches = b.submit(_topic("px_bob", "feature flag rollout safety"))
    ms = MatchStore(tmp_path / "m.db")
    for m in matches:
        ms.record(m.a, m.b, m.topic, m.similarity, m.mode)
        ms.record(m.b, m.a, m.topic, m.similarity, m.mode)
    assert len(ms.for_owner("px_bob")) == 1
    assert len(ms.for_owner("px_alice")) == 1
    assert ms.for_owner("px_alice")[0]["peer"] == "px_bob"
    assert ms.mutually_consented("px_alice", "px_bob", ms.for_owner("px_alice")[0]["topic"]) is False


# --------------------------------------------------------------------------- #
# drift: rising orphan spend across a simulated fortnight                       #
# --------------------------------------------------------------------------- #
def _drow(store, eid, ts, orphan, cost=1.0, actor="dev"):
    from abenlux.schema import DerivedRecord
    store.insert(DerivedRecord(
        event_id=eid, ts=ts, tier="tier2_gateway", provider="anthropic",
        actor_pseudonym=actor, request_model="claude-opus-4-8",
        input_tokens=900, output_tokens=100, duplicate_history_tokens=0,
        cost_usd=cost, cost_priced=True,
        objective_id=None if orphan else "obj-x",
        objective_label=None if orphan else "Platform X",
        is_orphan=orphan, attribution_method="none" if orphan else "ticket_join"))


def test_orphan_spend_drift_is_flagged(tmp_path):
    store = DerivedStore(tmp_path / "d.db")
    for i in range(5):       # prior window: attributed
        _drow(store, f"p{i}", 100 + i, orphan=False, actor=f"d{i}")
    for i in range(5):       # recent window: orphan spend explodes
        _drow(store, f"r{i}", 200 + i, orphan=True, actor=f"d{i}")
    rep = spend_trend(store)
    store.close()
    assert rep is not None
    assert rep.orphan_share.prior == 0.0 and rep.orphan_share.recent == 1.0
    assert rep.orphan_share.direction == "up" and rep.orphan_share.alert is True
    assert rep.any_alert


def test_stable_spend_does_not_alert(tmp_path):
    store = DerivedStore(tmp_path / "d2.db")
    for i in range(5):
        _drow(store, f"p{i}", 100 + i, orphan=(i % 2 == 0), actor=f"d{i}")
    for i in range(5):
        _drow(store, f"r{i}", 200 + i, orphan=(i % 2 == 0), actor=f"d{i}")
    rep = spend_trend(store)
    store.close()
    assert rep is not None and rep.orphan_share.alert is False


def test_cost_drift_flagged_on_spend_spike(tmp_path):
    store = DerivedStore(tmp_path / "d3.db")
    for i in range(5):
        _drow(store, f"p{i}", 100 + i, orphan=False, cost=1.0, actor=f"d{i}")
    for i in range(5):
        _drow(store, f"r{i}", 200 + i, orphan=False, cost=4.0, actor=f"d{i}")  # 4x spend
    rep = spend_trend(store)
    store.close()
    assert rep.cost.direction == "up" and rep.cost.alert is True


def test_insufficient_history_returns_no_drift(tmp_path):
    store = DerivedStore(tmp_path / "d4.db")
    _drow(store, "only", 100.0, orphan=False)
    assert spend_trend(store) is None  # one timestamp -> nothing to compare
    store.close()


# --------------------------------------------------------------------------- #
# fleet: many developers through the real pipeline -> aggregated management view#
# --------------------------------------------------------------------------- #
def test_fleet_of_developers_aggregates_with_kanon(tmp_path):
    kg = KnowledgeGraph()
    kg.add_objective(Objective("obj-acme", "Acme Checkout platform", "client", client="acme"))
    kg.map_ticket_prefix("ACME", "obj-acme")
    store = DerivedStore(tmp_path / "fleet.db")

    # 8 developers each make a ACME-attributed call -> clears k=5, renders in the report
    for i in range(8):
        ev = CanonicalEvent(
            messages=[Message("user", f"developer {i} working on ACME approvals")],
            output_messages=[Message("assistant", "use a saga")],
            usage=Usage(1000, 100), request_model="claude-opus-4-8",
            work=WorkContext(tool="aider", git_branch="feature/ACME-1-x", ticket_id="ACME-1", repo="acme-checkout"),
        )
        ev.actor_raw = f"dev{i}@corp.com"
        res = process(ev, kg=kg, hmac_key=b"fleet-key", waste_monitor=SessionWasteMonitor())
        store.insert(res.record)
        # each pseudonym is distinct and stable
        assert res.record.actor_pseudonym == pseudonymize(f"dev{i}@corp.com", b"fleet-key")

    rep = management_report(store, k=5)
    store.close()
    by_obj = {r["label"]: r for r in rep["by_objective"]}
    assert by_obj["Acme Checkout platform"]["suppressed"] is False
    assert by_obj["Acme Checkout platform"]["actors"] == 8
    assert rep["org_actors"] == 8
    assert rep["orphan_token_share"] == 0.0  # everything attributed by ticket join
