"""
Waste detection. The developer-facing, *leading* signals - fired mid-session before the
tokens pile up, surfaced ONLY to the developer (never to a manager-visible log).

Critical design rule established for this product: nudge only on *mechanical* waste
where there's no judgment call. We do NOT flag "deviation from your pattern" - productive
deep work and unproductive flailing look identical in telemetry, and punishing the former
kills the exploration that produces innovation.

Mechanical signals implemented here:
  1. retry loops      - near-verbatim successive prompts ("fighting the model")
  2. context bloat    - the same large history resent unchanged every turn
  3. answered-already - re-asking something already answered this session
  4. routing hint     - trivial calls that a smaller/cheaper model would serve

Similarity defaults to a dependency-free normalized measure (token Jaccard blended with
difflib ratio) so the scaffold runs offline. A real deployment injects a sentence-embedding
backend via `similarity_fn` for semantic (not lexical) matching.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Callable

_WORD = re.compile(r"\w+")


def _tokens(s: str) -> set[str]:
    return set(_WORD.findall(s.lower()))


def lexical_similarity(a: str, b: str) -> float:
    """Blend of token Jaccard and sequence ratio. 0..1. Offline default."""
    if not a or not b:
        return 0.0
    ta, tb = _tokens(a), _tokens(b)
    jacc = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    return 0.5 * jacc + 0.5 * seq


SimilarityFn = Callable[[str, str], float]


@dataclass
class WasteSignal:
    kind: str               # "retry_loop" | "context_bloat" | "answered_already" | "routing_hint"
    severity: str           # "info" | "warn"
    similarity: float = 0.0
    detail: str = ""
    suggestion: str = ""    # developer-facing, ambient copy
    recoverable_tokens: int = 0


@dataclass
class SessionWasteMonitor:
    """Per-developer, per-session, in-memory. Lives on the device. Nothing here is
    persisted to the analytics plane except the boolean/score on the DerivedRecord."""

    retry_threshold: float = 0.92
    context_bloat_ratio: float = 0.6     # >60% of input is unchanged resent history
    similarity_fn: SimilarityFn = lexical_similarity
    cheap_model_token_ceiling: int = 350  # below this, a small model usually suffices
    max_history: int = 100               # bound memory + keep answered-already O(window), not O(session)
    cache_min_resent: int = 2000         # only nudge on caching when the uncached resend is material

    _prompts: list[str] = field(default_factory=list)
    _answers: list[str] = field(default_factory=list)

    def observe(self, event) -> list[WasteSignal]:
        signals: list[WasteSignal] = []
        prompt = event.input_text()
        answer = event.output_text()

        # 1. retry loop - compare to recent prompts
        for prev in reversed(self._prompts[-4:]):
            sim = self.similarity_fn(prompt, prev)
            if sim >= self.retry_threshold:
                event.is_retry_loop = True if hasattr(event, "is_retry_loop") else None
                signals.append(
                    WasteSignal(
                        kind="retry_loop",
                        severity="warn",
                        similarity=round(sim, 3),
                        detail="near-identical to a prompt sent moments ago",
                        suggestion=(
                            "This looks close to your last try. Re-running the same prompt "
                            "rarely changes the answer - want to add a failing test, an error "
                            "message, or a constraint instead?"
                        ),
                        recoverable_tokens=event.usage.input_tokens,
                    )
                )
                break

        # 2. resent unchanged history. the RIGHT fix depends on whether it is being cached:
        #    - cached already  -> nothing to do, the resend is cheap (cache read ~0.1x input)
        #    - NOT cached      -> enable prompt caching: identical context, billed as a cache hit.
        #      this is the only token-saving lever with zero loss of accuracy or detail, so we
        #      prefer it over "trim your history" (which risks dropping context the model needed).
        dup = event.duplicate_history_tokens
        cache_read = getattr(event.usage, "cache_read_tokens", 0)
        if event.usage.input_tokens and dup:
            ratio = dup / max(event.usage.input_tokens, 1)
            uncached_resent = max(0, dup - cache_read)
            if uncached_resent >= self.cache_min_resent and cache_read < dup * 0.5:
                signals.append(
                    WasteSignal(
                        kind="cache_inefficiency",
                        severity="warn",
                        detail=f"{uncached_resent:,} tokens of resent history are not being cached",
                        suggestion=(
                            "You're resending context that isn't being cached. Turning on prompt "
                            "caching keeps the exact same context but bills it as a cache read - "
                            "real savings with zero loss of detail."
                        ),
                        recoverable_tokens=uncached_resent,
                    )
                )
            elif ratio >= self.context_bloat_ratio and cache_read < dup * 0.5:
                signals.append(
                    WasteSignal(
                        kind="context_bloat",
                        severity="info",
                        detail=f"{int(ratio*100)}% of input is resent, unchanged history",
                        suggestion=(
                            "A large chunk of this request is history you've sent before. "
                            "Caching it (or trimming what the model no longer needs) cuts input cost."
                        ),
                        recoverable_tokens=uncached_resent or dup,
                    )
                )

        # 3. answered already this session
        for prev_q, prev_a in zip(self._prompts, self._answers):
            if prev_a and self.similarity_fn(prompt, prev_q) >= 0.85:
                signals.append(
                    WasteSignal(
                        kind="answered_already",
                        severity="info",
                        detail="similar question was answered earlier this session",
                        suggestion="You asked something close to this earlier - scroll up, it may already be solved.",
                        recoverable_tokens=event.usage.total,
                    )
                )
                break

        # 4. routing hint - small/cheap model likely sufficient
        if 0 < event.usage.total <= self.cheap_model_token_ceiling and not signals:
            signals.append(
                WasteSignal(
                    kind="routing_hint",
                    severity="info",
                    detail="small, low-complexity call",
                    suggestion="(auto) routed to a smaller model - no action needed.",
                    recoverable_tokens=0,
                )
            )

        self._prompts.append(prompt)
        self._answers.append(answer)
        if len(self._prompts) > self.max_history:  # bound memory on very long sessions
            self._prompts = self._prompts[-self.max_history:]
            self._answers = self._answers[-self.max_history:]
        return signals
