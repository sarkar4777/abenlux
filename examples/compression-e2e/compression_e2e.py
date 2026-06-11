#!/usr/bin/env python3
"""
Real-model, before/after proof of the Abenlux edge compression layer.

It stands up the REAL stack - the central collector and TWO edge gateways - and drives a fleet of
developers through MULTI-TURN coding sessions against REAL model providers (Anthropic + Google
Gemini, no mock upstream). The two gateways see the IDENTICAL workload:

  rocket-base : ABEN_COMPRESS=off  ABEN_EXACT_CACHE=0   (a normal pass-through proxy)
  rocket-zip  : ABEN_COMPRESS=all  ABEN_EXACT_CACHE=1   (the compression layer on)

Every figure is the product of a real call: the gateway forwards the (compressed or not) request to
the real provider, the provider bills the real input tokens, and the content-free DerivedRecord is
forwarded to the collector. After the run it reads the collector store and prints a before/after
table - input tokens billed, dollars, exact-cache hits, edge-measured tokens removed - then writes
the numbers to result.json for the screenshot renderer.

Keys are read from the environment ONLY (ANTHROPIC_API_KEY, GEMINI_API_KEY) and never written to disk.

  ANTHROPIC_API_KEY=... GEMINI_API_KEY=... python examples/compression-e2e/compression_e2e.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid

import httpx

WORK = tempfile.mkdtemp(prefix="aben-czip-")
HMAC = "compression-e2e-hmac-not-for-prod"
INGEST = "compression-ingest-token"
COLLECTOR = "http://127.0.0.1:8090"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
GEMINI_MODEL = "gemini-2.5-flash"
HERE = os.path.dirname(os.path.abspath(__file__))

# tenant_id, compress spec, exact-cache flag, gateway port
BASE = ("rocket-base", "off", "0", 8201)
ZIP = ("rocket-zip", "all", "1", 8202)

PROCS: list[subprocess.Popen] = []


def log(m: str) -> None:
    print(m, flush=True)


# ----------------------------- the workload (what a developer actually pastes) -----------------------------

# a long, STABLE house-style guide. real coding tools prepend a big system prompt every turn; it is the
# same every call, so it is the natural cacheable prefix - unless a volatile token is injected ahead of
# it (which is exactly what prefix-stabilize fixes).
STABLE_SYSTEM = ("You are a senior staff engineer pair-programming inside the Rocket monorepo. "
                 "Follow the house style exactly.\n") + "\n".join(
    f"Rule {i}: " + txt for i, txt in enumerate([
        "Prefer pure functions and explicit dependencies over hidden global state.",
        "Every public function has a docstring stating its contract and failure modes.",
        "No semicolons in prose comments; keep comments terse and lowercase.",
        "Validate inputs at the boundary and never trust a client-supplied identifier.",
        "Errors are values; do not raise across module boundaries without wrapping.",
        "All money is integer minor units; never use a float for currency.",
        "Time is UTC end to end; convert only at the presentation edge.",
        "Database writes go through the repository layer, never inline SQL in handlers.",
        "A migration is reversible or it ships with a documented forward-only justification.",
        "Feature flags default off and are removed within two releases of full rollout.",
        "Tests assert behavior, not implementation; one logical assertion per test.",
        "Network calls have a bounded timeout and a typed error for each failure mode.",
        "Logs are structured key-value; never log a secret or a raw access token.",
        "Cache keys include a version so a format change cannot serve a stale shape.",
        "Idempotency keys are derived from the business identity of the operation.",
        "Retries use capped exponential backoff with jitter and a maximum attempt count.",
        "Public APIs are versioned; a breaking change is a new version, never a mutation.",
        "Background jobs are at-least-once; every consumer is idempotent by construction.",
        "Secrets come from the secret store at runtime, never from a file in the repo.",
        "Code review checks the threat model first, then correctness, then style.",
    ] * 3))   # repeated to comfortably exceed the model cache minimum, still stable across calls

# a noisy build log a developer pastes for help. ANSI color codes, and the same warning repeated a
# couple hundred times - command-trim strips the color and collapses the repeats to one line + a count.
NOISY_LOG = "\x1b[33m" + "\n".join(
    ["\x1b[33mnpm WARN deprecated har-validator@5.1.5: this library is no longer supported\x1b[0m"] * 180
    + ["\x1b[31mERROR\x1b[0m in ./src/checkout/capture.ts:42:7", "  TS2345: Argument of type 'string'",
       "\x1b[32m  + expected\x1b[0m", "\x1b[31m  - actual\x1b[0m"] * 1
) + "\x1b[0m"

# a big pretty-printed JSON config - compress-json minifies it; the parsed value is identical.
BIG_JSON = json.dumps({
    "service": "checkout", "version": 7,
    "limits": {"rps": 2000, "burst": 5000, "timeout_ms": 800, "retries": {"max": 4, "backoff_ms": 200}},
    "providers": [{"name": f"psp-{i}", "weight": i * 10, "regions": ["eu-west", "us-east", "ap-south"],
                   "fees": {"fixed_minor": 30 + i, "pct_bps": 250}} for i in range(8)],
    "flags": {f"flag_{i}": (i % 2 == 0) for i in range(20)},
}, indent=2)

# a verbose HTML results table - otsl-tables transcodes it to compact OTSL, cells preserved.
HTML_TABLE = ("<table><tr><th>objective</th><th>calls</th><th>p95_ms</th><th>err_rate</th></tr>"
              + "".join(f"<tr><td>checkout-{i}</td><td>{1000 + i}</td><td>{120 + i}</td>"
                       f"<td>0.0{i}</td></tr>" for i in range(15)) + "</table>")


def _html_table(title: str, cols: list[str], n: int) -> str:
    head = "".join(f"<th>{c}</th>" for c in cols)
    rows = "".join("<tr>" + "".join(f"<td>{title[:3]}-{r}-{c}</td>" for c in range(len(cols))) + "</tr>"
                   for r in range(n))
    return f"<table><tr>{head}</tr>{rows}</table>"


# a long requirements DOCUMENT a developer pastes for review: prose, several verbose HTML tables, and a
# couple of embedded JSON examples. exercises otsl-tables (the DocLang/document path) and compress-json
# heavily, the way a real spec review would.
DOC_SPEC = (
    "# Checkout settlement spec\n\nThe settlement service reconciles captured payments against the "
    "ledger nightly. Below are the data contracts, the routing matrix, and the SLA table.\n\n"
    + _html_table("routing matrix", ["region", "psp", "weight", "fallback", "max_latency_ms"], 18)
    + "\n\nThe per-currency limits are configured as:\n\n```json\n"
    + json.dumps({f"cur_{c}": {"min_minor": c * 100, "max_minor": c * 100000, "rounding": "half_even",
                               "providers": [f"psp-{p}" for p in range(4)]} for c in range(10)}, indent=2)
    + "\n```\n\nThe SLA targets per tier:\n\n"
    + _html_table("sla targets", ["tier", "p50_ms", "p95_ms", "p99_ms", "monthly_uptime"], 14)
    + "\n\nAnd the reconciliation status codes:\n\n"
    + _html_table("status codes", ["code", "meaning", "retryable", "owner"], 16))

# a second noisy log: a flaky test run that retried the same failure many times (command-trim collapses).
NOISY_TEST_LOG = "\x1b[36m" + "\n".join(
    ["\x1b[36mPASS\x1b[0m src/checkout/router.test.ts"] * 3
    + ["\x1b[33mRETRY\x1b[0m flaky: payment.capture.idempotency (attempt timed out after 5000ms)"] * 140
    + ["\x1b[31mFAIL\x1b[0m src/checkout/settle.test.ts  expected 200 received 503"] * 1) + "\x1b[0m"


def _volatile() -> str:
    # what a tool injects at the very TOP of the system prompt every call: a per-call session id and a
    # timestamp with seconds. it busts the cacheable prefix unless moved out of the way.
    return f"Session {uuid.uuid4()} started {time.strftime('%Y-%m-%dT%H:%M:%S')}. "


# ----------------------------- provider request shapes -----------------------------
# a 5-turn coding session: a build log, a config + perf table, a requirements DOCUMENT, a flaky test
# log, then an exact repeat of turn 2. each turn gives a different strategy something real to compress.
QUESTION = {1: "What is the actual error I should fix? One sentence.",
            2: "Is the retry budget consistent with the p95? One sentence.",
            3: "Does the routing matrix cover every currency in the limits? One sentence.",
            4: "Which test is genuinely failing versus just flaky? One sentence."}
DOCS = {1: lambda: "Build log:\n\n" + NOISY_LOG,
        2: lambda: "Config and perf table:\n\n```json\n" + BIG_JSON + "\n```\n\n" + HTML_TABLE,
        3: lambda: "Please review this spec:\n\n" + DOC_SPEC,
        4: lambda: "Test run:\n\n" + NOISY_TEST_LOG}


def user_text(turn: int) -> str:
    return DOCS[turn]() + "\n\n" + QUESTION[turn]


def anthropic_body(turn: int, fixed_vol: str | None = None) -> dict:
    system = [{"type": "text", "text": (fixed_vol or _volatile()) + STABLE_SYSTEM}]
    return {"model": ANTHROPIC_MODEL, "max_tokens": 64, "system": system,
            "messages": [{"role": "user", "content": user_text(turn)}]}


def gemini_body(turn: int, fixed_vol: str | None = None) -> dict:
    si = {"parts": [{"text": (fixed_vol or _volatile()) + STABLE_SYSTEM}]}
    return {"systemInstruction": si, "contents": [{"role": "user", "parts": [{"text": user_text(turn)}]}],
            "generationConfig": {"maxOutputTokens": 64}}


# ----------------------------- developers -----------------------------
# 24 developers, 5 turns each, pointing at BOTH gateways so the before/after is the same workload,
# compressed and not. 18 on Anthropic across many tool tags, 6 on Gemini (gemini-cli).
N_DEVS = 24
_ATOOLS = ["claude-code", "aider", "cline", "opencode", "crush", "continue", "droid", "goose", "windsurf"]
TOOLS_BY_DEV = [(_ATOOLS[i % len(_ATOOLS)] if i < 18 else "gemini-cli") for i in range(N_DEVS)]


def _drive_one_dev(dev: int, port: int, tenant: str, a_key: str, g_key: str) -> dict:
    tool = TOOLS_BY_DEV[dev]
    actor = f"{tenant}-dev{dev:02d}"
    is_gem = tool == "gemini-cli"
    build = gemini_body if is_gem else anthropic_body
    fixed_vol = _volatile()
    turn2 = build(2, fixed_vol)
    plan = [(1, build(1)), (2, turn2), (3, build(3)), (4, build(4)),
            (5, turn2)]   # turn 5 == turn 2, byte for byte -> exact-cache repeat
    out = {"calls": 0, "errors": 0}
    for turn, body in plan:
        try:
            if is_gem:
                url = f"http://127.0.0.1:{port}/v1beta/models/{GEMINI_MODEL}:generateContent"
                headers = {"x-goog-api-key": g_key, "content-type": "application/json",
                           "x-aben-actor": actor, "x-aben-tool": tool, "x-aben-branch": "feature/ROCKET-1"}
            else:
                url = f"http://127.0.0.1:{port}/v1/messages"
                headers = {"x-api-key": a_key, "anthropic-version": "2023-06-01",
                           "content-type": "application/json", "x-aben-actor": actor,
                           "x-aben-tool": tool, "x-aben-branch": "feature/ROCKET-1"}
            r = httpx.post(url, json=body, headers=headers, timeout=90.0)
            out["calls"] += 1
            if r.status_code >= 300:
                out["errors"] += 1
                if out["errors"] <= 2:
                    log(f"    upstream {r.status_code} for {actor} t{turn}: {r.text[:160]}")
        except Exception as e:
            out["errors"] += 1
            if out["errors"] <= 2:
                log(f"    error for {actor} t{turn}: {type(e).__name__} {e}")
    return out


def drive(port: int, tenant: str) -> dict:
    """drive all developers (parallel across devs, sequential within a dev so turn 5 can hit the cache
    its own turn 2 wrote). returns a small client-side tally."""
    from concurrent.futures import ThreadPoolExecutor
    a_key = os.environ["ANTHROPIC_API_KEY"]
    g_key = os.environ["GEMINI_API_KEY"]
    sent = {"calls": 0, "errors": 0}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_drive_one_dev, d, port, tenant, a_key, g_key) for d in range(N_DEVS)]
        for f in futs:
            r = f.result()
            sent["calls"] += r["calls"]
            sent["errors"] += r["errors"]
    return sent


# ----------------------------- process orchestration -----------------------------

def _base_env() -> dict:
    e = dict(os.environ)
    e.update(ABEN_HMAC_KEY=HMAC, ABEN_INGEST_TOKEN=INGEST, ABEN_KG=f"{WORK}/kg.yaml",
             ABEN_K_ANON="3", ABEN_NOTIFY="0", PYTHONUNBUFFERED="1")
    return e


def start(name: str, args: list[str], env: dict) -> None:
    f = open(f"{WORK}/{name}.log", "w")
    PROCS.append(subprocess.Popen(args, env=env, stdout=f, stderr=subprocess.STDOUT))


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


def gateway_env(tenant: str, compress: str, exact: str) -> dict:
    e = _base_env()
    # NB: no ABEN_*_UPSTREAM override -> the gateway forwards to the REAL provider.
    e.update(ABEN_TENANT=tenant, ABEN_RESIDENCY="eu", ABEN_COLLECTOR_URL=COLLECTOR,
             ABEN_COMPRESS=compress, ABEN_EXACT_CACHE=exact, ABEN_EXACT_CACHE_TTL="600",
             ABEN_DB=f"{WORK}/edge-{tenant}.db", ABEN_LOCAL_DB=f"{WORK}/local-{tenant}.db",
             ABEN_MATCH_DB=f"{WORK}/edge-matches-{tenant}.db")
    return e


def collector_env() -> dict:
    e = _base_env()
    e.update(ABEN_PRINCIPALS=f"{WORK}/principals.yaml", ABEN_DB=f"{WORK}/central.db",
             ABEN_LEDGER_DB=f"{WORK}/ledger.db", ABEN_TENANT_DB=f"{WORK}/tenants.db",
             ABEN_MATCH_DB=f"{WORK}/matches.db", ABEN_CONTACT_DB=f"{WORK}/contacts.db")
    return e


def write_config() -> None:
    import yaml
    open(f"{WORK}/kg.yaml", "w").write(
        "objectives:\n  - {id: obj-rocket, label: \"Rocket - Checkout\", kind: client, "
        "monthly_budget_usd: 5000}\nticket_prefix_to_objective:\n  ROCKET: obj-rocket\n")
    principals = [{"token": f"{t}-mgr", "subject": f"{t}-mgr", "role": "manager",
                   "tenant_id": t, "org": "rocket"} for t, *_ in (BASE, ZIP)]
    # a principal per developer so a developer's own /api/me (spend, nudges, collaboration matches)
    # resolves to exactly the rows their calls produced (token == subject == the actor they send).
    for t, *_ in (BASE, ZIP):
        for d in range(N_DEVS):
            tok = f"{t}-dev{d:02d}"
            principals.append({"token": tok, "subject": tok, "role": "developer",
                               "tenant_id": t, "org": "rocket", "slack": f"@{tok}"})
    open(f"{WORK}/principals.yaml", "w").write(yaml.safe_dump({"principals": principals}))


# ----------------------------- before/after measurement -----------------------------

# ----------------------------- per-strategy attribution (real estimator, no network) -----------------------------

def per_strategy_breakdown() -> list[dict]:
    """Prove EVERY strategy fires and quantify each on the actual workload, measured by the real token
    estimator the gateway uses. This runs each strategy in isolation on a representative request body so
    the saving is attributable per technique - including the DocLang/OTSL document one and tool slimming
    (which the live Anthropic path can't carry, because Anthropic rejects duplicate tool names outright)."""
    from abenlux.compress import compress_request, strategies

    tools = [{"name": n, "description": f"{n} a checkout resource",
              "input_schema": {"type": "object", "properties": {"id": {"type": "string"}},
                               "required": ["id"]}}
             for n in ["search", "read", "write", "patch", "delete", "list", "lock", "audit"]]
    tools_dup = tools + [dict(t) for t in tools]   # each definition resent once (Bifrost footgun)

    # one kitchen-sink request that gives every strategy something to bite.
    body = {
        "model": ANTHROPIC_MODEL, "max_tokens": 64,
        "system": [{"type": "text", "text": _volatile() + STABLE_SYSTEM}],
        "messages": [{"role": "user", "content":
                      NOISY_LOG + "\n\n```json\n" + BIG_JSON + "\n```\n\n" + HTML_TABLE
                      + "\n\n" + DOC_SPEC + "\n\n" + NOISY_TEST_LOG}],
        "tools": tools_dup,
    }
    reg = strategies()
    rows = []
    for name in ["prefix_stabilize", "command_trim", "otsl_tables", "compress_json", "slim_tools"]:
        res = compress_request(body, "anthropic", [reg[name]])
        rows.append({"strategy": name, "applied": bool(res.applied),
                     "saved_tokens": res.saved_tokens, "note": reg[name].note})
    allres = compress_request(body, "anthropic", list(reg.values()))
    rows.append({"strategy": "ALL (combined)", "applied": bool(allres.applied),
                 "saved_tokens": allres.saved_tokens, "note": "every strategy, one request"})
    return rows


# ----------------------------- real prompt-cache A/B for the prefix localizer -----------------------------

def prompt_cache_ab() -> dict:
    """Prove the prefix-cache technique with REAL provider cache metrics. Same big stable system prompt,
    twice. A: a volatile id sits in the cache-stable prefix (what tools inject) -> the second call cannot
    reuse the cache. B: the volatile id is moved out of the cached prefix (what prefix-stabilize does)
    -> the second call reads the whole stable prefix from cache. We report cache-read tokens on call 2."""
    a_key = os.environ["ANTHROPIC_API_KEY"]
    big_stable = (STABLE_SYSTEM + "\n") * 4   # comfortably over the model prompt-cache minimum (2048 tok)

    def call(system_blocks: list[dict]) -> dict:
        body = {"model": ANTHROPIC_MODEL, "max_tokens": 8, "system": system_blocks,
                "messages": [{"role": "user", "content": "Reply with the single word ok."}]}
        r = httpx.post("https://api.anthropic.com/v1/messages", json=body, timeout=60.0,
                       headers={"x-api-key": a_key, "anthropic-version": "2023-06-01",
                                "content-type": "application/json"})
        u = r.json().get("usage", {}) if r.status_code < 300 else {}
        return {"read": u.get("cache_read_input_tokens", 0), "create": u.get("cache_creation_input_tokens", 0)}

    # A - volatile token INSIDE the cached prefix (cache_control covers volatile + stable): busts on call 2
    def a_sys() -> list[dict]:
        return [{"type": "text", "text": _volatile() + big_stable, "cache_control": {"type": "ephemeral"}}]
    call(a_sys())              # warm
    a2 = call(a_sys())         # second call, different volatile -> prefix differs -> no read
    # B - stable prefix cached, volatile moved to a SEPARATE trailing block (what prefix-stabilize yields)
    b_warm = [{"type": "text", "text": big_stable, "cache_control": {"type": "ephemeral"}},
              {"type": "text", "text": _volatile()}]
    call(b_warm)               # warm: writes the stable prefix to cache
    b2 = [{"type": "text", "text": big_stable, "cache_control": {"type": "ephemeral"}},
          {"type": "text", "text": _volatile()}]
    b2u = call(b2)             # second call: same stable prefix -> cache read
    return {"volatile_in_prefix": a2, "volatile_moved_out": b2u}


def measure() -> dict:
    # read straight from the collector store. billed input tokens EXCLUDE exact-cache hits (those never
    # went upstream, so nothing was billed) - that is the honest before/after, same as the cost column.
    import sqlite3
    con = sqlite3.connect(f"{WORK}/central.db")
    try:
        out = {}
        for tenant, *_ in (BASE, ZIP):
            row = con.execute(
                "SELECT COUNT(*), "
                "COALESCE(SUM(CASE WHEN served_from_cache=0 THEN input_tokens ELSE 0 END),0), "
                "COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(cost_usd),0), "
                "COALESCE(SUM(saved_input_tokens),0), "
                "COALESCE(SUM(CASE WHEN served_from_cache=1 THEN 1 ELSE 0 END),0), "
                "COUNT(DISTINCT actor_pseudonym) FROM derived WHERE tenant_id=?", (tenant,)).fetchone()
            out[tenant] = {
                "calls": row[0], "input_tokens": row[1], "cache_read_tokens": row[2],
                "cost_usd": round(row[3], 4), "saved_input_tokens": row[4],
                "cache_hits": row[5], "actors": row[6],
            }
        return out
    finally:
        con.close()


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY") or not os.environ.get("GEMINI_API_KEY"):
        log("ANTHROPIC_API_KEY and GEMINI_API_KEY must be set in the environment.")
        return 2

    log("=== boot: collector + two real-upstream gateways (base vs zip) ===")
    write_config()
    start("collector", [sys.executable, "-m", "uvicorn", "abenlux.api.server:app", "--port", "8090"],
          collector_env())
    for tenant, comp, exact, port in (BASE, ZIP):
        start(f"gw-{tenant}", [sys.executable, "-m", "uvicorn", "abenlux.capture.gateway:app",
                               "--port", str(port)], gateway_env(tenant, comp, exact))
    ok = wait_http(f"{COLLECTOR}/health")
    for _t, _c, _e, port in (BASE, ZIP):
        ok = wait_http(f"http://127.0.0.1:{port}/health") and ok
    if not ok:
        log("stack failed to boot; logs:")
        for fn in os.listdir(WORK):
            if fn.endswith(".log"):
                log(f"--- {fn} ---\n" + open(f"{WORK}/{fn}").read()[-1500:])
        return 1
    log("  stack up")

    for tenant, comp, exact, port in (BASE, ZIP):
        log(f"=== drive {N_DEVS} developers x 5 turns through {tenant} (compress={comp}, exact_cache={exact}) ===")
        tally = drive(port, tenant)
        log(f"  {tenant}: {tally['calls']} real calls, {tally['errors']} errors")

    log("  waiting for edge -> collector forwarding to settle ...")
    last = -1
    for _ in range(25):
        time.sleep(2.0)
        n = httpx.get(f"{COLLECTOR}/health", timeout=5.0).json().get("events", 0)
        if n == last and n > 0:
            break
        last = n
    log(f"  collector ingested {last} derived records")

    log("\n=== per-strategy attribution (real estimator, measured on the actual workload) ===")
    breakdown = per_strategy_breakdown()
    log(f"  {'strategy':20} {'fired':>7} {'tokens saved':>14}   what it does")
    log("  " + "-" * 96)
    for r in breakdown:
        log(f"  {r['strategy']:20} {('yes' if r['applied'] else 'no'):>7} {r['saved_tokens']:>14,}   {r['note']}")

    log("\n=== real prompt-cache A/B (Anthropic cache-read tokens on the second call) ===")
    ab = prompt_cache_ab()
    log(f"  volatile id left in the cached prefix (no localizer): "
        f"{ab['volatile_in_prefix']['read']:,} cache-read tokens "
        f"(wrote {ab['volatile_in_prefix']['create']:,})")
    log(f"  volatile id moved out of the prefix (prefix-stabilize): "
        f"{ab['volatile_moved_out']['read']:,} cache-read tokens "
        f"(wrote {ab['volatile_moved_out']['create']:,})")

    result = measure()
    result["per_strategy"] = breakdown
    result["cache_ab"] = ab
    b, z = result[BASE[0]], result[ZIP[0]]
    # honest before/after: billed dollars and billed input tokens, same workload.
    tok_cut = (1 - z["input_tokens"] / b["input_tokens"]) * 100 if b["input_tokens"] else 0
    usd_cut = (1 - z["cost_usd"] / b["cost_usd"]) * 100 if b["cost_usd"] else 0
    result["delta"] = {"input_token_reduction_pct": round(tok_cut, 1),
                       "cost_reduction_pct": round(usd_cut, 1),
                       "dollars_saved": round(b["cost_usd"] - z["cost_usd"], 4)}

    log("\n=== BEFORE / AFTER (every figure billed by the real provider) ===")
    hdr = f"  {'metric':28} {'rocket-base (off)':>20} {'rocket-zip (on)':>20}"
    log(hdr + "\n  " + "-" * (len(hdr) - 2))
    rows = [("developers", "actors"), ("real calls captured", "calls"),
            ("input tokens billed", "input_tokens"), ("cache-read tokens", "cache_read_tokens"),
            ("exact-cache hits (free)", "cache_hits"), ("tokens removed at edge", "saved_input_tokens"),
            ("cost (USD)", "cost_usd")]
    for label, key in rows:
        bv, zv = b[key], z[key]
        bs = f"${bv:.4f}" if key == "cost_usd" else f"{bv:,}"
        zs = f"${zv:.4f}" if key == "cost_usd" else f"{zv:,}"
        log(f"  {label:28} {bs:>20} {zs:>20}")
    log(f"\n  input tokens cut {result['delta']['input_token_reduction_pct']}%  |  "
        f"cost cut {result['delta']['cost_reduction_pct']}%  |  "
        f"${result['delta']['dollars_saved']} saved on this run alone")

    json.dump(result, open(os.path.join(HERE, "result.json"), "w"), indent=2)
    log(f"\n  wrote {os.path.join(HERE, 'result.json')}")

    # snapshot the live collector state so the dashboard renderer can boot a collector against it and
    # screenshot the REAL product UI (management + developer views) with this run's data.
    import shutil
    evidence = os.path.join(HERE, "evidence")
    os.makedirs(evidence, exist_ok=True)
    for fn in ("central.db", "ledger.db", "tenants.db", "matches.db", "contacts.db",
               "principals.yaml", "kg.yaml"):
        src = os.path.join(WORK, fn)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(evidence, fn))
    log(f"  snapshotted collector state to {evidence}")

    checks = [
        ("both gateways captured the same fleet", b["actors"] == z["actors"] == N_DEVS),
        ("the compression layer removed input tokens", z["saved_input_tokens"] > 0),
        ("zip billed fewer input tokens than base", z["input_tokens"] < b["input_tokens"]),
        ("zip cost less than base", z["cost_usd"] < b["cost_usd"]),
        ("exact-cache served byte-identical repeats for free", z["cache_hits"] >= 1),
        ("base did NOT use the local cache (control)", b["cache_hits"] == 0),
    ]
    log("\n=== checks ===")
    fails = 0
    for name, cond in checks:
        log(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        fails += 0 if cond else 1
    log(f"\n  {len(checks) - fails}/{len(checks)} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        shutdown()
    sys.exit(code)
