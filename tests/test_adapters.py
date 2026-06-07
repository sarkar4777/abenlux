from abenlux.capture.adapters import (
    build_event,
    parse_anthropic_stream,
    parse_openai_stream,
    estimate_tokens,
)
from abenlux.schema import Provider


ANTHROPIC_STREAM = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"model":"claude-opus-4-8",'
    '"usage":{"input_tokens":1820,"output_tokens":1}}}\n\n'
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello "}}\n\n'
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"world"}}\n\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":37}}\n\n'
    'data: {"type":"message_stop"}\n\n'
)

OPENAI_STREAM_NO_USAGE = (
    'data: {"model":"gpt-4o","choices":[{"delta":{"content":"Hello "}}]}\n\n'
    'data: {"model":"gpt-4o","choices":[{"delta":{"content":"world"}}]}\n\n'
    'data: {"model":"gpt-4o","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    'data: [DONE]\n\n'
)

OPENAI_STREAM_WITH_USAGE = OPENAI_STREAM_NO_USAGE.replace(
    'data: [DONE]',
    'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":5}}\n\ndata: [DONE]',
)


def test_anthropic_stream_tokens_and_text():
    text, usage, finish, model = parse_anthropic_stream(ANTHROPIC_STREAM)
    assert text == "Hello world"
    assert usage.input_tokens == 1820
    # authoritative output count is the cumulative value in message_delta, NOT 1+...
    assert usage.output_tokens == 37
    assert finish == ["end_turn"]
    assert model == "claude-opus-4-8"


def test_openai_stream_estimates_when_usage_absent():
    text, usage, finish, model, had_usage = parse_openai_stream(OPENAI_STREAM_NO_USAGE)
    assert text == "Hello world"
    assert finish == ["stop"]
    assert model == "gpt-4o"
    assert had_usage is False  # caller must estimate


def test_openai_stream_uses_reported_usage_when_present():
    text, usage, finish, model, had_usage = parse_openai_stream(OPENAI_STREAM_WITH_USAGE)
    assert had_usage is True
    assert usage.input_tokens == 12
    assert usage.output_tokens == 5


def test_build_event_openai_estimates_input_output():
    req = {"model": "gpt-4o", "messages": [{"role": "user", "content": "x" * 40}]}
    ev = build_event(provider=Provider.OPENAI, request_body=req,
                     response_raw=OPENAI_STREAM_NO_USAGE, streamed=True)
    assert ev.output_text() == "Hello world"
    assert ev.usage.input_tokens == estimate_tokens("x" * 40)  # ~10
    assert ev.usage.output_tokens == estimate_tokens("Hello world")


def test_build_event_anthropic_request_parsing_with_system():
    req = {"model": "claude-opus-4-8", "system": "sys prompt",
           "messages": [{"role": "user", "content": "hi"}]}
    ev = build_event(provider=Provider.ANTHROPIC, request_body=req,
                     response_raw=ANTHROPIC_STREAM, streamed=True)
    roles = [m.role for m in ev.messages]
    assert roles == ["system", "user"]
    assert ev.usage.output_tokens == 37
