"""
A mock model upstream. The org-onboarding problem: a developer (or IT, validating a rollout)
needs to confirm "is my tool actually being captured?" without spending real tokens or sending a
client's prompt to a vendor. Point the gateway's upstream at this mock and run any tool normally -
it returns protocol-correct, well-formed streams for Anthropic, OpenAI, and Gemini with realistic
usage, so the full capture path (reassembly, tokens, cost, attribution) exercises end to end.

  abenlux mock --port 9111
  ABEN_ANTHROPIC_UPSTREAM=http://127.0.0.1:9111 abenlux gateway
  ANTHROPIC_BASE_URL=http://127.0.0.1:8088 <your tool> ...

The streams are deliberately spec-faithful (correct event names, the authoritative cumulative
usage in the final frame) so REAL vendor SDKs - which aider, Cline, Continue, opencode, Pi and
others sit on - parse them without complaint.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Abenlux mock upstream", version="0.2.0")

_ANTHROPIC_SSE = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"id":"msg_mock","type":"message","role":"assistant",'
    '"model":"claude-opus-4-8","content":[],"stop_reason":null,'
    '"usage":{"input_tokens":1820,"output_tokens":1}}}\n\n'
    'event: content_block_start\n'
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta",'
    '"text":"Use a Temporal saga with compensation steps."}}\n\n'
    'event: content_block_stop\n'
    'data: {"type":"content_block_stop","index":0}\n\n'
    'event: message_delta\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":42}}\n\n'
    'event: message_stop\n'
    'data: {"type":"message_stop"}\n\n'
)

_OPENAI_SSE = (
    'data: {"id":"chatcmpl_mock","object":"chat.completion.chunk","model":"gpt-5.5",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl_mock","object":"chat.completion.chunk","model":"gpt-5.5",'
    '"choices":[{"index":0,"delta":{"content":" world"},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl_mock","object":"chat.completion.chunk","model":"gpt-5.5",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    'data: {"id":"chatcmpl_mock","object":"chat.completion.chunk","model":"gpt-5.5","choices":[],'
    '"usage":{"prompt_tokens":12,"completion_tokens":5,"total_tokens":17}}\n\n'
    'data: [DONE]\n\n'
)


def _sse(text: str) -> StreamingResponse:
    async def gen():
        yield text.encode()
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/v1/messages")
async def anthropic(request: Request):
    body = await request.json()
    if body.get("stream"):
        return _sse(_ANTHROPIC_SSE)
    return JSONResponse({
        "id": "msg_mock", "type": "message", "role": "assistant", "model": "claude-opus-4-8",
        "content": [{"type": "text", "text": "Use a Temporal saga with compensation steps."}],
        "stop_reason": "end_turn", "usage": {"input_tokens": 1820, "output_tokens": 42},
    })


@app.post("/v1/chat/completions")
async def openai(request: Request):
    body = await request.json()
    if body.get("stream"):
        return _sse(_OPENAI_SSE)
    return JSONResponse({
        "id": "chatcmpl_mock", "object": "chat.completion", "model": "gpt-5.5",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello world"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
    })


@app.post("/v1beta/models/{model_path:path}")
async def gemini(model_path: str, request: Request):
    if "streamGenerateContent" in model_path:
        sse = (
            'data: {"candidates":[{"content":{"parts":[{"text":"Use a saga."}],"role":"model"},'
            '"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":120,'
            '"candidatesTokenCount":8},"modelVersion":"gemini-3.5-flash"}\n\n'
        )
        return _sse(sse)
    return JSONResponse({
        "candidates": [{"content": {"parts": [{"text": "Use a saga."}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 8},
        "modelVersion": "gemini-3.5-flash",
    })
