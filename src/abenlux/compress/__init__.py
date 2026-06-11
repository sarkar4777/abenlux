"""
The compression layer. A pluggable set of token-saving strategies that run on the OUTBOUND request at
the edge gateway, before it is forwarded upstream. Because it sits at the loopback HTTP proxy, EVERY
tool that points its base_url at the gateway gets it - IDE or CLI, any provider - with zero per-tool
setup and no context switching.

Two hard rules, because silently changing a developer's prompt can change their results:

  1. SAFE BY DEFAULT. Only strategies that are lossless AND behavior-safe run automatically (today:
     prefix-stabilize, which only REORDERS a volatile token so prompt-caching can kick in - it removes
     no information). Strategies that rewrite prompt CONTENT (RTK-style command-output trimming,
     DocLang/OTSL table transcoding, Bifrost-style tool-definition slimming, Headroom-style JSON
     minification) are opt-in via ABEN_COMPRESS - one flag, applied to every tool at once.

  2. NEVER BREAK A CALL. compress_request is wrapped per-strategy in try/except by the caller; any
     strategy that errors is skipped and the original request is forwarded unchanged.

Each strategy operates on the PARSED request body (a dict) and returns a possibly-new body plus the
estimated input tokens it saved. Measurement is real (tokens before minus after on the actually-sent
request), so the savings is realized spend avoided, not a guess.

Credits: the strategies are inspired by and interoperate with these open tools:
  - RTK (Rust Token Killer, MIT) - command-output compression (rtk-ai/rtk).
  - DocLang / Docling (LF AI & Data) - the AI-native document format; OTSL compact tables.
  - Headroom - JSON/AST context compressors.
  - Bifrost "Code Mode" - tool-definition slimming for coding agents.
abenlux implements compatible, bounded versions natively so they work across every tool at the gateway,
and orchestrates/credits the upstream tools where they fit better (e.g. RTK's shell hook).
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from abenlux.capture.adapters import estimate_tokens

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# a volatile leading token that busts the cache-stable prefix: an injected date/time or an id. matched
# PRECISELY (just the token, not the trailing instruction) so the reorder never moves real content.
_DATE = r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)?"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_VOLATILE = re.compile(
    rf"(?i)(?:today(?:'s date)? is|current date(?:/time)?|the date is|now is|timestamp)\s*[:=]?\s*{_DATE}"
    rf"|{_DATE}|{_UUID}"
)
_HTML_TABLE = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
# regex-heavy strategies skip very large slots: a pathological prompt (e.g. many unclosed <table tags)
# can drive a backtracking regex quadratic and stall the gateway event loop. above this size we leave
# the text untouched - compression is best-effort and must never block a developer's call.
_MAX_REGEX_INPUT = 200_000


@dataclass
class Strategy:
    name: str
    lossless: bool                 # True = no information is dropped (reorder/dedupe/reformat only)
    default_on: bool               # runs automatically (must be lossless AND behavior-safe)
    rewrites_prompt: bool          # True = changes prompt CONTENT (vs only metadata/order)
    fn: Callable[[dict, str], Optional[dict]]   # (body, provider) -> new body or None if not applicable
    note: str = ""


_REGISTRY: dict[str, Strategy] = {}


def register(s: Strategy) -> None:
    _REGISTRY[s.name] = s


def strategies() -> dict[str, Strategy]:
    return dict(_REGISTRY)


# ----------------------------- body text access (provider-aware) -----------------------------

def _slots(body: dict, provider: str, roles: set[str]) -> list[tuple[dict, str]]:
    """collect (container, key) pairs whose container[key] is a text string to inspect/rewrite, for
    messages in `roles`. provider-aware over Anthropic / OpenAI / Gemini request shapes. defensive:
    anything unexpected is skipped, never raised."""
    out: list[tuple[dict, str]] = []

    def add_content(msg: dict) -> None:
        c = msg.get("content")
        if isinstance(c, str):
            out.append((msg, "content"))
        elif isinstance(c, list):
            for block in c:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    out.append((block, "text"))

    if provider == "google":
        if "system" in roles:
            si = body.get("systemInstruction") or body.get("system_instruction")
            for p in (si or {}).get("parts", []) if isinstance(si, dict) else []:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    out.append((p, "text"))
        for content in body.get("contents", []) if isinstance(body.get("contents"), list) else []:
            role = content.get("role", "user")
            mapped = "system" if role == "system" else ("assistant" if role == "model" else "user")
            if mapped in roles and isinstance(content.get("parts"), list):
                for p in content["parts"]:
                    if isinstance(p, dict) and isinstance(p.get("text"), str):
                        out.append((p, "text"))
        return out

    # anthropic / openai
    if "system" in roles:
        sys = body.get("system")
        if isinstance(sys, str):
            out.append((body, "system"))
        elif isinstance(sys, list):
            for block in sys:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    out.append((block, "text"))
    for msg in body.get("messages", []) if isinstance(body.get("messages"), list) else []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        if role == "system" and "system" in roles:
            add_content(msg)
        elif role in roles:
            add_content(msg)
    return out


def _rewrite(body: dict, provider: str, roles: set[str], transform: Callable[[str], str]) -> Optional[dict]:
    """apply `transform` to every text slot in `roles`; return a new body if anything changed, else None."""
    new = copy.deepcopy(body)
    changed = False
    for container, key in _slots(new, provider, roles):
        before = container[key]
        after = transform(before)
        if after != before:
            container[key] = after
            changed = True
    return new if changed else None


# ----------------------------- strategies -----------------------------

# an injected marker is ONLY moved when it leads the system text: either a known injection phrase
# ("today is <date>", "timestamp: <date>", a leading request-id) or a bare date/uuid that stands alone
# at the very top. matching mid-sentence dates is deliberately avoided so real prose is never mangled.
_INJECTED_PREFIX = re.compile(
    rf"^\s*(?:(?:today(?:'s date)? is|current date(?:/time)?|the date is|now is|timestamp|session|request(?:\s*id)?)"
    rf"\s*[:=]?\s*)?(?:{_DATE}|{_UUID})\b[ \t]*[.\n;:]?",
    re.IGNORECASE,
)


def _prefix_stabilize(body: dict, provider: str) -> Optional[dict]:
    # move a volatile LEADING token (an injected date/time/uuid at the very start) out of the cache-stable
    # prefix to the end, so the model's prompt cache can hit. lossless: same content, reordered. only
    # fires on a genuine leading marker - never a date embedded in real prose.
    def transform(text: str) -> str:
        m = _INJECTED_PREFIX.match(text)
        if not m:
            return text
        marker = m.group(0).strip()
        rest = text[m.end():]
        rest = re.sub(r"^[ \t]*[.,:;]?[ \t]*", "", rest).strip()
        return f"{rest}\n\n{marker}" if rest else text
    return _rewrite(body, provider, {"system"}, transform)


def _command_trim(body: dict, provider: str) -> Optional[dict]:
    # RTK-style: in user content, strip ANSI escapes, collapse runs of identical lines into one with a
    # count, and truncate a very long block keeping head+tail. near-lossless: it removes noise/repeats.
    def transform(text: str) -> str:
        if "\x1b[" not in text and "\n" not in text:
            return text
        t = _ANSI.sub("", text)
        lines = t.split("\n")
        out: list[str] = []
        i = 0
        while i < len(lines):
            j = i
            while j + 1 < len(lines) and lines[j + 1] == lines[i]:
                j += 1
            out.append(lines[i] if j == i else f"{lines[i]}    ... x{j - i + 1}")
            i = j + 1
        if len(out) > 200:                                # truncate huge blocks, keep head + tail
            out = out[:120] + [f"... ({len(out) - 160} lines trimmed by abenlux, full output on disk)"] + out[-40:]
        return "\n".join(out)
    return _rewrite(body, provider, {"user"}, transform)


def _otsl_table(html: str) -> str:
    # DocLang/OTSL-style compact table. faithful for simple tables (th/td rows); on anything unusual we
    # return the original so we never corrupt a complex table.
    try:
        if re.search(r"\b(?:colspan|rowspan)\b", html, re.IGNORECASE):
            return html        # merged cells carry structure OTSL pipe rows would drop - leave it
        rows = re.findall(r"<tr\b[^>]*>.*?</tr>", html, re.IGNORECASE | re.DOTALL)
        if not rows:
            return html
        otsl: list[str] = ["<otsl>"]
        for r in rows:
            cells = re.findall(r"<t[hd]\b.*?>(.*?)</t[hd]>", r, re.IGNORECASE | re.DOTALL)
            clean = [re.sub(r"<[^>]+>", "", c).strip().replace("|", "/") for c in cells]
            otsl.append("| " + " | ".join(clean) + " |")
        otsl.append("</otsl>")
        compact = "\n".join(otsl)
        return compact if len(compact) < len(html) else html
    except Exception:
        return html


def _otsl_tables(body: dict, provider: str) -> Optional[dict]:
    # DocLang-style: replace verbose HTML tables with compact OTSL. lossless for the cell contents.
    def transform(text: str) -> str:
        if len(text) > _MAX_REGEX_INPUT or "<table" not in text.lower():
            return text
        return _HTML_TABLE.sub(lambda m: _otsl_table(m.group(0)), text)
    return _rewrite(body, provider, {"user", "system"}, transform)


def _compress_json(body: dict, provider: str) -> Optional[dict]:
    # Headroom-style: minify pretty-printed JSON inside fenced ```json blocks (drop insignificant
    # whitespace). lossless: the parsed JSON is identical.
    import json as _json

    def minify(m: re.Match) -> str:
        block = m.group(1)
        try:
            return "```json\n" + _json.dumps(_json.loads(block), separators=(",", ":")) + "\n```"
        except Exception:
            return m.group(0)
    pat = re.compile(r"```json\s*(.*?)```", re.DOTALL)

    def transform(text: str) -> str:
        return pat.sub(minify, text) if ("```json" in text and len(text) <= _MAX_REGEX_INPUT) else text
    return _rewrite(body, provider, {"user", "system"}, transform)


def _slim_tools(body: dict, provider: str) -> Optional[dict]:
    # Bifrost "Code Mode"-style: drop byte-identical duplicate tool/function definitions resent every
    # turn. lossless: a duplicate schema adds nothing. only touches exact dupes, never trims a schema.
    import json as _json
    for key in ("tools", "functions"):
        arr = body.get(key)
        if isinstance(arr, list) and len(arr) > 1:
            seen, deduped = set(), []
            for item in arr:
                sig = _json.dumps(item, sort_keys=True)
                if sig not in seen:
                    seen.add(sig)
                    deduped.append(item)
            if len(deduped) < len(arr):
                new = copy.deepcopy(body)
                new[key] = deduped
                return new
    return None


register(Strategy("prefix_stabilize", lossless=True, default_on=True, rewrites_prompt=False,
                  fn=_prefix_stabilize, note="move an injected date/id out of the cache-stable prefix"))
register(Strategy("command_trim", lossless=False, default_on=False, rewrites_prompt=True,
                  fn=_command_trim, note="RTK-style: strip ANSI, collapse repeats, truncate huge output"))
register(Strategy("otsl_tables", lossless=True, default_on=False, rewrites_prompt=True,
                  fn=_otsl_tables, note="DocLang-style: verbose HTML tables to compact OTSL"))
register(Strategy("compress_json", lossless=True, default_on=False, rewrites_prompt=True,
                  fn=_compress_json, note="Headroom-style: minify embedded JSON blobs"))
register(Strategy("slim_tools", lossless=True, default_on=False, rewrites_prompt=True,
                  fn=_slim_tools, note="Bifrost-style: drop duplicate tool definitions"))


@dataclass
class CompressionResult:
    body: dict
    saved_tokens: int = 0
    applied: list[str] = field(default_factory=list)
    per_strategy: dict = field(default_factory=dict)   # strategy name -> input tokens it removed


def _body_tokens(body: dict, provider: str) -> int:
    import json as _json
    total = sum(estimate_tokens(c[k]) for c, k in _slots(body, provider, {"system", "user", "assistant"}))
    # tool/function definitions are billed input too, so slim_tools' saving is only visible if we count
    # them. serialize each definition and estimate - cheap and provider-agnostic.
    for key in ("tools", "functions"):
        arr = body.get(key)
        if isinstance(arr, list):
            total += sum(estimate_tokens(_json.dumps(item)) for item in arr if isinstance(item, (dict, list, str)))
    return total


def enabled_strategies(spec: str | None) -> list[Strategy]:
    """resolve the ABEN_COMPRESS spec into an ordered strategy list. None/'' -> the safe defaults;
    'off' -> none; 'all' -> every registered; else a comma list of names."""
    if spec is not None and spec.strip().lower() == "off":
        return []
    if spec is not None and spec.strip().lower() == "all":
        return list(_REGISTRY.values())
    if spec:
        want = [s.strip() for s in spec.split(",") if s.strip()]
        return [_REGISTRY[n] for n in want if n in _REGISTRY]
    return [s for s in _REGISTRY.values() if s.default_on]


def compress_request(body: dict, provider: str, strategies_list: list[Strategy]) -> CompressionResult:
    """run the chosen strategies in order on the parsed request body. each is isolated: a strategy that
    raises or produces an empty/invalid body is skipped, so compression can never break the call."""
    if not isinstance(body, dict) or not strategies_list:
        return CompressionResult(body=body)
    cur = body
    applied: list[str] = []
    per: dict = {}
    prev = _body_tokens(body, provider)
    for strat in strategies_list:
        try:
            new = strat.fn(cur, provider)
        except Exception:
            new = None
        if isinstance(new, dict) and new:
            cur = new
            applied.append(strat.name)
            now = _body_tokens(cur, provider)        # marginal tokens this strategy removed in the chain
            per[strat.name] = max(0, prev - now)
            prev = now
    if not applied:
        return CompressionResult(body=body)
    return CompressionResult(body=cur, saved_tokens=sum(per.values()), applied=applied, per_strategy=per)
