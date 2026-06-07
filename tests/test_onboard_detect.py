from abenlux.agent.detect import detect
from abenlux.onboard import generate, render


def test_tier2_emits_base_url_for_protocol():
    ob = generate("aider")
    assert ob.tier == "tier2_gateway"
    assert ob.env.get("ANTHROPIC_BASE_URL", "").startswith("http://127.0.0.1:8088")
    ob_oai = generate("continue")
    assert ob_oai.env["OPENAI_BASE_URL"].endswith("/v1")


def test_tier1_emits_otel_env_and_claude_flags():
    ob = generate("claude-code")
    assert ob.tier == "tier1_otel_native"
    assert ob.env["OTEL_EXPORTER_OTLP_ENDPOINT"].startswith("http://")
    assert ob.env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert ob.env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/json"


def test_tier3_is_honest_about_no_local_content():
    ob = generate("cursor-agent")
    assert ob.tier == "tier3_vendor_admin"
    assert any("No local content" in n for n in ob.notes)


def test_shell_rendering_variants():
    assert '$env:ANTHROPIC_BASE_URL = "' in render("aider", shell="powershell")
    assert "set ANTHROPIC_BASE_URL=" in render("aider", shell="cmd")
    assert 'export ANTHROPIC_BASE_URL="' in render("aider", shell="bash")


def test_unknown_tool_renders_help():
    assert "unknown tool" in render("not-a-real-tool")


def test_detect_explicit_override(monkeypatch):
    monkeypatch.setenv("ABEN_TOOL", "crush")
    monkeypatch.setenv("ABEN_APP_CATEGORY", "cli")
    d = detect()
    assert d.tool == "crush" and d.source == "override"


def test_detect_env_marker(monkeypatch):
    monkeypatch.delenv("ABEN_TOOL", raising=False)
    for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CURSOR_TRACE_ID", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AIDER_MODEL", "claude")
    d = detect()
    assert d.tool == "aider" and d.source == "env"
