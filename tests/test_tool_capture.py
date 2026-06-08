"""
Wire-format accuracy for real CLI tools, pinned from genuine traffic captured while driving the
actual Gemini CLI and Codex through the gateway:

  * Gemini streams via :streamGenerateContent (no body "stream" flag) and puts the model in the URL,
    not the body - both used to drop the capture / price it at $0.
  * Codex speaks the OpenAI Responses API (/v1/responses), a different shape from chat/completions.
  * OpenAI/Azure fold cached tokens into prompt_tokens, which must be split out for the cache discount.
"""
from abenlux.capture.adapters import build_event, parse_openai_response_body
from abenlux.schema import Provider
from abenlux.pipeline import process
from abenlux.attribution.attributor import KnowledgeGraph


GEMINI_STREAM = (
    'data: {"candidates":[{"content":{"parts":[{"text":"Use a saga."}],"role":"model"},'
    '"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":1820,"candidatesTokenCount":227},'
    '"modelVersion":"gemini-3.5-flash"}\n\n'
)


def test_gemini_stream_is_captured_and_priced():
    # the model lives in the URL, not the request body, so it must be backfilled from the response
    ev = build_event(provider=Provider.GOOGLE, request_body={"contents": []},
                     response_raw=GEMINI_STREAM, streamed=True)
    assert ev.request_model == "gemini-3.5-flash"
    assert ev.usage.input_tokens == 1820 and ev.usage.output_tokens == 227
    rec = process(ev, kg=KnowledgeGraph(), hmac_key=b"k").record
    assert rec.cost_priced and abs(rec.cost_usd - (1820 * 1.5 + 227 * 9.0) / 1_000_000) < 1e-6


RESPONSES_BODY = {
    "model": "gpt-5.5", "status": "completed",
    "output": [{"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello world"}]}],
    "usage": {"input_tokens": 2000, "output_tokens": 100, "input_tokens_details": {"cached_tokens": 800}},
}


def test_responses_api_body_with_cached_tokens():
    import json
    ev = build_event(provider=Provider.OPENAI, request_body={"model": "gpt-5.5", "input": "hi"},
                     response_raw=json.dumps(RESPONSES_BODY), streamed=False, response_api=True)
    # cached tokens are split out of input so the discount applies
    assert ev.usage.input_tokens == 1200 and ev.usage.cache_read_tokens == 800
    assert ev.usage.output_tokens == 100
    assert ev.output_text() == "Hello world"


def test_responses_api_stream():
    sse = (
        'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"Hi"}\n\n'
        'event: response.completed\n'
        'data: {"type":"response.completed","response":{"model":"gpt-5.5","status":"completed",'
        '"usage":{"input_tokens":500,"output_tokens":12}}}\n\n'
    )
    ev = build_event(provider=Provider.OPENAI, request_body={"input": "hi"},
                     response_raw=sse, streamed=True, response_api=True)
    assert ev.usage.input_tokens == 500 and ev.usage.output_tokens == 12
    assert ev.output_text() == "Hi"


def test_openai_cached_tokens_split_out():
    body = {"model": "gpt-4o", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5000, "completion_tokens": 50,
                      "prompt_tokens_details": {"cached_tokens": 4000}}}
    _, usage, _, _ = parse_openai_response_body(body)
    assert usage.input_tokens == 1000 and usage.cache_read_tokens == 4000
