"""
Canonical telemetry schema for Abenlux.

This is the single normalization target. Every capture source - Tier 1 (OTel-native
tools), Tier 2 (gateway-intercepted open tools), Tier 3 (vendor admin APIs) - is mapped
into `CanonicalEvent`. Downstream (attribution, eval, dashboards) is source-agnostic.

Field names deliberately mirror the OpenTelemetry GenAI Semantic Conventions
(semconv v1.41, `gen_ai.*`) so the schema is forward-compatible with the standard the
ecosystem is converging on, rather than a bespoke shape we have to maintain forever.

  gen_ai.operation.name      -> operation
  gen_ai.provider.name       -> provider
  gen_ai.request.model       -> request_model
  gen_ai.response.model      -> response_model
  gen_ai.usage.input_tokens  -> usage.input_tokens
  gen_ai.usage.output_tokens -> usage.output_tokens
  gen_ai.response.finish_reasons -> finish_reasons
  gen_ai.input.messages      -> messages (content, opt-in capture)
  gen_ai.output.messages     -> output_messages (content, opt-in capture)

The domain model intentionally depends only on the standard library so it stays
trivially testable and import-light. Web/ML frameworks live at the edges.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional

class CaptureTier(str, Enum):

    OTEL_NATIVE = "tier1_otel_native"        # tool self-instruments (Claude Code, Codex, Copilot)
    GATEWAY_INTERCEPT = "tier2_gateway"      # base-URL overridable tool -> our loopback proxy
    VENDOR_ADMIN_API = "tier3_vendor_admin"  # closed tool, official admin/audit/metrics API
    NETWORK_METADATA = "tier3_network_meta"  # last resort: connection + byte counts only

    @property
    def has_full_content(self) -> bool:
        return self in (CaptureTier.OTEL_NATIVE, CaptureTier.GATEWAY_INTERCEPT)


class Provider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"
    AWS_BEDROCK = "aws.bedrock"
    AZURE_OPENAI = "azure.openai"
    UNKNOWN = "unknown"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def billable(self) -> int:
        # cache reads are discounted, surfaced separately so cost models stay honest
        return self.input_tokens + self.output_tokens


@dataclass
class Message:
    """One turn. `role` in {system, user, assistant, tool}. Content is opt-in and is
    redacted *before* it ever reaches this object in the persistence path."""

    role: str
    content: str = ""
    redacted: bool = False


@dataclass
class WorkContext:
    """Coarse, content-free signals captured at the moment of the call. This is the
    join key for attribution - never the prompt body."""

    tool: Optional[str] = None            # e.g. "claude-code", "aider", "cursor-chat"
    app_category: Optional[str] = None    # "ide" | "cli" | "doc" | "chat"
    repo: Optional[str] = None
    git_branch: Optional[str] = None
    ticket_id: Optional[str] = None       # parsed from branch, e.g. "ACME-1234"
    workspace: Optional[str] = None
    host_os: Optional[str] = None


@dataclass
class CanonicalEvent:
    """One model interaction, normalized. The unit everything else operates on."""

    # identity / lineage
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: float = field(default_factory=time.time)
    tier: CaptureTier = CaptureTier.GATEWAY_INTERCEPT
    provider: Provider = Provider.UNKNOWN

    # gen_ai.* core
    operation: str = "chat"
    request_model: Optional[str] = None
    response_model: Optional[str] = None
    usage: Usage = field(default_factory=Usage)
    finish_reasons: list[str] = field(default_factory=list)

    # content (opt-in, redacted before persistence)
    messages: list[Message] = field(default_factory=list)
    output_messages: list[Message] = field(default_factory=list)
    content_captured: bool = False

    # actor + context (pseudonymized downstream)
    actor_raw: Optional[str] = None       # raw id ONLY in-flight, never persisted
    actor_pseudonym: Optional[str] = None  # HMAC pseudonym, this is what persists
    work: WorkContext = field(default_factory=WorkContext)

    # streaming/perf
    streamed: bool = False
    latency_ms: Optional[float] = None
    duplicate_history_tokens: int = 0     # resent-context bloat detected by the gateway
    tokens_estimated: bool = False        # provider omitted usage -> counts are heuristic

    def input_text(self) -> str:
        return "\n".join(m.content for m in self.messages if m.content)

    def output_text(self) -> str:
        return "\n".join(m.content for m in self.output_messages if m.content)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tier"] = self.tier.value
        d["provider"] = self.provider.value
        return d


@dataclass
class DerivedRecord:
    """The ONLY thing that crosses into the analytics plane. Vectors, scores, counts -
    no readable content. Raw text is discarded after this is produced at the edge."""

    event_id: str
    ts: float
    tier: str
    provider: str
    actor_pseudonym: Optional[str]
    request_model: Optional[str]

    # token facts
    input_tokens: int
    output_tokens: int
    duplicate_history_tokens: int
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    tokens_estimated: bool = False            # True when provider omitted usage and we guessed

    # cost facts (USD, derived from request_model via the pricing table)
    cost_usd: float = 0.0
    cost_priced: bool = True                  # False -> model not in price table, cost is a placeholder

    # content-free work metadata (the attribution join key, safe to aggregate, never PII)
    tool: Optional[str] = None                # "claude-code", "aider", "cursor-agent"
    app_category: Optional[str] = None        # "ide" | "cli" | "doc" | "chat"
    repo: Optional[str] = None
    host_os: Optional[str] = None

    # derived signals
    embedding: Optional[list[float]] = None   # for clustering / collaboration matching
    quality_score: Optional[float] = None     # 0..1, populated by sampled eval only
    acceptance: Optional[float] = None         # 1 - edit_distance(output, committed)

    # waste signals (leading)
    is_retry_loop: bool = False
    retry_similarity: float = 0.0

    # attribution
    objective_id: Optional[str] = None
    objective_label: Optional[str] = None
    is_orphan: bool = True
    attribution_method: Optional[str] = None  # "ticket_join" | "repo_join" | "semantic" | "none"
    attribution_confidence: float = 0.0       # 1.0 for joins, <1 for semantic, 0 for orphan

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
