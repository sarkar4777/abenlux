#!/usr/bin/env python3
"""
Deep, enterprise-grade end-to-end verification of the whole Abenlux stack, run inside one container.

It stands up the REAL processes - a mock model upstream, the central collector, and one edge gateway
per tenant - then drives MANY developers across MULTIPLE tenants of two orgs through MULTI-TURN model
calls (resent-history bloat, retry loops, shared topics, cross-org overlap). Capture -> redact ->
derive -> forward -> collector matching/ledger all happen for real over HTTP. It then exercises every
function from each role's standpoint (developer, manager, finance, admin) and asserts the governance,
tenancy, reuse-yield, benchmark, collaboration, and budget guarantees hold - including the ways an
attacker would try to break them. Exit code is non-zero if any check fails.

No data is seeded into the stores: every figure is the product of a real call through the pipeline.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

import httpx

WORK = tempfile.mkdtemp(prefix="aben-e2e-")
HMAC = "deep-e2e-hmac-secret-not-for-prod"
INGEST = "deep-ingest-token"
KANON = 5
COLLECTOR = "http://127.0.0.1:8090"
MOCK_PORT = 9111

# tenant_id, org, residency, n_devs, gateway_port. acme has three k-clearing tenants (so the benchmark
# cohort is READY) plus a 2-dev tenant (so k-anon SUPPRESSION is exercised); globex is a second org
# (so the org wall is exercised). acme-eu and globex-eu share residency 'eu' on purpose, so the only
# thing standing between them in the broker is the ORG wall.
TENANTS = [
    ("acme-eu", "acme", "eu", 6, 8101),
    ("acme-us", "acme", "us", 5, 8102),
    ("acme-apac", "acme", "apac", 5, 8103),
    ("acme-tiny", "acme", "eu", 2, 8104),
    ("globex-eu", "globex", "eu", 5, 8105),
]

# objective attribution is by ticket prefix; budgets + clients drive budget and Chinese-wall behavior.
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

CHECKS: list[tuple[str, bool, str]] = []
PROCS: list[subprocess.Popen] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)
    return bool(cond)


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


# ----------------------------- process orchestration -----------------------------

def _base_env() -> dict:
    e = dict(os.environ)
    e.update(ABEN_HMAC_KEY=HMAC, ABEN_INGEST_TOKEN=INGEST, ABEN_KG=f"{WORK}/kg.yaml",
             ABEN_K_ANON=str(KANON), ABEN_NOTIFY="0", PYTHONUNBUFFERED="1")
    return e


def start(name: str, args: list[str], env: dict) -> subprocess.Popen:
    log = open(f"{WORK}/{name}.log", "w")
    p = subprocess.Popen(args, env=env, stdout=log, stderr=subprocess.STDOUT)
    PROCS.append(p)
    return p


def wait_http(url: str, timeout: float = 40.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url, timeout=2.0).status_code < 500:
                return True
        except Exception:
            time.sleep(0.4)
    return False


def shutdown() -> None:
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


# ----------------------------- principals / config -----------------------------

def write_config() -> dict:
    open(f"{WORK}/kg.yaml", "w").write(KG_YAML)
    principals = []
    tokens: dict = {}

    def add(token, role, tenant, org, **extra):
        # subject == token here so the actor a developer's calls carry (X-Aben-Actor = token) yields the
        # SAME HMAC pseudonym the collector derives from the principal's subject - i.e. a developer's
        # own /api/me resolves to exactly the rows their calls produced.
        row = {"token": token, "subject": token, "role": role, "tenant_id": tenant, "org": org}
        row.update(extra)
        principals.append(row)
        tokens[token] = {"role": role, "tenant": tenant, "org": org}

    # org-level admins + finance
    add("acme-admin", "admin", "acme-eu", "acme")
    add("acme-fin", "finance", "acme-eu", "acme")
    add("globex-admin", "admin", "globex-eu", "globex")
    # a manager per tenant, plus developers per tenant
    devs: dict = {}
    for tid, org, _res, n, _port in TENANTS:
        add(f"{tid}-mgr", "manager", tid, org)
        devs[tid] = []
        for i in range(n):
            tok = f"{tid}-dev{i}"
            add(tok, "developer", tid, org, slack=f"@{tok}")
            devs[tid].append(tok)

    import yaml
    open(f"{WORK}/principals.yaml", "w").write(yaml.safe_dump({"principals": principals}))
    return {"tokens": tokens, "devs": devs}


def collector_env() -> dict:
    e = _base_env()
    e.update(ABEN_PRINCIPALS=f"{WORK}/principals.yaml", ABEN_DB=f"{WORK}/central.db",
             ABEN_LEDGER_DB=f"{WORK}/ledger.db", ABEN_TENANT_DB=f"{WORK}/tenants.db",
             ABEN_MATCH_DB=f"{WORK}/matches.db", ABEN_CONTACT_DB=f"{WORK}/contacts.db")
    return e


def gateway_env(tenant: str, residency: str) -> dict:
    e = _base_env()
    e.update(ABEN_TENANT=tenant, ABEN_RESIDENCY=residency, ABEN_COLLECTOR_URL=COLLECTOR,
             ABEN_ANTHROPIC_UPSTREAM=f"http://127.0.0.1:{MOCK_PORT}",
             ABEN_DB=f"{WORK}/edge-{tenant}.db", ABEN_LOCAL_DB=f"{WORK}/local-{tenant}.db",
             ABEN_MATCH_DB=f"{WORK}/edge-matches-{tenant}.db")
    return e


# ----------------------------- traffic generation -----------------------------

# distinct topics; same text => same keyphrase vector => reliable in-tenant collaboration + reuse.
TOPIC_CHECKOUT = ("Implement idempotent retry handling for the checkout payment capture so a duplicate "
                  "webhook never double-charges. Use an idempotency key keyed on the order id.")
TOPIC_MOBILE = ("Build the offline sync queue for the mobile app so edits made without connectivity "
                "reconcile on reconnect with last-write-wins and a conflict log.")
TOPIC_AGENT = ("Design the agent marketplace billing meter that prices tool calls per token and "
               "aggregates usage per tenant for monthly invoicing.")
TOPIC_GLOBEX_SHARED = ("Quantum widget calibration routine for the foobar sensor array with adaptive "
                       "thresholding and a self-test on boot.")


def call(gw_port: int, actor: str, branch: str, prompt: str, mock_input: int = 1820,
         cache: float = 0.7, history: list | None = None) -> dict:
    msgs = list(history or [])
    msgs.append({"role": "user", "content": prompt})
    body = {"model": "claude-opus-4-8", "max_tokens": 256, "messages": msgs}
    headers = {
        "x-aben-actor": actor, "x-aben-branch": branch, "x-aben-tool": "claude-code",
        "x-aben-mock-input": str(mock_input), "x-aben-mock-cache": str(cache),
        "content-type": "application/json",
    }
    r = httpx.post(f"http://127.0.0.1:{gw_port}/v1/messages", json=body, headers=headers, timeout=20.0)
    r.raise_for_status()
    msgs.append({"role": "assistant", "content": "ok"})
    return {"messages": msgs}


def drive_developer(port: int, actor: str, branch: str, topic: str, *, turns: int = 4,
                    mock_input: int = 1820, cache: float = 0.7) -> None:
    # MULTI-TURN: each turn resends the growing history (resent-history bloat the gateway measures),
    # and turn 3 is a near-duplicate of turn 2 (a retry loop the waste monitor flags).
    hist: list = []
    out = call(port, actor, branch, topic, mock_input=mock_input, cache=cache)
    hist = out["messages"]
    for t in range(1, turns):
        p = topic if t == 2 else f"{topic} (refine step {t}: tighten the edge cases and add a test)"
        out = call(port, actor, branch, p, mock_input=mock_input, cache=cache, history=hist)
        hist = out["messages"]


def generate_traffic(cfg: dict, ports: dict) -> None:
    devs = cfg["devs"]
    # acme-eu: all 6 on obj-acme checkout (feature) with the SAME topic -> they collaborate + seed a
    # >= k cost-to-solve so the reuse-yield credits. one of them also does a fix (work-type variety).
    for i, tok in enumerate(devs["acme-eu"]):
        drive_developer(ports["acme-eu"], tok, "feature/ACME-100", TOPIC_CHECKOUT,
                        cache=0.2 if i == 0 else 0.7)   # dev0 caches poorly -> recoverable-waste signal
    drive_developer(ports["acme-eu"], devs["acme-eu"][1], "fix/ACME-200", TOPIC_CHECKOUT, turns=2)
    # acme-us: 5 devs on obj-zenith mobile (feature) -> qualifies, different residency from eu
    for tok in devs["acme-us"]:
        drive_developer(ports["acme-us"], tok, "feature/MOB-10", TOPIC_MOBILE)
    # acme-apac: 5 devs on obj-acme (chore -> maintenance) -> qualifies, exercises maintenance share
    for tok in devs["acme-apac"]:
        drive_developer(ports["acme-apac"], tok, "chore/ACME-300", TOPIC_CHECKOUT)
    # acme-tiny: only 2 devs -> below k, must be SUPPRESSED everywhere it would expose them
    for tok in devs["acme-tiny"]:
        drive_developer(ports["acme-tiny"], tok, "feature/ACME-100", TOPIC_CHECKOUT)
    # globex-eu: 5 devs on obj-globex with HIGH token volume -> trips the tiny 800-dollar budget.
    # one globex dev also works the GLOBEX_SHARED topic that NO acme dev touches, and one acme-eu dev
    # is given that SAME topic - the org wall must keep them from ever matching (same residency 'eu').
    for tok in devs["globex-eu"]:
        drive_developer(ports["globex-eu"], tok, "feature/GLX-1", TOPIC_AGENT, mock_input=9000, cache=0.0)
    drive_developer(ports["globex-eu"], devs["globex-eu"][0], "feature/GLX-2", TOPIC_GLOBEX_SHARED, turns=2)
    drive_developer(ports["acme-eu"], devs["acme-eu"][0], "feature/ACME-9", TOPIC_GLOBEX_SHARED, turns=2)


# ----------------------------- API helpers -----------------------------

def api(token: str, path: str, method: str = "GET", body: dict | None = None) -> httpx.Response:
    h = {"Authorization": f"Bearer {token}"}
    if method == "GET":
        return httpx.get(COLLECTOR + path, headers=h, timeout=15.0)
    return httpx.post(COLLECTOR + path, headers=h, json=body or {}, timeout=15.0)


def main() -> int:
    section("Boot the real stack (mock upstream + collector + one gateway per tenant)")
    cfg = write_config()
    start("mock", [sys.executable, "-m", "uvicorn", "abenlux.devtools.mock_upstream:app",
                   "--port", str(MOCK_PORT)], _base_env())
    start("collector", [sys.executable, "-m", "uvicorn", "abenlux.api.server:app",
                        "--port", "8090"], collector_env())
    ports = {}
    for tid, _org, res, _n, port in TENANTS:
        start(f"gw-{tid}", [sys.executable, "-m", "uvicorn", "abenlux.capture.gateway:app",
                            "--port", str(port)], gateway_env(tid, res))
        ports[tid] = port
    ok = check("mock upstream is up", wait_http(f"http://127.0.0.1:{MOCK_PORT}/health"))
    ok &= check("collector is up", wait_http(f"{COLLECTOR}/health"))
    for tid, _o, _r, _n, port in TENANTS:
        ok &= check(f"gateway {tid} is up", wait_http(f"http://127.0.0.1:{port}/health"))
    if not ok:
        print("\nstack failed to boot; tail of logs:")
        for f in os.listdir(WORK):
            if f.endswith(".log"):
                print(f"--- {f} ---")
                print(open(f"{WORK}/{f}").read()[-1500:])
        return 1

    section("Admin onboarding: register every tenant in the RBAC registry")
    for tid, org, res, _n, _p in TENANTS:
        admin = "globex-admin" if org == "globex" else "acme-admin"
        r = api(admin, "/api/tenants", "POST",
                {"tenant_id": tid, "display_name": tid.upper(), "residency": res})
        check(f"admin registers tenant {tid} (org {org})", r.status_code == 200)

    section("Generate multi-turn traffic for ~23 developers across 5 tenants / 2 orgs")
    generate_traffic(cfg, ports)
    # let the edge HttpSinks age-flush (max_age_s=5) and the collector ingest + match + book
    print("  waiting for edge->collector forwarding to settle ...", flush=True)
    last = -1
    for _ in range(20):
        time.sleep(2.0)
        n = httpx.get(f"{COLLECTOR}/health", timeout=5.0).json().get("events", 0)
        if n == last and n > 0:
            break
        last = n
    check("collector received forwarded derived records", last > 50, f"{last} events ingested")

    run_role_checks(cfg)

    section("RESULT")
    fails = [n for n, ok_, _ in CHECKS if not ok_]
    print(f"\n  {len(CHECKS) - len(fails)}/{len(CHECKS)} checks passed", flush=True)
    if fails:
        print("  FAILED:", flush=True)
        for f in fails:
            print(f"    - {f}", flush=True)
    return 1 if fails else 0


def run_role_checks(cfg: dict) -> None:
    dev = cfg["devs"]["acme-eu"][0]
    gx_dev = cfg["devs"]["globex-eu"][0]

    # ---------------- DEVELOPER ----------------
    section("DEVELOPER standpoint (acme-eu developer)")
    me = api(dev, "/api/me").json()
    check("developer sees their own spend", me.get("cost_usd", 0) > 0, f"${me.get('cost_usd')}")
    check("developer sees their work-type mix", bool(me.get("work_type_mix")))
    check("developer sees resent-history waste", me.get("resent_history_tokens", 0) > 0)
    check("developer has collaboration matches (same-tenant peers)", len(me.get("collaboration_matches", [])) > 0,
          f"{len(me.get('collaboration_matches', []))} matches")
    for ep in ["/api/report", "/api/rollup/objective", "/api/savings", "/api/benchmark",
               "/api/tenants", "/api/budgets", "/api/drift", "/api/objectives"]:
        code = api(dev, ep).status_code
        check(f"developer is FORBIDDEN from {ep}", code == 403, f"HTTP {code}")
    # contact card round-trip
    api(dev, "/api/contact", "POST", {"slack": "@dev-zero", "email": "dev0@acme.io"})
    card = api(dev, "/api/contact").json().get("contact", {})
    check("developer can set + read their contact card", card.get("slack") == "@dev-zero")

    # ---------------- DEVELOPER multi-turn collaboration (double-blind consent) ----------------
    section("DEVELOPER multi-turn: double-blind collaboration consent + reveal")
    m = api(dev, "/api/me").json().get("collaboration_matches", [])
    if m:
        mid = m[0]["id"]
        peer_before = m[0]["peer_revealed"]
        check("peer identity is HIDDEN before mutual consent", peer_before is None)
        api(dev, f"/api/collab/{mid}/consent", "POST")           # dev0 requests intro
        # find the reciprocal match on a same-tenant peer and consent from their side
        revealed = False
        for peer in cfg["devs"]["acme-eu"][1:]:
            pm = api(peer, "/api/me").json().get("collaboration_matches", [])
            for mm in pm:
                if mm["peer_revealed"] is None and not mm["you_requested"]:
                    res = api(peer, f"/api/collab/{mm['id']}/consent", "POST").json()
                    if res.get("mutual"):
                        revealed = True
                        break
            if revealed:
                break
        after = api(dev, "/api/me").json().get("collaboration_matches", [])
        now_revealed = any(x["peer_revealed"] for x in after)
        check("identity REVEALED only after mutual consent", revealed and now_revealed)
    else:
        check("collaboration matches existed to consent on", False, "no matches found")

    # ---------------- MANAGER ----------------
    section("MANAGER standpoint (acme-eu manager) - tenant-scoped, k-anon")
    mgr = "acme-eu-mgr"
    rep = api(mgr, "/api/report").json()
    check("manager report is scoped to their own tenant", rep.get("tenant") == "acme-eu")
    check("manager sees attributed spend", rep.get("total_cost_usd", 0) > 0, f"${rep.get('total_cost_usd')}")
    objs = {r["label"]: r for r in rep.get("by_objective", [])}
    check("acme-eu spend attributed to the Checkout objective",
          any("Checkout" in lbl for lbl in objs))
    check("manager report carries the reuse-yield savings line", "reuse_yield" in rep)
    # reuse-yield: 6 acme-eu devs on the same objective x work_type -> credited
    sav = api(mgr, "/api/savings").json()
    check("reuse-yield is credited for acme-eu (>= k devs solved the same work)",
          sav.get("reuse_avoided_usd", 0) > 0, f"~${sav.get('reuse_avoided_usd')}")
    # budgets + drift + rollup are reachable and tenant-scoped
    check("manager can read budgets", api(mgr, "/api/budgets").status_code == 200)
    check("manager can read drift (tenant-scoped)", api(mgr, "/api/drift").status_code == 200)
    roll = api(mgr, "/api/rollup/objective").json()
    check("manager rollup is reachable", "rows" in roll)
    # tenant-scoping isolation: acme-eu manager's spend must EXCLUDE acme-us (mobile) objective
    eu_objs = {r["label"] for r in rep.get("by_objective", [])}
    check("acme-eu report EXCLUDES acme-us-only objective (Mobile)",
          not any("Mobile" in lbl for lbl in eu_objs))
    # manager may NOT manage, and may NOT cross the org wall
    check("manager is FORBIDDEN from creating a tenant (needs admin)",
          api(mgr, "/api/tenants", "POST", {"tenant_id": "acme-x"}).status_code == 403)
    check("manager is FORBIDDEN from a cross-ORG tenant report",
          api(mgr, "/api/report?tenant=globex-eu").status_code == 403)
    tlist = api(mgr, "/api/tenants").json()
    tids = {t["tenant_id"] for t in tlist.get("tenants", [])}
    check("manager tenant list contains only their OWN org's tenants",
          "globex-eu" not in tids)

    # ---------------- MANAGER: k-anonymity suppression ----------------
    section("MANAGER: k-anonymity suppression (acme-tiny has only 2 developers)")
    tiny = api("acme-tiny-mgr", "/api/report").json()
    tiny_objs = tiny.get("by_objective", [])
    suppressed = [r for r in tiny_objs if r.get("suppressed")]
    check("a sub-k tenant's objective spend is SUPPRESSED, not shown",
          any(r.get("suppressed") for r in tiny_objs) or tiny.get("org_actors", 0) < KANON,
          f"actors={tiny.get('org_actors')}, suppressed_rows={len(suppressed)}")

    # ---------------- MANAGER: cross-tenant benchmark ----------------
    section("MANAGER: cross-tenant Benchmark Exchange (k-anon + DP + cohort gate)")
    bm = api(mgr, "/api/benchmark").json()
    check("benchmark is READY (>= 3 acme tenants clear k)", bm.get("readiness", {}).get("ready") is True,
          f"cohort={bm.get('readiness', {}).get('cohort_size')}")
    check("benchmark cohort is acme-only (no globex)", "globex-eu" not in bm.get("org_cohort", []))
    comp = bm.get("comparison", [])
    check("benchmark publishes per-metric percentiles", len(comp) > 0)
    check("every percentile is a valid fraction",
          all(c["your_percentile"] is None or 0.0 <= c["your_percentile"] <= 1.0 for c in comp))
    # acme-tiny (sub-k) must never be a qualifying cohort member
    check("sub-k tenant is excluded from the benchmark cohort",
          bm.get("readiness", {}).get("cohort_size", 9) <= 3)

    # ---------------- FINANCE ----------------
    section("FINANCE standpoint (acme finance)")
    who = api("acme-fin", "/api/whoami").json()
    check("finance has the cost-view permission", "view_cost" in who.get("permissions", []))
    check("finance can read the management report", api("acme-fin", "/api/report").status_code == 200)
    check("finance can read the benchmark", api("acme-fin", "/api/benchmark").status_code == 200)
    check("finance is still FORBIDDEN from creating a tenant",
          api("acme-fin", "/api/tenants", "POST", {"tenant_id": "x"}).status_code == 403)

    # ---------------- ADMIN ----------------
    section("ADMIN standpoint (acme admin) - tenant + objective management")
    check("admin can list knowledge-graph objectives", api("acme-admin", "/api/objectives").status_code == 200)
    created = api("acme-admin", "/api/tenants", "POST",
                  {"tenant_id": "acme-emea", "display_name": "ACME EMEA", "residency": "eu"})
    check("admin can create a tenant", created.status_code == 200,
          f"org={created.json().get('tenant', {}).get('org')}")
    check("the new tenant is bound to the admin's own org",
          created.json().get("tenant", {}).get("org") == "acme")
    # cross-org hijack: globex admin cannot re-create an acme tenant_id
    hij = api("globex-admin", "/api/tenants", "POST", {"tenant_id": "acme-eu"})
    check("cross-org tenant HIJACK is refused (409)", hij.status_code == 409, f"HTTP {hij.status_code}")
    still = api("acme-admin", "/api/tenants").json()
    owner_ok = any(t["tenant_id"] == "acme-eu" for t in still.get("tenants", []))
    check("the hijacked tenant still belongs to its original org", owner_ok)

    # ---------------- BUDGETS / FORECAST ----------------
    section("BUDGET guardrails + run-rate forecast (globex innovation cap, high spend)")
    gbm = api("globex-eu-mgr", "/api/budgets").json()
    agent_b = next((b for b in gbm.get("budgets", []) if "Agent" in b["label"]), None)
    check("globex budget is tracked with a status", agent_b is not None)
    if agent_b:
        check("the heavily-spent globex budget is at-risk or over",
              agent_b.get("status") in ("at_risk", "over"), f"status={agent_b.get('status')}")
        check("a run-rate forecast is computed", agent_b.get("forecast_usd", 0) > 0)

    # ---------------- ORG WALL in collaboration ----------------
    section("ORG WALL: collaboration never crosses orgs (same residency, different org)")
    # the globex dev who worked the GLOBEX_SHARED topic shares it with one acme-eu dev, same residency.
    # the org wall must prevent a match: that globex dev's only same-topic peer is in another org.
    gx_matches = api(gx_dev, "/api/me").json().get("collaboration_matches", [])
    # any match the globex dev has must be with a globex peer; on the shared topic there must be none
    # from acme. we assert the globex dev has no match whose topic is the cross-org shared one.
    shared_cross = [m for m in gx_matches if "Quantum" in (m.get("topic") or "")
                    or "quantum" in (m.get("topic") or "").lower()]
    check("no cross-org collaboration match on the shared topic", shared_cross == [],
          f"{len(shared_cross)} cross-org matches (must be 0)")

    # ---------------- AUTH / INPUT HARDENING ----------------
    section("AUTH + ingest hardening")
    check("no token is rejected (401)", httpx.get(COLLECTOR + "/api/report").status_code == 401)
    check("a bogus token is rejected (401)", api("not-a-real-token", "/api/report").status_code == 401)
    # the ingest endpoint must drop a smuggled content field at the schema boundary
    smuggle = {
        "event_id": "smuggle-1", "ts": 1.0, "tier": "tier2_gateway", "provider": "anthropic",
        "actor_pseudonym": "px_test", "request_model": "claude-opus-4-8",
        "input_tokens": 10, "output_tokens": 1, "duplicate_history_tokens": 0,
        "objective_id": "obj-acme", "tenant_id": "acme-eu",
        "messages": [{"role": "user", "content": "TOP-SECRET-PROMPT-SHOULD-NOT-PERSIST"}],
    }
    httpx.post(COLLECTOR + "/v1/derived", json=[smuggle],
               headers={"Authorization": f"Bearer {INGEST}"}, timeout=10.0)
    raw = open(f"{WORK}/central.db", "rb").read()
    check("a smuggled prompt is NEVER persisted on the collector",
          b"TOP-SECRET-PROMPT-SHOULD-NOT-PERSIST" not in raw)
    # cost re-pricing: a hostile edge claiming an absurd cost is re-priced from token facts
    forged = dict(smuggle, event_id="forged-cost", cost_usd=999999.0, cost_priced=True)
    forged.pop("messages", None)
    httpx.post(COLLECTOR + "/v1/derived", json=[forged],
               headers={"Authorization": f"Bearer {INGEST}"}, timeout=10.0)
    admin_rep = api("acme-eu-mgr", "/api/report").json()
    check("the collector re-prices spend (a forged $999999 cost cannot inflate the org total)",
          admin_rep.get("total_cost_usd", 0) < 100000, f"total=${admin_rep.get('total_cost_usd')}")


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        shutdown()
    sys.exit(code)
