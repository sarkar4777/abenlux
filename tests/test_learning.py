"""
The self-learning loop and the developer-local knowledge graph. Proves the device teaches itself
intent vocabulary from confident labels, that the cheap classifier then uses it (so the LLM fires
less over time), that long prompts are compressed to their salient intent, and that the developer
can inspect their own knowledge graph.
"""
from abenlux.attribution.attributor import KnowledgeGraph, Objective, classify_from_prompt
from abenlux.developer.knowledge_graph import DevKnowledgeGraph
from abenlux.embedding import hashing_embed
from abenlux.pipeline import process
from abenlux.schema import CanonicalEvent, Message, Usage, WorkContext
from abenlux.store import DerivedStore
from abenlux.worktype_learn import WorkTypeLearner
from abenlux.worktype_llm import _compress


# --------------------------------------------------------------------------- #
# the learner                                                                  #
# --------------------------------------------------------------------------- #
def test_learner_promotes_consistent_term_and_classifier_uses_it(tmp_path):
    learner = WorkTypeLearner(tmp_path / "wt.json", min_count=3)
    # a term the built-in patterns do not know, seen consistently as 'fix'
    for _ in range(4):
        learner.observe("the kafka rebalance storm keeps happening in production", "fix")
    learned = learner.patterns()
    assert any("rebalance storm" in t or "rebalance" in t for terms in learned.values() for t in terms)
    # a brand-new prompt using that vocabulary now classifies as fix for free
    assert classify_from_prompt("we have a rebalance storm again", learned) == "fix"


def test_learner_does_not_promote_ambiguous_terms(tmp_path):
    learner = WorkTypeLearner(tmp_path / "wt.json", min_count=3, dominant_share=0.8)
    for _ in range(3):
        learner.observe("the widget gizmo", "fix")
    for _ in range(3):
        learner.observe("the widget gizmo", "feature")  # same term, split label -> not dominant
    terms = {t for ts in learner.patterns().values() for t in ts}
    assert "widget gizmo" not in terms


def test_learner_persists_and_reloads(tmp_path):
    path = tmp_path / "wt.json"
    a = WorkTypeLearner(path, min_count=2)
    a.observe("the snowflake warehouse autosuspend tuning", "perf")
    a.observe("the snowflake warehouse autosuspend tuning", "perf")
    a.flush()
    b = WorkTypeLearner(path, min_count=2)
    terms = {t for ts in b.patterns().values() for t in ts}
    assert any("autosuspend" in t for t in terms)


# --------------------------------------------------------------------------- #
# pipeline self-learning: branch ground truth teaches the cheap layer          #
# --------------------------------------------------------------------------- #
def _event(prompt, branch=None):
    ev = CanonicalEvent(messages=[Message("user", prompt)], usage=Usage(1000, 100),
                        request_model="claude-opus-4-8",
                        work=WorkContext(git_branch=branch))
    ev.actor_raw = "dev@x"
    return ev


def test_pipeline_self_learns_from_branch_then_classifies_unbranched(tmp_path):
    kg = KnowledgeGraph()
    learner = WorkTypeLearner(tmp_path / "wt.json", min_count=3)
    distinctive = "the flux capacitor calibration drifts after every deploy"
    # several branch-labeled 'fix' events teach the vocabulary (branch is ground truth)
    for _ in range(4):
        process(_event(distinctive, branch="fix/CORE-1"), kg=kg, hmac_key=b"k",
                embed_fn=hashing_embed, work_type_learner=learner)
    # now the SAME prompt with NO branch and NO llm classifies as fix via the learned vocabulary
    res = process(_event("flux capacitor calibration is off again"), kg=kg, hmac_key=b"k",
                  embed_fn=hashing_embed, work_type_learner=learner)
    assert res.record.work_type == "fix"
    assert res.record.work_type_source == "prompt"  # learned, not llm


# --------------------------------------------------------------------------- #
# long-prompt extractive compression                                           #
# --------------------------------------------------------------------------- #
def test_compress_keeps_intent_and_shrinks_long_prompt():
    long = (
        "Here is a lot of background about our system architecture and history. " * 30
        + "Please fix the deadlock in the order reconciler that happens under load. "
        + "More rambling context that is not the point at all. " * 20
    )
    out = _compress(long)
    assert len(out) < len(long)
    assert "deadlock" in out.lower()  # the salient intent sentence survived


def test_compress_leaves_short_prompts_untouched():
    short = "add a new billing endpoint"
    assert _compress(short) == short


# --------------------------------------------------------------------------- #
# developer-local knowledge graph                                              #
# --------------------------------------------------------------------------- #
def test_dev_knowledge_graph_build_and_render(tmp_path):
    kg = KnowledgeGraph()
    kg.add_objective(Objective("obj-x", "Acme - Checkout Platform"))
    kg.map_ticket_prefix("SHOP", "obj-x")
    learner = WorkTypeLearner(tmp_path / "wt.json", min_count=2)
    store = DerivedStore(tmp_path / "local.db")
    for branch, prompt in [("feature/SHOP-1", "add a new checkout endpoint"),
                           ("fix/SHOP-2", "fix the broken coupon validation")]:
        res = process(_event(prompt, branch=branch), kg=kg, hmac_key=b"k",
                      embed_fn=hashing_embed, work_type_learner=learner)
        store.insert(res.record)

    g = DevKnowledgeGraph(store, learner).build()
    assert g["totals"]["calls"] == 2
    obj_labels = {o["label"] for o in g["objectives"]}
    assert "Acme - Checkout Platform" in obj_labels
    tickets = {t["ticket_id"] for t in g["tickets"]}
    assert {"SHOP-1", "SHOP-2"} <= tickets
    wt = {r["label"] for r in g["work_types"]}
    assert {"feature", "fix"} <= wt
    text = DevKnowledgeGraph(store, learner).render_text()
    assert "knowledge graph" in text and "SHOP-1" in text
    store.close()
