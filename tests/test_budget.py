"""
Budgets, forecast, and the developer-private guardrail - the differentiating capability. These
prove the spend-vs-budget math, the run-rate forecast, the over/at-risk classification, the
content-free edge snapshot, the private nudge copy, and the RBAC + endpoints around it.
"""
from fastapi.testclient import TestClient

from abenlux.analytics.budget import (
    budget_status,
    current_month_bounds,
    guardrail_line,
    status_snapshot,
)
from abenlux.api import server
from abenlux.attribution.attributor import KnowledgeGraph, Objective
from abenlux.schema import DerivedRecord
from abenlux.store import DerivedStore

# a fixed synthetic period: 100 units long, we are 25% through it
PSTART, PEND, NOW = 0.0, 100.0, 25.0


def _kg(budget=1000.0):
    kg = KnowledgeGraph()
    kg.add_objective(Objective("obj-x", "Platform X", monthly_budget_usd=budget))
    kg.add_objective(Objective("obj-free", "Uncapped R&D"))  # no budget -> excluded
    return kg


def _store_with_spend(tmp_path, objective, total_cost, n=4):
    s = DerivedStore(tmp_path / "b.db")
    for i in range(n):
        s.insert(DerivedRecord(
            event_id=f"e{i}", ts=10.0 + i, tier="t", provider="anthropic",
            actor_pseudonym=f"px{i}", request_model="claude-opus-4-8",
            input_tokens=1000, output_tokens=100, duplicate_history_tokens=0,
            cost_usd=total_cost / n, cost_priced=True,
            objective_id=objective, objective_label="Platform X", is_orphan=False,
            attribution_method="ticket_join"))
    return s


def test_spend_vs_budget_and_runrate_forecast(tmp_path):
    # spent $250 at 25% elapsed -> forecast $1000 == budget -> at the edge of at_risk
    s = _store_with_spend(tmp_path, "obj-x", 250.0)
    [b] = budget_status(s, _kg(1000.0), period_start=PSTART, period_end=PEND, now=NOW)
    s.close()
    assert b.spent_usd == 250.0 and b.pct == 0.25
    assert b.forecast_usd == 1000.0          # 250 / 0.25
    assert b.fraction_elapsed == 0.25


def test_status_over_when_spent_exceeds_budget(tmp_path):
    s = _store_with_spend(tmp_path, "obj-x", 1200.0)
    [b] = budget_status(s, _kg(1000.0), period_start=PSTART, period_end=PEND, now=NOW)
    s.close()
    assert b.status == "over" and b.projected_overrun_usd > 0


def test_status_at_risk_on_forecast_overrun(tmp_path):
    # only 30% spent (not >=80%) but at 25% elapsed -> forecast 4x budget -> at_risk
    s = _store_with_spend(tmp_path, "obj-x", 300.0)
    [b] = budget_status(s, _kg(1000.0), period_start=PSTART, period_end=PEND, now=NOW)
    s.close()
    assert b.status == "at_risk"
    assert b.forecast_usd == 1200.0


def test_status_ok_when_on_track(tmp_path):
    # spent $100 at 25% elapsed -> forecast $400 < $1000 budget -> ok
    s = _store_with_spend(tmp_path, "obj-x", 100.0)
    [b] = budget_status(s, _kg(1000.0), period_start=PSTART, period_end=PEND, now=NOW)
    s.close()
    assert b.status == "ok"


def test_uncapped_objectives_excluded(tmp_path):
    s = _store_with_spend(tmp_path, "obj-free", 9999.0)
    rows = budget_status(s, _kg(), period_start=PSTART, period_end=PEND, now=NOW)
    s.close()
    assert all(r.objective_id != "obj-free" for r in rows)  # no budget -> never reported


def test_snapshot_is_content_free():
    from abenlux.analytics.budget import BudgetStatus
    snap = status_snapshot([BudgetStatus("obj-x", "Platform X", 1000, 950, 0.95, 1100, 100, "at_risk", 0.8)])
    assert set(snap["obj-x"].keys()) == {"status", "pct", "forecast_pct"}
    # no dollar figures, no labels, no identities in the edge snapshot
    assert "spent_usd" not in snap["obj-x"] and "label" not in snap["obj-x"]


def test_guardrail_copy_only_fires_when_unhealthy():
    assert guardrail_line("ACME", {"status": "ok", "pct": 0.3, "forecast_pct": 0.5}) is None
    over = guardrail_line("ACME", {"status": "over", "pct": 1.1, "forecast_pct": 1.4})
    assert over and "fully spent" in over
    risk = guardrail_line("ACME", {"status": "at_risk", "pct": 0.85, "forecast_pct": 1.2})
    assert risk and "track" in risk


def test_month_bounds_contain_now():
    ps, pe, now = current_month_bounds(1_780_000_000.0)
    assert ps <= now < pe and pe > ps


# ---- API surface + RBAC ----
def test_budget_endpoints_rbac(tmp_path, monkeypatch):
    db = str(tmp_path / "api_b.db")
    s = DerivedStore(db)
    s.insert(DerivedRecord(
        event_id="e", ts=current_month_bounds()[2] - 1, tier="t", provider="anthropic",
        actor_pseudonym="px", request_model="claude-opus-4-8",
        input_tokens=1, output_tokens=1, duplicate_history_tokens=0, cost_usd=10.0, cost_priced=True,
        objective_id="obj-acme", objective_label="Acme - Checkout Platform", is_orphan=False))
    s.close()
    monkeypatch.setattr(server, "_store", lambda: DerivedStore(db))
    monkeypatch.setattr(server, "_kg", _kg_with_cot())
    c = TestClient(server.app)

    assert c.get("/api/budgets", headers={"Authorization": "Bearer dev-token"}).status_code == 403
    body = c.get("/api/budgets", headers={"Authorization": "Bearer mgr-token"}).json()
    assert any(b["objective_id"] == "obj-acme" for b in body["budgets"])

    # edge snapshot requires the device ingest token, not a principal, is content-free
    assert c.get("/v1/budget-status").status_code == 401
    snap = c.get("/v1/budget-status", headers={"Authorization": "Bearer dev-ingest-token"}).json()
    assert "obj-acme" in snap and "spent_usd" not in snap["obj-acme"]


def _kg_with_cot():
    kg = KnowledgeGraph()
    kg.add_objective(Objective("obj-acme", "Acme - Checkout Platform", monthly_budget_usd=5000))
    return kg


def test_gateway_fires_private_budget_guardrail(tmp_path, monkeypatch):
    # over-budget objective + a captured call attributed to it -> a PRIVATE nudge in the dev feed.
    # management never sees this, it is on the developer's own device.
    from types import SimpleNamespace

    from abenlux.capture import gateway
    from abenlux.developer.feed import LocalSignalFeed

    now = current_month_bounds()[2]
    store = DerivedStore(tmp_path / "gw_b.db")
    for i in range(3):  # $2000 spent this month vs a $1000 budget -> over
        store.insert(DerivedRecord(
            event_id=f"e{i}", ts=now - 10 + i, tier="t", provider="anthropic",
            actor_pseudonym=f"px{i}", request_model="claude-opus-4-8",
            input_tokens=1, output_tokens=1, duplicate_history_tokens=0,
            cost_usd=2000 / 3, cost_priced=True, objective_id="obj-x",
            objective_label="Platform X", is_orphan=False))
    feed = LocalSignalFeed(tmp_path / "feed.jsonl")

    monkeypatch.setattr(gateway, "_store", store)
    monkeypatch.setattr(gateway, "_kg", _kg(1000.0))
    monkeypatch.setattr(gateway, "_feed", feed)
    monkeypatch.setattr(gateway, "SETTINGS", SimpleNamespace(collector_url=None, ingest_token="t"))
    # force a fresh snapshot regardless of the platform's perf_counter origin (was flaky on macOS)
    monkeypatch.setattr(gateway, "_budget_state", {"snapshot": {}, "refreshed": -1e18})

    rec = DerivedRecord(
        event_id="cur", ts=now, tier="t", provider="anthropic", actor_pseudonym="pxme",
        request_model="claude-opus-4-8", input_tokens=1, output_tokens=1,
        duplicate_history_tokens=0, cost_usd=0.5, cost_priced=True,
        objective_id="obj-x", objective_label="Platform X", is_orphan=False, embedding=None)
    result = SimpleNamespace(waste_signals=[], record=rec)
    event = SimpleNamespace(work=SimpleNamespace(tool="aider"), request_model="claude-opus-4-8")

    gateway._surface_to_developer(result, event)
    nudges = feed.recent(10)
    assert any(n["kind"] == "budget_guardrail" and "fully spent" in n["line"] for n in nudges)
