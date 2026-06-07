"""
Onboarding generator. The org-scale problem isn't capturing one call - it's getting a hundred
developers on Windows, macOS, and Linux, each using a different tool, pointed at Abenlux without
a day of per-person setup. This turns "which env vars / config does MY tool on MY machine need?"
into one command: `abenlux onboard <tool> --shell <shell>`.

It is driven entirely by the capability registry, so a tool's tier decides the recipe:
  * Tier 2 (base_url)  -> the exact base-URL env var for that tool's protocol, in your shell.
  * Tier 1 (OTel)      -> the OTEL_* env + tool-specific enablement (Claude Code flags, Codex
                          config.toml, Gemini CLI telemetry flags).
  * Tier 3 (vendor)    -> honest: nothing to set locally, configure the vendor admin API token
                          and run the periodic sync. No content is capturable for these.

Shell rendering is cross-platform: PowerShell ($env:), cmd (set), and POSIX (export). The
default shell is inferred from the host OS so the happy path is just `abenlux onboard <tool>`.
"""
from __future__ import annotations

import platform
from dataclasses import dataclass, field

from abenlux.capture.tiers import ToolCapability, get
from abenlux.schema import CaptureTier

DEFAULT_BASE = "http://127.0.0.1:8088"


@dataclass
class Onboarding:
    tool: str
    tier: str
    env: dict[str, str] = field(default_factory=dict)
    files: list[tuple[str, str]] = field(default_factory=list)  # (path, content)
    commands: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render_env(self, shell: str) -> str:
        if not self.env:
            return ""
        lines = [_env_line(k, v, shell) for k, v in self.env.items()]
        return "\n".join(lines)


def default_shell() -> str:
    return "powershell" if platform.system() == "Windows" else "bash"


def _env_line(key: str, value: str, shell: str) -> str:
    if shell == "powershell":
        return f'$env:{key} = "{value}"'
    if shell == "cmd":
        return f"set {key}={value}"
    return f'export {key}="{value}"'  # bash / zsh / posix


def _gateway_env(protocol: str, base: str) -> dict[str, str]:
    if protocol == "anthropic":
        return {"ANTHROPIC_BASE_URL": base}
    if protocol == "openai":
        # OpenAI clients append /chat/completions to a base that ends in /v1
        return {"OPENAI_BASE_URL": f"{base}/v1", "OPENAI_API_BASE": f"{base}/v1"}
    if protocol == "gemini":
        return {"GOOGLE_GEMINI_BASE_URL": base}
    return {"ANTHROPIC_BASE_URL": base, "OPENAI_BASE_URL": f"{base}/v1"}


def _otel_env(base: str) -> dict[str, str]:
    return {
        "OTEL_EXPORTER_OTLP_ENDPOINT": base,
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    }


def _tier1(cap: ToolCapability, base: str) -> Onboarding:
    ob = Onboarding(cap.tool, cap.tier.value, env=_otel_env(base))
    if cap.tool == "claude-code":
        ob.env.update({
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "OTEL_LOGS_EXPORTER": "otlp",
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
            "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
        })
        ob.notes.append("Content arrives as OTLP log events, redaction still runs at ingest.")
    elif cap.tool == "openai-codex":
        ob.files.append((
            "~/.codex/config.toml",
            "[otel]\nexporter = \"otlp-http\"\nendpoint = \"%s\"\nprotocol = \"json\"\n" % base,
        ))
        ob.notes.append("`codex exec` exports traces+logs but not metrics, the interactive CLI is full.")
    elif cap.tool == "gemini-cli":
        ob.commands.append(f"gemini --telemetry --telemetry-target=local --telemetry-otlp-endpoint={base}")
        ob.notes.append("Or set telemetry in ~/.gemini/settings.json, emits gen_ai generate_content spans.")
    elif cap.tool == "vscode-copilot":
        ob.notes.append("Only agent interactions emit OTel, inline completions are Tier 3 (admin API).")
    return ob


def _tier3(cap: ToolCapability, base: str) -> Onboarding:
    ob = Onboarding(cap.tool, cap.tier.value)
    ob.notes.append(cap.note)
    ob.notes.append("No local content is capturable for this tool - by design, not omission.")
    if cap.tool == "cursor-agent":
        ob.env["ABEN_CURSOR_API_KEY"] = "<cursor-admin-api-key>"
        ob.commands.append("abenlux sync-cursor   # pulls usage events (model/token/cost), metadata only")
    return ob


def generate(tool: str, *, base: str = DEFAULT_BASE) -> Onboarding | None:
    cap = get(tool)
    if cap is None:
        return None
    if cap.tier == CaptureTier.OTEL_NATIVE:
        return _tier1(cap, base)
    if cap.tier == CaptureTier.VENDOR_ADMIN_API:
        return _tier3(cap, base)
    # Tier 2: base_url override
    ob = Onboarding(cap.tool, cap.tier.value, env=_gateway_env(cap.protocol, base))
    if cap.note:
        ob.notes.append(cap.note)
    ob.notes.append("Start the gateway first: `abenlux gateway` (loopback, localhost only).")
    return ob


def render(tool: str, *, shell: str | None = None, base: str = DEFAULT_BASE) -> str:
    shell = shell or default_shell()
    ob = generate(tool, base=base)
    if ob is None:
        known = "see `abenlux tiers` for supported tools"
        return f"unknown tool {tool!r} - {known}"
    out = [f"# Abenlux onboarding: {ob.tool}  (tier: {ob.tier}, shell: {shell})", ""]
    env_block = ob.render_env(shell)
    if env_block:
        out += ["# environment:", env_block, ""]
    for path, content in ob.files:
        out += [f"# write {path}:", content, ""]
    for cmd in ob.commands:
        out += [f"# run: {cmd}"]
    if ob.commands:
        out.append("")
    for note in ob.notes:
        out.append(f"# note: {note}")
    return "\n".join(out).rstrip() + "\n"
