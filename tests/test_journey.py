"""
Realistic developer journeys: long agentic sessions, concurrent conversations, a developer switching
objectives through a day, unpriced models, tool-use streams, and a crowd converging on one topic.
These are written from the end-user's seat and deliberately probe the edges that many-turn, many-user
load exposes - including gaps that simpler tests miss.
"""
from __future__ import annotations

from abenlux.capture.adapters import parse_anthropic_stream
from abenlux.capture.diff import SessionHistoryTracker, conversation_key
from abenlux.attribution.attributor import KnowledgeGraph, Objective
from abenlux.collaborate.broker import CollaborationBroker, TopicSignal
from abenlux.embedding import hashing_embed
from abenlux.pipeline import process
from abenlux.processing.waste import SessionWasteMonitor
from abenlux.schema import CanonicalEvent, Message, Usage, WorkContext
from abenlux.store import DerivedStore


def _ev(prompt, *, branch=None, ticket=None, repo=None, model="claude-opus-4-8", inp=200_000, answer="ok"):
    ev = CanonicalEvent(messages=[Message("user", prompt)], output_messages=[Message("assistant", answer)],
                        usage=Usage(input_tokens=inp, output_tokens=inp // 8, cache_read_tokens=int(inp * 0.7)),
                        request_model=model, work=WorkContext(tool="aider", git_branch=branch, ticket_id=ticket, repo=repo))
    ev.actor_raw = "dev@corp"
    return ev


# --------------------------------------------------------------------------- #
# a long agentic session: each turn resends the growing transcript            #
# --------------------------------------------------------------------------- #
def test_resent_history_grows_over_a_long_session():
    tracker = SessionHistoryTracker()
    transcript = [Message("system", "x" * 200)]
    dups = []
    for k in range(1, 13):  # 12 turns
        transcript.append(Message("user", f"turn {k}: " + "y" * 300))
        dups.append(tracker.duplicate_history_tokens("dev:anthropic:repo:anchor", list(transcript)))
        transcript.append(Message("assistant", "z" * 100))
    # the resent prefix grows turn over turn (turn 1 has nothing prior)
    assert dups[0] == 0
    assert all(b > a for a, b in zip(dups[1:], dups[2:]))  # strictly increasing after the first


# --------------------------------------------------------------------------- #
# GAP: two concurrent conversations on the same provider must not thrash       #
# --------------------------------------------------------------------------- #
def test_concurrent_conversations_are_isolated():
    tracker = SessionHistoryTracker()
    # conversation A and conversation B interleave, same actor + provider + repo
    a1 = [Message("system", "sys"), Message("user", "A: design the saga " + "a" * 200)]
    b1 = [Message("system", "sys"), Message("user", "B: fix the crash " + "b" * 200)]
    ka = conversation_key("dev", "anthropic", "repo", a1)
    kb = conversation_key("dev", "anthropic", "repo", b1)
    assert ka != kb  # different conversations -> different keys, no cross-thrash
    tracker.duplicate_history_tokens(ka, a1)
    tracker.duplicate_history_tokens(kb, b1)
    # A turn 2 resends A's transcript -> resent-history detected against A only, not B
    a2 = a1 + [Message("assistant", "use a saga"), Message("user", "A: now add retries")]
    dup_a = tracker.duplicate_history_tokens(conversation_key("dev", "anthropic", "repo", a2), a2)
    assert dup_a > 0
    # B's next turn, a *fresh* topic, is not polluted by A's history
    b2 = [Message("system", "sys"), Message("user", "B: totally new unrelated question")]
    dup_b = tracker.duplicate_history_tokens(conversation_key("dev", "anthropic", "repo", b2), b2)
    assert dup_b == 0


# --------------------------------------------------------------------------- #
# GAP: the waste monitor must stay bounded over a very long session            #
# --------------------------------------------------------------------------- #
def test_waste_monitor_memory_is_bounded():
    mon = SessionWasteMonitor()
    for i in range(500):
        mon.observe(CanonicalEvent(messages=[Message("user", f"distinct question number {i}")],
                                   output_messages=[Message("assistant", "ok")], usage=Usage(500, 50)))
    assert len(mon._prompts) <= mon.max_history
    assert len(mon._answers) <= mon.max_history


# --------------------------------------------------------------------------- #
# a developer switches across objectives through the day                       #
# --------------------------------------------------------------------------- #
def test_developer_switches_objectives_across_the_day(tmp_path):
    kg = KnowledgeGraph()
    for oid, label, pref in [("obj-shop", "Acme Checkout", "SHOP"), ("obj-data", "Initech Data", "DATA"),
                             ("obj-pay", "Globex Pay", "PAY")]:
        kg.add_objective(Objective(oid, label))
        kg.map_ticket_prefix(pref, oid)
    store = DerivedStore(tmp_path / "day.db")
    plan = [("feature/SHOP-1", "SHOP-1", "add a checkout endpoint", "obj-shop", "feature"),
            ("fix/DATA-7", "DATA-7", "fix the etl deadlock", "obj-data", "fix"),
            ("refactor/PAY-3", "PAY-3", "refactor and rename the gateway", "obj-pay", "refactor"),
            ("feature/SHOP-2", "SHOP-2", "build a new cart widget", "obj-shop", "feature")]
    for branch, ticket, prompt, oid, wt in plan:
        res = process(_ev(prompt, branch=branch, ticket=ticket), kg=kg, hmac_key=b"k", embed_fn=hashing_embed)
        assert res.record.objective_id == oid and res.record.work_type == wt
        store.insert(res.record)
    objs = {r[0] for r in store.conn.execute("SELECT DISTINCT objective_id FROM derived")}
    assert objs == {"obj-shop", "obj-data", "obj-pay"}
    store.close()


# --------------------------------------------------------------------------- #
# an unpriced model flows through and is flagged, never silently zeroed         #
# --------------------------------------------------------------------------- #
def test_unpriced_model_flagged_through_pipeline(tmp_path):
    from abenlux.analytics.reports import management_report
    kg = KnowledgeGraph()
    store = DerivedStore(tmp_path / "u.db")
    store.insert(process(_ev("hi", model="some-exotic-model-2027"), kg=kg, hmac_key=b"k", embed_fn=hashing_embed).record)
    rep = management_report(store, k=1)
    store.close()
    assert rep["unpriced_events"] == 1


# --------------------------------------------------------------------------- #
# anthropic tool-use stream: text reassembled, tool_use blocks skipped         #
# --------------------------------------------------------------------------- #
def test_anthropic_tool_use_stream():
    stream = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"model":"claude-opus-4-8","usage":{"input_tokens":500,"output_tokens":1}}}\n\n'
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"let me check"}}\n\n'
        'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","name":"grep"}}\n\n'
        'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"q\\":1}"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":88}}\n\n'
    )
    text, usage, finish, model = parse_anthropic_stream(stream)
    assert text == "let me check"        # tool_use json is NOT mixed into the reassembled text
    assert usage.output_tokens == 88     # authoritative cumulative count
    assert finish == ["tool_use"]


# --------------------------------------------------------------------------- #
# a crowd converges on one topic: each newcomer matches all prior peers         #
# --------------------------------------------------------------------------- #
def test_many_users_converge_on_a_topic():
    b = CollaborationBroker(threshold=0.8)
    emb = hashing_embed("temporal saga compensation for the approval workflow")
    counts = []
    for i in range(5):
        counts.append(len(b.submit(TopicSignal(f"dev{i}", emb, "approval saga", client="acme"))))
    assert counts == [0, 1, 2, 3, 4]  # the 5th developer is told about all four predecessors
