# Architecture

Abenlux has two runtimes that share one domain core. The core (`schema`, `pipeline`, `processing`,
`attribution`, `privacy`, `pricing`, `analytics`) depends only on the standard library, so it is
trivially testable; the web/ML concerns live at the edges (`capture`, `api`, `embedding`).

## Data flow

```
                          DEVELOPER MACHINE (edge agent)                      CENTRAL HOST (IT)
                          ──────────────────────────────                      ─────────────────
 tool ──base_url──▶ gateway._proxy ──stream-through──▶ upstream
                        │  (BackgroundTask, off the hot path)
                        ▼
 OTLP (Tier 1) ──▶ otel_ingest ─▶ CanonicalEvent
 Cursor (Tier 3, on the collector) ─▶ vendor_admin ─▶ DerivedRecord
                        │
                        ▼
                  pipeline.process
                   1 redact_event_inplace         (secrets/PII destroyed)
                   2 waste_monitor.observe        (retry/bloat/answered/routing → dev feed)
                   3 embed                         (vector, not text)
                   4 attribute (+semantic)         (objective join, confidence)
                   5 strip_raw_actor               (HMAC pseudonym; raw id dropped)
                   6 cost_usd                       (priced from request model)
                   7 build DerivedRecord; clear all message bodies
                        │
                        ├─▶ developer.feed / developer.matches   (PRIVATE, on device)
                        ▼
                  sink.insert ──HTTP /v1/derived (device token)──────────────▶ store (warehouse)
                                                                                    │
                                                                analytics.reports ──┤  RBAC: managers
                                                                analytics.drift  ───┤  → k-anon aggregates
                                                                                    ▼
                                                                          api.server + dashboard
```

## The five invariants (and where each is enforced)

1. **Redaction precedes persistence and derivation.** `pipeline.process` runs `redact_event_inplace`
   as step 1, before embedding, attribution, or any write. After the `DerivedRecord` is built, every
   message body is set to `""`. Proven on disk by `test_integration.test_privacy_invariant…` and
   `test_real_sdk` (a real Anthropic SDK call with a secret in the prompt → the secret is absent from
   the store file).

2. **Only derived data leaves the device.** The `DerivedSink` either writes locally (solo) or POSTs a
   `DerivedRecord.to_dict()` to the collector. The collector's `/v1/derived` accepts **only known
   derived fields** — a smuggled `messages`/`content` key is dropped at the schema boundary
   (`test_forwarding.test_central_ingest_strips_unknown_smuggled_fields`).

3. **Identity is one-way.** `strip_raw_actor_inplace` replaces the raw actor with an HMAC pseudonym
   and drops the raw id in-flight. The same key on edge and collector makes a person's rows line up
   for their *own* view without ever storing a name. The key lives in a secret store the analytics
   plane cannot read.

4. **Management sees only k-anonymized aggregates.** `analytics.reports` gates every group through
   `KAnonymityGate` (default k≥5; sub-k groups are suppressed, not noisily shown) and DP-noises
   org-wide totals. There is **no permission** for individual drilldown — see invariant 5.

5. **No role can see another individual.** `auth/rbac.py` defines `VIEW_OWN`, `VIEW_AGGREGATES`,
   `VIEW_COST`, `MANAGE`. `VIEW_OWN` is scoped to the caller's own pseudonym; there is deliberately no
   permission granting per-person detail to anyone. Enforced server-side in `api/server.py` and
   verified by `test_rbac` + `test_api` (developer → 403 on `/api/report`; `/api/me` returns only the
   caller's rows).

## Why these specific engineering choices

- **Stream-through + BackgroundTask, not buffer-then-return.** A base_url proxy must not add latency
  or break streaming. The gateway tees bytes to the tool as they arrive and runs capture in a
  Starlette `BackgroundTask` *after* the response completes — reliable, unlike an async-generator
  `finally` under ASGI. (This was a real bug the integration tests caught.)

- **Thread-safe stores.** Capture runs in the BackgroundTask threadpool, so `DerivedStore` /
  `MatchStore` open with `check_same_thread=False` (sqlite is serialized).

- **Snapshot copies in the resent-history tracker.** The pipeline wipes message content after
  derivation; the tracker stores `copy.copy` of each message so the next turn's baseline isn't
  blanked. (Also a bug the tests caught.)

- **Authoritative token semantics.** Anthropic's output count is the cumulative value in the final
  `message_delta`, not a sum of deltas; OpenAI usage exists only with `include_usage` (estimated and
  flagged otherwise); Gemini usage is the final `usageMetadata`. `capture/adapters.py`.

- **Longest-prefix pricing.** A new point release (`claude-opus-4-8-2026…`) inherits its family
  price instead of falling to $0. Unknown models are flagged `unpriced`.

## Testing topology

`make test` runs 99 tests: pure-core unit tests; FastAPI `TestClient` for RBAC/API; subprocess-based
end-to-end runs of the **real Anthropic and OpenAI SDKs** through a live gateway + mock upstream
(`test_real_sdk.py`); a full edge→collector forward loop (`test_forwarding.py`); and a simulated team
exercising suggestions, collaboration walls/consent, and drift (`test_multiuser.py`).
