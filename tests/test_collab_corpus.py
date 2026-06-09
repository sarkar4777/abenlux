"""
Thorough collaboration-matching corpus. Two developers should be paired when they're genuinely on the
same problem (even phrased differently, even buried in different boilerplate) and NOT paired when they
just share generic vocabulary. We run the real path - salient-intent extraction, embedding, and the
objective-aware broker bar - over labeled task groups and measure precision and recall.
"""
import itertools

from abenlux.salience import keyphrases
from abenlux.embedding import hashing_embed
from abenlux.collaborate.broker import CollaborationBroker, cosine

# task groups: phrasings of the SAME problem (same objective). within a group -> should match,
# across groups -> should not (unless near-identical text, which is legitimate technique reuse).
GROUPS = {
    "acme/webhook-idempotency": [
        "Design an idempotent retry strategy for the checkout payment webhook so duplicate events don't double-charge.",
        "I need to make the checkout payment webhook idempotent so retried events never double charge the customer.",
        "```js\nconst x=1\n```\nMake the payment webhook retry idempotent - duplicate checkout events must not double charge.",
    ],
    "umbrella/opcua-mqtt-bridge": [
        "Build an OPC-UA to MQTT bridge for the plant telemetry gateway.",
        "Create a bridge that forwards OPC-UA plant telemetry to an MQTT broker.",
        "We need an OPC-UA to MQTT bridge so factory sensor data lands on the MQTT topic.",
    ],
    "globex/tool-router": [
        "Design the tool-call router for the agent marketplace runtime.",
        "Implement a router that dispatches agent tool calls in the marketplace runtime.",
    ],
    "acme/dark-mode": [
        "Add a dark mode toggle to the settings page.",
        "Implement a dark theme switch on the account settings screen.",
    ],
}
OBJ = {  # which objective each group belongs to (drives the broker's same/cross bar)
    "acme/webhook-idempotency": "Acme - Checkout",
    "umbrella/opcua-mqtt-bridge": "Umbrella - IT/OT",
    "globex/tool-router": "Globex - Runtime",
    "acme/dark-mode": "Acme - Checkout",
}


def _match(p1, o1, p2, o2) -> bool:
    sim = cosine(hashing_embed(keyphrases(p1)), hashing_embed(keyphrases(p2)))
    br = CollaborationBroker()
    bar = br.threshold if o1 == o2 else br.cross_threshold
    return sim >= bar


def test_collaboration_precision_and_recall():
    items = [(g, p, OBJ[g]) for g, ps in GROUPS.items() for p in ps]
    tp = fp = fn = tn = 0
    false_pairs = []
    for (g1, p1, o1), (g2, p2, o2) in itertools.combinations(items, 2):
        should = g1 == g2                      # same task group = should be paired
        did = _match(p1, o1, p2, o2)
        if should and did:
            tp += 1
        elif should and not did:
            fn += 1
            false_pairs.append(("MISSED", g1, p1[:40], p2[:40]))
        elif not should and did:
            fp += 1
            false_pairs.append(("FALSE+", f"{g1} vs {g2}", p1[:40], p2[:40]))
        else:
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    print(f"\ncollaboration: tp={tp} fp={fp} fn={fn} tn={tn}")
    print(f"  precision={precision*100:.1f}%  recall={recall*100:.1f}%")
    for kind, where, a, b in false_pairs:
        print(f"  {kind}: {where} :: {a!r} / {b!r}")

    assert precision == 1.0, f"a false collaboration match slipped through (precision {precision:.0%})"
    assert recall >= 0.90, f"missed genuine collaborators (recall {recall:.0%})"


def test_generic_vocabulary_does_not_match():
    # same verb, different work, different objective -> must NOT pair
    a = ("write unit tests for the cart checkout service", "Acme - Checkout")
    b = ("write unit tests for the OPC-UA telemetry parser", "Umbrella - IT/OT")
    assert not _match(a[0], a[1], b[0], b[1])
