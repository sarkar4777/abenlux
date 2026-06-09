"""
Cross-tenant Benchmark Exchange. Tenants of one org (its geographies / business units) want to know
how they compare - "is our US region burning more per 1k tokens than EU, are we reusing as much, is
our spend going to net-new or maintenance" - WITHOUT either side seeing the other's raw numbers.

The whole design is ratios, not absolutes, behind several privacy walls:

  * ratios only - every metric is a unit-economics RATIO (cost per 1k tokens, cache-hit share, net-new
    share, ...). A ratio carries no tenant size, so comparing them leaks no headcount or total spend.

  * k-anonymity per tenant - a tenant is admitted to the cohort only if it clears k distinct developers
    (default k>=5). A 2-person region never publishes a ratio that could be backed out to a person.

  * cohort threshold - the comparison is released only when at least K_TENANTS tenants qualify.

  * order statistics withheld for small cohorts - cohort_min/median/max are a single tenant's ratio
    when the cohort is tiny (for 3 tenants, min/median/max ARE the three tenants' values). So we publish
    extremes only above ORDER_STATS_FLOOR tenants and the median only above MEDIAN_FLOOR. Each released
    order statistic is still ONE tenant's DP-noised ratio (not an average), but at those cohort sizes it
    is protected by the Laplace noise and the k-anonymity behind it, the same exposure class. Below the
    floor a caller gets only their own value and their PERCENTILE - which reveals rank and nothing else.

  * consistency + DP - the displayed point figures (your value, min, median, max) are derived from ONE
    Laplace-noised, sorted cohort series, so the row is internally consistent (you within [min,max],
    min<=median<=max) and DP-perturbed as defense in depth. The PERCENTILE is computed on the RAW
    (un-noised) ratios: rank is ordinal and already protected by the k-anon + cohort gates, so noising
    it would only make genuinely tied tenants jump between best and worst on reload, never helping.

A degenerate (unpriced) tenant - one whose model isn't in the price table, so its priced cost is 0 -
is excluded from the COST-denominated metrics (it would otherwise look "free" and collapse the cohort
min), while still participating in token/event-denominated metrics like cache-hit and retry rate.
"""
from __future__ import annotations

from dataclasses import dataclass

from abenlux.attribution.attributor import MAINTENANCE, NET_NEW
from abenlux.privacy.pseudonymize import KAnonymityGate

# default: at least this many qualifying tenants before any cross-tenant comparison is released.
DEFAULT_K_TENANTS = 3
# release cohort min/max only above this many qualifying tenants (below it they are near-raw values),
# and the median only above MEDIAN_FLOOR (so it is an interior average, not one tenant's raw point).
ORDER_STATS_FLOOR = 5
MEDIAN_FLOOR = 4

# each metric: key, label, whether HIGHER is better, and whether it is COST-denominated (excluded for
# a tenant whose priced cost is 0, since a degenerate denominator would publish a misleading ratio).
METRICS = [
    ("cost_per_1k_tokens", "Cost per 1k tokens", False, True),
    ("cache_hit_ratio", "Prompt-cache hit ratio", True, False),
    ("cache_recoverable_share", "Recoverable resent-history share", False, False),
    ("orphan_share", "Unattributed (orphan) share", False, False),
    ("net_new_share", "Net-new investment share", True, True),
    ("maintenance_share", "Maintenance share", False, True),
    ("retry_rate", "Retry-loop rate", False, False),
    ("reuse_share", "Reuse-yield share of spend", True, True),
]

# the DP perturbation on released figures. the authoritative comparison is the percentile, derived from
# the same noised series so the row stays consistent; the noise is defense in depth on the point values.
_DP_SENSITIVITY = 0.003


@dataclass
class TenantVector:
    tenant_id: str
    actors: int
    ratios: dict          # metric_key -> value or None (None = not applicable, e.g. unpriced for cost)
    qualifies: bool       # clears k-anonymity, so it may join the cohort


def _ratio(num: float, den: float) -> float:
    return round(num / den, 6) if den else 0.0


def tenant_vector(store, tenant: str, *, k: int = 5, reuse_avoided_usd: float = 0.0) -> TenantVector:
    """compute one tenant's content-free ratio vector from the derived store. cost-denominated metrics
    are None when the tenant has no PRICED cost (unpriced model), so a degenerate 0 never pollutes the
    cohort. reuse_avoided_usd (the tenant's k-gated reuse-yield) folds in as a share of spend."""
    totals = store.totals(tenant=tenant)
    tokens = totals["tokens"] or 0
    cost = totals["cost"] or 0.0
    events = totals["n"] or 0
    cache_read = totals.get("cache_read", 0)
    input_tokens = totals.get("input_tokens", 0)
    uncached_dup = max(0, totals["dup_tokens"] - cache_read)
    priced = cost > 0                       # any priced spend at all; if not, cost metrics are N/A

    net_new = maintenance = 0.0
    for r in store.rollup("work_type", tenant=tenant):
        if r["label"] in NET_NEW:
            net_new += r["cost"]
        elif r["label"] in MAINTENANCE:
            maintenance += r["cost"]

    ratios = {
        "cost_per_1k_tokens": _ratio(cost, tokens / 1000.0) if priced else None,
        "cache_hit_ratio": _ratio(cache_read, cache_read + input_tokens),
        "cache_recoverable_share": _ratio(uncached_dup, tokens),
        "orphan_share": round(store.orphan_token_share(tenant=tenant), 6),
        "net_new_share": _ratio(net_new, cost) if priced else None,
        "maintenance_share": _ratio(maintenance, cost) if priced else None,
        "retry_rate": _ratio(totals["retries"], events),
        "reuse_share": _ratio(reuse_avoided_usd, cost + reuse_avoided_usd) if priced else None,
    }
    actors = totals["actors"]
    return TenantVector(tenant, actors, ratios, qualifies=actors >= k)


def _median(sorted_xs: list[float]) -> float:
    n = len(sorted_xs)
    if n == 0:
        return 0.0
    mid = n // 2
    return sorted_xs[mid] if n % 2 else (sorted_xs[mid - 1] + sorted_xs[mid]) / 2.0


def _percentile_in(value: float, series: list[float], higher_is_better: bool) -> float:
    """fraction of the OTHER tenants this value beats on the good direction, in [0,1]. drops exactly ONE
    occurrence of the focus value (by EQUALITY, not object identity - identity wrongly removes genuine
    tie-peers that share a clamped constant), so genuinely tied tenants all land near 0.5 rather than a
    random extreme. ranked on the RAW (un-noised) ratios so the rank is stable across reloads and tied
    tenants stay tied - the rank is ordinal and already protected by the k-anon + cohort-size gates."""
    others = list(series)
    try:
        others.remove(value)            # remove a single equal element; value is always present
    except ValueError:
        pass
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
    seen: set = set()
    vectors = []
    for t in tenants:                                  # de-dup defensively, preserve order
        if t in seen:
            continue
        seen.add(t)
        vectors.append(tenant_vector(store, t, k=k, reuse_avoided_usd=reuse_by_tenant.get(t, 0.0)))
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

    # the focus tenant always gets its OWN (noised) ratio vector back - it is their own data.
    focus_ratios = _noise_ratios(focus.ratios, gate) if focus else {}

    comparison = []
    if ready:
        for key, label, higher, _cost in METRICS:
            # one entry per qualifying tenant that HAS this metric (cost metrics drop unpriced tenants).
            raw = {v.tenant_id: v.ratios[key] for v in qualifying if v.ratios.get(key) is not None}
            # DISPLAY figures (you/min/median/max) are DP-noised, derived from one sorted series so the
            # row is internally consistent (you within [min,max], min<=median<=max).
            noised = {tid: _noise(val, gate) for tid, val in raw.items()}
            series = sorted(noised.values())
            you = noised.get(focus_tenant)              # None if focus lacks this metric (unpriced)
            n = len(series)
            # the PERCENTILE (the authoritative comparison) is computed on the RAW ratios, so the rank is
            # stable across reloads and genuinely tied tenants stay tied - noise must not reorder ranks.
            raw_series = sorted(raw.values())
            raw_you = raw.get(focus_tenant)
            row = {
                "metric": key, "label": label, "higher_is_better": higher,
                "you": round(you, 6) if you is not None else None,
                "cohort_n": n,
                "cohort_min": round(series[0], 6) if n >= ORDER_STATS_FLOOR else None,
                "cohort_max": round(series[-1], 6) if n >= ORDER_STATS_FLOOR else None,
                "cohort_median": round(_median(series), 6) if n >= MEDIAN_FLOOR else None,
                "your_percentile": (_percentile_in(raw_you, raw_series, higher)
                                    if raw_you is not None and n > 1 else None),
            }
            comparison.append(row)

    return {
        "org_cohort": [v.tenant_id for v in vectors],
        "focus_tenant": focus_tenant,
        "readiness": readiness,
        "your_ratios": focus_ratios,
        "comparison": comparison,
        "privacy": {"k": k, "k_tenants": k_tenants, "dp_epsilon": dp_epsilon,
                    "order_stats_floor": ORDER_STATS_FLOOR, "median_floor": MEDIAN_FLOOR,
                    "note": "ratios only, k-anon per tenant, DP-noised, order stats withheld for small cohorts"},
    }


def _readiness_reason(focus, qualifying, k_tenants: int, k: int) -> str:
    if focus is None:
        return "no data for this tenant yet"
    if not focus.qualifies:
        return f"your tenant has {focus.actors} developer(s), needs >= {k} to publish a ratio"
    if len(qualifying) < k_tenants:
        return f"{len(qualifying)} of {k_tenants} required tenants qualify - invite more org units to compare"
    return "cohort ready"


def _noise(value: float, gate: KAnonymityGate) -> float:
    # ratios live in [0,~]. clamp negatives to 0 so noise can't print a nonsensical sub-zero rate.
    return max(0.0, value + gate.laplace_noise(sensitivity=_DP_SENSITIVITY))


def _noise_ratios(ratios: dict, gate: KAnonymityGate) -> dict:
    return {k: (round(_noise(v, gate), 6) if v is not None else None) for k, v in ratios.items()}
