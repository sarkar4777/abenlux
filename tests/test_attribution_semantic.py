from abenlux.attribution.attributor import KnowledgeGraph, Objective, attribute
from abenlux.embedding import hashing_embed
from abenlux.schema import CanonicalEvent, WorkContext


def test_from_yaml_loads_objectives_and_joins(tmp_path):
    yaml_text = """
objectives:
  - {id: obj-acme, label: "Acme Checkout platform", kind: client, client: acme}
repo_to_objective:
  acme-checkout: obj-acme
ticket_prefix_to_objective:
  ACME: obj-acme
"""
    p = tmp_path / "kg.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    kg = KnowledgeGraph.from_yaml(str(p))
    assert "obj-acme" in kg.objectives
    e = CanonicalEvent(work=WorkContext(git_branch="feature/ACME-9-x"))
    r = attribute(e, kg)
    assert r.method == "ticket_join" and r.objective_id == "obj-acme"


def test_semantic_fallback_only_above_threshold_and_flagged():
    kg = KnowledgeGraph(semantic_threshold=0.4)
    kg.add_objective(Objective("obj-pay", "payment processing and billing systems"))
    kg.add_objective(Objective("obj-auth", "user authentication and login security"))
    kg.embed_objectives(hashing_embed)
    # query has no ticket/repo, but text overlaps the auth objective lexically
    q = hashing_embed("authentication login security flow")
    e = CanonicalEvent(work=WorkContext())  # no join keys
    r = attribute(e, kg, query_embedding=q)
    assert r.method == "semantic"
    assert r.objective_id == "obj-auth"
    assert r.confidence < 1.0 and not r.is_orphan


def test_semantic_below_threshold_stays_orphan():
    kg = KnowledgeGraph(semantic_threshold=0.95)  # impossibly strict
    kg.add_objective(Objective("obj-pay", "payments"))
    kg.embed_objectives(hashing_embed)
    e = CanonicalEvent(work=WorkContext())
    r = attribute(e, kg, query_embedding=hashing_embed("totally unrelated topic xyz"))
    assert r.is_orphan and r.method == "none"


def test_join_beats_semantic_when_both_available():
    kg = KnowledgeGraph(semantic_threshold=0.0)
    kg.add_objective(Objective("obj-a", "alpha", client="x"))
    kg.add_objective(Objective("obj-b", "beta"))
    kg.map_repo("repo-a", "obj-a")
    kg.embed_objectives(hashing_embed)
    e = CanonicalEvent(work=WorkContext(repo="repo-a"))
    r = attribute(e, kg, query_embedding=hashing_embed("beta beta beta"))
    assert r.method == "repo_join" and r.objective_id == "obj-a"  # join wins, confidence 1.0
