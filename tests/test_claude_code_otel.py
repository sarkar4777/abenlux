"""
Claude Code self-instruments with its OWN OpenTelemetry shape, not the gen_ai semconv: token usage
arrives as an `api_request` LOG event (and an identical token.usage metric we deliberately ignore to
avoid double counting). These payloads are taken verbatim from a real `claude` run captured against a
raw OTLP sink, so this test pins the exact wire format the gateway must handle.
"""
from abenlux.capture.otel_ingest import events_from_otlp
from abenlux.pipeline import process
from abenlux.attribution.attributor import KnowledgeGraph


def _kv(key, **val):
    return {"key": key, "value": val}


def _api_request_log(model, inp, out, cache_r, cache_w):
    # the exact attribute shape Claude Code emits (incl. raw user.email, which must NOT be persisted)
    return {
        "body": {"stringValue": "claude_code.api_request"},
        "attributes": [
            _kv("user.id", stringValue="2c966dac43b7af20c51e7c25e94dd464aec812bb5ef857cf8a66d87a2493c903"),
            _kv("user.email", stringValue="dev@corp.example"),
            _kv("session.id", stringValue="834eeb55-7fc6-49ed-ac20-8e6d5953a01b"),
            _kv("event.name", stringValue="api_request"),
            _kv("model", stringValue=model),
            _kv("input_tokens", intValue=inp),
            _kv("output_tokens", intValue=out),
            _kv("cache_read_tokens", intValue=cache_r),
            _kv("cache_creation_tokens", intValue=cache_w),
            _kv("cost_usd", doubleValue=0.04),
        ],
    }


def _payload(*logs):
    return {"resourceLogs": [{"scopeLogs": [{"logRecords": list(logs)}]}]}


def test_claude_code_api_request_is_captured_with_cache_tokens():
    payload = _payload(
        _api_request_log("claude-haiku-4-5-20251001", 457, 13, 0, 0),
        _api_request_log("claude-opus-4-8", 1952, 97, 21383, 2754),
    )
    events = events_from_otlp(payload)
    assert len(events) == 2
    big = events[1]
    assert big.work.tool == "claude-code"
    assert big.usage.input_tokens == 1952 and big.usage.output_tokens == 97
    assert big.usage.cache_read_tokens == 21383 and big.usage.cache_creation_tokens == 2754
    # actor is the hashed user.id, never the email
    assert big.actor_raw and big.actor_raw.startswith("2c966dac")


def test_claude_code_cost_is_priced_cache_aware():
    ev = events_from_otlp(_payload(_api_request_log("claude-opus-4-8", 1952, 97, 21383, 2754)))[0]
    res = process(ev, kg=KnowledgeGraph(), hmac_key=b"k")
    # opus: 1952*5 + 97*25 + 21383*0.5 + 2754*6.25, all per-Mtok -> ~$0.04009
    assert abs(res.record.cost_usd - 0.040087) < 1e-4
    assert res.record.cache_read_tokens == 21383 and res.record.cache_creation_tokens == 2754


def test_claude_code_email_pii_never_persists():
    ev = events_from_otlp(_payload(_api_request_log("claude-opus-4-8", 10, 10, 0, 0)))[0]
    res = process(ev, kg=KnowledgeGraph(), hmac_key=b"k")
    blob = str(res.record.to_dict())
    assert "dev@corp.example" not in blob  # email is dropped at parse time, never reaches the record
