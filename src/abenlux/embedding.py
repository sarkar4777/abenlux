"""
Embedding backend selection. The pipeline derives a vector per interaction for two jobs:
semantic attribution fallback and the collaboration broker. Both want *semantic* similarity,
but the scaffold must run offline with zero heavy deps - so this module returns the best
embedder available and the rest of the system stays agnostic to which one it got.

  * if sentence-transformers is installed ([ml] extra), use a real MiniLM encoder (cached).
  * otherwise fall back to the deterministic hashing embedder, which is enough for the
    lexical-ish demo and for tests, and degrades attribution to "join-only" gracefully.

The choice is logged once so an operator is never confused about whether semantic matching
is actually on. Returning a callable (not a class) keeps the injection seam trivial.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from typing import Callable

EmbedFn = Callable[[str], list[float]]


def hashing_embed(text: str, dims: int = 64) -> list[float]:
    """deterministic, dependency-free embedding. bag-of-hashed-tokens, L2-normalized.
    not semantic, but stable and offline, the system treats any embedder uniformly."""
    vec = [0.0] * dims
    for tok in text.lower().split():
        h = int(hashlib.blake2b(tok.encode(), digest_size=8).hexdigest(), 16)
        vec[h % dims] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [round(v / norm, 6) for v in vec]


@lru_cache(maxsize=1)
def _sentence_transformer():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer("all-MiniLM-L6-v2")


@lru_cache(maxsize=1)
def get_embedder() -> EmbedFn:
    """return the strongest embedder available, cached so the model loads at most once."""
    try:
        model = _sentence_transformer()
    except Exception:
        return hashing_embed

    def _embed(text: str) -> list[float]:
        return model.encode(text or "", normalize_embeddings=True).tolist()

    return _embed
