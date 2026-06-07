from abenlux.analytics.reports import developer_report, management_report
from abenlux.schema import DerivedRecord
from abenlux.store import DerivedStore


def _rec(eid, actor, objective, cost, tool="aider", orphan=False):
    return DerivedRecord(
        event_id=eid, ts=0.0, tier="tier2_gateway", provider="anthropic",
        actor_pseudonym=actor, request_model="claude-opus-4-8",
        input_tokens=1000, output_tokens=100, duplicate_history_tokens=200,
        cost_usd=cost, cost_priced=True, tool=tool,
        objective_id=objective, objective_label=objective, is_orphan=orphan,
        attribution_method="none" if orphan else "ticket_join",
    )


def _store_with(tmp_path, records):
    s = DerivedStore(tmp_path / "t.db")
    for r in records:
        s.insert(r)
    return s


def test_management_report_suppresses_groups_below_k(tmp_path):
    # objective A has 6 distinct devs (>=k), objective B has 2 (<k)
    recs = [_rec(f"a{i}", f"dev{i}", "ObjA", 1.0) for i in range(6)]
    recs += [_rec(f"b{i}", f"bdev{i}", "ObjB", 5.0) for i in range(2)]
    s = _store_with(tmp_path, recs)
    rep = management_report(s, k=5)
    s.close()
    by_obj = {r["label"]: r for r in rep["by_objective"]}
    assert by_obj["ObjA"]["suppressed"] is False
    assert by_obj["ObjB"]["suppressed"] is True       # 2 devs < k=5
    assert by_obj["ObjB"]["cost"] == 0.0              # figures blanked, not leaked


def test_orphan_share_and_recoverable_band(tmp_path):
    recs = [_rec(f"x{i}", f"d{i}", "ObjA", 1.0) for i in range(5)]
    recs += [_rec(f"o{i}", f"od{i}", None, 1.0, orphan=True) for i in range(5)]
    s = _store_with(tmp_path, recs)
    rep = management_report(s, k=5)
    s.close()
    assert 0.0 < rep["orphan_token_share"] <= 1.0
    band = rep["recoverable_resent_history_usd"]
    assert band["floor"] <= band["ceiling"]


def test_developer_report_is_scoped_to_one_pseudonym(tmp_path):
    s = _store_with(tmp_path, [
        _rec("1", "me", "ObjA", 2.0),
        _rec("2", "me", "ObjA", 3.0),
        _rec("3", "other", "ObjA", 99.0),
    ])
    rep = developer_report(s, "me")
    s.close()
    assert rep["calls"] == 2
    assert round(rep["cost_usd"], 2) == 5.0  # 'other' excluded
