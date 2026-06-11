# Compression layer: real-model before/after

A whole-stack proof of the edge compression layer, run against **real model providers** (no mock).
It stands up the collector and **two** edge gateways and drives the **same** 12-developer, multi-turn
workload through both:

- `rocket-base` with `ABEN_COMPRESS=off` and `ABEN_EXACT_CACHE=0` (a plain pass-through proxy)
- `rocket-zip` with `ABEN_COMPRESS=all` and `ABEN_EXACT_CACHE=1` (the compression layer on)

Every figure is billed by the real provider: the gateway forwards the (compressed or not) request
upstream, the provider counts the real input tokens, and the content-free record is forwarded to the
collector. The script then reads the collector store and prints a before/after table.

## Run it

Keys are read from the environment only and never written to disk.

```
ANTHROPIC_API_KEY=...  GEMINI_API_KEY=...  python examples/compression-e2e/compression_e2e.py
python examples/compression-e2e/render_screens.py
```

The first command writes `result.json`. The second renders `docs/compression-before-after.png` and
`docs/compression-layer.png` from those measured numbers (nothing is invented).

## What the workload exercises

- A noisy build log with ANSI color and a warning repeated a couple hundred times -> `command_trim`.
- A large pretty-printed JSON config -> `compress_json` (the parsed value is identical).
- A verbose HTML results table -> `otsl_tables` (cells preserved).
- A volatile session id injected at the top of the system prompt -> `prefix_stabilize`.
- Turn 3 of each session is byte-identical to turn 2 -> the exact-match cache serves it for free on the
  zip side, while the base side pays for it again.

## A measured run (24 developers, 5 turns each, 120 real calls per side)

| metric | rocket-base (off) | rocket-zip (on) |
|---|--:|--:|
| input tokens billed | 527,229 | 207,300 |
| exact-cache hits (free) | 0 | 24 |
| tokens removed at the edge | 0 | 239,529 |
| cost (USD) | $0.4717 | $0.1922 |

Input tokens cut **61%**, cost cut **59%**, on the same workload with no change to any tool.

The run also prints a per-strategy attribution table and a real Anthropic prompt-cache A/B. In that A/B,
leaving the injected timestamp in the cache-stable prefix returned **0** cache-read tokens on the next
call, while moving it out (what `prefix_stabilize` does) returned **5,041**. Your own numbers will vary
with the shape of your traffic.

## Screenshots

```
python examples/compression-e2e/render_screens.py     # docs/compression-before-after.png + compression-layer.png
python examples/compression-e2e/render_dashboard.py   # docs/compression-dashboard.png + compression-developer.png
```

`render_dashboard.py` boots a real collector against the snapshot in `evidence/` and screenshots the
actual product dashboard, so the management (compression yield + per-strategy attribution) and
developer (collaboration) views are genuine tool output, not mockups.
