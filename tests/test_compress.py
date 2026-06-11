"""The compression layer: each strategy must save tokens, never corrupt the request, and the
lossless ones must preserve information. Compression runs on the outbound request at the gateway."""
import json

from abenlux.compress import (
    compress_request,
    enabled_strategies,
    strategies,
)


def _anthropic(system, user):
    return {"model": "claude-opus-4-8", "max_tokens": 64, "system": system,
            "messages": [{"role": "user", "content": user}]}


def test_prefix_stabilize_moves_volatile_token_to_the_end_losslessly():
    # an injected date at the TOP of the system prompt busts the cache-stable prefix every call
    body = _anthropic("Today is 2026-06-11 14:32. You are a senior engineer. Follow the house style.",
                      "fix the bug")
    out = compress_request(body, "anthropic", [strategies()["prefix_stabilize"]])
    assert "prefix_stabilize" in out.applied
    sysout = out.body["system"]
    # the stable instruction now leads (cache can hit); the date is preserved, just moved to the end
    assert sysout.startswith("You are a senior engineer")
    assert "2026-06-11" in sysout                       # lossless: the date is still there
    # nothing dropped
    assert "house style" in sysout


def test_prefix_stabilize_noop_when_prefix_is_already_stable():
    body = _anthropic("You are a senior engineer. Be terse.", "hi")
    out = compress_request(body, "anthropic", [strategies()["prefix_stabilize"]])
    assert out.applied == [] and out.body == body


def test_command_trim_strips_ansi_and_collapses_repeats():
    noisy = "\x1b[31mERROR\x1b[0m\n" + "\n".join(["retrying connection"] * 50) + "\ndone"
    body = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": noisy}]}
    out = compress_request(body, "anthropic", [strategies()["command_trim"]])
    text = out.body["messages"][0]["content"]
    assert "\x1b[" not in text                           # ANSI gone
    assert "... x50" in text                             # 50 repeats collapsed to one + count
    assert out.saved_tokens > 0


def test_otsl_tables_compacts_html_table_losslessly():
    html = ("<table><tr><th>obj</th><th>cost</th></tr>"
            "<tr><td>checkout</td><td>$12</td></tr><tr><td>mobile</td><td>$7</td></tr></table>")
    body = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "data: " + html}]}
    out = compress_request(body, "anthropic", [strategies()["otsl_tables"]])
    text = out.body["messages"][0]["content"]
    assert "<otsl>" in text and "<table" not in text.lower()
    for cell in ("obj", "cost", "checkout", "$12", "mobile", "$7"):   # every cell preserved
        assert cell in text
    assert out.saved_tokens >= 0


def test_compress_json_minifies_blocks_losslessly():
    pretty = json.dumps({"a": 1, "b": [1, 2, 3], "c": {"d": "e"}}, indent=2)
    body = {"model": "claude-opus-4-8",
            "messages": [{"role": "user", "content": f"config:\n```json\n{pretty}\n```"}]}
    out = compress_request(body, "anthropic", [strategies()["compress_json"]])
    text = out.body["messages"][0]["content"]
    block = text.split("```json")[1].split("```")[0].strip()
    assert json.loads(block) == {"a": 1, "b": [1, 2, 3], "c": {"d": "e"}}   # identical parsed JSON
    assert "  " not in block                              # whitespace gone


def test_slim_tools_drops_duplicate_definitions():
    tool = {"name": "search", "description": "search the repo", "input_schema": {"type": "object"}}
    body = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "go"}],
            "tools": [tool, dict(tool), {"name": "edit", "description": "edit a file"}]}
    out = compress_request(body, "anthropic", [strategies()["slim_tools"]])
    names = [t["name"] for t in out.body["tools"]]
    assert names == ["search", "edit"]                   # exact dupe removed, distinct kept


def test_openai_and_gemini_shapes_are_handled():
    oai = {"model": "gpt-4o-mini",
           "messages": [{"role": "system", "content": "Today is 2026-06-11. Be precise."},
                        {"role": "user", "content": "hi"}]}
    out = compress_request(oai, "openai", [strategies()["prefix_stabilize"]])
    assert out.body["messages"][0]["content"].startswith("Be precise")
    gem = {"contents": [{"role": "user", "parts": [{"text": "x" * 10}]}],
           "systemInstruction": {"parts": [{"text": "Today is 2026-06-11. Be precise."}]}}
    out2 = compress_request(gem, "google", [strategies()["prefix_stabilize"]])
    assert out2.body["systemInstruction"]["parts"][0]["text"].startswith("Be precise")


def test_a_broken_strategy_is_skipped_never_raises():
    from abenlux.compress import Strategy
    def boom(body, provider):
        raise ValueError("explode")
    body = _anthropic("sys", "user")
    out = compress_request(body, "anthropic", [Strategy("boom", True, True, False, boom)])
    assert out.body == body and out.applied == []        # request forwarded unchanged


def test_enabled_strategies_spec():
    assert enabled_strategies("off") == []
    assert {s.name for s in enabled_strategies(None)} == {"prefix_stabilize"}   # safe default only
    assert {s.name for s in enabled_strategies("all")} == set(strategies())
    assert [s.name for s in enabled_strategies("command_trim,otsl_tables")] == ["command_trim", "otsl_tables"]


def test_default_strategies_are_lossless_and_non_content_rewriting():
    # the only auto-on strategy must be lossless and must not rewrite prompt CONTENT (DX safety rule)
    for s in strategies().values():
        if s.default_on:
            assert s.lossless and not s.rewrites_prompt


def test_report_surfaces_compression_yield(tmp_path):
    # saved tokens + cache hits flow content-free to the management report as a yield block
    from abenlux.analytics.reports import management_report
    from abenlux.schema import DerivedRecord
    from abenlux.store import DerivedStore
    s = DerivedStore(tmp_path / "c.db")
    for i in range(5):
        s.insert(DerivedRecord(event_id=f"e{i}", ts=1.0, tier="t", provider="anthropic",
                               actor_pseudonym=f"a{i}", request_model="claude-opus-4-8",
                               input_tokens=1000, output_tokens=100, duplicate_history_tokens=0,
                               cost_usd=2.0, objective_id="O", objective_label="O", is_orphan=False,
                               saved_input_tokens=300, compression="prefix_stabilize",
                               served_from_cache=(i == 0)))
    rep = management_report(s, k=5)
    cz = rep["compression"]
    assert cz["saved_input_tokens"] == 1500 and cz["cache_hits"] == 1 and cz["saved_usd"] >= 0
    s.close()


def test_collector_does_not_reprice_a_cache_hit():
    # a local-cache hit made no upstream call, so the collector must keep its cost at 0, not re-price it
    from abenlux.api.server import _harden_inbound
    from abenlux.schema import DerivedRecord
    rec = DerivedRecord(event_id="c", ts=1.0, tier="t", provider="anthropic", actor_pseudonym="px",
                        request_model="claude-opus-4-8", input_tokens=1000, output_tokens=100,
                        duplicate_history_tokens=0, cost_usd=0.0, cost_priced=True, served_from_cache=True)
    _harden_inbound(rec)
    assert rec.cost_usd == 0.0 and rec.cost_priced   # not re-priced to a real cost
