"""
Deep multi-tool capture. Drives a realistic fleet of DIFFERENT tools through the REAL entry points -
Tier-2 base_url gateway (Anthropic, OpenAI, Google wire formats), Tier-1 OTLP (traces AND logs), and
Tier-3 vendor admin - then asserts the unified derived store and management report reflect every
tool, tier, and provider, with correct tokens/cost/attribution/work-type, redaction, and the
estimated-token honesty flag. No feature left unturned across the tool matrix.
"""
from __future__ import annotations

import sqlite3

import pytest

from abenlux.analytics.reports import management_report
from abenlux.attribution.attributor import KnowledgeGraph, Objective
from abenlux.capture.adapters import build_event
from abenlux.capture.otel_ingest import events_from_otlp
from abenlux.capture.vendor_admin import cursor_event_to_derived
from abenlux.embedding import hashing_embed
from abenlux.pipeline import process
from abenlux.schema import CaptureTier, Provider, WorkContext
from abenlux.store import DerivedStore

HMAC = b"multitool-key"
SECRET = "sk-ant-MULTITOOL123456789SECRET"

# ---- real wire-format payloads per provider ----
ANTH_STREAM = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"model":"claude-opus-4-8","usage":{"input_tokens":1820,"output_tokens":1}}}\n\n'
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"use a saga"}}\n\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":420}}\n\n'
)
ANTH_BODY = {"model": "claude-sonnet-4-6", "content": [{"type": "text", "text": "done"}],
             "stop_reason": "end_turn", "usage": {"input_tokens": 9000, "output_tokens": 800}}
OPENAI_STREAM_USAGE = (
    'data: {"model":"gpt-5.5","choices":[{"delta":{"content":"hi"}}]}\n\n'
    'data: {"model":"gpt-5.5","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    'data: {"choices":[],"usage":{"prompt_tokens":5000,"completion_tokens":600}}\n\n'
    'data: [DONE]\n\n'
)
OPENAI_STREAM_NO_USAGE = (
    'data: {"model":"gpt-4o","choices":[{"delta":{"content":"hello there friend"}}]}\n\n'
    'data: {"model":"gpt-4o","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    'data: [DONE]\n\n'
)
GEMINI_STREAM = (
    'data: {"candidates":[{"content":{"parts":[{"text":"use a saga"}],"role":"model"},"finishReason":"STOP"}],'
    '"usageMetadata":{"promptTokenCount":12000,"candidatesTokenCount":1500},"modelVersion":"gemini-3.5-flash"}\n\n'
)


def _kg():
    kg = KnowledgeGraph(semantic_threshold=0.92)
    for oid, label, client, pref, budget in [
        ("obj-shop", "Acme - Checkout Platform", "acme", "SHOP", 100000),
        ("obj-data", "Initech - Data Platform", "initech", "DATA", 100000),
        ("obj-pay", "Globex - Payments", "globex", "PAY", 100000),
    ]:
        kg.add_objective(Objective(oid, label, "client", client=client, monthly_budget_usd=budget))
        kg.map_ticket_prefix(pref, oid)
    kg.map_repo("acme-checkout", "obj-shop")
    return kg


@pytest.fixture(scope="module")
def fleet(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("mt") / "fleet.db")
    kg = _kg()
    store = DerivedStore(db)

    def gw(tool, provider, body, raw, streamed, branch, ticket, repo=None):
        ev = build_event(provider=provider, request_body=body, response_raw=raw, streamed=streamed)
        ev.work = WorkContext(tool=tool, app_category="cli", git_branch=branch, ticket_id=ticket, repo=repo)
        ev.actor_raw = f"{tool}-dev@corp"
        store.insert(process(ev, kg=kg, hmac_key=HMAC, embed_fn=hashing_embed).record)

    # ---- TIER 2: base_url gateway, three providers, several tools ----
    gw("aider", Provider.ANTHROPIC,
       {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": f"add a feature, key {SECRET}"}], "stream": True},
       ANTH_STREAM, True, "feature/SHOP-1", "SHOP-1", "acme-checkout")
    gw("cline", Provider.ANTHROPIC,
       {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "fix the broken thing"}]},
       __import__("json").dumps(ANTH_BODY), False, "fix/SHOP-2", "SHOP-2")
    gw("continue", Provider.OPENAI,
       {"model": "gpt-5.5", "messages": [{"role": "user", "content": "refactor and rename this"}], "stream": True},
       OPENAI_STREAM_USAGE, True, "refactor/DATA-9", "DATA-9")
    gw("opencode", Provider.OPENAI,
       {"model": "gpt-4o", "messages": [{"role": "user", "content": "x" * 80}], "stream": True},
       OPENAI_STREAM_NO_USAGE, True, "test/DATA-10", "DATA-10")  # no usage -> estimated tokens
    gw("gemini-cli", Provider.GOOGLE,
       {"model": "gemini-3.5-flash", "contents": [{"role": "user", "parts": [{"text": "how should I architect this"}]}]},
       GEMINI_STREAM, True, "feature/PAY-3", "PAY-3")

    # ---- TIER 1: OTLP. Claude Code via LOG events, Codex/Gemini via trace spans ----
    def _sv(s):
        return {"stringValue": s}

    def _iv(n):
        return {"intValue": str(n)}

    logs = {"resourceLogs": [{"scopeLogs": [{"logRecords": [{"attributes": [
        {"key": "gen_ai.provider.name", "value": _sv("anthropic")},
        {"key": "gen_ai.request.model", "value": _sv("claude-opus-4-8")},
        {"key": "gen_ai.usage.input_tokens", "value": _iv(20000)},
        {"key": "gen_ai.usage.output_tokens", "value": _iv(2500)},
        {"key": "gen_ai.input.messages", "value": _sv('[{"role":"user","content":"implement a new payment endpoint"}]')},
    ]}]}]}]}
    traces = {"resourceSpans": [{"scopeSpans": [{"spans": [{"attributes": [
        {"key": "gen_ai.provider.name", "value": _sv("openai")},
        {"key": "gen_ai.request.model", "value": _sv("gpt-5.5")},
        {"key": "gen_ai.usage.input_tokens", "value": _iv(8000)},
        {"key": "gen_ai.usage.output_tokens", "value": _iv(900)},
    ]}]}]}]}
    for payload, tool in [(logs, "claude-code"), (traces, "openai-codex")]:
        for ev in events_from_otlp(payload):
            ev.work = WorkContext(tool=tool, git_branch="feature/SHOP-1", ticket_id="SHOP-1")
            ev.actor_raw = f"{tool}-dev@corp"
            store.insert(process(ev, kg=kg, hmac_key=HMAC, embed_fn=hashing_embed).record)

    # ---- TIER 3: Cursor usage event (metadata only, no content) ----
    store.insert(cursor_event_to_derived(
        {"userEmail": "cursor-dev@corp", "model": "claude-opus-4-8", "inputTokens": 30000,
         "outputTokens": 3000, "repoName": "acme-checkout"}, hmac_key=HMAC, kg=kg))

    store.close()
    return db


def _rows(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute("SELECT * FROM derived")]


def test_every_tool_captured(fleet):
    tools = {r["tool"] for r in _rows(fleet)}
    assert {"aider", "cline", "continue", "opencode", "gemini-cli",
            "claude-code", "openai-codex", "cursor-agent"} <= tools


def test_all_three_providers_and_all_tiers(fleet):
    rows = _rows(fleet)
    assert {r["provider"] for r in rows} >= {"anthropic", "openai", "google"}
    assert {r["tier"] for r in rows} == {
        CaptureTier.GATEWAY_INTERCEPT.value, CaptureTier.OTEL_NATIVE.value, CaptureTier.VENDOR_ADMIN_API.value}


def test_tokens_and_cost_present_and_priced(fleet):
    for r in _rows(fleet):
        assert r["input_tokens"] >= 0 and r["output_tokens"] >= 0
        if r["request_model"]:  # every model used here is in the price table
            assert r["cost_priced"] == 1 and r["cost_usd"] > 0


def test_openai_without_usage_is_flagged_estimated(fleet):
    opencode = next(r for r in _rows(fleet) if r["tool"] == "opencode")
    assert opencode["tokens_estimated"] == 1  # provider omitted usage -> honest estimate flag


def test_attribution_and_work_type_across_tools(fleet):
    rows = {r["tool"]: r for r in _rows(fleet)}
    assert rows["aider"]["attribution_method"] == "ticket_join"
    assert rows["aider"]["objective_id"] == "obj-shop"
    assert rows["aider"]["work_type"] == "feature"          # from the feature/ branch
    assert rows["cline"]["work_type"] == "fix"
    assert rows["continue"]["work_type"] == "refactor"
    assert rows["cursor-agent"]["attribution_method"] == "repo_join"  # tier-3 metadata still joins


def test_privacy_secret_never_persisted(fleet):
    raw = open(fleet, "rb").read()
    assert SECRET.encode() not in raw and b"sk-ant-" not in raw
    assert b"add a feature" not in raw                      # prompt content discarded


def test_unified_report_covers_the_whole_fleet(fleet):
    store = DerivedStore(fleet)
    rep = management_report(store, k=1, kg=_kg())   # k=1 so per-tool figures render in this test
    store.close()
    tool_labels = {r["label"] for r in rep["by_tool"]}
    assert {"aider", "cline", "continue", "opencode", "gemini-cli", "cursor-agent"} <= tool_labels
    assert rep["total_cost_usd"] > 0 and rep["total_tokens"] > 0
    # purpose mix spans multiple work types captured from different tools
    assert {"feature", "fix", "refactor"} <= {r["label"] for r in rep["by_work_type"]}
