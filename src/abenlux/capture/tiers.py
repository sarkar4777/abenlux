"""
Capability-tier registry. The honest, load-bearing fact about each tool: HOW it can be
captured and therefore WHAT fidelity we can claim. The dashboard and the onboarding generator
both read from this so the product never presents metadata-only data as full-content data, and
so a developer is handed the exact setup their specific tool needs.

Determined by how the tool makes its call:
  * Tier 1 - self-instruments to OpenTelemetry GenAI conventions (full content opt-in).
  * Tier 2 - honors a custom base_url -> our loopback gateway sees the full exchange.
  * Tier 3 - assembles the prompt server-side on a vendor backend, so the real prompt never
             exists on the device. Best legitimate path = vendor admin/audit API (usage +
             metadata). Reverse-engineered MITM is a ToS/abuse-ban risk and is NOT used.

`protocol` tells the gateway which base_url env var family a Tier-2 tool speaks, so onboarding
can emit the right variable. Reflects researched behavior as of 2026-06, tools move between
tiers as they adopt OTel GenAI, so this is data, not logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from abenlux.schema import CaptureTier


@dataclass(frozen=True)
class ToolCapability:
    tool: str
    tier: CaptureTier
    captures_full_prompt: bool
    captures_response: bool
    exact_tokens: bool
    ingest_path: str
    note: str = ""
    protocol: str = ""                  # "anthropic" | "openai" | "gemini" (Tier-2 only)
    aliases: tuple[str, ...] = field(default_factory=tuple)


def _t1(tool: str, ingest: str, note: str) -> ToolCapability:
    return ToolCapability(tool, CaptureTier.OTEL_NATIVE, True, True, True, ingest, note)


def _t2(tool: str, protocol: str, note: str = "", aliases: tuple = ()) -> ToolCapability:
    return ToolCapability(
        tool, CaptureTier.GATEWAY_INTERCEPT, True, True, True,
        "base_url -> loopback gateway", note, protocol, aliases,
    )


def _t3(tool: str, ingest: str, note: str, captures_response: bool = True) -> ToolCapability:
    return ToolCapability(tool, CaptureTier.VENDOR_ADMIN_API, False, captures_response, False, ingest, note)


_TOOLS: list[ToolCapability] = [
    # ---- Tier 1: self-instrumented to OTel GenAI ----
    _t1("claude-code", "OTLP/HTTP -> /v1/otel (logs+traces)",
        "CLAUDE_CODE_ENABLE_TELEMETRY=1. Content arrives as OTLP log events, "
        "enable OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT for prompts."),
    _t1("openai-codex", "OTLP/HTTP -> /v1/otel",
        "[otel] in ~/.codex/config.toml (otlp-http). `codex exec` lacks metrics, cli is full."),
    _t1("gemini-cli", "OTLP/HTTP -> /v1/otel",
        "google-gemini telemetry: gen_ai generate_content spans + token-usage metrics."),
    _t1("vscode-copilot", "OTLP/HTTP -> /v1/otel (agent traces)",
        "Agent interactions emit traces/metrics. Inline completions remain Tier 3."),
    # ---- Tier 2: base_url-overridable, gateway sees the full exchange ----
    _t2("aider", "anthropic", "Set ANTHROPIC_BASE_URL or OPENAI_BASE_URL."),
    _t2("cline", "anthropic", "VS Code, supports custom Anthropic/OpenAI-compatible base url."),
    _t2("continue", "openai", "config.json apiBase -> gateway."),
    _t2("opencode", "anthropic", "Terminal agent, OpenAI/Anthropic compatible base url."),
    _t2("crush", "anthropic", "Charm/Crush (opencode successor). Broadest OS support incl. Windows/BSD."),
    _t2("pi", "anthropic", "Speaks Anthropic/OpenAI protocol, honors base_url override."),
    _t2("droid", "anthropic", "Factory Droid, custom model base_url field.", ("factory-droid",)),
    _t2("forgecode", "openai", "ForgeCode, OpenAI-compatible base url.", ("forge",)),
    _t2("goose", "openai", "Block Goose, OpenAI-compatible provider base url."),
    _t2("roo-code", "openai", "Roo Code (Cline fork), custom base url.", ("roo",)),
    _t2("kilo-code", "openai", "Kilo Code, custom base url.", ("kilo",)),
    # ---- Tier 3: prompt assembled server-side, vendor admin API only ----
    _t3("cursor-agent", "Cursor Admin + Analytics API (usage events)",
        "Prompt assembled on Cursor's backend, not on device. Admin API gives per-user "
        "model/token/cost. Custom base_url honored ONLY for chat, not the agent."),
    _t3("copilot-inline", "GitHub Copilot Enterprise metrics/audit API",
        "Context assembled on GitHub's proxy, individual plans have no custom endpoint.", False),
    _t3("windsurf", "Windsurf/Codeium enterprise analytics API",
        "Cascade prompt assembled server-side, enterprise analytics expose usage metadata."),
    _t3("amazon-q", "Amazon Q Developer usage in CloudTrail/usage reports",
        "Prompt assembled in AWS, usage metadata via CloudTrail + Q usage reports."),
]

REGISTRY: dict[str, ToolCapability] = {}
for _cap in _TOOLS:
    REGISTRY[_cap.tool] = _cap
    for _alias in _cap.aliases:
        REGISTRY[_alias] = _cap


def get(tool: str) -> ToolCapability | None:
    return REGISTRY.get(tool.lower())


def canonical_tools() -> list[ToolCapability]:
    """unique capabilities (no alias duplicates), in registry order."""
    seen: set[str] = set()
    out: list[ToolCapability] = []
    for cap in _TOOLS:
        if cap.tool not in seen:
            seen.add(cap.tool)
            out.append(cap)
    return out
