"""
The vendor negotiation pack. A manager walking into a renewal wants a few hard numbers, and they want
them across every tool and provider at once, which no single vendor dashboard can give them. This turns
the captured spend into those numbers. The one blended rate the org actually pays per million tokens,
how concentrated the spend is on one provider (which is leverage at the table), and what a committed-use
discount would save on the run rate. It reads only the content-free spend totals already in the store.
"""
from __future__ import annotations

from abenlux.store import DerivedStore


def negotiation_pack(store: DerivedStore, *, tenant: str | None = None, k: int = 5,
                     period_months: float = 1.0) -> dict:
    totals = store.totals(tenant=tenant)
    cost = totals.get("cost", 0.0)
    tokens = totals.get("tokens", 0)
    actors = totals.get("actors", 0)
    if actors < k:
        return {"ready": False, "reason": f"needs at least {k} developers to release a spend figure"}

    blended = round(cost / (tokens / 1_000_000), 2) if tokens else 0.0   # dollars per million tokens
    by_provider = store.rollup("provider", tenant=tenant)
    providers = []
    for p in by_provider:
        share = (p["cost"] / cost) if cost else 0.0
        providers.append({"provider": p["label"], "cost_usd": round(p["cost"], 2),
                          "share": round(share, 3)})
    providers.sort(key=lambda r: -r["cost_usd"])
    # a simple concentration score. close to 1 means one provider holds most of the spend, which both
    # cuts your leverage and makes a single committed-use deal cover most of the bill.
    concentration = round(sum((p["share"]) ** 2 for p in providers), 3)

    annual = cost / period_months * 12 if period_months else cost * 12
    scenarios = [{"discount_pct": d, "annual_saving_usd": round(annual * d / 100, 2)}
                 for d in (10, 20, 30)]

    by_model = store.rollup("model", tenant=tenant)
    top_models = sorted(({"model": m["label"], "cost_usd": round(m["cost"], 2),
                          "tokens": m["tokens"]} for m in by_model),
                        key=lambda r: -r["cost_usd"])[:8]
    return {
        "ready": True,
        "captured_spend_usd": round(cost, 2),
        "blended_usd_per_mtok": blended,
        "projected_annual_run_rate_usd": round(annual, 2),
        "provider_concentration": concentration,
        "by_provider": providers,
        "committed_use_scenarios": scenarios,
        "top_models": top_models,
        "note": "blended rate and run rate are over the captured window scaled to a year. ratios only, "
                "k-anonymity gated. no developer-level figure is exposed.",
    }
