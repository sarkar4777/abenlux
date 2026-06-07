# Critical view: what Abenlux does that nothing else does, and where the limits are

This is the honest section. It states the one thing Abenlux uniquely combines, compares it to the
tools that overlap, and lists the gaps we have not closed so deployers go in with eyes open.

## The thing no other tool does

Three capabilities exist separately in the market. Abenlux is the only thing that fuses all three:

1. **Cross-tool coding-assistant capture** (Claude Code, Codex, Gemini CLI, aider, Cline, Continue,
   Cursor, Copilot, opencode, Crush, Pi, Droid, ...) normalized to one schema.
2. **Spend attributed to a business objective by a work-context join** (branch/ticket/repo), priced
   in dollars, with orphan spend, budgets, run-rate forecast, and drift.
3. **Co-determination-grade privacy**: redaction on the device, derived-only persistence,
   k-anonymity, and RBAC where no role can see an individual, with the developer-facing waste,
   collaboration, and budget signals delivered ambiently to the developer and invisible to managers.

Put plainly: **objective-tied budget guardrails that warn the developer privately while management
sees only k-anonymized aggregates, across every coding tool.** Nobody ships that.

## Honest competitive comparison

| Capability | LLM gateways (LiteLLM/Portkey/Bifrost/Helicone) | Coding analytics (Cursor/Copilot) | LLM observability (Langfuse/Datadog) | Cloud FinOps (Vantage/CloudZero) | **Abenlux** |
|---|---|---|---|---|---|
| Covers IDE/CLI coding tools | no (your app's API calls) | single-vendor only | no | no | **yes, all tiers** |
| Attributes to a business objective by join | no (by API key/team) | no | no | by cloud tag | **yes (ticket/repo)** |
| Budgets + forecast | yes (by key/team) | partial | no | yes | **yes, by objective** |
| Budget warning to the *developer*, privately | no (admin reject) | no | no | no | **yes (toast/CLI)** |
| Edge redaction, no central prompt store | no | n/a | no (stores traces) | n/a | **yes** |
| No individual visible to management | no | no (per-dev dashboards) | no | n/a | **yes (RBAC)** |
| Double-blind collaboration matching | no | no | no | no | **yes** |

The gateways are excellent at what they do: real-time budget enforcement at an application's API
boundary, keyed by virtual API key. That is the wrong altitude for *developers using coding
assistants*, and it is management-shaped. Abenlux sits on the developer's machine, joins to the work
item, and keeps the individual private. (Sources: FinOps Foundation State of FinOps 2026, Vantage and
finout FinOps-for-AI guidance, getmaxim/LiteLLM/Portkey gateway docs, Cursor Admin/Analytics API.)

## Critical gaps and limits (what we did NOT solve)

- **Value numerator is partial.** We attribute *spend* to objectives precisely. The "value" side
  (cost per PR merged, acceptance = code kept, quality) is scaffolded in the schema
  (`quality_score`, `acceptance`) but not populated. Real ROI needs a commit/PR outcome signal wired
  in. This is the biggest honest gap and the top roadmap item.
- **Tier-3 is metadata-only, forever.** Cursor agent and Copilot inline assemble the prompt
  server-side. We capture usage via the vendor admin API and never the content. This is a ceiling,
  not a bug, and the dashboard labels it as such.
- **Semantic attribution is only as good as the embedder.** Offline it falls back to a hashing
  embedder (lexical). For real semantic nearest-objective, install `[ml]` (sentence-transformers).
  Join-based attribution (the default) needs no ML and is the defensible path.
- **Budgets are not k-anonymized.** Objective-level budget rows can reflect a small team. They are
  objective aggregates, not per-person, but a deployer with very small teams should treat them as
  semi-sensitive. The per-developer rollups ARE k-gated.
- **Pseudonymization is not unlinkability.** A stable pseudonym supports longitudinal analysis; a
  determined insider with the HMAC key and a known email list could re-link. The mitigation is key
  custody (analytics plane cannot read the key), not cryptographic unlinkability.
- **Static principals / single-process broker in the scaffold.** Production swaps the YAML principals
  for SSO/OIDC and the in-process collaboration broker for a privacy-preserving service. The seams
  are there; the hardened services are deployer-supplied.
- **Postgres backend is implemented but not integration-tested here** against a live server (the
  adapter and SQL translation are unit-tested with a fake connection). Run it against a real
  Postgres in staging before trusting it at scale.
- **Notification UX is OS-toast + terminal.** A first-class IDE status-bar/inline surface (VS Code,
  JetBrains) is the natural next step; today the integration contract is the on-device feed file.

## Where this is genuinely strong

Privacy-by-construction (not policy), the honest tier model, exact token/cost semantics per provider,
the developer-private ambient signals, and objective budgets with forecast and drift. Those are the
parts to build a company on.
