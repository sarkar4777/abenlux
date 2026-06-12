#!/usr/bin/env python3
"""
Run Abenlux live on your own machine and watch it work while two Claude Code sessions code.

It starts three things and keeps them running.
  the collector and dashboard on port 8090, where the manager view lives
  a gateway for the developer alice on port 8101
  a gateway for the developer bob on port 8102

Each gateway carries its own developer identity, so the two VS Code windows show up as two real people.
You point each window's Claude Code at its own gateway, open the dashboard in a browser, and watch the
spend, the value, and the collaboration appear as you code. Nothing about your workflow changes, the
gateway just sits in the middle and forwards every call to Anthropic.

  python examples/live-demo/start.py

Then follow the printed steps. Press Ctrl C here to stop everything.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
RUN = HERE / ".run"
CFG = HERE / "config"
HMAC = "live-demo-hmac-not-for-prod"
INGEST = "live-demo-ingest"
PROCS: list[subprocess.Popen] = []


def env(**extra) -> dict:
    e = dict(os.environ)
    e.update(ABEN_HMAC_KEY=HMAC, ABEN_INGEST_TOKEN=INGEST, ABEN_KG=str(CFG / "kg.yaml"),
             ABEN_K_ANON="1", ABEN_NOTIFY="0", PYTHONUNBUFFERED="1")
    e.update(extra)
    return e


def collector_env() -> dict:
    return env(ABEN_PRINCIPALS=str(CFG / "principals.yaml"), ABEN_DB=str(RUN / "central.db"),
               ABEN_LEDGER_DB=str(RUN / "ledger.db"), ABEN_TENANT_DB=str(RUN / "tenants.db"),
               ABEN_MATCH_DB=str(RUN / "matches.db"), ABEN_CONTACT_DB=str(RUN / "contacts.db"),
               ABEN_CAPSULE_DB=str(RUN / "capsules.db"), ABEN_RELAY_DB=str(RUN / "relay.db"),
               ABEN_OUTCOME_DB=str(RUN / "outcomes.db"), ABEN_EXCHANGE_DB=str(RUN / "exchange.db"))


def gateway_env(actor: str) -> dict:
    # ABEN_ACTOR is the developer this gateway speaks for. the gateway forwards each call to the real
    # Anthropic and sends only the content-free record to the collector.
    return env(ABEN_ACTOR=actor, ABEN_TENANT="acme", ABEN_RESIDENCY="eu",
               ABEN_COLLECTOR_URL="http://127.0.0.1:8090", ABEN_EXACT_CACHE="1",
               ABEN_DB=str(RUN / f"edge-{actor}.db"), ABEN_LOCAL_DB=str(RUN / f"local-{actor}.db"),
               ABEN_MATCH_DB=str(RUN / "matches.db"))


def start(name: str, args: list[str], e: dict) -> None:
    log = open(RUN / f"{name}.log", "w")
    PROCS.append(subprocess.Popen(args, env=e, stdout=log, stderr=subprocess.STDOUT))


def wait(url: str, t: float = 40) -> bool:
    end = time.time() + t
    while time.time() < end:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return True
        except Exception:
            time.sleep(0.4)
    return False


def shutdown(*_a) -> None:
    print("\nstopping the demo ...", flush=True)
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
    sys.exit(0)


def main() -> None:
    RUN.mkdir(exist_ok=True)
    print("starting the collector, the dashboard, and a gateway for alice and bob ...", flush=True)
    start("collector", [sys.executable, "-m", "uvicorn", "abenlux.api.server:app",
                        "--host", "127.0.0.1", "--port", "8090"], collector_env())
    start("gateway-alice", [sys.executable, "-m", "uvicorn", "abenlux.capture.gateway:app",
                            "--host", "127.0.0.1", "--port", "8101"], gateway_env("alice"))
    start("gateway-bob", [sys.executable, "-m", "uvicorn", "abenlux.capture.gateway:app",
                          "--host", "127.0.0.1", "--port", "8102"], gateway_env("bob"))
    ok = wait("http://127.0.0.1:8090/health") and wait("http://127.0.0.1:8101/health") and wait("http://127.0.0.1:8102/health")
    if not ok:
        print("something did not start. see the logs in", RUN)
        shutdown()
    # register the tenant so the manager report resolves
    try:
        httpx.post("http://127.0.0.1:8090/api/tenants", headers={"Authorization": "Bearer admin"},
                   json={"tenant_id": "acme", "display_name": "Acme", "residency": "eu"}, timeout=5)
    except Exception:
        pass

    bar = "=" * 78
    print(f"""
{bar}
  Abenlux is running. Here is how to watch it work.
{bar}

1) OPEN THE DASHBOARD (the manager view)
   In a browser go to:   http://127.0.0.1:8090
   Sign in with the token:   boss
   Leave this tab open. It updates every few seconds as alice and bob code.

2) WINDOW 1 = ALICE. In a VS Code terminal, set these and start Claude.
   PowerShell:
     $env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8101"
     $env:ANTHROPIC_API_KEY  = "<your anthropic api key>"
     claude

3) WINDOW 2 = BOB. In a second VS Code window terminal, set these and start Claude.
   PowerShell:
     $env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8102"
     $env:ANTHROPIC_API_KEY  = "<your anthropic api key>"
     claude

4) NOW CODE in both windows. Ask Claude to work on the same kind of thing in both,
   for example a checkout retry, so you can watch them MATCH as collaborators.
   Tip: name a branch like  feature/APP-100  so the spend ties to the Acme App goal.

5) WATCH IT LIVE
   - The dashboard at http://127.0.0.1:8090 (token boss) shows spend, value, savings,
     and the developer roster, refreshing on its own.
   - Each developer can see their OWN private view. Sign in to the same dashboard with
     the token  alice  or  bob  to see their spend, nudges, and collaboration matches.
   - Or in a terminal, tail the raw capture:
       PowerShell:  Get-Content -Wait '{RUN / "gateway-alice.log"}'

   Notes
   - Claude Code must use an Anthropic API key for this (set ANTHROPIC_API_KEY above).
     A subscription login does not route through a custom base url.
   - Everything is on your machine. Only content-free records leave each gateway, and
     they go to your own collector on 127.0.0.1, nowhere else.

  Press Ctrl C in THIS window to stop the demo.
{bar}
""", flush=True)

    signal.signal(signal.SIGINT, shutdown)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
