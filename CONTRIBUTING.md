# Contributing to Abenlux

Thanks for your interest. Abenlux is a privacy-first AI spend attribution plane, so contributions
are held to two bars beyond "it works": **the privacy invariants must not regress**, and **new
behavior ships with tests**.

## Setup

```
git clone https://github.com/sarkar4777/abenlux
cd abenlux
make install        # pip install -e ".[dev]"
make test           # 324 tests
make lint           # ruff
make demo           # see the pipeline run with zero setup
```

## The non-negotiables (CI will not let these slide)

1. **No raw content or identity leaves the edge.** If you touch the pipeline, store, sink, or API,
   keep the privacy invariant tests green (`test_integration.py`, `test_forwarding.py`). Redaction
   runs before derive/persist. Only `DerivedRecord` crosses the network.
2. **No management surface for an individual.** RBAC has no permission for per-person detail. Keep
   `test_rbac.py` / `test_api.py` green.
3. **Aggregates pass k-anonymity.** Anything management-facing goes through the gate.

## Style

- Terse, lowercase developer comments. No essays, no semicolons in prose, no em-dashes.
- Standard library only in the domain core (`schema`, `pipeline`, `processing`, `attribution`,
  `privacy`, `pricing`, `analytics`). Web/ML deps live at the edges.
- Run `make lint` (ruff, no semicolons rule) before pushing.

## Adding a tool

Most tools just need a registry entry in `capture/tiers.py` (pick the honest tier) and, if Tier 2,
the right `protocol`. Add an onboarding case in `onboard.py` if it needs special env. Verify with
`abenlux mock` + the real client SDK, no tokens spent.

## Pull requests

Keep them focused. Describe what changed and which invariant tests cover it. CI runs ruff + pytest
on Linux/macOS/Windows across Python 3.10-3.13.
