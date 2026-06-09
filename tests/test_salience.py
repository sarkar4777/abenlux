"""
Long-prompt handling and collaboration accuracy. The salient-intent extractor is the keystone: it
makes a long, code-heavy, multi-part prompt classify and match on its TASK, not its boilerplate.
"""
import math

from abenlux.salience import keyphrases, salient_intent, strip_noise
from abenlux.embedding import hashing_embed
from abenlux.collaborate.broker import CollaborationBroker, TopicSignal, cosine


# the real case: a SMALL shared intent buried under a LARGE wall of DIFFERENT pasted code/data
_CODE_A = "\n".join(f"def alpha_{i}(state): return state.bucket[{i}] + zonk_{i}(payload)" for i in range(60))
_CODE_B = "\n".join(f"const beta{i} = (q, r) => widget.render({{id: {i}, q, r}});" for i in range(60))

LONG_A = f"""Here is the whole module the agent keeps resending for context:
```python
{_CODE_A}
```
{{"config": {{"retries": 3, "timeout": 30}}, "flags": ["a","b","c"]}}
Design an idempotent retry strategy for the checkout payment webhook so duplicate events do not double-charge."""

LONG_B = f"""Pasting my entire frontend bundle below so you have everything:
```js
{_CODE_B}
```
import os, sys, json, collections, itertools, functools, dataclasses
Design an idempotent retry strategy for the checkout payment webhook so duplicate events do not double-charge."""

UNRELATED = """Build an OPC-UA to MQTT bridge for the plant telemetry gateway and keep the session alive across certificate rotation."""


def test_strip_noise_removes_code_and_data():
    out = strip_noise(LONG_A)
    assert "def charge" not in out and "Traceback" not in out and '{"config"' not in out
    assert "idempotent retry" in out


def test_salient_intent_pulls_the_task_from_a_long_prompt():
    s = salient_intent(LONG_A, max_chars=200)
    assert "idempotent retry" in s.lower()
    assert "def charge" not in s and "traceback" not in s.lower()
    assert len(s) <= 200


def test_salience_sharpens_collaboration_matching():
    # two prompts with the SAME task buried in DIFFERENT boilerplate
    raw_sim = cosine(hashing_embed(LONG_A), hashing_embed(LONG_B))
    key_sim = cosine(hashing_embed(keyphrases(LONG_A)), hashing_embed(keyphrases(LONG_B)))
    assert key_sim > raw_sim                          # keyphrase extraction makes the vectors agree
    assert key_sim >= CollaborationBroker().threshold  # and clears the broker bar
    # an unrelated task stays well below the bar
    assert cosine(hashing_embed(keyphrases(LONG_A)), hashing_embed(keyphrases(UNRELATED))) < 0.3


def _vec(cos: float, dims: int = 64) -> list[float]:
    v = [0.0] * dims
    v[0], v[1] = cos, math.sqrt(max(0.0, 1 - cos * cos))
    return v


def test_broker_is_objective_aware():
    # a 0.50 topic overlap: enough WITHIN an objective, not across two different ones
    a = TopicSignal("alice", _vec(1.0), "Acme - Checkout")
    b_same = TopicSignal("bob", _vec(0.50), "Acme - Checkout")
    b_diff = TopicSignal("carol", _vec(0.50), "Globex - Runtime")

    br = CollaborationBroker()
    assert br.submit(a) == []
    assert br.submit(b_same), "same-objective 0.50 should match (bar 0.40)"

    br2 = CollaborationBroker()
    br2.submit(a)
    assert br2.submit(b_diff) == [], "cross-objective 0.50 should NOT match (bar 0.55)"
