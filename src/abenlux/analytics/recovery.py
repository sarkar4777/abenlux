"""
Orphan spend recovery. Orphan spend is AI money that did not tie to any business objective, and it is
the headline waste number. Some of it is genuinely untracked work that deserves a name. This loop looks
at the unattributed records that carry a topic vector, groups the ones that are about the same thing,
and where a group is backed by enough developers it proposes a new objective for it, with the repo it
mostly came from. A manager accepts a proposal once and from then on that work tracks automatically, so
tomorrow's orphan pool is smaller. It is a self-healing loop on the headline waste metric.

It reads only content-free fields, the topic vector and the repo and the pseudonym, and it only ever
proposes a group that more than k developers share, so a single person's work is never surfaced.
"""
from __future__ import annotations

from collections import Counter

from abenlux.store import DerivedStore


def _cos(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    na = sum(x * x for x in a[:n]) ** 0.5
    nb = sum(x * x for x in b[:n]) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def recover_orphans(store: DerivedStore, *, tenant: str | None = None, k: int = 5,
                    threshold: float = 0.82) -> dict:
    # greedy grouping of the orphan vectors. each new record joins the first group it is close enough to,
    # otherwise it starts a new group. cheap and order-stable, good enough to surface candidates.
    samples = store.orphan_samples(tenant=tenant)
    groups: list[dict] = []
    for s in samples:
        placed = False
        for g in groups:
            if _cos(s["embedding"], g["centroid"]) >= threshold:
                g["members"].append(s)
                placed = True
                break
        if not placed:
            groups.append({"centroid": s["embedding"], "members": [s]})

    proposals = []
    for g in groups:
        actors = {m["actor"] for m in g["members"]}
        if len(actors) < k:                      # never surface a group fewer than k developers share
            continue
        repos = Counter(m["repo"] for m in g["members"] if m.get("repo"))
        top_repo = repos.most_common(1)[0][0] if repos else None
        spend = round(sum(m["cost"] for m in g["members"]), 2)
        proposals.append({
            "developers": len(actors),
            "records": len(g["members"]),
            "wasted_spend_usd": spend,
            "suggested_repo": top_repo,
            "suggested_objective_label": f"Untracked work in {top_repo}" if top_repo else "Untracked work",
        })
    proposals.sort(key=lambda p: -p["wasted_spend_usd"])
    return {"proposals": proposals,
            "note": "groups of unattributed spend shared by at least the k threshold of developers. "
                    "accept one to give it an objective and shrink tomorrow's orphan pool."}
