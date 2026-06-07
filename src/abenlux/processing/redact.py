"""
Edge redaction. Runs on the device BEFORE any content is written anywhere or used to
derive signals. This is non-negotiable: prompts here carry client material under NDA
(Shell / UMB / ENI etc.) and frequently contain credentials. Capturing those to any
store - even transiently - is the failure mode that turns an observability product into
a credential-exfiltration pipeline.

Two passes:
  1. High-confidence structured secrets (API keys, tokens, private keys, connection
     strings) via patterns + Shannon-entropy gate on long tokens.
  2. PII (emails, IPs, phone-ish, common national-ID shapes).

Redaction is destructive by design: the original substring is replaced by a typed
placeholder, e.g. <REDACTED:aws_key>. We never keep the original.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass


# ----- secret / credential signatures ----------------------------------------
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret", re.compile(r"(?i:\baws_secret_access_key\b)\s*[:=]\s*\S+")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]+?-----END[^-]+-----")),
    ("bearer", re.compile(r"(?i:\bbearer)\s+[A-Za-z0-9._-]{16,}")),
    ("conn_string", re.compile(r"\b[a-z]+://[^\s:@/]+:[^\s:@/]+@[^\s/]+")),
    ("password_kv", re.compile(r"(?i:\b(?:password|passwd|pwd|secret|api[_-]?key)\b\s*[:=]\s*\S+)")),
]

# ----- PII --------------------------------------------------------------------
_PII: list[tuple[str, re.Pattern]] = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    ("phone", re.compile(r"\b\+?\d[\d\s().-]{7,}\d\b")),
]

# long opaque tokens with high entropy that escaped the named patterns
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9+/_=-]{32,}\b")
_ENTROPY_THRESHOLD = 4.0  # bits/char, random base64 ~6, English prose ~2.5-3.2


@dataclass
class RedactionReport:
    text: str
    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


def redact(text: str) -> RedactionReport:
    if not text:
        return RedactionReport(text="", counts={})
    counts: dict[str, int] = {}

    def _sub(label: str, pattern: re.Pattern, s: str) -> str:
        def repl(_m: re.Match) -> str:
            counts[label] = counts.get(label, 0) + 1
            return f"<REDACTED:{label}>"
        return pattern.sub(repl, s)

    # pass 1: structured secrets (run private_key first so multi-line blocks go whole)
    ordered = sorted(_PATTERNS, key=lambda p: 0 if p[0] == "private_key" else 1)
    for label, pat in ordered:
        text = _sub(label, pat, text)

    # pass 2: high-entropy leftovers
    def entropy_repl(m: re.Match) -> str:
        tok = m.group(0)
        if shannon_entropy(tok) >= _ENTROPY_THRESHOLD:
            counts["high_entropy"] = counts.get("high_entropy", 0) + 1
            return "<REDACTED:high_entropy>"
        return tok
    text = _LONG_TOKEN.sub(entropy_repl, text)

    # pass 3: PII
    for label, pat in _PII:
        text = _sub(label, pat, text)

    return RedactionReport(text=text, counts=counts)


def redact_event_inplace(event) -> int:
    """Redact every message body on a CanonicalEvent in place. Returns total redactions.
    Marks each message `redacted=True`. Call this BEFORE derive()/persist()."""
    total = 0
    for m in list(event.messages) + list(event.output_messages):
        if m.content:
            rep = redact(m.content)
            m.content = rep.text
            m.redacted = True
            total += rep.total
    return total
