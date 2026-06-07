from abenlux.attribution.attributor import KnowledgeGraph, Objective
from abenlux.capture.vendor_admin import cursor_event_to_derived, sync_cursor_usage
from abenlux.privacy.pseudonymize import pseudonymize
from abenlux.schema import CaptureTier


def _kg():
    kg = KnowledgeGraph()
    kg.add_objective(Objective("obj-pay", "Payments platform", client="acme"))
    kg.map_repo("payments-svc", "obj-pay")
    return kg


def test_cursor_event_maps_to_tier3_derived_with_cost_and_join():
    ev = {
        "id": "evt-1", "userEmail": "dev@acme.com", "model": "claude-sonnet-4-6",
        "inputTokens": 1000, "outputTokens": 500, "repoName": "payments-svc", "timestamp": 123.0,
    }
    rec = cursor_event_to_derived(ev, hmac_key=b"k", kg=_kg())
    assert rec.tier == CaptureTier.VENDOR_ADMIN_API.value
    assert rec.tool == "cursor-agent"
    assert rec.objective_id == "obj-pay" and rec.attribution_method == "repo_join"
    assert rec.cost_priced and rec.cost_usd > 0
    assert rec.embedding is None              # no content -> no semantic signal, by design
    assert rec.tokens_estimated is False      # vendor-reported, billed-exact


def test_pseudonym_shared_with_edge_pipeline():
    ev = {"userEmail": "dev@acme.com", "model": "gpt-5.5", "inputTokens": 1, "outputTokens": 1}
    rec = cursor_event_to_derived(ev, hmac_key=b"key", kg=_kg())
    # same HMAC key -> same pseudonym as a Tier-2 capture for the same person
    assert rec.actor_pseudonym == pseudonymize("dev@acme.com", b"key")


def test_sync_iterates_injected_fetch():
    inserted = []
    events = [
        {"userEmail": "a@acme.com", "model": "gpt-5.5", "inputTokens": 10, "outputTokens": 5},
        {"userEmail": "b@acme.com", "model": "claude-opus-4-8", "inputTokens": 20, "outputTokens": 8},
    ]
    n = sync_cursor_usage(lambda: events, hmac_key=b"k", kg=_kg(), insert=inserted.append)
    assert n == 2 and len(inserted) == 2
