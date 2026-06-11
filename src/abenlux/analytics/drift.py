"""
Drift detection. A point-in-time orphan-share number tells you today's waste, the leading
governance signal is the *trend* - "unattributed AI spend rose from 18% to 34% over the last
two weeks." That is the early-warning a head of engineering acts on, before the quarterly bill.

We compare a recent window against the prior window of equal length and report movement on the
two metrics that matter for spend->value: orphan share (unattributed tokens) and total cost. A
drift is flagged only past a threshold so normal week-to-week noise doesn't cry wolf. Like every
other management surface this is computed over aggregates - no individual is in the comparison.

Deliberately simple and explainable: a manager can re-derive the number by hand from two sums.
Anomaly-detection ML is an injectable upgrade, not a prerequisite, and would only obscure a
metric whose whole value is that it is auditable.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MetricDrift:
    metric: str
    prior: float
    recent: float
    delta: float          # recent - prior
    direction: str        # "up" | "down" | "flat"
    alert: bool           # exceeded the threshold


@dataclass
class DriftReport:
    prior_window: dict
    recent_window: dict
    orphan_share: MetricDrift
    cost: MetricDrift

    @property
    def any_alert(self) -> bool:
        return self.orphan_share.alert or self.cost.alert


def _drift(metric: str, prior: float, recent: float, *, abs_threshold: float = 0.0,
           rel_threshold: float = 0.0) -> MetricDrift:
    delta = recent - prior
    direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    rel = (delta / prior) if prior else (1.0 if recent else 0.0)
    alert = (abs(delta) >= abs_threshold and abs_threshold > 0) or (rel >= rel_threshold and rel_threshold > 0 and delta > 0)
    return MetricDrift(metric, round(prior, 6), round(recent, 6), round(delta, 6), direction, alert)


def spend_trend(
    store,
    *,
    split_ts: float | None = None,
    orphan_abs_threshold: float = 0.10,   # +10 percentage points of orphan share -> alert
    cost_rel_threshold: float = 0.25,     # +25% spend window-over-window -> alert
    tenant: str | None = None,            # scope the trend to one tenant (None = org-wide / legacy)
    k: int = 1,                           # k-anonymity floor: a window with < k developers is suppressed
) -> DriftReport | None:
    """compare the recent half of the data against the prior half. returns None if there isn't
    enough history (a single window) to make a comparison. tenant scopes the trend so a tenant report
    never shows the org-wide (cross-tenant) spend/actor figures. a window backed by fewer than k
    developers has its raw cost/token/event figures suppressed - otherwise a single-developer tenant's
    exact per-window spend would leak through the one report surface that every other figure k-gates."""
    lo, hi = store.time_bounds(tenant=tenant)
    if hi <= lo:
        return None
    mid = split_ts if split_ts is not None else (lo + hi) / 2.0
    prior = store.window_stats(lo, mid, tenant=tenant)
    recent = store.window_stats(mid, hi + 1e-9, tenant=tenant)  # include the max-ts row in the recent window
    if prior["events"] == 0 or recent["events"] == 0:
        return None
    if prior["actors"] < k or recent["actors"] < k:
        # a sub-k window: suppress the raw figures and the alert (the drift signal would itself reveal
        # a single developer's spend movement). keep only the suppressed marker.
        for w in (prior, recent):
            for f in ("events", "tokens", "cost", "orphan_tokens"):
                w[f] = None
            w["suppressed"] = True
        flat = MetricDrift("suppressed", 0.0, 0.0, 0.0, "flat", False)
        return DriftReport(prior_window=prior, recent_window=recent, orphan_share=flat, cost=flat)
    return DriftReport(
        prior_window=prior,
        recent_window=recent,
        orphan_share=_drift("orphan_share", prior["orphan_share"], recent["orphan_share"],
                            abs_threshold=orphan_abs_threshold),
        cost=_drift("cost", prior["cost"], recent["cost"], rel_threshold=cost_rel_threshold),
    )
