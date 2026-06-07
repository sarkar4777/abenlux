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


def management_report(store: DerivedStore, *, k: int = 5, dp_epsilon: float = 1.0, kg=None) -> dict:
    """org-level spend->value report. all per-group figures are k-anonymity gated."""
    gate = KAnonymityGate(k=k, dp_epsilon=dp_epsilon)
    totals = store.totals()
    org_actors = totals["actors"]

    by_objective = _gate_rows(store.rollup("objective"), gate)
    by_tool = _gate_rows(store.rollup("tool"), gate)
    by_model = _gate_rows(store.rollup("model"), gate)

    # org-wide scalars: noise them, and only release when the whole org clears k
    noisy_cost = gate.noisy_count(totals["cost"], org_actors)
    orphan_share = store.orphan_token_share()

    # recoverable resent-history, as a cost band (cache-read floor .. full-input ceiling).
    # we approximate using the org blended rate implied by total cost / total tokens.
    blended = (totals["cost"] / totals["tokens"]) if totals["tokens"] else 0.0
    dup = totals["dup_tokens"]
    waste_floor = round(dup * blended * 0.1, 2)   # if fully cacheable
    waste_ceiling = round(dup * blended, 2)        # if not cached at all

    trend = spend_trend(store)  # recent vs prior window, None if not enough history

    budgets = []
    if kg is not None:
        from abenlux.analytics.budget import budget_status, current_month_bounds
        ps, pe, now = current_month_bounds()
        budgets = [b.to_dict() for b in budget_status(store, kg, period_start=ps, period_end=pe, now=now)]

    return {
        "trend": asdict(trend) if trend else None,
        "budgets": budgets,
        "org_actors": org_actors,
        "total_events": totals["n"],
        "total_tokens": totals["tokens"],
        "total_cost_usd": round(totals["cost"], 2),
        "total_cost_usd_dp": noisy_cost,             # None if org < k
        "orphan_token_share": round(orphan_share, 4),
        "unpriced_events": totals["unpriced"],
        "recoverable_resent_history_usd": {"floor": waste_floor, "ceiling": waste_ceiling},
        "by_objective": [r.__dict__ for r in by_objective],
        "by_tool": [r.__dict__ for r in by_tool],
        "by_model": [r.__dict__ for r in by_model],
        "privacy": {"k": k, "dp_epsilon": dp_epsilon, "note": "groups below k are suppressed"},
    }


def developer_report(store: DerivedStore, actor_pseudonym: str) -> dict:
    """one developer's private view. only the developer (by their own pseudonym) sees this."""
    s = store.actor_summary(actor_pseudonym)
    return {
        "actor_pseudonym": actor_pseudonym,
        "calls": s["calls"],
        "tokens": s["tokens"],
        "cost_usd": round(s["cost"], 4),
        "retry_loops": s["retries"],
        "resent_history_tokens": s["dup_tokens"],
        "note": "private to you, never visible to management",
    }
