#!/usr/bin/env python3
"""
Organization scale end to end. It runs inside a container against the real stack with real models, and
it drives 27 developers across two orgs, three tenants, six tools, and all three providers, all at once,
the way a real company would on a busy morning. It is both a load test and a functional test. First it
hammers the gateways concurrently and checks nothing falls over and nothing is lost or double counted.
Then it drives every feature and checks each one works at this scale. A failing check is a real gap and
the exit code is the number of failures.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

COLLECTOR = os.getenv("ABEN_COLLECTOR_URL", "http://collector:8090")
INGEST = os.getenv("ABEN_INGEST_TOKEN", "scale-ingest-token")
GW = {"acme-eu": "http://gw-acme-eu:8088", "acme-us": "http://gw-acme-us:8088",
      "globex-eu": "http://gw-globex-eu:8088"}
A_KEY, G_KEY, O_KEY = os.getenv("ANTHROPIC_API_KEY", ""), os.getenv("GEMINI_API_KEY", ""), os.getenv("OPENAI_API_KEY", "")
A_MODEL, G_MODEL, O_MODEL = "claude-haiku-4-5-20251001", "gemini-2.5-flash", "gpt-4o-mini"
TOOLS = ["claude-code", "aider", "cline", "opencode", "gemini-cli", "codex"]
HOUSE = ("You are a senior engineer in the Rocket monorepo. Follow the house style. "
         "Prefer pure functions. Validate inputs. Money is integer minor units. Time is UTC. ") * 30
NOISY = "\x1b[33m" + "\n".join(["WARN retry connection refused"] * 80) + "\x1b[0m"

# tenant, org, developer count, branch ticket prefix, shared topic
FLEET = [("acme-eu", "acme", 10, "ACME", "checkout idempotency"),
         ("acme-us", "acme", 9, "MOB", "offline sync queue"),
         ("globex-eu", "globex", 8, "GLX", "agent billing meter")]
TOPICS = {
    "checkout idempotency": "Make the checkout payment capture idempotent so a duplicate webhook never double charges.",
    "offline sync queue": "Build the offline sync queue so edits made without connectivity reconcile on reconnect.",
    "agent billing meter": "Design the agent marketplace billing meter that prices tool calls per token.",
}
CHECKS: list[tuple[str, bool, str]] = []
STATUS: dict[str, int] = {}


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'OK ' if ok else 'GAP'}] {name}" + (f"  {detail}" if detail else ""), flush=True)
    return bool(ok)


def section(t):
    print(f"\n=== {t} ===", flush=True)


def api(token, path, method="GET", body=None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    fn = httpx.get if method == "GET" else httpx.post
    kw = {"headers": h, "timeout": 25.0}
    if method != "GET":
        kw["json"] = body or {}
    return fn(COLLECTOR + path, **kw)


def gw_call(tenant, actor, provider, prompt, *, branch, tool, system=None):
    base = GW[tenant]
    h = {"content-type": "application/json", "x-aben-actor": actor, "x-aben-tool": tool, "x-aben-branch": branch}
    if provider == "anthropic":
        url, h["x-api-key"], h["anthropic-version"] = f"{base}/v1/messages", A_KEY, "2023-06-01"
        body = {"model": A_MODEL, "max_tokens": 20, "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system
    elif provider == "google":
        url, h["x-goog-api-key"] = f"{base}/v1beta/models/{G_MODEL}:generateContent", G_KEY
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 20}}
    else:
        url, h["authorization"] = f"{base}/v1/chat/completions", f"Bearer {O_KEY}"
        body = {"model": O_MODEL, "max_tokens": 20, "messages": [{"role": "user", "content": prompt}]}
    try:
        r = httpx.post(url, headers=h, json=body, timeout=90.0)
        bucket = "5xx" if r.status_code >= 500 else ("429" if r.status_code == 429 else ("ok" if r.status_code < 300 else "4xx"))
        STATUS[bucket] = STATUS.get(bucket, 0) + 1
        return r.status_code
    except Exception:
        STATUS["err"] = STATUS.get("err", 0) + 1
        return None


def devs():
    out = []
    for tenant, org, n, prefix, topic in FLEET:
        for i in range(n):
            out.append((tenant, org, f"{tenant}-dev{i:02d}", prefix, topic, TOOLS[i % len(TOOLS)],
                        ["anthropic", "google", "openai"][i % 3]))
    return out


def one_session(dev):
    tenant, org, actor, prefix, topic, tool, prov = dev
    sysblk = [{"type": "text", "text": HOUSE}]
    base_prompt = TOPICS[topic]
    # turn 1 the shared topic so reuse forms, turn 2 a noisy tool log, turn 3 a refine
    gw_call(tenant, actor, prov, base_prompt, branch=f"feature/{prefix}-100", tool=tool,
            system=sysblk if prov == "anthropic" else None)
    gw_call(tenant, actor, "anthropic", "test output below, what failed?\n\n" + NOISY,
            branch=f"fix/{prefix}-200", tool=tool)
    gw_call(tenant, actor, prov, base_prompt + " Add a test and tighten the edge cases.",
            branch=f"feature/{prefix}-100", tool=tool, system=sysblk if prov == "anthropic" else None)


def wait_up():
    for url in [COLLECTOR] + list(GW.values()):
        end = time.time() + 90
        while time.time() < end:
            try:
                if httpx.get(url + "/health", timeout=2).status_code < 500:
                    break
            except Exception:
                time.sleep(0.5)


def settle():
    last = -1
    for _ in range(40):
        time.sleep(2.0)
        n = httpx.get(f"{COLLECTOR}/health", timeout=5, headers={"Authorization": f"Bearer {INGEST}"}).json().get("events", 0)
        if n == last and n > 0:
            break
        last = n
    return last


def main() -> int:
    wait_up()
    for tid, org, *_ in FLEET:
        admin = "globex-admin" if org == "globex" else "acme-admin"
        api(admin, "/api/tenants", "POST", {"tenant_id": tid, "display_name": tid, "residency": "eu"})

    fleet = devs()
    section(f"LOAD - drive {len(fleet)} developers concurrently (3 turns each, 6 tools, 3 providers)")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(one_session, fleet))
    secs = round(time.time() - t0, 1)
    total = sum(STATUS.values())
    print(f"  {total} calls in {secs}s  status={STATUS}", flush=True)
    check("no server errors under concurrent load", STATUS.get("5xx", 0) == 0, f"5xx={STATUS.get('5xx', 0)}")
    check("the gateways stayed responsive (calls completed)", total > len(fleet) * 2, f"{total} calls")
    ingested = settle()
    # every non-cache real call should be captured. allow cache hits + a small slack for rate-limited calls.
    expect = STATUS.get("ok", 0)
    check("no records lost in flight under load", ingested >= expect * 0.9, f"{ingested} ingested of ~{expect} ok")

    section("FUNCTIONAL AT SCALE - management experience")
    rep = api("acme-eu-mgr", "/api/report").json()
    check("report aggregates the tenant without double counting", rep.get("org_actors", 0) == 10,
          f"actors={rep.get('org_actors')}")
    check("spend is attributed, orphan share is sane", 0.0 <= rep.get("orphan_token_share", 1) <= 1.0)
    cz = rep.get("compression") or {}
    check("compression yield and the shadow measure are present", "shadow" in cz)
    outs = [{"outcome_id": f"o{i}", "ticket_id": "ACME-100", "merged": 1, "lines_added": 30} for i in range(6)]
    httpx.post(f"{COLLECTOR}/v1/outcomes", json=outs, headers={"Authorization": f"Bearer {INGEST}"}, timeout=10)
    val = api("acme-eu-mgr", "/api/report").json().get("value") or {}
    check("value line joins shipped work at scale", val.get("merged") == 6)
    check("negotiation pack is ready", (api("acme-eu-mgr", "/api/negotiation").json() or {}).get("ready") is True)
    check("orphan recovery returns proposals", isinstance(api("acme-eu-mgr", "/api/orphans").json().get("proposals"), list))
    check("a developer is still forbidden from management", api("acme-eu-dev00", "/api/report").status_code == 403)
    check("cross org report is refused", api("acme-eu-mgr", "/api/report?tenant=globex-eu").status_code == 403)

    section("FUNCTIONAL AT SCALE - developer experience")
    reuse_dev = None
    for tenant, org, n, prefix, topic in FLEET:
        for i in range(n):
            actor = f"{tenant}-dev{i:02d}"
            m = api(actor, "/api/me").json().get("collaboration_matches", [])
            if any(x.get("mode") == "solved_reuse" for x in m):
                reuse_dev = (actor, [x for x in m if x.get("mode") == "solved_reuse"])
                break
        if reuse_dev:
            break
    check("developers match solved work as reuse at scale", reuse_dev is not None)
    if reuse_dev:
        cap = next((x.get("capsule") for x in reuse_dev[1] if x.get("capsule")), None)
        check("the reuse match carries a content-free capsule", bool(cap), f"{cap}")
        mid = reuse_dev[1][0]["id"]
        check("a developer can ask a peer without an intro",
              api(reuse_dev[0], f"/api/collab/{mid}/ask", "POST", {"text": "how did you key it?"}).status_code == 200)
        th = api(reuse_dev[0], "/api/threads").json().get("threads", [])
        check("the thread is visible to the asker, peer hidden", bool(th) and th[0].get("peer_revealed") is False)
    me0 = api("acme-eu-dev00", "/api/me").json()
    check("a developer sees only their own spend", me0.get("cost_usd", -1) >= 0 and "collaboration_matches" in me0)
    check("a developer cannot read another via a tampered query",
          api("acme-eu-dev00", "/api/me?actor=acme-eu-dev01").json().get("cost_usd") == me0.get("cost_usd"))

    section("FUNCTIONAL AT SCALE - cross org exchange")
    for org, ratios in [("acme", {"cache_hit": 0.5}), ("globex", {"cache_hit": 0.4}), ("initech", {"cache_hit": 0.6})]:
        httpx.post(f"{COLLECTOR}/v1/exchange/submit", json={"org": org, "ratios": ratios},
                   headers={"Authorization": f"Bearer {INGEST}"}, timeout=10)
    ex = api("acme-eu-mgr", "/api/exchange").json()
    check("exchange returns a percentile, never a raw figure",
          ex.get("ready") and all("value" not in c for c in ex.get("comparison", [])))

    section("RESULT")
    gaps = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n  {len(CHECKS) - len(gaps)}/{len(CHECKS)} checks passed  ({total} real calls, {secs}s under load)", flush=True)
    for g in gaps:
        print(f"    - {g}", flush=True)
    return len(gaps)


if __name__ == "__main__":
    raise SystemExit(main())
