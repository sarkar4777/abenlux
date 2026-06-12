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

import json
import os
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


def _capsules():
    import os

    from abenlux.developer.capsules import CapsuleStore
    return CapsuleStore(os.getenv("ABEN_CAPSULE_DB", "abenlux-capsules.db"))


def _outcomes():
    from abenlux.analytics.outcomes import OutcomeStore
    return OutcomeStore(os.getenv("ABEN_OUTCOME_DB", "abenlux-outcomes.db"))


def _relay():
    from abenlux.developer.relay import RelayStore
    return RelayStore(os.getenv("ABEN_RELAY_DB", "abenlux-relay.db"))


def _exchange():
    from abenlux.analytics.exchange import ExchangeStore
    return ExchangeStore(os.getenv("ABEN_EXCHANGE_DB", "abenlux-exchange.db"))


def _ledger():
    import os

    from abenlux.ledger import open_ledger
    return open_ledger(os.getenv("ABEN_LEDGER_DB", "abenlux-ledger.db"))


def _tenants():
    import os

    from abenlux.tenants import open_tenant_store
    return open_tenant_store(os.getenv("ABEN_TENANT_DB", "abenlux-tenants.db"))


# a surfaced collaboration match is only "live" for this long. a months-old live-duplication pairing
# must not be presented as someone working on it right now (the consent-ownership lookups still see all
# rows - this only bounds what is shown as a current match).
_MATCH_TTL_S = float(os.getenv("ABEN_MATCH_TTL_DAYS", "14")) * 86400


def _peer_card(peer_pseudonym: str, contacts) -> dict:
    # the peer's shareable card: their self-set handles override the static principal fallback.
    # only ever called for a mutually-consented match.
    verified = _principals.pseudonym_to_contact(peer_pseudonym) or {"name": "a colleague"}
    overlay = contacts.get(peer_pseudonym) or {}
    card = {**verified, **overlay}
    # the verified IdP display name is authoritative - a self-set card can never override the name shown
    # in the trusted "identity revealed" surface (it carries only handles, but re-assert as defense).
    card["name"] = verified.get("name", "a colleague")
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
async def health(authorization: str | None = Header(default=None)):
    # liveness is public, but the org-wide event COUNT is only returned to a holder of a valid ingest
    # token (an operational signal, not for anonymous callers). edges/ops poll with their token.
    token = authorization[7:].strip() if (authorization or "").lower().startswith("bearer ") else None
    if token not in SETTINGS.ingest_tokens:
        return {"status": "ok"}
    s = _store()
    out = {"status": "ok", "events": s.totals()["n"]}
    s.close()
    return out


# central collector ingest: edge agents forward content-free DerivedRecords here with a device
# ingest token. this is the ONLY write path into the central plane, and it accepts derived data
# only - there is no endpoint that accepts raw prompts, because raw prompts are destroyed on-device.
_DERIVED_FIELDS = set(DerivedRecord.__dataclass_fields__.keys())


_TOKEN_CAP = int(os.getenv("ABEN_TOKEN_CAP", "100000000"))   # clamp forged/absurd token counts on ingest


def _harden_inbound(rec: DerivedRecord) -> None:
    """the edge is on the developer's machine, so a buggy or hostile one could forge a record. the
    collector is the authoritative source of spend: re-derive cost from the (content-free) token facts
    rather than trusting a caller-supplied cost_usd (which could under-report to $0 or inflate the org
    total). also re-redact the free-text metadata fields as defense in depth - they should be slugs."""
    from abenlux.pricing import cost_usd
    from abenlux.processing.redact import redact
    # clamp every token fact to a sane non-negative bound first, so a forged record cannot drive pricing
    # with negative or absurd values, nor corrupt org-wide token/savings totals.
    for f in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
              "duplicate_history_tokens", "saved_input_tokens"):
        try:
            v = int(getattr(rec, f, 0) or 0)
        except (TypeError, ValueError):
            v = 0
        setattr(rec, f, max(0, min(v, _TOKEN_CAP)))
    # ALWAYS re-derive cost from the clamped tokens. a genuine local-cache hit carries zero billable
    # tokens (the edge moved the avoided input into saved_input_tokens), so it prices to $0 with no
    # trusted flag - which is why served_from_cache can no longer be abused to zero a real call's cost.
    cb = cost_usd(rec.request_model, rec.input_tokens, rec.output_tokens,
                  cache_read_tokens=rec.cache_read_tokens, cache_creation_tokens=rec.cache_creation_tokens)
    rec.cost_usd, rec.cost_priced = cb.total, cb.priced
    for f in ("repo", "objective_label", "ticket_id", "work_type", "tool"):
        v = getattr(rec, f, None)
        if isinstance(v, str) and v:
            setattr(rec, f, redact(v).text)


# a forwarded batch is bounded: one POST cannot monopolize the broker's process-global lock or churn
# its signal buffer (a buggy/hostile edge could otherwise evict the real cohort with one huge request).
_MAX_INGEST_BATCH = int(os.getenv("ABEN_MAX_INGEST_BATCH", "1000"))
_MAX_INGEST_BYTES = int(float(os.getenv("ABEN_MAX_INGEST_MB", "32")) * 1024 * 1024)  # bound body before parse


@app.post("/v1/derived")
async def ingest_derived(request: Request, authorization: str | None = Header(default=None)):
    token = authorization[7:].strip() if (authorization or "").lower().startswith("bearer ") else None
    if token not in SETTINGS.ingest_tokens:
        raise HTTPException(status_code=401, detail="invalid ingest token")
    # the built-in default ingest token is a dev convenience; refuse it in a configured (production)
    # posture so a deploy that forgot to set ABEN_INGEST_TOKEN cannot accept records from anyone.
    if token == "dev-ingest-token" and os.getenv("ABEN_PRINCIPALS") and not os.getenv("ABEN_ALLOW_DEFAULT_INGEST"):
        raise HTTPException(status_code=401, detail="default ingest token refused in this posture")
    # bound the body BEFORE parsing: request.json() would otherwise buffer an arbitrarily large payload
    # into memory (a cheap DoS) before the batch-size check below ever runs.
    raw = await request.body()
    if len(raw) > _MAX_INGEST_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        payload = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid JSON") from None
    items = payload if isinstance(payload, list) else [payload]
    if len(items) > _MAX_INGEST_BATCH:
        raise HTTPException(status_code=413, detail=f"batch too large (max {_MAX_INGEST_BATCH})")
    store = _store()
    mstore = _matches()
    ledger = _ledger()
    tenants = _tenants()
    capsules = _capsules()
    org_cache: dict[str, str] = {}     # tenant_id -> org/residency, resolved once per batch (broker walls)
    identity = _ingest_identity_index()  # {pseudonym: Principal} or None (no per-actor binding configured)
    n, rejected = 0, 0
    for d in items:
        if not isinstance(d, dict):
            rejected += 1
            continue
        # accept only known fields, a forwarded record that smuggled a content key is rejected
        clean = {k: v for k, v in d.items() if k in _DERIVED_FIELDS}
        try:
            rec = DerivedRecord(**clean)        # a malformed/mistyped item must not 500 the whole batch
            if identity is not None:
                principal = identity.get(rec.actor_pseudonym)
                if principal is None:
                    rejected += 1               # names an unknown actor: a forged/fabricated pseudonym
                    continue
                rec.tenant_id = principal.tenant_id   # bind tenant to the authenticated developer
            _harden_inbound(rec)                # re-price authoritatively + re-redact free-text fields
            store.insert(rec)
            _match_centrally(rec, mstore, ledger, _org_for(rec, tenants, org_cache),
                             _residency_for(rec, tenants, org_cache), capsules=capsules)
        except Exception:
            rejected += 1
            continue
        n += 1
    capsules.close()
    tenants.close()
    ledger.close()
    mstore.close()
    store.close()
    return {"ingested": n, "rejected": rejected}


def _objective_for_ticket(ticket_id: str | None) -> str | None:
    # resolve a ticket like ACME-123 to its objective through the same prefix map attribution uses.
    if not ticket_id or "-" not in ticket_id:
        return None
    return _kg.ticket_prefix_to_objective.get(ticket_id.split("-", 1)[0].upper())


@app.post("/v1/outcomes")
async def ingest_outcomes(request: Request, authorization: str | None = Header(default=None)):
    # the value feed. a git or CI integration posts content-free facts about each change (did it merge,
    # was it reverted, lines added and removed) keyed by the ticket, so the report can join spend to
    # shipped work. gated by the same ingest token as the spend feed, bounded the same way.
    token = authorization[7:].strip() if (authorization or "").lower().startswith("bearer ") else None
    if token not in SETTINGS.ingest_tokens:
        raise HTTPException(status_code=401, detail="invalid ingest token")
    raw = await request.body()
    if len(raw) > _MAX_INGEST_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        items = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid JSON") from None
    items = items if isinstance(items, list) else [items]
    if len(items) > _MAX_INGEST_BATCH:
        raise HTTPException(status_code=413, detail=f"batch too large (max {_MAX_INGEST_BATCH})")
    outcomes = _outcomes()
    n = 0
    for d in items:
        if not isinstance(d, dict):
            continue
        rec = dict(d)
        if not rec.get("objective_id"):
            rec["objective_id"] = _objective_for_ticket(rec.get("ticket_id"))
        if _kg.objectives and rec.get("objective_id") not in _kg.objectives:
            continue                       # only a known objective, so a forged id cannot seed value
        if outcomes.record(rec):
            n += 1
    outcomes.close()
    return {"recorded": n}


def _ingest_identity_index():
    """{pseudonym: Principal} for binding an ingested record to a known developer. ONLY enforced when a
    real principals registry is configured (production). without it (offline/solo) the device is the
    trust boundary and no per-actor binding is possible. the index lets the collector reject records
    that name an unknown actor (fabricated pseudonyms that would dilute k-anonymity or poison a feed)
    and stamp the record's tenant from the authenticated principal so a forged tenant_id cannot move
    spend into another tenant."""
    if not os.getenv("ABEN_PRINCIPALS"):
        return None
    return {p.pseudonym: p for p in _principals._by_token.values()}


def _residency_for(rec: DerivedRecord, tenants, cache: dict) -> str:
    # the residency wall must use the tenant registry's AUTHORITATIVE residency, not the edge-supplied
    # rec.residency (a hostile edge could claim any region to cross the wall). an unregistered tenant has
    # no registry entry, so it falls back to the edge value (it is already isolated in its own org bucket).
    tenant = getattr(rec, "tenant_id", None) or "default"
    key = f"res:{tenant}"
    if key not in cache:
        t = tenants.get(tenant)
        cache[key] = t.residency if t is not None else (getattr(rec, "residency", None) or "eu")
    return cache[key]


def _org_for(rec: DerivedRecord, tenants, cache: dict) -> str:
    # resolve a record's org from its tenant via the registry, cached per batch. a registered tenant
    # carries its real org. an UNREGISTERED tenant gets its OWN per-tenant org bucket ("unreg:<id>") -
    # NOT a shared "default" - so a developer still matches others in the same tenant (day-one demo)
    # while two DIFFERENT unregistered tenants (which may be two different companies on a shared
    # collector) never land in the same org bucket, and the broker never introduces them.
    tenant = getattr(rec, "tenant_id", None) or "default"
    if tenant not in cache:
        t = tenants.get(tenant)
        cache[tenant] = t.org if t is not None else f"unreg:{tenant}"
    return cache[tenant]


def _valid_embedding(emb) -> bool:
    # the embedding is caller-supplied; a NaN/inf/zero-norm or absurdly-sized vector must never reach
    # the broker (it would corrupt cosine math or be a cheap way to pollute matching). content-free check.
    import math
    if not isinstance(emb, list) or not (2 <= len(emb) <= 4096):
        return False
    norm = 0.0
    for x in emb:
        if not isinstance(x, (int, float)) or not math.isfinite(x):
            return False
        norm += x * x
    return norm > 0.0


def _match_centrally(rec: DerivedRecord, mstore: MatchStore, ledger=None, org: str = "default",
                     residency: str = "eu", capsules=None) -> None:
    # double-blind matching at the collector over content-free signals. writes one row per side,
    # each owner sees only their own. management never sees this - it is not a report.
    if not rec.embedding or not rec.objective_id or not rec.actor_pseudonym:
        return
    if not _valid_embedding(rec.embedding):     # reject garbage vectors before they hit the broker
        return
    obj = _kg.objectives.get(rec.objective_id)
    tenant = getattr(rec, "tenant_id", None) or "default"
    topic = rec.objective_label or "general"
    # a clean call is treated as a solved piece of work that others can reuse. a retry loop is a
    # developer still thrashing, so it is not yet a solved pattern. this is what lets a later developer
    # match already-cracked work as reuse instead of only catching two people working live at once.
    solved = not getattr(rec, "is_retry_loop", False)
    sig = TopicSignal(
        actor_pseudonym=rec.actor_pseudonym, topic_embedding=rec.embedding,
        topic_label=topic, client=getattr(obj, "client", None),
        residency=residency,                                # registry-authoritative, not edge-supplied
        org=org,                                            # enforce the org wall centrally
        # only a KG-RESOLVED objective is a trusted same-objective key (None for an unknown/forged id,
        # which then needs the stricter cross-objective bar instead of the spoofable label).
        objective_id=rec.objective_id if obj is not None else None,
        is_solved=solved,
    )
    # remember how this developer cracked this topic so a future matcher gets actionable, content-free
    # help at match time. only on a KG-resolved objective, so a forged id cannot seed the corpus.
    if solved and obj is not None and capsules is not None:
        try:
            capsules.record_solved(rec.actor_pseudonym, topic, work_type=rec.work_type or "unknown",
                                   model=rec.request_model or "unknown", tool=rec.tool or "unknown",
                                   retry_loops=0, usd=rec.cost_usd or 0.0, ts=rec.ts)
        except Exception:
            pass
    for m in _broker.submit(sig):
        mstore.record(m.a, m.b, m.topic, m.similarity, m.mode)
        mstore.record(m.b, m.a, m.topic, m.similarity, m.mode)
        # book the avoided re-solve: the broker just surfaced a reusable/duplicate effort, so part (or
        # all) of a second solve is avoided. the dollar value is recomputed live at report time.
        if ledger is not None:
            _book_avoided(rec, m, tenant, ledger)


def _book_avoided(rec: DerivedRecord, m, tenant: str, ledger) -> None:
    # when the knowledge graph is populated, a record may only book against a KNOWN objective - a forged
    # or unknown objective_id from a (trusted-but-buggy) edge can't be used to inflate savings against
    # an arbitrary scope. with no KG configured (offline/dev) we don't gate, so the demo still books.
    from abenlux.ledger import AvoidedCostEvent
    if _kg.objectives and rec.objective_id not in _kg.objectives:
        return
    # book only the content-free FACT of the avoided re-solve. the dollar value and the k-gate are
    # recomputed at read time in ledger.summary from the live derived store, so they are deterministic
    # (no ingest-order dependence) and track the cohort as it grows. value/actors here are advisory.
    ev = AvoidedCostEvent(
        tenant_id=tenant, objective_id=rec.objective_id, work_type=rec.work_type or "unknown",
        cluster_id=m.topic, estimated_avoided_usd=0.0, mode=m.mode, actors=0, ts=rec.ts,
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
    capsules = _capsules()
    raw_matches = mstore.for_owner(principal.pseudonym, max_age_s=_MATCH_TTL_S)  # drop stale pairings
    matches = []
    for m in raw_matches:
        revealed, card = None, None
        # consent is scoped to THIS topic: an intro granted on one shared problem never auto-reveals
        # identity on a different, later match between the same two developers.
        if mstore.mutually_consented(principal.pseudonym, m["peer"], m["topic"]):
            # identity AND contact handles revealed only after BOTH opted in, on this topic
            card = _peer_card(m["peer"], contacts)
            revealed = card.get("name")
        # on a reuse match the peer already solved this. attach their content-free solution capsule (which
        # model and tool cracked it, how many retry loops, a coarse cost band) so the developer gets
        # actionable help right now, before any intro. the capsule holds no code and no prompt.
        capsule = capsules.get(m["peer"], m["topic"]) if m["mode"] == "solved_reuse" else None
        matches.append({
            "id": m["id"], "topic": m["topic"], "similarity": m["similarity"],
            "mode": m["mode"], "peer_revealed": revealed, "peer_contact": card,
            "you_requested": mstore.has_consented(principal.pseudonym, m["peer"], m["topic"]),
            "capsule": capsule,
        })
    mstore.close()
    contacts.close()
    capsules.close()
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
    peer, topic = owned[match_id]["peer"], owned[match_id]["topic"]
    mstore.record_consent(principal.pseudonym, peer, topic)        # consent is per-topic
    mutual = mstore.mutually_consented(principal.pseudonym, peer, topic)
    card = None
    if mutual:
        contacts = _contacts()
        card = _peer_card(peer, contacts)
        contacts.close()
    mstore.close()
    return {"consented": True, "mutual": mutual,
            "peer_revealed": card.get("name") if card else None, "peer_contact": card}


@app.post("/api/collab/{match_id}/ask")
async def collab_ask(match_id: int, request: Request, principal: Principal = Depends(current_principal)):
    # send the matched peer one redacted question now, without waiting for a mutual intro. double-blind.
    _need(principal, Permission.VIEW_OWN)
    from abenlux.processing.redact import redact
    body = await request.json()
    text = redact(str(body.get("text", ""))[:4000]).text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty question")
    mstore = _matches()
    owned = {m["id"]: m for m in mstore.for_owner(principal.pseudonym)}
    mstore.close()
    if match_id not in owned:
        raise HTTPException(status_code=404, detail="match not found for this principal")
    relay = _relay()
    tid = relay.ask(principal.pseudonym, owned[match_id]["peer"], owned[match_id]["topic"], text)
    relay.close()
    return {"thread_id": tid, "sent": True}


@app.get("/api/threads")
async def get_threads(principal: Principal = Depends(current_principal)):
    # the developer's own help threads. the peer stays hidden unless both opted in on that topic.
    _need(principal, Permission.VIEW_OWN)
    relay = _relay()
    mstore = _matches()
    threads = relay.for_participant(principal.pseudonym)
    for t in threads:
        revealed = mstore.mutually_consented(principal.pseudonym, t["peer"], t["topic"])
        t["peer_revealed"] = revealed
        t.pop("peer", None)                  # never expose the raw peer pseudonym to the client
    relay.close()
    mstore.close()
    return {"threads": threads}


@app.post("/api/thread/{thread_id}/reply")
async def thread_reply(thread_id: int, request: Request, principal: Principal = Depends(current_principal)):
    _need(principal, Permission.VIEW_OWN)
    from abenlux.processing.redact import redact
    body = await request.json()
    text = redact(str(body.get("text", ""))[:4000]).text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty reply")
    relay = _relay()
    ok = relay.reply(thread_id, principal.pseudonym, text)
    relay.close()
    if not ok:
        raise HTTPException(status_code=404, detail="thread not found for this principal")
    return {"replied": True}


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


def _collector_orgs() -> set:
    # every org the collector knows about, from the registry (and the default catch-all). used to
    # detect a SHARED multi-org collector, where the 'default' tenant bucket would merge two companies.
    tenants = _tenants()
    try:
        return {t.org for t in tenants.list()}
    finally:
        tenants.close()


def _resolve_report_tenant(principal: Principal, requested: str | None) -> str:
    """a principal reports their OWN tenant by default. they may request another tenant only if it is
    in their own org (admins can manage any tenant in their org) - never another org's. cross-tenant
    DETAIL stays inside the org wall; cross-tenant COMPARISON is the k-anon benchmark, not this report."""
    if not requested or requested == principal.tenant_id:
        scope = principal.tenant_id
    else:
        tenants = _tenants()
        try:
            org = tenants.org_of(requested)
        finally:
            tenants.close()
        if org is None or org != principal.org:
            raise HTTPException(status_code=403, detail="tenant is outside your org")
        scope = requested
    # the 'default' tenant maps to NULL+'default' rows that carry NO org. on a collector that more than
    # one org ingests into, that bucket would merge two companies' aggregates (and a sub-k slice would
    # expose a rival's figures). refuse the default bucket there - register a tenant for your org first,
    # exactly the honesty the benchmark cohort already enforces. single-org deployments are unaffected.
    if scope == "default" and len(_collector_orgs() - {"default"}) > 0:
        raise HTTPException(
            status_code=409,
            detail="this collector hosts multiple orgs; register and use a tenant for your org")
    return scope


@app.get("/api/report")
async def report(tenant: str | None = None, principal: Principal = Depends(current_principal)):
    _need(principal, Permission.VIEW_AGGREGATES)
    scope = _resolve_report_tenant(principal, tenant)
    store = _store()
    outcomes = _outcomes()
    by_obj = outcomes.by_objective()
    outcomes.close()
    rep = management_report(store, k=SETTINGS.k_anon, dp_epsilon=SETTINGS.dp_epsilon, kg=_kg,
                            tenant=scope, outcomes=by_obj)
    ledger = _ledger()
    # avoided re-solves, recomputed live from the derived store, beside spend (never inside it)
    rep["reuse_yield"] = ledger.summary(store, scope, k=SETTINGS.k_anon)
    ledger.close()
    store.close()
    return rep


@app.post("/v1/exchange/submit")
async def exchange_submit(request: Request, authorization: str | None = Header(default=None)):
    # an org posts its OWN already-blurred ratios. ingest-token gated, content-free, no raw figures.
    token = authorization[7:].strip() if (authorization or "").lower().startswith("bearer ") else None
    if token not in SETTINGS.ingest_tokens:
        raise HTTPException(status_code=401, detail="invalid ingest token")
    body = await request.json()
    org = str(body.get("org", "")).strip()
    ratios = body.get("ratios") or {}
    if not org or not isinstance(ratios, dict):
        raise HTTPException(status_code=400, detail="need org and ratios")
    ex = _exchange()
    n = ex.submit(org, ratios)
    ex.close()
    return {"submitted": n}


@app.get("/api/exchange")
async def exchange(principal: Principal = Depends(current_principal)):
    # the cross-org percentile for the caller's OWN org. never another org's figure.
    _need(principal, Permission.VIEW_AGGREGATES)
    from abenlux.analytics.exchange import secure_aggregate
    ex = _exchange()
    rows = ex.rows()
    ex.close()
    return secure_aggregate(rows, principal.org)


@app.get("/api/orphans")
async def orphans(tenant: str | None = None, principal: Principal = Depends(current_principal)):
    # proposals to name groups of unattributed spend, so it stops being orphan. management view, k-gated.
    _need(principal, Permission.VIEW_AGGREGATES)
    from abenlux.analytics.recovery import recover_orphans
    scope = _resolve_report_tenant(principal, tenant)
    store = _store()
    out = recover_orphans(store, tenant=scope, k=SETTINGS.k_anon)
    store.close()
    return out


@app.get("/api/negotiation")
async def negotiation(tenant: str | None = None, principal: Principal = Depends(current_principal)):
    # the renewal pack. the blended rate the org pays across every tool, how concentrated the spend is,
    # and what a committed-use discount would save. management view, k-anonymity gated.
    _need(principal, Permission.VIEW_AGGREGATES)
    from abenlux.analytics.negotiation import negotiation_pack
    scope = _resolve_report_tenant(principal, tenant)
    store = _store()
    pack = negotiation_pack(store, tenant=scope, k=SETTINGS.k_anon)
    store.close()
    return pack


@app.get("/api/export")
async def export(dimension: str = "objective", format: str = "csv", tenant: str | None = None,
                 principal: Principal = Depends(current_principal)):
    # finance export: content-free aggregate spend rows for the FinOps/chargeback system. gated on
    # VIEW_COST (the permission that previously had no endpoint), scoped to the caller's org, and
    # k-anonymized - only groups that clear k are exported, never an individual-revealing sub-k row.
    _need(principal, Permission.VIEW_COST)
    scope = _resolve_report_tenant(principal, tenant)
    store = _store()
    try:
        rows = store.rollup(dimension, tenant=scope)
    except ValueError as e:
        store.close()
        raise HTTPException(status_code=400, detail=str(e))
    store.close()
    from abenlux.privacy.pseudonymize import KAnonymityGate
    gate = KAnonymityGate(k=SETTINGS.k_anon)
    out = [{"dimension": dimension, "label": r["label"], "calls": r["calls"], "tokens": r["tokens"],
            "cost_usd": round(r["cost"], 4), "actors": r["actors"]}
           for r in rows if gate.allows(r["actors"])]
    if format == "json":
        return {"dimension": dimension, "tenant": scope, "k": SETTINGS.k_anon, "rows": out}
    import csv
    import io

    def _csv_cell(v):
        # neutralize CSV/formula injection: a label beginning with =,+,-,@ (or a control char) is data,
        # not a spreadsheet formula. prefix it so a downstream Excel/Sheets cannot execute it.
        s = str(v)
        return ("'" + s) if s[:1] in ("=", "+", "-", "@", "\t", "\r", "\n") else s
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["dimension", "label", "calls", "tokens", "cost_usd", "actors"])
    for r in out:
        w.writerow([_csv_cell(r["dimension"]), _csv_cell(r["label"]), r["calls"], r["tokens"],
                    r["cost_usd"], r["actors"]])
    from fastapi.responses import Response
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=abenlux-{dimension}-{scope}.csv"})


@app.get("/api/savings")
async def savings(tenant: str | None = None, principal: Principal = Depends(current_principal)):
    # the reuse-yield ledger: estimated cost of re-solves avoided, k-anonymity gated, scoped to the
    # caller's tenant (or another tenant in their org). a savings figure, shown beside spend not inside.
    _need(principal, Permission.VIEW_AGGREGATES)
    scope = _resolve_report_tenant(principal, tenant)
    store = _store()
    ledger = _ledger()
    out = ledger.summary(store, scope, k=SETTINGS.k_anon)
    ledger.close()
    store.close()
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
            "mutual": mstore.mutually_consented(principal.pseudonym, m["peer"], m["topic"])}
           for m in mstore.for_owner(principal.pseudonym, max_age_s=_MATCH_TTL_S)]
    mstore.close()
    return {"matches": out}


@app.get("/api/drift")
async def drift(tenant: str | None = None, principal: Principal = Depends(current_principal)):
    _need(principal, Permission.VIEW_AGGREGATES)
    from dataclasses import asdict

    from abenlux.analytics.drift import spend_trend
    scope = _resolve_report_tenant(principal, tenant)  # never expose the org-wide cross-tenant trend
    store = _store()
    rep = spend_trend(store, tenant=scope, k=SETTINGS.k_anon)  # k-gate sub-k windows
    store.close()
    return {"trend": asdict(rep) if rep else None}


@app.get("/api/rollup/{dimension}")
async def rollup(dimension: str, tenant: str | None = None,
                 principal: Principal = Depends(current_principal)):
    _need(principal, Permission.VIEW_AGGREGATES)
    scope = _resolve_report_tenant(principal, tenant)  # scope to the caller's org, like report/savings
    store = _store()
    try:
        rows = store.rollup(dimension, tenant=scope)
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
    # the cohort is the caller's OWN org's REGISTERED tenants - never a raw scan of the derived store.
    # on a collector that more than one org ingests into, an unregistered tenant_id carries no proof of
    # org membership, so admitting it (the old distinct_tenants fallback) could pull a foreign company's
    # tenant_ids and ratios into the cohort. registering a tenant (abenlux tenant create) is the price of
    # admission; an org with none registered honestly sees "cohort not ready" against only itself.
    cohort = [t.tenant_id for t in tenants.list(org=principal.org)]
    tenants.close()
    if focus not in cohort:
        cohort = cohort + [focus]
    store = _store()
    ledger = _ledger()
    reuse_by_tenant = {t: ledger.summary(store, t, k=SETTINGS.k_anon)["reuse_avoided_usd"]
                       for t in cohort}
    ledger.close()
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
    # tenant_id is a global key. if another org already owns it, refuse - do not let one org re-create
    # a rival's tenant_id and flip its org (which would pass the org gate and leak the rival's reports).
    existing = tenants.get(tenant_id)
    if existing is not None and existing.org != principal.org:
        tenants.close()
        raise HTTPException(status_code=409, detail="tenant_id already belongs to another org")
    try:
        saved = tenants.upsert(Tenant(
            tenant_id=tenant_id, org=principal.org, display_name=display,
            residency=residency, created_ts=_time.time(),
        ))
    except ValueError:                  # lost a race to another org's concurrent create -> 409
        tenants.close()
        raise HTTPException(status_code=409, detail="tenant_id already belongs to another org")
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
