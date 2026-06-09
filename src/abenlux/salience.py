"""
Salient-intent extraction. Long, multi-part prompts (the 20-40 line kind with pasted code, stack
traces, and three asks) are the hard case for both jobs the edge does on a prompt:

  * classifying WHAT the spend is for (a wall of code dilutes the one verb that matters)
  * embedding it for collaboration matching (the topic vector smears across boilerplate)

So before either job we reduce a prompt to its intent-dense core, deterministically and for free:
strip code/data noise, then keep the highest-salience sentences (imperative verbs + signal words).
Research on prompt compression (LLMLingua-2) and keyphrase extraction shows this beats truncation and
sidesteps the 15-47% accuracy drop long contexts cause. No ML model, no network, no per-call cost -
the optional LLM classifier sees only this compressed core, and the embedding is computed over it too,
which is what makes collaboration matching sharp instead of fuzzy.
"""
from __future__ import annotations

import re

# filler/stopwords stripped before the collaboration embedding. the embedding should reflect the
# DOMAIN terms (webhook, idempotent, telemetry), not "i need to please make the" - otherwise two
# developers on the same problem look different just because they phrased the request differently.
_STOP = frozenset(
    "a an the to of for and or but so in on at by with from into onto is are be was were am "
    "do does did doing done i we you they it he she this that these those my our your their its "
    "need needs want wants make makes please can could would should shall will may might must "
    "help me us them get gets got set sets use uses using also then than there here what which who "
    "how why when where if else not no yes ok okay just only very really quite some any all each "
    "let lets like want about above below over under again more most much many few new old "
    "right now today thing things stuff way ways able sure thanks thank reckon think feel".split())

# imperative starters and intent signal words used to score sentences. shared by the work-type
# classifier and the embedding path so "what it's for" and "who else is on it" agree on the intent.
IMPERATIVE = ("fix", "add", "implement", "build", "create", "refactor", "rename", "extract",
              "optimi", "speed", "write", "document", "test", "investigate", "debug", "design",
              "compare", "evaluate", "explore", "support", "integrate", "scaffold", "simplif",
              "remove", "update", "migrate", "review", "explain", "wire", "handle", "prototype")
SIGNAL = ("bug", "error", "broken", "failing", "crash", "exception", "traceback", "slow",
          "performance", "latency", "bottleneck", "endpoint", "feature", "new", "test", "tests",
          "refactor", "docs", "readme", "prototype", "spike", "poc", "saga", "schema", "api",
          "webhook", "retry", "idempoten", "cache", "auth", "migration", "queue", "pipeline")

_FENCE = re.compile(r"```.*?```", re.DOTALL)         # fenced code blocks
_INLINE = re.compile(r"`[^`]*`")                       # inline code spans
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")
_WS = re.compile(r"[ \t]+")


def _is_noise_line(line: str) -> bool:
    """a line that is mostly symbols/non-prose - pasted code, JSON, a stack frame, a diff. these are
    context, not intent, so they are dropped before scoring (which is what helps long prompts most)."""
    s = line.strip()
    if not s:
        return True
    if s[:1] in "{}[]<>|+#@$" or s.startswith(("def ", "class ", "import ", "from ", "    ", "\t",
                                               "at ", "File \"", "Traceback", "- ", "* ", "//")):
        return True
    alphaish = sum(c.isalpha() or c.isspace() for c in s)
    return (alphaish / len(s)) < 0.62


def strip_noise(text: str) -> str:
    t = _FENCE.sub(" ", text)
    t = _INLINE.sub(" ", t)
    kept = [ln for ln in t.splitlines() if not _is_noise_line(ln)]
    return _WS.sub(" ", "\n".join(kept)).strip()


def salient_intent(text: str | None, *, max_chars: int = 400) -> str:
    """reduce a prompt to its intent-dense core. short prompts pass through; long/noisy ones are
    stripped of code/data and compressed to the highest-salience sentences, original order preserved."""
    t = (text or "").strip()
    if not t:
        return ""
    if len(t) <= max_chars:
        return t
    # a long prompt: strip code/data noise, then keep only the intent-bearing sentences. we score even
    # when the cleaned text fits the budget, so low-signal filler ("here is the module...") is dropped
    # too - that filler is exactly what smears the topic vector and weakens matching.
    # a prompt that is ALL code/data (strip_noise empties it) has no prose intent - return nothing
    # rather than falling back to the raw code, so we don't embed/classify on a wall of source.
    cleaned = strip_noise(t)
    sents = [s.strip() for s in _SENT.split(cleaned) if s.strip()]
    if len(sents) <= 1:
        return (sents[0] if sents else cleaned)[:max_chars]
    scored = []
    for i, s in enumerate(sents):
        low = s.lower()
        words = low.split()
        score = 0
        if any(w.startswith(v) for w in words for v in IMPERATIVE):  # an imperative anywhere = intent
            score += 3
        score += sum(1 for w in SIGNAL if w in low)
        if i == 0:                                                    # the opening ask carries weight
            score += 1
        if len(s) > 220:                                             # rambling sentence, less intent-dense
            score -= 1
        scored.append((score, i, s))
    # when a genuinely intent-bearing sentence exists, drop the filler ("here is the module...",
    # "thanks!") entirely - keeping it would smear the topic vector and weaken collaboration matching.
    top = max(s[0] for s in scored)
    floor = 2 if top >= 3 else 0
    candidates = [x for x in scored if x[0] >= floor] or scored
    keep, used = [], 0
    for score, i, s in sorted(candidates, key=lambda x: (-x[0], x[1])):
        if used + len(s) > max_chars and keep:
            break
        keep.append((i, s))
        used += len(s) + 1
    keep.sort()
    return " ".join(s for _, s in keep) or cleaned[:max_chars]


def keyphrases(text: str | None, *, max_terms: int = 22) -> str:
    """the salient intent reduced to its domain keywords (stopwords/filler dropped, deduped). this is
    what we embed for collaboration: two developers on the same problem share these terms even when
    their phrasing differs, so the topic vectors line up. precision-first - it never invents terms."""
    intent = salient_intent(text, max_chars=400)
    out, seen = [], set()
    for tok in re.findall(r"[a-z][a-z0-9+_.-]{2,}", intent.lower()):
        if tok in _STOP or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= max_terms:
            break
    return " ".join(out)
