# Deploying Abenlux in an organization

There are exactly **two things to install**, and keeping them separate is the whole privacy
story. Read this section first — it answers "what does the developer download, and what does IT
run so management can see the numbers?"

```
   ┌─────────────────────────── developer's machine (Windows / macOS / Linux) ───────────────────────────┐
   │                                                                                                      │
   │   IDE / CLI tool ──base_url──▶  Abenlux Edge Agent  ──redact→derive→pseudonymize (ON DEVICE)──┐      │
   │   (Claude Code, aider,          (`abenlux gateway`, loopback :8088)                            │      │
   │    Cline, Cursor*, pi, …)       • full prompt exists ONLY here, in memory, never written       │      │
   │                                 • developer-private waste/collab feed stays here too            │      │
   └──────────────────────────────────────────────────────────────────────────────────────────────│──────┘
                                                                                                    │
                                                       ONLY content-free DerivedRecords (HTTPS) ────┘
                                                                                                    ▼
   ┌─────────────────────────────────── IT-managed central host ───────────────────────────────────────┐
   │   Abenlux Collector + API + Dashboard  (`abenlux serve`, :8090)                                     │
   │   • receives DerivedRecords at /v1/derived (device token)   • RBAC: managers see k-anon aggregates  │
   │   • derived warehouse (Postgres/BigQuery in prod)           • NO content, NO individual rows ever   │
   └────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

`*` Cursor agent / Copilot inline assemble the prompt on the vendor's backend, so there is nothing
to intercept locally — those are captured by pulling the **vendor admin API** into the collector
(`abenlux sync-cursor`), usage metadata only.

## Why the split is non-negotiable

A *central* gateway would see every developer's raw prompt before redaction — turning an
observability tool into a content-exfiltration pipeline. So redaction happens **on the device**, in
the edge agent, and only the `DerivedRecord` (vectors, token counts, cost, objective id — no text,
no name) crosses the network. The collector has no endpoint that accepts raw prompts, because raw
prompts are destroyed before they could be sent.

---

## 1. What the developer installs — the Edge Agent

One package, one command, runs locally. Cross-platform (pure Python).

```
pip install abenlux                      # or pipx install abenlux
# point the edge agent at your org's collector so it forwards derived records:
#   ABEN_COLLECTOR_URL=https://abenlux.mycorp.internal
#   ABEN_INGEST_TOKEN=<device token from IT>
#   ABEN_HMAC_KEY=<org pseudonymization key, from IT secret store>
abenlux gateway                          # loopback capture proxy + Tier-1 OTLP ingest on :8088
```

Then the developer points their tool at the agent. Get the exact snippet for any tool + shell:

```
abenlux onboard <tool> --shell powershell   # or bash / cmd
```

### CLI tools

| Tool | How captured | One-liner (bash) |
|------|--------------|------------------|
| Claude Code | Tier 1 (OTel) | `CLAUDE_CODE_ENABLE_TELEMETRY=1 OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8088 OTEL_EXPORTER_OTLP_PROTOCOL=http/json claude` |
| OpenAI Codex | Tier 1 (OTel) | `[otel] exporter="otlp-http" endpoint="http://127.0.0.1:8088"` in `~/.codex/config.toml` |
| Gemini CLI | Tier 1 (OTel) | `gemini --telemetry --telemetry-otlp-endpoint=http://127.0.0.1:8088` |
| aider | Tier 2 (base_url) | `ANTHROPIC_BASE_URL=http://127.0.0.1:8088 aider` |
| opencode / Crush / Pi / Droid / ForgeCode | Tier 2 (base_url) | set `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` to `http://127.0.0.1:8088` |

### IDE tools

| Tool | How captured | Setup |
|------|--------------|-------|
| Cline / Roo / Kilo | Tier 2 | set the provider Base URL to `http://127.0.0.1:8088` in the extension settings |
| Continue | Tier 2 | `apiBase: http://127.0.0.1:8088/v1` in `config.json` |
| VS Code Copilot (agent) | Tier 1 | OTel export to the edge agent |
| Cursor agent / Copilot inline | Tier 3 | nothing local — captured via vendor admin API on the collector |

The developer is informed **ambiently**, no browser required, all private to them:

```
# automatic: native desktop toast fires the moment a nudge happens (set ABEN_NOTIFY=0 to disable)
abenlux watch     # live terminal tail of your private signals, keep it in a spare pane
abenlux me        # on-demand: your own spend + retry/resent-history nudges + collaboration matches
abenlux graph     # your developer-local knowledge graph: objectives, tickets, purpose, learned vocab
```

These read files under the developer's own home dir (`~/.abenlux/`) and are **never** visible to
management. The feed file is also the integration contract for an IDE status-bar/extension.

### Optional: make intent classification ultra-smart (almost free)

Abenlux labels what each call is for from the branch convention and free keyword patterns, learning
your team's vocabulary over time. For the rare prompt both miss, point it at the LLM your org already
uses — it sends a cheap model an extractively-compressed prompt and a 5-token reply, cached, so it
costs fractions of a cent. Standard env names work, or the `ABEN_CLASSIFIER_*` ones.

```
LLM_PROVIDER=openai|azure|anthropic|google
# openai:    OPENAI_API_KEY
# azure:     AZURE_OPENAI_API_KEY + AZURE_OPENAI_API_BASE + AZURE_OPENAI_DEPLOYMENT_FAST + AZURE_OPENAI_API_VERSION
# anthropic: ANTHROPIC_API_KEY      google: GEMINI_API_KEY
```

### Verifying a tool is actually captured (no tokens spent)

```
abenlux mock                 # protocol-correct fake upstream on :9111 (terminal A)
ABEN_ANTHROPIC_UPSTREAM=http://127.0.0.1:9111 abenlux gateway   # terminal B
# run your tool against the agent; then:
abenlux me                   # you should see the call show up
```

---

## 2. What IT installs — the Collector + Dashboard

The central, management-facing host. One command; front it with TLS + SSO in production.

```
ABEN_HMAC_KEY=<org key>            # MUST be in a secret store the analytics plane cannot read
ABEN_DB=<warehouse dsn or path>    # SQLite for a pilot; Postgres/BigQuery for scale
ABEN_KG=knowledge_graph.yaml       # the company objective map (repos/tickets -> objectives)
ABEN_PRINCIPALS=principals.yaml    # token -> role (replace with SSO/OIDC in prod)
ABEN_INGEST_TOKEN=<device token>   # edge agents present this to /v1/derived
abenlux serve --host 0.0.0.0 --port 8090
```

Open `http://<host>:8090/` → sign in. Managers/finance see the **k-anonymized** spend→value
dashboard (by objective, tool, model), orphan-spend %, recoverable-waste band, and **drift**
alerts. Admins manage the knowledge graph. **No role — not even admin — can see an individual
developer's rows.** That is enforced in `auth/rbac.py`, not by UI discipline.

Pull Tier-3 (Cursor) usage on a schedule:

```
ABEN_CURSOR_API_KEY=<admin key> abenlux sync-cursor --period 30d
```

### Docker (pilot / single-host)

`docker compose up` brings up the collector, a local gateway, and an OTel collector. For a real
rollout the **gateway runs on each developer's laptop** (not in this compose file) and forwards to
the central collector service.

---

## Configuration reference

| Variable | Where | Meaning |
|----------|-------|---------|
| `ABEN_HMAC_KEY` | both | pseudonymization key; same on edge + collector so a person's rows line up |
| `ABEN_COLLECTOR_URL` | edge | set → forward derived records; unset → write local sqlite (solo mode) |
| `ABEN_INGEST_TOKEN` | both | device token the edge presents to the collector's `/v1/derived` |
| `ABEN_DB` | collector | derived warehouse (SQLite path by default, or a `postgresql://` DSN for scale) |
| `ABEN_NOTIFY` | edge | `0` disables desktop toasts (e.g. headless agents) |
| `ABEN_KG` | both | objective knowledge graph (YAML) |
| `ABEN_PRINCIPALS` | collector | token→role map (SSO in prod) |
| `ABEN_K_ANON` / `ABEN_DP_EPSILON` | collector | k-anonymity threshold + DP noise for rollups |
| `ABEN_SIGNAL_FEED` | edge | path to the developer's private nudge feed |
