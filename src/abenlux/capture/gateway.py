"""
Tier-2 capture gateway. A loopback (localhost) reverse proxy. Base-URL-overridable tools
(Aider, Cline, Continue, opencode, Crush, Pi, Droid, Gemini CLI, etc.) point their
ANTHROPIC_BASE_URL / OPENAI_BASE_URL / GEMINI base here. The gateway:

  * streams the upstream response straight through to the tool (zero added latency, the tool
    sees an unmodified, still-streaming response) while teeing the bytes into a buffer,
  * once the stream completes, reassembles the full exchange off the hot path,
  * measures resent-history bloat by diffing this request against the actor's previous one,
  * builds a CanonicalEvent and runs it through the edge pipeline,
  * persists ONLY the derived record.

It also mounts the Tier-1 OTLP ingest routes (/v1/otel/traces, /v1/otel/logs, /v1/otel) so a
single process handles self-instrumenting tools (Claude Code, Codex, Gemini CLI) and proxied
tools alike. Downstream of build_event / events_from_otlp, the two paths are identical.

Run:  abenlux gateway   (or: uvicorn abenlux.capture.gateway:app)
Then: ANTHROPIC_BASE_URL=http://127.0.0.1:8088 aider ...

This file imports web deps (fastapi, httpx). The domain/pipeline modules do not, so the core
stays unit-testable without a server.
"""
from __future__ import annotations

import json
import os
import time
from collections import OrderedDict

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from abenlux.attribution.attributor import KnowledgeGraph
from abenlux.capture.adapters import build_event
from abenlux.capture.context import current_actor, current_work_context
from abenlux.capture.diff import SessionHistoryTracker
from abenlux.capture.otel_ingest import events_from_otlp
from abenlux.collaborate.broker import CollaborationBroker, TopicSignal
from abenlux.developer.feed import LocalSignalFeed
from abenlux.developer.matches import MatchStore
from abenlux.developer.notify import Debouncer, notify
from abenlux.embedding import get_embedder
from abenlux.pipeline import process
from abenlux.pricing import cost_usd
from abenlux.processing.waste import SessionWasteMonitor
from abenlux.schema import Provider
from abenlux.settings import SETTINGS
from abenlux.sink import build_sink
from abenlux.store import open_store
from abenlux.worktype_learn import WorkTypeLearner
from abenlux.worktype_llm import get_classifier

_UPSTREAMS = {
    Provider.ANTHROPIC: SETTINGS.anthropic_upstream,
    Provider.OPENAI: SETTINGS.openai_upstream,
    Provider.GOOGLE: SETTINGS.google_upstream,
}
_HOP_BY_HOP = {"host", "content-length", "content-encoding", "transfer-encoding", "connection"}

app = FastAPI(title="Abenlux Gateway", version="0.2.0")
_store = open_store(SETTINGS.db_path)
_sink = build_sink(SETTINGS, local_store=_store)  # local sqlite, or forward-to-collector if configured
_embed = get_embedder()
_classifier = get_classifier()    # optional, tiny LLM intent fallback. None unless configured.
_learner = WorkTypeLearner()      # self-learning work-type memory, on-device, hot-reloaded
_dev_store = open_store(SETTINGS.local_db)  # the developer's own rows -> their personal knowledge graph
_kg = KnowledgeGraph.from_yaml(SETTINGS.kg_path, embed_fn=_embed) if SETTINGS.kg_path else KnowledgeGraph()
_history = SessionHistoryTracker()
_feed = LocalSignalFeed()        # developer-private, on-device, never crosses to analytics
_broker = CollaborationBroker()  # double-blind, in deployment this is a privacy-preserving service
_matchstore = MatchStore(os.getenv("ABEN_MATCH_DB", "abenlux-matches.db"))  # per-owner, RBAC-private
_MAX_MONITORS = 4096
_monitors: "OrderedDict[str, SessionWasteMonitor]" = OrderedDict()

_NOTIFY = os.getenv("ABEN_NOTIFY", "1") != "0"  # desktop toasts, on by default, set 0 for headless
_debounce = Debouncer()


def _toast(kind: str, line: str) -> None:
    # raise a native desktop notification so the developer sees the nudge wherever they are,
    # without opening anything. debounced per kind so identical signals do not spam.
    if _NOTIFY and line and _debounce.allow(kind):
        notify(line)

_BUDGET_TTL = 60.0
# refreshed starts far in the past so the FIRST snapshot always computes, regardless of the
# perf_counter origin on a given platform (which is otherwise undefined and was flaky on macOS).
_budget_state = {"snapshot": {}, "refreshed": -1e18}


def _budget_snapshot() -> dict:
    """objective -> status, refreshed on a TTL. local compute in solo mode, poll the collector
    when forwarding (the edge agent has the device token). best-effort, never raises."""
    now = time.perf_counter()
    if now - _budget_state["refreshed"] < _BUDGET_TTL:
        return _budget_state["snapshot"]
    _budget_state["refreshed"] = now
    try:
        if SETTINGS.collector_url:
            r = httpx.Client(timeout=3.0).get(
                SETTINGS.collector_url.rstrip("/") + "/v1/budget-status",
                headers={"Authorization": f"Bearer {SETTINGS.ingest_token}"})
            if r.status_code == 200:
                _budget_state["snapshot"] = r.json()
        else:
            from abenlux.analytics.budget import budget_status, current_month_bounds, status_snapshot
            ps, pe, t = current_month_bounds()
            _budget_state["snapshot"] = status_snapshot(
                budget_status(_store, _kg, period_start=ps, period_end=pe, now=t))
    except Exception:
        pass
    return _budget_state["snapshot"]


def _surface_to_developer(result, event) -> None:
    """push waste nudges + collaboration matches to the developer's OWN local feed. tool-agnostic:
    runs identically for gateway (Tier-2) and OTLP (Tier-1) captures."""
    tool = event.work.tool
    for s in result.waste_signals:
        rec_usd = cost_usd(event.request_model, getattr(s, "recoverable_tokens", 0), 0).total
        _feed.append_waste(s, tool=tool, recoverable_usd=rec_usd)
        if s.severity == "warn":  # only nudge the dev for actionable waste, not info hints
            _toast(s.kind, s.suggestion or s.detail)
    rec = result.record
    if rec.embedding and rec.objective_id:
        obj = _kg.objectives.get(rec.objective_id)
        sig = TopicSignal(
            actor_pseudonym=rec.actor_pseudonym or "local",
            topic_embedding=rec.embedding,
            topic_label=rec.objective_label or "general",
            client=getattr(obj, "client", None),
        )
        for match in _broker.submit(sig):
            _feed.append_collab(match, tool=tool)
            _toast("collab", f"A colleague is on a similar problem: {match.topic}. Run `abenlux collab` for a double-blind intro.")
            # persist for the dashboard, one row per side, each owner sees only their own
            _matchstore.record(match.a, match.b, match.topic, match.similarity, match.mode)
            _matchstore.record(match.b, match.a, match.topic, match.similarity, match.mode)

    # budget guardrail: PRIVATE nudge when this developer's current objective is over/at-risk.
    # management never sees this fire, it is a heads-up to the person, on their own device.
    if rec.objective_id:
        snap = _budget_snapshot().get(rec.objective_id)
        if snap:
            from abenlux.analytics.budget import guardrail_line
            line = guardrail_line(rec.objective_label or rec.objective_id, snap)
            if line:
                _feed.append_budget(line, tool=tool)
                _toast("budget_guardrail", line)


def _monitor_for(actor: str) -> SessionWasteMonitor:
    mon = _monitors.get(actor)
    if mon is None:
        mon = SessionWasteMonitor()
        _monitors[actor] = mon
        while len(_monitors) > _MAX_MONITORS:
            _monitors.popitem(last=False)
    _monitors.move_to_end(actor)
    return mon


def _capture(provider: Provider, req_json: dict, raw: bytes, streamed: bool, latency: float) -> None:
    """run a completed exchange through the edge pipeline. never raises into the request path."""
    try:
        event = build_event(provider=provider, request_body=req_json, response_raw=raw, streamed=streamed)
        event.latency_ms = latency
        event.work = current_work_context()
        actor = current_actor()
        event.actor_raw = actor
        # resent-history bloat: diff this request's messages against the actor's last request
        event.duplicate_history_tokens = _history.duplicate_history_tokens(
            f"{actor}:{provider.value}", event.messages
        )
        result = process(
            event, kg=_kg, hmac_key=SETTINGS.hmac_bytes,
            waste_monitor=_monitor_for(actor), embed_fn=_embed, work_type_classifier=_classifier, work_type_learner=_learner,
        )
        _sink.insert(result.record)  # forward to collector or write the shared store
        _dev_store.insert(result.record)  # personal copy on-device for the developer knowledge graph
        _surface_to_developer(result, event)  # nudges + collab -> developer's private feed
    except Exception:
        pass


async def _proxy(request: Request, provider: Provider, path: str) -> Response:
    body = await request.body()
    try:
        req_json = json.loads(body) if body else {}
    except (json.JSONDecodeError, ValueError):
        req_json = {}
    streamed = bool(req_json.get("stream"))
    fwd = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    url = f"{_UPSTREAMS[provider]}{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    started = time.perf_counter()

    client = httpx.AsyncClient(timeout=None)
    upstream_req = client.build_request(request.method, url, content=body, headers=fwd)
    upstream = await client.send(upstream_req, stream=True)
    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}
    captured = bytearray()

    async def tee():
        async for chunk in upstream.aiter_raw():
            captured.extend(chunk)
            yield chunk
        await upstream.aclose()
        await client.aclose()

    def after() -> None:
        # runs as a Starlette BackgroundTask AFTER the full response is streamed to the tool.
        # this is reliable (unlike a generator finally under ASGI) and keeps capture entirely
        # off the request's hot path - the developer never waits on it.
        latency = (time.perf_counter() - started) * 1000
        _capture(provider, req_json, bytes(captured), streamed, latency)

    return StreamingResponse(
        tee(), status_code=upstream.status_code, headers=resp_headers,
        media_type=upstream.headers.get("content-type", "application/json"),
        background=BackgroundTask(after),
    )


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    return await _proxy(request, Provider.ANTHROPIC, "/v1/messages")


@app.post("/v1/chat/completions")
async def openai_chat(request: Request):
    return await _proxy(request, Provider.OPENAI, "/v1/chat/completions")


@app.post("/v1beta/models/{model_path:path}")
async def gemini_generate(request: Request, model_path: str):
    # gemini's path encodes the model + method, e.g. gemini-3.5-flash:streamGenerateContent
    return await _proxy(request, Provider.GOOGLE, f"/v1beta/models/{model_path}")


# ------------------------------------------------------------------ Tier-1 OTLP ingest ----
# Two ways tools reach us: directly (OTEL_EXPORTER_OTLP_ENDPOINT -> /v1/traces, /v1/logs, the
# OTLP/HTTP standard paths) or via an OTel Collector that forwards to /v1/otel. We accept both.
# Bodies must be OTLP/JSON (set OTEL_EXPORTER_OTLP_PROTOCOL=http/json), a non-JSON (protobuf)
# body is answered 200 with a hint rather than erroring, so a misconfigured exporter never
# crashes a developer's tool.
def _ingest_otlp(payload: dict) -> int:
    n = 0
    for event in events_from_otlp(payload):
        event.work = current_work_context()
        actor = current_actor()
        event.actor_raw = actor
        result = process(
            event, kg=_kg, hmac_key=SETTINGS.hmac_bytes,
            waste_monitor=_monitor_for(actor), embed_fn=_embed, work_type_classifier=_classifier, work_type_learner=_learner,
        )
        _sink.insert(result.record)
        _dev_store.insert(result.record)
        _surface_to_developer(result, event)  # same private feed, regardless of tool/tier
        n += 1
    return n


async def _otel_route(request: Request) -> JSONResponse:
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(
            {"ingested": 0, "hint": "send OTLP/JSON (OTEL_EXPORTER_OTLP_PROTOCOL=http/json)"},
            status_code=200,
        )
    return JSONResponse({"ingested": _ingest_otlp(payload)})


# OTLP/HTTP standard signal paths (direct, collector-free)
app.add_api_route("/v1/traces", _otel_route, methods=["POST"])
app.add_api_route("/v1/logs", _otel_route, methods=["POST"])
# collector-forwarded paths
app.add_api_route("/v1/otel/traces", _otel_route, methods=["POST"])
app.add_api_route("/v1/otel/logs", _otel_route, methods=["POST"])
app.add_api_route("/v1/otel", _otel_route, methods=["POST"])


@app.post("/v1/metrics")
async def otel_metrics(request: Request):
    # metrics carry counts without the attribution context, we derive from traces/logs instead.
    # accept-and-ignore so a tool exporting all three signals doesn't see export errors.
    return JSONResponse({"ingested": 0, "note": "metrics ignored, traces/logs carry context"})


@app.get("/health")
async def health():
    forwarding = SETTINGS.collector_url is not None
    out = {
        "status": "ok",
        "mode": "edge-forward" if forwarding else "local",
        "collector": SETTINGS.collector_url,
        "objectives_loaded": len(_kg.objectives),
        "semantic_attribution": any(o.embedding for o in _kg.objectives.values()),
        "dev_key_warning": SETTINGS.using_dev_key,
    }
    if not forwarding:  # local mode has the figures on hand, forwarded mode lives on the collector
        t = _store.totals()
        out["events"] = t["n"]
        out["orphan_token_share"] = round(_store.orphan_token_share(), 4)
    return out
