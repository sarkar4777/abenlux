from abenlux.capture.adapters import (
    build_event,
    parse_gemini_response_body,
    parse_gemini_stream,
)
from abenlux.schema import Provider

GEMINI_STREAM = (
    'data: {"candidates":[{"content":{"parts":[{"text":"Use a "}]}}],"modelVersion":"gemini-3.5-flash"}\n\n'
    'data: {"candidates":[{"content":{"parts":[{"text":"saga."}],"role":"model"},"finishReason":"STOP"}],'
    '"usageMetadata":{"promptTokenCount":120,"candidatesTokenCount":8,"cachedContentTokenCount":40}}\n\n'
)

GEMINI_BODY = {
    "candidates": [{"content": {"parts": [{"text": "Hello world"}]}, "finishReason": "STOP"}],
    "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 5},
    "modelVersion": "gemini-3.5-flash",
}


def test_gemini_stream_reassembles_and_takes_final_usage():
    text, usage, finish, model = parse_gemini_stream(GEMINI_STREAM)
    assert text == "Use a saga."
    assert usage.input_tokens == 120
    assert usage.output_tokens == 8
    assert usage.cache_read_tokens == 40
    assert finish == ["STOP"]
    assert model == "gemini-3.5-flash"


def test_gemini_response_body():
    text, usage, finish, model = parse_gemini_response_body(GEMINI_BODY)
    assert text == "Hello world"
    assert usage.input_tokens == 11 and usage.output_tokens == 5


def test_build_event_google_request_roles_normalized():
    req = {
        "model": "gemini-3.5-flash",
        "systemInstruction": {"parts": [{"text": "be terse"}]},
        "contents": [
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": [{"text": "prior answer"}]},
        ],
    }
    ev = build_event(provider=Provider.GOOGLE, request_body=req, response_raw=GEMINI_STREAM, streamed=True)
    roles = [m.role for m in ev.messages]
    assert roles == ["system", "user", "assistant"]  # gemini 'model' -> 'assistant'
    assert ev.usage.input_tokens == 120
    assert ev.output_text() == "Use a saga."
