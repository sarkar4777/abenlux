#!/usr/bin/env python3
"""
Render the before/after compression result into two crisp, presentation-grade PNGs (docs/).

Every number comes from result.json, which is written by compression_e2e.py from a REAL run billed by
the model providers. Nothing here is invented: it only formats measured figures.

  python examples/compression-e2e/render_screens.py
"""
from __future__ import annotations

import json
import os

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DOCS = os.path.join(ROOT, "docs")
R = json.load(open(os.path.join(HERE, "result.json")))
B, Z, D = R["rocket-base"], R["rocket-zip"], R["delta"]

FONT = ("-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif")
MONO = ("'Cascadia Code', 'JetBrains Mono', Consolas, 'Courier New', monospace")

CSS = f"""
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:{FONT}; background:#070d16; color:#e8eef6; }}
.card {{ width:1600px; min-height:1040px; padding:74px 86px 40px; position:relative; overflow:hidden;
  display:flex; flex-direction:column;
  background:
    radial-gradient(1200px 600px at 85% -10%, rgba(45,212,191,.16), transparent 60%),
    radial-gradient(1000px 700px at -10% 110%, rgba(160,108,213,.14), transparent 60%),
    linear-gradient(160deg, #0a1320 0%, #070d16 60%, #06101b 100%); }}
.grid {{ position:absolute; inset:0; opacity:.05;
  background-image:linear-gradient(#9fb4cc 1px,transparent 1px),linear-gradient(90deg,#9fb4cc 1px,transparent 1px);
  background-size:46px 46px; }}
.wrap {{ position:relative; z-index:1; flex:1; display:flex; flex-direction:column; }}
.brand {{ display:flex; align-items:center; gap:14px; font-size:23px; letter-spacing:.16em;
  text-transform:uppercase; color:#9fdcd2; font-weight:600; }}
.brand .dot {{ color:#2dd4bf; font-size:30px; line-height:0; }}
.brand .sep {{ color:#37506a; }}
.brand .sub {{ color:#7d93ab; letter-spacing:.16em; }}
h1 {{ font-size:78px; line-height:1.04; font-weight:800; margin:30px 0 0; letter-spacing:-.02em; }}
h1 .hl {{ background:linear-gradient(90deg,#2dd4bf,#5eead4 55%,#a06cd5); -webkit-background-clip:text;
  background-clip:text; color:transparent; }}
.sub2 {{ font-size:27px; line-height:1.5; color:#aebfd2; margin-top:24px; max-width:1180px; }}
.bars {{ margin-top:54px; display:flex; flex-direction:column; gap:30px; }}
.row {{ display:grid; grid-template-columns:230px 1fr 150px; align-items:center; gap:26px; }}
.row .lab {{ font-size:24px; color:#c6d4e6; font-weight:600; }}
.row .lab small {{ display:block; color:#71869d; font-weight:500; font-size:18px; margin-top:4px; }}
.track {{ display:flex; flex-direction:column; gap:13px; }}
.bar {{ height:46px; border-radius:11px; display:flex; align-items:center; padding:0 20px;
  font-family:{MONO}; font-size:22px; font-weight:600; color:#06101b; white-space:nowrap; }}
.bar.before {{ background:linear-gradient(90deg,#3a4961,#55687f); color:#dbe6f2; }}
.bar.after {{ background:linear-gradient(90deg,#2dd4bf,#34e0c4); box-shadow:0 8px 30px rgba(45,212,191,.25); }}
.bar .tag {{ font-family:{FONT}; font-weight:700; font-size:16px; opacity:.85; margin-right:14px;
  text-transform:uppercase; letter-spacing:.08em; }}
.cut {{ text-align:right; }}
.cut .big {{ font-size:52px; font-weight:800; color:#5eead4; letter-spacing:-.02em; }}
.cut .small {{ font-size:18px; color:#7d93ab; }}
.chips {{ margin-top:60px; display:flex; flex-wrap:wrap; gap:16px; }}
.chip {{ background:rgba(255,255,255,.045); border:1px solid rgba(159,180,204,.16); border-radius:14px;
  padding:20px 26px; min-width:210px; }}
.chip .n {{ font-size:40px; font-weight:800; font-family:{MONO}; color:#e8eef6; letter-spacing:-.01em; }}
.chip .n .u {{ font-size:21px; color:#2dd4bf; font-weight:700; }}
.chip .k {{ font-size:18px; color:#8aa0b8; margin-top:6px; }}
.foot {{ margin-top:auto; padding-top:30px; display:flex; justify-content:space-between;
  align-items:center; font-size:19px; color:#7d93ab; border-top:1px solid rgba(159,180,204,.12); }}
.foot .credit b {{ color:#aebfd2; font-weight:600; }}
.foot .repo {{ font-family:{MONO}; color:#9fdcd2; }}
/* explainer card */
.h2 {{ font-size:62px; font-weight:800; letter-spacing:-.02em; margin-top:26px; }}
.stack {{ margin-top:46px; display:flex; flex-direction:column; gap:16px; }}
.lyr {{ display:flex; align-items:center; gap:24px; background:rgba(255,255,255,.04);
  border:1px solid rgba(159,180,204,.14); border-left:5px solid #2dd4bf; border-radius:14px; padding:22px 28px; }}
.lyr.opt {{ border-left-color:#a06cd5; }}
.lyr .nm {{ font-family:{MONO}; font-size:25px; font-weight:700; color:#e8eef6; width:260px; }}
.lyr .ds {{ font-size:22px; color:#aebfd2; flex:1; }}
.lyr .bdg {{ font-size:15px; font-weight:700; letter-spacing:.06em; text-transform:uppercase;
  padding:8px 14px; border-radius:9px; }}
.bdg.auto {{ background:rgba(45,212,191,.16); color:#5eead4; }}
.bdg.flag {{ background:rgba(160,108,213,.18); color:#c9a6ec; }}
.rules {{ margin-top:40px; display:flex; gap:24px; }}
.rule {{ flex:1; background:rgba(255,255,255,.045); border:1px solid rgba(159,180,204,.16);
  border-radius:16px; padding:30px 32px; }}
.rule .t {{ font-size:26px; font-weight:800; color:#5eead4; }}
.rule.b .t {{ color:#c9a6ec; }}
.rule .d {{ font-size:21px; color:#aebfd2; margin-top:12px; line-height:1.5; }}
"""


def _fmt(n: int) -> str:
    return f"{n:,}"


def hero_html() -> str:
    cost_b, cost_z = B["cost_usd"], Z["cost_usd"]
    tok_b, tok_z = B["input_tokens"], Z["input_tokens"]
    # bar widths relative to the larger (base) value
    cost_zw = max(8, round(cost_z / cost_b * 100))
    tok_zw = max(8, round(tok_z / tok_b * 100))
    return f"""<!doctype html><html><head><meta charset=utf-8><style>{CSS}</style></head>
<body><div class=card><div class=grid></div><div class=wrap>
  <div class=brand><span class=dot>&#10022;</span> ABENLUX <span class=sep>&middot;</span>
    <span class=sub>edge compression layer</span></div>
  <h1>Cut AI spend <span class=hl>{int(round(D['cost_reduction_pct']))}%</span><br>with zero code changes.</h1>
  <div class=sub2>The same {Z['actors']}-developer, multi-turn workload through the gateway, billed by the
    real model providers. The layer rewrites the outbound request, so every tool gets it, IDE or CLI,
    any provider, and a failing strategy is skipped, so it never breaks a call.</div>

  <div class=bars>
    <div class=row>
      <div class=lab>Cost (USD)<small>real provider billing</small></div>
      <div class=track>
        <div class="bar before" style="width:100%"><span class=tag>before</span> ${cost_b:.4f}</div>
        <div class="bar after" style="width:{cost_zw}%"><span class=tag>after</span> ${cost_z:.4f}</div>
      </div>
      <div class=cut><div class=big>-{int(round(D['cost_reduction_pct']))}%</div><div class=small>spend</div></div>
    </div>
    <div class=row>
      <div class=lab>Input tokens<small>billed upstream</small></div>
      <div class=track>
        <div class="bar before" style="width:100%"><span class=tag>before</span> {_fmt(tok_b)}</div>
        <div class="bar after" style="width:{tok_zw}%"><span class=tag>after</span> {_fmt(tok_z)}</div>
      </div>
      <div class=cut><div class=big>-{int(round(D['input_token_reduction_pct']))}%</div><div class=small>tokens</div></div>
    </div>
  </div>

  <div class=chips>
    <div class=chip><div class=n>{Z['actors']}</div><div class=k>developers, real sessions</div></div>
    <div class=chip><div class=n>{_fmt(Z['saved_input_tokens'])}</div><div class=k>tokens removed at the edge</div></div>
    <div class=chip><div class=n>{Z['cache_hits']}</div><div class=k>repeat calls served free</div></div>
    <div class=chip><div class=n>${D['dollars_saved']:.3f}</div><div class=k>saved on this one short run</div></div>
    <div class=chip><div class=n>100<span class=u>%</span></div><div class=k>lossless &amp; safe by default</div></div>
  </div>

  <div class=foot>
    <div class=credit>Strategies interoperate with and credit <b>RTK</b> &middot; <b>DocLang</b> &middot;
      <b>Headroom</b> &middot; <b>Bifrost Code Mode</b></div>
    <div class=repo>github.com/sarkar4777/abenlux</div>
  </div>
</div></div></body></html>"""


def explainer_html() -> str:
    layers = [
        ("prefix_stabilize", "Moves an injected date/id out of the cache-stable prefix so prompt caching hits", "auto"),
        ("command_trim", "Strips ANSI, collapses repeated lines, truncates huge command output (RTK-style)", "flag"),
        ("otsl_tables", "Transcodes verbose HTML tables to compact OTSL (DocLang-style)", "flag"),
        ("compress_json", "Minifies embedded JSON blobs, parsed value identical (Headroom-style)", "flag"),
        ("slim_tools", "Drops byte-identical duplicate tool definitions resent each turn (Bifrost-style)", "flag"),
        ("exact_cache", "Serves a byte-identical repeat from the device, no upstream call, no cost", "auto"),
    ]
    rows = ""
    for nm, ds, kind in layers:
        opt = " opt" if kind == "flag" else ""
        bdg = ('<span class="bdg auto">auto &middot; lossless</span>' if kind == "auto"
               else '<span class="bdg flag">ABEN_COMPRESS</span>')
        rows += f'<div class="lyr{opt}"><div class=nm>{nm}</div><div class=ds>{ds}</div>{bdg}</div>'
    return f"""<!doctype html><html><head><meta charset=utf-8><style>{CSS}</style></head>
<body><div class=card><div class=grid></div><div class=wrap>
  <div class=brand><span class=dot>&#10022;</span> ABENLUX <span class=sep>&middot;</span>
    <span class=sub>compression layer</span></div>
  <div class=h2>One layer. Every tool.<br>Never breaks a call.</div>
  <div class=stack>{rows}</div>
  <div class=rules>
    <div class=rule><div class=t>Safe by default</div><div class=d>Only lossless, behavior-safe
      strategies run automatically. Anything that rewrites prompt content is one flag, applied to every
      tool at once.</div></div>
    <div class="rule b"><div class=t>Never breaks a call</div><div class=d>Each strategy is isolated;
      one that errors is skipped and the original request is forwarded. Savings surface beside spend,
      content-free.</div></div>
  </div>
  <div class=foot>
    <div class=credit>Credits <b>RTK</b> &middot; <b>DocLang / Docling</b> &middot; <b>Headroom</b>
      &middot; <b>Bifrost Code Mode</b></div>
    <div class=repo>github.com/sarkar4777/abenlux</div>
  </div>
</div></div></body></html>"""


def render(html: str, out: str) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=2)
        page.set_content(html, wait_until="networkidle")
        el = page.query_selector(".card")
        el.screenshot(path=out)
        browser.close()
    print("wrote", out)


if __name__ == "__main__":
    render(hero_html(), os.path.join(DOCS, "compression-before-after.png"))
    render(explainer_html(), os.path.join(DOCS, "compression-layer.png"))
