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


def test_prefix_stabilize_leaves_a_mid_sentence_date_untouched():
    # a real date inside prose must NOT be yanked out (that would mangle the prompt, not stabilize a cache)
    body = _anthropic("The outage on 2024-01-15 was severe. Follow the house style.", "fix it")
    out = compress_request(body, "anthropic", [strategies()["prefix_stabilize"]])
    assert out.applied == [] and out.body == body          # nothing moved, prose intact


def test_compression_skips_pathological_large_input_fast():
    import time
    # a backtracking-bait blob (many unclosed <table tags) must not stall: above the size cap the
    # regex strategies leave it untouched.
    blob = "<table " * 80000                                # ~640 KB, well over the cap
    body = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": blob}]}
    t0 = time.time()
    out = compress_request(body, "anthropic", [strategies()["otsl_tables"], strategies()["compress_json"]])
    assert (time.time() - t0) < 2.0                         # bounded, no quadratic blowup
    assert "otsl_tables" not in out.applied                 # skipped, body unchanged


def test_otsl_preserves_tables_with_merged_cells():
    html = "<table><tr><th colspan=2>span</th></tr><tr><td>a</td><td>b</td></tr></table>"
    body = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": html}]}
    out = compress_request(body, "anthropic", [strategies()["otsl_tables"]])
    assert "<table" in out.body["messages"][0]["content"]   # merged-cell table left intact, not transcoded


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
    assert out.saved_tokens > 0                           # the dropped definition is billed input, so it counts


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
    # the safe default set is the lossless, behavior-safe ones that need no developer decision
    assert {s.name for s in enabled_strategies(None)} == {"prefix_stabilize", "cache_breakpoints"}
    assert {s.name for s in enabled_strategies("all")} == set(strategies())
    assert [s.name for s in enabled_strategies("command_trim,otsl_tables")] == ["command_trim", "otsl_tables"]


def test_default_strategies_are_lossless_and_non_content_rewriting():
    # the only auto-on strategy must be lossless and must not rewrite prompt CONTENT (DX safety rule)
    for s in strategies().values():
        if s.default_on:
            assert s.lossless and not s.rewrites_prompt


def test_cache_breakpoints_marks_the_stable_system_prompt():
    big = "You are a senior engineer. Follow the house rules. " * 80   # well over the size guard
    body = _anthropic(big, "fix the bug")
    out = compress_request(body, "anthropic", [strategies()["cache_breakpoints"]])
    assert "cache_breakpoints" in out.applied
    sysout = out.body["system"]
    assert isinstance(sysout, list) and sysout[-1]["cache_control"] == {"type": "ephemeral"}
    assert big.strip()[:20] in sysout[-1]["text"]            # the words the model reads are unchanged


def test_cache_breakpoints_skips_a_tiny_prompt_and_non_anthropic():
    out = compress_request(_anthropic("You are terse.", "hi"), "anthropic",
                           [strategies()["cache_breakpoints"]])
    assert out.applied == []                                  # too small to be worth caching
    oai = {"model": "gpt-4o-mini", "messages": [{"role": "system", "content": "x" * 5000},
                                                {"role": "user", "content": "hi"}]}
    assert compress_request(oai, "openai", [strategies()["cache_breakpoints"]]).applied == []


def test_tool_result_trim_folds_noisy_tool_output():
    noisy = "\x1b[31mERR\x1b[0m\n" + "\n".join(["retry connection"] * 60) + "\ndone"
    body = {"model": "claude-opus-4-8", "messages": [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": noisy},
            {"type": "text", "text": "what failed?"}]}]}
    out = compress_request(body, "anthropic", [strategies()["tool_result_trim"]])
    trimmed = out.body["messages"][0]["content"][0]["content"]
    assert "\x1b[" not in trimmed and "... x60" in trimmed    # color gone, repeats folded
    assert out.body["messages"][0]["content"][1]["text"] == "what failed?"   # the question is untouched
    assert out.saved_tokens > 0                               # the saving on tool output is now counted


def test_per_strategy_attribution_sums_to_total():
    # the chain reports what EACH strategy removed, and the parts add up to the whole
    noisy = "\x1b[31mE\x1b[0m\n" + "\n".join(["dupe line"] * 40)
    pretty = json.dumps({"a": [1, 2, 3], "b": {"c": "d"}}, indent=2)
    body = {"model": "claude-opus-4-8",
            "messages": [{"role": "user", "content": noisy + "\n```json\n" + pretty + "\n```"}]}
    out = compress_request(body, "anthropic", [strategies()["command_trim"], strategies()["compress_json"]])
    assert set(out.per_strategy) == {"command_trim", "compress_json"}
    assert all(v >= 0 for v in out.per_strategy.values())
    assert sum(out.per_strategy.values()) == out.saved_tokens > 0


def test_slim_tools_attribution_counts_tool_tokens():
    tool = {"name": "search", "description": "search the repo with a long enough schema to matter",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}
    body = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "go"}],
            "tools": [tool, dict(tool), {"name": "edit", "description": "edit"}]}
    out = compress_request(body, "anthropic", [strategies()["slim_tools"]])
    assert out.per_strategy.get("slim_tools", 0) > 0     # the dropped duplicate definition is billed input


def test_report_attributes_compression_by_strategy(tmp_path):
    import json as _json

    from abenlux.analytics.reports import management_report
    from abenlux.schema import DerivedRecord
    from abenlux.store import DerivedStore
    s = DerivedStore(tmp_path / "c.db")
    for i in range(5):
        s.insert(DerivedRecord(event_id=f"e{i}", ts=1.0, tier="t", provider="anthropic",
                               actor_pseudonym=f"a{i}", request_model="claude-opus-4-8",
                               input_tokens=1000, output_tokens=100, duplicate_history_tokens=0,
                               cost_usd=2.0, objective_id="O", objective_label="O", is_orphan=False,
                               saved_input_tokens=500, compression="command_trim,otsl_tables",
                               compression_detail=_json.dumps({"command_trim": 400, "otsl_tables": 100})))
    rep = management_report(s, k=5)
    bys = rep["compression"]["by_strategy"]
    assert bys["command_trim"]["tokens"] == 2000 and bys["otsl_tables"]["tokens"] == 500
    # ordered by tokens desc
    assert list(bys) == ["command_trim", "otsl_tables"]
    s.close()


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


def test_collector_prices_a_cache_hit_at_zero_billable_tokens():
    # a real local-cache hit carries ZERO billable tokens (the edge moved the avoided input into
    # saved_input_tokens), so it re-prices to $0 from the tokens, with no trusted flag.
    from abenlux.api.server import _harden_inbound
    from abenlux.schema import DerivedRecord
    rec = DerivedRecord(event_id="c", ts=1.0, tier="t", provider="anthropic", actor_pseudonym="px",
                        request_model="claude-opus-4-8", input_tokens=0, output_tokens=0,
                        duplicate_history_tokens=0, saved_input_tokens=1000, served_from_cache=True)
    _harden_inbound(rec)
    assert rec.cost_usd == 0.0 and rec.cost_priced and rec.saved_input_tokens == 1000


def test_served_from_cache_flag_cannot_deflate_a_real_call():
    # the deflation attack: a hostile edge marks a real expensive call served_from_cache to hide its
    # cost. the collector re-prices from the real tokens regardless, so the spend is NOT zeroed.
    from abenlux.api.server import _harden_inbound
    from abenlux.schema import DerivedRecord
    rec = DerivedRecord(event_id="d", ts=1.0, tier="t", provider="anthropic", actor_pseudonym="px",
                        request_model="claude-opus-4-8", input_tokens=2_000_000, output_tokens=200_000,
                        duplicate_history_tokens=0, cost_usd=0.0, cost_priced=True, served_from_cache=True)
    _harden_inbound(rec)
    assert rec.cost_usd > 0.0   # the real 2M-token call is priced, not hidden


def test_ingest_clamps_negative_and_absurd_token_counts():
    from abenlux.api.server import _harden_inbound
    from abenlux.schema import DerivedRecord
    rec = DerivedRecord(event_id="n", ts=1.0, tier="t", provider="anthropic", actor_pseudonym="px",
                        request_model="claude-opus-4-8", input_tokens=-5, output_tokens=10**18,
                        duplicate_history_tokens=0, saved_input_tokens=-99)
    _harden_inbound(rec)
    assert rec.input_tokens == 0 and rec.output_tokens <= 100_000_000 and rec.saved_input_tokens == 0
    assert rec.cost_usd >= 0.0


def test_value_numerator_joins_outcomes_to_spend(tmp_path):
    # spend joined to shipped-work outcomes produces a return-on-spend line, k-gated
    from abenlux.analytics.outcomes import OutcomeStore
    from abenlux.analytics.reports import management_report
    from abenlux.schema import DerivedRecord
    from abenlux.store import DerivedStore
    s = DerivedStore(tmp_path / "c.db")
    for i in range(5):
        s.insert(DerivedRecord(event_id=f"e{i}", ts=1.0, tier="t", provider="anthropic",
                               actor_pseudonym=f"a{i}", request_model="claude-opus-4-8",
                               input_tokens=1000, output_tokens=100, duplicate_history_tokens=0,
                               cost_usd=2.0, objective_id="O", objective_label="O", is_orphan=False))
    oc = OutcomeStore(tmp_path / "o.db")
    for i in range(4):
        oc.record({"outcome_id": f"o{i}", "objective_id": "O", "merged": 1,
                   "lines_added": 50, "lines_removed": 10})
    oc.record({"outcome_id": "o9", "objective_id": "O", "merged": 0, "reverted": 1})
    rep = management_report(s, k=5, outcomes=oc.by_objective())
    v = rep["value"]
    assert v["merged"] == 4 and v["changes"] == 5 and v["merge_rate"] == 0.8
    assert v["cost_per_merged_change"] == 2.5      # $10 spend / 4 merged
    assert v["net_lines"] == 160                   # 4 * (50-10)
    oc.close()
    s.close()


def test_negotiation_pack_blended_rate_and_k_gate(tmp_path):
    from abenlux.analytics.negotiation import negotiation_pack
    from abenlux.schema import DerivedRecord
    from abenlux.store import DerivedStore
    s = DerivedStore(tmp_path / "n.db")
    # below k -> suppressed
    s.insert(DerivedRecord(event_id="e0", ts=1.0, tier="t", provider="anthropic", actor_pseudonym="a0",
                           request_model="claude-opus-4-8", input_tokens=500000, output_tokens=0,
                           duplicate_history_tokens=0, cost_usd=2.5))
    assert negotiation_pack(s, k=5)["ready"] is False
    for i in range(1, 5):
        s.insert(DerivedRecord(event_id=f"e{i}", ts=1.0, tier="t", provider="anthropic",
                               actor_pseudonym=f"a{i}", request_model="claude-opus-4-8",
                               input_tokens=500000, output_tokens=0, duplicate_history_tokens=0, cost_usd=2.5))
    pack = negotiation_pack(s, k=5)
    assert pack["ready"] is True
    assert pack["blended_usd_per_mtok"] == 5.0          # $12.5 over 2.5M tokens = $5/Mtok
    assert pack["provider_concentration"] == 1.0        # all anthropic
    assert pack["committed_use_scenarios"][0]["discount_pct"] == 10
    s.close()


def test_orphan_recovery_proposes_a_named_objective_for_a_shared_cluster(tmp_path):
    from abenlux.analytics.recovery import recover_orphans
    from abenlux.schema import DerivedRecord
    from abenlux.store import DerivedStore
    s = DerivedStore(tmp_path / "r.db")
    vec = [0.1, 0.2, 0.3, 0.4]
    for i in range(5):                       # 5 developers, same topic, all orphan, same repo
        s.insert(DerivedRecord(event_id=f"o{i}", ts=1.0, tier="t", provider="anthropic",
                               actor_pseudonym=f"a{i}", request_model="m", input_tokens=1000,
                               output_tokens=0, duplicate_history_tokens=0, cost_usd=1.0,
                               is_orphan=True, embedding=vec, repo="acme/checkout"))
    out = recover_orphans(s, k=5)
    assert out["proposals"] and out["proposals"][0]["developers"] == 5
    assert out["proposals"][0]["suggested_repo"] == "acme/checkout"
    assert recover_orphans(s, k=6)["proposals"] == []     # below the k threshold -> nothing surfaced
    s.close()


def test_shadow_yield_shows_what_enabling_a_strategy_would_save(tmp_path):
    import json as _json

    from abenlux.analytics.reports import management_report
    from abenlux.schema import DerivedRecord
    from abenlux.store import DerivedStore
    s = DerivedStore(tmp_path / "sh.db")
    for i in range(5):
        s.insert(DerivedRecord(event_id=f"e{i}", ts=1.0, tier="t", provider="anthropic",
                               actor_pseudonym=f"a{i}", request_model="claude-opus-4-8",
                               input_tokens=1000, output_tokens=100, duplicate_history_tokens=0,
                               cost_usd=2.0, objective_id="O", objective_label="O", is_orphan=False,
                               shadow_savings=_json.dumps({"command_trim": 400})))
    rep = management_report(s, k=5)
    sh = rep["compression"]["shadow"]
    assert sh["command_trim"]["tokens"] == 2000 and sh["command_trim"]["usd"] >= 0
    s.close()


def test_exchange_returns_only_a_percentile_after_enough_orgs(tmp_path):
    from abenlux.analytics.exchange import ExchangeStore, secure_aggregate
    ex = ExchangeStore(tmp_path / "ex.db")
    ex.submit("acme", {"cache_hit": 0.6, "reuse_share": 0.3})
    ex.submit("globex", {"cache_hit": 0.4, "reuse_share": 0.5})
    rows = ex.rows()
    assert secure_aggregate(rows, "acme", k_orgs=3)["ready"] is False    # only 2 orgs, below cohort min
    ex.submit("initech", {"cache_hit": 0.5, "reuse_share": 0.1})
    out = secure_aggregate(ex.rows(), "acme", k_orgs=3)
    assert out["ready"] is True
    cache = next(c for c in out["comparison"] if c["metric"] == "cache_hit")
    assert cache["cohort_orgs"] == 3 and 0.0 <= cache["your_percentile"] <= 1.0
    assert "value" not in cache and "acme" not in str(cache)             # never exposes a raw figure
    ex.close()


def test_value_only_counts_objectives_the_tenant_spent_on(tmp_path):
    # a merged change for one tenant's objective must never show in another tenant's value line
    from abenlux.analytics.outcomes import OutcomeStore
    from abenlux.analytics.reports import management_report
    from abenlux.schema import DerivedRecord
    from abenlux.store import DerivedStore
    s = DerivedStore(tmp_path / "c.db")
    for i in range(3):                       # acme-eu spent on obj-acme
        s.insert(DerivedRecord(event_id=f"a{i}", ts=1.0, tier="t", provider="anthropic", actor_pseudonym=f"a{i}",
                               request_model="m", input_tokens=1000, output_tokens=10, duplicate_history_tokens=0,
                               cost_usd=1.0, tenant_id="acme-eu", objective_id="obj-acme", objective_label="Acme"))
    for i in range(3):                       # acme-us spent on obj-zenith
        s.insert(DerivedRecord(event_id=f"b{i}", ts=1.0, tier="t", provider="anthropic", actor_pseudonym=f"b{i}",
                               request_model="m", input_tokens=1000, output_tokens=10, duplicate_history_tokens=0,
                               cost_usd=1.0, tenant_id="acme-us", objective_id="obj-zenith", objective_label="Zenith"))
    oc = OutcomeStore(tmp_path / "o.db")
    oc.record({"outcome_id": "x", "objective_id": "obj-acme", "merged": 1})   # no tenant tag
    by = oc.by_objective()
    repA = management_report(s, k=3, tenant="acme-eu", outcomes=by)
    repB = management_report(s, k=3, tenant="acme-us", outcomes=by)
    assert repA["value"]["merged"] == 1                      # acme-eu spent on obj-acme, so it counts
    assert (repB["value"] or {}).get("merged", 0) == 0       # acme-us did not, so it never leaks in
    oc.close()
    s.close()


def test_compression_block_is_suppressed_below_k(tmp_path):
    from abenlux.analytics.reports import management_report
    from abenlux.schema import DerivedRecord
    from abenlux.store import DerivedStore
    s = DerivedStore(tmp_path / "k.db")
    for i in range(2):                       # only 2 developers, below k=5
        s.insert(DerivedRecord(event_id=f"e{i}", ts=1.0, tier="t", provider="anthropic", actor_pseudonym=f"a{i}",
                               request_model="m", input_tokens=1000, output_tokens=10, duplicate_history_tokens=0,
                               cost_usd=1.0, saved_input_tokens=500))
    cz = management_report(s, k=5)["compression"]
    assert cz == {"suppressed": True}        # savings are an org figure, hidden below the k threshold
    s.close()


def test_negotiation_drops_single_developer_provider_and_model(tmp_path):
    from abenlux.analytics.negotiation import negotiation_pack
    from abenlux.schema import DerivedRecord
    from abenlux.store import DerivedStore
    s = DerivedStore(tmp_path / "ng.db")
    for i in range(5):                       # 5 developers on anthropic -> shown
        s.insert(DerivedRecord(event_id=f"a{i}", ts=1.0, tier="t", provider="anthropic", actor_pseudonym=f"a{i}",
                               request_model="claude-opus-4-8", input_tokens=100000, output_tokens=0,
                               duplicate_history_tokens=0, cost_usd=0.5))
    # one lone developer on google -> would leak their spend, must be dropped from the per-provider rows
    s.insert(DerivedRecord(event_id="g0", ts=1.0, tier="t", provider="google", actor_pseudonym="g0",
                           request_model="gemini-2.5-flash", input_tokens=999999, output_tokens=0,
                           duplicate_history_tokens=0, cost_usd=9.99))
    pack = negotiation_pack(s, k=5)
    provs = {p["provider"] for p in pack["by_provider"]}
    models = {m["model"] for m in pack["top_models"]}
    assert "anthropic" in provs and "google" not in provs      # the single-dev provider is suppressed
    assert "gemini-2.5-flash" not in models                    # and the single-dev model too
    s.close()


def test_exchange_percentile_is_coarse_and_never_an_exact_extreme():
    from abenlux.analytics.exchange import secure_aggregate
    rows = [{"org": o, "metric": "cache_hit", "value": v}
            for o, v in [("acme", 0.9), ("globex", 0.5), ("initech", 0.1)]]
    out = secure_aggregate(rows, "acme", k_orgs=3)         # acme is the clear best
    p = out["comparison"][0]["your_percentile"]
    assert 0.1 <= p <= 0.9 and p not in (0.0, 1.0)         # the exact extreme is pulled inward


def test_exchange_org_token_binding_parse():
    import os
    from abenlux.api.server import _exchange_org_tokens
    os.environ["ABEN_EXCHANGE_ORG_TOKENS"] = "acme:tokA, globex:tokB"
    try:
        m = _exchange_org_tokens()
        assert m == {"acme": "tokA", "globex": "tokB"}     # an org may only submit with its own token
    finally:
        del os.environ["ABEN_EXCHANGE_ORG_TOKENS"]


def test_orphan_cos_treats_dimension_mismatch_as_not_similar():
    from abenlux.analytics.recovery import _cos
    assert _cos([1.0, 0.0], [1.0, 0.0]) == 1.0          # same vector, fully similar
    assert _cos([1.0, 0.0, 0.0], [1.0, 0.0]) == 0.0     # different embedder dims are not comparable
