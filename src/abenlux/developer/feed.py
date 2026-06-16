"""
The developer-private signal feed. This is the surface the user called out as the point of the
whole product: waste nudges and collaboration matches that are visible ONLY to the developer,
and that work identically no matter which tool produced the call.

Why it's tool-agnostic: the feed sits downstream of the normalized CanonicalEvent, so a retry
loop in Claude Code (Tier 1), Aider (Tier 2), or a Cursor usage event (Tier 3) all arrive as the
same WasteSignal and render the same way. The tool is just a tag.

Why it's private: it is written to a file under the developer's OWN home directory on their OWN
machine (~/.abenlux/feed.jsonl), never to the central derived store, never uploaded. There is no
management read path to it - not as a policy toggle, but because the bytes live somewhere
management has no access to. That storage location IS the privacy guarantee.

The feed is append-only and self-trimming so it can't grow unbounded on a busy day. Entries are
small JSON objects: a kind, a human-readable line, and content-free structured fields.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


def _default_path() -> Path:
    env = os.getenv("ABEN_SIGNAL_FEED")
    if env:
        return Path(env)
    return Path.home() / ".abenlux" / "feed.jsonl"


@dataclass
class FeedEntry:
    ts: float
    kind: str            # "retry_loop" | "context_bloat" | "routing_hint" | "collab_*" | ...
    severity: str        # "info" | "warn"
    line: str            # the ambient, developer-facing copy
    tool: Optional[str] = None
    recoverable_tokens: int = 0
    recoverable_usd: float = 0.0
    detail: str = ""


class LocalSignalFeed:
    def __init__(self, path: str | Path | None = None, *, max_entries: int = 2000):
        self.path = Path(path) if path else _default_path()
        self.max_entries = max_entries
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # the feed is written from the gateway's BackgroundTask threadpool (concurrent captures), so the
        # append + read-modify-write trim must be serialized or a trim can drop a concurrently-appended
        # line (and on Windows the 'w' reopen can collide with another thread's 'a' handle).
        self._lock = threading.Lock()
        self._appends = 0
        # amortize the O(n) read-rewrite trim: run it every ~quarter-bound appends, capped so a huge
        # bound doesn't defer trimming too long. the file stays bounded by max_entries + interval.
        self._trim_interval = max(1, min(256, max_entries // 4 or 1))

    def append(self, entry: FeedEntry) -> None:
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(entry)) + "\n")
            self._appends += 1
            if self._appends % self._trim_interval == 0:
                self._trim()

    def append_waste(self, signal, *, tool: str | None = None, recoverable_usd: float = 0.0) -> None:
        self.append(FeedEntry(
            ts=time.time(), kind=signal.kind, severity=signal.severity,
            line=signal.suggestion or signal.detail, tool=tool,
            recoverable_tokens=getattr(signal, "recoverable_tokens", 0),
            recoverable_usd=round(recoverable_usd, 4), detail=signal.detail,
        ))

    def _collab_line(self, mode: str, topic: str) -> str:
        if mode == "solved_reuse":
            return (f"Someone in the org already solved something close to '{topic}'. "
                    f"Reuse beats re-solve - request a double-blind intro to compare notes.")
        return (f"You and another developer are working on very similar problems "
                f"('{topic}') right now. Want a double-blind intro?")

    def append_collab(self, match, *, tool: str | None = None) -> None:
        self.append(FeedEntry(
            ts=time.time(), kind=f"collab_{match.mode}", severity="info",
            line=self._collab_line(match.mode, match.topic), tool=tool,
            detail=f"similarity={match.similarity}",
        ))

    def append_collab_remote(self, topic: str, mode: str, similarity, *, tool: str | None = None) -> None:
        # a match surfaced by polling the central collector (org/forward mode), not the local broker
        self.append(FeedEntry(
            ts=time.time(), kind=f"collab_{mode}", severity="info",
            line=self._collab_line(mode, topic), tool=tool, detail=f"similarity={similarity}",
        ))

    def append_budget(self, line: str, *, tool: str | None = None) -> None:
        self.append(FeedEntry(ts=time.time(), kind="budget_guardrail", severity="warn",
                              line=line, tool=tool))

    def append_savings(self, line: str, *, kind: str = "savings", tool: str | None = None,
                       recoverable_usd: float = 0.0) -> None:
        # a win the developer should just see, routing or a cache hit, no action needed
        self.append(FeedEntry(ts=time.time(), kind=kind, severity="info", line=line, tool=tool,
                              recoverable_usd=round(recoverable_usd, 4)))

    def recent(self, n: int = 20) -> list[dict]:
        if not self.path.exists():
            return []
        with self._lock:
            with self.path.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()
        out = []
        for ln in lines[-n:]:
            try:
                out.append(json.loads(ln))
            except (json.JSONDecodeError, ValueError):
                continue
        return out

    def _trim(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) > self.max_entries:
            with self.path.open("w", encoding="utf-8") as fh:
                fh.writelines(lines[-self.max_entries:])
