#!/usr/bin/env python3
"""
Screenshot the natural terminal a developer sees, running the real `abenlux report` for the
uncompressed tenant and then the compressed one, raw output, no decoration.

Both panes are captured stdout from the actual CLI against this run's evidence. Output goes to
docs/compression-cli-before-after.png. Run compression_e2e.py first.

  python examples/compression-e2e/render_cli.py
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
EV = os.path.join(HERE, "evidence")


def run_report(tenant: str) -> list[str]:
    env = dict(os.environ)
    env.update(ABEN_DB=f"{EV}/central.db", ABEN_LEDGER_DB=f"{EV}/ledger.db", ABEN_KG=f"{EV}/kg.yaml",
               ABEN_K_ANON="3", PYTHONUNBUFFERED="1")
    out = subprocess.run([sys.executable, "-m", "abenlux.cli", "report", "--tenant", tenant],
                         capture_output=True, text=True, env=env, cwd=ROOT).stdout
    return out.splitlines()


def prompt(cmd: str) -> str:
    return (f'<span class="p">~/rocket</span> <span class="g">$</span> '
            f'<span class="c">{html.escape(cmd)}</span>')


def out_lines(lines: list[str]) -> str:
    return "\n".join(html.escape(ln) if ln.strip() else "" for ln in lines)


def render() -> None:
    base = run_report("rocket-base")
    zipt = run_report("rocket-zip")
    session = "\n".join([
        prompt("abenlux report --tenant rocket-base"),
        out_lines(base[:9]),
        "",
        prompt("abenlux report --tenant rocket-zip"),
        out_lines(zipt[:12]),
        "",
        '<span class="p">~/rocket</span> <span class="g">$</span> <span class="cur">&#9608;</span>',
    ])
    css = """
    *{margin:0;padding:0;box-sizing:border-box}
    body{background:#05080d;padding:46px}
    .win{width:1320px;border-radius:11px;overflow:hidden;background:#0c1219;
      border:1px solid #1d2734;box-shadow:0 26px 70px rgba(0,0,0,.55)}
    .bar{display:flex;align-items:center;padding:13px 16px;background:#141c27;
      border-bottom:1px solid #1d2734}
    .d{width:12px;height:12px;border-radius:50%;margin-right:8px}
    .r{background:#ff5f57}.y{background:#febc2e}.gr{background:#28c840}
    .ttl{flex:1;text-align:center;color:#7d8aa0;font-family:'Segoe UI',sans-serif;font-size:14px;
      margin-right:60px}
    pre{font-family:'Cascadia Code','JetBrains Mono',Consolas,monospace;font-size:17.5px;
      line-height:1.6;color:#c6d0dc;padding:24px 26px 28px;white-space:pre-wrap;word-break:break-word}
    .p{color:#5eead4}.g{color:#a6da95}.c{color:#ffffff;font-weight:600}
    .cur{color:#5eead4}
    """
    doc = (f"<!doctype html><html><head><meta charset=utf-8><style>{css}</style></head><body>"
           f'<div class=win><div class=bar><span class="d r"></span><span class="d y"></span>'
           f'<span class="d gr"></span><span class=ttl>rocket  -  abenlux report</span></div>'
           f"<pre>{session}</pre></div></body></html>")
    os.makedirs(DOCS, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1420, "height": 1000}, device_scale_factor=2)
        pg.set_content(doc, wait_until="networkidle")
        pg.query_selector(".win").screenshot(path=os.path.join(DOCS, "compression-cli-before-after.png"))
        b.close()
    print("wrote", os.path.join(DOCS, "compression-cli-before-after.png"))


if __name__ == "__main__":
    if not os.path.exists(f"{EV}/central.db"):
        print("no evidence found - run compression_e2e.py first.")
        sys.exit(2)
    render()
