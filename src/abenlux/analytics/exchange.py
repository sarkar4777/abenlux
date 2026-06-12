"""
The cross-org benchmark, done so no one has to trust a shared collector. Every company wants to know
how its AI coding spend efficiency compares to its peers, but no company will ever hand a competitor its
raw numbers. This solves both. Each org noises its own ratios on its own machine and sends only the
blurred shares. The exchange never sees a real figure from anyone. It waits until enough orgs have
joined, then tells each org only its percentile in the group, never another org's number. The more orgs
join, the sharper everyone's percentile gets, so it pays to participate.

The store holds only blurred ratios keyed by org, no spend, no identities, no raw figures.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

# how many distinct orgs must have submitted a metric before the exchange releases a percentile for it.
COHORT_MIN_ORGS = int(__import__("os").getenv("ABEN_EXCHANGE_MIN_ORGS", "3"))


class ExchangeStore:
    def __init__(self, path: str | Path = "abenlux-exchange.db"):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS submissions (org TEXT, metric TEXT, value REAL, ts REAL,"
            " PRIMARY KEY (org, metric))")
        self.conn.commit()

    def submit(self, org: str, ratios: dict) -> int:
        n = 0
        for metric, value in (ratios or {}).items():
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            self.conn.execute(
                "INSERT OR REPLACE INTO submissions (org, metric, value, ts) VALUES (?,?,?,?)",
                (org, metric, v, time.time()))
            n += 1
        self.conn.commit()
        return n

    def rows(self) -> list[dict]:
        cur = self.conn.execute("SELECT org, metric, value FROM submissions")
        return [{"org": o, "metric": m, "value": v} for o, m, v in cur.fetchall()]

    def close(self) -> None:
        self.conn.close()


def _percentile(value: float, series: list[float], higher_is_better: bool = True) -> float:
    # where the value sits in the group, as a fraction from 0 to 1
    if len(series) < 2:
        return 0.5
    below = sum(1 for x in series if (x < value) == higher_is_better)
    return round(below / (len(series) - 1), 3)


def secure_aggregate(rows: list[dict], focus_org: str, *, k_orgs: int = COHORT_MIN_ORGS,
                     higher_is_better: dict | None = None) -> dict:
    # gather the blurred values per metric and tell the focus org only its percentile in the group.
    higher = higher_is_better or {}
    by_metric: dict[str, dict] = {}
    for r in rows:
        by_metric.setdefault(r["metric"], {})[r["org"]] = r["value"]
    comparison = []
    for metric, vals in sorted(by_metric.items()):
        orgs = len(vals)
        if orgs < k_orgs or focus_org not in vals:
            continue                          # not enough orgs joined, or the focus org has not submitted
        series = list(vals.values())
        comparison.append({
            "metric": metric, "cohort_orgs": orgs,
            "your_percentile": _percentile(vals[focus_org], series, higher.get(metric, True)),
        })
    return {
        "ready": bool(comparison),
        "focus_org": focus_org,
        "cohort_min_orgs": k_orgs,
        "comparison": comparison,
        "note": "each org blurs its own ratios before sending. the exchange returns only your percentile "
                "in the group, never another org's figure.",
    }
