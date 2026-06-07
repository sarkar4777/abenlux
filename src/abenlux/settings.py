"""
Process configuration, read once from the environment (ABEN_* / OTEL_*). Centralized so the
gateway, the OTLP ingest, and the report CLI agree on the same store path, HMAC key, knowledge
graph, and privacy parameters instead of each re-reading os.getenv with drifting defaults.

The HMAC key is the single most sensitive value: it must come from a secret store the analytics
plane cannot read, so pseudonyms can't be reversed by dictionary attack from inside analytics.
We refuse to silently run management rollups on the default dev key.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    anthropic_upstream: str = os.getenv("ABEN_ANTHROPIC_UPSTREAM", "https://api.anthropic.com")
    openai_upstream: str = os.getenv("ABEN_OPENAI_UPSTREAM", "https://api.openai.com")
    google_upstream: str = os.getenv("ABEN_GOOGLE_UPSTREAM", "https://generativelanguage.googleapis.com")
    hmac_key: str = os.getenv("ABEN_HMAC_KEY", "dev-key-change-me")
    db_path: str = os.getenv("ABEN_DB", "abenlux.db")
    kg_path: str | None = os.getenv("ABEN_KG") or None
    # edge -> central: when set, the on-device agent forwards DERIVED records (no content) to the
    # central collector instead of writing them to a local file. this is what makes the privacy
    # model hold at org scale: raw prompts are redacted on-device and never leave it.
    collector_url: str | None = os.getenv("ABEN_COLLECTOR_URL") or None
    ingest_token: str = os.getenv("ABEN_INGEST_TOKEN", "dev-ingest-token")
    # additional accepted device tokens (comma-separated) so per-device tokens can be rotated
    # without redeploying the collector. the edge agent always presents `ingest_token`.
    extra_ingest_tokens: str = os.getenv("ABEN_INGEST_TOKENS", "")
    tool: str | None = os.getenv("ABEN_TOOL") or None
    app_category: str = os.getenv("ABEN_APP_CATEGORY", "cli")
    actor: str | None = os.getenv("ABEN_ACTOR") or None
    k_anon: int = int(os.getenv("ABEN_K_ANON", "5"))
    dp_epsilon: float = float(os.getenv("ABEN_DP_EPSILON", "1.0"))

    @property
    def hmac_bytes(self) -> bytes:
        return self.hmac_key.encode()

    @property
    def ingest_tokens(self) -> set[str]:
        toks = {self.ingest_token}
        toks.update(t.strip() for t in self.extra_ingest_tokens.split(",") if t.strip())
        return toks

    @property
    def using_dev_key(self) -> bool:
        return self.hmac_key in ("dev-key-change-me", "change-me", "change-me-to-a-long-random-secret")


SETTINGS = Settings()
