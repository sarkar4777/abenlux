"""
Budgets, forecast, and guardrails - the capability the research says no existing tool combines.

LLM gateways (LiteLLM, Portkey, Bifrost, Helicone) enforce budgets, but keyed by virtual API key
or team, at an application's API boundary, divorced from business value and aimed at management. AI
coding analytics (Cursor, Copilot) report per-developer usage but are single-vendor and surveillance-
shaped. None tie a budget to a business objective via work-context join, none cover the spread of
IDE/CLI coding tools, and none surface the warning to the developer while staying blind to the
individual at the management layer.

Abenlux can, because it already attributes spend to an objective. This module turns that into:
  * spend-vs-budget per objective, as unit economics ("$ of the ACME budget consumed"),
  * a run-rate FORECAST to period end (spent / fraction-of-period-elapsed) and projected overrun -
    the early warning that stops an Uber-style "full-year budget gone in four months",
  * a content-free status snapshot the on-device edge agent polls to nudge the developer PRIVATELY
    ("the ACME budget is 90% spent with 9 days left") - never a manager-visible alert on a person.

All math is explainable and auditable, a finance lead can re-derive every number from two sums.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

# status thresholds, deliberately simple
_AT_RISK_PCT = 0.80          # >=80% of budget already spent -> at risk
_FORECAST_OVER_MARGIN = 1.0  # forecast > budget -> on track to overrun


@dataclass
class BudgetStatus:
    objective_id: str
    label: str
    budget_usd: float
    spent_usd: float
    pct: float                 # spent / budget
    forecast_usd: float        # run-rate projection to period end
    projected_overrun_usd: float
    status: str                # "ok" | "at_risk" | "over"
    fraction_elapsed: float

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def current_month_bounds(now: float | None = None) -> tuple[float, float, float]:
    """(period_start, period_end, now) for the current calendar month in UTC. monthly budgets
    are the FinOps norm, swap for a sprint/quarter window by passing explicit bounds."""
    now = now if now is not None else time.time()
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    start = datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)
    end = datetime(dt.year + (dt.month == 12), (dt.month % 12) + 1, 1, tzinfo=timezone.utc)
    return start.timestamp(), end.timestamp(), now


def _status(spent: float, budget: float, forecast: float) -> str:
    if budget <= 0:
        return "ok"
    if spent >= budget:
        return "over"
    if (spent / budget) >= _AT_RISK_PCT or forecast > budget * _FORECAST_OVER_MARGIN:
        return "at_risk"
    return "ok"


def budget_status(
    store, kg, *, period_start: float, period_end: float, now: float
) -> list[BudgetStatus]:
    """per-objective spend-vs-budget with run-rate forecast. only objectives that declare a
    budget are included, the rest are uncapped by definition."""
    span = max(period_end - period_start, 1e-9)
    elapsed = min(max((now - period_start) / span, 0.0), 1.0)
    out: list[BudgetStatus] = []
    for obj in kg.objectives.values():
        if not obj.monthly_budget_usd:
            continue
        spent = store.objective_window_cost(obj.id, period_start, now + 1e-9)
        # floor the elapsed fraction used for the run-rate projection: a small spend a few minutes into
        # the period must not extrapolate to an absurd monthly forecast (caps the multiple at ~25x).
        forecast = spent / max(elapsed, 0.04)
        budget = float(obj.monthly_budget_usd)
        out.append(BudgetStatus(
            objective_id=obj.id, label=obj.label, budget_usd=budget,
            spent_usd=round(spent, 4), pct=round(spent / budget, 4) if budget else 0.0,
            forecast_usd=round(forecast, 4),
            projected_overrun_usd=round(max(0.0, forecast - budget), 4),
            status=_status(spent, budget, forecast), fraction_elapsed=round(elapsed, 4),
        ))
    out.sort(key=lambda b: b.pct, reverse=True)
    return out


def status_snapshot(statuses: list[BudgetStatus]) -> dict[str, dict]:
    """compact, content-free map the edge agent polls. objective_id -> {status, pct, forecast_pct}.
    carries no spend figures or identities - just enough to drive a private developer nudge."""
    return {
        s.objective_id: {
            "status": s.status,
            "pct": round(min(s.pct, 9.99), 3),
            "forecast_pct": round(s.forecast_usd / s.budget_usd, 3) if s.budget_usd else 0.0,
        }
        for s in statuses
    }


def guardrail_line(objective_label: str, snap: dict) -> str | None:
    """developer-facing copy for a budget nudge, or None if the objective is healthy."""
    status = snap.get("status")
    if status == "over":
        return (f"Heads up: the {objective_label} AI budget is fully spent for this period "
                f"({int(snap['pct']*100)}%). Consider a smaller model or trimming context until reset.")
    if status == "at_risk":
        fp = int(snap.get("forecast_pct", 0) * 100)
        return (f"The {objective_label} AI budget is {int(snap['pct']*100)}% spent and on track for "
                f"~{fp}% by period end. A cheaper model for routine calls would keep it in bounds.")
    return None
