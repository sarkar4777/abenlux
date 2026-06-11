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
from abenlux.capture.diff import SessionHistoryTracker, conversation_key
from abenlux.capture.otel_ingest import events_from_otlp
from abenlux.collaborate.broker import CollaborationBroker, TopicSignal
from abenlux.developer.feed import LocalSignalFeed
from abenlux.developer.matches import MatchStore
from abenlux.developer.notify import Debouncer, notify
from abenlux.embedding import get_embedder
from abenlux.pipeline import process
from abenlux.pricing import cache_recoverable_usd, cost_usd
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
# in-place upgrade that then adopts a named tenant: claim this device's pre-tenant (NULL) rows for it,
# so its history isn't orphaned out of tenant-scoped reports. no-op for the default tenant.
if getattr(SETTINGS, "tenant_id", "default") != "default":
    _store.claim_null_tenant(SETTINGS.tenant_id)
    if _dev_store is not _store:
        _dev_store.claim_null_tenant(SETTINGS.tenant_id)
_kg = KnowledgeGraph.from_yaml(SETTINGS.kg_path, embed_fn=_embed) if SETTINGS.kg_path else KnowledgeGraph()
_history = SessionHistoryTracker()
_feed = LocalSignalFeed()        # developer-private, on-device, never crosses to analytics
_broker = CollaborationBroker()  # double-blind, in deployment this is a privacy-preserving service
_matchstore = MatchStore(os.getenv("ABEN_MATCH_DB"))  # per-owner; defaults to the dev's private ~/.abenlux
_MAX_MONITORS = 4096
# cap how much of an upstream response we buffer for capture. normal LLM responses are KB to low MB;
# this only guards against a pathologically large body (e.g. a non-LLM payload mistakenly proxied)
# ballooning gateway memory. passthrough to the tool is never capped. override via ABEN_MAX_CAPTURE_MB.
_MAX_CAPTURE_BYTES = int(float(os.getenv("ABEN_MAX_CAPTURE_MB", "16")) * 1024 * 1024)
_monitors: "OrderedDict[str, SessionWasteMonitor]" = OrderedDict()

_NOTIFY = os.getenv("ABEN_NOTIFY", "1") != "0"  # desktop toasts, on by default, set 0 for headless
_debounce = Debouncer()


def _log_capture_error(where: str) -> None:
    # capture must never raise into the request path, but silently swallowing also hid a real bug
    # (a dropped Gemini stream) for a while. so we surface it: a one-liner always, full traceback on
    # ABEN_DEBUG. capture failing should be visible, not invisible.
    import sys
    import traceback
    if os.getenv("ABEN_DEBUG"):
        traceback.print_exc()
    else:
        print(f"abenlux: capture error in {where} (set ABEN_DEBUG=1 for the traceback)", file=sys.stderr)


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


_COLLAB_TTL = 45.0
# in forward/org mode the broker runs at the COLLECTOR, so the local broker never matches across
# developers. instead the edge polls the collector for its own matches (content-free) on a TTL and
# toasts the new ones - this is what makes "a colleague is on the same problem" a live nudge at scale.
_collab_state = {"refreshed": -1e18, "seen": OrderedDict()}


def _poll_collab_matches(pseudonym: str) -> list[dict]:
    """fetch THIS developer's collaboration matches from the collector, return only NEW ones. the
    collector binds the result to the authenticated developer, so we present the developer's own
    principal token (ABEN_TOKEN), never the shared device token - the device token must not be able
    to select whose matches are returned. best-effort, never raises."""
    import os as _os
    dev_token = _os.getenv("ABEN_TOKEN")
    if not SETTINGS.collector_url or not dev_token:
        return []
    now = time.perf_counter()
    if now - _collab_state["refreshed"] < _COLLAB_TTL:
        return []
    _collab_state["refreshed"] = now
    fresh: list[dict] = []
    try:
        r = httpx.Client(timeout=3.0).get(
            SETTINGS.collector_url.rstrip("/") + "/v1/collab-status",
            headers={"Authorization": f"Bearer {dev_token}"})
        if r.status_code == 200:
            seen = _collab_state["seen"]
            for m in r.json().get("matches", []):
                if m.get("mutual"):           # already introduced, no need to nudge again
                    continue
                key = m.get("id")
                if key in seen:
                    continue
                seen[key] = True
                seen.move_to_end(key)
                while len(seen) > 4096:
                    seen.popitem(last=False)
                fresh.append(m)
    except Exception:
        pass
    return fresh


def _surface_to_developer(result, event) -> None:
    """push waste nudges + collaboration matches to the developer's OWN local feed. tool-agnostic:
    runs identically for gateway (Tier-2) and OTLP (Tier-1) captures."""
    tool = event.work.tool
    for s in result.waste_signals:
        tokens = getattr(s, "recoverable_tokens", 0)
        # resent-history signals save the input-vs-cache delta (lossless), avoidable calls save full cost
        if s.kind in ("cache_inefficiency", "context_bloat"):
            rec_usd = cache_recoverable_usd(event.request_model, tokens)
        else:
            rec_usd = cost_usd(event.request_model, tokens, 0).total
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
        for match in _broker.submit(sig):  # solo/local mode: the on-device broker matched
            _feed.append_collab(match, tool=tool)
            _toast("collab", f"A colleague is on a similar problem: {match.topic}. Run `abenlux collab` for a double-blind intro.")
            # persist for the dashboard, one row per side, each owner sees only their own
            _matchstore.record(match.a, match.b, match.topic, match.similarity, match.mode)
            _matchstore.record(match.b, match.a, match.topic, match.similarity, match.mode)

    # forward/org mode: matching happens at the collector, so poll it for THIS developer's new
    # matches and live-push a toast. content-free, on a TTL, deduped against what we've shown.
    if SETTINGS.collector_url and rec.actor_pseudonym:
        for m in _poll_collab_matches(rec.actor_pseudonym):
            _feed.append_collab_remote(m["topic"], m["mode"], m["similarity"], tool=tool)
            _toast("collab", f"A colleague is on a similar problem: {m['topic']}. Run `abenlux collab` for a double-blind intro.")

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


def _work_overrides(headers) -> dict:
    # a tool wrapper / desktop agent can tag each call with work-context via X-Aben-* headers,
    # so one agent can attribute calls from different workspaces without restarting.
    g = headers.get
    return {k: v for k, v in {
        "actor": g("x-aben-actor"), "repo": g("x-aben-repo"),
        "branch": g("x-aben-branch"), "ticket": g("x-aben-ticket"), "tool": g("x-aben-tool"),
    }.items() if v}


def _capture(provider: Provider, req_json: dict, raw: bytes, streamed: bool, latency: float,
             overrides: dict | None = None, response_api: bool = False) -> None:
    """run a completed exchange through the edge pipeline. never raises into the request path."""
    try:
        from abenlux.attribution.attributor import extract_ticket
        event = build_event(provider=provider, request_body=req_json, response_raw=raw,
                            streamed=streamed, response_api=response_api)
        event.latency_ms = latency
        event.work = current_work_context()
        overrides = overrides or {}
        if overrides.get("repo"):
            event.work.repo = overrides["repo"]
        if overrides.get("tool"):
            event.work.tool = overrides["tool"]
        if overrides.get("branch"):
            event.work.git_branch = overrides["branch"]
            event.work.ticket_id = overrides.get("ticket") or extract_ticket(overrides["branch"])
        elif overrides.get("ticket"):
            event.work.ticket_id = overrides["ticket"]
        actor = overrides.get("actor") or current_actor()
        event.actor_raw = actor
        # resent-history bloat: diff this request's messages against the actor's last request
        event.duplicate_history_tokens = _history.duplicate_history_tokens(
            conversation_key(actor, provider.value, event.work.repo, event.messages), event.messages
        )
        result = process(
            event, kg=_kg, hmac_key=SETTINGS.hmac_bytes,
            waste_monitor=_monitor_for(actor), embed_fn=_embed, work_type_classifier=_classifier, work_type_learner=_learner,
        )
        result.record.residency = getattr(SETTINGS, "residency", "eu")  # edge region for the residency wall
        result.record.tenant_id = getattr(SETTINGS, "tenant_id", "default")  # org unit / geography
        _sink.insert(result.record)  # forward to collector or write the shared store
        _dev_store.insert(result.record)  # personal copy on-device for the developer knowledge graph
        _surface_to_developer(result, event)  # nudges + collab -> developer's private feed
    except Exception:
        _log_capture_error("_capture")


async def _proxy(request: Request, provider: Provider, path: str, upstream: str | None = None,
                 response_api: bool = False) -> Response:
    body = await request.body()
    try:
        req_json = json.loads(body) if body else {}
    except (json.JSONDecodeError, ValueError):
        req_json = {}
    # streaming is a body flag for OpenAI/Anthropic, but Gemini signals it in the URL
    # (:streamGenerateContent + ?alt=sse) with no body flag - detect both, or we would try to
    # JSON-parse an SSE stream and silently drop the capture.
    query = request.url.query or ""
    streamed = bool(req_json.get("stream")) or "streamGenerateContent" in path or "alt=sse" in query
    overrides = _work_overrides(request.headers)
    fwd = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    url = f"{upstream or _UPSTREAMS[provider]}{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    started = time.perf_counter()

    # read timeout stays unbounded - a legitimate long agentic stream has minute-long gaps between SSE
    # chunks - but connect/write/pool are bounded so a dead or black-holed upstream is reaped instead of
    # hanging the developer's tool call forever (the gateway must never be worse than calling direct).
    client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=15.0, write=30.0, pool=15.0))
    upstream_req = client.build_request(request.method, url, content=body, headers=fwd)
    try:
        upstream = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as e:
        # a connect/transport failure must surface as a provider-shaped error so the tool's own retry
        # logic engages - never an opaque 500 - and the client must be closed so the pool doesn't leak.
        await client.aclose()
        _log_capture_error(f"upstream unreachable: {type(e).__name__}")
        code = 504 if isinstance(e, httpx.TimeoutException) else 502
        return JSONResponse(status_code=code, content={
            "error": {"type": "upstream_unreachable", "message": "abenlux gateway could not reach the model upstream"}})
    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}
    captured = bytearray()
    truncated = {"hit": False}

    async def tee():
        # close the upstream response + client even if the downstream tool disconnects mid-stream
        # (the generator is then cancelled), otherwise the connection and AsyncClient leak. the captured
        # buffer is capped so a pathologically large body can't balloon gateway memory - passthrough
        # (yield) is never throttled, so the developer's stream is unaffected.
        try:
            async for chunk in upstream.aiter_raw():
                if len(captured) < _MAX_CAPTURE_BYTES:
                    captured.extend(chunk)
                    if len(captured) >= _MAX_CAPTURE_BYTES:
                        truncated["hit"] = True
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    def after() -> None:
        # runs as a Starlette BackgroundTask AFTER the full response is streamed to the tool.
        # this is reliable (unlike a generator finally under ASGI) and keeps capture entirely
        # off the request's hot path - the developer never waits on it.
        latency = (time.perf_counter() - started) * 1000
        _capture(provider, req_json, bytes(captured), streamed, latency, overrides, response_api)

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


@app.post("/v1/responses")
async def openai_responses(request: Request):
    # the OpenAI Responses API - Codex and newer tools speak this instead of chat/completions
    return await _proxy(request, Provider.OPENAI, "/v1/responses", response_api=True)


@app.post("/openai/deployments/{deployment}/chat/completions")
async def azure_openai_chat(request: Request, deployment: str):
    # Azure OpenAI: the deployment lives in the path, auth is an api-key header, and api-version is
    # a query param - a different shape from vanilla OpenAI, but the response body is OpenAI-compatible
    # so the same adapter parses it. Azure is a top-tier enterprise provider, it gets a first-class route.
    if not SETTINGS.azure_upstream:
        return JSONResponse(
            {"error": "Azure OpenAI capture is not configured. Set ABEN_AZURE_UPSTREAM to your "
                      "Azure resource or APIM host (e.g. https://my-resource.openai.azure.com)."},
            status_code=503,
        )
    return await _proxy(request, Provider.OPENAI, f"/openai/deployments/{deployment}/chat/completions",
                        upstream=SETTINGS.azure_upstream)


@app.post("/v1beta/models/{model_path:path}")
async def gemini_generate(request: Request, model_path: str):
    # gemini's path encodes the model + method, e.g. gemini-3.5-flash:streamGenerateContent
    return await _proxy(request, Provider.GOOGLE, f"/v1beta/models/{model_path}")

def _ingest_otlp(payload: dict) -> int:
    n = 0
    for event in events_from_otlp(payload):
        try:
            # a tool that self-reports an actor (Claude Code sends a hashed user.id) wins, else fall
            # back to the device's own actor. work-context (repo/branch) comes from the device env,
            # since OTel telemetry does not carry git context.
            tool_actor = event.actor_raw
            work = current_work_context()
            if event.work.tool:  # OTel parser already set the tool, keep it over the env default
                work.tool = event.work.tool
                work.app_category = event.work.app_category or work.app_category
            event.work = work
            actor = tool_actor or current_actor()
            event.actor_raw = actor
            result = process(
                event, kg=_kg, hmac_key=SETTINGS.hmac_bytes,
                waste_monitor=_monitor_for(actor), embed_fn=_embed, work_type_classifier=_classifier, work_type_learner=_learner,
            )
            result.record.residency = getattr(SETTINGS, "residency", "eu")
            result.record.tenant_id = getattr(SETTINGS, "tenant_id", "default")
            _sink.insert(result.record)
            _dev_store.insert(result.record)
            _surface_to_developer(result, event)  # same private feed, regardless of tool/tier
            n += 1
        except Exception:
            _log_capture_error("_ingest_otlp")  # one bad event must not drop the whole OTLP batch
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
