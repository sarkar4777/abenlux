# Changelog

All notable changes to this project are documented here. This project adheres to semantic
versioning.

## [0.6.0] - 2026-06

Two more ways to spend fewer tokens, built on the edge gateway and the collector, proven across a team
of more than twenty five developers signing in both ways.

### Added
- Model routing (route.py + the gateway). An easy request is sent to a cheaper model, a real piece of
  work stays on the strong one. The decision is made on the device from the request alone, no extra
  model call, and is conservative so genuine work is never quietly handled by a weaker model.
  ABEN_ROUTE=on sends easy calls down for real, ABEN_ROUTE=shadow only measures what it would save. The
  realized or shadow dollars are re-derived authoritatively at the collector from the model names and the
  clamped token facts, never trusted from the edge.
- Team memory in shadow (teammemory.py + the collector ingest). A content-free index that, for each new
  request, finds a close earlier one from a different teammate in the same tenant and labels it serve (an
  almost identical ask in the same language, ready to reuse) or warm_start (the same task in another
  language). It changes no call and matches on the embedding only, never the prompt, so it records what a
  live team memory would save before anyone turns the live version on. Scoped to one tenant, so it never
  crosses an org wall.
- A coarse content-free language tag on each record, so team memory can tell a Python solution from a Go
  one.
- Routing yield and team-memory yield in the management report and the developer view, beside compression
  and reuse-yield, k-anonymity gated.
- A new end-to-end example (examples/routing-teammemory-e2e) that drives 34 developers across 5 tenants
  and 13 IDE and CLI tools, both sign-ins, and renders the developer CLI screenshots.
- Routing and the exact-match cache now run in the forward proxy too, so a subscription tool in an IDE
  gets both, not just a base-url key tool. Same on Windows, macOS and Linux.
- The wins reach the developer where they already are. A routed call and a cache hit are written to the
  private on-device feed and raised as a native desktop toast through the background agent, so a developer
  in their IDE or terminal never has to open a dashboard to see the saving.

## [0.5.0] - 2026-06

The forward proxy. One capture path that works no matter how a tool signs in, a company subscription or
a personal API key, and the only path that can compress a subscription tool's request on the wire. The
tool routes through the agent as an ordinary HTTPS proxy, the agent terminates TLS for the model API
hosts with a small local certificate it owns, redacts and compresses on the device, and forwards to the
real provider with the tool's own credential untouched. Everything else, the browser and every other
app, is tunnelled straight through unread, and the proxy is scoped to just the tool launched with
abenlux run. Because the saving now happens on the wire for any sign-in, a separate tool-output
compressor like RTK is no longer required for compression to work.

### Added
- Forward TLS-terminating proxy (capture/forward_proxy.py). A LocalCA mints a short-lived leaf
  certificate per model API host on demand, so the agent presents a trusted certificate, reads the
  request on the device, runs the same compress_request and the same gateway._capture pipeline the
  base-url gateway uses, and forwards to the real provider. It terminates TLS only for the known model
  hosts and tunnels every other host through unread.
- abenlux ca / run / proxy commands. ca prints the local certificate to trust once, run launches a tool
  routed through the proxy with the proxy and trusted certificate scoped to that one process tree, and
  proxy runs the forward proxy on its own for IT to push to every machine.
- x-aben-* attribution headers (actor, tool, branch, repo, ticket) are read in the forward proxy for
  attribution, then stripped before forwarding so they never reach the provider.
- Both-path proof (examples/proxy-suite-e2e). Drives six developers and tools across all three providers
  down both capture paths in one run, the base-url gateway and the forward proxy, each call with a real
  API key in its native header. 17 of 17 checks pass and a detailed REPORT.md is written, covering
  traffic isolation, capture, compression and savings, collaboration and reuse, value, renewal, and
  privacy. The isolation check proves a non-model site is tunnelled untouched while a model host is
  intercepted.
- Single-provider forward-proxy E2E against the real Anthropic (examples/proxy-e2e).

### Changed
- README and ARCHITECTURE lead with the forward proxy. The README hero states the any-sign-in promise,
  a new what-sets-it-apart row covers it, and the ARCHITECTURE data-flow diagram shows the forward proxy
  as a first-class capture path beside the base-url gateway and OTLP telemetry.

## [0.4.0] - 2026-06

The edge compression layer. A pluggable set of token savers that run on the outbound request at the
gateway, so every tool gets them with no per-tool setup, IDE or CLI, any provider. Safe-and-lossless
strategies run automatically; content-rewriting ones are one flag. Built to interoperate with and
credit the open tools that pioneered each lever (RTK, DocLang/Docling, Headroom, Bifrost Code Mode).

### Added
- Compression layer (compress/__init__.py). A registry of provider-aware Strategy objects that
  rewrite the outbound request body before it is forwarded. Two DX invariants: a strategy that errors
  is skipped so compression can never break a call, and only lossless non-content-rewriting strategies
  run by default.
- Prefix-Break Localizer (prefix_stabilize, auto-on). Moves an injected date/request-id out of the
  cache-stable prefix of the system prompt so the provider prompt cache hits. Lossless, just reordered.
- Opt-in content strategies via ABEN_COMPRESS (csv or all): command_trim (RTK-style command-output
  trimming: strip ANSI, collapse repeats, truncate), otsl_tables (HTML tables to compact OTSL),
  compress_json (minify embedded JSON), slim_tools (dedupe resent tool/function definitions).
- Exact-match request cache (ABEN_EXACT_CACHE, on). A byte-identical non-streamed repeat within a TTL
  is served from a bounded on-device LRU with no upstream call. The cached response stays on the
  device and is never forwarded; the served record is content-free and costs nothing.
- Compression-yield surfacing. DerivedRecord carries content-free saved_input_tokens, compression, and
  served_from_cache. The management report, CLI report, and dashboard show realized savings (tokens
  removed, calls served from cache) beside spend, never inside it.
- Per-strategy attribution. compress_request reports the tokens each strategy removed; the gateway
  records a content-free per-strategy map (compression_detail); the report, CLI and dashboard attribute
  the compression yield by technique. _body_tokens now counts tool/function definitions so slim_tools
  savings are no longer undercounted (#36).
- Real-model before/after harness (examples/compression-e2e): drives 24 developers x 5 multi-turn
  sessions through two gateways (compress off vs all) against real providers, prints a before/after
  table plus per-strategy attribution and a real prompt-cache A/B, and renders product screenshots.
- agent install wires RTK's tool-hook (rtk init -g) when RTK is present; RTK runs below abenlux so the
  two stack.

### Changed
- Collector _harden_inbound honors served_from_cache and does not re-price a free local-cache hit.

## [0.3.0] - 2026-06

Multi-tenancy, two new analytics features, a real-model test harness, and a large round of
adversarial-review fixes. License changed from Apache-2.0 to MIT.

### Added
- Multi-tenant plane. A tenant is an org unit or geography stamped content-free at the edge
  (ABEN_TENANT). Reports, budgets, and drift scope by tenant. Tenant registry with RBAC: create
  needs admin, list needs manager+, both scoped to the caller's own org. CLI tenant command, API
  /api/tenants, admin Tenants tab in the dashboard.
- Reuse-Yield Ledger. Books the avoided cost of re-solving work the org already solved, valued at
  the tenant winsorized-mean cost-to-solve, k-anonymity gated, recomputed live, shown beside spend.
  CLI savings line, API /api/savings.
- Cross-tenant Benchmark Exchange. Compares tenants of one org on ratios only, k-anon per tenant,
  Laplace DP, cohort threshold, percentile within the cohort. CLI benchmark command, API
  /api/benchmark.
- Finance cost export /api/export (CSV/JSON, k-gated) and a by-provider rollup for invoice
  reconciliation. me --today burn-rate and a calls per-call drill-down command.
- Multi-container real-model E2E (examples/multi-dev-e2e): collector plus one gateway per tenant,
  driven against OpenAI, Gemini, and Anthropic. Deep single-container E2E (examples/deep-e2e).

### Fixed
- Three rounds of multi-agent adversarial review and real-model testing. See the closed issues for
  the full list. Highlights: cross-org tenant hijack, reuse-yield over-crediting and book-time
  staleness, several sub-k aggregate leaks, the org and residency walls, gateway robustness, and a
  gzip-response capture bug that only shows against real providers.

### Changed
- License: Apache-2.0 to MIT.

## [0.2.0] - 2026-06

First public release.

### Capture
- Tier-1 OTLP ingest for traces AND logs (Claude Code emits message content as log events).
- Tier-2 stream-through gateway for Anthropic, OpenAI, and Google Gemini wire formats, with
  capture off the hot path via a background task.
- Tier-3 vendor admin connector (Cursor usage events to derived, metadata only).
- Resent-history detection across turns. Cross-platform tool detection. Per-tool/OS onboarding.

### Value and cost
- Cost model with current 2026 per-model rates, cache-aware, longest-prefix matching.
- Attribution by ticket/repo join with a confidence-gated semantic fallback. Orphan-spend metric.
- Objective budgets, run-rate forecast, and projected overrun.
- Spend drift detection (orphan-share and cost trend, window over window).

### Intelligence (what the spend is for)
- Purpose/work-type classification: branch convention -> keyword patterns + self-learned vocabulary
  -> a tiny optional LLM call. Net-new vs maintenance investment split and new-initiative detection.
- Self-learning loop: confident labels (branch or LLM) teach the free keyword layer, persisted on
  device and hot-reloaded, so the LLM fires less over time. No classification signal is wasted.
- Developer-local knowledge graph (`abenlux graph`): objectives, tickets, purpose, tools, models,
  and self-learned vocabulary, private and on-device.
- Optional minimal LLM classifier for OpenAI / Azure OpenAI / Anthropic / Gemini, with extractive
  prompt compression for long prompts and a cache, so cost is fractions of a cent at org scale.

### Developer experience
- Tool-agnostic private signal feed: retry loops, context bloat, answered-already, routing hints.
- Ambient delivery: native desktop toasts, `abenlux watch` live tail, `abenlux me` summary.
- Double-blind collaboration matching (live-duplication and solved-reuse) with mutual-consent reveal.

### Governance
- Edge redaction, derived-only persistence, HMAC pseudonyms, k-anonymity, DP noise.
- RBAC where no role can see another individual's rows. Role-aware web dashboard.
- Edge to central forwarding (batched, spooled) so the privacy model holds at org scale.

### Storage and ops
- SQLite (WAL) by default, optional Postgres backend selected by DSN.
- Docker compose, OTel collector config, 121 tests plus a browser UI test and real-SDK e2e.
