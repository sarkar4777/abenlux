"""
Reporting. This is where the governance split becomes code, not policy.

Two audiences, two hard-separated surfaces:

  * MANAGEMENT view - spend attributed to objectives, orphan share, recoverable waste, model
    mix. Every row is an aggregate over many people, and every aggregate passes the
    k-anonymity gate (default k>=5) before it is allowed to render. A group smaller than k is
    SUPPRESSED, not shown noisily - you cannot back out an individual from a k>=5 bucket.
    Cross-team totals get Laplace DP noise. There is no per-developer drill-down here at all.

  * DEVELOPER view - one person's own spend, retries, and resent-history waste, keyed by their
    own pseudonym. Private to them. This is the only place individual rows exist, and it never
    feeds a management surface. That asymmetry is the trust architecture.

Recoverable-waste is reported as a cost band, not a single confident number: resent-history is
billed at the cache-read rate if the tool caches and the full input rate if it does not, so the
honest answer is a range. Reporting a single figure here would be the same "confident wrong
number" the cost model refuses to print for unpriced models.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from abenlux.analytics.drift import spend_trend
from abenlux.privacy.pseudonymize import KAnonymityGate
from abenlux.store import DerivedStore


@dataclass
class RollupRow:
    label: str
    calls: int
    tokens: int
    cost: float
    actors: int
    suppressed: bool = False  # True -> below k, figures hidden, only the fact-of-existence shown


def _gate_rows(rows: list[dict], gate: KAnonymityGate) -> list[RollupRow]:
    out: list[RollupRow] = []
    for r in rows:
        if gate.allows(r["actors"]):
            out.append(RollupRow(r["label"], r["calls"], r["tokens"], round(r["cost"], 4), r["actors"]))
        else:
            # keep the row's existence but blank the figures, never leak a sub-k aggregate
            out.append(RollupRow(r["label"], 0, 0, 0.0, r["actors"], suppressed=True))
    return out


def management_report(store: DerivedStore, *, k: int = 5, dp_epsilon: float = 1.0, kg=None,
                      tenant: str | None = None) -> dict:
    """org-level spend->value report. all per-group figures are k-anonymity gated. tenant scopes the
    whole report to one org unit / geography (None = org-wide / legacy single-tenant)."""
    gate = KAnonymityGate(k=k, dp_epsilon=dp_epsilon)
    totals = store.totals(tenant=tenant)
    org_actors = totals["actors"]

    by_objective = _gate_rows(store.rollup("objective", tenant=tenant), gate)
    by_tool = _gate_rows(store.rollup("tool", tenant=tenant), gate)
    by_model = _gate_rows(store.rollup("model", tenant=tenant), gate)

    # org-wide scalars: noise them, and only release when the whole org clears k
    noisy_cost = gate.noisy_count(totals["cost"], org_actors)
    orphan_share = store.orphan_token_share(tenant=tenant)

    # recoverable resent-history, cache-AWARE. resent history that is already a cache hit is cheap
    # and not recoverable, so we only count the prefix that was billed as fresh input
    # (dup - cache_read). priced as a band: cache-read floor .. full-input ceiling.
    blended = (totals["cost"] / totals["tokens"]) if totals["tokens"] else 0.0
    dup = totals["dup_tokens"]
    uncached_dup = max(0, dup - totals.get("cache_read", 0))
    waste_floor = round(uncached_dup * blended * 0.1, 2)   # if made fully cacheable
    waste_ceiling = round(uncached_dup * blended, 2)        # if it stays uncached
    # cache-hit ratio: the share of resendable input already served from cache. high = efficient.
    cache_read = totals.get("cache_read", 0)
    cache_base = cache_read + totals.get("input_tokens", 0)
    cache_hit_ratio = round(cache_read / cache_base, 4) if cache_base else 0.0

    trend = spend_trend(store, tenant=tenant)  # recent vs prior window, None if not enough history

    budgets = []
    if kg is not None:
        from abenlux.analytics.budget import budget_status, current_month_bounds
        ps, pe, now = current_month_bounds()
        # k-anonymity also applies to budgets: an objective with 1-2 developers must not expose its
        # spend/forecast here (it would bypass the gate that suppresses the same group in by_objective).
        obj_actors = {r["label"]: r["actors"] for r in store.rollup("objective", tenant=tenant)}
        for b in budget_status(store, kg, period_start=ps, period_end=pe, now=now, tenant=tenant):
            row = b.to_dict()
            actors = obj_actors.get(b.label, 0)
            if 0 < actors < k:                          # sub-k and non-empty -> hide the spend figures
                for f in ("spent_usd", "pct", "forecast_usd", "projected_overrun_usd"):
                    row[f] = None
                row["status"] = "suppressed"
                row["suppressed"] = True
            else:
                row["suppressed"] = False
            budgets.append(row)

    # WHAT the spend is for: purpose mix + net-new vs maintenance investment + new initiatives
    from abenlux.attribution.attributor import MAINTENANCE, NET_NEW
    wt_rows = store.rollup("work_type", tenant=tenant)
    by_work_type = _gate_rows(wt_rows, gate)
    investment = {"net_new": 0.0, "maintenance": 0.0, "unclassified": 0.0}
    for r in wt_rows:
        if not gate.allows(r["actors"]):
            continue  # a work-type with fewer than k developers is suppressed here too, not summed in
        lbl = r["label"]
        cat = "net_new" if lbl in NET_NEW else ("maintenance" if lbl in MAINTENANCE else "unclassified")
        investment[cat] += r["cost"]
    investment = {k2: round(v, 2) for k2, v in investment.items()}
    lo, hi = store.time_bounds(tenant=tenant)
    since = (lo + hi) / 2 if hi > lo else lo
    new_initiatives = []
    for o in store.new_objectives(since, tenant=tenant):
        new_initiatives.append({
            "label": o["objective_label"], "work_type": o["work_type"], "actors": o["actors"],
            "cost": round(o["cost"], 2) if gate.allows(o["actors"]) else None,  # k-gate the figure
        })

    return {
        "tenant": tenant,
        "trend": asdict(trend) if trend else None,
        "budgets": budgets,
        "by_work_type": [r.__dict__ for r in by_work_type],
        "investment": investment,
        "new_initiatives": new_initiatives,
        "org_actors": org_actors,
        "total_events": totals["n"],
        "total_tokens": totals["tokens"],
        "total_cost_usd": round(totals["cost"], 2),
        "total_cost_usd_dp": noisy_cost,             # None if org < k
        "orphan_token_share": round(orphan_share, 4),
        "unpriced_events": totals["unpriced"],
        "cache_hit_ratio": cache_hit_ratio,
        "cache_read_tokens": cache_read,
        "recoverable_resent_history_usd": {"floor": waste_floor, "ceiling": waste_ceiling},
        "by_objective": [r.__dict__ for r in by_objective],
        "by_tool": [r.__dict__ for r in by_tool],
        "by_model": [r.__dict__ for r in by_model],
        "privacy": {"k": k, "dp_epsilon": dp_epsilon, "note": "groups below k are suppressed"},
    }


def developer_report(store: DerivedStore, actor_pseudonym: str) -> dict:
    """one developer's private view. only the developer (by their own pseudonym) sees this."""
    s = store.actor_summary(actor_pseudonym)
    cache_base = s.get("cache_read", 0) + s.get("input_tokens", 0)
    return {
        "actor_pseudonym": actor_pseudonym,
        "calls": s["calls"],
        "tokens": s["tokens"],
        "cost_usd": round(s["cost"], 4),
        "retry_loops": s["retries"],
        "resent_history_tokens": s["dup_tokens"],
        "cache_read_tokens": s.get("cache_read", 0),
        "cache_hit_ratio": round(s.get("cache_read", 0) / cache_base, 4) if cache_base else 0.0,
        "uncached_resent_tokens": max(0, s["dup_tokens"] - s.get("cache_read", 0)),
        "work_type_mix": [{"label": r["label"], "cost": round(r["cost"], 4), "calls": r["calls"]}
                          for r in store.actor_work_types(actor_pseudonym)],
        "note": "private to you, never visible to management",
    }
