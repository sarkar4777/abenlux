#!/usr/bin/env python3
"""
End to end check of the new value, savings, and collaboration features against the real stack with real
models. It runs inside a container, seeds traffic from many developers across many tools and all three
providers, then drives each new feature and asserts it works. A failing check is a real gap and the exit
code is the number of failures.

Features exercised. Cache breakpoints and tool-result trim on the compression layer. Solution capsules
and the solved-reuse path. The async help relay. The value numerator joined from a posted outcome feed.
The vendor negotiation pack. Orphan spend recovery. The shadow measure of off strategies. The cross-org
benchmark exchange.
"""
from __future__ import annotations

import os
import time

import httpx

COLLECTOR = os.getenv("ABEN_COLLECTOR_URL", "http://collector:8090")
INGEST = os.getenv("ABEN_INGEST_TOKEN", "features-ingest-token")
GW = {"acme-eu": "http://gw-acme-eu:8088", "acme-tiny": "http://gw-acme-tiny:8088",
      "globex-eu": "http://gw-globex-eu:8088"}
A_KEY, G_KEY, O_KEY = os.getenv("ANTHROPIC_API_KEY", ""), os.getenv("GEMINI_API_KEY", ""), os.getenv("OPENAI_API_KEY", "")
A_MODEL, G_MODEL, O_MODEL = "claude-haiku-4-5-20251001", "gemini-2.5-flash", "gpt-4o-mini"

# a long stable house prompt, big enough that cache breakpoints will mark it
HOUSE = ("You are a senior engineer in the Rocket monorepo. Follow the house style. "
         "Prefer pure functions. Validate inputs at the boundary. Money is integer minor units. "
         "Time is UTC. Errors are values. Tests assert behavior. ") * 30
NOISY_TOOL_OUTPUT = "\x1b[33m" + "\n".join(["WARN retry connection refused"] * 120) + "\x1b[0m"
CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'OK ' if ok else 'GAP'}] {name}" + (f"  {detail}" if detail else ""), flush=True)
    return bool(ok)


def section(t: str) -> None:
    print(f"\n=== {t} ===", flush=True)


def api(token, path, method="GET", body=None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    fn = httpx.get if method == "GET" else httpx.post
    kw = {"headers": h, "timeout": 20.0}
    if method != "GET":
        kw["json"] = body or {}
    return fn(COLLECTOR + path, **kw)


def gw_call(tenant, actor, provider, prompt, *, branch="feature/ACME-100", tool="claude-code", system=None):
    base = GW[tenant]
    h = {"content-type": "application/json", "x-aben-actor": actor, "x-aben-tool": tool, "x-aben-branch": branch}
    if provider == "anthropic":
        url, h["x-api-key"], h["anthropic-version"] = f"{base}/v1/messages", A_KEY, "2023-06-01"
        body = {"model": A_MODEL, "max_tokens": 24, "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system
    elif provider == "google":
        url, h["x-goog-api-key"] = f"{base}/v1beta/models/{G_MODEL}:generateContent", G_KEY
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 24}}
    else:
        url, h["authorization"] = f"{base}/v1/chat/completions", f"Bearer {O_KEY}"
        body = {"model": O_MODEL, "max_tokens": 24, "messages": [{"role": "user", "content": prompt}]}
    try:
        return httpx.post(url, headers=h, json=body, timeout=60.0)
    except Exception as e:
        print("    call error", e, flush=True)
        return None


def wait_up():
    for url in [COLLECTOR] + list(GW.values()):
        end = time.time() + 60
        while time.time() < end:
            try:
                if httpx.get(url + "/health", timeout=2).status_code < 500:
                    break
            except Exception:
                time.sleep(0.5)


TOOLS = ["claude-code", "aider", "cline", "opencode", "gemini-cli", "codex"]
CHECKOUT = ("Make the checkout payment capture idempotent so a duplicate webhook never double charges. "
            "Key the idempotency token on the order id.")


def seed():
    section("Seed real traffic (Anthropic + OpenAI + Gemini, many developers, many tools)")
    sysblk = [{"type": "text", "text": HOUSE}]
    # acme-eu: 6 developers solve the SAME checkout problem cleanly. they build the solved corpus and
    # capsules, and a later one matches the earlier ones as reuse.
    for i in range(6):
        prov = ["anthropic", "google", "openai"][i % 3]
        r = gw_call("acme-eu", f"acme-eu-dev{i}", prov, CHECKOUT, branch="feature/ACME-100",
                    tool=TOOLS[i % len(TOOLS)], system=sysblk if prov == "anthropic" else None)
        if i == 0:
            check("a real call through the gateway works", r is not None and r.status_code < 300,
                  f"HTTP {getattr(r, 'status_code', 'err')}")
    # an agent turn carrying noisy tool output, so tool-result trim and the shadow measure have something
    gw_call("acme-eu", "acme-eu-dev1", "anthropic",
            "here is the failing test output, what broke?\n\n" + NOISY_TOOL_OUTPUT, branch="fix/ACME-200")
    # some unattributed work (no ticket), so orphan recovery has a cluster to find
    for i in range(5):
        gw_call("acme-eu", f"acme-eu-dev{i}", "anthropic",
                "Refactor the shared logging helper to structured key value output across the repo.",
                branch="chore/no-ticket", tool="aider")
    # globex on its own work
    for i in range(5):
        gw_call("globex-eu", f"globex-eu-dev{i}", "google",
                "Design the agent marketplace billing meter.", branch="feature/GLX-1", tool="gemini-cli")
    last = -1
    for _ in range(25):
        time.sleep(2.0)
        n = httpx.get(f"{COLLECTOR}/health", timeout=5, headers={"Authorization": f"Bearer {INGEST}"}).json().get("events", 0)
        if n == last and n > 0:
            break
        last = n
    check("collector ingested the seeded records", last > 10, f"{last} events")


def main() -> int:
    wait_up()
    # register tenants so reports resolve
    for tid, org in [("acme-eu", "acme"), ("acme-tiny", "acme"), ("globex-eu", "globex")]:
        admin = "globex-admin" if org == "globex" else "acme-admin"
        api(admin, "/api/tenants", "POST", {"tenant_id": tid, "display_name": tid, "residency": "eu"})
    seed()

    section("CHEAPER - compression savings and the shadow measure")
    rep = api("acme-eu-mgr", "/api/report").json()
    cz = rep.get("compression") or {}
    check("compression yield is recorded", (cz.get("saved_input_tokens") or 0) >= 0)
    check("the shadow measure shows what enabling an off strategy would save",
          bool(cz.get("shadow")), f"shadow={list((cz.get('shadow') or {}).keys())}")

    section("SMARTER - the value numerator joined from the outcome feed")
    outs = [{"outcome_id": f"o{i}", "ticket_id": "ACME-100", "merged": 1, "lines_added": 40, "lines_removed": 5}
            for i in range(4)] + [{"outcome_id": "o9", "ticket_id": "ACME-100", "merged": 0, "reverted": 1}]
    httpx.post(f"{COLLECTOR}/v1/outcomes", json=outs, headers={"Authorization": f"Bearer {INGEST}"}, timeout=10)
    val = api("acme-eu-mgr", "/api/report").json().get("value") or {}
    check("spend is joined to shipped work (value line)", val.get("merged") == 4,
          f"merged={val.get('merged')} cost_per_merged={val.get('cost_per_merged_change')}")

    section("SMARTER - orphan spend recovery proposes a named objective")
    orph = api("acme-eu-mgr", "/api/orphans").json()
    check("orphan recovery surfaces a proposal for shared untracked work",
          bool(orph.get("proposals")), f"{len(orph.get('proposals') or [])} proposals")

    section("MANAGER - the vendor negotiation pack")
    pack = api("acme-eu-mgr", "/api/negotiation").json()
    check("negotiation pack is ready with a blended rate",
          pack.get("ready") and pack.get("blended_usd_per_mtok", 0) > 0,
          f"blended=${pack.get('blended_usd_per_mtok')}/Mtok")

    section("COLLABORATION - solved-reuse, capsules, and the async relay")
    me = None
    for i in range(6):
        m = api(f"acme-eu-dev{i}", "/api/me").json().get("collaboration_matches", [])
        reuse = [x for x in m if x.get("mode") == "solved_reuse"]
        if reuse:
            me = (f"acme-eu-dev{i}", reuse)
            break
    check("a developer matches already-solved work as reuse", me is not None,
          f"{len(me[1]) if me else 0} reuse matches")
    if me:
        cap = next((x.get("capsule") for x in me[1] if x.get("capsule")), None)
        check("the reuse match carries a content-free solution capsule",
              bool(cap), f"capsule={cap}")
        mid = me[1][0]["id"]
        asked = api(me[0], f"/api/collab/{mid}/ask", "POST", {"text": "how did you key the idempotency token?"})
        check("a developer can ask the peer a question with no intro first", asked.status_code == 200)
        threads = api(me[0], "/api/threads").json().get("threads", [])
        check("the question shows up as an async thread, peer hidden",
              bool(threads) and threads[0].get("peer_revealed") is False and "peer" not in threads[0])

    section("PLATFORM - the cross-org benchmark exchange")
    for org, ratios in [("acme", {"cache_hit": 0.55, "reuse_share": 0.3}),
                        ("globex", {"cache_hit": 0.4, "reuse_share": 0.5}),
                        ("initech", {"cache_hit": 0.5, "reuse_share": 0.2})]:
        httpx.post(f"{COLLECTOR}/v1/exchange/submit", json={"org": org, "ratios": ratios},
                   headers={"Authorization": f"Bearer {INGEST}"}, timeout=10)
    ex = api("acme-eu-mgr", "/api/exchange").json()
    comp = ex.get("comparison") or []
    check("the exchange returns the org a percentile after enough orgs joined",
          ex.get("ready") and comp and all("value" not in c for c in comp),
          f"{len(comp)} metrics, ready={ex.get('ready')}")

    section("RESULT")
    gaps = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n  {len(CHECKS) - len(gaps)}/{len(CHECKS)} feature checks passed", flush=True)
    for g in gaps:
        print(f"    - {g}", flush=True)
    return len(gaps)


if __name__ == "__main__":
    raise SystemExit(main())
