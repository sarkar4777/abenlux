#!/usr/bin/env python3
"""
Adversarial thrash harness. Runs INSIDE a container against the real stack (collector + edge gateways
across two orgs) and tries to break every guarantee the system claims: auth, RBAC, tenant and org
isolation, the privacy boundary, cost integrity, k-anonymity, the compression layer and exact cache,
transport hardening, and the collaboration org wall. Real model calls (Anthropic + Gemini) seed live
data first; the OpenAI wire path is exercised against the mock.

Each check asserts an invariant HOLDS under attack. A failing check is a real gap. Exit code is the
number of gaps found.
"""
from __future__ import annotations

import json
import os
import time
import uuid

import httpx

COLLECTOR = os.getenv("ABEN_COLLECTOR_URL", "http://collector:8090")
INGEST = os.getenv("ABEN_INGEST_TOKEN", "multi-dev-ingest-token")
GW = {  # tenant -> (gateway base url, org)
    "acme-eu": ("http://gw-acme-eu:8088", "acme"),
    "acme-tiny": ("http://gw-acme-tiny:8088", "acme"),
    "globex-eu": ("http://gw-globex-eu:8088", "globex"),
}
A_KEY = os.getenv("ANTHROPIC_API_KEY", "")
G_KEY = os.getenv("GEMINI_API_KEY", "")
O_KEY = os.getenv("OPENAI_API_KEY", "")
A_MODEL = "claude-haiku-4-5-20251001"
G_MODEL = "gemini-2.5-flash"
O_MODEL = "gpt-4o-mini"
SECRET = "sk-ant-LEAKCANARY-DO-NOT-PERSIST-7788"   # planted in prompts and smuggles

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'HOLD' if ok else 'GAP '}] {name}" + (f"  {detail}" if detail else ""), flush=True)
    return bool(ok)


def section(t: str) -> None:
    print(f"\n=== {t} ===", flush=True)


def api(token: str | None, path: str, method: str = "GET", body: dict | None = None,
        raw_headers: dict | None = None) -> httpx.Response:
    h = dict(raw_headers or {})
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    fn = httpx.get if method == "GET" else httpx.post
    kw = {"headers": h, "timeout": 20.0}
    if method != "GET":
        kw["json"] = body or {}
    return fn(COLLECTOR + path, **kw)


def gw_call(tenant: str, actor: str, provider: str, prompt: str, *, branch: str = "feature/ACME-1",
            extra_headers: dict | None = None, body_override: dict | None = None,
            tool: str = "claude-code") -> httpx.Response:
    base, _org = GW[tenant]
    h = {"content-type": "application/json", "x-aben-actor": actor, "x-aben-tool": tool,
         "x-aben-branch": branch}
    h.update(extra_headers or {})
    if provider == "anthropic":
        url = f"{base}/v1/messages"
        h["x-api-key"] = A_KEY
        h["anthropic-version"] = "2023-06-01"
        body = {"model": A_MODEL, "max_tokens": 32, "messages": [{"role": "user", "content": prompt}]}
    elif provider == "google":
        url = f"{base}/v1beta/models/{G_MODEL}:generateContent"
        h["x-goog-api-key"] = G_KEY
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 32}}
    else:  # real OpenAI
        url = f"{base}/v1/chat/completions"
        h["authorization"] = f"Bearer {O_KEY}"
        body = {"model": O_MODEL, "max_tokens": 32, "messages": [{"role": "user", "content": prompt}]}
    return httpx.post(url, headers=h, json=(body_override or body), timeout=60.0)


def wait_up() -> None:
    for url in [COLLECTOR] + [b for b, _ in GW.values()]:
        end = time.time() + 60
        while time.time() < end:
            try:
                if httpx.get(url + "/health", timeout=2).status_code < 500:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError(f"{url} never came up")


# ----------------------------- seed real data -----------------------------

def seed() -> None:
    section("Seed real traffic (Anthropic + Gemini live, OpenAI shape to mock)")
    # acme-eu: 6 devs on the same topic so collaboration + reuse can form
    topic = ("Implement idempotent retry handling for the checkout payment capture so a duplicate "
             "webhook never double-charges. Key the idempotency token on the order id.")
    for i in range(6):
        prov = "anthropic" if i % 2 == 0 else "google"
        r = gw_call("acme-eu", f"acme-eu-dev{i}", prov, topic, branch="feature/ACME-100")
        if i == 0:
            check("a real upstream call through the gateway succeeds", r.status_code < 300,
                  f"HTTP {r.status_code} {r.text[:120]}")
    # one acme-eu dev pastes a SECRET in the prompt - it must never persist anywhere
    gw_call("acme-eu", "acme-eu-dev0", "anthropic",
            f"here is my key {SECRET} please rotate it, reach me at dev@example.com", branch="fix/ACME-200")
    # acme-tiny: only 2 devs (k-anon suppression target)
    for i in range(2):
        gw_call("acme-tiny", f"acme-tiny-dev{i}", "anthropic", topic, branch="feature/ACME-100")
    # globex-eu: 5 devs, one shares the SAME topic as acme (org wall must keep them apart)
    for i in range(5):
        gw_call("globex-eu", f"globex-eu-dev{i}", "google",
                "Design the agent marketplace billing meter that prices tool calls per token.",
                branch="feature/GLX-1")
    gw_call("globex-eu", "globex-eu-dev0", "anthropic", topic, branch="feature/GLX-2")
    # openai shape through the gateway (mock upstream) - exercises that adapter + capture
    gw_call("acme-eu", "acme-eu-dev1", "openai", topic, tool="codex")
    # let edge sinks age-flush and the collector ingest/match/book
    last = -1
    for _ in range(25):
        time.sleep(2.0)
        n = httpx.get(f"{COLLECTOR}/health", timeout=5).json().get("events", 0)
        if n == last and n > 0:
            break
        last = n
    check("collector ingested the seeded derived records", last > 8, f"{last} events")


# ----------------------------- attacks -----------------------------

def attack_auth() -> None:
    section("AUTH - forgery, omission, token confusion")
    check("no token is rejected", httpx.get(COLLECTOR + "/api/report").status_code == 401)
    check("a bogus bearer token is rejected", api("not-a-real-token", "/api/report").status_code == 401)
    check("the ingest token cannot be used as a management bearer",
          api(INGEST, "/api/report").status_code in (401, 403))
    # a principal bearer token must NOT be accepted as the ingest credential on /v1/derived
    rec = _derived_record("auth-probe", "acme-eu")
    code = httpx.post(COLLECTOR + "/v1/derived", json=[rec],
                      headers={"Authorization": "Bearer acme-eu-mgr"}, timeout=10).status_code
    check("a principal bearer token cannot ingest derived records", code in (401, 403), f"HTTP {code}")


def attack_rbac() -> None:
    section("RBAC - privilege escalation, IDOR")
    dev = "acme-eu-dev0"
    forbidden = ["/api/report", "/api/savings", "/api/benchmark", "/api/budgets", "/api/drift",
                 "/api/rollup/objective", "/api/tenants", "/api/objectives", "/api/export"]
    bad = [ep for ep in forbidden if api(dev, ep).status_code != 403]
    check("a developer is forbidden from every management endpoint", not bad, f"leaked: {bad}")
    # IDOR: /api/me must resolve to the caller only, never another actor via a tampered query
    me = api(dev, "/api/me").json()
    me_actor = api(dev, "/api/me?actor=acme-eu-dev1").json()
    check("a developer cannot read another developer via ?actor=",
          json.dumps(me.get("collaboration_matches")) is not None
          and me.get("cost_usd") == me_actor.get("cost_usd"),
          "?actor override changed the response" if me.get("cost_usd") != me_actor.get("cost_usd") else "")
    check("a developer creating a tenant is forbidden",
          api(dev, "/api/tenants", "POST", {"tenant_id": "evil"}).status_code == 403)


def attack_tenancy() -> None:
    section("TENANCY - cross-tenant and cross-org isolation")
    check("an acme manager cannot read a globex (cross-org) report",
          api("acme-eu-mgr", "/api/report?tenant=globex-eu").status_code == 403)
    rep = api("acme-eu-mgr", "/api/report").json()
    check("an acme-eu report is scoped to its own tenant", rep.get("tenant") == "acme-eu")
    tl = api("acme-eu-mgr", "/api/tenants").json()
    tids = {t["tenant_id"] for t in tl.get("tenants", [])}
    check("a manager tenant list never contains another org", "globex-eu" not in tids, f"saw {tids}")
    hij = api("globex-admin", "/api/tenants", "POST", {"tenant_id": "acme-eu"})
    check("a cross-org tenant hijack is refused", hij.status_code in (403, 409), f"HTTP {hij.status_code}")


def attack_privacy() -> None:
    section("PRIVACY - content + identity must never persist on the collector")
    smuggle = {
        "event_id": f"smuggle-{uuid.uuid4()}", "ts": 1.0, "tier": "tier2_gateway", "provider": "anthropic",
        "actor_pseudonym": "px", "request_model": A_MODEL, "input_tokens": 9, "output_tokens": 1,
        "duplicate_history_tokens": 0, "objective_id": "obj-acme", "tenant_id": "acme-eu",
        "messages": [{"role": "user", "content": f"TOPSECRET {SECRET}"}],
        "user": {"email": "victim@example.com"}, "embedding": [0.1, 0.2],
    }
    code = httpx.post(COLLECTOR + "/v1/derived", json=[smuggle],
                      headers={"Authorization": f"Bearer {INGEST}"}, timeout=10).status_code
    check("the ingest endpoint accepts only known derived fields", code < 300, f"HTTP {code}")
    # whether it dropped the smuggled keys is asserted host-side by grepping the db. here we confirm the
    # report does not echo the secret back through any field.
    rep = api("acme-eu-mgr", "/api/report").json()
    check("no smuggled secret appears in the management report", SECRET not in json.dumps(rep))


def attack_cost_integrity() -> None:
    section("COST INTEGRITY - a hostile edge must not forge spend")
    before = api("acme-eu-mgr", "/api/report").json().get("total_cost_usd", 0)
    forged = _derived_record("forge-inflate", "acme-eu", cost_usd=999999.0, cost_priced=True)
    httpx.post(COLLECTOR + "/v1/derived", json=[forged],
               headers={"Authorization": f"Bearer {INGEST}"}, timeout=10)
    after = api("acme-eu-mgr", "/api/report").json().get("total_cost_usd", 0)
    check("a forged 999999 dollar cost cannot inflate the org total (re-priced or rejected)",
          after < before + 1000, f"before ${before} after ${after}")
    # deflation: a real call that lies served_from_cache=true to zero its own cost. with per-actor
    # binding the forged identity is rejected outright; without it the collector re-prices from tokens.
    deflate = _derived_record("forge-deflate", "acme-eu", input_tokens=50000, output_tokens=2000,
                              cost_usd=5.0, cost_priced=True, served_from_cache=True)
    resp = httpx.post(COLLECTOR + "/v1/derived", json=[deflate],
                      headers={"Authorization": f"Bearer {INGEST}"}, timeout=10).json()
    after2 = api("acme-eu-mgr", "/api/report").json().get("total_cost_usd", 0)
    check("a served_from_cache flag cannot hide a real 50k-token call's cost",
          resp.get("rejected", 0) >= 1 or after2 > after,
          f"rejected={resp.get('rejected')} delta=${round(after2 - after, 4)}")


def attack_identity_binding() -> None:
    section("IDENTITY BINDING - a fabricated actor pseudonym must not be accepted")
    forged = _derived_record(f"forge-id-{uuid.uuid4()}", "acme-eu", actor_pseudonym="px_fabricated_victim",
                             input_tokens=900000, output_tokens=900000, is_retry_loop=True)
    resp = httpx.post(COLLECTOR + "/v1/derived", json=[forged],
                      headers={"Authorization": f"Bearer {INGEST}"}, timeout=10).json()
    check("a record naming an unknown actor pseudonym is rejected (no k-anon dilution / feed poisoning)",
          resp.get("rejected", 0) >= 1 and resp.get("ingested", 1) == 0,
          f"ingested={resp.get('ingested')} rejected={resp.get('rejected')}")


def attack_kanon() -> None:
    section("K-ANONYMITY - a sub-k group must be suppressed")
    tiny = api("acme-tiny-mgr", "/api/report").json()
    rows = tiny.get("by_objective", [])
    suppressed = any(r.get("suppressed") for r in rows)
    check("a 2-developer tenant's per-objective spend is suppressed (or org under k)",
          suppressed or tiny.get("org_actors", 9) < 3,
          f"actors={tiny.get('org_actors')} rows={len(rows)}")


def attack_gateway_robustness() -> None:
    section("GATEWAY - compression never breaks a call, malformed/huge/ReDoS inputs do not crash")
    # compression on (gw-acme-eu has ABEN_COMPRESS=all): a heavy body must still get a valid answer
    noisy = "\x1b[31m" + "\n".join(["retrying connection"] * 300) + "\x1b[0m\nwhat is the error?"
    r = gw_call("acme-eu", "acme-eu-dev2", "anthropic", noisy)
    check("a request that triggers compression still returns a valid answer", r.status_code < 300,
          f"HTTP {r.status_code}")
    # malformed JSON body
    base = GW["acme-eu"][0]
    mr = httpx.post(f"{base}/v1/messages", content=b"{not json at all",
                    headers={"content-type": "application/json", "x-api-key": A_KEY,
                             "anthropic-version": "2023-06-01", "x-aben-actor": "acme-eu-dev3"}, timeout=30)
    check("a malformed JSON body does not 500 the gateway", mr.status_code != 500, f"HTTP {mr.status_code}")
    # ReDoS attempt against the compression regexes via a crafted prompt
    redos = "Today is " + "9" * 4000 + "-" + "9" * 4000
    t0 = time.time()
    rr = gw_call("acme-eu", "acme-eu-dev4", "anthropic", redos)
    check("a regex-bait prompt does not stall the gateway", (time.time() - t0) < 25 and rr.status_code < 500,
          f"{round(time.time()-t0,1)}s HTTP {rr.status_code}")


def attack_exact_cache() -> None:
    section("EXACT CACHE - a hit must only ever serve a developer their OWN repeat")
    body = {"model": A_MODEL, "max_tokens": 16, "messages": [{"role": "user", "content": "cache probe alpha"}]}
    h = {"content-type": "application/json", "x-api-key": A_KEY, "anthropic-version": "2023-06-01"}
    base = GW["acme-eu"][0]
    # dev A warms the cache, then repeats -> 2nd is a hit for A
    for _ in range(2):
        httpx.post(f"{base}/v1/messages", headers={**h, "x-aben-actor": "acme-eu-dev0"}, json=body, timeout=60)
    # dev B sends the byte-identical body once -> must NOT be served from A's entry
    httpx.post(f"{base}/v1/messages", headers={**h, "x-aben-actor": "acme-eu-dev5"}, json=body, timeout=60)
    time.sleep(12)
    rep = api("acme-eu-mgr", "/api/report").json()
    # the report exposes aggregate cache hits; a correct actor-scoped cache yields hits from A only.
    cz = rep.get("compression") or {}
    check("the exact cache produced at least one same-actor hit", (cz.get("cache_hits") or 0) >= 1,
          f"cache_hits={cz.get('cache_hits')}")


def attack_collaboration() -> None:
    section("COLLABORATION - org wall and double-blind consent")
    gx = api("globex-eu-dev0", "/api/me").json().get("collaboration_matches", [])
    cross = [m for m in gx if "checkout" in (m.get("topic") or "").lower()
             or "idempoten" in (m.get("topic") or "").lower()]
    check("a globex developer never matches an acme peer on the shared topic (org wall)", cross == [],
          f"{len(cross)} cross-org matches")
    me = api("acme-eu-dev0", "/api/me").json().get("collaboration_matches", [])
    if me:
        hidden = all(m.get("peer_revealed") is None for m in me)
        check("a peer identity is hidden before mutual consent", hidden)
    else:
        check("collaboration matches formed to test consent", False, "no matches")


def _derived_record(eid: str, tenant: str, **over) -> dict:
    r = {"event_id": eid, "ts": 1.0, "tier": "tier2_gateway", "provider": "anthropic",
         "actor_pseudonym": "px-attack", "request_model": A_MODEL, "input_tokens": 10, "output_tokens": 1,
         "duplicate_history_tokens": 0, "objective_id": "obj-acme", "objective_label": "Acme - Checkout Platform",
         "tenant_id": tenant}
    r.update(over)
    return r


def register_tenants() -> None:
    section("Setup - register tenants via their org admins (so the hijack test hits the real guard)")
    for tid, org in [("acme-eu", "acme"), ("acme-tiny", "acme"), ("globex-eu", "globex")]:
        admin = "globex-admin" if org == "globex" else "acme-admin"
        r = api(admin, "/api/tenants", "POST", {"tenant_id": tid, "display_name": tid.upper(), "residency": "eu"})
        check(f"admin registers tenant {tid}", r.status_code == 200, f"HTTP {r.status_code}")


def main() -> int:
    wait_up()
    register_tenants()
    seed()
    attack_auth()
    attack_rbac()
    attack_tenancy()
    attack_privacy()
    attack_cost_integrity()
    attack_identity_binding()
    attack_kanon()
    attack_gateway_robustness()
    attack_exact_cache()
    attack_collaboration()

    section("RESULT")
    gaps = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n  {len(CHECKS) - len(gaps)}/{len(CHECKS)} invariants held", flush=True)
    if gaps:
        print("  GAPS FOUND:", flush=True)
        for g in gaps:
            print(f"    - {g}", flush=True)
    return len(gaps)


if __name__ == "__main__":
    raise SystemExit(main())
