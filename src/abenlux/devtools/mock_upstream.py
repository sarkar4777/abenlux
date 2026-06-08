"""
A mock model upstream. The org-onboarding problem: a developer (or IT, validating a rollout) needs
to confirm "is my tool actually being captured?" without spending real tokens or sending a client's
prompt to a vendor. Point the gateway's upstream at this mock and run any tool normally - it returns
protocol-correct, well-formed streams for Anthropic, OpenAI, and Gemini.

Usage is realistic and controllable: pass an `X-Aben-Mock-Input` header (the gateway forwards it) to
make the mock report that many input tokens (output and cache scale from it), so a fleet of real
captures produces realistic, varied cost. Default is a typical agentic call.

  abenlux mock --port 9111
  ABEN_ANTHROPIC_UPSTREAM=http://127.0.0.1:9111 abenlux gateway

The streams are spec-faithful (correct event names, cumulative authoritative usage on the final
frame) so REAL vendor SDKs - which aider, Cline, Continue, opencode, Pi and others sit on - parse
them without complaint.
"""
from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Abenlux mock upstream", version="0.2.0")

_TEXT = "Use a Temporal saga with compensation steps."


@app.get("/health")
async def health():
    return {"status": "ok"}


def _usage(request: Request) -> tuple[int, int, int]:
    try:
        inp = int(request.headers.get("x-aben-mock-input", "1820"))
    except (TypeError, ValueError):
        inp = 1820
    # cache-read fraction is controllable so a test can simulate a well-cached vs an uncached session.
    # default 0.7 (typical agentic caching), pass X-Aben-Mock-Cache=0 to simulate no caching.
    try:
        frac = float(request.headers.get("x-aben-mock-cache", "0.7"))
    except (TypeError, ValueError):
        frac = 0.7
    return inp, max(1, inp // 8), int(inp * frac)  # input, output, cache_read


def _sse(text: str) -> StreamingResponse:
    async def gen():
        yield text.encode()
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/v1/messages")
async def anthropic(request: Request):
    inp, out, cache = _usage(request)
    body = await request.json()
    if body.get("stream"):
        sse = (
            'event: message_start\n'
            'data: {"type":"message_start","message":{"id":"msg_mock","type":"message","role":"assistant",'
            f'"model":"claude-opus-4-8","content":[],"stop_reason":null,'
            f'"usage":{{"input_tokens":{inp},"output_tokens":1,"cache_read_input_tokens":{cache}}}}}}}\n\n'
            'event: content_block_start\n'
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
            'event: content_block_delta\n'
            f'data: {{"type":"content_block_delta","index":0,"delta":{{"type":"text_delta","text":"{_TEXT}"}}}}\n\n'
            'event: content_block_stop\n'
            'data: {"type":"content_block_stop","index":0}\n\n'
            'event: message_delta\n'
            f'data: {{"type":"message_delta","delta":{{"stop_reason":"end_turn"}},"usage":{{"output_tokens":{out}}}}}\n\n'
            'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        )
        return _sse(sse)
    return JSONResponse({
        "id": "msg_mock", "type": "message", "role": "assistant", "model": "claude-opus-4-8",
        "content": [{"type": "text", "text": _TEXT}], "stop_reason": "end_turn",
        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cache},
    })


@app.post("/openai/deployments/{deployment}/chat/completions")
async def azure_openai(deployment: str, request: Request):
    # Azure OpenAI shape: deployment in the path, OpenAI-compatible response body. the reported model
    # echoes the deployment so pricing resolves the same way a real Azure response would.
    return await openai(request, model=deployment)


@app.post("/v1/chat/completions")
async def openai(request: Request, model: str = "gpt-5.5"):
    inp, out, _ = _usage(request)
    body = await request.json()
    if body.get("stream"):
        chunk = ('data: {{"id":"chatcmpl_mock","object":"chat.completion.chunk","model":"{m}",'
                 '"choices":[{{"index":0,"delta":{d},"finish_reason":{fr}}}]}}\n\n')
        sse = (
            chunk.format(m=model, d='{"role":"assistant","content":"Hello"}', fr="null")
            + chunk.format(m=model, d='{"content":" world"}', fr="null")
            + chunk.format(m=model, d="{}", fr='"stop"')
            + f'data: {{"id":"chatcmpl_mock","object":"chat.completion.chunk","model":"{model}","choices":[],'
            f'"usage":{{"prompt_tokens":{inp},"completion_tokens":{out},"total_tokens":{inp+out}}}}}\n\n'
            + 'data: [DONE]\n\n'
        )
        return _sse(sse)
    return JSONResponse({
        "id": "chatcmpl_mock", "object": "chat.completion", "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello world"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out},
    })


@app.post("/v1/responses")
async def openai_responses(request: Request):
    # OpenAI Responses API shape (what Codex speaks). usage uses input_tokens/output_tokens.
    inp, out, _ = _usage(request)
    model = "gpt-5.5"
    body = await request.json()
    msg = {"type": "message", "role": "assistant", "status": "completed",
           "content": [{"type": "output_text", "text": "Hello world"}]}
    usage = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
             "input_tokens_details": {"cached_tokens": 0}}
    resp = {"id": "resp_mock", "object": "response", "model": model, "status": "completed",
            "output": [msg], "usage": usage}
    if body.get("stream"):
        sse = (
            'event: response.created\n'
            f'data: {{"type":"response.created","response":{{"id":"resp_mock","model":"{model}","status":"in_progress"}}}}\n\n'
            'event: response.output_text.delta\n'
            'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
            'event: response.output_text.delta\n'
            'data: {"type":"response.output_text.delta","delta":" world"}\n\n'
            'event: response.output_text.done\n'
            'data: {"type":"response.output_text.done","text":"Hello world"}\n\n'
            'event: response.completed\n'
            f'data: {{"type":"response.completed","response":{json.dumps(resp)}}}\n\n'
        )
        return _sse(sse)
    return JSONResponse(resp)


@app.post("/v1beta/models/{model_path:path}")
async def gemini(model_path: str, request: Request):
    inp, out, _ = _usage(request)
    if "streamGenerateContent" in model_path:
        sse = (
            'data: {"candidates":[{"content":{"parts":[{"text":"Use a saga."}],"role":"model"},'
            f'"finishReason":"STOP"}}],"usageMetadata":{{"promptTokenCount":{inp},'
            f'"candidatesTokenCount":{out}}},"modelVersion":"gemini-3.5-flash"}}\n\n'
        )
        return _sse(sse)
    return JSONResponse({
        "candidates": [{"content": {"parts": [{"text": "Use a saga."}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": inp, "candidatesTokenCount": out},
        "modelVersion": "gemini-3.5-flash",
    })
