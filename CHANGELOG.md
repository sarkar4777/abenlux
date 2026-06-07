# Changelog

All notable changes to this project are documented here. This project adheres to semantic
versioning.

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
