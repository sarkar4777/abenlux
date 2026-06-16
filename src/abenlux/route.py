"""model routing. an easy request goes to a cheaper model, a hard one stays on the strong model.

the decision runs on the device from the request body alone (no extra model call). it is conservative
on purpose, it only routes down when the request clearly looks small and low risk, so a real piece of
work is never quietly handled by a weaker model.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

# cheaper model per provider. override the whole map with ABEN_ROUTE_MAP as provider=model pairs.
_CHEAP = {"anthropic": "claude-haiku-4-5", "openai": "gpt-4o-mini", "google": "gemini-2.5-flash"}

# strong tiers we are willing to route down from
_STRONG = ("opus", "sonnet", "gpt-4o", "gpt-4.1", "gpt-5", "gemini-2.5-pro")

# phrases that mark a small, low risk request
_EASY = ("rename", "typo", "spelling", "format", "lint", "gofmt", "prettier", "reindent",
         "add a comment", "docstring", "one line", "one-line", "fix the import", "add a test for",
         "what does this", "explain this line", "bump the version")


@dataclass
class RouteDecision:
    original: str
    target: Optional[str] = None     # cheaper model, or None to keep the original
    reason: str = ""


def _route_map() -> dict:
    raw = os.getenv("ABEN_ROUTE_MAP")
    if not raw:
        return _CHEAP
    out = dict(_CHEAP)
    for pair in raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def cheap_for(provider: str) -> Optional[str]:
    return _route_map().get(provider)


def _last_user_text(body: dict, provider: str) -> str:
    try:
        if provider == "google":
            for c in reversed(body.get("contents") or []):
                if c.get("role", "user") == "user":
                    return " ".join(p.get("text", "") for p in c.get("parts", []) if isinstance(p, dict))
            return ""
        for m in reversed(body.get("messages") or []):
            if m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    return " ".join(b.get("text", "") for b in c if isinstance(b, dict))
        return ""
    except Exception:
        return ""


def _has_tools(body: dict) -> bool:
    return bool(body.get("tools") or body.get("functions"))


def difficulty(body: dict, provider: str) -> float:
    # 0 easy, 1 hard
    if _has_tools(body):
        return 1.0
    if len(json.dumps(body)) > 8000:
        return 1.0
    text = _last_user_text(body, provider).lower()
    score = 0.5
    if any(h in text for h in _EASY):
        score -= 0.4
    if len(text) < 240:
        score -= 0.2
    try:
        if int(body.get("max_tokens") or body.get("max_output_tokens") or 1024) <= 64:
            score -= 0.2
    except (TypeError, ValueError):
        pass
    return max(0.0, min(1.0, score))


def decide(body: dict, provider: str) -> RouteDecision:
    if not isinstance(body, dict):
        return RouteDecision(original="")
    original = body.get("model") or ""
    if not original or not any(s in original.lower() for s in _STRONG):
        return RouteDecision(original=original)
    target = _route_map().get(provider)
    if not target or target.lower() in original.lower():
        return RouteDecision(original=original)
    if difficulty(body, provider) <= 0.15:
        return RouteDecision(original=original, target=target, reason="easy request")
    return RouteDecision(original=original)


def saving_usd(original: str, target: str, input_tokens: int, output_tokens: int,
               cache_read: int = 0, cache_creation: int = 0) -> float:
    # dollars between running the original model and the cheaper one on the same tokens
    from abenlux.pricing import cost_usd
    a = cost_usd(original, input_tokens, output_tokens,
                 cache_read_tokens=cache_read, cache_creation_tokens=cache_creation).total
    b = cost_usd(target, input_tokens, output_tokens,
                 cache_read_tokens=cache_read, cache_creation_tokens=cache_creation).total
    return max(0.0, round(a - b, 6))
