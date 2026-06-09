# Architecture

Abenlux has two runtimes that share one domain core. The core (`schema`, `pipeline`, `processing`,
`attribution`, `salience`, `privacy`, `pricing`, `analytics`, `collaborate`) depends only on the
standard library, so it is trivially testable; the web and ML concerns live at the edges (`capture`,
`api`, `embedding`, `agent`).

The whole privacy posture is the **order of operations on the device**: a prompt is redacted, derived
into vectors and counts, attributed, and pseudonymized *before* anything is written or leaves the
machine. Only a content-free `DerivedRecord` ever crosses to the central host.

## Data flow

```mermaid
flowchart TB
  subgraph EDGE["Developer machine — the edge agent (abenlux gateway / agent)"]
    direction TB
    TOOL["AI coding tool — Claude Code · Codex · Gemini CLI · aider · opencode · Cline · …"]
    TOOL -->|"Tier 2 — base_url proxy"| PROXY["gateway._proxy<br/>stream-through + tee<br/>BackgroundTask, off the hot path"]
    TOOL -.->|"Tier 1 — OTLP telemetry"| OTEL["otel_ingest<br/>gen_ai.* and claude_code.api_request"]
    PROXY -->|"forward unchanged"| UP["upstream model API<br/>Anthropic · OpenAI · Azure · Gemini"]
    PROXY --> EVT["CanonicalEvent"]
    OTEL --> EVT

    EVT --> PIPE
    subgraph PIPE["pipeline.process — runs entirely on the device"]
      direction TB
      P1["1 · redact secrets and PII"] --> P2["2 · waste + cache-efficiency signals"]
      P2 --> P3["3 · salient intent → keyphrase embedding"]
      P3 --> P4["4 · attribute to objective — ticket/repo join, semantic fallback"]
      P4 --> P5["5 · classify work-type + self-learn vocabulary"]
      P5 --> P6["6 · HMAC-pseudonymize the actor, drop the raw id"]
      P6 --> P7["7 · cache-aware pricing → DerivedRecord, then clear every message body"]
    end

    PIPE -->|"waste / collab / budget nudges"| FEED["private feed + native desktop toast<br/>abenlux me · watch · graph · agent"]
    PIPE --> SINK["sink.insert"]
  end

  SINK ==>|"HTTPS /v1/derived — device token, derived-only"| STORE

  subgraph HOST["Central host — collector + dashboard (IT)"]
    direction TB
    STORE["DerivedStore — warehouse, sqlite or Postgres"]
    STORE --> CENTRAL["central collaboration broker<br/>content-free, objective-aware"]
    STORE --> ANALYTICS["analytics — reports · drift · budgets"]
    CENTRAL --> MATCH["per-owner match store"]
    ANALYTICS --> API["api.server + dashboard<br/>RBAC — k-anonymized aggregates only"]
  end

  TIER3["Tier 3 — vendor admin API (Cursor): metadata only"] -.->|"abenlux sync-cursor"| STORE
  API -.->|"VIEW_OWN — only your own pseudonym's rows"| FEED
  HOST -.->|"edge polls /v1/budget-status + /v1/collab-status on a TTL"| FEED

  classDef edge fill:#0d1b2a,stroke:#1b9aaa,color:#e0fbfc;
  classDef host fill:#1a1423,stroke:#a06cd5,color:#f3e8ff;
  class TOOL,PROXY,OTEL,EVT,P1,P2,P3,P4,P5,P6,P7,FEED,SINK,UP edge;
  class STORE,CENTRAL,ANALYTICS,MATCH,API,TIER3 host;
```

The dashed line back to the feed is the point of the product: a developer's own data and nudges stay
on their machine; the only thing that travels *to* the edge from the host is a content-free budget and
collaboration status poll that drives a private toast.

## Capture is tiered by how each tool makes its call

| Tier | Mechanism | Tools | What it sees |
|---|---|---|---|
| **1 — OTLP native** | the tool self-instruments to an OTLP endpoint | Claude Code, Codex, Gemini CLI | usage + cache tokens; content only if the tool exports it |
| **2 — gateway proxy** | the tool honors a custom `base_url` → loopback reverse proxy | aider, Cline, Continue, opencode, Crush, Droid, Goose, … | full request/response (redacted on-device) |
| **3 — vendor admin** | a server-side tool exposes an admin/usage API | Cursor, Copilot | metadata only (no prompt) |

`capture/otel_ingest.py` parses **two** Tier-1 shapes: the `gen_ai.*` semantic conventions, and Claude
Code's own `claude_code.api_request` **log** events (bare `input_tokens`/`cache_read_tokens`
attributes, not `gen_ai.*` — so it needs its own parser; its raw `user.email` is dropped at parse
time, the hashed `user.id` is the actor). `capture/adapters.py` handles every Tier-2 wire format:
Anthropic `/v1/messages`, OpenAI `/v1/chat/completions`, the OpenAI **Responses API** `/v1/responses`
(Codex), Azure OpenAI `/openai/deployments/.../chat/completions`, and Gemini
`/v1beta/models/...` — including Gemini's URL-based streaming flag (`:streamGenerateContent`, no body
field) and its model living in the URL, both of which are easy to mis-handle and were caught by driving
the real CLIs.

## The five privacy invariants (and where each is enforced)

1. **Redaction precedes persistence and derivation.** `pipeline.process` runs `redact_event_inplace`
   as step 1, before embedding, attribution, or any write. After the `DerivedRecord` is built, every
   message body is set to `""`. Proven on disk by `test_integration` and `test_real_sdk` (a real
   Anthropic SDK call with a secret in the prompt → the secret is absent from the store file), and on
   real Tier-1 data by `test_claude_code_otel` (the raw `user.email` never reaches the record).

2. **Only derived data leaves the device.** The `DerivedSink` either writes locally (solo) or POSTs a
   `DerivedRecord.to_dict()` to the collector. The collector's `/v1/derived` accepts **only known
   derived fields** — a smuggled `messages`/`content` key is dropped at the schema boundary
   (`test_forwarding`).

3. **Identity is one-way.** `strip_raw_actor_inplace` replaces the raw actor with an HMAC pseudonym
   and drops the raw id in-flight. The same key on edge and collector makes a person's rows line up for
   their *own* view without ever storing a name. The key lives in a secret store the analytics plane
   cannot read; the gateway refuses to run management rollups on the default dev key.

4. **Management sees only k-anonymized aggregates.** `analytics.reports` gates every group through
   `KAnonymityGate` (default k≥5; sub-k groups are suppressed, not noisily shown) and DP-noises
   org-wide totals. There is **no permission** for individual drilldown — see invariant 5.

5. **No role can see another individual.** `auth/rbac.py` defines `VIEW_OWN`, `VIEW_AGGREGATES`,
   `VIEW_COST`, `MANAGE`. `VIEW_OWN` is scoped to the caller's own pseudonym; there is deliberately no
   permission granting per-person detail to anyone. Enforced server-side in `api/server.py` and
   verified by `test_rbac` + `test_api` (developer → 403 on `/api/report`; `/api/me` returns only the
   caller's rows).

## The edge pipeline, step by step

`pipeline.process` is the heart of the system and the privacy boundary. Each numbered step in the
diagram maps to one concern:

- **Salient intent (step 3) is the keystone.** Long, code-heavy, multi-part prompts are reduced to
  their intent-dense core (`salience.py`: strip code/stack-trace noise, keep the highest-salience
  sentences) before both classification and embedding. This is deterministic and free — no ML model,
  no per-call LLM. It is *why* a pasted stack trace doesn't get mislabelled a "fix", and *why*
  collaboration matching is sharp: the vector is built from **keyphrases** (domain terms, stopwords
  dropped), so two developers on the same problem match even when phrased differently.

- **Work-type classification (step 5)** is a cascade: branch convention first (auditable
  ground-truth), then a weighted keyword/pattern classifier over the salient text plus the device's
  self-learned vocabulary, then — only when all of those miss — one tiny, cached, extractively
  compressed LLM call (optional; OpenAI/Azure/Claude/Gemini). Every confident label teaches the free
  keyword layer, so the LLM fires less over time. Accuracy is held by a labelled corpus
  (`test_intent_corpus`): 98.6% on 69 varied prompts, 100% on the net-new-vs-maintenance split.

- **Cache-aware pricing (step 7)** separates fresh input from cache reads and writes per call, so cost
  matches the provider's bill to the cent. It also powers the **cache-inefficiency** nudge (step 2):
  resent context that *isn't* being cached is the one token-saving lever with zero loss of detail —
  the exact same context, billed as a cache hit.

## Collaboration

Matching runs **centrally at the collector** (`api/server._match_centrally`) over the content-free
forwarded records — the embedding + objective label, never prompt text — so two developers on two
machines actually match. The broker (`collaborate/broker.py`) is **objective-aware**: a high topic
overlap within the same objective pairs people (bar 0.40 on the keyphrase-hashing embedder), while a
*different* objective needs a stronger match (0.55), because cross-objective overlap is more often
coincidental. It is **precision-first** — a false pairing is worse than a miss — and verified at 100%
precision on a labelled corpus (`test_collab_corpus`). Two walls are enforced in code: it never matches
across a different **client** (Chinese wall) or a **data-residency** boundary. Identities and contact
handles are revealed only on a **mutual double-blind consent**, and in org mode the edge agent
live-pushes a toast by polling `/v1/collab-status` for its own new matches.

## The background agent

`abenlux agent install` runs the capture agent in the background, started at user login, in the
developer's own GUI session — the only place desktop toasts and the D-Bus/Aqua notification daemons
exist, which is why it is a **user-level** unit, never a root service (`agent/service.py`):

- **Linux** → a systemd `--user` unit
- **macOS** → a launchd LaunchAgent (`RunAtLoad` + `KeepAlive`)
- **Windows** → a hidden Startup-folder launcher (chosen over a scheduled task, which a locked-down
  machine denies a standard user) plus a registered toast AppUserModelID so Win10/11 actually shows it

Config is snapshotted to `~/.abenlux/agent.env` and reloaded by `agent run` before `Settings` reads
the environment. Service-manager calls are best-effort (tolerant of a missing `systemctl`/`launchctl`).

## Why these specific engineering choices

- **Stream-through + BackgroundTask, not buffer-then-return.** A base_url proxy must not add latency or
  break streaming. The gateway tees bytes to the tool as they arrive and runs capture in a Starlette
  `BackgroundTask` *after* the response completes — reliable, unlike an async-generator `finally` under
  ASGI. (A real bug the integration tests caught.)

- **Thread-safe stores.** Capture runs in the BackgroundTask threadpool, so `DerivedStore` /
  `MatchStore` open with `check_same_thread=False` (sqlite is serialized). Postgres is a drop-in
  alternative via `open_store(dsn)`.

- **Snapshot copies in the resent-history tracker.** The pipeline wipes message content after
  derivation; the tracker stores `copy.copy` of each message so the next turn's baseline isn't blanked.
  Conversations are isolated by a key anchored on the first user message, so concurrent sessions don't
  thrash one shared baseline.

- **Authoritative token semantics.** Anthropic's output count is the cumulative value in the final
  `message_delta`, not a sum of deltas; OpenAI usage exists only with `include_usage` (estimated and
  flagged otherwise), and its `prompt_tokens_details.cached_tokens` are split out so the cache discount
  applies; the Responses API uses `input_tokens`/`output_tokens`; Gemini usage is the final
  `usageMetadata`. `capture/adapters.py`.

- **Longest-prefix pricing.** A new point release (`claude-opus-4-8-2026…`) inherits its family price
  instead of falling to $0. Unknown models are flagged `unpriced`, never guessed.

## Testing topology

`make test` runs **203 tests**: pure-core unit tests; FastAPI `TestClient` for RBAC/API; subprocess
end-to-end runs of the **real Anthropic, OpenAI, and Azure OpenAI SDKs** through a live gateway + mock
upstream (`test_real_sdk.py`); wire-format tests pinned from genuine **Claude Code, Gemini CLI, and
Codex (Responses API)** traffic (`test_tool_capture.py`, `test_claude_code_otel.py`); a full
edge→collector forward loop (`test_forwarding.py`); labelled accuracy corpora for intent and
collaboration (`test_intent_corpus.py`, `test_collab_corpus.py`); the background agent and its per-OS
units (`test_agent.py`); and a simulated team exercising suggestions, collaboration walls/consent, and
drift (`test_multiuser.py`, `test_exhaustive.py`).

Two reproducible Docker harnesses back the claims that can't live in `make test` (they need Docker and
real tool images): [`examples/tool-verification`](examples/tool-verification/) drives Gemini CLI,
Codex, and opencode through a running gateway, and [`examples/agent-verification`](examples/agent-verification/)
verifies the Linux background agent and a real `notify-send` toast received by a notification daemon.
