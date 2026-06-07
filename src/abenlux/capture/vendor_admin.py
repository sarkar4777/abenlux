"""
Tier-3 capture: vendor admin/analytics connectors. For tools that assemble the prompt
server-side (Cursor agent, Copilot inline), the real prompt never exists on the developer's
machine, so there is nothing local to intercept and MITM would violate ToS and trip abuse
detection. The only legitimate signal is the vendor's own enterprise admin/metrics API:
usage + metadata, never content.

This connector pulls those usage events and maps them into the SAME DerivedRecord the edge
pipeline emits - so Tier-3 spend lands in the exact rollups as Tier-1/2, just flagged with its
tier so the dashboard never implies content-level fidelity it doesn't have. Token counts come
straight from the vendor (exact, billed), there is no content, no embedding, and attribution is
metadata-only (the vendor exposes repo/user, which still joins to an objective via the KG).

Cursor's Admin/Analytics API (Team & Enterprise) exposes per-user usage events with model,
token counts, and cost. We pseudonymize the vendor's user identifier with the SAME HMAC key as
the edge, so a person's Tier-3 Cursor spend and their Tier-2 Aider spend share one pseudonym and
roll up together. The HTTP call itself is injected (a callable) so this stays unit-testable and
offline, deployment passes a real authenticated fetch.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Callable, Optional

from abenlux.attribution.attributor import KnowledgeGraph, attribute
from abenlux.pricing import cost_usd
from abenlux.privacy.pseudonymize import pseudonymize
from abenlux.schema import CaptureTier, DerivedRecord, Provider, WorkContext


def _provider_of(model: str) -> str:
    m = (model or "").lower()
    if "claude" in m:
        return Provider.ANTHROPIC.value
    if "gemini" in m:
        return Provider.GOOGLE.value
    if "gpt" in m or m.startswith("o"):
        return Provider.OPENAI.value
    return Provider.UNKNOWN.value


def cursor_event_to_derived(
    ev: dict, *, hmac_key: bytes, kg: KnowledgeGraph
) -> DerivedRecord:
    """map one Cursor usage event into a DerivedRecord. metadata only, tier3, no content.

    expected event shape (Cursor analytics 'usage events'):
      {"userEmail","model","inputTokens","outputTokens","cacheReadTokens"?,"repoName"?,"timestamp"}
    fields are read defensively because the vendor schema drifts."""
    model = ev.get("model") or ev.get("modelIntent")
    inp = int(ev.get("inputTokens", ev.get("promptTokens", 0)) or 0)
    out = int(ev.get("outputTokens", ev.get("completionTokens", 0)) or 0)
    cache_read = int(ev.get("cacheReadTokens", ev.get("cacheReadInputTokens", 0)) or 0)
    repo = ev.get("repoName") or ev.get("repo")
    actor_raw = ev.get("userEmail") or ev.get("user") or ev.get("userId") or "unknown"

    # attribution still works on metadata (repo join via the KG), no embedding -> join-only
    work = WorkContext(tool="cursor-agent", app_category="ide", repo=repo)
    attr = attribute(SimpleNamespace(work=work), kg)

    cb = cost_usd(model, inp, out, cache_read_tokens=cache_read)
    return DerivedRecord(
        event_id=str(ev.get("id") or ev.get("eventId") or f"cursor:{actor_raw}:{ev.get('timestamp')}"),
        ts=float(ev.get("timestamp", 0) or 0),
        tier=CaptureTier.VENDOR_ADMIN_API.value,
        provider=_provider_of(model or ""),
        actor_pseudonym=pseudonymize(actor_raw, hmac_key),
        request_model=model,
        input_tokens=inp,
        output_tokens=out,
        duplicate_history_tokens=0,           # not observable from vendor metadata
        cache_read_tokens=cache_read,
        tokens_estimated=False,               # vendor-reported, billed-exact
        cost_usd=cb.total,
        cost_priced=cb.priced,
        tool="cursor-agent",
        app_category="ide",
        repo=repo,
        host_os=None,
        embedding=None,                       # no content -> no semantic signal, by design
        objective_id=attr.objective_id,
        objective_label=attr.objective_label,
        is_orphan=attr.is_orphan,
        attribution_method=attr.method,
        attribution_confidence=attr.confidence,
    )


def sync_cursor_usage(
    fetch: Callable[[], list[dict]],
    *,
    hmac_key: bytes,
    kg: KnowledgeGraph,
    insert: Callable[[DerivedRecord], None],
    since: Optional[str] = None,
) -> int:
    """pull a page of Cursor usage events via the injected fetch and persist them as derived.
    returns the number ingested. `fetch` owns auth/pagination, we own normalization."""
    count = 0
    for ev in fetch():
        insert(cursor_event_to_derived(ev, hmac_key=hmac_key, kg=kg))
        count += 1
    return count
