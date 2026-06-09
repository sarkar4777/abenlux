"""
The management + developer API. This is the read plane: the capture gateway writes derived
records, this server serves them back - but only through RBAC, so the README's trust
architecture is enforced by the authorization layer, not by UI discipline.

Endpoint access mirrors the governance model exactly:
  /api/me            VIEW_OWN        your own spend + waste + collaboration matches (your pseudonym only)
  /api/report        VIEW_AGGREGATES k-anonymized org rollups (NO individual rows, any role)
  /api/rollup/{dim}  VIEW_AGGREGATES one content-free dimension, k-anon gated
  /api/objectives    MANAGE          knowledge-graph objectives (admin)
  /api/collab/...    VIEW_OWN        double-blind consent to a collaboration match

There is no endpoint, at any permission level, that returns another named individual's detail -
because no such permission exists (see auth/rbac.py). The dashboard at / is a static page that
authenticates with a bearer token and renders the role-appropriate view.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from abenlux.analytics.reports import developer_report, management_report
from abenlux.attribution.attributor import KnowledgeGraph
from abenlux.auth.principals import load_principals
from abenlux.auth.rbac import AuthorizationError, Permission, Principal, require
from abenlux.collaborate.broker import CollaborationBroker, TopicSignal
from abenlux.developer.matches import MatchStore
from abenlux.schema import DerivedRecord
from abenlux.settings import SETTINGS
from abenlux.store import open_store

app = FastAPI(title="Abenlux API", version="0.2.0")
_STATIC = Path(__file__).parent / "static"

_principals = load_principals()
_kg = KnowledgeGraph.from_yaml(SETTINGS.kg_path) if SETTINGS.kg_path else KnowledgeGraph()
# central, double-blind collaboration broker. runs over the content-free forwarded records (the
# embedding + objective, never prompt text), so two developers on two machines actually match.
_broker = CollaborationBroker()


def _store():
    return open_store(SETTINGS.db_path)


def _matches() -> MatchStore:
    import os
    return MatchStore(os.getenv("ABEN_MATCH_DB", "abenlux-matches.db"))


def _contacts():
    import os

    from abenlux.developer.contacts import ContactStore
    return ContactStore(os.getenv("ABEN_CONTACT_DB", "abenlux-contacts.db"))


def _ledger():
    import os

    from abenlux.ledger import open_ledger
    return open_ledger(os.getenv("ABEN_LEDGER_DB", "abenlux-ledger.db"))


def _tenants():
    import os

    from abenlux.tenants import open_tenant_store
    return open_tenant_store(os.getenv("ABEN_TENANT_DB", "abenlux-tenants.db"))


def _peer_card(peer_pseudonym: str, contacts) -> dict:
    # the peer's shareable card: their self-set handles override the static principal fallback.
    # only ever called for a mutually-consented match.
    card = _principals.pseudonym_to_contact(peer_pseudonym) or {"name": "a colleague"}
    overlay = contacts.get(peer_pseudonym)
    if overlay:
        card = {**card, **overlay}
    return card


def current_principal(
    authorization: str | None = Header(default=None),
    x_abenlux_token: str | None = Header(default=None),
) -> Principal:
    token = x_abenlux_token
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    principal = _principals.resolve(token)
    if principal is None:
        raise HTTPException(status_code=401, detail="invalid or missing token")
    return principal


def _need(principal: Principal, perm: Permission) -> None:
    try:
        require(principal, perm)
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=str(e))


@app.exception_handler(AuthorizationError)
async def _authz_handler(_request, exc: AuthorizationError):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.get("/health")
async def health():
    s = _store()
    out = {"status": "ok", "events": s.totals()["n"]}
    s.close()
    return out


# central collector ingest: edge agents forward content-free DerivedRecords here with a device
# ingest token. this is the ONLY write path into the central plane, and it accepts derived data
# only - there is no endpoint that accepts raw prompts, because raw prompts are destroyed on-device.
_DERIVED_FIELDS = set(DerivedRecord.__dataclass_fields__.keys())


def _harden_inbound(rec: DerivedRecord) -> None:
    """the edge is on the developer's machine, so a buggy or hostile one could forge a record. the
    collector is the authoritative source of spend: re-derive cost from the (content-free) token facts
    rather than trusting a caller-supplied cost_usd (which could under-report to $0 or inflate the org
    total). also re-redact the free-text metadata fields as defense in depth - they should be slugs."""
    from abenlux.pricing import cost_usd
    from abenlux.processing.redact import redact
    cb = cost_usd(rec.request_model, rec.input_tokens, rec.output_tokens,
                  cache_read_tokens=rec.cache_read_tokens, cache_creation_tokens=rec.cache_creation_tokens)
    rec.cost_usd, rec.cost_priced = cb.total, cb.priced
    for f in ("repo", "objective_label", "ticket_id", "work_type", "tool"):
        v = getattr(rec, f, None)
        if isinstance(v, str) and v:
            setattr(rec, f, redact(v).text)


@app.post("/v1/derived")
async def ingest_derived(request: Request, authorization: str | None = Header(default=None)):
    token = authorization[7:].strip() if (authorization or "").lower().startswith("bearer ") else None
    if token not in SETTINGS.ingest_tokens:
        raise HTTPException(status_code=401, detail="invalid ingest token")
    payload = await request.json()
    items = payload if isinstance(payload, list) else [payload]
    store = _store()
    mstore = _matches()
    ledger = _ledger()
    n, rejected = 0, 0
    for d in items:
        if not isinstance(d, dict):
            rejected += 1
            continue
        # accept only known fields, a forwarded record that smuggled a content key is rejected
        clean = {k: v for k, v in d.items() if k in _DERIVED_FIELDS}
        try:
            rec = DerivedRecord(**clean)        # a malformed/mistyped item must not 500 the whole batch
            _harden_inbound(rec)                # re-price authoritatively + re-redact free-text fields
            store.insert(rec)
            _match_centrally(rec, mstore, store, ledger)
        except Exception:
            rejected += 1
            continue
        n += 1
    ledger.close()
    mstore.close()
    store.close()
    return {"ingested": n, "rejected": rejected}


def _match_centrally(rec: DerivedRecord, mstore: MatchStore, store=None, ledger=None) -> None:
    # double-blind matching at the collector over content-free signals. writes one row per side,
    # each owner sees only their own. management never sees this - it is not a report.
    if not rec.embedding or not rec.objective_id or not rec.actor_pseudonym:
        return
    obj = _kg.objectives.get(rec.objective_id)
    tenant = getattr(rec, "tenant_id", None) or "default"
    sig = TopicSignal(
        actor_pseudonym=rec.actor_pseudonym, topic_embedding=rec.embedding,
        topic_label=rec.objective_label or "general", client=getattr(obj, "client", None),
        residency=getattr(rec, "residency", None) or "eu",  # enforce the residency wall centrally
    )
    for m in _broker.submit(sig):
        mstore.record(m.a, m.b, m.topic, m.similarity, m.mode)
        mstore.record(m.b, m.a, m.topic, m.similarity, m.mode)
        # book the avoided re-solve: the broker just surfaced a reusable/duplicate effort, so part (or
        # all) of a second solve is avoided. valued at the tenant's k-gated median cost-to-solve.
        if store is not None and ledger is not None:
            _book_avoided(rec, m, tenant, store, ledger)


def _book_avoided(rec: DerivedRecord, m, tenant: str, store, ledger) -> None:
    from abenlux.ledger import AvoidedCostEvent, estimate_avoided
    costs = store.actor_costs_for(rec.objective_id, rec.work_type, tenant=tenant)
    if not costs:
        return
    ev = AvoidedCostEvent(
        tenant_id=tenant, objective_id=rec.objective_id, work_type=rec.work_type or "unknown",
        cluster_id=m.topic, estimated_avoided_usd=estimate_avoided(costs, m.mode),
        mode=m.mode, actors=len(costs), ts=rec.ts,
    )
    ledger.book(ev, pair=(m.a, m.b))


@app.get("/api/whoami")
async def whoami(principal: Principal = Depends(current_principal)):
    return {
        "name": principal.display_name,
        "role": principal.role.value,
        "tenant_id": principal.tenant_id,
        "org": principal.org,
        "permissions": sorted(p.value for p in principal.permissions),
    }


@app.get("/api/me")
async def me(principal: Principal = Depends(current_principal)):
    _need(principal, Permission.VIEW_OWN)
    store = _store()
    rep = developer_report(store, principal.pseudonym)  # scoped to caller's pseudonym only
    store.close()
    mstore = _matches()
    contacts = _contacts()
    raw_matches = mstore.for_owner(principal.pseudonym)
    matches = []
    for m in raw_matches:
        revealed, card = None, None
        if mstore.mutually_consented(principal.pseudonym, m["peer"]):
            # identity AND contact handles revealed only after BOTH opted in
            card = _peer_card(m["peer"], contacts)
            revealed = card.get("name")
        matches.append({
            "id": m["id"], "topic": m["topic"], "similarity": m["similarity"],
            "mode": m["mode"], "peer_revealed": revealed, "peer_contact": card,
            "you_requested": mstore.has_consented(principal.pseudonym, m["peer"]),
        })
    mstore.close()
    contacts.close()
    rep["collaboration_matches"] = matches
    return rep


@app.post("/api/collab/{match_id}/consent")
async def collab_consent(match_id: int, principal: Principal = Depends(current_principal)):
    _need(principal, Permission.VIEW_OWN)
    mstore = _matches()
    owned = {m["id"]: m for m in mstore.for_owner(principal.pseudonym)}
    if match_id not in owned:
        mstore.close()
        raise HTTPException(status_code=404, detail="match not found for this principal")
    peer = owned[match_id]["peer"]
    mstore.record_consent(principal.pseudonym, peer)
    mutual = mstore.mutually_consented(principal.pseudonym, peer)
    card = None
    if mutual:
        contacts = _contacts()
        card = _peer_card(peer, contacts)
        contacts.close()
    mstore.close()
    return {"consented": True, "mutual": mutual,
            "peer_revealed": card.get("name") if card else None, "peer_contact": card}


@app.get("/api/contact")
async def get_contact(principal: Principal = Depends(current_principal)):
    _need(principal, Permission.VIEW_OWN)
    contacts = _contacts()
    card = contacts.get(principal.pseudonym) or (principal.contact or {})
    contacts.close()
    return {"contact": card}


@app.post("/api/contact")
async def set_contact(request: Request, principal: Principal = Depends(current_principal)):
    _need(principal, Permission.VIEW_OWN)
    from abenlux.developer.contacts import clean_card
    body = await request.json()
    contacts = _contacts()
    saved = contacts.set(principal.pseudonym, clean_card(body))
    contacts.close()
    return {"contact": saved}


def _resolve_report_tenant(principal: Principal, requested: str | None) -> str:
    """a principal reports their OWN tenant by default. they may request another tenant only if it is
    in their own org (admins can manage any tenant in their org) - never another org's. cross-tenant
    DETAIL stays inside the org wall; cross-tenant COMPARISON is the k-anon benchmark, not this report."""
    if not requested or requested == principal.tenant_id:
        return principal.tenant_id
    tenants = _tenants()
    try:
        org = tenants.org_of(requested)
    finally:
        tenants.close()
    if org is not None and org == principal.org:
        return requested
    raise HTTPException(status_code=403, detail="tenant is outside your org")


@app.get("/api/report")
async def report(tenant: str | None = None, principal: Principal = Depends(current_principal)):
    _need(principal, Permission.VIEW_AGGREGATES)
    scope = _resolve_report_tenant(principal, tenant)
    store = _store()
    rep = management_report(store, k=SETTINGS.k_anon, dp_epsilon=SETTINGS.dp_epsilon, kg=_kg, tenant=scope)
    store.close()
    ledger = _ledger()
    rep["reuse_yield"] = ledger.summary(scope, k=SETTINGS.k_anon)  # avoided re-solves, beside spend
    ledger.close()
    return rep


@app.get("/api/savings")
async def savings(tenant: str | None = None, principal: Principal = Depends(current_principal)):
    # the reuse-yield ledger: estimated cost of re-solves avoided, k-anonymity gated, scoped to the
    # caller's tenant (or another tenant in their org). a savings figure, shown beside spend not inside.
    _need(principal, Permission.VIEW_AGGREGATES)
    scope = _resolve_report_tenant(principal, tenant)
    ledger = _ledger()
    out = ledger.summary(scope, k=SETTINGS.k_anon)
    ledger.close()
    return out


@app.get("/api/budgets")
async def budgets(principal: Principal = Depends(current_principal)):
    _need(principal, Permission.VIEW_AGGREGATES)
    from abenlux.analytics.budget import budget_status, current_month_bounds
    store = _store()
    ps, pe, now = current_month_bounds()
    rows = [b.to_dict() for b in budget_status(store, _kg, period_start=ps, period_end=pe, now=now)]
    store.close()
    return {"budgets": rows}


@app.get("/v1/budget-status")
async def budget_status_for_edge(authorization: str | None = Header(default=None)):
    # content-free objective->status map the on-device edge agent polls to drive PRIVATE developer
    # nudges. device ingest token (not a principal) - no spend figures, no identities leave here.
    token = authorization[7:].strip() if (authorization or "").lower().startswith("bearer ") else None
    if token not in SETTINGS.ingest_tokens:
        raise HTTPException(status_code=401, detail="invalid ingest token")
    from abenlux.analytics.budget import budget_status, current_month_bounds, status_snapshot
    store = _store()
    ps, pe, now = current_month_bounds()
    snap = status_snapshot(budget_status(store, _kg, period_start=ps, period_end=pe, now=now))
    store.close()
    return snap


@app.get("/v1/collab-status")
async def collab_status_for_edge(principal: Principal = Depends(current_principal)):
    # content-free collaboration matches for the AUTHENTICATED developer, so the edge agent can
    # live-push a toast when a colleague starts on the same problem. the pseudonym is taken from the
    # authenticated principal, never a request header - the shared device token authenticates the
    # device class, not a developer, so it must not be sufficient to choose whose feed is returned
    # (that was an IDOR: device token + a forged pseudonym enumerated anyone's matches). no peer
    # identity is returned, only topic + mode + state (peers reveal on a mutual double-blind consent).
    _need(principal, Permission.VIEW_OWN)
    mstore = _matches()
    out = [{"id": m["id"], "topic": m["topic"], "similarity": m["similarity"], "mode": m["mode"],
            "mutual": mstore.mutually_consented(principal.pseudonym, m["peer"])}
           for m in mstore.for_owner(principal.pseudonym)]
    mstore.close()
    return {"matches": out}


@app.get("/api/drift")
async def drift(principal: Principal = Depends(current_principal)):
    _need(principal, Permission.VIEW_AGGREGATES)
    from dataclasses import asdict

    from abenlux.analytics.drift import spend_trend
    store = _store()
    rep = spend_trend(store)
    store.close()
    return {"trend": asdict(rep) if rep else None}


@app.get("/api/rollup/{dimension}")
async def rollup(dimension: str, principal: Principal = Depends(current_principal)):
    _need(principal, Permission.VIEW_AGGREGATES)
    store = _store()
    try:
        rows = store.rollup(dimension)
    except ValueError as e:
        store.close()
        raise HTTPException(status_code=400, detail=str(e))
    # apply k-anon suppression here too so a direct rollup call can't leak a sub-k group
    from abenlux.privacy.pseudonymize import KAnonymityGate
    gate = KAnonymityGate(k=SETTINGS.k_anon)
    out = []
    for r in rows:
        if gate.allows(r["actors"]):
            out.append(r)
        else:
            out.append({"label": r["label"], "calls": 0, "tokens": 0, "cost": 0.0,
                        "actors": r["actors"], "suppressed": True})
    store.close()
    return {"dimension": dimension, "rows": out}


@app.get("/api/benchmark")
async def benchmark_view(tenant: str | None = None, principal: Principal = Depends(current_principal)):
    # cross-tenant comparison within the caller's OWN org. k-anon per tenant, DP-noised, gated on a
    # minimum cohort size. the focus tenant defaults to the caller's own, overridable to any tenant in
    # their org. no other org's tenants are ever in the cohort.
    _need(principal, Permission.VIEW_BENCHMARK)
    focus = _resolve_report_tenant(principal, tenant)
    from abenlux.analytics.benchmark import benchmark as build_benchmark
    tenants = _tenants()
    cohort = [t.tenant_id for t in tenants.list(org=principal.org)]
    tenants.close()
    # a single-org demo deployment (the unconfigured "default" org) has not registered tenants yet, so
    # it benchmarks over whatever tenant_ids appear in the data - useful on day one. a NAMED org never
    # does this: pulling raw tenant_ids would risk mixing another org's tenants into the cohort, so a
    # named org with no registered tenants compares only itself (honestly "cohort not ready").
    if not cohort and principal.org == "default":
        store0 = _store()
        cohort = store0.distinct_tenants()
        store0.close()
    if focus not in cohort:
        cohort = cohort + [focus]
    ledger = _ledger()
    reuse_by_tenant = {t: ledger.summary(t, k=SETTINGS.k_anon)["reuse_avoided_usd"] for t in cohort}
    ledger.close()
    store = _store()
    out = build_benchmark(
        store, tenants=cohort, focus_tenant=focus, k=SETTINGS.k_anon,
        dp_epsilon=SETTINGS.dp_epsilon, reuse_by_tenant=reuse_by_tenant,
    )
    store.close()
    out["org"] = principal.org
    return out


@app.get("/api/tenants")
async def list_tenants(principal: Principal = Depends(current_principal)):
    # a principal sees the tenants of their OWN org only - the set that forms their benchmark cohort.
    _need(principal, Permission.VIEW_AGGREGATES)
    tenants = _tenants()
    rows = [t.to_dict() for t in tenants.list(org=principal.org)]
    tenants.close()
    return {"org": principal.org, "tenants": rows}


@app.post("/api/tenants")
async def create_tenant(request: Request, principal: Principal = Depends(current_principal)):
    # creating a tenant (org unit / geography) is an admin action, and the new tenant is always bound
    # to the admin's OWN org - you cannot mint a tenant into someone else's org.
    _need(principal, Permission.MANAGE)
    body = await request.json()
    tenant_id = (body.get("tenant_id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id is required")
    from abenlux.processing.redact import redact
    from abenlux.tenants import Tenant
    tenant_id = redact(tenant_id).text
    display = redact(str(body.get("display_name") or tenant_id)).text
    residency = redact(str(body.get("residency") or SETTINGS.residency)).text
    import time as _time
    tenants = _tenants()
    saved = tenants.upsert(Tenant(
        tenant_id=tenant_id, org=principal.org, display_name=display,
        residency=residency, created_ts=_time.time(),
    ))
    tenants.close()
    return {"tenant": saved.to_dict()}


@app.get("/api/objectives")
async def objectives(principal: Principal = Depends(current_principal)):
    _need(principal, Permission.MANAGE)
    return {"objectives": [
        {"id": o.id, "label": o.label, "kind": o.kind, "client": o.client}
        for o in _kg.objectives.values()
    ]}


@app.get("/")
async def dashboard():
    return FileResponse(_STATIC / "dashboard.html")
