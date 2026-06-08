"""
Tier-1 ingest: an OTLP/HTTP receiver for tools that self-instrument to the OpenTelemetry
GenAI semantic conventions (Claude Code, Codex, Gemini CLI, Copilot agent). The tool is
configured with OTEL_EXPORTER_OTLP_ENDPOINT pointing here, we parse gen_ai.* attributes into
the same CanonicalEvent the gateway produces, so downstream is identical.

Two transports, because tools split content differently:
  * TRACES - usage + (opt-in) message content as span attributes. Codex, Gemini CLI, Copilot.
  * LOGS   - Claude Code emits message content as OTLP *log records* (the GenAI "events"
             semconv), not span attributes. Parsing only traces would silently drop Claude
             Code's prompts. We parse both and normalize identically.

Maps (semconv, gen_ai_latest_experimental):
  gen_ai.provider.name / gen_ai.system     -> provider
  gen_ai.request.model                     -> request_model
  gen_ai.response.model                    -> response_model
  gen_ai.usage.input_tokens                -> usage.input_tokens
  gen_ai.usage.output_tokens               -> usage.output_tokens
  gen_ai.response.finish_reasons           -> finish_reasons
  gen_ai.input.messages / .output.messages -> messages (list OR json string, content capture)
"""
from __future__ import annotations

import json
from typing import Any

from abenlux.schema import CanonicalEvent, CaptureTier, Message, Provider, Usage

_PROVIDER_MAP = {
    "anthropic": Provider.ANTHROPIC,
    "openai": Provider.OPENAI,
    "azure.ai.openai": Provider.AZURE_OPENAI,
    "az.ai.openai": Provider.AZURE_OPENAI,
    "gcp.gen_ai": Provider.GOOGLE,
    "gcp.gemini": Provider.GOOGLE,
    "google": Provider.GOOGLE,
    "aws.bedrock": Provider.AWS_BEDROCK,
}


def _attr_value(v: dict[str, Any]) -> Any:
    """Unwrap an OTLP AnyValue, including nested kvlist/array (log bodies use these)."""
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        return int(v["intValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "boolValue" in v:
        return v["boolValue"]
    if "arrayValue" in v:
        return [_attr_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return {kv["key"]: _attr_value(kv["value"]) for kv in v["kvlistValue"].get("values", [])}
    return None


def _attrs(node: dict) -> dict[str, Any]:
    return {a["key"]: _attr_value(a["value"]) for a in node.get("attributes", [])}


def _coerce_messages(val: Any) -> list[dict]:
    """messages arrive as a list of kvlist dicts OR a JSON string, normalize to dicts."""
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except (json.JSONDecodeError, ValueError):
            return []
    return [m for m in val if isinstance(m, dict)] if isinstance(val, list) else []


def _flatten_content(content: Any) -> str:
    """message content may be a plain string or a list of typed parts (semconv 'parts')."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict):
                out.append(p.get("content") or p.get("text") or "")
            elif isinstance(p, str):
                out.append(p)
        return "".join(out)
    return ""


def _event_from_attrs(a: dict[str, Any]) -> CanonicalEvent | None:
    if not any(k.startswith("gen_ai.") for k in a):
        return None
    provider_name = (a.get("gen_ai.provider.name") or a.get("gen_ai.system") or "").lower()
    ev = CanonicalEvent(
        tier=CaptureTier.OTEL_NATIVE,
        provider=_PROVIDER_MAP.get(provider_name, Provider.UNKNOWN),
        operation=a.get("gen_ai.operation.name", "chat"),
        request_model=a.get("gen_ai.request.model"),
        response_model=a.get("gen_ai.response.model"),
        usage=Usage(
            input_tokens=int(a.get("gen_ai.usage.input_tokens", 0) or 0),
            output_tokens=int(a.get("gen_ai.usage.output_tokens", 0) or 0),
            cache_read_tokens=int(a.get("gen_ai.usage.cache_read_input_tokens", 0) or 0),
            cache_creation_tokens=int(a.get("gen_ai.usage.cache_creation_input_tokens", 0) or 0),
        ),
        finish_reasons=a.get("gen_ai.response.finish_reasons") or [],
    )
    for m in _coerce_messages(a.get("gen_ai.input.messages")):
        ev.content_captured = True
        ev.messages.append(Message(role=m.get("role", "user"), content=_flatten_content(m.get("content", ""))))
    for m in _coerce_messages(a.get("gen_ai.output.messages")):
        ev.output_messages.append(Message(role=m.get("role", "assistant"), content=_flatten_content(m.get("content", ""))))
    return ev


def event_from_genai_span(span: dict) -> CanonicalEvent | None:
    return _event_from_attrs(_attrs(span))


def event_from_genai_log(record: dict) -> CanonicalEvent | None:
    """a log record carries gen_ai.* in attributes and/or a kvlist body. merge both."""
    a = _attrs(record)
    body = record.get("body")
    if isinstance(body, dict):
        unwrapped = _attr_value(body)
        if isinstance(unwrapped, dict):
            for k, v in unwrapped.items():
                a.setdefault(k, v)
    return _event_from_attrs(a)


def event_from_claude_code_log(record: dict) -> CanonicalEvent | None:
    """Claude Code self-instruments with its OWN telemetry, not the gen_ai semconv: an
    `api_request` log event carrying bare input_tokens / output_tokens / cache_read_tokens /
    cache_creation_tokens / model attributes (the token usage METRIC repeats the same numbers,
    so we parse only the log to avoid double counting). Without this, real Claude Code is not
    captured at all. user.id (already a hash) is the actor, user.email (raw PII) is dropped."""
    a = _attrs(record)
    body = record.get("body")
    name = a.get("event.name") or (body.get("stringValue") if isinstance(body, dict) else None)
    if name not in ("api_request", "claude_code.api_request"):
        return None
    if "input_tokens" not in a and "output_tokens" not in a:
        return None
    ev = CanonicalEvent(
        tier=CaptureTier.OTEL_NATIVE,
        provider=Provider.ANTHROPIC,                       # Claude Code calls Anthropic models
        operation="chat",
        request_model=a.get("model"),
        response_model=a.get("model"),
        usage=Usage(
            input_tokens=int(a.get("input_tokens", 0) or 0),
            output_tokens=int(a.get("output_tokens", 0) or 0),
            cache_read_tokens=int(a.get("cache_read_tokens", 0) or 0),
            cache_creation_tokens=int(a.get("cache_creation_tokens", 0) or 0),
        ),
    )
    ev.work.tool = "claude-code"
    ev.work.app_category = "cli"
    ev.actor_raw = a.get("user.id")                        # a hash, never the email
    return ev


def events_from_otlp_traces(payload: dict) -> list[CanonicalEvent]:
    events: list[CanonicalEvent] = []
    for rs in payload.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                ev = event_from_genai_span(span)
                if ev:
                    events.append(ev)
    return events


def events_from_otlp_logs(payload: dict) -> list[CanonicalEvent]:
    events: list[CanonicalEvent] = []
    for rl in payload.get("resourceLogs", []):
        for sl in rl.get("scopeLogs", []):
            for rec in sl.get("logRecords", []):
                ev = event_from_genai_log(rec) or event_from_claude_code_log(rec)
                if ev:
                    events.append(ev)
    return events


def events_from_otlp(payload: dict) -> list[CanonicalEvent]:
    """dispatch on whichever signal the export body carries (traces or logs)."""
    if "resourceSpans" in payload:
        return events_from_otlp_traces(payload)
    if "resourceLogs" in payload:
        return events_from_otlp_logs(payload)
    return []
