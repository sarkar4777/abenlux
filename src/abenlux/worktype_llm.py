"""
Optional LLM work-type classifier. Branch conventions and the free keyword patterns label the vast
majority of calls. This is the rare, smart fallback for the leftover "unknown" cases - and it is
deliberately tiny so it costs almost nothing:

  * called ONLY when branch + patterns both fail (a small minority of calls),
  * one cheap model, a ~5-token reply, the prompt truncated to a few hundred chars,
  * temperature 0, and an in-memory cache so repeated phrasings never re-ask.

So a thousand-developer org spends fractions of a cent classifying intent, while the label quality
jumps to "ultra smart". It runs on the edge on REDACTED text and only the one-word label persists.

Configurable per the LLM the org already uses: OpenAI, Azure OpenAI, Anthropic (Claude), or Google
(Gemini). Set ABEN_CLASSIFIER_PROVIDER + ABEN_CLASSIFIER_KEY (and model/base_url as needed).
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

_ALLOWED = ("feature", "fix", "refactor", "perf", "exploration", "chore", "docs", "test")
_SYSTEM = ("You label a developer's coding request by intent. Reply with exactly ONE word from: "
           "feature, fix, refactor, perf, exploration, chore, docs, test. No punctuation.")
_DEFAULT_MODEL = {
    "openai": "gpt-4o-mini", "azure": "", "anthropic": "claude-3-5-haiku",
    "google": "gemini-2.0-flash",
}



def _compress(text: str, *, max_chars: int = 900) -> str:
    """extractive intent compression for the LLM: the shared salient-intent extractor, so the work-type
    classifier and the collaboration embedding agree on what the prompt is actually about."""
    from abenlux.salience import salient_intent
    return salient_intent(text, max_chars=max_chars)


def _match(reply: str) -> Optional[str]:
    r = (reply or "").lower()
    for a in _ALLOWED:
        if a in r:
            return a
    return None


class WorkTypeClassifier:
    """tiny, provider-agnostic intent classifier. __call__(text) -> label or None. never raises."""

    def __init__(self, provider: str, key: str, *, model: str = "", base_url: str = "",
                 api_version: str = "", timeout: float = 4.0):
        self.provider = provider.lower()
        self.key = key
        self.model = model or _DEFAULT_MODEL.get(self.provider, "")
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version or "2024-02-15-preview"
        self.timeout = timeout

    def __call__(self, text: Optional[str]) -> Optional[str]:
        if not text or not self.key:
            return None
        return _cached(self, _compress(text))

    def _request(self, text: str):
        import httpx
        p = self.provider
        if p in ("openai", "azure"):
            if p == "azure":
                # base may already include /openai/deployments (common in APIM setups)
                stem = self.base_url if "/deployments" in self.base_url else f"{self.base_url}/openai/deployments"
                url = f"{stem}/{self.model}/chat/completions?api-version={self.api_version}"
                headers = {"api-key": self.key}
                body = {"messages": [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": text}],
                        "max_tokens": 5, "temperature": 0}
            else:
                url = f"{self.base_url or 'https://api.openai.com'}/v1/chat/completions"
                headers = {"Authorization": f"Bearer {self.key}"}
                body = {"model": self.model, "max_tokens": 5, "temperature": 0,
                        "messages": [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": text}]}
            r = httpx.post(url, json=body, headers=headers, timeout=self.timeout)
            return r.json()["choices"][0]["message"]["content"]
        if p == "anthropic":
            url = f"{self.base_url or 'https://api.anthropic.com'}/v1/messages"
            headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01"}
            body = {"model": self.model, "max_tokens": 5, "system": _SYSTEM,
                    "messages": [{"role": "user", "content": text}]}
            r = httpx.post(url, json=body, headers=headers, timeout=self.timeout)
            return "".join(b.get("text", "") for b in r.json().get("content", []))
        if p == "google":
            base = self.base_url or "https://generativelanguage.googleapis.com"
            url = f"{base}/v1beta/models/{self.model}:generateContent?key={self.key}"
            body = {"systemInstruction": {"parts": [{"text": _SYSTEM}]},
                    "contents": [{"parts": [{"text": text}]}],
                    "generationConfig": {"maxOutputTokens": 5, "temperature": 0}}
            r = httpx.post(url, json=body, timeout=self.timeout)
            parts = r.json()["candidates"][0]["content"]["parts"]
            return "".join(pp.get("text", "") for pp in parts)
        return ""

    def classify(self, text: str) -> Optional[str]:
        try:
            return _match(self._request(text))
        except Exception:
            return None


@lru_cache(maxsize=4096)
def _cached(clf: WorkTypeClassifier, text: str) -> Optional[str]:
    # cache keyed on (classifier identity, truncated text) so repeated phrasings never re-ask
    return clf.classify(text)


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default


def get_classifier() -> Optional[WorkTypeClassifier]:
    """build the classifier from env, or None if not configured (the default - patterns only).
    accepts ABEN_CLASSIFIER_* and the developer's existing standard env names so it drops into an
    org's config with no new secrets. credentials are read from env, never stored by this tool."""
    provider = _env("ABEN_CLASSIFIER_PROVIDER", "LLM_PROVIDER").lower()
    if not provider:
        return None
    if provider == "azure":
        key = _env("ABEN_CLASSIFIER_KEY", "AZURE_OPENAI_API_KEY")
        base = _env("ABEN_CLASSIFIER_BASE_URL", "AZURE_OPENAI_API_BASE")
        model = _env("ABEN_CLASSIFIER_MODEL", "AZURE_OPENAI_DEPLOYMENT_FAST", "AZURE_OPENAI_DEPLOYMENT_PRIMARY")
        api_version = _env("ABEN_CLASSIFIER_API_VERSION", "AZURE_OPENAI_API_VERSION")
    elif provider == "openai":
        key = _env("ABEN_CLASSIFIER_KEY", "OPENAI_API_KEY")
        base = _env("ABEN_CLASSIFIER_BASE_URL", "OPENAI_BASE_URL")
        model, api_version = _env("ABEN_CLASSIFIER_MODEL", default="gpt-4o-mini"), ""
    elif provider == "anthropic":
        key = _env("ABEN_CLASSIFIER_KEY", "ANTHROPIC_API_KEY")
        base = _env("ABEN_CLASSIFIER_BASE_URL")
        model, api_version = _env("ABEN_CLASSIFIER_MODEL", default="claude-3-5-haiku"), ""
    elif provider == "google":
        key = _env("ABEN_CLASSIFIER_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
        base = _env("ABEN_CLASSIFIER_BASE_URL")
        model, api_version = _env("ABEN_CLASSIFIER_MODEL", default="gemini-2.0-flash"), ""
    else:
        return None
    if not key:
        return None
    return WorkTypeClassifier(provider, key, model=model, base_url=base, api_version=api_version)
