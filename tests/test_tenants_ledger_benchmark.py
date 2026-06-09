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
from abenlux.ledger import AvoidedCostEvent, LedgerStore, estimate_avoided, median
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
    # solved reuse credits the full median, live duplication a conservative half
    assert estimate_avoided([2.0, 4.0], "solved_reuse") == 3.0
    assert estimate_avoided([2.0, 4.0], "live_duplication") == 1.5
    assert estimate_avoided([], "solved_reuse") == 0.0


def test_ledger_books_dedups_and_upgrades(tmp_path):
    led = LedgerStore(tmp_path / "l.db")
    ev = AvoidedCostEvent("acme-eu", "ObjA", "feature", "topic", 3.0, "live_duplication", 5, 1.0)
    assert led.book(ev, pair=("a", "b")) is True
    # same pair x objective x work_type x cluster, even reversed, is not booked twice
    assert led.book(ev, pair=("b", "a")) is False
    # but a confirmed solved-reuse upgrades the prior live-duplication for the same opportunity
    up = AvoidedCostEvent("acme-eu", "ObjA", "feature", "topic", 6.0, "solved_reuse", 5, 1.0)
    assert led.book(up, pair=("a", "b")) is True
    summ = led.summary("acme-eu", k=5)
    assert summ["events_credited"] == 1 and summ["reuse_avoided_usd"] == 6.0   # upgraded, not doubled
    led.close()


def test_ledger_k_gates_savings(tmp_path):
    led = LedgerStore(tmp_path / "l.db")
    # backed by only 2 developers -> below k=5 -> suppressed, never credited
    led.book(AvoidedCostEvent("t", "Obj", "fix", "c1", 10.0, "solved_reuse", 2, 1.0), pair=("a", "b"))
    # backed by 5 -> credited
    led.book(AvoidedCostEvent("t", "Obj", "fix", "c2", 4.0, "solved_reuse", 5, 1.0), pair=("c", "d"))
    summ = led.summary("t", k=5)
    assert summ["reuse_avoided_usd"] == 4.0
    assert summ["events_credited"] == 1 and summ["events_suppressed"] == 1
    led.close()


def test_ledger_summary_scopes_by_tenant(tmp_path):
    led = LedgerStore(tmp_path / "l.db")
    led.book(AvoidedCostEvent("acme-eu", "O", "feature", "c", 5.0, "solved_reuse", 5, 1.0), pair=("a", "b"))
    led.book(AvoidedCostEvent("acme-us", "O", "feature", "c", 9.0, "solved_reuse", 5, 1.0), pair=("a", "b"))
    assert led.summary("acme-eu", k=5)["reuse_avoided_usd"] == 5.0
    assert led.summary("acme-us", k=5)["reuse_avoided_usd"] == 9.0
    led.close()


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
        assert 0.0 <= c["your_percentile"] <= 1.0
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
