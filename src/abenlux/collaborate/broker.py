"""
Collaboration broker. Two token-saving mechanisms, both peer-to-peer and double-blind -
never management-mediated (a 'these two duplicate work' report handed to a manager becomes
an efficiency-policing weapon).

  1. live duplication   - two actors working on semantically similar problems right now.
  2. solved-knowledge   - someone starting on a problem the org already solved, surface
                          the existing (redacted, generalized) pattern. Reuse > re-solve,
                          and it's async, which is what a global team actually needs.

Two hard walls enforced here:
  * privacy - matching needs identity, governance demands anonymity. Resolved by a
    double-blind handshake: reveal identities only on MUTUAL opt-in.
  * client confidentiality - matching runs on ABSTRACTED topic embeddings (technique /
    pattern level), and never crosses an engagement Chinese wall or data-residency boundary.
    The shareable asset is the generalizable pattern, not the client deliverable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class TopicSignal:
    """An ABSTRACTED unit of work - never raw content. `client` + `residency` enforce
    the Chinese wall."""

    actor_pseudonym: str
    topic_embedding: list[float]
    topic_label: str            # generalized, e.g. "Temporal saga for approval workflow"
    client: Optional[str] = None
    residency: str = "eu"
    is_solved: bool = False     # part of the reusable solved-pattern corpus


@dataclass
class Match:
    a: str
    b: str
    similarity: float
    topic: str
    mode: str                   # "live_duplication" | "solved_reuse"


@dataclass
class CollaborationBroker:
    threshold: float = 0.82
    signals: list[TopicSignal] = field(default_factory=list)
    _consents: set[tuple[str, str]] = field(default_factory=set)

    def _wall_ok(self, x: TopicSignal, y: TopicSignal) -> bool:
        # never match across different clients, never across residency boundaries
        if x.client and y.client and x.client != y.client:
            return False
        return x.residency == y.residency

    def submit(self, sig: TopicSignal) -> list[Match]:
        matches: list[Match] = []
        for other in self.signals:
            if other.actor_pseudonym == sig.actor_pseudonym or not self._wall_ok(sig, other):
                continue
            sim = cosine(sig.topic_embedding, other.topic_embedding)
            if sim >= self.threshold:
                mode = "solved_reuse" if (other.is_solved or sig.is_solved) else "live_duplication"
                matches.append(
                    Match(sig.actor_pseudonym, other.actor_pseudonym, round(sim, 3), other.topic_label, mode)
                )
        self.signals.append(sig)
        return matches

    def record_consent(self, actor: str, peer: str) -> None:
        self._consents.add((actor, peer))

    def mutually_consented(self, a: str, b: str) -> bool:
        return (a, b) in self._consents and (b, a) in self._consents
