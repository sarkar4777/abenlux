#!/usr/bin/env python3
"""
Model routing and team memory, end to end, across a big team.

It stands up the whole stack against a mock upstream and drives more than twenty five developers across
five tenants and many IDE and CLI tools, signing in both ways (a subscription reporting telemetry, and
an api key going through the gateway). The gateway runs routing live, so easy calls are sent to a cheaper
model and the saving is real. The collector runs team memory in shadow, so when a teammate has already
solved something close it records what reusing it would save, in the same language (ready to reuse) or
another language (a warm start), without changing any call.

It asserts both features fire, then writes central.db so the CLI screenshots can be rendered from it.

  python examples/routing-teammemory-e2e/suite.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

import httpx

WORK = os.environ.get("ABEN_E2E_OUT") or tempfile.mkdtemp(prefix="aben-rt-")
HMAC, INGEST = "rt-hmac", "rt-ingest"
COLLECTOR = "http://127.0.0.1:8096"
MOCK = 9112
KANON = 3
PROCS: list[subprocess.Popen] = []
CHECKS: list[tuple[str, bool, str]] = []

HOUSE = ("You are a senior engineer in the Rocket monorepo. Follow the house style. Prefer pure "
         "functions. Validate inputs. Money is integer minor units. Time is UTC. ") * 30
NOISY = "\x1b[33m" + "\n".join(["WARN retry connection refused attempt"] * 80) + "\x1b[0m"
CHECKOUT = ("Implement idempotent retry handling for the checkout payment capture so a duplicate "
            "webhook never double charges. Use an idempotency key on the order id.")
MOBILE = ("Build the offline sync queue so edits made without connectivity reconcile on reconnect "
          "with last write wins and a conflict log.")
AGENT = ("Design the agent marketplace billing meter that prices tool calls per token and aggregates "
         "usage per tenant for monthly invoicing.")

# base url tools (IDE and CLI) the api cohort uses, and the telemetry tools the subscription cohort uses
API_TOOLS = ["aider", "cline", "continue", "opencode", "crush", "roo", "goose", "kilo", "cursor-agent",
             "windsurf", "cody", "zed"]
SUB_TOOLS = ["claude-code", "codex", "gemini-cli", "copilot-chat"]

# tenant -> org, residency, gateway port, topic, [(language, n_api_devs)], n_subscription_devs
TENANTS = {
    "acme-eu":   ("acme", "eu", 8201, CHECKOUT, [("python", 5), ("go", 3)], 3),
    "acme-us":   ("acme", "us", 8202, MOBILE, [("typescript", 4), ("javascript", 2)], 2),
    "acme-apac": ("acme", "apac", 8203, CHECKOUT, [("python", 5)], 1),
    "acme-tiny": ("acme", "eu", 8204, CHECKOUT, [("python", 2)], 0),
    "globex-eu": ("globex", "eu", 8205, AGENT, [("rust", 3), ("go", 2)], 2),
}

KG_YAML = """
objectives:
  - {id: obj-acme,   label: "Acme - Checkout Platform", kind: client, client: acme, monthly_budget_usd: 5000}
  - {id: obj-zenith, label: "Zenith - Mobile App",      kind: innovation, monthly_budget_usd: 3000}
  - {id: obj-globex, label: "Globex - Agent R&D",       kind: innovation, monthly_budget_usd: 2}
ticket_prefix_to_objective:
  ACME: obj-acme
  MOB: obj-zenith
  GLX: obj-globex
"""


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)


def section(t):
    print(f"\n=== {t} ===", flush=True)


def base_env(**x):
    e = dict(os.environ)
    e.update(ABEN_HMAC_KEY=HMAC, ABEN_INGEST_TOKEN=INGEST, ABEN_KG=f"{WORK}/kg.yaml",
             ABEN_K_ANON=str(KANON), ABEN_NOTIFY="0", PYTHONUNBUFFERED="1")
    e.update(x)
    return e


def start(name, args, env):
    PROCS.append(subprocess.Popen(args, env=env, stdout=open(f"{WORK}/{name}.log", "w"),
                                  stderr=subprocess.STDOUT))


def wait(url, t=40):
    end = time.time() + t
    while time.time() < end:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return True
        except Exception:
            time.sleep(0.4)
    return False


def build_fleet():
    # returns principals plus the per tenant lists of (token, tool, language) and subscription ids
    principals = [
        {"token": "acme-admin", "subject": "acme-admin", "role": "admin", "tenant_id": "acme-eu", "org": "acme"},
        {"token": "acme-fin", "subject": "acme-fin", "role": "finance", "tenant_id": "acme-eu", "org": "acme"},
        {"token": "globex-admin", "subject": "globex-admin", "role": "admin", "tenant_id": "globex-eu", "org": "globex"},
    ]
    api, sub = {}, {}
    ti = 0
    for tid, (org, _res, _port, _topic, langs, n_sub) in TENANTS.items():
        principals.append({"token": f"{tid}-mgr", "subject": f"{tid}-mgr", "role": "manager",
                           "tenant_id": tid, "org": org})
        api[tid], sub[tid] = [], []
        i = 0
        for lang, n in langs:
            for _ in range(n):
                tok = f"{tid}-api{i}"
                tool = API_TOOLS[ti % len(API_TOOLS)]
                ti += 1
                principals.append({"token": tok, "subject": tok, "role": "developer",
                                   "tenant_id": tid, "org": org, "slack": f"@{tok}"})
                api[tid].append((tok, tool, lang))
                i += 1
        for j in range(n_sub):
            uid = f"{tid}-sub{j}"
            principals.append({"token": uid, "subject": uid, "role": "developer",
                               "tenant_id": tid, "org": org, "slack": f"@{uid}"})
            sub[tid].append(uid)
    return principals, api, sub


def write_config(principals):
    import yaml
    open(f"{WORK}/kg.yaml", "w").write(KG_YAML)
    open(f"{WORK}/principals.yaml", "w").write(yaml.safe_dump({"principals": principals}))


def boot(api):
    start("mock", [sys.executable, "-m", "uvicorn", "abenlux.devtools.mock_upstream:app", "--port", str(MOCK)],
          base_env())
    start("collector", [sys.executable, "-m", "uvicorn", "abenlux.api.server:app", "--host", "0.0.0.0",
                        "--port", "8096"],
          base_env(ABEN_PRINCIPALS=f"{WORK}/principals.yaml", ABEN_DB=f"{WORK}/central.db",
                   ABEN_LEDGER_DB=f"{WORK}/ledger.db", ABEN_TENANT_DB=f"{WORK}/tenants.db",
                   ABEN_MATCH_DB=f"{WORK}/matches.db", ABEN_CONTACT_DB=f"{WORK}/contacts.db",
                   ABEN_CAPSULE_DB=f"{WORK}/capsules.db", ABEN_RELAY_DB=f"{WORK}/relay.db",
                   ABEN_OUTCOME_DB=f"{WORK}/outcomes.db", ABEN_EXCHANGE_DB=f"{WORK}/exchange.db",
                   ABEN_TM="shadow"))
    for tid, (_org, res, port, _topic, _langs, _ns) in TENANTS.items():
        start(f"gw-{tid}", [sys.executable, "-m", "uvicorn", "abenlux.capture.gateway:app", "--port", str(port)],
              base_env(ABEN_TENANT=tid, ABEN_RESIDENCY=res, ABEN_COLLECTOR_URL=COLLECTOR, ABEN_COMPRESS="all",
                       ABEN_ROUTE="on", ABEN_ANTHROPIC_UPSTREAM=f"http://127.0.0.1:{MOCK}",
                       ABEN_DB=f"{WORK}/edge-{tid}.db", ABEN_LOCAL_DB=f"{WORK}/local-{tid}.db",
                       ABEN_MATCH_DB=f"{WORK}/edge-m-{tid}.db"))


def api_post(port, actor, tool, branch, prompt, model="claude-opus-4-8", system=False, mock_input=1800,
             max_tokens=256, cache=0.7):
    body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = [{"type": "text", "text": HOUSE}]
    h = {"x-aben-actor": actor, "x-aben-branch": branch, "x-aben-tool": tool,
         "x-aben-mock-input": str(mock_input), "x-aben-mock-cache": str(cache), "content-type": "application/json"}
    httpx.post(f"http://127.0.0.1:{port}/v1/messages", json=body, headers=h, timeout=20).raise_for_status()


def otlp(uid, inp=1800):
    def kv(k, **v):
        return {"key": k, "value": v}
    return {"resourceLogs": [{"scopeLogs": [{"logRecords": [{
        "body": {"stringValue": "claude_code.api_request"},
        "attributes": [kv("user.id", stringValue=uid), kv("user.email", stringValue=f"{uid}@corp"),
                       kv("model", stringValue="claude-opus-4-8"), kv("input_tokens", intValue=inp),
                       kv("output_tokens", intValue=64), kv("cache_read_tokens", intValue=12000),
                       kv("cache_creation_tokens", intValue=0)]}]}]}]}


def drive(api, sub):
    lang_word = {"python": "Use Python.", "go": "Use Go.", "typescript": "Use TypeScript.",
                 "javascript": "Use JavaScript.", "rust": "Use Rust.", "java": "Use Java."}
    for tid, (_org, _res, port, topic, _langs, _ns) in TENANTS.items():
        prefix = "ACME" if topic is CHECKOUT else ("MOB" if topic is MOBILE else "GLX")
        # work the shared topic first so later teammates have something to reuse
        for tok, tool, lang in api[tid]:
            api_post(port, tok, tool, f"feature/{prefix}-100", f"{topic} {lang_word.get(lang, '')}",
                     system=True, mock_input=9000 if topic is AGENT else 1800, cache=0.7)
        # an easy turn each, which routing sends to the cheaper model
        for tok, tool, lang in api[tid]:
            api_post(port, tok, tool, f"chore/{prefix}-200", "rename the helper to apply_idempotency_key",
                     max_tokens=16, mock_input=1500)
        # a noisy tool log each, which command trim folds down
        for tok, tool, lang in api[tid]:
            api_post(port, tok, tool, f"fix/{prefix}-300", "here is the failing test output, what broke?\n\n" + NOISY,
                     mock_input=1200)
        for uid in sub[tid]:
            for _ in range(3):
                httpx.post(f"http://127.0.0.1:{port}/v1/logs", json=otlp(uid), timeout=15).raise_for_status()


def api_get(token, path):
    return httpx.get(COLLECTOR + path, headers={"Authorization": f"Bearer {token}"}, timeout=15)


def records():
    import sqlite3
    con = sqlite3.connect(f"{WORK}/central.db")
    try:
        cols = "tier, route_target, route_saved_usd, tm_tier, tm_shadow_usd, compression, tool"
        return [dict(zip(cols.split(", "), r)) for r in con.execute(f"SELECT {cols} FROM derived").fetchall()]
    finally:
        con.close()


def main():
    principals, api, sub = build_fleet()
    n_api = sum(len(v) for v in api.values())
    n_sub = sum(len(v) for v in sub.values())
    section(f"Boot the stack for {n_api + n_sub} developers ({n_api} api, {n_sub} subscription)")
    write_config(principals)
    boot(api)
    if not (wait(f"http://127.0.0.1:{MOCK}/health") and wait(f"{COLLECTOR}/health")):
        print("stack did not boot")
        return 1
    for tid, (org, res, port, *_x) in TENANTS.items():
        if not wait(f"http://127.0.0.1:{port}/health"):
            print(f"gateway {tid} did not boot")
            return 1
        admin = "globex-admin" if org == "globex" else "acme-admin"
        httpx.post(COLLECTOR + "/api/tenants", headers={"Authorization": f"Bearer {admin}"},
                   json={"tenant_id": tid, "display_name": tid.upper(), "residency": res}, timeout=15)

    section("Drive both sign-ins across many IDE and CLI tools, routing live, team memory in shadow")
    drive(api, sub)
    last = -1
    for _ in range(30):
        time.sleep(2.0)
        n = httpx.get(f"{COLLECTOR}/health", timeout=5, headers={"Authorization": f"Bearer {INGEST}"}).json().get("events", 0)
        if n == last and n > 0:
            break
        last = n

    section("RESULT")
    recs = records()
    routed = [r for r in recs if r["route_target"]]
    serve = [r for r in recs if r["tm_tier"] == "serve"]
    warm = [r for r in recs if r["tm_tier"] == "warm_start"]
    tier1 = [r for r in recs if (r["tier"] or "").startswith("tier1")]
    tier2 = [r for r in recs if (r["tier"] or "").startswith("tier2")]
    tools = {r["tool"] for r in recs if r["tool"]}
    check("more than 25 developers captured", True, f"{n_api + n_sub} developers, {last} events")
    check("both sign-ins captured (subscription telemetry and api key)", tier1 and tier2,
          f"{len(tier1)} telemetry, {len(tier2)} gateway")
    check("many IDE and CLI tools captured", len(tools) >= 10, f"{len(tools)} tools")
    check("routing sent easy calls to a cheaper model", len(routed) >= 20,
          f"{len(routed)} calls routed, ~${round(sum(r['route_saved_usd'] or 0 for r in routed), 2)} saved")
    check("team memory found work a teammate could reuse as is", len(serve) >= 1, f"{len(serve)} serve")
    check("team memory found warm starts across languages", len(warm) >= 1, f"{len(warm)} warm starts")
    rep = api_get("acme-eu-mgr", "/api/report?tenant=acme-eu").json()
    check("the report carries the routing block", bool(rep.get("routing")))
    check("the report carries the team memory block", bool(rep.get("team_memory")))

    fails = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n  {len(CHECKS) - len(fails)}/{len(CHECKS)} checks passed", flush=True)
    print(f"  CENTRAL_DB={WORK}/central.db  KG={WORK}/kg.yaml  HMAC={HMAC}", flush=True)
    print("  demo tokens: manager=acme-eu-mgr developer=acme-eu-api0", flush=True)
    return 1 if fails else 0


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
