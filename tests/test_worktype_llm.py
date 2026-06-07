"""
The optional LLM intent classifier. Covers each provider's request shape and parsing, the Azure
URL handling, env-based configuration with standard names, caching, and silent failure - all with a
mocked HTTP layer so no real credentials or network are needed.
"""
import httpx

from abenlux.worktype_llm import WorkTypeClassifier, get_classifier


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _capture(payload):
    seen = {}

    def post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, body=json, headers=headers or {})
        return _Resp(payload)

    return post, seen


def test_openai_request_and_parse(monkeypatch):
    post, seen = _capture({"choices": [{"message": {"content": "Feature"}}]})
    monkeypatch.setattr(httpx, "post", post)
    clf = WorkTypeClassifier("openai", "sk-x", model="gpt-4o-mini")
    assert clf.classify("add a new endpoint") == "feature"
    assert seen["url"].endswith("/v1/chat/completions")
    assert seen["headers"]["Authorization"] == "Bearer sk-x"
    assert seen["body"]["max_tokens"] == 5


def test_azure_url_handles_deployments_base(monkeypatch):
    post, seen = _capture({"choices": [{"message": {"content": "fix"}}]})
    monkeypatch.setattr(httpx, "post", post)
    clf = WorkTypeClassifier("azure", "key", model="gpt-4o",
                             base_url="https://x.azure-api.net/openai/deployments",
                             api_version="2024-10-01-preview")
    assert clf.classify("the thing is broken") == "fix"
    # base already had /deployments, so it is not doubled
    assert seen["url"] == "https://x.azure-api.net/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-01-preview"
    assert seen["headers"]["api-key"] == "key"


def test_anthropic_request_and_parse(monkeypatch):
    post, seen = _capture({"content": [{"text": "refactor"}]})
    monkeypatch.setattr(httpx, "post", post)
    clf = WorkTypeClassifier("anthropic", "key", model="claude-3-5-haiku")
    assert clf.classify("rename and split this") == "refactor"
    assert seen["url"].endswith("/v1/messages")
    assert seen["headers"]["x-api-key"] == "key"


def test_google_request_and_parse(monkeypatch):
    post, seen = _capture({"candidates": [{"content": {"parts": [{"text": "exploration"}]}}]})
    monkeypatch.setattr(httpx, "post", post)
    clf = WorkTypeClassifier("google", "key", model="gemini-2.0-flash")
    assert clf.classify("what is the best approach") == "exploration"
    assert "generateContent?key=key" in seen["url"]


def test_unknown_reply_returns_none(monkeypatch):
    post, _ = _capture({"choices": [{"message": {"content": "banana"}}]})
    monkeypatch.setattr(httpx, "post", post)
    assert WorkTypeClassifier("openai", "k").classify("xyz") is None


def test_failure_is_silent(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "post", boom)
    assert WorkTypeClassifier("openai", "k").classify("add a feature") is None


def test_cache_avoids_repeat_calls(monkeypatch):
    calls = {"n": 0}

    def post(url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        return _Resp({"choices": [{"message": {"content": "feature"}}]})

    monkeypatch.setattr(httpx, "post", post)
    clf = WorkTypeClassifier("openai", "k")
    txt = "implement the brand new widget gallery please now"
    assert clf(txt) == "feature"
    assert clf(txt) == "feature"
    assert calls["n"] == 1  # second call served from cache


def test_get_classifier_reads_standard_azure_env(monkeypatch):
    for v in ("ABEN_CLASSIFIER_PROVIDER", "ABEN_CLASSIFIER_KEY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "azure")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azkey")
    monkeypatch.setenv("AZURE_OPENAI_API_BASE", "https://x/openai/deployments")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4o")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-01-preview")
    clf = get_classifier()
    assert clf is not None and clf.provider == "azure" and clf.model == "gpt-4o"


def test_get_classifier_none_without_config(monkeypatch):
    for v in ("ABEN_CLASSIFIER_PROVIDER", "LLM_PROVIDER"):
        monkeypatch.delenv(v, raising=False)
    assert get_classifier() is None
