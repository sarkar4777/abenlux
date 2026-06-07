"""
Abenlux CLI.

  abenlux demo          run a sample exchange through the full edge pipeline (no setup)
  abenlux gateway       start the capture server (Tier-2 proxy + Tier-1 OTLP ingest)
  abenlux tiers         print the tool capability matrix
  abenlux onboard TOOL  print the exact setup for a tool on your OS/shell
  abenlux detect        show which AI tool the agent detects in this environment
  abenlux cost MODEL    price an interaction (input/output tokens -> USD)
  abenlux report        management spend->value report (k-anonymity gated)
  abenlux me            your OWN private view: spend + recent waste/collab nudges

The `demo` path uses only the standard library + the domain core, so it runs without any model
API, network, or heavyweight ML deps. It proves the pipeline is correct end to end.
"""
from __future__ import annotations

import argparse
import json

from abenlux.analytics.reports import developer_report, management_report
from abenlux.attribution.attributor import KnowledgeGraph, Objective
from abenlux.capture.adapters import build_event
from abenlux.capture.context import current_actor
from abenlux.capture.tiers import canonical_tools
from abenlux.pipeline import process
from abenlux.pricing import cost_usd, price_for
from abenlux.privacy.pseudonymize import pseudonymize
from abenlux.processing.waste import SessionWasteMonitor
from abenlux.schema import Provider, WorkContext
from abenlux.settings import SETTINGS
from abenlux.store import open_store


def _demo_kg() -> KnowledgeGraph:
    kg = KnowledgeGraph()
    kg.add_objective(Objective("obj-acme", "Acme - Checkout Platform", "client", client="acme"))
    kg.add_objective(Objective("obj-globex", "Globex - Agent Marketplace R&D", "innovation"))
    kg.map_ticket_prefix("ACME", "obj-acme")
    kg.map_repo("globex-runtime", "obj-globex")
    return kg


_SAMPLE_ANTHROPIC_STREAM = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"model":"claude-opus-4-8",'
    '"usage":{"input_tokens":1820,"output_tokens":1}}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","delta":{"type":"text_delta",'
    '"text":"Use a Temporal saga with compensation steps "}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","delta":{"type":"text_delta",'
    '"text":"for the approval workflow."}}\n\n'
    'event: message_delta\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    '"usage":{"output_tokens":42}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def cmd_demo(_args) -> None:
    kg = _demo_kg()
    monitor = SessionWasteMonitor()

    req = {
        "model": "claude-opus-4-8",
        "system": "You are a senior engineer. My key is sk-ant-abc123SECRETkey4567890 do not echo it.",
        "messages": [{"role": "user", "content": "How should I orchestrate the ACME approval workflow? Reach me at dev@example.com"}],
        "stream": True,
    }
    event = build_event(
        provider=Provider.ANTHROPIC, request_body=req,
        response_raw=_SAMPLE_ANTHROPIC_STREAM, streamed=True,
    )
    event.work = WorkContext(tool="claude-code", git_branch="feature/ACME-1488-approvals",
                             ticket_id="ACME-1488", repo="acme-checkout")
    event.actor_raw = "alice@example.com"

    print("== BEFORE pipeline (full content, in-flight only) ==")
    print(" input tokens :", event.usage.input_tokens, " output tokens:", event.usage.output_tokens)
    print(" reassembled  :", repr(event.output_text()))
    print(" actor_raw    :", event.actor_raw)
    print(" branch/ticket:", event.work.git_branch, "->", event.work.ticket_id)

    result = process(event, kg=kg, hmac_key=b"demo-secret", waste_monitor=monitor)

    print("\n== AFTER pipeline ==")
    print(" redactions   :", result.redactions, "(secret + PII destroyed before persistence)")
    print(" actor_raw    :", event.actor_raw, "(dropped)")
    print(" pseudonym    :", result.record.actor_pseudonym)
    print(" cost (USD)   :", result.record.cost_usd, "(priced:", result.record.cost_priced, ")")
    print(" objective    :", result.record.objective_label, "via", result.record.attribution_method)
    print(" content left :", repr(event.output_text()), "(discarded)")
    print("\n== DERIVED RECORD (the only thing that crosses into analytics) ==")
    d = result.record.to_dict()
    d["embedding"] = f"<{len(d['embedding'])}-dim vector>" if d["embedding"] else None
    print(json.dumps(d, indent=2, default=str))


def cmd_gateway(args) -> None:
    import uvicorn
    uvicorn.run("abenlux.capture.gateway:app", host="127.0.0.1", port=args.port, reload=False)


def cmd_serve(args) -> None:
    # the management + developer API and dashboard. bind localhost by default, put a real
    # reverse proxy + TLS + SSO in front for an org deployment.
    import uvicorn
    uvicorn.run("abenlux.api.server:app", host=args.host, port=args.port, reload=False)


def cmd_mock(args) -> None:
    # a protocol-correct fake model upstream so any tool can be verified without spending tokens.
    import uvicorn
    uvicorn.run("abenlux.devtools.mock_upstream:app", host="127.0.0.1", port=args.port, reload=False)


def cmd_sync_cursor(args) -> None:
    # Tier-3: pull Cursor usage events (metadata only) into the derived store. requires an
    # admin api key, the fetch is a thin authenticated GET so this stays honest and testable.
    import os

    import httpx

    from abenlux.attribution.attributor import KnowledgeGraph
    from abenlux.capture.vendor_admin import sync_cursor_usage

    key = os.getenv("ABEN_CURSOR_API_KEY")
    if not key:
        print("set ABEN_CURSOR_API_KEY (Cursor admin api key) first, see `abenlux onboard cursor-agent`")
        return
    kg = KnowledgeGraph.from_yaml(SETTINGS.kg_path) if SETTINGS.kg_path else KnowledgeGraph()
    store = open_store(SETTINGS.db_path)

    def fetch() -> list:
        url = os.getenv("ABEN_CURSOR_API", "https://api.cursor.com/analytics/usage-events")
        r = httpx.get(url, params={"period": args.period}, auth=(key, ""), timeout=30.0)
        r.raise_for_status()
        data = r.json()
        return data.get("usageEvents", data) if isinstance(data, dict) else data

    n = sync_cursor_usage(fetch, hmac_key=SETTINGS.hmac_bytes, kg=kg, insert=store.insert)
    store.close()
    print(f"ingested {n} Cursor usage events (metadata only, tier3, no content)")


def cmd_tiers(_args) -> None:
    print(f"{'tool':<16}{'tier':<22}{'full_prompt':<13}{'exact_tokens':<14}ingest")
    print("-" * 96)
    for t in canonical_tools():
        print(f"{t.tool:<16}{t.tier.value:<22}{str(t.captures_full_prompt):<13}"
              f"{str(t.exact_tokens):<14}{t.ingest_path}")


def cmd_onboard(args) -> None:
    from abenlux.onboard import default_shell, render
    if not args.tool:
        print("usage: abenlux onboard <tool> [--shell powershell|cmd|bash] [--base URL]")
        print("tools: " + ", ".join(t.tool for t in canonical_tools()))
        return
    print(render(args.tool, shell=args.shell or default_shell(), base=args.base))


def cmd_detect(_args) -> None:
    from abenlux.agent.detect import detect
    d = detect()
    print(json.dumps({"tool": d.tool, "app_category": d.app_category, "source": d.source}, indent=2))


def cmd_cost(args) -> None:
    p = price_for(args.model)
    if p is None:
        print(f"{args.model}: UNPRICED (not in table) - cost would be flagged, never guessed")
        return
    cb = cost_usd(args.model, args.input, args.output, cache_read_tokens=args.cache_read)
    print(json.dumps({
        "model": args.model, "matched_price_key": cb.matched_key,
        "rate_per_mtok": {"input": p.input, "output": p.output, "cache_read": p.cache_read},
        "input_tokens": args.input, "output_tokens": args.output, "cache_read_tokens": args.cache_read,
        "cost_usd": {"input": cb.input_cost, "output": cb.output_cost, "cache": cb.cache_cost, "total": cb.total},
    }, indent=2))


def cmd_report(args) -> None:
    kg = KnowledgeGraph.from_yaml(SETTINGS.kg_path) if SETTINGS.kg_path else KnowledgeGraph()
    store = open_store(SETTINGS.db_path)
    rep = management_report(store, k=SETTINGS.k_anon, dp_epsilon=SETTINGS.dp_epsilon, kg=kg)
    store.close()
    if args.json:
        print(json.dumps(rep, indent=2))
        return
    print("== Abenlux management report (k-anonymity gated) ==")
    print(f" actors:{rep['org_actors']}  events:{rep['total_events']}  "
          f"tokens:{rep['total_tokens']:,}  cost:${rep['total_cost_usd']:,.2f}")
    print(f" orphan token share : {rep['orphan_token_share']*100:.1f}%  "
          f"(unattributed AI spend - the headline waste metric)")
    band = rep["recoverable_resent_history_usd"]
    print(f" recoverable resent-history : ${band['floor']:,.2f}–${band['ceiling']:,.2f}")
    if rep["unpriced_events"]:
        print(f" unpriced events : {rep['unpriced_events']} (model not in price table)")
    print("\n spend by objective:")
    for r in rep["by_objective"]:
        if r["suppressed"]:
            print(f"   {r['label']:<34} [suppressed: <{rep['privacy']['k']} developers]")
        else:
            print(f"   {r['label']:<34} ${r['cost']:>10,.2f}  ({r['actors']} devs)")
    print("\n spend by tool:")
    for r in rep["by_tool"]:
        if not r["suppressed"]:
            print(f"   {str(r['label']):<20} ${r['cost']:>10,.2f}  ({r['actors']} devs)")
    if rep.get("budgets"):
        print("\n budgets (spend vs ceiling, run-rate forecast):")
        for b in rep["budgets"]:
            flag = {"over": "OVER", "at_risk": "AT-RISK", "ok": "ok"}[b["status"]]
            print(f"   {b['label']:<34} ${b['spent_usd']:>9,.2f}/${b['budget_usd']:<9,.0f} "
                  f"{b['pct']*100:4.0f}%  forecast ${b['forecast_usd']:>9,.2f}  [{flag}]")


def cmd_me(args) -> None:
    from abenlux.developer.feed import LocalSignalFeed
    actor = SETTINGS.actor or current_actor()
    pseudo = pseudonymize(actor, SETTINGS.hmac_bytes)
    store = open_store(SETTINGS.db_path)
    rep = developer_report(store, pseudo)
    store.close()
    print(f"== your private view ({actor}) ==")
    print(f" calls:{rep['calls']}  tokens:{rep['tokens']:,}  cost:${rep['cost_usd']:,.4f}")
    print(f" retry loops:{rep['retry_loops']}  resent-history tokens:{rep['resent_history_tokens']:,}")
    print(" (private to you, never visible to management)")
    print("\n recent nudges (this device only):")
    for e in LocalSignalFeed().recent(args.n):
        tag = e.get("tool") or "?"
        usd = e.get("recoverable_usd", 0.0)
        extra = f"  ~${usd:.4f}" if usd else ""
        print(f"   [{e['kind']:<16}] ({tag}) {e['line']}{extra}")


def cmd_watch(args) -> None:
    # live ambient tail of your private signal feed. keep it open in a spare terminal pane and
    # nudges appear as you work, no browser. desktop toasts fire automatically from the gateway.
    import time

    from abenlux.developer.feed import LocalSignalFeed
    feed = LocalSignalFeed()
    icon = {"retry_loop": "[retry]", "context_bloat": "[bloat]", "answered_already": "[asked]",
            "routing_hint": "[route]", "budget_guardrail": "[budget]",
            "collab_live_duplication": "[collab]", "collab_solved_reuse": "[reuse]"}
    existing = feed.recent(500)
    last_ts = max((e["ts"] for e in existing), default=0.0)
    if args.all:
        for e in existing:
            print(f"  {icon.get(e['kind'], '[' + e['kind'] + ']'):<9} ({e.get('tool') or '?'}) {e['line']}")
    print(f"abenlux watch  {feed.path}")
    print("private to you, never sent to management. Ctrl-C to stop.\n")
    try:
        while True:
            for e in feed.recent(200):
                if e["ts"] > last_ts:
                    last_ts = e["ts"]
                    usd = e.get("recoverable_usd", 0.0)
                    extra = f"  ~${usd:.4f} recoverable" if usd else ""
                    print(f"  {icon.get(e['kind'], '[' + e['kind'] + ']'):<9} ({e.get('tool') or '?'}) {e['line']}{extra}")
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopped.")


def main() -> None:
    p = argparse.ArgumentParser(prog="abenlux")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("demo").set_defaults(func=cmd_demo)

    g = sub.add_parser("gateway")
    g.add_argument("--port", type=int, default=8088)
    g.set_defaults(func=cmd_gateway)

    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8090)
    sv.set_defaults(func=cmd_serve)

    sc = sub.add_parser("sync-cursor")
    sc.add_argument("--period", default="30d")
    sc.set_defaults(func=cmd_sync_cursor)

    mk = sub.add_parser("mock")
    mk.add_argument("--port", type=int, default=9111)
    mk.set_defaults(func=cmd_mock)

    sub.add_parser("tiers").set_defaults(func=cmd_tiers)

    o = sub.add_parser("onboard")
    o.add_argument("tool", nargs="?")
    o.add_argument("--shell", choices=["powershell", "cmd", "bash"])
    o.add_argument("--base", default="http://127.0.0.1:8088")
    o.set_defaults(func=cmd_onboard)

    sub.add_parser("detect").set_defaults(func=cmd_detect)

    c = sub.add_parser("cost")
    c.add_argument("model")
    c.add_argument("--input", type=int, default=0)
    c.add_argument("--output", type=int, default=0)
    c.add_argument("--cache-read", type=int, default=0, dest="cache_read")
    c.set_defaults(func=cmd_cost)

    r = sub.add_parser("report")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_report)

    m = sub.add_parser("me")
    m.add_argument("-n", type=int, default=20)
    m.set_defaults(func=cmd_me)

    w = sub.add_parser("watch")
    w.add_argument("--all", action="store_true", help="print existing history before tailing")
    w.set_defaults(func=cmd_watch)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
