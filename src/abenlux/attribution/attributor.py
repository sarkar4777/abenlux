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


# branch/commit-prefix conventions -> what the spend is FOR. a join on naming conventions, not a
# guess about content. lets management see where AI investment flows: net-new build vs upkeep.
_WORK_PREFIX = {
    "feature": "feature", "feat": "feature", "feature-new": "feature", "new": "feature",
    "fix": "fix", "bug": "fix", "bugfix": "fix", "hotfix": "fix", "patch": "fix",
    "refactor": "refactor", "refac": "refactor", "cleanup": "refactor", "tidy": "refactor",
    "perf": "perf", "optimize": "perf",
    "chore": "chore", "build": "chore", "ci": "chore", "ops": "chore", "deps": "chore", "release": "chore",
    "spike": "exploration", "poc": "exploration", "prototype": "exploration",
    "experiment": "exploration", "explore": "exploration", "research": "exploration",
    "docs": "docs", "doc": "docs",
    "test": "test", "tests": "test", "qa": "test",
}

# net-new development vs keeping the lights on. the split management actually cares about.
NET_NEW = {"feature", "exploration"}
MAINTENANCE = {"fix", "refactor", "perf", "chore", "docs", "test"}


# when there is no branch convention, infer purpose from the prompt. each pattern is weighted,
# the highest-scoring work type above the floor wins. runs on the edge on REDACTED text and only
# the resulting label persists, never the prompt. strong signals first.
_PROMPT_SIGNALS = [
    ("fix", r"\b(fix|bug|broken|fails?|failing|stack ?trace|traceback|exception|errors?|crash(?:es|ing)?|regression|throw(?:s|ing|n)?|raises?|doesn'?t work|not working|times? ?out|timeout|null pointer|segfault|sort it out|debug|stop(?:s|ped)? working|wrong|incorrect|unexpected|off by|misbehav|returns the wrong|nan|race condition|deadlock|flaky)\b", 3),
    ("test", r"\b(unit tests?|integration tests?|e2e tests?|write tests?|add tests?|test coverage|test cases?|pytest|jest|vitest|mock this|increase coverage)\b", 3),
    ("docs", r"\b(document this|docstring|write (a |the )?readme|update (the )?readme|add comments?|explain (this|the) (code|function)|write (the )?docs|api docs)\b", 3),
    ("refactor", r"\b(refactor|clean ?up|rename|extract\b.{0,40}?\b(helper|method|function|class|module|logic)|reusable|simplif|restructure|de-?dup(?:e|licate)?|tidy|untangle|split (this|it) (up|into))\b", 3),
    ("perf", r"\b(optimi[sz]e|(?:too |really |very )?slow|sluggish|performance|latency|speed (it |this )?up|reduce (memory|allocations|time)|bottleneck|faster|profile (this|it))\b", 3),
    ("chore", r"\b(bump|dependenc(?:y|ies)|lock ?file|set ?up ci|ci(?: |/)(pipeline|cd)|github actions?|dockerfile|docker image|cut a release|tag it|release v?\d|upgrade\b.{0,30}\bto\b|pin (the )?version|pre-?commit|lint config)\b", 2),
    ("exploration", r"\b(how (do|should|can|would) (i|we)|what(?:'s| is) the best|compare|versus|trade-?offs?|options? for|prototype|proof of concept|poc|spike|evaluate|which (library|approach|one|framework))\b", 2),
    ("feature", r"\b(add|implement|build|create|scaffold|new (?:\w+ ){0,2}(feature|endpoint|api|page|screen|component|service|app|module|importer|flow)|support (for|sso|saml)|wire up|integrate|set up (a |the )?(new )?(service|endpoint|page|api)|make it possible|allow (users?|the user|them|people) to|let (users?|me|them|people)|i (need|want) (a|an|to)|ability to|enable)\b", 2),
]
_PROMPT_PATTERNS = [(label, re.compile(rx, re.IGNORECASE), w) for label, rx, w in _PROMPT_SIGNALS]


def classify_from_prompt(text: Optional[str], learned: Optional[dict] = None) -> str:
    """infer work type from (already-redacted) prompt text. built-in patterns plus the device's
    self-learned vocabulary (label -> set of terms). content-free output, label only."""
    if not text:
        return "unknown"
    low = text.lower()
    scores: dict[str, int] = {}
    for label, pat, weight in _PROMPT_PATTERNS:
        hits = len(pat.findall(text))
        if hits:
            scores[label] = scores.get(label, 0) + hits * weight
    if learned:
        for label, terms in learned.items():
            for term in terms:
                if term in low:
                    scores[label] = scores.get(label, 0) + 2
    if not scores:
        return "unknown"
    return max(scores.items(), key=lambda kv: kv[1])[0]


def classify_work_type(branch: Optional[str], ticket_id: Optional[str] = None,
                       prompt_text: Optional[str] = None) -> str:
    """purpose of the spend. branch convention first (auditable), prompt-pattern fallback next."""
    if branch:
        head = re.split(r"[/_-]", branch.strip().lower(), maxsplit=1)[0]
        wt = _WORK_PREFIX.get(head)
        if wt:
            return wt
    return classify_from_prompt(prompt_text)


def work_type_and_source(branch: Optional[str], ticket_id: Optional[str],
                         prompt_text: Optional[str], llm=None, learned: Optional[dict] = None) -> tuple[str, str]:
    """return (work_type, source) where source is 'branch' | 'prompt' | 'llm' | 'none'.
    cascade: branch convention (auditable, free) -> keyword patterns + device-learned vocabulary
    (free) -> one tiny cached llm call only if everything above failed. minimizes spend."""
    if branch:
        head = re.split(r"[/_-]", branch.strip().lower(), maxsplit=1)[0]
        wt = _WORK_PREFIX.get(head)
        if wt:
            return wt, "branch"
    wt = classify_from_prompt(prompt_text, learned)
    if wt != "unknown":
        return wt, "prompt"
    if llm is not None and prompt_text:
        smart = llm(prompt_text)
        if smart:
            return smart, "llm"
    return "unknown", "none"


def is_net_new(work_type: Optional[str]) -> bool:
    return work_type in NET_NEW


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

    # semantic fallback - nearest objective by embedding, only above the floor. confidence
    #is the raw similarity so a weak match reads as low-confidence, never a clean join.
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

    #orphan
    return AttributionResult(None, None, "none", True, 0.0)
