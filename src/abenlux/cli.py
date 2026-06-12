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


def cmd_agent(args) -> None:
    # the background capture agent: install it once and it starts at login on Linux/macOS/Windows,
    # runs in YOUR session (so toasts render), and nudges you about waste, budgets, and collaboration.
    from abenlux.agent import service
    action = getattr(args, "action", "status") or "status"
    if action == "run":
        # load the snapshotted config into the environment, then SUPERVISE the gateway in a CHILD
        # process. Settings reads the environment once at import, so the gateway must be (re-)imported in
        # a fresh process for agent.env to take effect. the supervisor restarts the child on a crash with
        # capped backoff and logs each exit - this gives Windows the restart-on-failure its Startup-folder
        # launcher lacks (systemd/launchd already restart on Linux/macOS; the loop is harmless there).
        import os
        import subprocess
        import sys
        import time
        n = service.load_env_file()
        print(f"abenlux agent: loaded {n} config vars from {service.ENV_FILE}, supervising capture on :{args.port}")
        backoff = 2.0
        while True:
            started = time.monotonic()
            try:
                rc = subprocess.run(
                    [sys.executable, "-m", "abenlux.cli", "gateway", "--port", str(args.port)],
                    env=os.environ.copy()).returncode
            except KeyboardInterrupt:
                raise SystemExit(0)              # clean stop, do not restart
            if rc == 0:
                raise SystemExit(0)              # gateway was asked to stop
            service.log_agent_crash(rc, args.port)
            ran = time.monotonic() - started
            backoff = 2.0 if ran > 60 else min(backoff * 2, 60.0)  # reset if it ran a while, else back off
            print(f"abenlux agent: capture exited ({rc}); restarting in {int(backoff)}s "
                  f"(see {service.AGENT_LOG})", file=sys.stderr)
            time.sleep(backoff)
    elif action == "install":
        print(service.install(args.port))
        print(" config snapshot:", service.ENV_FILE, "(edit it and re-run `abenlux agent install` to update)")
    elif action == "uninstall":
        print(service.uninstall())
    else:
        print(service.status())
        print(" run state config:", service.ENV_FILE if service.ENV_FILE.exists() else "(none - using current env)")


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
    tenant = getattr(args, "tenant", None) or SETTINGS.tenant_id
    store = open_store(SETTINGS.db_path)
    import os as _os

    from abenlux.analytics.outcomes import OutcomeStore
    _oc = OutcomeStore(_os.getenv("ABEN_OUTCOME_DB", "abenlux-outcomes.db"))
    _by_obj = _oc.by_objective()
    _oc.close()
    rep = management_report(store, k=SETTINGS.k_anon, dp_epsilon=SETTINGS.dp_epsilon, kg=kg,
                            tenant=tenant, outcomes=_by_obj)
    from abenlux.ledger import open_ledger
    ledger = open_ledger(_os.getenv("ABEN_LEDGER_DB", "abenlux-ledger.db"))
    rep["reuse_yield"] = ledger.summary(store, tenant, k=SETTINGS.k_anon)
    ledger.close()
    store.close()
    if args.json:
        print(json.dumps(rep, indent=2))
        return
    # a brand-new or sub-k tenant has its org-wide scalars suppressed to None. print a clear line
    # instead of crashing on the number formatting.
    def _n(v, fmt="{:,}"):
        return fmt.format(v) if isinstance(v, (int, float)) else "suppressed (below k)"
    print(f"== Abenlux management report (k-anonymity gated)  tenant:{tenant} ==")
    print(f" actors:{rep.get('org_actors', 0)}  events:{_n(rep.get('total_events'))}  "
          f"tokens:{_n(rep.get('total_tokens'))}  cost:{_n(rep.get('total_cost_usd'), '${:,.2f}')}")
    if not isinstance(rep.get("total_cost_usd"), (int, float)):
        print(" (this tenant has fewer than the k-anonymity threshold of developers, so its totals are hidden)")
        return
    val = rep.get("value")
    if val and val.get("merged"):
        cpm = val.get("cost_per_merged_change")
        print(f" value : {val['merged']} merged changes  "
              f"{f'${cpm:,.2f} per merged change  ' if cpm is not None else ''}"
              f"{(val.get('merge_rate') or 0)*100:.0f}% merge rate  "
              f"{(val.get('revert_rate') or 0)*100:.0f}% reverted  (spend joined to shipped work)")
    print(f" orphan token share : {rep['orphan_token_share']*100:.1f}%  "
          f"(unattributed AI spend - the headline waste metric)")
    print(f" prompt-cache hit ratio : {rep['cache_hit_ratio']*100:.1f}%  "
          f"(resent input served from cache - higher is cheaper)")
    band = rep["recoverable_resent_history_usd"]
    print(f" recoverable via caching : ${band['floor']:,.2f}-${band['ceiling']:,.2f}  "
          f"(uncached resends, same context - zero detail loss)")
    ry = rep.get("reuse_yield") or {}
    if ry.get("reuse_avoided_usd"):
        print(f" reuse-yield (avoided re-solves) : ~${ry['reuse_avoided_usd']:,.2f}  "
              f"({ry['events_credited']} reuses, k-gated - a SAVING, shown beside spend)")
    cz = rep.get("compression") or {}
    if cz.get("saved_input_tokens") or cz.get("cache_hits"):
        print(f" compression yield : {cz.get('saved_input_tokens', 0):,} tokens saved (~${cz.get('saved_usd', 0):,.2f})  "
              f"{cz.get('cache_hits', 0)} calls served from cache  (edge compression layer)")
        for name, d in (cz.get("by_strategy") or {}).items():
            print(f"   - {name:18} {d['tokens']:>10,} tokens  (~${d['usd']:,.2f})")
    for name, d in (cz.get("shadow") or {}).items():
        if d.get("usd", 0) >= 0.01:
            print(f" would save : enabling {name} would save ~{d['tokens']:,} tokens (~${d['usd']:,.2f})")
    if rep["unpriced_events"]:
        print(f" unpriced events : {rep['unpriced_events']} (model not in price table)")
    trend = rep.get("trend")
    if trend and (trend["orphan_share"]["alert"] or trend["cost"]["alert"]):
        os_ = trend["orphan_share"]
        print(f" DRIFT ALERT : unattributed spend {os_['direction']} "
              f"{os_['prior']*100:.0f}% -> {os_['recent']*100:.0f}% window-over-window")
    inv = rep.get("investment")
    if inv:
        tot = sum(inv.values()) or 1
        print(f"\n what the spend is for : net-new ${inv['net_new']:,.2f} ({inv['net_new']/tot*100:.0f}%)  "
              f"maintenance ${inv['maintenance']:,.2f} ({inv['maintenance']/tot*100:.0f}%)  "
              f"unclassified ${inv['unclassified']:,.2f}")
    new = rep.get("new_initiatives") or []
    if new:
        print(" new this period:")
        for n in new:
            cost = "spend hidden (<k devs)" if n["cost"] is None else f"${n['cost']:,.2f}"
            print(f"   {n['label']:<34} {n.get('work_type') or 'work':<11} {cost}")
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
            if b.get("suppressed"):
                print(f"   {b['label']:<34} [spend hidden: <{rep['privacy']['k']} developers]")
                continue
            flag = {"over": "OVER", "at_risk": "AT-RISK", "ok": "ok"}[b["status"]]
            print(f"   {b['label']:<34} ${b['spent_usd']:>9,.2f}/${b['budget_usd']:<9,.0f} "
                  f"{b['pct']*100:4.0f}%  forecast ${b['forecast_usd']:>9,.2f}  [{flag}]")


def _local_midnight_ts() -> float:
    import time as _t
    lt = _t.localtime()
    return _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def cmd_me(args) -> None:
    import time as _t

    from abenlux.developer.feed import LocalSignalFeed
    actor = SETTINGS.actor or current_actor()
    pseudo = pseudonymize(actor, SETTINGS.hmac_bytes)
    store = open_store(SETTINGS.db_path)
    rep = developer_report(store, pseudo)
    # a 'today' burn-rate: spend since local midnight + a run-rate projection to end of day, so a
    # developer can course-correct against a daily pace, not just see an all-time total. private, local.
    if getattr(args, "today", False):
        midnight = _local_midnight_ts()
        s = store.actor_summary(pseudo, start_ts=midnight)
        elapsed = max((_t.time() - midnight) / 86400.0, 0.04)   # fraction of the day, floored like budgets
        pace = s["cost"] / elapsed
        print(f"== today ({actor}) ==")
        print(f" today: calls:{s['calls']}  tokens:{s['tokens']:,}  spent:${s['cost']:,.4f}")
        print(f" at this pace ~${pace:,.4f} by end of day  (retry loops today: {s['retries']})")
        print(" (private to you, never visible to management)")
        store.close()
        return
    store.close()
    print(f"== your private view ({actor}) ==")
    print(f" calls:{rep['calls']}  tokens:{rep['tokens']:,}  cost:${rep['cost_usd']:,.4f}")
    print(f" retry loops:{rep['retry_loops']}  resent-history tokens:{rep['resent_history_tokens']:,}")
    print(f" cache hit ratio:{rep['cache_hit_ratio']*100:.0f}%  "
          f"uncached resent:{rep['uncached_resent_tokens']:,} tokens (cache these, same context)")
    mix = rep.get("work_type_mix") or []
    if mix:
        print(" your work mix: " + "  ".join(f"{m['label']} ${m['cost']:,.2f}" for m in mix))
    print(" (private to you, never visible to management)")
    print("\n recent nudges (this device only):")
    for e in LocalSignalFeed().recent(args.n):
        tag = e.get("tool") or "?"
        usd = e.get("recoverable_usd", 0.0)
        extra = f"  ~${usd:.4f}" if usd else ""
        print(f"   [{e['kind']:<16}] ({tag}) {e['line']}{extra}")


def cmd_calls(args) -> None:
    # per-call drill-down of YOUR OWN recent calls, private to you (scoped to your pseudonym). lets a
    # developer who sees a spend spike ask "what were my last N calls, by cost?" - all on-device.
    actor = SETTINGS.actor or current_actor()
    pseudo = pseudonymize(actor, SETTINGS.hmac_bytes)
    since = _local_midnight_ts() if getattr(args, "today", False) else None
    order = "cost" if getattr(args, "top_cost", False) else "ts"
    store = open_store(SETTINGS.db_path)
    rows = store.recent_records(pseudo, args.n, since_ts=since, objective=args.objective, order=order)
    store.close()
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("no calls found for this view.")
        return
    print(f"== your recent calls ({actor}) {'ordered by cost' if order == 'cost' else 'most recent first'} ==")
    print(f" {'model':<22}{'in':>7}{'out':>7}{'cache':>8}{'cost':>11}  what")
    for r in rows:
        flag = " retry" if r.get("is_retry_loop") else ""
        priced = "" if r.get("cost_priced", 1) else " (unpriced)"
        what = f"{r['objective']} / {r['work_type']}" + (f" [{r['ticket_id']}]" if r.get("ticket_id") else "")
        print(f" {str(r['request_model'])[:22]:<22}{r['input_tokens']:>7}{r['output_tokens']:>7}"
              f"{r['cache_read_tokens']:>8}{('$' + format(r['cost_usd'], '.4f')):>11}{priced}  {what}{flag}")
    print(" (private to you, never visible to management)")


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
    ms = MatchStore(os.getenv("ABEN_MATCH_DB"))   # private ~/.abenlux by default
    rows = [{"id": m["id"], "peer": m["peer"], "topic": m["topic"], "similarity": m["similarity"],
             "mode": m["mode"], "peer_revealed": None} for m in ms.for_owner(pseudo)]
    if action == "list":
        ms.close()
        _print_matches(rows)
        return
    mid = _resolve_match_id(args.id, rows)
    if mid is not None:
        row = next(m for m in rows if m["id"] == mid)
        peer, topic = row["peer"], row["topic"]
        ms.record_consent(pseudo, peer, topic)               # consent is scoped to this topic
        _report_intro(ms.mutually_consented(pseudo, peer, topic), None)
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
        cs = ContactStore(os.getenv("ABEN_CONTACT_DB"))   # private ~/.abenlux by default
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


def cmd_tenant(args) -> None:
    # tenants are org units / geographies of one org, the unit the benchmark compares. content-free.
    import os as _os
    import time as _time

    from abenlux.tenants import Tenant, open_tenant_store
    store = open_tenant_store(_os.getenv("ABEN_TENANT_DB", "abenlux-tenants.db"))
    if args.tenant_cmd == "create":
        t = store.upsert(Tenant(
            tenant_id=args.tenant_id, org=args.org,
            display_name=args.name or args.tenant_id,
            residency=args.residency or SETTINGS.residency, created_ts=_time.time(),
        ))
        store.close()
        print(f"created tenant {t.tenant_id}  org:{t.org}  region:{t.residency}")
        print(f"point that region's edges at it:  ABEN_TENANT={t.tenant_id} abenlux gateway")
        return
    rows = store.list(org=args.org)
    store.close()
    if args.json:
        print(json.dumps([r.to_dict() for r in rows], indent=2))
        return
    if not rows:
        print("no tenants yet. create one:  abenlux tenant create <id> --org <org>")
        return
    print(f"{'tenant_id':<20} {'org':<14} {'region':<8} display")
    for r in rows:
        print(f"{r.tenant_id:<20} {r.org:<14} {r.residency:<8} {r.display_name}")


def cmd_benchmark(args) -> None:
    # cross-tenant comparison within one org: ratios only, k-anon per tenant, DP-noised, cohort-gated.
    import os as _os

    from abenlux.analytics.benchmark import benchmark as build_benchmark
    from abenlux.ledger import open_ledger
    from abenlux.tenants import open_tenant_store
    focus = args.tenant or SETTINGS.tenant_id
    tstore = open_tenant_store(_os.getenv("ABEN_TENANT_DB", "abenlux-tenants.db"))
    cohort = [t.tenant_id for t in tstore.list(org=args.org)]
    tstore.close()
    store = open_store(SETTINGS.db_path)
    if not cohort:
        cohort = store.distinct_tenants()
    if focus not in cohort:
        cohort = cohort + [focus]
    ledger = open_ledger(_os.getenv("ABEN_LEDGER_DB", "abenlux-ledger.db"))
    reuse = {t: ledger.summary(store, t, k=SETTINGS.k_anon)["reuse_avoided_usd"] for t in cohort}
    ledger.close()
    out = build_benchmark(store, tenants=cohort, focus_tenant=focus, k=SETTINGS.k_anon,
                          dp_epsilon=SETTINGS.dp_epsilon, reuse_by_tenant=reuse)
    store.close()
    if args.json:
        print(json.dumps(out, indent=2))
        return
    rd = out["readiness"]
    print(f"== benchmark: {focus} vs org cohort ==")
    print(f" cohort: {rd['cohort_size']}/{rd['k_tenants_required']} tenants qualify  ({rd['reason']})")
    if not rd["ready"]:
        print(" your ratios (private to you until the cohort is ready):")
        for key, _label, _h in [(m[0], m[1], m[2]) for m in _bench_metrics()]:
            print(f"   {key:<28} {out['your_ratios'].get(key)}")
        return
    print(f"{'metric':<30} {'you':>10} {'cohort med':>12} {'percentile':>11}")
    for c in out["comparison"]:
        arrow = "higher=better" if c["higher_is_better"] else "lower=better"
        print(f"{c['label']:<30} {c['you']:>10.4f} {c['cohort_median']:>12.4f} "
              f"{c['your_percentile']*100:>9.0f}%  ({arrow})")


def _bench_metrics():
    from abenlux.analytics.benchmark import METRICS
    return METRICS


_OVERVIEW = """abenlux - AI spend -> value attribution plane

YOUR STUFF (private to you, never seen by management)
  abenlux me [--today]       your spend + recent nudges (--today: burn-rate to end of day)
  abenlux calls [--top-cost] your own recent calls, per-call (private to you)
  abenlux watch              live tail of your private signals (keep in a spare pane)
  abenlux graph              your on-device knowledge graph (objectives, tickets, purpose)
  abenlux collab             see collaboration matches; `collab intro <id>` to request an intro
  abenlux contact            your shareable contact card (revealed only on a mutual intro)

SET UP CAPTURE
  abenlux agent install      install the background agent (starts at login, Win/mac/Linux)
  abenlux gateway            run the on-device capture agent (loopback proxy + OTLP ingest)
  abenlux onboard <tool>     print the exact setup for your tool and shell
  abenlux tiers              the tool capability matrix
  abenlux detect             which AI tool is detected here
  abenlux mock               a fake upstream to verify capture without spending tokens

MANAGEMENT / IT
  abenlux serve              the collector + dashboard (k-anonymized, RBAC)
  abenlux report             spend -> value report (k-anonymity gated)
  abenlux tenant             create / list tenants (org units, geographies)
  abenlux benchmark          compare your tenant vs the org cohort (k-anon, DP)
  abenlux sync-cursor        pull Tier-3 Cursor usage (metadata only)

UTIL
  abenlux demo               run the full edge pipeline once, offline
  abenlux cost <model>       price an interaction

Run `abenlux <command> -h` for details on any command."""


def cmd_help(_args) -> None:
    print(_OVERVIEW)


def cmd_mcp(args) -> None:
    from abenlux.mcp_server import serve
    serve()


def cmd_proxy(args) -> None:
    # the forward TLS-terminating proxy. works for a subscription tool and a key tool alike, because the
    # tool routes through it as an ordinary HTTPS proxy instead of pointing a base url at it.
    from abenlux.capture.forward_proxy import serve
    serve(args.port)


def cmd_ca(args) -> None:
    # print the local CA the proxy uses, so a developer can trust it once.
    from abenlux.capture.forward_proxy import LocalCA
    ca = LocalCA()
    print(f"Abenlux local CA certificate: {ca.cert_path}")
    print("Trust it once so your tools accept the proxy:")
    print("  Windows :  certutil -addstore -user Root \"" + str(ca.cert_path) + "\"")
    print("  macOS   :  sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain \""
          + str(ca.cert_path) + "\"")
    print("  Linux   :  copy it into /usr/local/share/ca-certificates and run update-ca-certificates")
    print("  Node tools also honor:  NODE_EXTRA_CA_CERTS=" + str(ca.cert_path))


def cmd_run(args) -> None:
    # start the forward proxy, point ONE tool's process at it (and trust the CA for that process only),
    # then run the tool. only this process tree is proxied, so the browser and everything else is untouched.
    import os
    import subprocess
    import threading

    from abenlux.capture.forward_proxy import make_server
    server = make_server(args.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ca = str(server.ca.cert_path)
    proxy = f"http://127.0.0.1:{args.port}"
    e = dict(os.environ)
    e.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy, https_proxy=proxy, http_proxy=proxy,
             NODE_EXTRA_CA_CERTS=ca, SSL_CERT_FILE=ca, REQUESTS_CA_BUNDLE=ca)
    cmd = [args.tool, *args.toolargs]
    print(f"abenlux: routing {args.tool} through the local proxy on {proxy} (CA {ca})", flush=True)
    try:
        rc = subprocess.run(cmd, env=e).returncode
    except FileNotFoundError:
        print(f"abenlux: could not find the tool '{args.tool}' on PATH")
        rc = 127
    finally:
        server.shutdown()
    raise SystemExit(rc)


def main() -> None:
    p = argparse.ArgumentParser(prog="abenlux", description="AI spend -> value attribution plane.",
                                add_help=True)
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("demo", help="run the full edge pipeline once, offline").set_defaults(func=cmd_demo)

    g = sub.add_parser("gateway", help="run the on-device capture agent")
    g.add_argument("--port", type=int, default=8088)
    g.set_defaults(func=cmd_gateway)

    ag = sub.add_parser("agent", help="background capture agent: install/run at login (Win/mac/Linux)")
    ag.add_argument("action", nargs="?", choices=["install", "uninstall", "status", "run"],
                    default="status", help="install (autostart at login), uninstall, status, or run")
    ag.add_argument("--port", type=int, default=8088)
    ag.set_defaults(func=cmd_agent)

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

    sub.add_parser("mcp", help="run the read plane as an MCP server for coding agents").set_defaults(func=cmd_mcp)

    px = sub.add_parser("proxy", help="forward HTTPS proxy: captures + compresses ANY tool, subscription or key")
    px.add_argument("--port", type=int, default=8889)
    px.set_defaults(func=cmd_proxy)

    sub.add_parser("ca", help="print the local CA the proxy uses, and how to trust it").set_defaults(func=cmd_ca)

    rn = sub.add_parser("run", help="run a tool routed through the forward proxy (only that tool, not the browser)")
    rn.add_argument("--port", type=int, default=8889)
    rn.add_argument("tool", help="the tool to launch, e.g. claude, codex, aider, gemini")
    rn.add_argument("toolargs", nargs=argparse.REMAINDER, help="arguments passed through to the tool")
    rn.set_defaults(func=cmd_run)

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
    r.add_argument("--tenant", help="scope to one tenant (org unit / geography), default ABEN_TENANT")
    r.set_defaults(func=cmd_report)

    tn = sub.add_parser("tenant", help="create / list tenants (org units, geographies)")
    tn.add_argument("tenant_cmd", nargs="?", choices=["create", "list"], default="list")
    tn.add_argument("tenant_id", nargs="?", help="tenant id, e.g. acme-us (for create)")
    tn.add_argument("--org", default="default", help="the org the tenant belongs to")
    tn.add_argument("--name", help="display name")
    tn.add_argument("--residency", help="data-residency region, default ABEN_RESIDENCY")
    tn.add_argument("--json", action="store_true")
    tn.set_defaults(func=cmd_tenant)

    bm = sub.add_parser("benchmark", help="compare your tenant vs the org cohort (k-anon, DP)")
    bm.add_argument("--tenant", help="focus tenant, default ABEN_TENANT")
    bm.add_argument("--org", default="default", help="the org whose tenants form the cohort")
    bm.add_argument("--json", action="store_true")
    bm.set_defaults(func=cmd_benchmark)

    m = sub.add_parser("me", help="your own private spend + recent nudges")
    m.add_argument("-n", type=int, default=20, help="how many recent nudges to show")
    m.add_argument("--today", action="store_true", help="spend since midnight + run-rate to end of day")
    m.set_defaults(func=cmd_me)

    cl2 = sub.add_parser("calls", help="your own recent calls, per-call (private to you)")
    cl2.add_argument("-n", type=int, default=20, help="how many calls to show")
    cl2.add_argument("--today", action="store_true", help="only calls since local midnight")
    cl2.add_argument("--top-cost", action="store_true", dest="top_cost", help="order by most expensive")
    cl2.add_argument("--objective", help="filter to one objective label")
    cl2.add_argument("--json", action="store_true")
    cl2.set_defaults(func=cmd_calls)

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
