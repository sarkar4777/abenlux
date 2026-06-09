"""
Multi-tenant foundation + the two flagship features: the Reuse-Yield Ledger (money NOT spent, booked
beside money spent) and the cross-tenant Benchmark Exchange (geographies of one org compared, k-anon +
DP + cohort-gated). These tests pin the privacy walls as hard as the spend-side tests do: a tenant's
data scopes cleanly, a sub-k group never publishes a ratio, and no cross-org cohort ever forms.
"""
from fastapi.testclient import TestClient

from abenlux.analytics.benchmark import benchmark, tenant_vector
from abenlux.analytics.reports import management_report
from abenlux.api import server
from abenlux.auth.principals import PrincipalStore
from abenlux.auth.rbac import Permission, Principal, Role, permissions_for
from abenlux.ledger import (
    AvoidedCostEvent,
    LedgerStore,
    cost_to_solve,
    estimate_avoided,
    median,
)
from abenlux.schema import DerivedRecord
from abenlux.store import DerivedStore
from abenlux.tenants import Tenant, TenantStore


def _rec(eid, actor, tenant="default", objective="ObjA", work_type="feature", cost=1.0,
         embedding=None):
    return DerivedRecord(
        event_id=eid, ts=1.0, tier="tier2_gateway", provider="anthropic",
        actor_pseudonym=actor, request_model="claude-opus-4-8",
        input_tokens=1000, output_tokens=100, duplicate_history_tokens=0,
        cost_usd=cost, cost_priced=True, tool="aider",
        objective_id=objective, objective_label=objective, is_orphan=False,
        attribution_method="ticket_join", work_type=work_type, tenant_id=tenant,
        embedding=embedding,
    )


# ----------------------------- tenant registry -----------------------------

def test_tenant_store_create_list_and_org_scoping(tmp_path):
    s = TenantStore(tmp_path / "t.db")
    s.upsert(Tenant("acme-eu", "acme", "ACME EU", "eu", 1.0))
    s.upsert(Tenant("acme-us", "acme", "ACME US", "us", 2.0))
    s.upsert(Tenant("globex-eu", "globex", "Globex EU", "eu", 3.0))
    assert {t.tenant_id for t in s.list(org="acme")} == {"acme-eu", "acme-us"}
    assert [t.tenant_id for t in s.list(org="globex")] == ["globex-eu"]
    assert s.org_of("acme-us") == "acme"
    assert s.org_of("nope") is None
    s.close()


def test_tenant_upsert_is_idempotent(tmp_path):
    s = TenantStore(tmp_path / "t.db")
    s.upsert(Tenant("acme-eu", "acme", "old name", "eu", 1.0))
    s.upsert(Tenant("acme-eu", "acme", "new name", "eu", 1.0))
    assert len(s.list()) == 1
    assert s.get("acme-eu").display_name == "new name"
    s.close()


# ----------------------------- tenant-scoped store/report -----------------------------

def test_store_scopes_totals_and_rollup_by_tenant(tmp_path):
    st = DerivedStore(tmp_path / "s.db")
    for i in range(3):
        st.insert(_rec(f"eu{i}", f"a{i}", tenant="acme-eu", cost=2.0))
    for i in range(2):
        st.insert(_rec(f"us{i}", f"b{i}", tenant="acme-us", cost=5.0))
    assert round(st.totals(tenant="acme-eu")["cost"], 2) == 6.0
    assert round(st.totals(tenant="acme-us")["cost"], 2) == 10.0
    assert round(st.totals()["cost"], 2) == 16.0          # None = whole org
    eu_obj = st.rollup("objective", tenant="acme-eu")
    assert eu_obj[0]["actors"] == 3 and round(eu_obj[0]["cost"], 2) == 6.0
    st.close()


def test_default_tenant_matches_legacy_null_rows(tmp_path):
    # rows written before the tenant column existed are NULL, and must still belong to "default"
    st = DerivedStore(tmp_path / "s.db")
    st.conn.execute("INSERT INTO derived (event_id, ts, cost_usd, actor_pseudonym, input_tokens, "
                    "output_tokens) VALUES ('legacy', 1.0, 3.0, 'old', 10, 1)")
    st.conn.commit()
    st.insert(_rec("new", "fresh", tenant="default", cost=1.0))
    assert round(st.totals(tenant="default")["cost"], 2) == 4.0   # NULL + 'default' both counted
    st.close()


def test_management_report_scoped_to_tenant(tmp_path):
    st = DerivedStore(tmp_path / "s.db")
    for i in range(5):
        st.insert(_rec(f"eu{i}", f"a{i}", tenant="acme-eu", cost=2.0))
    for i in range(5):
        st.insert(_rec(f"us{i}", f"b{i}", tenant="acme-us", cost=9.0))
    rep = management_report(st, k=5, tenant="acme-eu")
    assert rep["tenant"] == "acme-eu"
    assert round(rep["total_cost_usd"], 2) == 10.0          # only EU, not the 45 from US
    st.close()


# ----------------------------- RBAC tenant walls -----------------------------

def test_benchmark_permission_sits_with_management():
    assert Permission.VIEW_BENCHMARK in permissions_for(Role.MANAGER)
    assert Permission.VIEW_BENCHMARK in permissions_for(Role.ADMIN)
    assert Permission.VIEW_BENCHMARK not in permissions_for(Role.DEVELOPER)


def test_principal_carries_tenant_and_org_from_yaml(tmp_path):
    y = tmp_path / "p.yaml"
    y.write_text(
        "principals:\n"
        "  - token: t1\n    subject: a@x\n    role: manager\n    tenant_id: acme-us\n    org: acme\n"
    )
    store = PrincipalStore.from_yaml(str(y), hmac_key=b"k")
    p = store.resolve("t1")
    assert p.tenant_id == "acme-us" and p.org == "acme"


# ----------------------------- reuse-yield ledger -----------------------------

def test_median_and_estimate_avoided():
    assert median([]) == 0.0
    assert median([3.0]) == 3.0
    assert median([1.0, 3.0]) == 2.0
    assert median([5.0, 1.0, 3.0]) == 3.0
    # solved reuse credits the full cost-to-solve, live duplication a conservative half
    assert estimate_avoided([2.0, 4.0], "solved_reuse") == 3.0
    assert estimate_avoided([2.0, 4.0], "live_duplication") == 1.5
    assert estimate_avoided([], "solved_reuse") == 0.0


def test_cost_to_solve_is_never_one_developers_exact_figure():
    # winsorized mean: trims the extremes and averages the rest, so a runaway session can't inflate it
    # and no single developer's exact spend is ever the published figure (the odd-n median leak).
    costs = [1.0, 2.0, 4.0, 8.0, 100.0]      # one runaway 100.0
    cts = cost_to_solve(costs)
    assert cts == (2.0 + 4.0 + 8.0) / 3             # trims 1.0 and 100.0, averages the middle three
    assert cts not in costs                          # structurally a blend, not any one dev's raw spend
    assert cts < 10.0                                # the runaway 100.0 cannot inflate it
    assert cost_to_solve([]) == 0.0


def _seed_cohort(store, *, tenant, objective, work_type, n, cost):
    # n distinct developers each with one record on objective x work_type in tenant -> cost-to-solve
    for i in range(n):
        store.insert(_rec(f"{tenant}-{objective}-{work_type}-{i}", f"{tenant}-dev{i}", tenant=tenant,
                          objective=objective, work_type=work_type, cost=cost))


def test_ledger_books_dedups_and_upgrades(tmp_path):
    store = DerivedStore(tmp_path / "s.db")
    _seed_cohort(store, tenant="acme-eu", objective="ObjA", work_type="feature", n=5, cost=2.0)
    led = LedgerStore(tmp_path / "l.db")
    ev = AvoidedCostEvent("acme-eu", "ObjA", "feature", "topic", 0.0, "live_duplication", 0, 1.0)
    assert led.book(ev, pair=("a", "b")) is True
    # same opportunity (pair x objective x work_type), even reversed pair, is not booked twice
    assert led.book(ev, pair=("b", "a")) is False
    # a label drift on the same opportunity must NOT re-book (cluster_id is not part of the dedup key)
    drift = AvoidedCostEvent("acme-eu", "ObjA", "feature", "topic-renamed", 0.0, "live_duplication", 0, 1.0)
    assert led.book(drift, pair=("a", "b")) is False
    # but a confirmed solved-reuse upgrades the prior live-duplication for the same opportunity
    up = AvoidedCostEvent("acme-eu", "ObjA", "feature", "topic", 0.0, "solved_reuse", 0, 1.0)
    assert led.book(up, pair=("a", "b")) is True
    summ = led.summary(store, "acme-eu", k=5)
    # one credited opportunity, now solved -> full cost-to-solve (2.0), not doubled
    assert summ["events_credited"] == 1 and summ["reuse_avoided_usd"] == 2.0
    led.close()
    store.close()


def test_ledger_k_gates_savings_at_read_time(tmp_path):
    store = DerivedStore(tmp_path / "s.db")
    _seed_cohort(store, tenant="t", objective="Big", work_type="fix", n=5, cost=4.0)    # >= k
    _seed_cohort(store, tenant="t", objective="Small", work_type="fix", n=2, cost=10.0)  # < k
    led = LedgerStore(tmp_path / "l.db")
    led.book(AvoidedCostEvent("t", "Big", "fix", "c1", 0, "solved_reuse", 0, 1.0), pair=("a", "b"))
    led.book(AvoidedCostEvent("t", "Small", "fix", "c2", 0, "solved_reuse", 0, 1.0), pair=("c", "d"))
    summ = led.summary(store, "t", k=5)
    assert summ["reuse_avoided_usd"] == 4.0                       # only the >= k objective credited
    assert summ["events_credited"] == 1 and summ["events_suppressed"] == 1
    led.close()
    store.close()


def test_ledger_credits_later_once_cohort_grows(tmp_path):
    # the read-time recompute fix: an opportunity booked when the cohort was below k is credited LATER,
    # automatically, once enough developers have solved that work - no re-poll of the original pair.
    store = DerivedStore(tmp_path / "s.db")
    _seed_cohort(store, tenant="t", objective="O", work_type="feature", n=3, cost=2.0)
    led = LedgerStore(tmp_path / "l.db")
    led.book(AvoidedCostEvent("t", "O", "feature", "c", 0, "solved_reuse", 0, 1.0), pair=("a", "b"))
    assert led.summary(store, "t", k=5)["reuse_avoided_usd"] == 0.0   # only 3 devs so far -> suppressed
    # two more developers solve the same work; the SAME booked opportunity now clears k
    store.insert(_rec("t-O-feature-3", "t-dev3", tenant="t", objective="O", work_type="feature", cost=2.0))
    store.insert(_rec("t-O-feature-4", "t-dev4", tenant="t", objective="O", work_type="feature", cost=2.0))
    assert led.summary(store, "t", k=5)["reuse_avoided_usd"] == 2.0   # now credited, no re-book needed
    led.close()
    store.close()


def test_ledger_summary_scopes_by_tenant(tmp_path):
    store = DerivedStore(tmp_path / "s.db")
    _seed_cohort(store, tenant="acme-eu", objective="O", work_type="feature", n=5, cost=5.0)
    _seed_cohort(store, tenant="acme-us", objective="O", work_type="feature", n=5, cost=9.0)
    led = LedgerStore(tmp_path / "l.db")
    led.book(AvoidedCostEvent("acme-eu", "O", "feature", "c", 0, "solved_reuse", 0, 1.0), pair=("a", "b"))
    led.book(AvoidedCostEvent("acme-us", "O", "feature", "c", 0, "solved_reuse", 0, 1.0), pair=("a", "b"))
    assert led.summary(store, "acme-eu", k=5)["reuse_avoided_usd"] == 5.0
    assert led.summary(store, "acme-us", k=5)["reuse_avoided_usd"] == 9.0
    led.close()
    store.close()


# ----------------------------- benchmark exchange -----------------------------

def _seed_three_tenants(st, *, actors=5):
    # three tenants, each clearing k, with deliberately different efficiency profiles
    for t, cost in [("acme-eu", 1.0), ("acme-us", 4.0), ("acme-apac", 2.0)]:
        for i in range(actors):
            st.insert(_rec(f"{t}{i}", f"{t}-a{i}", tenant=t, cost=cost))


def test_tenant_vector_is_ratios_only(tmp_path):
    st = DerivedStore(tmp_path / "s.db")
    _seed_three_tenants(st)
    v = tenant_vector(st, "acme-eu", k=5)
    assert v.qualifies and v.actors == 5
    assert set(v.ratios) >= {"cost_per_1k_tokens", "cache_hit_ratio", "orphan_share", "net_new_share"}
    # net_new because work_type=feature is net-new; ratio in [0,1]
    assert 0.0 <= v.ratios["net_new_share"] <= 1.0
    st.close()


def test_benchmark_ready_with_cohort_and_gives_percentiles(tmp_path):
    st = DerivedStore(tmp_path / "s.db")
    _seed_three_tenants(st)
    out = benchmark(st, tenants=["acme-eu", "acme-us", "acme-apac"], focus_tenant="acme-eu",
                    k=5, k_tenants=3)
    assert out["readiness"]["ready"] is True
    assert out["readiness"]["cohort_size"] == 3
    comp = {c["metric"]: c for c in out["comparison"]}
    # eu is the cheapest per-1k (cost 1.0 vs 4.0 / 2.0), lower-is-better -> top percentile
    cpk = comp["cost_per_1k_tokens"]
    assert cpk["higher_is_better"] is False
    assert cpk["your_percentile"] == 1.0
    for c in out["comparison"]:
        assert c["your_percentile"] is None or 0.0 <= c["your_percentile"] <= 1.0


def test_benchmark_withholds_order_stats_for_small_cohort(tmp_path):
    # for a 3-tenant cohort, min/median/max ARE the three tenants' near-raw values - so they are
    # withheld (None). only the percentile (rank) is released. this closes the raw-ratio leak.
    st = DerivedStore(tmp_path / "s.db")
    _seed_three_tenants(st)
    out = benchmark(st, tenants=["acme-eu", "acme-us", "acme-apac"], focus_tenant="acme-eu",
                    k=5, k_tenants=3)
    for c in out["comparison"]:
        assert c["cohort_min"] is None and c["cohort_max"] is None   # < ORDER_STATS_FLOOR (5)
        assert c["cohort_median"] is None                            # < MEDIAN_FLOOR (4)
        assert c["your_percentile"] is not None                      # rank is still safe to release
    st.close()


def test_benchmark_release_is_internally_consistent_for_large_cohort(tmp_path):
    # with >= ORDER_STATS_FLOOR tenants, min/median/max are released and must be mutually consistent:
    # you sits within [min,max], min <= median <= max - derived from one noised series, so no
    # contradiction (the independently-noised-statistics bug).
    st = DerivedStore(tmp_path / "s.db")
    for j, cost in enumerate([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]):
        t = f"acme-{j}"
        for i in range(5):
            st.insert(_rec(f"{t}{i}", f"{t}-a{i}", tenant=t, cost=cost))
    cohort = [f"acme-{j}" for j in range(6)]
    out = benchmark(st, tenants=cohort, focus_tenant="acme-0", k=5, k_tenants=3)
    assert out["readiness"]["ready"] is True
    for c in out["comparison"]:
        if c["cohort_min"] is None:
            continue
        assert c["cohort_min"] <= c["cohort_median"] <= c["cohort_max"]   # monotone
        if c["you"] is not None:
            assert c["cohort_min"] <= c["you"] <= c["cohort_max"]         # you within the envelope
    st.close()


def test_benchmark_excludes_unpriced_tenant_from_cost_metrics(tmp_path):
    # a tenant on an unpriced model (cost=0) would look "free" and collapse the cohort cost minimum.
    # it must be excluded from cost-denominated metrics but still appear in token/event metrics.
    st = DerivedStore(tmp_path / "s.db")
    _seed_three_tenants(st)
    for i in range(5):     # unpriced tenant: real tokens, zero priced cost
        st.insert(DerivedRecord(
            event_id=f"up{i}", ts=1.0, tier="t", provider="x", actor_pseudonym=f"up-a{i}",
            request_model="some-unknown-model", input_tokens=1000, output_tokens=100,
            duplicate_history_tokens=0, cost_usd=0.0, cost_priced=False, tool="aider",
            objective_id="ObjA", objective_label="ObjA", is_orphan=False, work_type="feature",
            tenant_id="acme-unpriced"))
    v = tenant_vector(st, "acme-unpriced", k=5)
    assert v.ratios["cost_per_1k_tokens"] is None        # cost metric N/A for an unpriced tenant
    assert v.ratios["cache_hit_ratio"] is not None       # token metric still computed
    st.close()


def test_benchmark_not_ready_below_cohort_threshold(tmp_path):
    st = DerivedStore(tmp_path / "s.db")
    for i in range(5):
        st.insert(_rec(f"eu{i}", f"a{i}", tenant="acme-eu"))
    out = benchmark(st, tenants=["acme-eu"], focus_tenant="acme-eu", k=5, k_tenants=3)
    assert out["readiness"]["ready"] is False
    assert out["comparison"] == []
    assert out["your_ratios"]            # you still see your OWN ratios, just no cohort comparison
    st.close()


def test_benchmark_suppresses_subk_tenant_from_cohort(tmp_path):
    st = DerivedStore(tmp_path / "s.db")
    _seed_three_tenants(st)
    # a 2-developer tenant must not be admitted to the cohort (its ratio could be backed out)
    for i in range(2):
        st.insert(_rec(f"tiny{i}", f"tiny-a{i}", tenant="acme-tiny"))
    out = benchmark(st, tenants=["acme-eu", "acme-us", "acme-apac", "acme-tiny"],
                    focus_tenant="acme-tiny", k=5, k_tenants=3)
    assert out["readiness"]["focus_qualifies"] is False
    assert out["readiness"]["ready"] is False     # focus tenant itself is sub-k


# ----------------------------- API integration -----------------------------

def _principals():
    # an org "acme" with a manager in acme-eu, plus a manager in a DIFFERENT org "globex"
    k = b"test-key"
    from abenlux.privacy.pseudonymize import pseudonymize
    return PrincipalStore({
        "acme-mgr": Principal("m@acme", "Acme Mgr", Role.MANAGER, pseudonymize("m@acme", k),
                              tenant_id="acme-eu", org="acme"),
        "acme-admin": Principal("ad@acme", "Acme Admin", Role.ADMIN, pseudonymize("ad@acme", k),
                                tenant_id="acme-eu", org="acme"),
        "globex-mgr": Principal("m@globex", "Globex Mgr", Role.MANAGER, pseudonymize("m@globex", k),
                                tenant_id="globex-eu", org="globex"),
    })


def _wire(monkeypatch, tmp_path):
    db = str(tmp_path / "central.db")
    ledger_db = str(tmp_path / "ledger.db")
    tenant_db = str(tmp_path / "tenants.db")
    monkeypatch.setattr(server, "_store", lambda: DerivedStore(db))
    monkeypatch.setattr(server, "_ledger", lambda: LedgerStore(ledger_db))
    monkeypatch.setattr(server, "_tenants", lambda: TenantStore(tenant_db))
    monkeypatch.setattr(server, "_principals", _principals())
    return db, ledger_db, tenant_db


def test_api_report_scopes_to_principal_tenant(tmp_path, monkeypatch):
    db, _, _ = _wire(monkeypatch, tmp_path)
    st = DerivedStore(db)
    for i in range(5):
        st.insert(_rec(f"eu{i}", f"a{i}", tenant="acme-eu", cost=2.0))
    for i in range(5):
        st.insert(_rec(f"us{i}", f"b{i}", tenant="acme-us", cost=9.0))
    st.close()
    client = TestClient(server.app)
    rep = client.get("/api/report", headers={"Authorization": "Bearer acme-mgr"}).json()
    assert rep["tenant"] == "acme-eu"
    assert round(rep["total_cost_usd"], 2) == 10.0   # the manager sees only their own tenant
    assert "reuse_yield" in rep


def test_api_report_cross_org_tenant_is_forbidden(tmp_path, monkeypatch):
    _, _, tenant_db = _wire(monkeypatch, tmp_path)
    ts = TenantStore(tenant_db)
    ts.upsert(Tenant("acme-us", "acme", "ACME US", "us", 1.0))
    ts.close()
    client = TestClient(server.app)
    # acme manager may pull a sibling tenant in the SAME org
    ok = client.get("/api/report?tenant=acme-us", headers={"Authorization": "Bearer acme-mgr"})
    assert ok.status_code == 200 and ok.json()["tenant"] == "acme-us"
    # but a globex manager may NOT read an acme tenant
    no = client.get("/api/report?tenant=acme-us", headers={"Authorization": "Bearer globex-mgr"})
    assert no.status_code == 403


def test_api_create_tenant_binds_to_callers_org(tmp_path, monkeypatch):
    _wire(monkeypatch, tmp_path)
    client = TestClient(server.app)
    # a manager lacks MANAGE -> cannot create a tenant
    assert client.post("/api/tenants", json={"tenant_id": "acme-apac"},
                       headers={"Authorization": "Bearer acme-mgr"}).status_code == 403
    # an admin can, and the tenant is bound to the admin's own org regardless of any smuggled field
    r = client.post("/api/tenants", json={"tenant_id": "acme-apac", "org": "globex"},
                    headers={"Authorization": "Bearer acme-admin"})
    assert r.status_code == 200
    assert r.json()["tenant"]["org"] == "acme"          # org forced to the caller's, not "globex"


def test_api_full_loop_books_reuse_yield_and_serves_savings(tmp_path, monkeypatch):
    # five devs on the same objective+work_type with identical topic embeddings -> the broker matches
    # them -> avoided-cost events are booked -> /api/savings and /api/report surface a k-gated saving.
    db, _, _ = _wire(monkeypatch, tmp_path)
    collector = TestClient(server.app)
    emb = [1.0, 0.0, 0.0]
    batch = [_rec(f"e{i}", f"px_{i}", tenant="acme-eu", cost=2.0, embedding=emb).to_dict()
             for i in range(5)]
    r = collector.post("/v1/derived", json=batch, headers={"Authorization": "Bearer dev-ingest-token"})
    assert r.status_code == 200 and r.json()["ingested"] == 5

    sav = collector.get("/api/savings", headers={"Authorization": "Bearer acme-mgr"}).json()
    assert sav["tenant"] == "acme-eu"
    assert sav["reuse_avoided_usd"] > 0           # at least one reuse booked and credited (5 >= k)
    assert sav["events_credited"] >= 1

    rep = collector.get("/api/report", headers={"Authorization": "Bearer acme-mgr"}).json()
    assert rep["reuse_yield"]["reuse_avoided_usd"] == sav["reuse_avoided_usd"]


def test_api_cross_org_tenant_hijack_is_refused(tmp_path, monkeypatch):
    # the critical finding: org B must NOT be able to re-create org A's tenant_id and flip its org
    # (which would pass the org gate and leak org A's reports). The registry refuses the reassignment.
    _, _, tenant_db = _wire(monkeypatch, tmp_path)
    ts = TenantStore(tenant_db)
    ts.upsert(Tenant("shared-eu", "acme", "ACME EU", "eu", 1.0))   # acme owns it first
    ts.close()
    client = TestClient(server.app)
    # a globex admin (different org) tries to mint the same tenant_id
    monkeypatch.setattr(server, "_principals", _principals_with_globex_admin())
    r = client.post("/api/tenants", json={"tenant_id": "shared-eu"},
                    headers={"Authorization": "Bearer globex-admin"})
    assert r.status_code == 409                                    # refused, not silently reassigned
    ts = TenantStore(tenant_db)
    assert ts.get("shared-eu").org == "acme"                       # ownership unchanged
    ts.close()


def _principals_with_globex_admin():
    k = b"test-key"
    from abenlux.privacy.pseudonymize import pseudonymize
    base = _principals()._by_token
    base["globex-admin"] = Principal("ad@globex", "Globex Admin", Role.ADMIN,
                                     pseudonymize("ad@globex", k), tenant_id="globex-eu", org="globex")
    return PrincipalStore(base)


def test_api_drift_is_tenant_scoped(tmp_path, monkeypatch):
    # /api/drift must not leak the org-wide cross-tenant spend trend into a tenant-scoped manager's view
    db, _, _ = _wire(monkeypatch, tmp_path)
    st = DerivedStore(db)
    # acme-eu: two windows of modest spend; acme-us: huge spend that must NOT bleed into eu's trend
    for i in range(6):
        st.insert(_rec(f"eu{i}", f"a{i}", tenant="acme-eu", cost=1.0))
    st.conn.execute("UPDATE derived SET ts=100.0 WHERE event_id IN ('eu0','eu1','eu2')")
    st.conn.execute("UPDATE derived SET ts=200.0 WHERE event_id IN ('eu3','eu4','eu5')")
    for i in range(6):
        st.insert(_rec(f"us{i}", f"b{i}", tenant="acme-us", cost=1000.0))
    st.conn.commit()
    st.close()
    client = TestClient(server.app)
    out = client.get("/api/drift", headers={"Authorization": "Bearer acme-mgr"}).json()
    if out["trend"]:
        # the recent-window cost is acme-eu only (a few dollars), never the acme-us thousands
        assert out["trend"]["recent_window"]["cost"] < 100.0


def test_broker_enforces_org_wall():
    from abenlux.collaborate.broker import CollaborationBroker, TopicSignal
    b = CollaborationBroker()
    emb = [1.0, 0.0, 0.0]
    # two developers in DIFFERENT orgs, same residency, same topic - must never match
    b.submit(TopicSignal("acme-dev", emb, "Saga", residency="eu", org="acme"))
    cross = b.submit(TopicSignal("globex-dev", emb, "Saga", residency="eu", org="globex"))
    assert cross == []
    # same org, same topic -> matches
    same = b.submit(TopicSignal("acme-dev2", emb, "Saga", residency="eu", org="acme"))
    assert any(m.b == "acme-dev" for m in same)


def test_api_benchmark_endpoint(tmp_path, monkeypatch):
    db, _, tenant_db = _wire(monkeypatch, tmp_path)
    ts = TenantStore(tenant_db)
    for t in ("acme-eu", "acme-us", "acme-apac"):
        ts.upsert(Tenant(t, "acme", t, "eu", 1.0))
    ts.close()
    st = DerivedStore(db)
    for t, cost in [("acme-eu", 1.0), ("acme-us", 4.0), ("acme-apac", 2.0)]:
        for i in range(5):
            st.insert(_rec(f"{t}{i}", f"{t}-a{i}", tenant=t, cost=cost))
    st.close()
    client = TestClient(server.app)
    # a developer lacks VIEW_BENCHMARK
    assert client.get("/api/benchmark", headers={"Authorization": "Bearer dev-token"}).status_code in (401, 403)
    out = client.get("/api/benchmark", headers={"Authorization": "Bearer acme-mgr"}).json()
    assert out["org"] == "acme"
    assert out["readiness"]["ready"] is True
    assert len(out["comparison"]) == len(out["your_ratios"])
