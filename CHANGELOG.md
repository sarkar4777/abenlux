# Changelog

All notable changes to this project are documented here. This project adheres to semantic
versioning.

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
