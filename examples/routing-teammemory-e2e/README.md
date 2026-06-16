# Model routing and team memory, end to end

Two ways to spend fewer tokens, proven across a big team.

- **Model routing** runs at the gateway. An easy request (a rename, a format, a one liner) is sent to a
  cheaper model, a real piece of work stays on the strong one. The decision is made on the device from
  the request alone, no extra model call. `ABEN_ROUTE=on` sends easy calls down for real, `ABEN_ROUTE=shadow`
  only measures what it would save.
- **Team memory** runs at the collector where every developer's records meet. For each new request it
  looks for a close earlier one from a different teammate in the same tenant. An almost identical ask in
  the same language is `serve` (the answer could have been reused as is), the same task in another
  language is a `warm_start` (a strong head start). It runs in shadow with `ABEN_TM=shadow`, so it records
  what reusing solved work would save without changing any call. It matches on the content-free embedding
  only, never the prompt.

## Run it

```bash
ABEN_E2E_OUT=/tmp/rt python examples/routing-teammemory-e2e/suite.py
ABEN_E2E_OUT=/tmp/rt python examples/routing-teammemory-e2e/render.py
```

`suite.py` stands up the whole stack against a mock upstream and drives **34 developers across 5 tenants
and 13 IDE and CLI tools**, signing in both ways (a subscription reporting telemetry, and an api key
through the gateway). It asserts routing sent easy calls to a cheaper model, that team memory found work
to reuse and warm starts across languages, and that both show up in the report.

`render.py` reads the resulting `central.db` and renders the developer CLI screenshots to `docs/`
(`routing-teammemory-report.png`, the manager view, and `routing-teammemory-me.png`, a developer's
private view).

Nothing is seeded. Every figure is the product of a real call through the full edge pipeline.
