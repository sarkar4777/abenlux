"""
Cost model. Tokens become spend here. Without this the product reports token *counts*,
which no finance org acts on, the whole premise is "AI spend -> value", so a defensible
$/token table is load-bearing.

Rates are published list prices per 1M tokens (USD), captured 2026-06. They drift, so the
table is data, not logic: override `PRICES` from a YAML/secret at deploy time rather than
editing code. Cache reads are heavily discounted and cache writes carry a premium, both are
modeled because resent-history bloat (the headline waste signal) only shows real savings
once cache economics are accounted for.

Matching is longest-prefix on the model id so a new point release (claude-opus-4-8-xxxxxx)
inherits its family's price instead of silently falling to $0 and understating spend. An
unmatched model returns a zero-cost estimate flagged `priced=False` so the dashboard can
show "unpriced", never a confident wrong number.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    # all values are USD per 1,000,000 tokens
    input: float
    output: float
    cache_read: float = 0.0      # discounted reuse of cached input
    cache_write: float = 0.0     # premium to establish a cache entry

    @classmethod
    def simple(cls, inp: float, out: float) -> "ModelPrice":
        # convention: anthropic-style cache read = 0.1x input, write = 1.25x input
        return cls(inp, out, round(inp * 0.1, 4), round(inp * 1.25, 4))


# longest-prefix keys -> price. order-independent, we sort by key length at lookup.
PRICES: dict[str, ModelPrice] = {
    # --- anthropic (per-Mtok, cache read 0.1x, write 1.25x) ---
    "claude-opus-4": ModelPrice(5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-4": ModelPrice(3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4": ModelPrice(1.00, 5.00, 0.10, 1.25),
    "claude-3-5-haiku": ModelPrice(0.80, 4.00, 0.08, 1.00),
    "claude-3-haiku": ModelPrice(0.25, 1.25, 0.03, 0.30),
    "claude-3-opus": ModelPrice(15.00, 75.00, 1.50, 18.75),
    # --- openai ---
    "gpt-5.5": ModelPrice(5.00, 30.00, 0.50, 0.0),
    "gpt-5.4": ModelPrice(2.50, 15.00, 0.25, 0.0),
    "gpt-5": ModelPrice(2.50, 15.00, 0.25, 0.0),
    "gpt-4o-mini": ModelPrice(0.15, 0.60, 0.075, 0.0),
    "gpt-4o": ModelPrice(2.50, 10.00, 1.25, 0.0),
    "gpt-4.1-mini": ModelPrice(0.40, 1.60, 0.10, 0.0),
    "gpt-4.1": ModelPrice(2.00, 8.00, 0.50, 0.0),
    "o4-mini": ModelPrice(1.10, 4.40, 0.275, 0.0),
    "o3": ModelPrice(2.00, 8.00, 0.50, 0.0),
    # --- google gemini ---
    "gemini-3.5-flash": ModelPrice(1.50, 9.00, 0.375, 0.0),
    "gemini-3.1-pro": ModelPrice(2.00, 12.00, 0.50, 0.0),
    "gemini-3": ModelPrice(2.00, 12.00, 0.50, 0.0),
    "gemini-2.5-pro": ModelPrice(1.25, 10.00, 0.31, 0.0),
    "gemini-2.5-flash": ModelPrice(0.30, 2.50, 0.075, 0.0),
    "gemini-2.0-flash": ModelPrice(0.10, 0.40, 0.025, 0.0),
    # --- aws bedrock / azure inherit by family below via normalization ---
}


@dataclass
class CostBreakdown:
    input_cost: float
    output_cost: float
    cache_cost: float
    total: float
    priced: bool          # False -> model unknown, total is a 0 placeholder, flag it
    matched_key: str | None


def _normalize(model: str) -> str:
    """strip provider routing prefixes so 'anthropic/claude-opus-4-8' and
    'bedrock/anthropic.claude-opus-4' both match the family key."""
    m = model.lower().strip()
    m = m.split("/")[-1]                 # drop 'anthropic/', 'openrouter/...'
    m = m.replace("anthropic.", "").replace("google.", "").replace("openai.", "")
    m = m.removeprefix("us.").removeprefix("eu.").removeprefix("apac.")  # bedrock region prefixes
    return m


def price_for(model: str | None) -> ModelPrice | None:
    if not model:
        return None
    norm = _normalize(model)
    # longest matching prefix wins (so claude-opus-4 beats a hypothetical claude-)
    best: tuple[int, ModelPrice] | None = None
    for key, price in PRICES.items():
        if norm.startswith(key) and (best is None or len(key) > best[0]):
            best = (len(key), price)
    return best[1] if best else None


def cache_recoverable_usd(model: str | None, fresh_resent_tokens: int) -> float:
    """The LOSSLESS saving from serving resent context as a cache read instead of fresh input:
    the per-token gap between the input rate and the discounted cache-read rate. This is the only
    waste lever that costs nothing in accuracy or detail - the exact same context is sent, it is
    just billed as a cache hit. Returns 0 for an unpriced model or non-positive token count."""
    p = price_for(model)
    if not p or fresh_resent_tokens <= 0:
        return 0.0
    return round(fresh_resent_tokens * max(0.0, p.input - p.cache_read) / 1_000_000, 6)


def cost_usd(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> CostBreakdown:
    """compute spend for one interaction. cache_read tokens are billed at the discounted
    rate and are NOT double-counted in input (callers pass the non-cached input count)."""
    p = price_for(model)
    if p is None:
        return CostBreakdown(0.0, 0.0, 0.0, 0.0, priced=False, matched_key=None)

    inp = input_tokens / 1_000_000 * p.input
    out = output_tokens / 1_000_000 * p.output
    cache = (
        cache_read_tokens / 1_000_000 * p.cache_read
        + cache_creation_tokens / 1_000_000 * p.cache_write
    )
    total = round(inp + out + cache, 6)
    norm = _normalize(model)
    matched = max((k for k in PRICES if norm.startswith(k)), key=len, default=None)
    return CostBreakdown(
        round(inp, 6), round(out, 6), round(cache, 6), total, priced=True, matched_key=matched
    )
