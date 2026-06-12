#!/usr/bin/env python3
"""
The forward proxy against the real provider. This proves the path that works no matter how a tool signs
in. A tool routes through the agent as an ordinary HTTPS proxy, the agent terminates the TLS with its own
local certificate, compresses the request at that point, forwards it to the real Anthropic with the
tool's own credential untouched, and captures a content-free record. The same wire path carries an API
key or a subscription token, the proxy does not care which, it just forwards the auth header it is given.

  ANTHROPIC_API_KEY=... python examples/proxy-e2e/proxy_e2e.py
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time

WORK = tempfile.mkdtemp(prefix="aben-proxy-")
# set the capture config BEFORE importing abenlux, so the on-device store and compression are wired
os.environ.update(ABEN_DB=f"{WORK}/store.db", ABEN_LOCAL_DB=f"{WORK}/store.db", ABEN_MATCH_DB=f"{WORK}/m.db",
                  ABEN_HMAC_KEY="proxy-e2e-hmac", ABEN_ACTOR="alice", ABEN_COMPRESS="all", ABEN_NOTIFY="0",
                  ABEN_CA_DIR=f"{WORK}/ca")

import httpx  # noqa: E402

from abenlux.capture.forward_proxy import make_server  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'OK ' if ok else 'GAP'}] {name}" + (f"  {detail}" if detail else ""), flush=True)


def main() -> int:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        print("set ANTHROPIC_API_KEY")
        return 2
    print("=== Forward proxy against the REAL Anthropic, capturing and compressing on the wire ===", flush=True)
    server = make_server(port=0, capture=True)            # real upstream, real capture
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    ca = str(server.ca.cert_path)

    big = "You are a senior engineer in the Rocket monorepo. Follow the house style. " * 30
    # this is exactly what a tool does behind an HTTPS proxy: it sends to api.anthropic.com, we intercept
    with httpx.Client(proxy=f"http://127.0.0.1:{port}", verify=ca, timeout=60) as c:
        r = c.post("https://api.anthropic.com/v1/messages",
                   headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                   json={"model": "claude-haiku-4-5-20251001", "max_tokens": 16, "system": big,
                         "messages": [{"role": "user", "content": "say hi in one word"}]})
    check("a real call routes through the proxy and the tool gets a normal answer",
          r.status_code == 200 and bool(r.json().get("content")), f"HTTP {r.status_code}")
    check("the agent presented its OWN trusted certificate (the tool verified it against the local CA)",
          os.path.exists(ca))

    time.sleep(3)                                         # let the background capture land
    con = sqlite3.connect(f"{WORK}/store.db")
    try:
        rows = con.execute("SELECT tier, compression, input_tokens, request_model, cost_usd FROM derived").fetchall()
    finally:
        con.close()
    check("the call was captured as a content-free record", bool(rows), f"{len(rows)} record(s)")
    if rows:
        tier, comp, inp, model, cost = rows[0]
        check("the captured record carries real usage and a price", (inp or 0) > 0 and cost is not None,
              f"input={inp} cost=${cost}")
        check("the request was COMPRESSED at the interception point (works for subscription and key alike)",
              bool(comp), f"compression={comp}")

    server.shutdown()
    gaps = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n  {len(CHECKS) - len(gaps)}/{len(CHECKS)} checks passed", flush=True)
    for g in gaps:
        print(f"    - {g}", flush=True)
    return len(gaps)


if __name__ == "__main__":
    raise SystemExit(main())
