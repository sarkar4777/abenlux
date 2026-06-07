"""
Privacy plane. Two invariants the whole product hangs on:

  1. Identity is one-way hashed at the edge. The raw actor id (email, machine user)
     becomes a stable HMAC pseudonym. Only the pseudonym + a separately-governed
     role/team/objective mapping crosses into analytics. Names and content never do.

  2. Management-facing aggregates enforce k-anonymity (default k>=5) and add
     differential-privacy noise to cross-team rollups. Individual rows are visible
     only to the individual.

The HMAC key lives in a secret store the analytics plane cannot read, so pseudonyms
can't be reversed by dictionary attack on known emails from inside analytics.
"""
from __future__ import annotations

import hashlib
import hmac
import random
from dataclasses import dataclass


def pseudonymize(raw_actor: str, hmac_key: bytes, *, salt: str = "abenlux.v1") -> str:
    """Stable, non-reversible pseudonym for an actor. Same input -> same output, so
    longitudinal patterns work, but the mapping back requires the secret key."""
    mac = hmac.new(hmac_key, f"{salt}:{raw_actor}".encode("utf-8"), hashlib.sha256)
    return "px_" + mac.hexdigest()[:20]


def strip_raw_actor_inplace(event, hmac_key: bytes) -> None:
    """Replace the raw actor with a pseudonym and drop the raw value. Call before persist."""
    if event.actor_raw:
        event.actor_pseudonym = pseudonymize(event.actor_raw, hmac_key)
        event.actor_raw = None


@dataclass
class KAnonymityGate:
    """Gate any aggregate before it reaches a management view."""

    k: int = 5
    dp_epsilon: float = 1.0  # smaller = more noise = more privacy

    def allows(self, distinct_actors: int) -> bool:
        return distinct_actors >= self.k

    def laplace_noise(self, sensitivity: float = 1.0) -> float:
        """Laplace mechanism for (epsilon)-DP on a count/sum aggregate."""
        scale = sensitivity / self.dp_epsilon
        u = random.random() - 0.5
        return -scale * (1 if u >= 0 else -1) * _safe_log(1 - 2 * abs(u))

    def noisy_count(self, value: float, distinct_actors: int) -> float | None:
        """Return a DP-noised aggregate, or None if it fails the k-threshold (suppress)."""
        if not self.allows(distinct_actors):
            return None
        return round(value + self.laplace_noise(sensitivity=1.0), 2)


def _safe_log(x: float) -> float:
    import math
    return math.log(max(x, 1e-12))
