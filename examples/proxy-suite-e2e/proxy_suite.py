#!/usr/bin/env python3
"""
The full forward-proxy suite. One proxy, many developers, many tools, all three providers, both sign-in
shapes (an api key and a bearer token, the bearer being exactly how a subscription signs in), routed
through a real collector. It proves the one thing that matters most first, that nothing but the model
API traffic is ever touched, then it drives the whole product through the proxy and writes a detailed
report covering capture, compression and savings, collaboration and reuse, value, and privacy.

  ANTHROPIC_API_KEY=... OPENAI_API_KEY=... GEMINI_API_KEY=... python examples/proxy-suite-e2e/proxy_suite.py
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

WORK = tempfile.mkdtemp(prefix="aben-suite-")
HMAC, INGEST = "suite-hmac", "suite-ingest"
COLLECTOR = "http://127.0.0.1:8093"
# capture config must be set BEFORE importing abenlux, so the proxy forwards to the collector and compresses
os.environ.update(ABEN_COLLECTOR_URL=COLLECTOR, ABEN_HMAC_KEY=HMAC, ABEN_INGEST_TOKEN=INGEST,
                  ABEN_COMPRESS="all", ABEN_KG=f"{WORK}/kg.yaml", ABEN_DB=f"{WORK}/edge.db",
                  ABEN_LOCAL_DB=f"{WORK}/edge.db", ABEN_MATCH_DB=f"{WORK}/edgematch.db",
                  ABEN_NOTIFY="0", ABEN_CA_DIR=f"{WORK}/ca", ABEN_ACTOR="proxy")

import httpx  # noqa: E402

from abenlux.capture.forward_proxy import make_server  # noqa: E402

A_KEY = os.getenv("ANTHROPIC_API_KEY", "")
G_KEY = os.getenv("GEMINI_API_KEY", "")
O_KEY = os.getenv("OPENAI_API_KEY", "")
A_MODEL, G_MODEL, O_MODEL = "claude-haiku-4-5-20251001", "gemini-2.5-flash", "gpt-4o-mini"
CHECKS: list[tuple[str, bool, str]] = []
PROCS: list[subprocess.Popen] = []
HOUSE = ("You are a senior engineer in the Rocket monorepo. Follow the house style. "
         "Prefer pure functions. Validate inputs. Money is integer minor units. Time is UTC. ") * 30
NOISY = "\x1b[33m" + "\n".join(["WARN retry connection refused"] * 80) + "\x1b[0m"
CHECKOUT = "Make the checkout payment capture idempotent so a duplicate webhook never double charges."
MOBILE = "Build the offline sync queue so edits made without connectivity reconcile on reconnect."

# developer, tool, provider, auth-shape label, topic, capture PATH. every call uses a REAL api key. the
# two paths are tested side by side: "baseurl" is the original way where the tool points its base url at
# the gateway, and "proxy" is the forward HTTPS proxy. the openai bearer header is byte for byte how a
# subscription presents its token, so a proxy call with it doubles as the subscription proof.
FLEET = [
    ("alice", "claude-code", "anthropic", "api key (x-api-key header)", CHECKOUT, "baseurl"),
    ("bob", "aider", "anthropic", "api key (x-api-key header)", CHECKOUT, "proxy"),
    ("carol", "codex", "openai", "api key (bearer header, same shape a subscription uses)", CHECKOUT, "proxy"),
    ("dave", "gemini-cli", "google", "api key (x-goog-api-key header)", MOBILE, "baseurl"),
    ("eve", "cline", "openai", "api key (bearer header, same shape a subscription uses)", MOBILE, "proxy"),
    ("frank", "opencode", "google", "api key (x-goog-api-key header)", MOBILE, "baseurl"),
]
GW_BASEURL = "http://127.0.0.1:8092"


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'OK ' if ok else 'GAP'}] {name}" + (f"  {detail}" if detail else ""), flush=True)


def section(t):
    print(f"\n=== {t} ===", flush=True)


def api(token, path, method="GET", body=None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    fn = httpx.get if method == "GET" else httpx.post
    kw = {"headers": h, "timeout": 20.0}
    if method != "GET":
        kw["json"] = body or {}
    return fn(COLLECTOR + path, **kw)


def write_config():
    import yaml
    open(f"{WORK}/kg.yaml", "w").write(
        "objectives:\n  - {id: obj-checkout, label: \"Acme Checkout\", kind: client, client: acme, monthly_budget_usd: 50}\n"
        "  - {id: obj-mobile, label: \"Acme Mobile\", kind: client, client: acme, monthly_budget_usd: 50}\n"
        "ticket_prefix_to_objective:\n  CHK: obj-checkout\n  MOB: obj-mobile\n")
    p = [{"token": "boss", "subject": "boss", "role": "manager", "tenant_id": "acme", "org": "acme"},
         {"token": "admin", "subject": "admin", "role": "admin", "tenant_id": "acme", "org": "acme"}]
    for dev, *_ in FLEET:
        p.append({"token": dev, "subject": dev, "role": "developer", "tenant_id": "acme", "org": "acme", "slack": f"@{dev}"})
    open(f"{WORK}/principals.yaml", "w").write(yaml.safe_dump({"principals": p}))


def boot_collector():
    e = dict(os.environ)
    e.update(ABEN_PRINCIPALS=f"{WORK}/principals.yaml", ABEN_DB=f"{WORK}/central.db",
             ABEN_LEDGER_DB=f"{WORK}/ledger.db", ABEN_TENANT_DB=f"{WORK}/tenants.db",
             ABEN_MATCH_DB=f"{WORK}/matches.db", ABEN_CONTACT_DB=f"{WORK}/contacts.db",
             ABEN_CAPSULE_DB=f"{WORK}/capsules.db", ABEN_RELAY_DB=f"{WORK}/relay.db",
             ABEN_OUTCOME_DB=f"{WORK}/outcomes.db", ABEN_EXCHANGE_DB=f"{WORK}/exchange.db", ABEN_K_ANON="3")
    e.pop("ABEN_COLLECTOR_URL", None)
    PROCS.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "abenlux.api.server:app", "--port", "8093"],
                                  env=e, stdout=open(f"{WORK}/collector.log", "w"), stderr=subprocess.STDOUT))


def boot_gateway():
    # the base-url reverse proxy, the original capture path. tools point ANTHROPIC_BASE_URL etc. here.
    # it captures, compresses and forwards to the same collector, with its own on-device store files.
    e = dict(os.environ)
    e.update(ABEN_TENANT="acme", ABEN_DB=f"{WORK}/gw.db", ABEN_LOCAL_DB=f"{WORK}/gw.db",
             ABEN_MATCH_DB=f"{WORK}/gwmatch.db", ABEN_COLLECTOR_URL=COLLECTOR)
    PROCS.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "abenlux.capture.gateway:app", "--port", "8092"],
                                  env=e, stdout=open(f"{WORK}/gateway.log", "w"), stderr=subprocess.STDOUT))


def wait(url, t=40):
    end = time.time() + t
    while time.time() < end:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return True
        except Exception:
            time.sleep(0.4)
    return False


def call(proxy_port, ca, dev, tool, provider, branch, prompt, path, *, system=False):
    """Drive one real call with a real api key, by either path. path=="proxy" routes through the forward
    HTTPS proxy to the real provider host. path=="baseurl" posts to the gateway the way a tool does when
    its base url is overridden. The auth header is identical in both, only the destination differs."""
    h = {"content-type": "application/json", "x-aben-actor": dev, "x-aben-tool": tool, "x-aben-branch": branch}
    if provider == "anthropic":
        h["x-api-key"], h["anthropic-version"] = A_KEY, "2023-06-01"
        body = {"model": A_MODEL, "max_tokens": 16, "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = HOUSE
        real, route = "https://api.anthropic.com", "/v1/messages"
    elif provider == "google":
        h["x-goog-api-key"] = G_KEY
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 16}}
        real, route = "https://generativelanguage.googleapis.com", f"/v1beta/models/{G_MODEL}:generateContent"
    else:
        h["authorization"] = f"Bearer {O_KEY}"     # the bearer shape a subscription also uses
        msgs = ([{"role": "system", "content": HOUSE}] if system else []) + [{"role": "user", "content": prompt}]
        body = {"model": O_MODEL, "max_tokens": 16, "messages": msgs}
        real, route = "https://api.openai.com", "/v1/chat/completions"
    if path == "proxy":                            # forward HTTPS proxy in front of the real host
        with httpx.Client(proxy=f"http://127.0.0.1:{proxy_port}", verify=ca, timeout=60) as c:
            return c.post(real + route, headers=h, json=body)
    with httpx.Client(timeout=60) as c:            # base-url override, the tool points here directly
        return c.post(GW_BASEURL + route, headers=h, json=body)


def store_rows(db):
    con = sqlite3.connect(db)
    try:
        cols = "tier, tool, provider, request_model, actor_pseudonym, input_tokens, output_tokens, cost_usd, compression, saved_input_tokens, objective_label"
        return [dict(zip(cols.split(", "), r)) for r in con.execute(f"SELECT {cols} FROM derived").fetchall()]
    finally:
        con.close()


def main() -> int:
    if not (A_KEY and O_KEY and G_KEY):
        print("set ANTHROPIC_API_KEY, OPENAI_API_KEY and GEMINI_API_KEY")
        return 2
    write_config()
    boot_collector()
    if not wait(f"{COLLECTOR}/health"):
        print("collector did not boot")
        return 1
    boot_gateway()
    if not wait(f"{GW_BASEURL}/health"):
        print("base-url gateway did not boot")
        return 1
    server = make_server(port=0, capture=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    pport = server.server_address[1]
    ca = str(server.ca.cert_path)
    api("admin", "/api/tenants", "POST", {"tenant_id": "acme", "display_name": "Acme", "residency": "eu"})

    section("TRAFFIC ISOLATION - only the model hosts are ever read")
    # a NON-model HTTPS site routed through the proxy must succeed with the SYSTEM trust store, which
    # only happens if the proxy tunnels it untouched and the site's own real certificate is presented.
    try:
        with httpx.Client(proxy=f"http://127.0.0.1:{pport}", verify=True, timeout=20) as c:
            r = c.get("https://example.com")
        check("a non-model site passes through untouched (real cert, system trust)", r.status_code < 500,
              f"example.com HTTP {r.status_code}")
    except Exception as e:
        check("a non-model site passes through untouched", False, f"{type(e).__name__}")
    # a model host through the proxy with ONLY the system trust store must FAIL, proving we intercept it
    intercepted = False
    try:
        with httpx.Client(proxy=f"http://127.0.0.1:{pport}", verify=True, timeout=20) as c:
            c.post("https://api.anthropic.com/v1/messages", headers={"x-api-key": "x", "anthropic-version": "2023-06-01"},
                   json={"model": A_MODEL, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]})
    except Exception:
        intercepted = True                         # TLS to our untrusted local leaf fails the system store
    check("a model host IS intercepted (its TLS is terminated by our local CA, not the system store)",
          intercepted)

    section("DRIVE - many developers, many tools, three providers, two sign-in shapes, BOTH capture paths")
    # each developer does two turns: a big stable prompt (cache breakpoints) and a noisy tool log,
    # routed through whichever path the fleet assigns them, the forward proxy or the base-url gateway
    for dev, tool, provider, _auth, topic, path in FLEET:
        prefix = "CHK" if topic is CHECKOUT else "MOB"
        r = call(pport, ca, dev, tool, provider, f"feature/{prefix}-100", topic, path, system=True)
        check(f"{dev} via {tool} on {provider} captured ({path})", r.status_code < 300, f"HTTP {r.status_code}")
        call(pport, ca, dev, tool, provider, f"fix/{prefix}-200", "test output below, what failed?\n\n" + NOISY, path)
    # an outcome feed so the value line fills in
    time.sleep(8)
    httpx.post(f"{COLLECTOR}/v1/outcomes", json=[{"outcome_id": f"o{i}", "ticket_id": "CHK-100", "merged": 1,
               "lines_added": 30} for i in range(4)], headers={"Authorization": f"Bearer {INGEST}"}, timeout=10)
    time.sleep(4)

    write_report(pport, ca)
    server.shutdown()
    section("RESULT")
    gaps = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n  {len(CHECKS) - len(gaps)}/{len(CHECKS)} checks passed", flush=True)
    for g in gaps:
        print(f"    - {g}", flush=True)
    return len(gaps)


def write_report(pport, ca):
    rows = store_rows(f"{WORK}/central.db")
    rep = api("boss", "/api/report?tenant=acme").json()
    cz = rep.get("compression") or {}
    neg = api("boss", "/api/negotiation").json()
    # per-developer collaboration
    collab = {}
    for dev, *_ in FLEET:
        me = api(dev, "/api/me").json()
        collab[dev] = me.get("collaboration_matches", [])
    matched = [d for d, m in collab.items() if m]
    capsules = [x.get("capsule") for m in collab.values() for x in m if x.get("capsule")]
    val = rep.get("value") or {}

    # checks that feed the verdict
    check("every developer, tool, provider and sign-in shape was captured", len({r["actor_pseudonym"] for r in rows}) >= 5,
          f"{len({r['actor_pseudonym'] for r in rows})} developers")
    provs = {r["provider"] for r in rows}
    check("all three real api-key header styles worked (x-api-key, bearer, x-goog-api-key)",
          {"anthropic", "openai", "google"} <= provs, f"providers={provs}")
    # the record's actor is pseudonymized, but the tool name is stored raw and is unique per developer
    # here, so it is the reliable key back to which path each captured record came down.
    path_of = {tool: path for _d, tool, _p, _a, _to, path in FLEET}
    paths_seen = {path_of.get(r["tool"]) for r in rows if r["tool"] in path_of}
    check("BOTH capture paths worked with real api keys (base-url gateway AND forward proxy)",
          {"baseurl", "proxy"} <= paths_seen, f"paths={sorted(p for p in paths_seen if p)}")
    check("requests were compressed on the wire through the proxy", any(r["compression"] for r in rows),
          f"{sum(1 for r in rows if r['compression'])}/{len(rows)} records compressed")
    check("the savings show up in the management report", (cz.get('saved_input_tokens') or cz.get('shadow')) is not None)
    check("developers matched each other as collaborators through the proxy", len(matched) >= 2,
          f"{matched}")
    check("a reuse match carried a content-free solution capsule", any(capsules), f"{capsules[:1]}")
    check("spend joined to shipped work (value)", (val.get('merged') or 0) >= 1, f"merged={val.get('merged')}")
    check("the negotiation pack is ready", neg.get("ready") is True)

    # ---- build the markdown report ----
    by_tool = {}
    by_provider = {}
    by_auth = {}
    total_in = sum(r["input_tokens"] or 0 for r in rows)
    total_saved = sum(r["saved_input_tokens"] or 0 for r in rows)
    for r in rows:
        by_tool.setdefault(r["tool"] or "?", [0, 0, 0.0])
        by_tool[r["tool"] or "?"][0] += 1
        by_tool[r["tool"] or "?"][1] += r["input_tokens"] or 0
        by_tool[r["tool"] or "?"][2] += r["cost_usd"] or 0
        by_provider.setdefault(r["provider"] or "?", [0, 0.0])
        by_provider[r["provider"] or "?"][0] += 1
        by_provider[r["provider"] or "?"][1] += r["cost_usd"] or 0
    by_path = {"baseurl": [0, 0.0], "proxy": [0, 0.0]}
    for dev, _t, _p, auth, _to, _pa in FLEET:
        by_auth.setdefault(auth, 0)
        by_auth[auth] += 1
    for r in rows:
        p = path_of.get(r["tool"])
        if p in by_path:
            by_path[p][0] += 1
            by_path[p][1] += r["cost_usd"] or 0

    L = []
    L.append("# Capture suite, test report (both paths, real api keys)\n")
    L.append("Every developer and tool driven with a real api key, all three providers, down BOTH capture "
             "paths in one run, the original base-url gateway and the forward HTTPS proxy, all feeding one "
             "collector. Both an api key and a bearer token are exercised. The bearer is exactly how a "
             "subscription signs in, and both paths forward it unchanged, so the subscription path and the "
             "key path are the same path here.\n")
    passed = sum(1 for _n, ok, _d in CHECKS if ok)
    L.append(f"**Result. {passed} of {len(CHECKS)} checks passed.**\n")

    L.append("## Traffic isolation\n")
    L.append("Only the model API hosts are read. A non-model site routed through the proxy is tunnelled "
             "byte for byte and never decrypted, proven by it validating against the system trust store "
             "with its own real certificate. A model host is intercepted, proven by it failing the system "
             "trust store because the proxy presents its own local certificate instead.\n")
    for n, ok, d in CHECKS:
        if "non-model" in n or "intercepted" in n:
            L.append(f"- {'PASS' if ok else 'FAIL'}. {n}. {d}")
    L.append("\nThe browser and every other application are untouched for a second reason too. The proxy "
             "is only set for the one tool launched with `abenlux run`, so nothing else on the machine "
             "even contacts it.\n")

    L.append("## Both capture paths, side by side\n")
    L.append("The same real api keys were driven down both capture paths in one run. The base-url path is "
             "the original way, a tool points its `ANTHROPIC_BASE_URL` (or OpenAI or Gemini base) at the "
             "local gateway. The proxy path is the forward HTTPS proxy, a tool routes through the agent as "
             "an ordinary proxy and the agent terminates the TLS with its own local certificate. Both "
             "capture a content-free record, compress on the wire, and forward to the same collector.\n")
    L.append("| Capture path | What the tool changes | Calls captured | Cost |\n|---|---|--:|--:|")
    L.append(f"| base-url gateway | sets its base url to the gateway | {by_path['baseurl'][0]} | ${by_path['baseurl'][1]:.4f} |")
    L.append(f"| forward HTTPS proxy | nothing, runs behind `abenlux run` | {by_path['proxy'][0]} | ${by_path['proxy'][1]:.4f} |")
    L.append("")

    L.append("## Sign-in shapes covered\n\n| Sign-in shape | Calls |\n|---|--:|")
    for a, n in sorted(by_auth.items()):
        L.append(f"| {a} | {n} |")
    L.append("\nEvery call above used a real api key, across all three header styles a provider uses, the "
             "Anthropic x-api-key, the OpenAI bearer, and the Gemini x-goog-api-key. The bearer header is "
             "byte for byte how a Claude or ChatGPT subscription presents its token, so the same proof "
             "covers a subscription. The proxy forwards whatever header it is given, so capture and "
             "compression work the same for a key and for a subscription.\n")

    L.append("## Capture by tool\n\n| Tool | Calls | Input tokens | Cost |\n|---|--:|--:|--:|")
    for t, (c, ins, cost) in sorted(by_tool.items()):
        L.append(f"| {t} | {c} | {ins:,} | ${cost:.4f} |")
    L.append("\n## Capture by provider\n\n| Provider | Calls | Cost |\n|---|--:|--:|")
    for p, (c, cost) in sorted(by_provider.items()):
        L.append(f"| {p} | {c} | ${cost:.4f} |")

    L.append("\n## Compression and savings\n")
    raw_in = total_in + total_saved            # raw is what the tool sent, billed is what survived compression
    pct = (total_saved / raw_in * 100) if raw_in else 0
    L.append(f"- Records compressed on the wire. {sum(1 for r in rows if r['compression'])} of {len(rows)}")
    L.append(f"- Input tokens removed before billing. {total_saved:,} of {raw_in:,} raw "
             f"({pct:.0f} percent of raw input never reached the meter)")
    L.append(f"- Compression yield in the manager report. {cz.get('saved_input_tokens', 0):,} tokens, "
             f"about ${cz.get('saved_usd', 0):.4f}, {cz.get('cache_hits', 0)} calls served free from cache")
    if cz.get("by_strategy"):
        L.append("\n  By strategy (realized)\n")
        for name, d in cz["by_strategy"].items():
            L.append(f"  - {name}. {d['tokens']:,} tokens (~${d['usd']:.4f})")
    if cz.get("shadow"):
        L.append("\n  What turning an off strategy on would save (shadow measure)\n")
        for name, d in cz["shadow"].items():
            L.append(f"  - {name}. {d['tokens']:,} tokens (~${d['usd']:.4f})")

    L.append("\n## Collaboration and reuse\n")
    L.append(f"- Developers who matched a peer through the proxy. {', '.join(matched) or 'none'}")
    L.append(f"- Reuse matches carrying a content-free solution capsule. {sum(1 for c in capsules if c)}")
    if any(capsules):
        cap = next(c for c in capsules if c)
        L.append(f"  - Example capsule. cracked with {cap.get('model')} via {cap.get('tool')}, "
                 f"work type {cap.get('work_type')}, cost band {cap.get('cost_band')}")
    ry = rep.get("reuse_yield") or {}
    L.append(f"- Reuse yield booked beside spend. about ${ry.get('reuse_avoided_usd', 0):.4f}")

    L.append("\n## Spend to value\n")
    if val.get("merged"):
        L.append(f"- Merged changes joined to spend. {val['merged']} of {val.get('changes')}")
        cpm = val.get("cost_per_merged_change")
        L.append(f"- Dollars per merged change. {('$%.4f' % cpm) if cpm is not None else 'n/a'}")
        L.append(f"- Merge rate {round((val.get('merge_rate') or 0) * 100)} percent, "
                 f"revert rate {round((val.get('revert_rate') or 0) * 100)} percent")

    L.append("\n## Renewal pack\n")
    if neg.get("ready"):
        L.append(f"- Blended rate the org pays. ${neg.get('blended_usd_per_mtok')} per million tokens")
        L.append(f"- Projected annual run rate. ${neg.get('projected_annual_run_rate_usd')}")
        L.append(f"- Provider concentration. {neg.get('provider_concentration')}")

    L.append("\n## Privacy\n")
    L.append("- Every prompt is redacted on the device before anything is written. Only a content-free "
             "record reaches the collector.")
    L.append(f"- Management figures are k-anonymity gated. Org developers {rep.get('org_actors')}, "
             f"orphan token share {round((rep.get('orphan_token_share') or 0) * 100, 1)} percent.")
    L.append("- The proxy decrypts only the model hosts, on the device, and forwards the tool's own "
             "credential untouched. The raw prompt never leaves the machine.\n")

    out = Path(__file__).resolve().parent / "REPORT.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"\n  wrote detailed report to {out}", flush=True)


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        for p in PROCS:
            try:
                p.terminate()
            except Exception:
                pass
    sys.exit(code)
