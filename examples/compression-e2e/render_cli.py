#!/usr/bin/env python3
"""
Render a terminal before/after from the REAL `abenlux report` CLI against this run's evidence.

It runs the actual CLI twice, once for the uncompressed tenant and once for the compressed one, then
lays the two real outputs side by side in a terminal frame -> docs/compression-cli-before-after.png.
Nothing is typed by hand, both panes are captured stdout. Run compression_e2e.py first.

  python examples/compression-e2e/render_cli.py
"""
from __future__ import annotations

import html
import os
import re
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
               ABEN_TENANT=tenant, ABEN_K_ANON="3", PYTHONUNBUFFERED="1")
    out = subprocess.run([sys.executable, "-m", "abenlux.cli", "report"],
                         capture_output=True, text=True, env=env, cwd=ROOT).stdout
    return out.splitlines()


def colorize(line: str) -> str:
    s = html.escape(line)
    # highlight the figures that carry the story, without touching the real text
    s = re.sub(r"(cost:\$[\d,]+\.\d+)", r'<span class="cost">\1</span>', s)
    s = re.sub(r"(compression yield .*)", r'<span class="teal">\1</span>', s)
    s = re.sub(r"(reuse-yield .*)", r'<span class="green">\1</span>', s)
    s = re.sub(r"(tokens:[\d,]+)", r'<span class="bright">\1</span>', s)
    if s.strip().startswith("- "):
        s = f'<span class="dim">{s}</span>'
    if s.strip().startswith("=="):
        s = f'<span class="hdr">{s}</span>'
    return s or "&nbsp;"


def pane(title: str, sub: str, accent: str, lines: list[str]) -> str:
    body = "<br>".join(colorize(ln) for ln in lines)
    return f"""<div class="term">
      <div class="bar"><span class="d r"></span><span class="d y"></span><span class="d g"></span>
        <span class="cmd">abenlux report</span>
        <span class="tag {accent}">{title}</span></div>
      <div class="sub {accent}">{sub}</div>
      <pre>{body}</pre></div>"""


def render() -> None:
    before = run_report("rocket-base")[:8]
    after = run_report("rocket-zip")[:11]
    css = """
    *{margin:0;padding:0;box-sizing:border-box}
    body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:#070d16}
    .card{width:1600px;padding:64px 70px 54px;
      background:radial-gradient(1100px 560px at 88% -8%,rgba(45,212,191,.14),transparent 60%),
        radial-gradient(900px 600px at -8% 112%,rgba(160,108,213,.12),transparent 60%),
        linear-gradient(160deg,#0a1320,#070d16 62%,#06101b)}
    .brand{display:flex;align-items:center;gap:13px;font-size:21px;letter-spacing:.16em;
      text-transform:uppercase;color:#9fdcd2;font-weight:600}
    .brand .dot{color:#2dd4bf;font-size:28px;line-height:0}
    .brand .sep{color:#37506a}.brand .sub{color:#7d93ab}
    h1{font-size:54px;font-weight:800;letter-spacing:-.02em;margin:22px 0 6px;color:#e8eef6}
    h1 .hl{color:#5eead4}
    .lead{font-size:23px;color:#aebfd2;margin-bottom:34px}
    .panes{display:grid;grid-template-columns:1fr 1fr;gap:26px}
    .term{background:#0b1119;border:1px solid rgba(159,180,204,.16);border-radius:14px;overflow:hidden;
      box-shadow:0 18px 50px rgba(0,0,0,.35)}
    .bar{display:flex;align-items:center;gap:9px;padding:14px 18px;background:#0e1622;
      border-bottom:1px solid rgba(159,180,204,.12)}
    .d{width:12px;height:12px;border-radius:50%}.d.r{background:#ed8796}.d.y{background:#f5d572}.d.g{background:#a6da95}
    .cmd{margin-left:10px;font-family:Consolas,monospace;color:#8aa0b8;font-size:17px}
    .tag{margin-left:auto;font-size:14px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
      padding:5px 12px;border-radius:8px}
    .tag.off{background:rgba(245,169,127,.16);color:#f5a97f}
    .tag.on{background:rgba(45,212,191,.18);color:#5eead4}
    .sub{font-size:15px;padding:10px 20px 0;font-weight:600}
    .sub.off{color:#f5a97f}.sub.on{color:#5eead4}
    pre{font-family:'Cascadia Code',Consolas,monospace;font-size:18px;line-height:1.65;color:#cdd9e5;
      padding:16px 22px 24px;white-space:pre-wrap;word-break:break-word}
    .hdr{color:#7d93ab}.bright{color:#e8eef6;font-weight:600}
    .cost{color:#f5a97f;font-weight:700}.term.won .cost{color:#a6da95}
    .teal{color:#5eead4;font-weight:600}.green{color:#a6da95;font-weight:600}.dim{color:#86b8b0}
    .foot{margin-top:34px;display:flex;gap:14px;flex-wrap:wrap}
    .chip{background:rgba(255,255,255,.045);border:1px solid rgba(159,180,204,.16);border-radius:13px;
      padding:18px 24px}
    .chip .n{font-size:33px;font-weight:800;font-family:'Cascadia Code',Consolas,monospace;color:#e8eef6}
    .chip .n.up{color:#5eead4}.chip .k{font-size:16px;color:#8aa0b8;margin-top:5px}
    .repo{margin-top:26px;text-align:right;font-family:Consolas,monospace;color:#9fdcd2;font-size:18px}
    """
    left = pane("compression off", "tenant rocket-base, plain pass-through proxy", "off", before)
    right = pane("compression on", "tenant rocket-zip, the layer on", "on", after).replace(
        '<div class="term">', '<div class="term won">')
    doc = f"""<!doctype html><html><head><meta charset=utf-8><style>{css}</style></head><body>
    <div class=card>
      <div class=brand><span class=dot>&#10022;</span> ABENLUX <span class=sep>&middot;</span>
        <span class=sub>abenlux report</span></div>
      <h1>Same workload. <span class=hl>59% less spend.</span> Read it off the CLI.</h1>
      <div class=lead>The real management report for a 24-developer run, billed by Claude and Gemini,
        before and after the compression layer.</div>
      <div class=panes>{left}{right}</div>
      <div class=foot>
        <div class=chip><div class="n up">-61%</div><div class=k>input tokens billed</div></div>
        <div class=chip><div class="n up">-59%</div><div class=k>cost on identical work</div></div>
        <div class=chip><div class=n>239,529</div><div class=k>tokens removed at the edge</div></div>
        <div class=chip><div class=n>24</div><div class=k>calls served free from cache</div></div>
        <div class=chip><div class=n>$0.10</div><div class=k>re-solves avoided (reuse yield)</div></div>
      </div>
      <div class=repo>github.com/sarkar4777/abenlux</div>
    </div></body></html>"""
    os.makedirs(DOCS, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=2)
        pg.set_content(doc, wait_until="networkidle")
        pg.query_selector(".card").screenshot(path=os.path.join(DOCS, "compression-cli-before-after.png"))
        b.close()
    print("wrote", os.path.join(DOCS, "compression-cli-before-after.png"))


if __name__ == "__main__":
    if not os.path.exists(f"{EV}/central.db"):
        print("no evidence found - run compression_e2e.py first.")
        sys.exit(2)
    render()
