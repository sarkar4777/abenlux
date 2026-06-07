<div align="center">

# ✦ Abenlux

### The AI spend → value attribution plane

**See where every AI token goes, know *what it was for*, tie it to a business objective, catch budget
overruns before they happen, and keep developers private from management — across every IDE and CLI
coding tool. It even learns your team's intent vocabulary so it gets smarter and cheaper over time.**

[![CI](https://github.com/sarkar4777/abenlux/actions/workflows/ci.yml/badge.svg)](https://github.com/sarkar4777/abenlux/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![tests](https://img.shields.io/badge/tests-156%20passing-brightgreen.svg)](tests/)
[![privacy](https://img.shields.io/badge/privacy-edge--redacted%20%C2%B7%20k--anon%20%C2%B7%20RBAC-success.svg)](CRITIQUE.md)

</div>

---

`aben` + *lux* — it puts light on where AI tokens go. Abenlux captures token usage from Claude Code,
Codex, Gemini CLI, Cursor, Copilot, aider, Cline, Continue, opencode, Crush, Pi, Droid and more,
normalizes it to one schema, **attributes spend to a business objective by a join (not a guess)**,
classifies **what the spend is for** (new feature vs bug fix vs refactor), prices it in dollars, runs
**objective budgets with forecast and drift alerts**, and **learns your team's intent vocabulary** so
classification gets smarter and nearly free. Every prompt is redacted on the developer's own machine,
and management only ever sees privacy-preserving aggregates.

> **What no other tool does:** objective-tied budget guardrails that warn the **developer** privately
> while management sees only k-anonymized aggregates, with **purpose traceability** (net-new vs
> maintenance) and a **self-learning local knowledge graph** — across **every** coding tool. See
> [CRITIQUE.md](CRITIQUE.md) for the honest competitive analysis and limits.

<div align="center">
<img src="docs/dashboard-management.png" alt="Abenlux management dashboard" width="900">
<br><em>Real captured data: spend → value, budgets with forecast, what the AI spend is for
(net-new build vs maintenance), new initiatives, orphan spend, drift — all k-anonymized.</em>
</div>

---

## Killer features

| | |
|---|---|
| 🎯 **Spend → value by join** | Branch/ticket → objective via your knowledge graph. No ML, fully auditable. Repo-join and a confidence-gated semantic fallback follow. Unmatched spend is **orphan spend**, the headline waste metric. |
| 🧭 **Purpose traceability** | Every dollar is labelled with *what it's for* — feature, fix, refactor, perf, exploration, chore, docs, test — and split into **net-new build vs maintenance**. Traced to the ticket. |
| 🆕 **New-initiative radar** | Detects new apps/features that started consuming AI spend this period, with the work type and trace. |
| 🧠 **Self-learning** | Every confident label (branch ground-truth or the LLM) teaches a free keyword layer, so the system classifies more for free and the LLM fires less over time. No signal is wasted. |
| 🕸 **Developer-local knowledge graph** | Each developer owns a private, on-device graph of their objectives, tickets, purpose mix, tools, and self-learned vocabulary. View it anytime with `abenlux graph`. |
| 💸 **Budgets + forecast + guardrails** | Per-objective ceilings, run-rate projection, projected overrun, and a **private** nudge to the developer when their objective is over/at-risk. |
| 📈 **Drift** | Window-over-window orphan-share and cost trend with alerts — the early warning before the quarterly bill. |
| 🤝 **Double-blind collaboration** | Live-duplication and solved-reuse matches across developers, Chinese-wall + residency enforced, identities revealed only on mutual consent. Never a manager-visible report. |
| 🔔 **Ambient developer signals** | Waste/collab/budget nudges via native desktop toast, `abenlux watch`, and `abenlux me` — wherever the developer is, no browser. |
| 🔐 **Governance as code** | Edge redaction, derived-only persistence, HMAC pseudonyms, k-anonymity, DP noise, and RBAC where **no role — not even admin — can see another individual's rows**. |
| 🪶 **Minimal, optional LLM** | When branch + patterns + learned vocabulary all miss, one tiny cached call (OpenAI / Azure / Claude / Gemini) with **extractive prompt compression** classifies intent for fractions of a cent. |

---

## Quickstart (60 seconds, no API keys)

```bash
git clone https://github.com/sarkar4777/abenlux
cd abenlux
make install          # pip install -e ".[dev]"
make demo             # one exchange through the full edge pipeline, offline
make test             # 156 tests
```

`make demo` redacts a secret, reassembles a streamed response, prices it, attributes it to an
objective, classifies its purpose, pseudonymizes the actor, and prints the only thing that would ever
persist: a content-free `DerivedRecord`.

Verify any real tool without spending tokens:

```bash
abenlux mock                                   # protocol-correct fake upstream (terminal A)
ABEN_ANTHROPIC_UPSTREAM=http://127.0.0.1:9111 abenlux gateway   # terminal B
ANTHROPIC_BASE_URL=http://127.0.0.1:8088 <your tool>            # terminal C
abenlux me            # your call shows up, privately
abenlux graph         # your personal on-device knowledge graph
```

---

## 👩‍💻 For developers: download and configure (separate from the management UI)

A developer installs **one** thing — the edge agent — and never needs the dashboard. It runs locally,
redacts on the device, and (in an org) forwards only content-free records to the collector.

```bash
pipx install abenlux            # or: pip install abenlux

# in an organization, point the edge agent at your collector (values from IT):
export ABEN_COLLECTOR_URL=https://abenlux.mycorp.internal
export ABEN_INGEST_TOKEN=<device token>
export ABEN_HMAC_KEY=<org pseudonymization key>

abenlux gateway                 # loopback capture agent on :8088
abenlux onboard <tool>          # prints the exact env/config for YOUR tool + shell
```

Then point your tool at the agent (`abenlux onboard` gives the exact line):

| Your tool | How |
|---|---|
| Claude Code, Codex, Gemini CLI | Tier 1 — export OTel to the agent (`OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8088`) |
| aider, opencode, Crush, Pi, Droid, ForgeCode | `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` → `http://127.0.0.1:8088` |
| Cline, Continue, Roo, Kilo (IDE) | set the provider Base URL in extension settings to the agent |
| Cursor agent, Copilot inline | Tier 3 — captured via the vendor admin API on the collector (no local content) |

Everything the developer sees is **private to them**, never the management plane:

```bash
abenlux me        # your spend + waste/collab nudges
abenlux watch     # live ambient tail in a spare terminal pane
abenlux graph     # your local knowledge graph: objectives, tickets, purpose, learned vocabulary
```

Native desktop toasts fire automatically when a nudge happens. Full per-tool/per-OS guide:
**[DEPLOYMENT.md](DEPLOYMENT.md)**.

### Make it ultra-smart (optional, almost free)

Point Abenlux at whatever LLM your org already uses to classify intent on the rare prompts that branch
and keyword patterns miss. It uses a cheap model, a 5-token reply, extractive compression of long
prompts, and a cache — fractions of a cent at org scale — and it *teaches the free layer* so it fires
less over time. Standard env names work out of the box:

```bash
# OpenAI / Azure OpenAI / Anthropic / Gemini — pick one
export LLM_PROVIDER=azure
export AZURE_OPENAI_API_BASE=...   AZURE_OPENAI_API_KEY=...   AZURE_OPENAI_DEPLOYMENT_FAST=gpt-4o
```

---

## 🏢 For IT: install the collector + dashboard

```bash
ABEN_HMAC_KEY=<org key> ABEN_DB=<sqlite or postgres dsn> ABEN_KG=knowledge_graph.yaml \
ABEN_PRINCIPALS=principals.yaml ABEN_INGEST_TOKEN=<device token> \
abenlux serve --host 0.0.0.0 --port 8090
```

Open `http://<host>:8090/` and sign in. Managers/finance see the k-anonymized spend→value dashboard
(budgets, purpose, new initiatives, drift, tool/model mix). Admins manage the knowledge graph. **No
role can see an individual's rows.** A *central* gateway would see raw prompts before redaction — so
the edge agent redacts on the device and forwards only the content-free `DerivedRecord`.

---

## Capture is tiered by how each tool actually makes its call

`abenlux tiers` prints the live matrix. We never present metadata-only data as full-content data.

| Tier | How | Tools | Full prompt | Exact tokens |
|---|---|---|:--:|:--:|
| **1 — OTel-native** | tool self-instruments to OTel GenAI | Claude Code, Codex, Gemini CLI, Copilot agent | ✅ opt-in | ✅ |
| **2 — Gateway** | tool honors a custom `base_url` | aider, Cline, Continue, opencode, Crush, Pi, Droid, ForgeCode, Roo, Goose, Kilo | ✅ | ✅ |
| **3 — Vendor API** | prompt assembled server-side | Cursor agent, Copilot inline, Windsurf, Amazon Q | ❌ | metadata |

**Tier 3 is a ceiling, not a bug.** Cursor and Copilot build the prompt on their own backend, so the
real prompt never exists on the device. The only legitimate signal is the vendor's admin API.

---

## How the intelligence works

```
purpose of spend  =  branch convention   (free, auditable: feature/ fix/ spike/ ...)
                  →  keyword patterns + the device's self-learned vocabulary   (free)
                  →  one tiny LLM call on an extractively-compressed prompt    (rare, cached, ~5 tokens)
                  ↑
                  every confident label feeds the learner, so the free layers get smarter over time
```

The privacy posture *is* the pipeline order, run **on the device**:

```
capture (full content, in-flight only)
  → REDACT        destroy secrets/PII before anything is written or derived
  → DERIVE        embedding + token facts + cost + waste + purpose  (vectors and labels, not text)
  → ATTRIBUTE     join work-context → objective, semantic fallback, flag orphan
  → PSEUDONYMIZE  one-way HMAC the actor, drop the raw id
  → PERSIST       the DerivedRecord only, raw content is discarded here
  → FORWARD       ship the content-free DerivedRecord to the central collector
```

There is no central, management-readable store of anyone's prompts. That asset never exists. Details:
**[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Built for thousands of developers

- **Storage:** SQLite (WAL) by default, **optional Postgres** (`pip install abenlux[postgres]`, point
  `ABEN_DB` at a `postgresql://` DSN). Schema self-migrates on open.
- **Forwarding:** the edge batches + spools derived records, retries on collector outage, dedups by
  event id — at-least-once delivery, and a collector blip never breaks a developer's call.
- **Privacy at scale:** k-anonymity (k≥5) suppression, DP noise on org totals, per-device ingest
  tokens, RBAC enforced server-side.
- **Cross-platform:** Windows, macOS, Linux. CI runs the suite on all three across Python 3.10-3.13.

---

## Command reference

```
abenlux demo / gateway / serve / mock      run the pipeline / edge agent / collector / fake upstream
abenlux onboard <tool>                     exact setup for a tool on your OS/shell
abenlux tiers                              the tool capability matrix
abenlux cost <model>                       price an interaction
abenlux report                             management spend→value report (k-anonymity gated)
abenlux me / watch                         your own private spend + nudges (summary / live tail)
abenlux graph [--json]                     your developer-local knowledge graph
abenlux detect / sync-cursor               detected tool / pull Tier-3 Cursor usage (metadata only)
```

---

## Testing

156 unit + integration tests, including an **exhaustive multi-user org simulation**, the **real
Anthropic and OpenAI SDKs driven through a live gateway**, a self-learning loop test, and a Playwright
browser test of every dashboard screen and role.

```bash
make test       # 156 tests
make lint       # ruff (incl. no-semicolon style)
```

## Docs

- **[DEPLOYMENT.md](DEPLOYMENT.md)** — developer install vs IT install, every tool, every OS
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — module map, data flow, the five invariants
- **[CRITIQUE.md](CRITIQUE.md)** — what no tool does, competitive comparison, honest gaps
- **[CONTRIBUTING.md](CONTRIBUTING.md)** · **[SECURITY.md](SECURITY.md)** · **[CHANGELOG.md](CHANGELOG.md)**

## License

Apache 2.0. See [LICENSE](LICENSE).
