# Multi-container, multi-developer, real-model E2E

A realistic **distributed** deployment in Docker: separate containers for the central collector and
**one edge gateway per tenant**, driven by ~23 developers across 5 tenants of 2 orgs through
multi-turn model calls — against a **real model** when you supply a key, or the mock upstream
otherwise. Nothing is seeded; every figure comes from a real call through the full edge pipeline.

## Containers (each its own image instance)

- `collector` — RBAC + k-anonymity + reuse-yield ledger + benchmark + collaboration broker
- `gw-acme-eu`, `gw-acme-us`, `gw-acme-apac`, `gw-acme-tiny`, `gw-globex-eu` — edge gateways, each
  stamping its own `tenant_id` / `residency`, forwarding only content-free `DerivedRecord`s
- `mock` — protocol-faithful upstream for the no-key path
- `driver` — drives the developers and asserts every role's view

## Run against the mock (no key, no spend)

```bash
docker compose -f examples/multi-dev-e2e/docker-compose.yml up --build \
  --abort-on-container-exit --exit-code-from driver
```

## Run against a REAL model

Provide a key + the real upstream on the **host** (never committed, never baked into the image):

```bash
# Anthropic (cheapest tier recommended)
ABEN_REAL=1 ABEN_PROVIDER=anthropic ABEN_MODEL=claude-haiku-4-5 \
ABEN_UPSTREAM=https://api.anthropic.com ANTHROPIC_API_KEY=sk-ant-... \
docker compose -f examples/multi-dev-e2e/docker-compose.yml up --build \
  --abort-on-container-exit --exit-code-from driver

# OpenAI
ABEN_REAL=1 ABEN_PROVIDER=openai ABEN_MODEL=gpt-4o-mini \
ABEN_OPENAI_UPSTREAM=https://api.openai.com OPENAI_API_KEY=sk-... \
docker compose -f examples/multi-dev-e2e/docker-compose.yml up --build \
  --abort-on-container-exit --exit-code-from driver
```

`max_tokens` is 64 and prompts are short, so a full real run is a few dozen cheap calls (cents on
`claude-haiku-4-5` / `gpt-4o-mini`). The driver verifies the **real** spend was captured and priced
from genuine token facts (`unpriced_events == 0`).

## What it asserts

Topology up → tenant registration → multi-turn traffic → forwarding settled → then, per role:
developer (own view only, 403 on every aggregate, double-blind consent reveal), manager
(tenant-scoped report, reuse-yield, k-anon suppression of the 2-dev tenant, benchmark cohort,
cross-org 403), finance (`view_cost`, report, tenant-create 403), admin (objectives, tenant create
bound to own org, cross-org hijack 409), budget overrun, the org wall on collaboration, and 401s.

`docker compose ... --exit-code-from driver` returns the driver's exit code (non-zero on any failure).

## Teardown

```bash
docker compose -f examples/multi-dev-e2e/docker-compose.yml down -v
```
