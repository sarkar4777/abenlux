from abenlux.processing.redact import redact, shannon_entropy
from abenlux.processing.waste import SessionWasteMonitor, lexical_similarity
from abenlux.privacy.pseudonymize import pseudonymize, KAnonymityGate
from abenlux.attribution.attributor import (
    KnowledgeGraph, Objective, attribute, extract_ticket,
)
from abenlux.schema import CanonicalEvent, Message, Usage, WorkContext


# ---------------- redaction ----------------
def test_redact_destroys_api_keys_and_pii():
    text = ("connect with sk-ant-abcdefghij1234567890XYZ and email me at jane@corp.com "
            "from 10.0.0.4")
    rep = redact(text)
    assert "sk-ant-" not in rep.text
    assert "jane@corp.com" not in rep.text
    assert "10.0.0.4" not in rep.text
    assert rep.total >= 3
    assert "<REDACTED:" in rep.text


def test_redact_keeps_normal_prose():
    text = "How do I structure a Temporal saga for the approval workflow?"
    rep = redact(text)
    assert rep.text == text
    assert rep.total == 0


def test_entropy_separates_random_from_prose():
    assert shannon_entropy("aGVsbG8gd29ybGQgdGhpcyBpcyBiYXNlNjQ=") > shannon_entropy("the quick brown fox")


def test_private_key_block_redacted_whole():
    text = "-----BEGIN PRIVATE KEY-----\nMIIabc123\n-----END PRIVATE KEY-----"
    rep = redact(text)
    assert "MIIabc123" not in rep.text
    assert rep.counts.get("private_key") == 1


# ---------------- waste ----------------
def test_retry_loop_detected_on_near_verbatim():
    mon = SessionWasteMonitor()
    e1 = CanonicalEvent(messages=[Message("user", "fix the failing auth test please")],
                        output_messages=[Message("assistant", "try X")], usage=Usage(100, 20))
    e2 = CanonicalEvent(messages=[Message("user", "fix the failing auth test please!!")],
                        output_messages=[Message("assistant", "try Y")], usage=Usage(100, 20))
    mon.observe(e1)
    sigs = mon.observe(e2)
    assert any(s.kind == "retry_loop" for s in sigs)


def test_context_bloat_detected():
    mon = SessionWasteMonitor()
    e = CanonicalEvent(messages=[Message("user", "small new question")],
                       usage=Usage(input_tokens=10000, output_tokens=50))
    e.duplicate_history_tokens = 9000
    sigs = mon.observe(e)
    assert any(s.kind == "context_bloat" for s in sigs)


def test_lexical_similarity_bounds():
    assert lexical_similarity("hello world", "hello world") > 0.95
    assert lexical_similarity("hello world", "completely different text") < 0.4


# ---------------- privacy ----------------
def test_pseudonym_stable_and_irreversible_shape():
    a = pseudonymize("user@x.com", b"key")
    b = pseudonymize("user@x.com", b"key")
    c = pseudonymize("user@x.com", b"different-key")
    assert a == b and a != c and a.startswith("px_")


def test_k_anonymity_suppresses_small_groups():
    gate = KAnonymityGate(k=5)
    assert gate.noisy_count(100.0, distinct_actors=3) is None      # suppressed
    assert gate.noisy_count(100.0, distinct_actors=8) is not None  # allowed


# ---------------- attribution ----------------
def test_ticket_join_beats_orphan():
    kg = KnowledgeGraph()
    kg.add_objective(Objective("obj-acme", "Acme Checkout", "client", client="acme"))
    kg.map_ticket_prefix("ACME", "obj-acme")
    e = CanonicalEvent(work=WorkContext(git_branch="feature/ACME-1488-x"))
    r = attribute(e, kg)
    assert r.method == "ticket_join" and not r.is_orphan and r.objective_id == "obj-acme"


def test_unmapped_work_is_orphan():
    kg = KnowledgeGraph()
    e = CanonicalEvent(work=WorkContext(git_branch="main", repo="random"))
    r = attribute(e, kg)
    assert r.is_orphan and r.method == "none"


def test_extract_ticket():
    assert extract_ticket("feature/ACME-1234-foo") == "ACME-1234"
    assert extract_ticket("main") is None
