"""team memory. a content free index of what teammates already solved, matched on the embedding.

it runs at the collector where every developer's records meet. for each new record it looks for a close
earlier one from a DIFFERENT teammate in the same tenant and labels the match:

  serve       an almost identical ask in the same language, the answer could have been reused as is
  warm_start  the same task but only close, or the same task in another language, a strong head start

it changes nothing about the call. it records what a live team memory WOULD have saved, so the number
can be shown with proof before anyone turns the live version on.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Optional

SERVE = float(os.getenv("ABEN_TM_SERVE", "0.95"))
WARM = float(os.getenv("ABEN_TM_WARM", "0.80"))
WARM_FRACTION = float(os.getenv("ABEN_TM_WARM_FRACTION", "0.4"))
_CAP = int(os.getenv("ABEN_TM_CAP", "5000"))      # entries kept per tenant

# rough language markers, enough to tell common stacks apart from the salient text
_LANG = [
    ("python", ("python", " def ", "pytest", "pip ", ".py", "django", "flask", "asyncio")),
    ("go", (" golang", " go ", "goroutine", "gofmt", ".go", "func (")),
    ("rust", ("rust", "cargo", "borrow checker", ".rs", "impl ")),
    ("typescript", ("typescript", ".tsx", ".ts", "interface ", "tsconfig")),
    ("javascript", ("javascript", " node ", ".js", "react", "express")),
    ("java", (" java", "spring boot", ".java", "maven", "gradle")),
    ("csharp", ("c#", ".net", "csharp")),
    ("sql", (" sql", "select ", "postgres", "query plan")),
    ("ruby", ("ruby", "rails", ".rb")),
]


def detect_language(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = text.lower()
    for name, hints in _LANG:
        if any(h in t for h in hints):
            return name
    return None


def _cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _family(model: Optional[str]) -> str:
    if not model:
        return ""
    m = model.lower().split("/")[-1]
    return "-".join(m.split("-")[:2])     # claude-opus, gpt-4o, gemini-2.5


@dataclass
class _Entry:
    embedding: list
    language: Optional[str]
    model: Optional[str]
    solver: Optional[str]
    cost_usd: float


@dataclass
class TeamMatch:
    tier: str
    similarity: float
    same_language: bool
    solver: Optional[str]
    shadow_usd: float


class TeamMemory:
    """per tenant content free index. look a record up with match(), then add() it for later ones."""

    def __init__(self):
        self._by_tenant: dict[str, list[_Entry]] = {}

    def match(self, tenant, embedding, language, model, solver, cost_usd) -> Optional[TeamMatch]:
        if not embedding:
            return None
        best = None
        for e in self._by_tenant.get(tenant, []):
            if e.solver and solver and e.solver == solver:
                continue                      # a teammate's work, not your own
            sim = _cosine(embedding, e.embedding)
            if best is None or sim > best[0]:
                best = (sim, e)
        if best is None:
            return None
        sim, e = best
        same_lang = bool(language and e.language and language == e.language)
        cost = max(0.0, cost_usd or 0.0)
        if sim >= SERVE and same_lang and _family(model) == _family(e.model):
            return TeamMatch("serve", round(sim, 4), True, e.solver, round(cost, 6))
        if sim >= WARM:
            return TeamMatch("warm_start", round(sim, 4), same_lang, e.solver,
                             round(cost * WARM_FRACTION, 6))
        return None

    def add(self, tenant, embedding, language, model, solver, cost_usd) -> None:
        if not embedding:
            return
        bucket = self._by_tenant.setdefault(tenant, [])
        bucket.append(_Entry(embedding, language, model, solver, max(0.0, cost_usd or 0.0)))
        if len(bucket) > _CAP:
            del bucket[0]
