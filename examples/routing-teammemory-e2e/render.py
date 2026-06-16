#!/usr/bin/env python3
"""
Render the developer CLI screenshots from the suite's central.db. Run suite.py first with ABEN_E2E_OUT
pointing at a directory, then point this at the same directory.

  ABEN_E2E_OUT=/tmp/rt python examples/routing-teammemory-e2e/render.py
"""
from __future__ import annotations

import html
import os
import subprocess
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DOCS = os.path.join(ROOT, "docs")
OUT = os.environ.get("ABEN_E2E_OUT")
HMAC = "rt-hmac"

CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{background:#05080d;padding:46px}
.win{width:1320px;border-radius:11px;overflow:hidden;background:#0c1219;
  border:1px solid #1d2734;box-shadow:0 26px 70px rgba(0,0,0,.55)}
.bar{display:flex;align-items:center;padding:13px 16px;background:#141c27;border-bottom:1px solid #1d2734}
.d{width:12px;height:12px;border-radius:50%;margin-right:8px}
.r{background:#ff5f57}.y{background:#febc2e}.gr{background:#28c840}
.ttl{flex:1;text-align:center;color:#7d8aa0;font-family:'Segoe UI',sans-serif;font-size:14px;margin-right:60px}
pre{font-family:'Cascadia Code','JetBrains Mono',Consolas,monospace;font-size:17px;
  line-height:1.6;color:#c6d0dc;padding:24px 26px 28px;white-space:pre-wrap;word-break:break-word}
.p{color:#5eead4}.g{color:#a6da95}.c{color:#ffffff;font-weight:600}.cur{color:#5eead4}
"""


def run(cmd_args, **env_extra):
    env = dict(os.environ)
    env.update(ABEN_DB=f"{OUT}/central.db", ABEN_KG=f"{OUT}/kg.yaml", ABEN_HMAC_KEY=HMAC,
               ABEN_K_ANON="3", PYTHONUNBUFFERED="1")
    env.update(env_extra)
    return subprocess.run([sys.executable, "-m", "abenlux.cli", *cmd_args],
                          capture_output=True, text=True, env=env, cwd=ROOT).stdout.splitlines()


def prompt(cmd):
    return f'<span class="p">~/rocket</span> <span class="g">$</span> <span class="c">{html.escape(cmd)}</span>'


def body(lines):
    return "\n".join(html.escape(ln) if ln.strip() else "" for ln in lines)


def shoot(title, session, path):
    doc = (f"<!doctype html><html><head><meta charset=utf-8><style>{CSS}</style></head><body>"
           f'<div class=win><div class=bar><span class="d r"></span><span class="d y"></span>'
           f'<span class="d gr"></span><span class=ttl>{html.escape(title)}</span></div>'
           f"<pre>{session}</pre></div></body></html>")
    os.makedirs(DOCS, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1420, "height": 1200}, device_scale_factor=2)
        pg.set_content(doc, wait_until="networkidle")
        pg.query_selector(".win").screenshot(path=path)
        b.close()
    print("wrote", path)


def render():
    rep = run(["report", "--tenant", "acme-eu"])
    # a fresh feed path so the private view shows this run only, not stale local nudges
    me = run(["me"], ABEN_ACTOR="acme-eu-api5", ABEN_SIGNAL_FEED=f"{OUT}/feed.jsonl")

    mgmt = "\n".join([prompt("abenlux report --tenant acme-eu"), body(rep),
                      '<span class="p">~/rocket</span> <span class="g">$</span> <span class="cur">&#9608;</span>'])
    shoot("rocket  -  abenlux report", mgmt, os.path.join(DOCS, "routing-teammemory-report.png"))

    dev = "\n".join([prompt("abenlux me"), body(me),
                     '<span class="p">~/rocket</span> <span class="g">$</span> <span class="cur">&#9608;</span>'])
    shoot("rocket  -  abenlux me", dev, os.path.join(DOCS, "routing-teammemory-me.png"))


if __name__ == "__main__":
    if not OUT or not os.path.exists(f"{OUT}/central.db"):
        print("set ABEN_E2E_OUT to the suite output dir (run suite.py first)")
        sys.exit(2)
    render()
