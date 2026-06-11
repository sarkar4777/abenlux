# LinkedIn post (real numbers from the 24-developer run)

Numbers below are measured, billed by the real providers (Claude + Gemini). Reproduce from
examples/compression-e2e. Screenshots: docs/compression-before-after.png and
docs/compression-dashboard.png.

---

We put a compression layer in front of our AI coding tools and cut token spend 60% with zero code changes.

Here is the actual test, no mock, no projection.

24 developers. 5-turn coding sessions with the messy stuff people really paste: build logs, test
output, JSON config, a requirements doc full of tables. Real calls to Claude and Gemini, billed by the
providers. The exact same workload sent two ways: straight through, and through one gateway with the
compression layer on.

What came back:

- Input tokens billed: 527,229 down to 207,300. That is 61% fewer.
- Cost on the same work: $0.47 down to $0.19. Down 59%.
- 239,529 input tokens removed before they ever left the machine.
- 24 repeat calls served free from a local cache, with no upstream call at all.

Where the savings came from, attributed by strategy right in the dashboard:

- Trimming command output (the build and test logs): the big one by far.
- OTSL for tables and JSON minification for config blobs: steady wins on every document-heavy turn.
- An exact-match cache for byte-identical repeats.
- A prefix localizer that moves the per-call timestamp out of your cache-stable prefix. In a direct
  A/B that turned 0 cached tokens into 5,041 read from cache on the very next call.

The part that matters most: it runs at the gateway, so every tool gets it. Claude Code, aider, Cline,
Gemini CLI, whatever the developer uses, IDE or CLI, no per-tool setup and no context switching. Safe
lossless strategies run automatically. Anything that rewrites your prompt is one flag. A strategy that
errors is skipped, so it can never break a call.

We did not invent these techniques. The layer credits and interoperates with the open tools that
pioneered each one: RTK, DocLang/Docling, Headroom, and Bifrost Code Mode.

And the savings sit next to spend in the dashboard, attributed by strategy and k-anonymized, so
management sees aggregates and never an individual developer.

Built on Abenlux. 278 tests. MIT licensed.
github.com/sarkar4777/abenlux

#AI #LLM #DeveloperTools #Engineering #CostOptimization #LLMOps
