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

import hashlib
from collections import OrderedDict
from dataclasses import dataclass

from abenlux.capture.adapters import estimate_tokens


def _content(m) -> str:
    return getattr(m, "content", "") or ""


def _fingerprint(m) -> tuple[str, bytes, int]:
    """role + a content HASH + length. this is what the tracker retains across turns instead of the
    raw message, so no pre-redaction prompt text (the diff is computed before redaction runs) is ever
    held in memory beyond the in-flight call. the digest is enough for exact-prefix identity."""
    c = _content(m)
    return (getattr(m, "role", ""), hashlib.blake2b(c.encode("utf-8", "replace"), digest_size=16).digest(), len(c))


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


def unchanged_prefix_chars(prev_fps: list, curr: list) -> int:
    """length in characters of the leading run of messages whose role+content matches the stored
    fingerprints. `prev_fps` are (role, hash, len) tuples; `curr` are live messages. exact-prefix,
    so it never claims novel context is resent."""
    chars = 0
    for (role, h, _ln), b in zip(prev_fps, curr):
        bc = _content(b)
        bh = hashlib.blake2b(bc.encode("utf-8", "replace"), digest_size=16).digest()
        if role == getattr(b, "role", "") and h == bh:
            chars += len(bc)
        else:
            break
    return chars


@dataclass
class SessionHistoryTracker:
    """per-actor previous-request memory, bounded. one instance lives in the gateway. it retains only
    content FINGERPRINTS (role + hash + length), never raw message text - the resent-history diff runs
    on in-flight pre-redaction messages, so storing copies would keep redacted-away secrets in process
    memory across turns. fingerprints give the same exact-prefix detection with nothing to leak."""

    max_sessions: int = 2048
    _prev: "OrderedDict[str, list]" = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._prev = OrderedDict()

    def duplicate_history_tokens(self, session_key: str, messages: list) -> int:
        prev = self._prev.get(session_key)
        dup = 0
        if prev:
            dup = estimate_tokens_from_chars(unchanged_prefix_chars(prev, messages))
        self._prev[session_key] = [_fingerprint(m) for m in messages]  # fingerprints only, no raw text
        self._prev.move_to_end(session_key)
        while len(self._prev) > self.max_sessions:
            self._prev.popitem(last=False)  # evict oldest
        return dup


def estimate_tokens_from_chars(chars: int) -> int:
    # mirror adapters.estimate_tokens (~4 chars/token) without re-joining text
    return estimate_tokens("x" * chars) if chars else 0
