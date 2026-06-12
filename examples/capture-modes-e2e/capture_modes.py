#!/usr/bin/env python3
"""
The two ways a tool is captured, and what compression can and cannot touch in each.

A subscription tool reports its own usage to the agent over telemetry. The tool makes its OWN call to
the provider, so the agent only sees the usage AFTER the fact. Nothing can be compressed on that path,
because the request was already sent. A bring-your-own-key tool points its base url at the agent, so the
agent sits IN the call and rewrites the request before it goes out, which is where compression happens.

This boots the real stack and drives both paths against the real provider, then reads the records and
proves the difference. A telemetry record is captured with its usage but carries no compression. A base
url record is captured AND compressed. Run it to confirm the honest scope of the savings layer.

  ANTHROPIC_API_KEY=... python examples/capture-modes-e2e/capture_modes.py
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx

WORK = tempfile.mkdtemp(prefix="aben-modes-")
HMAC, INGEST = "modes-hmac", "modes-ingest"
COLLECTOR = "http://127.0.0.1:8094"
GW = 8095
A_KEY = os.getenv("ANTHROPIC_API_KEY", "")
A_MODEL = "claude-haiku-4-5-20251001"
PROCS: list[subprocess.Popen] = []
CHECKS: list[tuple[str, bool, str]] = []
HOUSE = ("You are a senior engineer in the Rocket monorepo. Follow the house style. "
         "Prefer pure functions. Validate inputs. Money is integer minor units. Time is UTC. ") * 30


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'OK ' if ok else 'GAP'}] {name}" + (f"  {detail}" if detail else ""), flush=True)


def section(t):
    print(f"\n=== {t} ===", flush=True)


def env(**x):
    e = dict(os.environ)
    e.update(ABEN_HMAC_KEY=HMAC, ABEN_INGEST_TOKEN=INGEST, ABEN_KG=f"{WORK}/kg.yaml", ABEN_K_ANON="1",
             ABEN_NOTIFY="0", PYTHONUNBUFFERED="1")
    e.update(x)
    return e


def start(name, args, e):
    PROCS.append(subprocess.Popen(args, env=e, stdout=open(f"{WORK}/{name}.log", "w"), stderr=subprocess.STDOUT))


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


# ----- a real Claude Code telemetry payload (verbatim shape from a real claude run) -----

def _kv(key, **val):
    return {"key": key, "value": val}


def _otlp_log(model, inp, out, cache_r, cache_w):
    return {"resourceLogs": [{"scopeLogs": [{"logRecords": [{
        "body": {"stringValue": "claude_code.api_request"},
        "attributes": [
            _kv("user.id", stringValue="2c966dac43b7af20c51e7c25e94dd464aec812bb5ef857cf8a66d87a2493c903"),
            _kv("user.email", stringValue="dev@corp.example"),
            _kv("model", stringValue=model),
            _kv("input_tokens", intValue=inp), _kv("output_tokens", intValue=out),
            _kv("cache_read_tokens", intValue=cache_r), _kv("cache_creation_tokens", intValue=cache_w),
        ]}]}]}]}


def records():
    con = sqlite3.connect(f"{WORK}/central.db")
    try:
        rows = con.execute("SELECT tier, compression, input_tokens, request_model, tool FROM derived").fetchall()
        return [{"tier": r[0], "compression": r[1], "input_tokens": r[2], "model": r[3], "tool": r[4]} for r in rows]
    finally:
        con.close()


def main() -> int:
    if not A_KEY:
        print("set ANTHROPIC_API_KEY")
        return 2
    open(f"{WORK}/kg.yaml", "w").write("objectives:\n  - {id: o, label: O}\n")
    section("Boot the real stack (collector + one gateway, compression on, real Anthropic upstream)")
    start("collector", [sys.executable, "-m", "uvicorn", "abenlux.api.server:app", "--port", "8094"],
          env(ABEN_DB=f"{WORK}/central.db", ABEN_LEDGER_DB=f"{WORK}/l.db", ABEN_TENANT_DB=f"{WORK}/t.db",
              ABEN_MATCH_DB=f"{WORK}/m.db", ABEN_CONTACT_DB=f"{WORK}/ct.db", ABEN_CAPSULE_DB=f"{WORK}/cp.db",
              ABEN_RELAY_DB=f"{WORK}/r.db", ABEN_OUTCOME_DB=f"{WORK}/o.db", ABEN_EXCHANGE_DB=f"{WORK}/e.db"))
    start("gateway", [sys.executable, "-m", "uvicorn", "abenlux.capture.gateway:app", "--port", str(GW)],
          env(ABEN_ACTOR="alice", ABEN_TENANT="acme", ABEN_COLLECTOR_URL=COLLECTOR, ABEN_COMPRESS="all",
              ABEN_DB=f"{WORK}/edge.db", ABEN_LOCAL_DB=f"{WORK}/local.db", ABEN_MATCH_DB=f"{WORK}/m.db"))
    if not (wait(f"{COLLECTOR}/health") and wait(f"http://127.0.0.1:{GW}/health")):
        print("stack did not boot")
        return 1

    section("TELEMETRY path (how a SUBSCRIPTION tool like Claude Code is captured)")
    # the tool reports its own usage to the agent's OTLP log endpoint. it already made its own call.
    r = httpx.post(f"http://127.0.0.1:{GW}/v1/logs", json=_otlp_log(A_MODEL, 1820, 96, 21000, 0), timeout=15)
    check("a telemetry report is accepted by the agent", r.status_code < 300, f"HTTP {r.status_code}")

    section("BASE URL path (how a bring-your-own-key tool is captured, with compression)")
    # the tool points its base url at the agent, so the agent rewrites the request before forwarding.
    big_sys = [{"type": "text", "text": HOUSE}]
    rp = httpx.post(f"http://127.0.0.1:{GW}/v1/messages",
                    headers={"x-api-key": A_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": A_MODEL, "max_tokens": 16, "system": big_sys,
                          "messages": [{"role": "user", "content": "say hi"}]}, timeout=60)
    check("a base-url call is forwarded to the real provider", rp.status_code < 300, f"HTTP {rp.status_code}")

    last = -1
    for _ in range(20):
        time.sleep(2.0)
        n = httpx.get(f"{COLLECTOR}/health", timeout=5, headers={"Authorization": f"Bearer {INGEST}"}).json().get("events", 0)
        if n >= 2 and n == last:
            break
        last = n

    section("RESULT - the two records and what compression touched")
    recs = records()
    tele = [r for r in recs if (r["tier"] or "").startswith("tier1")]
    proxy = [r for r in recs if (r["tier"] or "").startswith("tier2")]
    check("the telemetry call was captured (usage, model, attribution)",
          bool(tele) and tele[0]["input_tokens"] > 0, f"{len(tele)} tier1 record(s)")
    check("the telemetry call carries NO compression (the tool already sent its own request)",
          bool(tele) and not tele[0]["compression"], f"compression={tele[0]['compression'] if tele else 'n/a'}")
    check("the base-url call was captured", bool(proxy), f"{len(proxy)} tier2 record(s)")
    check("the base-url call WAS compressed (the agent rewrote it before forwarding)",
          bool(proxy) and bool(proxy[0]["compression"]), f"compression={proxy[0]['compression'] if proxy else 'n/a'}")

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
