<div align="center">

# ✦ Abenlux

### The AI spend → value attribution plane

**See where every AI token goes, tie it to a business objective, catch budget overruns before they
happen, and keep developers private from management — across every IDE and CLI coding tool.**

[![CI](https://github.com/sarkar4777/abenlux/actions/workflows/ci.yml/badge.svg)](https://github.com/sarkar4777/abenlux/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![tests](https://img.shields.io/badge/tests-121%20passing-brightgreen.svg)](tests/)
[![privacy](https://img.shields.io/badge/privacy-edge--redacted%20%C2%B7%20k--anon%20%C2%B7%20RBAC-success.svg)](CRITIQUE.md)

</div>

---

`aben` + *lux* — it puts light on where AI tokens go. Abenlux captures token usage from Claude Code,
Codex, Gemini CLI, Cursor, Copilot, aider, Cline, Continue, opencode, Crush, Pi, Droid and more,
normalizes it to one schema, **attributes spend to an objective by a join (not a guess)**, prices it
in dollars, and runs **objective budgets with run-rate forecast and drift alerts** — while every
prompt is redacted on the developer's own machine and management only ever sees privacy-preserving
aggregates.

> **What no other tool does:** objective-tied budget guardrails that warn the **developer** privately
> while management sees only k-anonymized aggregates, across **every** coding tool. LLM gateways
> enforce budgets by API key at your app boundary. Cursor/Copilot analytics are single-vendor and
> manager-facing. Abenlux fuses cross-tool capture + value attribution + co-determination-grade
> privacy. See [CRITIQUE.md](CRITIQUE.md) for the honest competitive analysis and limits.

<div align="center">
<img src="docs/dashboard-management.png" alt="Abenlux management dashboard" width="900">
<br><em>Management view: spend → value, budgets with forecast, orphan spend, drift, all k-anonymized.</em>
</div>

---

## Quickstart (60 seconds, no API keys)

```bash
git clone https://github.com/sarkar4777/abenlux
cd abenlux
make install          # pip install -e ".[dev]"
make demo             # one exchange through the full edge pipeline, offline
make test             # 121 tests
```

`make demo` redacts a secret, reassembles a streamed response, prices it, attributes it to an
objective by ticket join, pseudonymizes the actor, and prints the only thing that would ever persist:
a content-free `DerivedRecord`.

Want to see it with a real tool but spend zero tokens?

```bash
abenlux mock                                   # protocol-correct fake upstream (terminal A)
ABEN_ANTHROPIC_UPSTREAM=http://127.0.0.1:9111 abenlux gateway   # terminal B
ANTHROPIC_BASE_URL=http://127.0.0.1:8088 <your tool>            # terminal C
abenlux me                                     # your call shows up, privately
```

---

## How developers are informed — without opening anything

The waste, correction, and collaboration signals reach the developer **where they already work**.
The dashboard is optional. Four ambient channels, all private to the developer, none visible to
management:

| Channel | What it is |
|---|---|
| 🔔 **Desktop toast** | native OS notification fires the moment a nudge happens (Windows/macOS/Linux), debounced |
| 📟 `abenlux watch` | live terminal tail of your private feed — keep it in a spare pane |
| 📄 `abenlux me` | on-demand summary of your spend + recent nudges |
| 🧩 feed file | `~/.abenlux/feed.jsonl` is the integration contract for an IDE status-bar/extension |

```
[retry]   (aider)       This looks close to your last try. Add a failing test instead of re-running.
[budget]  (claude-code) The Acme - Checkout Platform AI budget is 96% spent and on track for ~190%.
[collab]  (cline)       A colleague is on 'Temporal saga for the checkout approval workflow'. Intro?
```

---

## Two things to install (and why the split matters)

A *central* gateway would see every developer's raw prompt before redaction. So Abenlux splits:

```
 developer's machine                                  IT-managed central host
 ───────────────────                                  ───────────────────────
 tool ──base_url──▶ Abenlux edge agent                Abenlux collector + API + dashboard
                    (abenlux gateway, loopback)       (abenlux serve)
                    redact → derive → pseudonymize     receives ONLY content-free DerivedRecords
                    full prompt exists ONLY here  ───▶ RBAC: managers see k-anon aggregates,
                    private feed stays here too        no individual rows, ever
```

- **Developers** run `pip install abenlux` + `abenlux gateway`, then `abenlux onboard <tool>` prints
  the exact one-liner for their tool on their OS/shell.
- **IT** runs `abenlux serve` (the collector + dashboard) behind TLS/SSO.

Full guide, per-tool and per-OS: **[DEPLOYMENT.md](DEPLOYMENT.md)**.

---

## Capture is tiered by how each tool actually makes its call

`abenlux tiers` prints the live matrix. We never present metadata-only data as full-content data.

| Tier | How | Tools | Full prompt | Exact tokens |
|---|---|---|:--:|:--:|
| **1 — OTel-native** | tool self-instruments to OTel GenAI | Claude Code, Codex, Gemini CLI, Copilot agent | ✅ opt-in | ✅ |
| **2 — Gateway** | tool honors a custom `base_url` | aider, Cline, Continue, opencode, Crush, Pi, Droid, ForgeCode, Roo, Goose, Kilo | ✅ | ✅ |
| **3 — Vendor API** | prompt assembled server-side | Cursor agent, Copilot inline, Windsurf, Amazon Q | ❌ | metadata |

**Tier 3 is a ceiling, not a bug.** Cursor and Copilot build the prompt on their own backend, so the
real prompt never exists on the device. The only legitimate signal is the vendor's admin API (usage,
no content). Abenlux is built around that limit instead of pretending it away.

---

## What you get

- **Spend → value, by join.** `feature/ACME-1234` → objective via your knowledge graph. No ML, fully
  auditable. Repo-join and a confidence-gated semantic fallback follow. Everything unmatched is
  **orphan spend**, the headline waste metric.
- **A real cost model.** Current 2026 per-model rates, cache-aware, longest-prefix so a point release
  inherits its family price. Unknown models are flagged `unpriced`, never silently zeroed.
- **Budgets, forecast, guardrails.** Per-objective ceilings, run-rate projection to period end,
  projected overrun, and a private developer nudge when their objective is over or at risk.
- **Drift.** Window-over-window orphan-share and cost trend with alerts — the early warning before
  the quarterly bill.
- **Developer-private signals, tool-agnostic.** Retry loops, resent-history bloat, answered-already,
  routing hints — identical whether the call came from Claude Code, aider, or a Cursor usage event.
- **Collaboration, double-blind.** Live-duplication and solved-reuse matches, Chinese-wall and
  residency enforced, identities revealed only on mutual consent. Never a manager-visible report.
- **Governance as code.** RBAC where no role, not even admin, can see another individual's rows.
  Managers get only k-anonymized, DP-noised aggregates.

---

## The privacy posture *is* the pipeline order (run on the device)

```
capture (full content, in-flight only)
  → REDACT        destroy secrets/PII before anything is written or derived
  → DERIVE        embedding + token facts + cost + waste signals  (vectors and counts, not text)
  → ATTRIBUTE     join work-context → objective, semantic fallback, flag orphan spend
  → PSEUDONYMIZE  one-way HMAC the actor, drop the raw id
  → PERSIST       the DerivedRecord only, raw content is discarded here
  → FORWARD       ship the content-free DerivedRecord to the central collector
```

There is no central, management-readable store of anyone's prompts. That asset never exists. The
separation is enforced by `auth/rbac.py` and by where the bytes physically live, not a feature toggle.
Architecture and data flow: **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Built for thousands of developers

- **Storage:** SQLite (WAL) by default for zero config, **optional Postgres** for scale —
  `pip install abenlux[postgres]` and point `ABEN_DB` at a `postgresql://` DSN.
- **Forwarding:** the edge batches and spools derived records, retries on collector outage, and the
  collector dedups by event id, so delivery is at-least-once and a collector blip never breaks a
  developer's call.
- **Privacy at scale:** k-anonymity (default k≥5) suppresses small groups, DP noise on org totals,
  per-device ingest tokens, RBAC enforced server-side.
- **Cross-platform:** Windows, macOS, Linux. CI runs the suite on all three across Python 3.10-3.13.

---

## Command reference

```
abenlux demo              run the full edge pipeline offline
abenlux gateway           start the edge capture agent (Tier-2 proxy + Tier-1 OTLP) on :8088
abenlux serve             start the collector + API + dashboard on :8090
abenlux onboard <tool>    print exact setup for a tool on your OS/shell
abenlux tiers             the tool capability matrix
abenlux cost <model>      price an interaction
abenlux report            management spend→value report (k-anonymity gated)
abenlux me                your own private spend + recent nudges
abenlux watch             live ambient tail of your private signals
abenlux detect            which AI tool the agent detects here
abenlux mock              protocol-correct fake upstream for token-free verification
abenlux sync-cursor       pull Tier-3 Cursor usage (metadata only)
```

---

## Testing

121 unit + integration tests, plus a real-SDK end-to-end (the genuine Anthropic and OpenAI SDKs
driven through a live gateway) and a Playwright browser test of every dashboard screen and role.

```bash
make test       # 121 tests
make lint       # ruff (incl. no-semicolon style)
```

---

## Docs

- **[DEPLOYMENT.md](DEPLOYMENT.md)** — developer install vs IT install, every tool, every OS
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — module map, data flow, the five invariants
- **[CRITIQUE.md](CRITIQUE.md)** — what no tool does, competitive comparison, honest gaps
- **[CONTRIBUTING.md](CONTRIBUTING.md)** · **[SECURITY.md](SECURITY.md)** · **[CHANGELOG.md](CHANGELOG.md)**

## License

Apache 2.0. See [LICENSE](LICENSE).
