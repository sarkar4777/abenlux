"""
Regression tests for the issues an adversarial multi-agent review surfaced. Each test pins one
confirmed bug fixed, so it cannot silently come back.
"""
from abenlux.processing.redact import redact
from abenlux.capture.diff import SessionHistoryTracker
from abenlux.developer.matches import MatchStore
from abenlux.attribution.attributor import classify_from_prompt
from abenlux.collaborate.broker import CollaborationBroker, TopicSignal
from abenlux.schema import Message
from abenlux.sink import HttpSink
from abenlux.schema import DerivedRecord


def _rec(eid: str) -> DerivedRecord:
    return DerivedRecord(event_id=eid, ts=0.0, tier="t", provider="p", actor_pseudonym="a",
                         request_model="m", input_tokens=1, output_tokens=1, duplicate_history_tokens=0)


def test_redaction_catches_hex_secrets():
    # a 32-char hex token (entropy ~3.5, under the old 4.0 gate) must now be redacted
    r = redact("the db token is deadbeefdeadbeefdeadbeefdeadbeef99 ok")
    assert "deadbeef" not in r.text and "<REDACTED" in r.text


def test_history_tracker_retains_no_raw_prompt_content():
    # the diff runs on PRE-redaction messages, so the baseline must store fingerprints, not raw text
    t = SessionHistoryTracker()
    t.duplicate_history_tokens("k", [Message("user", "NDA hunter2 super-secret-credential")])
    blob = repr(t._prev)
    assert "hunter2" not in blob and "super-secret-credential" not in blob


def test_match_store_dedups_repeated_matches(tmp_path):
    ms = MatchStore(str(tmp_path / "m.db"))
    for _ in range(4):
        ms.record("alice", "bob", "Acme - Checkout", 0.9, "live_duplication")
    assert len(ms.for_owner("alice")) == 1   # one row, refreshed - not four duplicates
    ms.close()


def test_learned_term_uses_word_boundary():
    learned = {"chore": {"test"}}   # learned: the term "test" -> chore
    # "latest" CONTAINS "test" as a substring but must not fire the learned term
    assert classify_from_prompt("ship the latest greatest build", None) == \
        classify_from_prompt("ship the latest greatest build", learned)


def test_broker_enforces_residency_wall():
    br = CollaborationBroker()
    vec = [1.0] + [0.0] * 15
    br.submit(TopicSignal("a", vec, "Same Topic", residency="eu"))
    # identical topic + objective, but a different residency region -> must NOT match
    assert br.submit(TopicSignal("b", vec, "Same Topic", residency="us")) == []


def test_ingest_rejects_malformed_without_500():
    from fastapi.testclient import TestClient

    from abenlux.api.server import app
    c = TestClient(app)
    r = c.post("/v1/derived", json=["not-a-dict", 123],
               headers={"Authorization": "Bearer dev-ingest-token"})
    assert r.status_code == 200 and r.json()["rejected"] == 2 and r.json()["ingested"] == 0


def test_sink_requeues_failed_batch_in_order_no_loss():
    state, posts = {"ok": False}, []

    def post(u, b, tok, to):
        if not state["ok"]:
            return False
        posts.append([r["event_id"] for r in b])
        return True

    s = HttpSink("https://c", "t", batch_size=2, post=post)
    s.insert(_rec("e0"))
    s.insert(_rec("e1"))             # batch of 2 -> flush -> fails -> re-queued at the front
    assert posts == []
    state["ok"] = True
    s.insert(_rec("e2"))             # now e0,e1,e2 deliver together, in order, nothing lost
    assert posts and posts[0] == ["e0", "e1", "e2"]


def test_collector_reprices_authoritatively_and_re_redacts():
    # red-team: the collector trusted caller-supplied cost_usd (poison to $9999 or under-report to $0)
    from abenlux.api.server import _harden_inbound
    poison = DerivedRecord(event_id="x", ts=0.0, tier="t", provider="anthropic", actor_pseudonym="px",
                           request_model="claude-opus-4-8", input_tokens=1, output_tokens=1,
                           duplicate_history_tokens=0, cost_usd=9999.99, cost_priced=True)
    _harden_inbound(poison)
    assert poison.cost_usd < 0.01 and poison.cost_priced   # re-derived from token facts, 9999 ignored
    stuffed = DerivedRecord(event_id="y", ts=0.0, tier="t", provider="p", actor_pseudonym="px",
                            request_model="m", input_tokens=1, output_tokens=1, duplicate_history_tokens=0,
                            repo="repo sk-ant-SECRET1234567890abcdefghij here")
    _harden_inbound(stuffed)
    assert "sk-ant-SECRET" not in (stuffed.repo or "")     # free-text re-redacted as defense in depth


def test_management_budgets_are_k_anonymity_gated(tmp_path):
    # red-team: the budgets array leaked sub-k objective spend, bypassing the k-anon gate
    from abenlux.store import DerivedStore
    from abenlux.analytics.reports import management_report
    from abenlux.attribution.attributor import KnowledgeGraph, Objective
    s = DerivedStore(str(tmp_path / "r.db"))
    solo = DerivedRecord(event_id="e1", ts=0.0, tier="t", provider="p", actor_pseudonym="only-dev",
                         request_model="m", input_tokens=10, output_tokens=10, duplicate_history_tokens=0,
                         cost_usd=5.0, objective_id="obj-x", objective_label="Solo Objective", is_orphan=False)
    s.insert(solo)
    kg = KnowledgeGraph()
    kg.add_objective(Objective("obj-x", "Solo Objective", "client", monthly_budget_usd=100))
    rep = management_report(s, k=3, kg=kg)
    b = next(x for x in rep["budgets"] if x["label"] == "Solo Objective")
    assert b["suppressed"] and b["spent_usd"] is None      # one developer -> spend hidden in budgets too
    s.close()


def test_classifier_does_not_cache_a_transient_failure():
    from abenlux import worktype_llm
    worktype_llm._CACHE.clear()

    class Flaky:
        n = 0

        def classify(self, text):
            Flaky.n += 1
            return None if Flaky.n == 1 else "feature"

    clf = Flaky()
    assert worktype_llm._cached(clf, "phrase") is None        # transient failure, NOT cached
    assert worktype_llm._cached(clf, "phrase") == "feature"   # retried on the next call
