"""
The edge pipeline. The order of operations is the entire privacy/security posture:

  capture (full content)
    -> REDACT (destroy secrets/PII before anything is written or derived)
    -> DERIVE (embedding, token facts, waste signals - vectors & counts, not text)
    -> ATTRIBUTE (join to objective, flag orphan)
    -> PSEUDONYMIZE (one-way hash actor, drop raw id)
    -> PERSIST DERIVED ONLY (raw content is discarded here)

Nothing readable and management-visible is ever stored centrally. This runs on the
device (or a per-tenant enclave).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from abenlux.attribution.attributor import KnowledgeGraph, attribute
from abenlux.pricing import cost_usd
from abenlux.privacy.pseudonymize import strip_raw_actor_inplace
from abenlux.processing.redact import redact_event_inplace
from abenlux.processing.waste import SessionWasteMonitor, WasteSignal
from abenlux.schema import CanonicalEvent, DerivedRecord


@dataclass
class PipelineResult:
    record: DerivedRecord
    waste_signals: list[WasteSignal]   # developer-facing only, not persisted as content
    redactions: int


def embed_stub(text: str, dims: int = 16) -> list[float]:
    """Deterministic cheap embedding so the scaffold runs offline. Replace with a real
    sentence-embedding model in deployment (injected, not hard-coded)."""
    import hashlib
    vec = [0.0] * dims
    for tok in text.lower().split():
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        vec[h % dims] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [round(v / norm, 4) for v in vec]


def process(
    event: CanonicalEvent,
    *,
    kg: KnowledgeGraph,
    hmac_key: bytes,
    waste_monitor: Optional[SessionWasteMonitor] = None,
    capture_embedding: bool = True,
    embed_fn=embed_stub,
) -> PipelineResult:
    # 1. REDACT FIRST - before derive, before persist, before anything touches disk
    redactions = redact_event_inplace(event)

    # 2. waste signals (computed on redacted text, only the score/flag persists)
    signals: list[WasteSignal] = []
    if waste_monitor is not None:
        signals = waste_monitor.observe(event)

    # 3. DERIVE - embedding + token facts (no readable text leaves this function persisted)
    embedding = embed_fn(event.input_text()) if capture_embedding else None

    # 4. ATTRIBUTE - join first, semantic fallback only if an embedding exists
    attr = attribute(event, kg, query_embedding=embedding)

    # 5. PSEUDONYMIZE - one-way, drop raw actor
    strip_raw_actor_inplace(event, hmac_key)

    is_retry = any(s.kind == "retry_loop" for s in signals)
    retry_sim = max((s.similarity for s in signals if s.kind == "retry_loop"), default=0.0)

    # cost is derived from the request model + the (possibly cache-discounted) token facts
    cb = cost_usd(
        event.request_model,
        event.usage.input_tokens,
        event.usage.output_tokens,
        cache_read_tokens=event.usage.cache_read_tokens,
        cache_creation_tokens=event.usage.cache_creation_tokens,
    )

    # 6. build DERIVED record - this is the only thing that crosses into analytics
    record = DerivedRecord(
        event_id=event.event_id,
        ts=event.ts,
        tier=event.tier.value,
        provider=event.provider.value,
        actor_pseudonym=event.actor_pseudonym,
        request_model=event.request_model,
        input_tokens=event.usage.input_tokens,
        output_tokens=event.usage.output_tokens,
        duplicate_history_tokens=event.duplicate_history_tokens,
        cache_read_tokens=event.usage.cache_read_tokens,
        cache_creation_tokens=event.usage.cache_creation_tokens,
        tokens_estimated=event.tokens_estimated,
        cost_usd=cb.total,
        cost_priced=cb.priced,
        tool=event.work.tool,
        app_category=event.work.app_category,
        repo=event.work.repo,
        host_os=event.work.host_os,
        embedding=embedding,
        is_retry_loop=is_retry,
        retry_similarity=retry_sim,
        objective_id=attr.objective_id,
        objective_label=attr.objective_label,
        is_orphan=attr.is_orphan,
        attribution_method=attr.method,
        attribution_confidence=attr.confidence,
    )

    # raw content is dropped: clear bodies after derivation
    for m in event.messages + event.output_messages:
        m.content = ""

    return PipelineResult(record=record, waste_signals=signals, redactions=redactions)
