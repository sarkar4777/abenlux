from abenlux.pricing import cost_usd, price_for


def test_longest_prefix_match_picks_specific_family():
    # point release inherits its family price, not $0
    assert price_for("claude-opus-4-8-20260528").input == 5.0
    assert price_for("gpt-5.5-preview").output == 30.0
    assert price_for("gemini-2.5-pro-exp").input == 1.25


def test_provider_routing_prefixes_normalize():
    assert price_for("anthropic/claude-opus-4-8").input == 5.0
    assert price_for("bedrock/anthropic.claude-opus-4").input == 5.0
    assert price_for("us.anthropic.claude-sonnet-4-6").input == 3.0


def test_unpriced_model_is_flagged_not_zeroed_silently():
    cb = cost_usd("some-unknown-model-9", 1000, 1000)
    assert cb.priced is False
    assert cb.total == 0.0
    assert cb.matched_key is None


def test_cost_math_with_cache():
    # 1M input @5, 1M output @25, 1M cache-read @0.5
    cb = cost_usd("claude-opus-4-8", 1_000_000, 1_000_000, cache_read_tokens=1_000_000)
    assert cb.input_cost == 5.0
    assert cb.output_cost == 25.0
    assert cb.cache_cost == 0.5
    assert cb.total == 30.5
    assert cb.priced is True


def test_cache_creation_premium_modeled():
    cb = cost_usd("claude-sonnet-4-6", 0, 0, cache_creation_tokens=1_000_000)
    assert cb.cache_cost == 3.75  # 1.25x the $3 input rate
