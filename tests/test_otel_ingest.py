from abenlux.capture.otel_ingest import (
    events_from_otlp,
    events_from_otlp_logs,
    events_from_otlp_traces,
)
from abenlux.schema import Provider


def _sv(s):
    return {"stringValue": s}


def _iv(n):
    return {"intValue": str(n)}


def test_trace_span_maps_usage_and_provider():
    payload = {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "attributes": [
            {"key": "gen_ai.provider.name", "value": _sv("openai")},
            {"key": "gen_ai.request.model", "value": _sv("gpt-5.5")},
            {"key": "gen_ai.usage.input_tokens", "value": _iv(100)},
            {"key": "gen_ai.usage.output_tokens", "value": _iv(20)},
        ]
    }]}]}]}
    events = events_from_otlp_traces(payload)
    assert len(events) == 1
    ev = events[0]
    assert ev.provider == Provider.OPENAI
    assert ev.usage.input_tokens == 100 and ev.usage.output_tokens == 20


def test_log_record_carries_claude_code_content_as_json_string():
    # Claude Code emits message content via OTLP log events, messages can be a JSON string
    msgs = '[{"role":"user","content":"how do I orchestrate the approval workflow?"}]'
    payload = {"resourceLogs": [{"scopeLogs": [{"logRecords": [{
        "attributes": [
            {"key": "gen_ai.provider.name", "value": _sv("anthropic")},
            {"key": "gen_ai.request.model", "value": _sv("claude-opus-4-8")},
            {"key": "gen_ai.usage.input_tokens", "value": _iv(1820)},
            {"key": "gen_ai.usage.output_tokens", "value": _iv(42)},
            {"key": "gen_ai.input.messages", "value": _sv(msgs)},
        ]
    }]}]}]}
    events = events_from_otlp_logs(payload)
    assert len(events) == 1
    ev = events[0]
    assert ev.provider == Provider.ANTHROPIC
    assert ev.content_captured is True
    assert "approval workflow" in ev.input_text()
    assert ev.usage.input_tokens == 1820


def test_log_record_body_kvlist_merges_into_attrs():
    body = {"kvlistValue": {"values": [
        {"key": "gen_ai.usage.output_tokens", "value": _iv(7)},
    ]}}
    payload = {"resourceLogs": [{"scopeLogs": [{"logRecords": [{
        "attributes": [{"key": "gen_ai.request.model", "value": _sv("claude-haiku-4-5")}],
        "body": body,
    }]}]}]}
    ev = events_from_otlp_logs(payload)[0]
    assert ev.usage.output_tokens == 7


def test_dispatch_picks_signal():
    traces = {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "attributes": [{"key": "gen_ai.request.model", "value": _sv("gpt-4o")}]}]}]}]}
    assert len(events_from_otlp(traces)) == 1
    assert events_from_otlp({}) == []
