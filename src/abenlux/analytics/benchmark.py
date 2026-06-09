"""
Cross-tenant Benchmark Exchange. Tenants of one org (its geographies / business units) want to know
how they compare - "is our US region burning more per 1k tokens than EU, are we reusing as much, is
our spend going to net-new or maintenance" - WITHOUT either side seeing the other's raw numbers.

The whole design is ratios, not absolutes, behind three privacy walls:

  * ratios only - every metric is a unit-economics RATIO (cost per 1k tokens, cache-hit share, net-new
    share, ...). A ratio carries no tenant size, so comparing them leaks no headcount or total spend.

  * k-anonymity per tenant - a tenant is admitted to the cohort only if it clears k distinct developers
    (default k>=5). A 2-person region never publishes a ratio that could be backed out to a person.

  * cohort threshold + DP - the comparison is released only when at least K_TENANTS tenants qualify, so
    one tenant can't read another off a 2-tenant cohort. Published ratios carry Laplace DP noise, and
    a tenant's standing is given as a PERCENTILE within the cohort, never another tenant's raw figure.

What a caller gets back: their own (noised) ratio vector, the cohort distribution per metric (min /
median / max + their percentile), and a readiness panel that says exactly why a comparison is or is
not available yet. The single-tenant case returns the ratio vector plus "cohort not ready", honestly.
"""
from __future__ import annotations

from dataclasses import dataclass

from abenlux.attribution.attributor import MAINTENANCE, NET_NEW
from abenlux.privacy.pseudonymize import KAnonymityGate

# default: at least this many qualifying tenants before any cross-tenant comparison is released.
DEFAULT_K_TENANTS = 3

# each metric: key, label, and whether a HIGHER value is better (drives percentile + UI coloring).
METRICS = [
    ("cost_per_1k_tokens", "Cost per 1k tokens", False),
    ("cache_hit_ratio", "Prompt-cache hit ratio", True),
    ("cache_recoverable_share", "Recoverable resent-history share", False),
    ("orphan_share", "Unattributed (orphan) share", False),
    ("net_new_share", "Net-new investment share", True),
    ("maintenance_share", "Maintenance share", False),
    ("retry_rate", "Retry-loop rate", False),
    ("reuse_share", "Reuse-yield share of spend", True),
]


@dataclass
class TenantVector:
    tenant_id: str
    actors: int
    ratios: dict          # metric_key -> value
    qualifies: bool       # clears k-anonymity, so it may join the cohort


def _ratio(num: float, den: float) -> float:
    return round(num / den, 6) if den else 0.0


def tenant_vector(store, tenant: str, *, k: int = 5, reuse_avoided_usd: float = 0.0) -> TenantVector:
    """compute one tenant's content-free ratio vector from the derived store. reuse_avoided_usd is the
    tenant's k-gated reuse-yield (from the ledger), folded in as a share of spend so a tenant that
    reuses heavily scores well on the dimension that most sets this product apart."""
    totals = store.totals(tenant=tenant)
    tokens = totals["tokens"] or 0
    cost = totals["cost"] or 0.0
    events = totals["n"] or 0
    cache_read = totals.get("cache_read", 0)
    input_tokens = totals.get("input_tokens", 0)
    uncached_dup = max(0, totals["dup_tokens"] - cache_read)

    net_new = maintenance = 0.0
    for r in store.rollup("work_type", tenant=tenant):
        if r["label"] in NET_NEW:
            net_new += r["cost"]
        elif r["label"] in MAINTENANCE:
            maintenance += r["cost"]

    ratios = {
        "cost_per_1k_tokens": _ratio(cost, tokens / 1000.0),
        "cache_hit_ratio": _ratio(cache_read, cache_read + input_tokens),
        "cache_recoverable_share": _ratio(uncached_dup, tokens),
        "orphan_share": round(store.orphan_token_share(tenant=tenant), 6),
        "net_new_share": _ratio(net_new, cost),
        "maintenance_share": _ratio(maintenance, cost),
        "retry_rate": _ratio(totals["retries"], events),
        "reuse_share": _ratio(reuse_avoided_usd, cost + reuse_avoided_usd),
    }
    actors = totals["actors"]
    return TenantVector(tenant, actors, ratios, qualifies=actors >= k)


def _percentile(value: float, others: list[float], higher_is_better: bool) -> float:
    """fraction of the cohort this tenant beats on the good direction, in [0,1]. ties count as half."""
    if not others:
        return 0.0
    wins = sum((value > o) if higher_is_better else (value < o) for o in others)
    ties = sum(value == o for o in others)
    return round((wins + 0.5 * ties) / len(others), 4)


def benchmark(
    store, *, tenants: list[str], focus_tenant: str, k: int = 5, dp_epsilon: float = 1.0,
    k_tenants: int = DEFAULT_K_TENANTS, reuse_by_tenant: dict | None = None,
) -> dict:
    """assemble the cross-tenant comparison for `focus_tenant` against the cohort of `tenants` (the
    tenants of one org). content-free, k-anonymized, DP-noised, and gated on a minimum cohort size."""
    gate = KAnonymityGate(k=k, dp_epsilon=dp_epsilon)
    reuse_by_tenant = reuse_by_tenant or {}
    vectors = [
        tenant_vector(store, t, k=k, reuse_avoided_usd=reuse_by_tenant.get(t, 0.0))
        for t in tenants
    ]
    qualifying = [v for v in vectors if v.qualifies]
    focus = next((v for v in vectors if v.tenant_id == focus_tenant), None)

    ready = len(qualifying) >= k_tenants and focus is not None and focus.qualifies
    readiness = {
        "ready": ready,
        "cohort_size": len(qualifying),
        "k_tenants_required": k_tenants,
        "focus_qualifies": bool(focus and focus.qualifies),
        "focus_actors": focus.actors if focus else 0,
        "k": k,
        "reason": _readiness_reason(focus, qualifying, k_tenants, k),
    }

    # the focus tenant always gets its OWN ratio vector back (it's their own data), DP-noised so even a
    # screenshot doesn't expose an exact figure. the cohort comparison is only filled in when ready.
    focus_ratios = _noise_ratios(focus.ratios, gate) if focus else {}

    comparison = []
    if ready:
        for key, label, higher in METRICS:
            cohort_vals = [v.ratios[key] for v in qualifying]
            others = [v.ratios[key] for v in qualifying if v.tenant_id != focus_tenant]
            cohort_vals_sorted = sorted(cohort_vals)
            comparison.append({
                "metric": key, "label": label, "higher_is_better": higher,
                "you": focus_ratios.get(key),
                "cohort_min": round(_noise(min(cohort_vals), gate), 6),
                "cohort_median": round(_noise(_median(cohort_vals_sorted), gate), 6),
                "cohort_max": round(_noise(max(cohort_vals), gate), 6),
                "your_percentile": _percentile(focus.ratios[key], others, higher),
            })

    return {
        "org_cohort": [t for t in tenants],
        "focus_tenant": focus_tenant,
        "readiness": readiness,
        "your_ratios": focus_ratios,
        "comparison": comparison,
        "privacy": {"k": k, "k_tenants": k_tenants, "dp_epsilon": dp_epsilon,
                    "note": "ratios only, k-anon per tenant, DP-noised, released only above the cohort threshold"},
    }


def _readiness_reason(focus, qualifying, k_tenants: int, k: int) -> str:
    if focus is None:
        return "no data for this tenant yet"
    if not focus.qualifies:
        return f"your tenant has {focus.actors} developer(s), needs >= {k} to publish a ratio"
    if len(qualifying) < k_tenants:
        return f"{len(qualifying)} of {k_tenants} required tenants qualify - invite more org units to compare"
    return "cohort ready"


def _median(sorted_xs: list[float]) -> float:
    n = len(sorted_xs)
    if n == 0:
        return 0.0
    mid = n // 2
    return sorted_xs[mid] if n % 2 else (sorted_xs[mid - 1] + sorted_xs[mid]) / 2.0


def _noise(value: float, gate: KAnonymityGate) -> float:
    # DP noise scaled to a small-magnitude ratio: ratios live in [0,~]. clamp negatives to 0 so noise
    # can't print a nonsensical sub-zero rate. sensitivity is tiny because these are bounded ratios.
    noised = value + gate.laplace_noise(sensitivity=0.01)
    return max(0.0, noised)


def _noise_ratios(ratios: dict, gate: KAnonymityGate) -> dict:
    return {k: round(_noise(v, gate), 6) for k, v in ratios.items()}
