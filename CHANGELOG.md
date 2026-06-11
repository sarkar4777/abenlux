# Changelog

All notable changes to this project are documented here. This project adheres to semantic
versioning.

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
