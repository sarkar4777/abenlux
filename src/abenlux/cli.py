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


def _fmt_contact(card) -> str:
    if not card:
        return "a colleague (hidden until you both request an intro)"
    bits = [card.get("name", "a colleague")]
    for k in ("slack", "teams", "email", "github"):
        if card.get(k):
            bits.append(f"{k}: {card[k]}")
    return "   ".join(bits)


def _print_matches(matches) -> None:
    if not matches:
        print(" No collaboration matches yet. When a colleague works on a similar problem, it shows here.")
        return
    print(f" You have {len(matches)} collaboration match(es):\n")
    for m in matches:
        who = _fmt_contact(m.get("peer_contact")) if m.get("peer_contact") else \
            (m.get("peer_revealed") or "a colleague (hidden until you both request an intro)")
        pending = " - you have requested an intro, waiting on them" if m.get("you_requested") and not m.get("peer_contact") else ""
        print(f"  [{m['id']}] {m['mode'].replace('_', ' ')}: '{m['topic']}'  (similarity {m['similarity']})")
        print(f"        with {who}{pending}")
    print("\n Request a double-blind intro, right here in your terminal:")
    print("   abenlux collab intro <id>      (or just `abenlux collab intro` if you have one match)")


def _collab_matches_remote(b, h):
    import httpx
    return httpx.get(f"{b}/api/me", headers=h, timeout=15).json().get("collaboration_matches", [])


def cmd_collab(args) -> None:
    # see and act on collaboration matches from the terminal, no browser. forward mode talks to the
    # collector with your token, solo mode reads your local match store.
    import os

    action = getattr(args, "action", "list") or "list"
    base, token = SETTINGS.collector_url, os.getenv("ABEN_TOKEN")
    remote = bool(base and token)

    if remote:
        import httpx
        h, b = {"Authorization": f"Bearer {token}"}, base.rstrip("/")
        matches = _collab_matches_remote(b, h)
        if action == "list":
            _print_matches(matches)
            return
        mid = _resolve_match_id(args.id, matches)
        if mid is None:
            return
        resp = httpx.post(f"{b}/api/collab/{mid}/consent", headers=h, timeout=15)
        if resp.status_code != 200:
            detail = resp.json().get("detail", resp.status_code) if resp.content else resp.status_code
            print(f" Could not request intro for match {mid}: {detail}. Run `abenlux collab` to see your matches.")
            return
        r = resp.json()
        _report_intro(r.get("mutual"), r.get("peer_contact"))
        return

    # solo / local match store
    from abenlux.developer.matches import MatchStore
    from abenlux.privacy.pseudonymize import pseudonymize
    pseudo = pseudonymize(SETTINGS.actor or current_actor(), SETTINGS.hmac_bytes)
    ms = MatchStore(os.getenv("ABEN_MATCH_DB", "abenlux-matches.db"))
    rows = [{"id": m["id"], "peer": m["peer"], "topic": m["topic"], "similarity": m["similarity"],
             "mode": m["mode"], "peer_revealed": None} for m in ms.for_owner(pseudo)]
    if action == "list":
        ms.close()
        _print_matches(rows)
        return
    mid = _resolve_match_id(args.id, rows)
    if mid is not None:
        peer = next(m["peer"] for m in rows if m["id"] == mid)
        ms.record_consent(pseudo, peer)
        _report_intro(ms.mutually_consented(pseudo, peer), None)
    ms.close()


def _resolve_match_id(given, matches):
    # intuitive: use the given id, or if you have exactly one match use it automatically
    if given is not None:
        if any(m["id"] == given for m in matches):
            return given
        print(f" No match [{given}] for you. Run `abenlux collab` to see your match ids.")
        return None
    if len(matches) == 1:
        return matches[0]["id"]
    if not matches:
        print(" You have no collaboration matches yet.")
    else:
        print(" You have several matches - pick one: abenlux collab intro <id>")
        _print_matches(matches)
    return None


def _report_intro(mutual, card):
    if mutual:
        print(" Intro made - you have both opted in. Reach out via:")
        print("   " + _fmt_contact(card))
    else:
        print(" Intro requested. The other developer will see a request too, and you are revealed to each")
        print(" other (with the contact handles you each chose to share) only when they also accept.")


def cmd_contact(args) -> None:
    # your shareable contact card. set the handles you are willing to share (slack/teams/email/github),
    # revealed to a peer ONLY after a mutual double-blind intro.
    import os

    fields = {k: getattr(args, k) for k in ("name", "email", "slack", "teams", "github", "note")
              if getattr(args, k)}
    setting = args.action == "set" or bool(fields)
    base, token = SETTINGS.collector_url, os.getenv("ABEN_TOKEN")
    if base and token:
        import httpx
        h, b = {"Authorization": f"Bearer {token}"}, base.rstrip("/")
        if setting:
            card = httpx.post(f"{b}/api/contact", json=fields, headers=h, timeout=15).json().get("contact", {})
        else:
            card = httpx.get(f"{b}/api/contact", headers=h, timeout=15).json().get("contact", {})
    else:
        from abenlux.developer.contacts import ContactStore
        from abenlux.privacy.pseudonymize import pseudonymize
        pseudo = pseudonymize(SETTINGS.actor or current_actor(), SETTINGS.hmac_bytes)
        cs = ContactStore(os.getenv("ABEN_CONTACT_DB", "abenlux-contacts.db"))
        card = cs.set(pseudo, fields) if setting else (cs.get(pseudo) or {})
        cs.close()
    if card:
        print(" Your contact card (shared only after a mutual double-blind intro):")
        for k, v in card.items():
            print(f"   {k:<7}: {v}")
    else:
        print(" No contact card yet. Add the handles colleagues can reach you on after a mutual intro.")
    print("\n set/update:  abenlux contact set --slack @you --email you@corp --teams 'Your Name'")


def cmd_graph(args) -> None:
    # the developer's own on-device knowledge graph: objectives, tickets, purpose, learned vocabulary
    from abenlux.developer.knowledge_graph import DevKnowledgeGraph
    from abenlux.worktype_learn import WorkTypeLearner
    store = open_store(SETTINGS.local_db)
    g = DevKnowledgeGraph(store, WorkTypeLearner())
    print(g.to_json() if args.json else g.render_text())
    store.close()


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


_OVERVIEW = """abenlux - AI spend -> value attribution plane

YOUR STUFF (private to you, never seen by management)
  abenlux me                 your spend + recent waste/collaboration nudges
  abenlux watch              live tail of your private signals (keep in a spare pane)
  abenlux graph              your on-device knowledge graph (objectives, tickets, purpose)
  abenlux collab             see collaboration matches; `collab intro <id>` to request an intro
  abenlux contact            your shareable contact card (revealed only on a mutual intro)

SET UP CAPTURE
  abenlux gateway            run the on-device capture agent (loopback proxy + OTLP ingest)
  abenlux onboard <tool>     print the exact setup for your tool and shell
  abenlux tiers              the tool capability matrix
  abenlux detect             which AI tool is detected here
  abenlux mock               a fake upstream to verify capture without spending tokens

MANAGEMENT / IT
  abenlux serve              the collector + dashboard (k-anonymized, RBAC)
  abenlux report             spend -> value report (k-anonymity gated)
  abenlux sync-cursor        pull Tier-3 Cursor usage (metadata only)

UTIL
  abenlux demo               run the full edge pipeline once, offline
  abenlux cost <model>       price an interaction

Run `abenlux <command> -h` for details on any command."""


def cmd_help(_args) -> None:
    print(_OVERVIEW)


def main() -> None:
    p = argparse.ArgumentParser(prog="abenlux", description="AI spend -> value attribution plane.",
                                add_help=True)
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("demo", help="run the full edge pipeline once, offline").set_defaults(func=cmd_demo)

    g = sub.add_parser("gateway", help="run the on-device capture agent")
    g.add_argument("--port", type=int, default=8088)
    g.set_defaults(func=cmd_gateway)

    sv = sub.add_parser("serve", help="run the management collector + dashboard")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8090)
    sv.set_defaults(func=cmd_serve)

    sc = sub.add_parser("sync-cursor", help="pull Tier-3 Cursor usage (metadata only)")
    sc.add_argument("--period", default="30d")
    sc.set_defaults(func=cmd_sync_cursor)

    mk = sub.add_parser("mock", help="fake upstream to verify capture without spending tokens")
    mk.add_argument("--port", type=int, default=9111)
    mk.set_defaults(func=cmd_mock)

    sub.add_parser("tiers", help="the tool capability matrix").set_defaults(func=cmd_tiers)

    o = sub.add_parser("onboard", help="print the exact setup for a tool on your OS/shell")
    o.add_argument("tool", nargs="?", help="tool name, e.g. claude-code, aider, cline")
    o.add_argument("--shell", choices=["powershell", "cmd", "bash"])
    o.add_argument("--base", default="http://127.0.0.1:8088")
    o.set_defaults(func=cmd_onboard)

    sub.add_parser("detect", help="which AI tool is detected here").set_defaults(func=cmd_detect)

    c = sub.add_parser("cost", help="price an interaction")
    c.add_argument("model")
    c.add_argument("--input", type=int, default=0)
    c.add_argument("--output", type=int, default=0)
    c.add_argument("--cache-read", type=int, default=0, dest="cache_read")
    c.set_defaults(func=cmd_cost)

    r = sub.add_parser("report", help="management spend -> value report (k-anonymity gated)")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_report)

    m = sub.add_parser("me", help="your own private spend + recent nudges")
    m.add_argument("-n", type=int, default=20, help="how many recent nudges to show")
    m.set_defaults(func=cmd_me)

    w = sub.add_parser("watch", help="live tail of your private signals")
    w.add_argument("--all", action="store_true", help="print existing history before tailing")
    w.set_defaults(func=cmd_watch)

    gr = sub.add_parser("graph", help="your developer-local knowledge graph")
    gr.add_argument("--json", action="store_true")
    gr.set_defaults(func=cmd_graph)

    cl = sub.add_parser("collab", help="see and act on collaboration matches (no browser)")
    cl.add_argument("action", nargs="?", choices=["list", "intro"], default="list",
                    help="`list` (default) or `intro` to request a double-blind intro")
    cl.add_argument("id", nargs="?", type=int, help="match id (optional if you have one match)")
    cl.set_defaults(func=cmd_collab)

    ct = sub.add_parser("contact", help="your shareable contact card (revealed only on mutual intro)")
    ct.add_argument("action", nargs="?", choices=["show", "set"], default="show")
    for f in ("name", "email", "slack", "teams", "github", "note"):
        ct.add_argument(f"--{f}")
    ct.set_defaults(func=cmd_contact)

    sub.add_parser("help", help="show the command overview").set_defaults(func=cmd_help)

    args = p.parse_args()
    if not getattr(args, "func", None):  # bare `abenlux` -> friendly overview, not an error
        cmd_help(args)
        return
    args.func(args)


if __name__ == "__main__":
    main()
