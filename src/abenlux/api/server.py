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


@app.post("/v1/derived")
async def ingest_derived(request: Request, authorization: str | None = Header(default=None)):
    token = authorization[7:].strip() if (authorization or "").lower().startswith("bearer ") else None
    if token not in SETTINGS.ingest_tokens:
        raise HTTPException(status_code=401, detail="invalid ingest token")
    payload = await request.json()
    items = payload if isinstance(payload, list) else [payload]
    store = _store()
    mstore = _matches()
    n = 0
    for d in items:
        # accept only known fields, a forwarded record that smuggled a content key is rejected
        clean = {k: v for k, v in d.items() if k in _DERIVED_FIELDS}
        rec = DerivedRecord(**clean)
        store.insert(rec)
        _match_centrally(rec, mstore)
        n += 1
    mstore.close()
    store.close()
    return {"ingested": n}


def _match_centrally(rec: DerivedRecord, mstore: MatchStore) -> None:
    # double-blind matching at the collector over content-free signals. writes one row per side,
    # each owner sees only their own. management never sees this - it is not a report.
    if not rec.embedding or not rec.objective_id or not rec.actor_pseudonym:
        return
    obj = _kg.objectives.get(rec.objective_id)
    sig = TopicSignal(
        actor_pseudonym=rec.actor_pseudonym, topic_embedding=rec.embedding,
        topic_label=rec.objective_label or "general", client=getattr(obj, "client", None),
    )
    for m in _broker.submit(sig):
        mstore.record(m.a, m.b, m.topic, m.similarity, m.mode)
        mstore.record(m.b, m.a, m.topic, m.similarity, m.mode)


@app.get("/api/whoami")
async def whoami(principal: Principal = Depends(current_principal)):
    return {
        "name": principal.display_name,
        "role": principal.role.value,
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


@app.get("/api/report")
async def report(principal: Principal = Depends(current_principal)):
    _need(principal, Permission.VIEW_AGGREGATES)
    store = _store()
    rep = management_report(store, k=SETTINGS.k_anon, dp_epsilon=SETTINGS.dp_epsilon, kg=_kg)
    store.close()
    return rep


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
async def collab_status_for_edge(
    authorization: str | None = Header(default=None),
    x_aben_pseudonym: str | None = Header(default=None),
):
    # content-free collaboration matches for ONE pseudonym, so the edge agent can live-push a toast
    # when a colleague starts on the same problem (matching itself happens centrally - the edge can't,
    # it only sees its own signals). device ingest token + the caller's own pseudonym; no peer identity
    # is returned (that needs a mutual double-blind consent via /api/collab), only topic + mode + state.
    token = authorization[7:].strip() if (authorization or "").lower().startswith("bearer ") else None
    if token not in SETTINGS.ingest_tokens:
        raise HTTPException(status_code=401, detail="invalid ingest token")
    if not x_aben_pseudonym:
        return {"matches": []}
    mstore = _matches()
    out = [{"id": m["id"], "topic": m["topic"], "similarity": m["similarity"], "mode": m["mode"],
            "mutual": mstore.mutually_consented(x_aben_pseudonym, m["peer"])}
           for m in mstore.for_owner(x_aben_pseudonym)]
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
