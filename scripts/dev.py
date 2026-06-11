#!/usr/bin/env python3
"""
One-command local dev stack: boots the whole thing on your machine so you can develop against it.

It starts three processes and wires them together:
  1. a MOCK model upstream            (so you spend no tokens and need no API key)
  2. the MANAGEMENT side: collector + dashboard   (abenlux serve, port 8090)
  3. the DEVELOPER side:  the edge agent           (abenlux gateway, port 8088), forwarding to (2)

Point any AI tool at http://127.0.0.1:8088 and its calls flow through the real pipeline into the
collector. Open the dashboard at http://127.0.0.1:8090. Ctrl-C stops everything and cleans up.

Run it with `make dev` (or `python scripts/dev.py`). State lives in ./.dev and is wiped each start,
so every run is clean. Logs are in ./.dev/*.log.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEV = ROOT / ".dev"
CONF = ROOT / "examples"

MOCK_PORT, COLLECTOR_PORT, GATEWAY_PORT = 9111, 8090, 8088
# demo secrets - fine for local dev only. the same key + token on both sides is what links them.
# the edge stamps the 'default' tenant so the bundled example principals (all the default tenant) see
# the data out of the box; the multi-tenant demo lives in examples/multi-dev-e2e.
HMAC, INGEST, TENANT, DEV_TOKEN = "dev-shared-key", "dev-device-token", "default", "dev-token"

PROCS: list[subprocess.Popen] = []


def _shared_env() -> dict:
    e = dict(os.environ)
    e.update(ABEN_HMAC_KEY=HMAC, ABEN_INGEST_TOKEN=INGEST, ABEN_NOTIFY="0", PYTHONUNBUFFERED="1",
             ABEN_KG=str(CONF / "knowledge_graph.example.yaml"))
    return e


def _start(name: str, args: list[str], env: dict) -> None:
    log = open(DEV / f"{name}.log", "w")
    PROCS.append(subprocess.Popen([sys.executable, "-m", *args], env=env, stdout=log,
                                  stderr=subprocess.STDOUT, cwd=str(ROOT)))


def _wait(url: str, timeout: float = 40.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url, timeout=2.0).status_code < 500:
                return True
        except Exception:
            time.sleep(0.4)
    return False


def _shutdown() -> None:
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


def main() -> int:
    if DEV.exists():
        shutil.rmtree(DEV, ignore_errors=True)
    DEV.mkdir(parents=True, exist_ok=True)

    print("starting local dev stack (mock + collector + gateway) ...", flush=True)

    # 1. mock upstream
    _start("mock", ["uvicorn", "abenlux.devtools.mock_upstream:app", "--port", str(MOCK_PORT)],
           _shared_env())

    # 2. management side: collector + dashboard
    coll_env = _shared_env()
    coll_env.update(ABEN_DB=str(DEV / "central.db"), ABEN_LEDGER_DB=str(DEV / "ledger.db"),
                    ABEN_TENANT_DB=str(DEV / "tenants.db"), ABEN_MATCH_DB=str(DEV / "matches.db"),
                    ABEN_CONTACT_DB=str(DEV / "contacts.db"),
                    ABEN_PRINCIPALS=str(CONF / "principals.example.yaml"))
    _start("collector", ["uvicorn", "abenlux.api.server:app", "--port", str(COLLECTOR_PORT)], coll_env)

    # 3. developer side: edge agent, forwarding to the collector
    gw_env = _shared_env()
    gw_env.update(ABEN_COLLECTOR_URL=f"http://127.0.0.1:{COLLECTOR_PORT}", ABEN_TENANT=TENANT,
                  ABEN_TOKEN=DEV_TOKEN, ABEN_ANTHROPIC_UPSTREAM=f"http://127.0.0.1:{MOCK_PORT}",
                  ABEN_DB=str(DEV / "edge.db"), ABEN_LOCAL_DB=str(DEV / "local.db"))
    _start("gateway", ["uvicorn", "abenlux.capture.gateway:app", "--port", str(GATEWAY_PORT)], gw_env)

    ok = _wait(f"http://127.0.0.1:{MOCK_PORT}/health")
    ok &= _wait(f"http://127.0.0.1:{COLLECTOR_PORT}/health")
    ok &= _wait(f"http://127.0.0.1:{GATEWAY_PORT}/health")
    if not ok:
        print("\na process failed to start. tail of the logs:", flush=True)
        for f in sorted(DEV.glob("*.log")):
            print(f"--- {f.name} ---\n" + f.read_text()[-1200:], flush=True)
        _shutdown()
        return 1

    print(f"""
  dev stack is up. three processes, all on your machine:

    MANAGEMENT side   dashboard   http://127.0.0.1:{COLLECTOR_PORT}    (sign in: mgr-token, fin-token, admin-token)
    DEVELOPER side    edge agent  http://127.0.0.1:{GATEWAY_PORT}    (point an AI tool here)
    mock model        upstream    http://127.0.0.1:{MOCK_PORT}    (so no tokens are spent)

  try it:
    # send a call through the developer side (any Anthropic-style client; here, plain curl):
    curl -s http://127.0.0.1:{GATEWAY_PORT}/v1/messages -H 'content-type: application/json' \\
      -H 'x-aben-actor: {DEV_TOKEN}' -H 'x-aben-branch: feature/ACME-1' \\
      -d '{{"model":"claude-opus-4-8","max_tokens":64,"messages":[{{"role":"user","content":"hi"}}]}}' >/dev/null

    # or point a real tool at it:
    #   ANTHROPIC_BASE_URL=http://127.0.0.1:{GATEWAY_PORT} <your tool>

    abenlux me        # the DEVELOPER's own private view
    # open the dashboard URL above as a manager for the MANAGEMENT view

  logs: ./.dev/*.log     state: ./.dev/ (wiped on next start)
  press Ctrl-C to stop everything.
""", flush=True)

    try:
        while True:
            time.sleep(1.0)
            for p in PROCS:
                if p.poll() is not None:
                    print("a process exited; shutting down. check ./.dev/*.log", flush=True)
                    return 1
    except KeyboardInterrupt:
        print("\nstopping dev stack ...", flush=True)
        return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        _shutdown()
    sys.exit(code)
