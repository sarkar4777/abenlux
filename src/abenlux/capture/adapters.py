"""
Provider wire-format adapters.

The hard, easy-to-get-wrong part of Tier-2 capture: model APIs stream responses as
Server-Sent Events with *provider-specific* schemas, and token usage is reported in
different places mid-stream. These adapters consume the raw SSE byte stream (exactly
what the loopback proxy sees) and reconstruct a `CanonicalEvent` with correct token
counts and reassembled text.

Accurate to the wire formats as of 2026:

ANTHROPIC  (/v1/messages, stream=true)
  event: message_start      data: {"message":{"model":..,"usage":{"input_tokens":N,"output_tokens":1}}}
  event: content_block_delta data: {"delta":{"type":"text_delta","text":"..."}}
  event: message_delta      data: {"delta":{"stop_reason":".."},"usage":{"output_tokens":M}}
  event: message_stop
  -> input_tokens come in message_start, the AUTHORITATIVE output_tokens is the
     cumulative value in the final message_delta (NOT a sum of deltas).

OPENAI  (/v1/chat/completions, stream=true)
  data: {"choices":[{"delta":{"content":"..."}}]}
  data: {"choices":[{"finish_reason":"stop","delta":{}}]}
  data: {"usage":{"prompt_tokens":N,"completion_tokens":M}}   # only if stream_options.include_usage
  data: [DONE]
  -> usage is absent unless include_usage was set, we estimate when missing.

Non-streaming bodies are handled too (the common case for many tools).
"""
from __future__ import annotations

import json
from typing import Iterable, Optional

from abenlux.schema import (
    CanonicalEvent,
    CaptureTier,
    Message,
    Provider,
    Usage,
)


# --------------------------------------------------------------------------- #
# SSE line parsing (shared)                                                    #
# --------------------------------------------------------------------------- #
def iter_sse_data(raw: bytes | str) -> Iterable[str]:
    """Yield the JSON payload of every `data:` line in an SSE stream, in order.
    Tolerant of the `event:`/`id:` lines and blank separators."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload and payload != "[DONE]":
            yield payload


# --------------------------------------------------------------------------- #
# Heuristic token estimation (only used when the provider omits usage)         #
# --------------------------------------------------------------------------- #
def estimate_tokens(text: str) -> int:
    """~4 chars/token English heuristic. Marked as estimate by callers so the
    dashboard can flag low-confidence counts rather than present them as exact."""
    if not text:
        return 0
    return max(1, round(len(text) / 4))


# --------------------------------------------------------------------------- #
# Anthropic                                                                    #
# --------------------------------------------------------------------------- #
def parse_anthropic_request(body: dict) -> tuple[list[Message], Optional[str]]:
    messages: list[Message] = []
    if isinstance(body.get("system"), str) and body["system"]:
        messages.append(Message(role="system", content=body["system"]))
    elif isinstance(body.get("system"), list):  # blocks form
        sys_txt = "".join(b.get("text", "") for b in body["system"] if isinstance(b, dict))
        if sys_txt:
            messages.append(Message(role="system", content=sys_txt))
    for m in body.get("messages", []):
        content = m.get("content", "")
        if isinstance(content, list):
            content = "".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
            )
        messages.append(Message(role=m.get("role", "user"), content=content or ""))
    return messages, body.get("model")


def parse_anthropic_stream(raw: bytes | str) -> tuple[str, Usage, list[str], Optional[str]]:
    text_parts: list[str] = []
    usage = Usage()
    finish: list[str] = []
    model: Optional[str] = None
    for payload in iter_sse_data(raw):
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "message_start":
            msg = ev.get("message", {})
            model = msg.get("model", model)
            u = msg.get("usage", {})
            usage.input_tokens = u.get("input_tokens", usage.input_tokens)
            usage.cache_read_tokens = u.get("cache_read_input_tokens", 0)
            usage.cache_creation_tokens = u.get("cache_creation_input_tokens", 0)
        elif etype == "content_block_delta":
            delta = ev.get("delta", {})
            if delta.get("type") == "text_delta":
                text_parts.append(delta.get("text", ""))
        elif etype == "message_delta":
            # AUTHORITATIVE output token count lives here (cumulative), not a sum of deltas
            u = ev.get("usage", {})
            if "output_tokens" in u:
                usage.output_tokens = u["output_tokens"]
            sr = ev.get("delta", {}).get("stop_reason")
            if sr:
                finish.append(sr)
    return "".join(text_parts), usage, finish, model


def parse_anthropic_response_body(body: dict) -> tuple[str, Usage, list[str], Optional[str]]:
    text = "".join(
        b.get("text", "") for b in body.get("content", []) if isinstance(b, dict) and b.get("type") == "text"
    )
    u = body.get("usage", {})
    usage = Usage(
        input_tokens=u.get("input_tokens", 0),
        output_tokens=u.get("output_tokens", 0),
        cache_read_tokens=u.get("cache_read_input_tokens", 0),
        cache_creation_tokens=u.get("cache_creation_input_tokens", 0),
    )
    finish = [body["stop_reason"]] if body.get("stop_reason") else []
    return text, usage, finish, body.get("model")


# --------------------------------------------------------------------------- #
# OpenAI                                                                       #
# --------------------------------------------------------------------------- #
def parse_openai_request(body: dict) -> tuple[list[Message], Optional[str]]:
    messages = [
        Message(role=m.get("role", "user"), content=_openai_content(m.get("content", "")))
        for m in body.get("messages", [])
    ]
    return messages, body.get("model")


def _openai_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # multimodal parts
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def parse_openai_stream(raw: bytes | str) -> tuple[str, Usage, list[str], Optional[str], bool]:
    text_parts: list[str] = []
    usage = Usage()
    finish: list[str] = []
    model: Optional[str] = None
    had_usage = False
    for payload in iter_sse_data(raw):
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            continue
        model = ev.get("model", model)
        for ch in ev.get("choices", []):
            piece = ch.get("delta", {}).get("content")
            if piece:
                text_parts.append(piece)
            if ch.get("finish_reason"):
                finish.append(ch["finish_reason"])
        if ev.get("usage"):  # present only with stream_options.include_usage
            u = ev["usage"]
            cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
            # OpenAI/Azure fold cached tokens INTO prompt_tokens. split them out so the cache
            # discount applies, otherwise cached input is billed at the full rate (overstated).
            usage.input_tokens = max(0, u.get("prompt_tokens", 0) - cached)
            usage.cache_read_tokens = cached
            usage.output_tokens = u.get("completion_tokens", 0)
            had_usage = True
    return "".join(text_parts), usage, finish, model, had_usage


def parse_openai_response_body(body: dict) -> tuple[str, Usage, list[str], Optional[str]]:
    choice = (body.get("choices") or [{}])[0]
    text = _openai_content(choice.get("message", {}).get("content", ""))
    u = body.get("usage", {})
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    # split cached tokens out of prompt_tokens so the cache discount applies (see stream parser)
    usage = Usage(
        input_tokens=max(0, u.get("prompt_tokens", 0) - cached),
        output_tokens=u.get("completion_tokens", 0),
        cache_read_tokens=cached,
    )
    finish = [choice["finish_reason"]] if choice.get("finish_reason") else []
    return text, usage, finish, body.get("model")


# OpenAI Responses API (/v1/responses) - what Codex and newer tools speak 
# A different shape from chat/completions: the request carries `input` (string or a list of
# role/content items) + `instructions`, usage is `input_tokens`/`output_tokens` (cached tokens in
# input_tokens_details), and streaming is SSE typed events ending in `response.completed`.
def _responses_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def parse_responses_request(body: dict) -> tuple[list[Message], Optional[str]]:
    messages: list[Message] = []
    if body.get("instructions"):
        messages.append(Message(role="system", content=str(body["instructions"])))
    inp = body.get("input")
    if isinstance(inp, str):
        messages.append(Message(role="user", content=inp))
    elif isinstance(inp, list):
        for m in inp:
            if isinstance(m, dict):
                messages.append(Message(role=m.get("role", "user"), content=_responses_content(m.get("content", ""))))
    return messages, body.get("model")


def _responses_usage(u: dict) -> Usage:
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
    return Usage(
        input_tokens=max(0, (u.get("input_tokens", 0) or 0) - cached),
        output_tokens=u.get("output_tokens", 0) or 0,
        cache_read_tokens=cached,
    )


def _responses_output_text(output) -> str:
    text = []
    for item in output or []:
        if isinstance(item, dict) and item.get("type") == "message":
            text.append(_responses_content(item.get("content", [])))
    return "".join(text)


def parse_responses_body(body: dict) -> tuple[str, Usage, list[str], Optional[str]]:
    text = _responses_output_text(body.get("output"))
    usage = _responses_usage(body.get("usage", {}))
    finish = [body["status"]] if body.get("status") else []
    return text, usage, finish, body.get("model")


def parse_responses_stream(raw: bytes | str) -> tuple[str, Usage, list[str], Optional[str]]:
    text_parts: list[str] = []
    usage = Usage()
    finish: list[str] = []
    model: Optional[str] = None
    for payload in iter_sse_data(raw):
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type", "")
        if etype == "response.output_text.delta":
            text_parts.append(ev.get("delta", "") or "")
        elif etype in ("response.completed", "response.incomplete", "response.failed"):
            resp = ev.get("response", {})
            model = resp.get("model", model)
            if resp.get("usage"):
                usage = _responses_usage(resp["usage"])
            if resp.get("status"):
                finish.append(resp["status"])
            if not text_parts:  # some servers only emit the full text on completion
                text_parts.append(_responses_output_text(resp.get("output")))
    return "".join(text_parts), usage, finish, model

# Google Gemini (generativelanguage / Vertex generateContent)                 
# Gemini's wire shape is its own: requests carry `contents` (role + parts[].text),
# usage lives in `usageMetadata` (promptTokenCount / candidatesTokenCount /
# cachedContentTokenCount), and streaming is a JSON-array SSE of GenerateContentResponse
# chunks where usageMetadata is cumulative and authoritative only on the final chunk.
# The model is not echoed per-chunk, so it is taken from the request.
def _gemini_parts_text(parts) -> str:
    if not isinstance(parts, list):
        return ""
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def parse_gemini_request(body: dict) -> tuple[list[Message], Optional[str]]:
    messages: list[Message] = []
    sysi = body.get("systemInstruction") or body.get("system_instruction")
    if isinstance(sysi, dict):
        sys_txt = _gemini_parts_text(sysi.get("parts"))
        if sys_txt:
            messages.append(Message(role="system", content=sys_txt))
    for c in body.get("contents", []):
        if not isinstance(c, dict):
            continue
        # gemini uses role "model" for the assistant, normalize to "assistant"
        role = c.get("role", "user")
        role = "assistant" if role == "model" else role
        messages.append(Message(role=role, content=_gemini_parts_text(c.get("parts"))))
    return messages, body.get("model")


def _gemini_usage(meta: dict) -> Usage:
    return Usage(
        input_tokens=meta.get("promptTokenCount", 0) or 0,
        output_tokens=meta.get("candidatesTokenCount", 0) or 0,
        cache_read_tokens=meta.get("cachedContentTokenCount", 0) or 0,
    )


def _gemini_candidate_text(resp: dict) -> tuple[str, list[str]]:
    text_parts: list[str] = []
    finish: list[str] = []
    for cand in resp.get("candidates", []):
        if not isinstance(cand, dict):
            continue
        text_parts.append(_gemini_parts_text(cand.get("content", {}).get("parts")))
        if cand.get("finishReason"):
            finish.append(cand["finishReason"])
    return "".join(text_parts), finish


def parse_gemini_stream(raw: bytes | str) -> tuple[str, Usage, list[str], Optional[str]]:
    text_parts: list[str] = []
    usage = Usage()
    finish: list[str] = []
    model: Optional[str] = None
    for payload in iter_sse_data(raw):
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            continue
        model = ev.get("modelVersion", model)
        chunk_text, chunk_finish = _gemini_candidate_text(ev)
        if chunk_text:
            text_parts.append(chunk_text)
        finish.extend(chunk_finish)
        if ev.get("usageMetadata"):  # cumulative, last one wins
            usage = _gemini_usage(ev["usageMetadata"])
    return "".join(text_parts), usage, finish, model


def parse_gemini_response_body(body: dict) -> tuple[str, Usage, list[str], Optional[str]]:
    text, finish = _gemini_candidate_text(body)
    usage = _gemini_usage(body.get("usageMetadata", {}))
    return text, usage, finish, body.get("modelVersion")


# --------------------------------------------------------------------------- #
# Top-level: build a CanonicalEvent from an intercepted exchange               #
# --------------------------------------------------------------------------- #
def build_event(
    *,
    provider: Provider,
    request_body: dict,
    response_raw: bytes | str,
    streamed: bool,
    tier: CaptureTier = CaptureTier.GATEWAY_INTERCEPT,
    response_api: bool = False,
) -> CanonicalEvent:
    """Single entry point used by the gateway once it has the full request body and
    the (buffered) response. Picks the right adapter and produces normalized output."""
    ev = CanonicalEvent(provider=provider, tier=tier, streamed=streamed, content_captured=True)

    if response_api:  # OpenAI Responses API (/v1/responses), used by Codex and newer tools
        ev.messages, ev.request_model = parse_responses_request(request_body)
        if streamed:
            out, usage, finish, rmodel = parse_responses_stream(response_raw)
        else:
            out, usage, finish, rmodel = parse_responses_body(_as_json(response_raw))
    elif provider == Provider.ANTHROPIC:
        ev.messages, ev.request_model = parse_anthropic_request(request_body)
        if streamed:
            out, usage, finish, rmodel = parse_anthropic_stream(response_raw)
        else:
            out, usage, finish, rmodel = parse_anthropic_response_body(
                _as_json(response_raw)
            )
    elif provider == Provider.OPENAI:
        ev.messages, ev.request_model = parse_openai_request(request_body)
        if streamed:
            out, usage, finish, rmodel, had_usage = parse_openai_stream(response_raw)
            if not had_usage:  # provider omitted usage -> estimate, flag as such
                usage.input_tokens = estimate_tokens(ev.input_text())
                usage.output_tokens = estimate_tokens(out)
                ev.tokens_estimated = True
        else:
            out, usage, finish, rmodel = parse_openai_response_body(_as_json(response_raw))
    elif provider == Provider.GOOGLE:
        ev.messages, ev.request_model = parse_gemini_request(request_body)
        if streamed:
            out, usage, finish, rmodel = parse_gemini_stream(response_raw)
        else:
            out, usage, finish, rmodel = parse_gemini_response_body(_as_json(response_raw))
    else:
        raise ValueError(f"no adapter for provider {provider}")

    # backfill the request model from the response when the request body omitted it (Gemini puts the
    # model in the URL path, not the body), so pricing has a model to match instead of falling to $0.
    ev.request_model = ev.request_model or rmodel
    ev.response_model = rmodel or ev.request_model
    ev.usage = usage
    ev.finish_reasons = finish
    ev.output_messages = [Message(role="assistant", content=out)] if out else []
    return ev


def _as_json(raw: bytes | str) -> dict:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    return json.loads(raw)
