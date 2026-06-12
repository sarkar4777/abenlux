#!/usr/bin/env python3
"""
Developer and management experience, run on the plain Windows machine as a real user would, with real
models. It boots the collector and a gateway as ordinary processes, drives a handful of developers
through real calls, then walks the whole product the way two people actually use it. A developer checks
their own private view and asks a peer for help. A manager reads the spend to value report, the savings,
the waste proposals, and the renewal pack. It also runs the real command line tool and the agent tools
on this machine, so we know the experience works here and not only in a container.

  ANTHROPIC_API_KEY=... OPENAI_API_KEY=... GEMINI_API_KEY=... python examples/windows-e2e/windows_native.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

import httpx

WORK = tempfile.mkdtemp(prefix="aben-win-")
HMAC = "windows-native-hmac"
INGEST = "windows-native-ingest"
COLLECTOR = "http://127.0.0.1:8096"
GW_PORT = 8097
A_KEY, G_KEY, O_KEY = os.getenv("ANTHROPIC_API_KEY", ""), os.getenv("GEMINI_API_KEY", ""), os.getenv("OPENAI_API_KEY", "")
A_MODEL, G_MODEL, O_MODEL = "claude-haiku-4-5-20251001", "gemini-2.5-flash", "gpt-4o-mini"
PROCS: list[subprocess.Popen] = []
CHECKS: list[tuple[str, bool, str]] = []
CHECKOUT = "Make the checkout payment capture idempotent so a duplicate webhook never double charges."


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'OK ' if ok else 'GAP'}] {name}" + (f"  {detail}" if detail else ""), flush=True)
    return bool(ok)


def section(t):
    print(f"\n=== {t} ===", flush=True)


def env(**extra):
    e = dict(os.environ)
    e.update(ABEN_HMAC_KEY=HMAC, ABEN_INGEST_TOKEN=INGEST, ABEN_KG=f"{WORK}/kg.yaml", ABEN_K_ANON="3",
             ABEN_NOTIFY="0", PYTHONUNBUFFERED="1")
    e.update(extra)
    return e


def collector_env():
    return env(ABEN_PRINCIPALS=f"{WORK}/principals.yaml", ABEN_DB=f"{WORK}/central.db",
               ABEN_LEDGER_DB=f"{WORK}/ledger.db", ABEN_TENANT_DB=f"{WORK}/tenants.db",
               ABEN_MATCH_DB=f"{WORK}/matches.db", ABEN_CONTACT_DB=f"{WORK}/contacts.db",
               ABEN_CAPSULE_DB=f"{WORK}/capsules.db", ABEN_RELAY_DB=f"{WORK}/relay.db",
               ABEN_OUTCOME_DB=f"{WORK}/outcomes.db", ABEN_EXCHANGE_DB=f"{WORK}/exchange.db")


def gw_env():
    return env(ABEN_TENANT="acme-eu", ABEN_RESIDENCY="eu", ABEN_COLLECTOR_URL=COLLECTOR,
               ABEN_DB=f"{WORK}/edge.db", ABEN_LOCAL_DB=f"{WORK}/local.db", ABEN_MATCH_DB=f"{WORK}/edgematch.db",
               ABEN_EXACT_CACHE="1")


def start(name, args, e):
    f = open(f"{WORK}/{name}.log", "w")
    PROCS.append(subprocess.Popen(args, env=e, stdout=f, stderr=subprocess.STDOUT))


def wait(url, t=40):
    end = time.time() + t
    while time.time() < end:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return True
        except Exception:
            time.sleep(0.4)
    return False


def shutdown():
    for p in PROCS:
        try:
            p.terminate()
        except Exception:
            pass
    for p in PROCS:
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()


def api(token, path, method="GET", body=None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    fn = httpx.get if method == "GET" else httpx.post
    kw = {"headers": h, "timeout": 20.0}
    if method != "GET":
        kw["json"] = body or {}
    return fn(COLLECTOR + path, **kw)


def gw_call(actor, provider, prompt, *, branch="feature/ACME-100", tool="claude-code"):
    base = f"http://127.0.0.1:{GW_PORT}"
    h = {"content-type": "application/json", "x-aben-actor": actor, "x-aben-tool": tool, "x-aben-branch": branch}
    if provider == "anthropic":
        url, h["x-api-key"], h["anthropic-version"] = f"{base}/v1/messages", A_KEY, "2023-06-01"
        body = {"model": A_MODEL, "max_tokens": 20, "messages": [{"role": "user", "content": prompt}]}
    elif provider == "google":
        url, h["x-goog-api-key"] = f"{base}/v1beta/models/{G_MODEL}:generateContent", G_KEY
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 20}}
    else:
        url, h["authorization"] = f"{base}/v1/chat/completions", f"Bearer {O_KEY}"
        body = {"model": O_MODEL, "max_tokens": 20, "messages": [{"role": "user", "content": prompt}]}
    try:
        return httpx.post(url, headers=h, json=body, timeout=60.0).status_code
    except Exception:
        return None


def write_config():
    import yaml
    open(f"{WORK}/kg.yaml", "w").write(
        "objectives:\n  - {id: obj-acme, label: \"Acme Checkout\", kind: client, client: acme, monthly_budget_usd: 50}\n"
        "ticket_prefix_to_objective:\n  ACME: obj-acme\n")
    principals = [
        {"token": "acme-admin", "subject": "acme-admin", "role": "admin", "tenant_id": "acme-eu", "org": "acme"},
        {"token": "acme-eu-mgr", "subject": "acme-eu-mgr", "role": "manager", "tenant_id": "acme-eu", "org": "acme"},
        {"token": "acme-fin", "subject": "acme-fin", "role": "finance", "tenant_id": "acme-eu", "org": "acme"},
    ]
    for i in range(5):
        t = f"acme-eu-dev{i}"
        principals.append({"token": t, "subject": t, "role": "developer", "tenant_id": "acme-eu", "org": "acme", "slack": f"@{t}"})
    open(f"{WORK}/principals.yaml", "w").write(yaml.safe_dump({"principals": principals}))


def run_cli(args, e) -> tuple[int, str]:
    r = subprocess.run([sys.executable, "-m", "abenlux.cli"] + args, env=e, capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    if not (A_KEY and G_KEY and O_KEY):
        print("set ANTHROPIC_API_KEY, OPENAI_API_KEY and GEMINI_API_KEY")
        return 2
    section("Boot the stack as plain Windows processes (real upstreams)")
    write_config()
    start("collector", [sys.executable, "-m", "uvicorn", "abenlux.api.server:app", "--port", "8096"], collector_env())
    start("gateway", [sys.executable, "-m", "uvicorn", "abenlux.capture.gateway:app", "--port", str(GW_PORT)], gw_env())
    ok = wait(f"{COLLECTOR}/health") and wait(f"http://127.0.0.1:{GW_PORT}/health")
    if not check("collector and gateway are up on Windows", ok):
        for fn in os.listdir(WORK):
            if fn.endswith(".log"):
                print(open(f"{WORK}/{fn}").read()[-1200:])
        return 1
    api("acme-admin", "/api/tenants", "POST", {"tenant_id": "acme-eu", "display_name": "Acme EU", "residency": "eu"})

    section("Five developers do real work (Anthropic, OpenAI, Gemini)")
    for i in range(5):
        gw_call(f"acme-eu-dev{i}", ["anthropic", "google", "openai"][i % 3], CHECKOUT, tool=["claude-code", "aider", "cline", "opencode", "gemini-cli"][i])
    gw_call("acme-eu-dev0", "anthropic", "Refactor the shared logging helper repo wide.", branch="chore/none")
    last = -1
    for _ in range(25):
        time.sleep(2.0)
        n = httpx.get(f"{COLLECTOR}/health", timeout=5, headers={"Authorization": f"Bearer {INGEST}"}).json().get("events", 0)
        if n == last and n > 0:
            break
        last = n
    check("the work was captured", last > 3, f"{last} events")
    httpx.post(f"{COLLECTOR}/v1/outcomes", json=[{"outcome_id": f"o{i}", "ticket_id": "ACME-100", "merged": 1, "lines_added": 25} for i in range(3)],
               headers={"Authorization": f"Bearer {INGEST}"}, timeout=10)

    section("DEVELOPER experience (private to the developer)")
    me = api("acme-eu-dev0", "/api/me").json()
    check("a developer sees their own spend and work mix", me.get("cost_usd", -1) >= 0 and "work_type_mix" in me)
    check("a developer is blocked from the management report", api("acme-eu-dev0", "/api/report").status_code == 403)
    reuse = None
    for i in range(5):
        m = api(f"acme-eu-dev{i}", "/api/me").json().get("collaboration_matches", [])
        r = [x for x in m if x.get("mode") == "solved_reuse"]
        if r:
            reuse = (f"acme-eu-dev{i}", r)
            break
    check("a developer sees already-solved work as reuse", reuse is not None)
    if reuse:
        cap = next((x.get("capsule") for x in reuse[1] if x.get("capsule")), None)
        check("the reuse card is content free and useful", bool(cap), f"{cap}")
        mid = reuse[1][0]["id"]
        check("a developer can ask the peer for help with no intro",
              api(reuse[0], f"/api/collab/{mid}/ask", "POST", {"text": "how did you key it?"}).status_code == 200)

    section("MANAGEMENT experience (only grouped numbers)")
    rep = api("acme-eu-mgr", "/api/report").json()
    check("the manager sees attributed spend", rep.get("total_cost_usd", -1) >= 0)
    check("spend is joined to shipped work (value)", (rep.get("value") or {}).get("merged") == 3)
    check("the compression and shadow blocks are present", "shadow" in (rep.get("compression") or {}))
    check("the negotiation pack is ready", (api("acme-eu-mgr", "/api/negotiation").json() or {}).get("ready") is True)
    check("orphan recovery is reachable", isinstance(api("acme-eu-mgr", "/api/orphans").json().get("proposals"), list))

    section("The real command line tool and agent tools, on this Windows machine")
    rc, out = run_cli(["report", "--tenant", "acme-eu"], collector_env())
    check("abenlux report runs on Windows and prints the management view",
          rc == 0 and "management report" in out.lower() and "cost:$" in out, f"rc={rc}")
    rc_empty, _ = run_cli(["report", "--tenant", "nobody-here"], collector_env())
    check("abenlux report does not crash on an empty or sub-k tenant", rc_empty == 0, f"rc={rc_empty}")
    rc2, out2 = run_cli(["cost", A_MODEL, "--input", "100000", "--output", "2000"], env())
    check("abenlux cost runs on Windows", rc2 == 0, f"rc={rc2}")
    from abenlux.mcp_server import tool_cost_estimate
    est = tool_cost_estimate(A_MODEL, input_tokens=100000)
    check("the agent cost tool runs on Windows", est.get("cost_usd", 0) > 0)

    section("RESULT")
    gaps = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n  {len(CHECKS) - len(gaps)}/{len(CHECKS)} checks passed", flush=True)
    for g in gaps:
        print(f"    - {g}", flush=True)
    return len(gaps)


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        shutdown()
    sys.exit(code)
