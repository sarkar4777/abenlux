"""
Attribution. Turns a token event into an objective by a *join*, not a guess, wherever
possible. The reason most AI-ROI systems hallucinate value is they infer purpose, we
link it.

Resolution order (most to least defensible), recorded as `attribution_method`:
  1. ticket_join  - branch encodes a ticket id (feature/ACME-1234), ticket -> objective
                    via the org knowledge graph. No ML, fully auditable.
  2. repo_join    - repo maps directly to an initiative/objective.
  3. semantic     - fall back to embedding nearest-objective (low confidence, flagged).
  4. none         - resolves to no node -> ORPHAN. This is the key waste metric.

The semantic step is deliberately last and confidence-gated. Inferring purpose is exactly
the move that makes AI-ROI dashboards lie, so it runs only when no join exists, only above a
similarity floor, and its result is always stamped method="semantic" with a sub-1.0
confidence - orphan spend is never silently disguised as attributed.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

# branch -> ticket id (JIRA/ADO style). Tune per org.
_TICKET = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")


@dataclass
class Objective:
    id: str
    label: str
    kind: str = "client"          # "client" | "innovation" | "internal"
    client: Optional[str] = None  # enforces Chinese-wall scoping downstream
    embedding: Optional[list[float]] = None  # set when semantic fallback is enabled
    monthly_budget_usd: Optional[float] = None  # spend ceiling for budgets/forecast/guardrails


@dataclass
class KnowledgeGraph:
    """The one-time 'deep research on the company' artifact: the mapping of repos,
    ticket-project-prefixes and initiatives to objectives. Loaded from YAML in prod."""

    objectives: dict[str, Objective] = field(default_factory=dict)
    repo_to_objective: dict[str, str] = field(default_factory=dict)
    ticket_prefix_to_objective: dict[str, str] = field(default_factory=dict)
    semantic_threshold: float = 0.55  # below this, stay orphan rather than guess

    def add_objective(self, obj: Objective) -> None:
        self.objectives[obj.id] = obj

    def map_repo(self, repo: str, objective_id: str) -> None:
        self.repo_to_objective[repo.lower()] = objective_id

    def map_ticket_prefix(self, prefix: str, objective_id: str) -> None:
        self.ticket_prefix_to_objective[prefix.upper()] = objective_id

    def embed_objectives(self, embed_fn) -> None:
        """precompute an embedding per objective label so the semantic fallback can run.
        called once at load, uses the same embed_fn the pipeline uses for queries."""
        for obj in self.objectives.values():
            obj.embedding = embed_fn(obj.label)

    @classmethod
    def from_yaml(cls, path: str, *, embed_fn=None) -> "KnowledgeGraph":
        """load the company knowledge graph from the version-controlled YAML artifact.
        if embed_fn is given, objective embeddings are precomputed for semantic fallback."""
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        kg = cls()
        for o in data.get("objectives", []):
            kg.add_objective(Objective(
                id=o["id"], label=o["label"],
                kind=o.get("kind", "client"), client=o.get("client"),
                monthly_budget_usd=o.get("monthly_budget_usd") or o.get("budget_usd"),
            ))
        for repo, oid in (data.get("repo_to_objective") or {}).items():
            kg.map_repo(repo, oid)
        for prefix, oid in (data.get("ticket_prefix_to_objective") or {}).items():
            kg.map_ticket_prefix(prefix, oid)
        if "semantic_threshold" in data:
            kg.semantic_threshold = float(data["semantic_threshold"])
        if embed_fn is not None:
            kg.embed_objectives(embed_fn)
        return kg


@dataclass
class AttributionResult:
    objective_id: Optional[str]
    objective_label: Optional[str]
    method: str           # "ticket_join" | "repo_join" | "semantic" | "none"
    is_orphan: bool
    confidence: float     # 1.0 for joins, <1 for semantic


def extract_ticket(branch: Optional[str]) -> Optional[str]:
    if not branch:
        return None
    m = _TICKET.search(branch)
    return m.group(1) if m else None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def attribute(event, kg: KnowledgeGraph, *, query_embedding: Optional[list[float]] = None) -> AttributionResult:
    work = event.work

    # 1. ticket join
    ticket = event.work.ticket_id or extract_ticket(work.git_branch)
    if ticket:
        prefix = ticket.split("-", 1)[0].upper()
        oid = kg.ticket_prefix_to_objective.get(prefix)
        if oid and oid in kg.objectives:
            obj = kg.objectives[oid]
            return AttributionResult(oid, obj.label, "ticket_join", False, 1.0)

    # 2. repo join
    if work.repo:
        oid = kg.repo_to_objective.get(work.repo.lower())
        if oid and oid in kg.objectives:
            obj = kg.objectives[oid]
            return AttributionResult(oid, obj.label, "repo_join", False, 1.0)

    # 3. semantic fallback - nearest objective by embedding, only above the floor. confidence
    #    is the raw similarity so a weak match reads as low-confidence, never a clean join.
    if query_embedding is not None:
        best: tuple[float, Objective] | None = None
        for obj in kg.objectives.values():
            if not obj.embedding:
                continue
            sim = _cosine(query_embedding, obj.embedding)
            if best is None or sim > best[0]:
                best = (sim, obj)
        if best and best[0] >= kg.semantic_threshold:
            sim, obj = best
            return AttributionResult(obj.id, obj.label, "semantic", False, round(sim, 3))

    # 4. orphan
    return AttributionResult(None, None, "none", True, 0.0)
