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
import threading
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
    # the embedding is computed over the prompt's KEYPHRASES (domain terms, see salience.keyphrases),
    # which separates same-task from different-task cleanly. same objective -> a topic-overlap bar is
    # enough; a DIFFERENT objective needs a stricter bar, because cross-objective overlap is more often
    # coincidental than a genuine shared problem. both gates use only content-free fields (embedding +
    # objective label). these defaults are tuned for the offline keyphrase-hashing embedder; a semantic
    # embedder ([ml] extra) produces a different cosine scale - override via the constructor if used.
    threshold: float = 0.40            # same objective (or the reusable solved-pattern corpus)
    cross_threshold: float = 0.55      # different objective: a stronger topic match required
    max_signals: int = 5000            # bound memory for a central broker across many developers
    signals: list[TopicSignal] = field(default_factory=list)
    _consents: set[tuple[str, str]] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def _wall_ok(self, x: TopicSignal, y: TopicSignal) -> bool:
        # never match across different clients, never across residency boundaries
        if x.client and y.client and x.client != y.client:
            return False
        return x.residency == y.residency

    def submit(self, sig: TopicSignal) -> list[Match]:
        with self._lock:                                  # mutated from concurrent capture threads
            return self._submit_locked(sig)

    def _submit_locked(self, sig: TopicSignal) -> list[Match]:
        matches: list[Match] = []
        for other in self.signals:
            if other.actor_pseudonym == sig.actor_pseudonym or not self._wall_ok(sig, other):
                continue
            sim = cosine(sig.topic_embedding, other.topic_embedding)
            solved = other.is_solved or sig.is_solved
            same_obj = bool(sig.topic_label and other.topic_label and sig.topic_label == other.topic_label)
            bar = self.threshold if (same_obj or solved) else self.cross_threshold
            if sim >= bar:
                mode = "solved_reuse" if solved else "live_duplication"
                matches.append(
                    Match(sig.actor_pseudonym, other.actor_pseudonym, round(sim, 3), other.topic_label, mode)
                )
        # keep only the actor's latest signal, and bound total memory for a long-lived central broker
        self.signals = [s for s in self.signals if s.actor_pseudonym != sig.actor_pseudonym]
        self.signals.append(sig)
        if len(self.signals) > self.max_signals:
            self.signals = self.signals[-self.max_signals:]
        return matches

    def record_consent(self, actor: str, peer: str) -> None:
        self._consents.add((actor, peer))

    def mutually_consented(self, a: str, b: str) -> bool:
        return (a, b) in self._consents and (b, a) in self._consents
