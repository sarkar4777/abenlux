# Deep end-to-end verification (Docker)

A single container that stands up the **whole stack** and drives it the way a real org would, then
asserts every role's view and every guarantee. Nothing is seeded — every figure is the product of a
real model call through the full edge pipeline.

## What it runs

Inside the container it starts real processes:

- a **mock model upstream** (protocol-faithful Anthropic/OpenAI/Gemini responses, no tokens spent),
- the **central collector** (`abenlux.api.server`) with RBAC, k-anonymity, the reuse-yield ledger,
  the benchmark, and the collaboration broker,
- **one edge gateway per tenant** (`abenlux.capture.gateway`), each stamping its own `tenant_id` /
  `residency` and forwarding only content-free `DerivedRecord`s to the collector.

Then it generates **multi-turn traffic for ~23 developers across 5 tenants of 2 orgs**
(`acme-eu`, `acme-us`, `acme-apac`, `acme-tiny`, `globex-eu`) — resent-history bloat, retry loops,
shared topics (collaboration + reuse), cross-org topic overlap (the org wall), poor caching
(recoverable waste), and a high-spend tenant (budget overrun).

## What it asserts (every role, multi-turn, adversarial)

- **Developer** — sees only their own spend / waste / collaboration matches; is `403` on every
  aggregate endpoint; runs the full **double-blind consent** flow (peer hidden → mutual consent →
  revealed); sets and reads their contact card.
- **Manager** — tenant-scoped report (excludes other tenants' objectives), reuse-yield savings line,
  budgets, drift, rollup; **k-anonymity suppression** of a 2-developer tenant; the **Benchmark
  Exchange** (ready cohort, acme-only, valid percentiles, sub-k tenant excluded); `403` on tenant
  creation and on any **cross-org** report.
- **Finance** — has `view_cost`, reads report + benchmark, still `403` on tenant creation.
- **Admin** — lists objectives, creates a tenant bound to their own org, and a **cross-org hijack is
  refused (409)** with ownership preserved.
- **Budgets** — a heavily-spent innovation budget shows `at_risk`/`over` with a run-rate forecast.
- **Org wall** — two developers in different orgs but the same residency, on the same topic, never
  match in the broker.
- **Hardening** — no/garbage token → `401`; a smuggled prompt field is never persisted; a forged
  `$999999` cost is re-priced from token facts and cannot inflate the org total.

## Run

```bash
# from the repo root
docker build -t abenlux-deep-e2e -f examples/deep-e2e/Dockerfile .
docker run --rm abenlux-deep-e2e
```

The run prints a `PASS/FAIL` line per check and a final tally, and exits non-zero if anything fails.
