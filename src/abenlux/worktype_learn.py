"""
Self-learning work-type memory. No classification signal is wasted: every time the purpose of a
call is known with confidence - because the branch said so (ground truth) or the LLM resolved it -
the prompt's vocabulary is fed back here. Terms that consistently and repeatedly co-occur with one
work type get promoted into a learned keyword layer that the FREE classifier then uses.

The effect compounds: a team that follows branch conventions teaches the system its own language, so
later prompts with no branch are caught by patterns for free, and the (already minimal) LLM fallback
fires less and less over time. Deterministic and auditable - promotion needs a minimum support count
and a dominant-label share, so one-off identifiers never get learned and ambiguous terms never flip.

Privacy: this runs on the edge on REDACTED text and persists only aggregate term->label counts to a
local file on the developer's own machine. Prompts are never stored. Bounded in size, hot-reloaded.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

_TOKEN = re.compile(r"[a-z][a-z0-9+#.\-]{3,}")
_STOP = {
    "the", "this", "that", "with", "into", "from", "your", "you", "our", "for", "and", "but", "not",
    "please", "help", "need", "want", "make", "using", "use", "when", "what", "should", "could",
    "would", "there", "here", "just", "like", "some", "more", "very", "also", "then", "than", "them",
    "they", "have", "has", "can", "will", "are", "was", "were", "its", "it's", "code", "function",
    "method", "class", "file", "line", "issue", "thing", "stuff", "about", "which", "where", "how",
}


def _terms(text: str) -> list[str]:
    # unigrams + adjacent bigrams of salient tokens. bigrams catch "race condition", "memory leak".
    # cap to the first ~400 salient words so a 30-line prompt does not flood the counts with noise.
    words = [w for w in _TOKEN.findall(text.lower()) if w not in _STOP][:400]
    out = list(dict.fromkeys(words))  # de-dup, keep order
    for a, b in zip(words, words[1:]):
        out.append(f"{a} {b}")
    return out


class WorkTypeLearner:
    def __init__(self, path: str | Path | None = None, *, min_count: int = 4,
                 dominant_share: float = 0.8, max_terms: int = 5000):
        self.path = Path(path) if path else (Path(os.getenv("ABEN_WT_MEMORY")
                    or (Path.home() / ".abenlux" / "worktype_memory.json")))
        self.min_count = min_count
        self.dominant_share = dominant_share
        self.max_terms = max_terms
        self._lock = threading.Lock()
        self._mtime = 0.0
        self.counts: dict[str, dict[str, int]] = {}
        self.learned: dict[str, str] = {}
        self._dirty_since_save = 0
        self._load()

    # ----- learning -----
    def observe(self, text: str | None, label: str) -> None:
        """teach: this redacted prompt mapped to `label`. promotes stable terms. never raises."""
        if not text or not label or label == "unknown":
            return
        try:
            with self._lock:
                for term in _terms(text):
                    bucket = self.counts.setdefault(term, {})
                    bucket[label] = bucket.get(label, 0) + 1
                    self._maybe_promote(term, bucket)
                self._evict_if_needed()
                self._dirty_since_save += 1
                if self._dirty_since_save >= 20:
                    self._save()
        except Exception:
            pass

    def _maybe_promote(self, term: str, bucket: dict[str, int]) -> None:
        total = sum(bucket.values())
        if total < self.min_count:
            return
        top_label, top = max(bucket.items(), key=lambda kv: kv[1])
        if top / total >= self.dominant_share:
            self.learned[term] = top_label
        elif term in self.learned:
            del self.learned[term]  # became ambiguous, withdraw it

    def _evict_if_needed(self) -> None:
        if len(self.counts) <= self.max_terms:
            return
        # drop the least-supported, never-promoted terms first
        victims = sorted((t for t in self.counts if t not in self.learned),
                         key=lambda t: sum(self.counts[t].values()))
        for t in victims[: len(self.counts) - self.max_terms]:
            self.counts.pop(t, None)

    # ----- classifying input -----
    def patterns(self) -> dict[str, set[str]]:
        """label -> set of learned terms, hot-reloaded if the file changed under us."""
        # hold the lock across reload + iteration: observe() mutates self.learned from capture threads,
        # and a hot-reload replaces it wholesale - reading it unsynchronized risks a dict-changed-during-
        # iteration error and a torn read. _reload_if_changed/_load assume the lock is already held.
        with self._lock:
            self._reload_if_changed()
            out: dict[str, set[str]] = {}
            for term, label in self.learned.items():
                out.setdefault(label, set()).add(term)
            return out

    # ----- persistence -----
    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.counts = data.get("counts", {})
                self.learned = data.get("learned", {})
                self._mtime = self.path.stat().st_mtime
        except Exception:
            pass

    def _reload_if_changed(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_mtime != self._mtime:
                self._load()
        except Exception:
            pass

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"counts": self.counts, "learned": self.learned}),
                                 encoding="utf-8")
            self._mtime = self.path.stat().st_mtime
            self._dirty_since_save = 0
        except Exception:
            pass

    def flush(self) -> None:
        with self._lock:
            self._save()
