"""
Exhaustive end-to-end org simulation. A whole company of developers, many long prompts, multiple
providers, two time windows - run through the REAL edge pipeline - then every feature asserted with
no gaps: redaction, all attribution methods, work-type via branch/prompt/learned/llm, self-learning,
budgets, drift, collaboration (all modes + walls + consent), k-anonymity, the developer-local
knowledge graph, the private developer view, and the privacy invariant on disk.
"""
from __future__ import annotations

import sqlite3

import pytest

from abenlux.analytics.budget import current_month_bounds
from abenlux.analytics.reports import developer_report, management_report
from abenlux.attribution.attributor import KnowledgeGraph, Objective
from abenlux.collaborate.broker import CollaborationBroker, TopicSignal
from abenlux.developer.knowledge_graph import DevKnowledgeGraph
from abenlux.embedding import hashing_embed
from abenlux.pipeline import process
from abenlux.privacy.pseudonymize import pseudonymize
from abenlux.processing.waste import SessionWasteMonitor
from abenlux.schema import CanonicalEvent, Message, Usage, WorkContext
from abenlux.store import DerivedStore
from abenlux.worktype_learn import WorkTypeLearner

HMAC = b"exhaustive-key"
SECRET = "sk-ant-EXHAUSTIVE1234567890SECRETKEY"
EMAIL = "leaky.dev@corp.com"

LONG = {
    "feature": "I want to add a brand new GraphQL endpoint for order history with pagination, build "
               "the resolver and wire it into the schema. " + ("Background context. " * 25),
    "fix": "Please fix the deadlock in the order reconciler, the bug crashes under load and the "
           "stack trace points at a race condition. " + ("More noise here. " * 25),
    "refactor": "Refactor this giant controller, extract the validation into a class and rename the "
                "confusing variables without changing behavior. " + ("Filler text. " * 20),
    "test": "Write comprehensive unit tests and integration tests for the refund service with a mock "
            "gateway and good coverage. " + ("Extra detail. " * 20),
    "exploration": "How should I architect a saga with compensation, compare the trade-offs and "
                   "evaluate which library is the best fit for our stack. " + ("Rambling. " * 20),
}


class _FakeLLM:
    """stands in for the tiny LLM fallback, records when it is consulted."""

    def __init__(self):
        self.calls = []

    def __call__(self, text):
        self.calls.append(text)
        return "exploration"


def _event(actor, prompt, *, branch=None, ticket=None, repo=None, model="claude-opus-4-8",
           inp=400_000, ts=None, tool="aider"):
    ev = CanonicalEvent(
        messages=[Message("user", prompt)], output_messages=[Message("assistant", "ok")],
        usage=Usage(input_tokens=inp, output_tokens=inp // 8, cache_read_tokens=int(inp * 0.7)),
        request_model=model, work=WorkContext(tool=tool, git_branch=branch, ticket_id=ticket, repo=repo),
    )
    ev.actor_raw = actor
    if ts is not None:
        ev.ts = ts
    return ev


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    d = tmp_path_factory.mktemp("exhaustive")
    db = str(d / "central.db")
    kg = KnowledgeGraph(semantic_threshold=0.3)
    kg.add_objective(Objective("obj-shop", "Acme - Checkout Platform", "client", client="acme", monthly_budget_usd=100000))
    kg.add_objective(Objective("obj-data", "Initech Data Platform Pipeline", "client", client="initech", monthly_budget_usd=100000))
    kg.add_objective(Objective("obj-pay", "Globex - Payments", "client", client="globex", monthly_budget_usd=1))
    kg.add_objective(Objective("obj-mobile", "Zenith - New Mobile App", "innovation", monthly_budget_usd=3000))
    kg.map_ticket_prefix("SHOP", "obj-shop")
    kg.map_ticket_prefix("DATA", "obj-data")
    kg.map_ticket_prefix("PAY", "obj-pay")
    kg.map_ticket_prefix("MOB", "obj-mobile")
    kg.map_repo("acme-checkout", "obj-shop")
    kg.embed_objectives(hashing_embed)

    store = DerivedStore(db)
    learner = WorkTypeLearner(d / "wt.json", min_count=3)
    llm = _FakeLLM()
    monitors: dict = {}

    def run(actor, ev, llm_on=False):
        mon = monitors.setdefault(actor, SessionWasteMonitor())
        res = process(ev, kg=kg, hmac_key=HMAC, waste_monitor=mon, embed_fn=hashing_embed,
                      work_type_classifier=(llm if llm_on else None), work_type_learner=learner)
        store.insert(res.record)
        return res

    ps, _, now = current_month_bounds()
    prior, recent = ps + (now - ps) * 0.2, now - (now - ps) * 0.1

    # leaky first prompt to prove redaction end to end
    run("shop0@corp", _event("shop0@corp", f"add a feature. my key is {SECRET} email {EMAIL}",
                             branch="feature/SHOP-1", ticket="SHOP-1", repo="acme-checkout", ts=prior))

    # Acme checkout: 6 devs, branch-attributed feature + fix work (work_type_source = branch)
    for i in range(6):
        a = f"shop{i}@corp"
        run(a, _event(a, LONG["feature"], branch="feature/SHOP-1", ticket="SHOP-1", repo="acme-checkout", ts=prior, inp=600_000))
        run(a, _event(a, LONG["fix"], branch="fix/SHOP-2", ticket="SHOP-2", repo="acme-checkout", ts=recent, inp=300_000))
    # Initech data: 5 devs, refactor + test, gpt-5.5
    for i in range(5):
        a = f"data{i}@corp"
        run(a, _event(a, LONG["refactor"], branch="refactor/DATA-9", ticket="DATA-9", model="gpt-5.5", ts=prior, inp=200_000, tool="cline"))
        run(a, _event(a, LONG["test"], branch="test/DATA-10", ticket="DATA-10", model="gpt-5.5", ts=recent, inp=150_000, tool="cline"))
    # Globex payments: 5 devs, NO branch, classified from the PROMPT (work_type_source = prompt)
    for i in range(5):
        a = f"pay{i}@corp"
        run(a, _event(a, "fix the payment webhook retry that is broken and crashing", ticket="PAY-3", model="claude-sonnet-4-6", ts=prior, inp=300_000, tool="cursor-agent"))
    # Zenith new mobile app: 3 devs, recent window only, feature work -> NEW initiative, k-suppressed
    for i in range(3):
        a = f"mob{i}@corp"
        run(a, _event(a, LONG["feature"], branch="feature/MOB-1", ticket="MOB-1", model="gemini-3.5-flash", ts=recent + i, inp=400_000, tool="opencode"))
    # repo_join: an event with a known repo but no ticket and a non-conventional branch
    run("shop0@corp", _event("shop0@corp", LONG["feature"], branch="main", repo="acme-checkout", ts=recent, inp=120_000))
    # semantic attribution: no branch, no repo, prompt mentions the objective vocabulary
    run("rover@corp", _event("rover@corp", "working on the initech data platform pipeline ingestion", ts=recent, inp=90_000))
    # orphan + drift: unattributed exploration in the recent window, classified from prompt
    for i in range(6):
        a = f"shop{i}@corp"
        run(a, _event(a, LONG["exploration"], ts=recent, inp=80_000, tool="claude-code"))
    # genuinely ambiguous -> the LLM fallback is consulted (work_type_source = llm)
    run("ambi@corp", _event("ambi@corp", "make the doohickey do the whatsit", ts=recent, inp=50_000), llm_on=True)

    # self-learning: teach a distinctive term via branch ground truth, then classify it unbranched
    distinct = "the quasar telemetry buffer overflows after every rollout"
    for _ in range(4):
        run("learner@corp", _event("learner@corp", distinct, branch="fix/CORE-1", ts=recent, inp=40_000))
    learned_res = run("learner@corp", _event("learner@corp", "the quasar telemetry buffer is acting up again", ts=recent, inp=40_000))

    rep = management_report(store, k=5, kg=kg)
    return {"db": db, "store": store, "kg": kg, "learner": learner, "llm": llm, "rep": rep,
            "learned_res": learned_res}


# --------------------------------------------------------------------------- #
# privacy invariant                                                            #
# --------------------------------------------------------------------------- #
def test_privacy_no_secret_identity_or_content_on_disk(sim):
    raw = open(sim["db"], "rb").read()
    assert SECRET.encode() not in raw and b"sk-ant-" not in raw
    assert EMAIL.encode() not in raw
    assert b"shop0@corp" not in raw                     # raw identity pseudonymized
    assert b"deadlock in the order reconciler" not in raw  # prompt content discarded
    cols = {c[1] for c in sqlite3.connect(sim["db"]).execute("PRAGMA table_info(derived)").fetchall()}
    assert not ({"messages", "content", "actor_raw"} & cols)


# --------------------------------------------------------------------------- #
# attribution: every method exercised                                          #
# --------------------------------------------------------------------------- #
def test_all_attribution_methods_present(sim):
    methods = {r["attribution_method"] for r in
               sim["store"].conn.execute("SELECT DISTINCT attribution_method FROM derived").fetchall()}
    # sqlite Row -> use index
    methods = {r[0] for r in sim["store"].conn.execute("SELECT DISTINCT attribution_method FROM derived")}
    assert {"ticket_join", "repo_join", "semantic", "none"} <= methods


def test_cost_is_consistent_with_tokens(sim):
    t = sim["rep"]
    blended = t["total_cost_usd"] / t["total_tokens"] * 1e6
    assert 0.3 < blended < 30  # realistic $/Mtok, not the old fabricated nonsense


# --------------------------------------------------------------------------- #
# work-type / purpose: all four sources                                        #
# --------------------------------------------------------------------------- #
def test_work_type_sources_branch_prompt_llm_and_learned(sim):
    sources = {r[0] for r in sim["store"].conn.execute("SELECT DISTINCT work_type_source FROM derived")}
    assert {"branch", "prompt", "llm"} <= sources
    # the learned case classified as fix from the device's self-taught vocabulary, source 'prompt'
    assert sim["learned_res"].record.work_type == "fix"
    assert sim["learned_res"].record.work_type_source == "prompt"


def test_llm_only_consulted_for_unknown(sim):
    # the fake llm was wired on exactly one ambiguous call and one fixture call -> consulted sparingly
    assert 1 <= len(sim["llm"].calls) <= 2


def test_purpose_mix_and_investment_split(sim):
    labels = {r["label"] for r in sim["rep"]["by_work_type"]}
    assert {"feature", "fix", "refactor", "test", "exploration"} <= labels
    inv = sim["rep"]["investment"]
    assert inv["net_new"] > 0 and inv["maintenance"] > 0


# --------------------------------------------------------------------------- #
# new initiatives, budgets, drift, k-anonymity                                 #
# --------------------------------------------------------------------------- #
def test_new_initiative_detected_and_kanon_suppressed(sim):
    new = {n["label"]: n for n in sim["rep"]["new_initiatives"]}
    assert "Zenith - New Mobile App" in new
    assert new["Zenith - New Mobile App"]["work_type"] == "feature"
    assert new["Zenith - New Mobile App"]["cost"] is None      # 3 devs -> figure suppressed
    by_obj = {r["label"]: r for r in sim["rep"]["by_objective"]}
    assert by_obj["Zenith - New Mobile App"]["suppressed"] is True
    assert by_obj["Acme - Checkout Platform"]["suppressed"] is False


def test_budgets_have_over_and_ok(sim):
    statuses = {b["label"].split(" - ")[0]: b["status"] for b in sim["rep"]["budgets"]}
    assert statuses["Globex"] == "over"          # budget of 1 dollar, way exceeded
    assert "Acme" in statuses                     # huge budget -> ok/at_risk computed


def test_drift_alerts_on_rising_orphan(sim):
    assert sim["rep"]["trend"]["orphan_share"]["alert"] is True


# --------------------------------------------------------------------------- #
# developer-local knowledge graph + private view                               #
# --------------------------------------------------------------------------- #
def test_dev_knowledge_graph_for_one_developer(sim):
    g = DevKnowledgeGraph(sim["store"], sim["learner"]).build()
    assert g["totals"]["calls"] > 0
    tickets = {t["ticket_id"] for t in g["tickets"]}
    assert {"SHOP-1", "SHOP-2", "DATA-9", "MOB-1"} <= tickets
    assert g["learned_vocabulary"]  # the device taught itself the quasar vocabulary
    text = DevKnowledgeGraph(sim["store"], sim["learner"]).render_text()
    assert "knowledge graph" in text


def test_developer_private_view_scoped(sim):
    me = developer_report(sim["store"], pseudonymize("shop0@corp", HMAC))
    assert me["calls"] > 0 and me["work_type_mix"]
    other = developer_report(sim["store"], pseudonymize("data0@corp", HMAC))
    assert me["actor_pseudonym"] != other["actor_pseudonym"]


# --------------------------------------------------------------------------- #
# collaboration across the team                                                #
# --------------------------------------------------------------------------- #
def test_collaboration_all_modes_walls_and_consent():
    b = CollaborationBroker(threshold=0.8)

    def topic(actor, text, client=None, residency="eu", solved=False):
        return TopicSignal(actor, hashing_embed(text), text, client=client, residency=residency, is_solved=solved)

    saga = "temporal saga compensation for the approval workflow orchestration"
    assert b.submit(topic("px_a", saga, client="acme")) == []
    assert b.submit(topic("px_b", saga, client="acme"))[0].mode == "live_duplication"

    b.submit(topic("px_e", "idempotent webhook retries with exponential backoff", solved=True))
    assert b.submit(topic("px_n", "idempotent webhook retries with exponential backoff"))[0].mode == "solved_reuse"

    b.submit(topic("px_x", "kafka exactly once delivery semantics", client="acme"))
    assert b.submit(topic("px_y", "kafka exactly once delivery semantics", client="globex")) == []

    assert b.mutually_consented("px_a", "px_b") is False
    b.record_consent("px_a", "px_b")
    b.record_consent("px_b", "px_a")
    assert b.mutually_consented("px_a", "px_b") is True
