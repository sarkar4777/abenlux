#!/usr/bin/env python3
"""
Screenshot the REAL Abenlux dashboard against the data captured by compression_e2e.py.

It boots an actual collector pointed at the snapshot the harness left in ./evidence, then drives the
real product UI (the same dashboard.html shipped in the package) with Playwright and captures:

  docs/compression-dashboard.png   - the management view: spend -> value, the Compression-yield card
                                      with per-strategy attribution, reuse-yield, budgets.
  docs/compression-developer.png   - a developer's private view: their own spend, waste nudges, and
                                      double-blind collaboration matches.

These are genuine screenshots of the running tool, not mockups. Run compression_e2e.py first.

  python examples/compression-e2e/render_dashboard.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DOCS = os.path.join(ROOT, "docs")
EV = os.path.join(HERE, "evidence")
PORT = 8097
BASE = f"http://127.0.0.1:{PORT}"
HMAC = "compression-e2e-hmac-not-for-prod"
INGEST = "compression-ingest-token"


def boot_collector() -> subprocess.Popen:
    env = dict(os.environ)
    env.update(ABEN_HMAC_KEY=HMAC, ABEN_INGEST_TOKEN=INGEST, ABEN_K_ANON="3", ABEN_NOTIFY="0",
               PYTHONUNBUFFERED="1", ABEN_PRINCIPALS=f"{EV}/principals.yaml", ABEN_KG=f"{EV}/kg.yaml",
               ABEN_DB=f"{EV}/central.db", ABEN_LEDGER_DB=f"{EV}/ledger.db",
               ABEN_TENANT_DB=f"{EV}/tenants.db", ABEN_MATCH_DB=f"{EV}/matches.db",
               ABEN_CONTACT_DB=f"{EV}/contacts.db")
    log = open(f"{EV}/collector.log", "w")
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "abenlux.api.server:app", "--port", str(PORT)],
                         env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            if httpx.get(f"{BASE}/health", timeout=2).status_code < 500:
                return p
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("collector did not boot; see evidence/collector.log")


def shot(page, token: str, tab_wait: str, out: str) -> None:
    # inject the access token the way the dashboard expects (localStorage), then load: it auto-boots
    # into the right view for the role and fetches live data from the collector.
    page.context.add_init_script(f"window.localStorage.setItem('aben_token', {token!r})")
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_selector(tab_wait, timeout=15000)
    page.wait_for_timeout(1200)            # let the async cards finish fetching
    page.screenshot(path=out, full_page=True)
    print("wrote", out)


def main() -> int:
    if not os.path.exists(f"{EV}/central.db"):
        print("no evidence found - run compression_e2e.py first.")
        return 2
    proc = boot_collector()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            # management view (manager of the compressed tenant)
            ctx = browser.new_context(viewport={"width": 1500, "height": 1000}, device_scale_factor=2)
            shot(ctx.new_page(), "rocket-zip-mgr", "#compression",
                 os.path.join(DOCS, "compression-dashboard.png"))
            ctx.close()
            # developer view (collaboration + private spend)
            ctx = browser.new_context(viewport={"width": 1500, "height": 1000}, device_scale_factor=2)
            shot(ctx.new_page(), "rocket-zip-dev00", "#collab",
                 os.path.join(DOCS, "compression-developer.png"))
            ctx.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
