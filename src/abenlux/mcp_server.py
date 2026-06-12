"""
Abenlux as a tool the coding agent can call mid task. Today a developer has to stop and run a command
to see their spend or check whether someone already solved their problem. This exposes the same
read-only views over the Model Context Protocol, so the agent that is actually burning the tokens can
ask the questions itself, in flow. It can check its own spend, ask whether it is about to redo work the
team already cracked, look up what a branch is attributed to, and estimate what a model call would cost.

It is a thin wrapper over read functions that already exist and are already tested. It adds no new
permission, it never writes to the warehouse, and it only ever returns the caller's own data, resolved
from the caller's own token exactly the way the web view resolves it. The agent gets a capability, not a
new trust boundary.
"""
from __future__ import annotations

import os

from abenlux.auth.principals import load_principals
from abenlux.settings import SETTINGS


def _principal(token: str | None):
    token = token or os.getenv("ABEN_MCP_TOKEN") or os.getenv("ABEN_TOKEN")
    p = load_principals().resolve(token)
    if p is None:
        raise PermissionError("no valid token. set ABEN_MCP_TOKEN to the developer's access token.")
    return p


_MATCH_TTL_S = float(os.getenv("ABEN_MATCH_TTL_DAYS", "14")) * 86400


def _store():
    # the developer's OWN on-device store, opened the same way the rest of the tool opens it (which
    # honors a Postgres backend too), not a fresh cwd-relative sqlite file.
    from abenlux.store import open_store
    return open_store(SETTINGS.local_db)


def tool_my_spend(token: str | None = None) -> dict:
    """The caller's own spend, work mix, and the mechanical-waste nudges. Only the caller's own rows."""
    from abenlux.analytics.reports import developer_report
    p = _principal(token)
    store = _store()
    rep = developer_report(store, p.pseudonym)
    store.close()
    return rep


def tool_check_reuse(token: str | None = None) -> dict:
    """Has anyone already solved what the caller is working on. Returns the caller's reuse matches and,
    for each solved one, the content-free solution capsule (which model and tool cracked it, a cost
    band). Lets the agent skip redoing work and pick the right model first."""
    from abenlux.developer.capsules import CapsuleStore
    from abenlux.developer.matches import MatchStore
    p = _principal(token)
    # pass None when unset so these resolve to the developer's private ~/.abenlux stores, the same place
    # the gateway wrote them, not a stray empty file in the agent's working directory.
    mstore = MatchStore(os.getenv("ABEN_MATCH_DB"))
    caps = CapsuleStore(os.getenv("ABEN_CAPSULE_DB"))
    out = []
    for m in mstore.for_owner(p.pseudonym, max_age_s=_MATCH_TTL_S):   # drop stale pairings, like the web view
        capsule = caps.get(m["peer"], m["topic"]) if m["mode"] == "solved_reuse" else None
        out.append({"topic": m["topic"], "mode": m["mode"], "similarity": m["similarity"],
                    "capsule": capsule})
    mstore.close()
    caps.close()
    return {"matches": out}


def tool_attribute(branch: str | None = None, repo: str | None = None, ticket: str | None = None) -> dict:
    """What objective and kind of work a branch or ticket maps to, by the same join the meter uses."""
    from abenlux.attribution.attributor import (
        KnowledgeGraph,
        classify_work_type,
        extract_ticket,
    )
    kg_path = os.getenv("ABEN_KG") or SETTINGS.kg_path
    kg = KnowledgeGraph.from_yaml(kg_path) if kg_path else KnowledgeGraph()
    tkt = ticket or extract_ticket(branch)
    objective = None
    if tkt and "-" in tkt:
        objective = kg.ticket_prefix_to_objective.get(tkt.split("-", 1)[0].upper())
    obj = kg.objectives.get(objective) if objective else None
    return {"ticket": tkt, "objective_id": objective,
            "objective_label": obj.label if obj else None,
            "work_type": classify_work_type(branch, tkt)}


def tool_cost_estimate(model: str, input_tokens: int = 0, output_tokens: int = 0,
                       cache_read_tokens: int = 0) -> dict:
    """What a model call would cost in dollars, so the agent can right-size the model before it calls."""
    from abenlux.pricing import cost_usd
    cb = cost_usd(model, int(input_tokens), int(output_tokens), cache_read_tokens=int(cache_read_tokens))
    return {"model": model, "cost_usd": round(cb.total, 6), "priced": cb.priced}


TOOLS = {
    "my_spend": tool_my_spend,
    "check_reuse": tool_check_reuse,
    "attribute": tool_attribute,
    "cost_estimate": tool_cost_estimate,
}


def serve() -> None:
    # run the read plane as an MCP server over stdio. needs the optional mcp package.
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:
        raise SystemExit(
            "the mcp package is needed for the server. install it with: pip install \"abenlux[mcp]\"") from e
    server = FastMCP("abenlux")
    server.tool()(tool_my_spend)
    server.tool()(tool_check_reuse)
    server.tool()(tool_attribute)
    server.tool()(tool_cost_estimate)
    server.run()
