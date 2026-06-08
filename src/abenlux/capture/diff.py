"""
Resent-history detection. The single most actionable token-waste signal in agentic tools
is the same conversation prefix shipped on every turn: a 30-message session resends turns
1..29 to ask turn 30. waste.py already knows how to *react* to `duplicate_history_tokens`,
but nothing was *computing* it - the field was inert. This closes that gap.

The gateway sees raw requests in order, per actor. We keep the previous request's message
list per session key and measure how much of the new request is an unchanged prefix of the
old one. We report it in tokens using the same ~4 char/token heuristic the adapters use, so
the ratio in waste.py is apples-to-apples with reported input tokens.

This is deliberately prefix-based, not a fuzzy diff: chat transports replay an exact prefix,
and an exact-prefix measure has no false positives (it never claims novel context is bloat).
Bounded memory: one previous request per session, capped session count, content never stored
beyond the in-flight compare and never persisted.
"""
from __future__ import annotations

import copy
import hashlib
from collections import OrderedDict
from dataclasses import dataclass

from abenlux.capture.adapters import estimate_tokens


def _msg_key(m) -> str:
    # role + content identity, content here is pre-redaction in-flight text
    return f"{getattr(m, 'role', '')}\x00{getattr(m, 'content', '')}"


def conversation_key(actor: str, provider: str, repo: str | None, messages: list) -> str:
    """a session key that isolates concurrent conversations. anchored on the FIRST user message
    (the task), which is stable across a conversation's turns but distinct between conversations -
    so two agentic sessions on the same provider/repo do not thrash one shared baseline. system
    prompts are skipped because tools often send the same one for every conversation."""
    anchor = ""
    for m in messages:
        if getattr(m, "role", "") == "user" and (getattr(m, "content", "") or ""):
            anchor = m.content[:200]
            break
    h = hashlib.blake2b(anchor.encode("utf-8", "replace"), digest_size=6).hexdigest()
    return f"{actor}:{provider}:{repo or '-'}:{h}"


def unchanged_prefix_chars(prev: list, curr: list) -> int:
    """length in characters of the leading run of messages identical in both lists."""
    chars = 0
    for a, b in zip(prev, curr):
        if _msg_key(a) == _msg_key(b):
            chars += len(getattr(b, "content", "") or "")
        else:
            break
    return chars


@dataclass
class SessionHistoryTracker:
    """per-actor previous-request memory, bounded. one instance lives in the gateway."""

    max_sessions: int = 2048
    _prev: "OrderedDict[str, list]" = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._prev = OrderedDict()

    def duplicate_history_tokens(self, session_key: str, messages: list) -> int:
        prev = self._prev.get(session_key)
        dup = 0
        if prev:
            dup = estimate_tokens_from_chars(unchanged_prefix_chars(prev, messages))
        # snapshot COPIES of the messages: the pipeline wipes the originals' content after
        # derivation, so storing references would blank our baseline for the next turn.
        self._prev[session_key] = [copy.copy(m) for m in messages]
        self._prev.move_to_end(session_key)
        while len(self._prev) > self.max_sessions:
            self._prev.popitem(last=False)  # evict oldest
        return dup


def estimate_tokens_from_chars(chars: int) -> int:
    # mirror adapters.estimate_tokens (~4 chars/token) without re-joining text
    return estimate_tokens("x" * chars) if chars else 0
