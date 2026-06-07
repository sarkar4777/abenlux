r"""
The developer's personal, on-device knowledge graph. Every call the developer makes is recorded
locally (content-free) and woven into a graph they own and can inspect anytime with `abenlux graph`.
It connects what they work on to what it cost and what it was for:

    developer -> objective -> ticket -> work type -> self-learned intent vocabulary
                         \-> tools, models, spend

This is the developer's private mirror of their AI work. It never leaves the machine, it is not the
management plane, and it is what makes the self-learning visible: the learned vocabulary the device
has taught itself is a first-class part of the graph.
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class DevKnowledgeGraph:
    store: object
    learner: object | None = None

    def build(self) -> dict:
        s = self.store
        totals = s.totals()
        objectives = [r for r in s.rollup("objective") if r["label"] != "(unattributed)"]
        learned: dict[str, list[str]] = {}
        if self.learner is not None:
            for label, terms in self.learner.patterns().items():
                learned[label] = sorted(terms)
        return {
            "totals": {"calls": totals["n"], "cost": round(totals["cost"], 2), "tokens": totals["tokens"]},
            "objectives": objectives,
            "tickets": s.ticket_rollup(),
            "work_types": [r for r in s.rollup("work_type")],
            "tools": [r for r in s.rollup("tool")],
            "models": [r for r in s.rollup("model")],
            "learned_vocabulary": learned,
        }

    def render_text(self) -> str:
        g = self.build()
        usd = lambda v: f"${v:,.2f}"  # noqa: E731
        out = ["== Your Abenlux knowledge graph (local, private to this machine) =="]
        t = g["totals"]
        out.append(f" {t['calls']} calls · {usd(t['cost'])} · {t['tokens']:,} tokens\n")

        out.append(" Objectives you work on:")
        tickets_by_obj: dict[str, list] = {}
        for tk in g["tickets"]:
            tickets_by_obj.setdefault(tk.get("objective") or "", []).append(tk)
        if not g["objectives"]:
            out.append("   (none attributed yet)")
        for o in g["objectives"]:
            out.append(f"   * {o['label']:<34} {usd(o['cost'])}  ({o['calls']} calls)")
            for tk in tickets_by_obj.get(o["label"], [])[:6]:
                out.append(f"       - {tk['ticket_id']:<12} {tk['work_type']:<11} {usd(tk['cost'])}")

        out.append("\n What the spend is for (purpose mix):")
        out.append("   " + "  ".join(f"{r['label']} {usd(r['cost'])}" for r in g["work_types"]))
        out.append("\n Tools:  " + "  ".join(f"{r['label']} {usd(r['cost'])}" for r in g["tools"]))
        out.append(" Models: " + "  ".join(f"{r['label']} {usd(r['cost'])}" for r in g["models"]))

        if g["learned_vocabulary"]:
            out.append("\n Self-learned intent vocabulary (the device taught itself, no LLM needed):")
            for label, terms in g["learned_vocabulary"].items():
                out.append(f"   {label:<12}: {', '.join(terms[:8])}")
        return "\n".join(out)

    def to_json(self) -> str:
        return json.dumps(self.build(), indent=2, default=str)
