"""
Deep, realistic simulation: a team of developers running many long, varied prompts through the
REAL edge pipeline. Exercises work-type/purpose classification (branch + prompt fallback), all four
waste patterns, collaboration matching, the investment/new-initiative reports, budgets, drift,
k-anonymity, and the developer-private view - end to end, nothing fabricated.
"""
from __future__ import annotations

from abenlux.analytics.budget import current_month_bounds
from abenlux.analytics.reports import developer_report, management_report
from abenlux.attribution.attributor import (
    KnowledgeGraph,
    Objective,
    classify_from_prompt,
    work_type_and_source,
)
from abenlux.collaborate.broker import CollaborationBroker, TopicSignal
from abenlux.embedding import hashing_embed
from abenlux.pipeline import process
from abenlux.privacy.pseudonymize import pseudonymize
from abenlux.processing.waste import SessionWasteMonitor
from abenlux.schema import CanonicalEvent, Message, Usage, WorkContext
from abenlux.store import DerivedStore

HMAC = b"deep-key"

# long, realistic prompts per intent - no branch, so the prompt-pattern classifier must catch them
PROMPTS = {
    "fix": [
        "The checkout service throws a NullPointerException when the cart is empty and the user "
        "applies a coupon. Here is the stack trace from production. It is failing intermittently "
        "and breaking the order flow. Please help me fix the root cause, not just the symptom.",
        "Our nightly ETL job has been failing for three days with a deadlock error on the orders "
        "table. The bug seems to be a race condition between the loader and the reconciler.",
    ],
    "feature": [
        "I need to add a new GraphQL endpoint that returns a customer's full order history with "
        "pagination and filtering by date range. Build the resolver, wire it into the schema, and "
        "create the new service method. This is a brand new capability for the mobile app.",
        "Implement a new webhook delivery system that supports retries with exponential backoff "
        "and idempotency keys, and add a new admin page to inspect delivery status.",
    ],
    "refactor": [
        "This 800-line controller has grown unmanageable. Refactor it: extract the validation into "
        "a separate class, rename the confusing variables, and simplify the nested conditionals "
        "without changing behavior.",
    ],
    "perf": [
        "This dashboard aggregation query is way too slow, it takes 12 seconds. Help me optimize it, "
        "the latency is killing the user experience and there is an obvious N+1 bottleneck.",
    ],
    "test": [
        "Write comprehensive unit tests for the payment refund service, covering partial refunds, "
        "double-refund protection, and currency rounding. Add integration tests with a mock gateway.",
    ],
    "exploration": [
        "How should I architect a saga with compensation for the multi-step approval workflow? "
        "Compare the trade-offs between an orchestration approach and a choreography approach, and "
        "evaluate which library would be the best fit for our stack.",
    ],
}


def _kg():
    kg = KnowledgeGraph()
    kg.add_objective(Objective("obj-shop", "Acme - Checkout Platform", "client", client="acme", monthly_budget_usd=4000))
    kg.add_objective(Objective("obj-data", "Initech - Data Platform", "client", client="initech", monthly_budget_usd=6000))
    kg.add_objective(Objective("obj-mobile", "Zenith - New Mobile App", "innovation", monthly_budget_usd=2000))
    kg.map_ticket_prefix("SHOP", "obj-shop")
    kg.map_ticket_prefix("DATA", "obj-data")
    kg.map_ticket_prefix("MOB", "obj-mobile")
    kg.map_repo("acme-checkout", "obj-shop")
    return kg


def _event(actor, prompt, *, branch=None, ticket=None, repo=None, model="claude-opus-4-8",
           inp=120_000, out=12_000, cache=80_000, ts=None, answer="done", tool="aider"):
    ev = CanonicalEvent(
        messages=[Message("user", prompt)], output_messages=[Message("assistant", answer)],
        usage=Usage(input_tokens=inp, output_tokens=out, cache_read_tokens=cache),
        request_model=model,
        work=WorkContext(tool=tool, git_branch=branch, ticket_id=ticket, repo=repo),
    )
    ev.actor_raw = actor
    if ts is not None:
        ev.ts = ts
    return ev


# --------------------------------------------------------------------------- #
# work-type / purpose classification                                          #
# --------------------------------------------------------------------------- #
def test_prompt_classifier_handles_long_realistic_prompts():
    for expected, prompts in PROMPTS.items():
        for p in prompts:
            assert classify_from_prompt(p) == expected, f"{p[:40]} -> expected {expected}"


def test_branch_convention_beats_prompt_and_sets_source():
    # branch says feature, prompt sounds like a fix - branch (auditable) wins
    wt, src = work_type_and_source("feature/SHOP-12-new-cart", "SHOP-12", "fix the broken thing")
    assert wt == "feature" and src == "branch"
    # no branch -> prompt pattern, source prompt
    wt, src = work_type_and_source(None, None, PROMPTS["refactor"][0])
    assert wt == "refactor" and src == "prompt"
    # nothing classifiable, no llm -> none
    wt, src = work_type_and_source(None, None, "hello there")
    assert wt == "unknown" and src == "none"


def test_llm_fallback_only_when_branch_and_pattern_fail():
    calls = []

    def fake_llm(text):
        calls.append(text)
        return "feature"

    # a clear pattern -> llm NOT called
    work_type_and_source(None, None, PROMPTS["fix"][0], llm=fake_llm)
    assert calls == []
    # unclassifiable -> llm consulted exactly once
    wt, src = work_type_and_source(None, None, "make the thingy do the stuff", llm=fake_llm)
    assert wt == "feature" and src == "llm" and len(calls) == 1


# --------------------------------------------------------------------------- #
# full team simulation through the real pipeline                              #
# --------------------------------------------------------------------------- #
def _run_team(store, kg):
    ps, pe, now = current_month_bounds()
    prior, recent = ps + (now - ps) * 0.2, now - (now - ps) * 0.1
    monitors = {}

    def run(actor, ev):
        mon = monitors.setdefault(actor, SessionWasteMonitor())
        res = process(ev, kg=kg, hmac_key=HMAC, waste_monitor=mon, embed_fn=hashing_embed)
        store.insert(res.record)
        return res

    waste_kinds = set()
    # 6 developers on the Checkout platform, varied intents, branch-attributed
    for i in range(6):
        a = f"dev{i}@acme"
        run(a, _event(a, PROMPTS["feature"][0], branch="feature/SHOP-1-cart", ticket="SHOP-1",
                      repo="acme-checkout", ts=prior, inp=200_000))
        run(a, _event(a, PROMPTS["fix"][0], branch="fix/SHOP-2-crash", ticket="SHOP-2",
                      repo="acme-checkout", ts=recent, inp=150_000))
    # 5 developers on the Data platform, maintenance heavy
    for i in range(5):
        a = f"data{i}@acme"
        run(a, _event(a, PROMPTS["refactor"][0], branch="refactor/DATA-9", ticket="DATA-9",
                      model="gpt-5.5", ts=prior, inp=120_000, tool="cline"))
        run(a, _event(a, PROMPTS["test"][0], branch="test/DATA-10", ticket="DATA-10",
                      model="gpt-5.5", ts=recent, inp=80_000, tool="cline"))
    # NEW initiative this period: Zenith mobile app, only appears in the recent window, feature work,
    # and only 3 developers (so it is k-suppressed in the by-objective figures)
    for i in range(3):
        a = f"mob{i}@acme"
        run(a, _event(a, PROMPTS["feature"][1], branch="feature/MOB-1-new-app", ticket="MOB-1",
                      model="gemini-3.5-flash", ts=recent + i, inp=300_000, tool="opencode"))
    # orphan / unattributed exploration with no branch, classified from the prompt, recent window
    for i in range(6):
        a = f"dev{i}@acme"
        run(a, _event(a, PROMPTS["exploration"][0], ts=recent, inp=90_000, tool="claude-code"))
    return waste_kinds


def test_team_reports_purpose_budgets_drift_and_kanon(tmp_path):
    kg = _kg()
    store = DerivedStore(tmp_path / "deep.db")
    _run_team(store, kg)
    rep = management_report(store, k=5, kg=kg)

    # purpose mix: feature + fix + refactor + test + exploration all present
    labels = {r["label"] for r in rep["by_work_type"]}
    assert {"feature", "fix", "refactor", "test", "exploration"} <= labels

    # net-new vs maintenance investment both material
    assert rep["investment"]["net_new"] > 0 and rep["investment"]["maintenance"] > 0

    # new initiative detected and traced: the Zenith mobile app, feature work, started this period
    new_labels = {n["label"] for n in rep["new_initiatives"]}
    assert "Zenith - New Mobile App" in new_labels
    zen = next(n for n in rep["new_initiatives"] if n["label"] == "Zenith - New Mobile App")
    assert zen["work_type"] == "feature"
    assert zen["cost"] is None  # only 3 devs -> spend figure k-suppressed

    # by-objective: Zenith (3 devs) suppressed, Acme/Initech (>=5) shown
    by_obj = {r["label"]: r for r in rep["by_objective"]}
    assert by_obj["Zenith - New Mobile App"]["suppressed"] is True
    assert by_obj["Acme - Checkout Platform"]["suppressed"] is False

    # budgets computed for every budgeted objective
    assert len(rep["budgets"]) == 3

    # drift: orphan rose in the recent window (the unattributed exploration) -> alert
    assert rep["trend"]["orphan_share"]["alert"] is True
    store.close()


def test_developer_private_view_is_scoped_and_has_work_mix(tmp_path):
    kg = _kg()
    store = DerivedStore(tmp_path / "deep2.db")
    _run_team(store, kg)
    me = developer_report(store, pseudonymize("dev0@acme", HMAC))
    assert me["calls"] >= 3              # dev0 ran feature + fix + exploration
    assert me["work_type_mix"]           # has a purpose breakdown
    mix = {m["label"] for m in me["work_type_mix"]}
    assert "feature" in mix and "fix" in mix
    # scoped: another developer's spend is not included
    other = developer_report(store, pseudonymize("data0@acme", HMAC))
    assert other["actor_pseudonym"] != me["actor_pseudonym"]
    assert {m["label"] for m in other["work_type_mix"]} & {"refactor", "test"}
    store.close()


# --------------------------------------------------------------------------- #
# waste patterns across a long single-developer session                        #
# --------------------------------------------------------------------------- #
def test_all_waste_patterns_fire_in_one_session():
    mon = SessionWasteMonitor()
    seen = set()

    def observe(prompt, answer="ok", inp=500, out=50, dup=0):
        ev = _event("solo@acme", prompt, inp=inp, out=out, answer=answer)
        ev.duplicate_history_tokens = dup
        for s in mon.observe(ev):
            seen.add(s.kind)

    observe("how do I configure the retry policy for the worker", answer="set it to 5")
    observe("optimize the slow report query", inp=400_000, out=40_000, dup=300_000)  # context bloat
    observe("please fix the failing auth integration test now")
    observe("please fix the failing auth integration test now!!")                     # retry loop
    observe("how do I configure the retry policy for the worker again")               # answered already
    observe("rename one variable", inp=120, out=30)                                   # routing hint
    assert {"retry_loop", "context_bloat", "answered_already", "routing_hint"} <= seen


# --------------------------------------------------------------------------- #
# collaboration across the team                                                #
# --------------------------------------------------------------------------- #
def test_collaboration_live_dup_solved_reuse_and_walls():
    b = CollaborationBroker(threshold=0.8)

    def topic(actor, text, client=None, residency="eu", solved=False):
        return TopicSignal(actor, hashing_embed(text), text, client=client, residency=residency, is_solved=solved)

    saga = "temporal saga compensation for the multi step approval workflow"
    assert b.submit(topic("px_a", saga, client="acme")) == []
    live = b.submit(topic("px_b", saga, client="acme"))
    assert live and live[0].mode == "live_duplication"

    b.submit(topic("px_expert", "idempotent webhook retries with backoff", solved=True))
    reuse = b.submit(topic("px_new", "idempotent webhook retries with backoff"))
    assert reuse and reuse[0].mode == "solved_reuse"

    # chinese wall: identical topic, different clients -> never matched
    b.submit(topic("px_x", "kafka exactly once delivery", client="acme"))
    assert b.submit(topic("px_y", "kafka exactly once delivery", client="initech")) == []
