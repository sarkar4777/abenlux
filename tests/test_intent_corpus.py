"""
Thorough intent (work-type) accuracy corpus. This is the label management tracks (net-new vs
maintenance), so it has to be right across the full range of how developers actually phrase things:
terse, verbose, polite, indirect, multi-sentence, with pasted code/stack traces, and multi-part.

Each case runs the REAL edge path: salient-intent extraction (handles long/noisy prompts) then the
deterministic pattern classifier. We assert high overall accuracy and print a per-category breakdown.
"""
from abenlux.attribution.attributor import classify_from_prompt, NET_NEW
from abenlux.salience import salient_intent

_BIG_TRACE = "\n".join(f'  File "mod{i}.py", line {i*7}, in handler\n    do_thing_{i}(x)' for i in range(40))
_BIG_CODE = "\n".join(f"def helper_{i}(a, b): return a * {i} + b  # noqa" for i in range(50))

# (prompt, expected_work_type, complexity_tag)
CORPUS = [
    # --- feature (net-new) ---
    ("Add a dark mode toggle to the settings page", "feature", "terse"),
    ("Implement OAuth login with Google for the web app", "feature", "terse"),
    ("Build a new REST endpoint that exports invoices as CSV", "feature", "terse"),
    ("create a react component for the pricing table", "feature", "lowercase"),
    ("wire up push notifications for the mobile onboarding", "feature", "terse"),
    ("Could you help me add support for SAML single sign-on? It's a new requirement from the customer.", "feature", "polite"),
    ("scaffold a new microservice for the recommendation engine", "feature", "terse"),
    (f"Here is the module for context:\n```python\n{_BIG_CODE}\n```\nAdd an idempotency key to the checkout webhook so retries don't double charge.", "feature", "long+code"),
    ("integrate Stripe so we can charge customers", "feature", "terse"),
    # --- fix (maintenance) ---
    ("Fix the null pointer exception when the cart is empty", "fix", "terse"),
    ("The login button doesn't work on Safari, can you figure out why", "fix", "indirect"),
    ("tax rounding is off by a cent on multi-currency carts, find and fix it", "fix", "terse"),
    ("users are getting a 500 error on checkout, please debug", "fix", "polite"),
    (f"why is this throwing?\n{_BIG_TRACE}\nIt crashes every time someone uploads a PDF.", "fix", "long+trace"),
    ("the nightly job keeps failing with a timeout, sort it out", "fix", "terse"),
    ("there's a regression in the export, it was working last week", "fix", "indirect"),
    # --- refactor (maintenance) ---
    ("Refactor the payment service to use dependency injection", "refactor", "terse"),
    ("clean up the duplicated validation logic across the controllers", "refactor", "terse"),
    ("extract the retry logic into a reusable helper function", "refactor", "terse"),
    ("rename getUserData to fetchUserProfile everywhere it's used", "refactor", "terse"),
    ("this module is a mess, please restructure it so it's easier to follow", "refactor", "polite"),
    ("dedupe the three copies of the date formatting code", "refactor", "terse"),
    # --- perf (maintenance) ---
    ("The dashboard query is too slow, optimize it", "perf", "terse"),
    ("reduce memory allocations in the request hot path", "perf", "terse"),
    ("speed up the image upload, it takes 8 seconds right now", "perf", "terse"),
    ("there's a latency bottleneck in the search endpoint, profile and improve it", "perf", "verbose"),
    # --- exploration (net-new) ---
    ("How should I architect a multi-tenant billing system?", "exploration", "question"),
    ("what's the best library for PDF generation in Python?", "exploration", "question"),
    ("compare Temporal versus a custom saga for the approval workflow, with trade-offs", "exploration", "verbose"),
    ("prototype a vector-search ranker for the marketplace results", "exploration", "terse"),
    ("which approach should we take for real-time updates, websockets or SSE?", "exploration", "question"),
    ("spike on whether we can move the queue to Kafka", "exploration", "terse"),
    # --- chore (maintenance) ---
    ("bump the dependencies and fix the lockfile conflicts", "chore", "terse"),
    ("set up CI for the new repository", "chore", "terse"),
    ("update the Dockerfile to node 22 and rebuild the image", "chore", "terse"),
    ("cut a release and tag it v2.3.0", "chore", "terse"),
    # --- docs (maintenance) ---
    ("write a README for the auth module", "docs", "terse"),
    ("document this function with a proper docstring", "docs", "terse"),
    ("add comments explaining how the rate limiter works", "docs", "terse"),
    # --- test (maintenance) ---
    ("write unit tests for the cart service", "test", "terse"),
    ("add integration tests for the checkout flow", "test", "terse"),
    ("increase test coverage on the input validators", "test", "terse"),
    ("we need pytest cases for the new pricing logic", "test", "terse"),

    # ---- harder: symptom-style bugs with no explicit "fix" verb ----
    ("this returns the wrong total when a discount is applied", "fix", "symptom"),
    ("NaN keeps showing up in the price field on some orders", "fix", "symptom"),
    ("after the last deploy, search just stopped working", "fix", "symptom"),
    ("the totals are off by a cent for EUR carts", "fix", "symptom"),
    ("there's a race condition in the worker pool", "fix", "symptom"),
    ("the test suite is flaky on CI, fails maybe 1 in 5 runs", "fix", "symptom"),
    # ---- harder: feature intents phrased indirectly ----
    ("let users export their data as JSON from the profile page", "feature", "indirect"),
    ("make it possible to filter orders by a date range", "feature", "indirect"),
    ("I need a CSV importer for bulk user uploads", "feature", "indirect"),
    ("give admins the ability to impersonate a user for support", "feature", "indirect"),
    ("we should allow people to pay with Apple Pay", "feature", "indirect"),
    ("set up a new GraphQL endpoint for the mobile team", "feature", "terse"),
    # ---- verbose / rambling ----
    ("So I've been thinking about this for a while and I reckon the cleanest move is to pull the "
     "tangled notification logic out of the controller and put it behind a small interface so we can "
     "test it in isolation", "refactor", "rambling"),
    ("Honestly the checkout page feels sluggish, customers complain it takes forever, can we make the "
     "whole thing load noticeably faster please", "perf", "rambling"),
    ("Quick question for the team: for the new event pipeline, would you go with Kafka or just "
     "Postgres LISTEN/NOTIFY? Keen to understand the trade-offs before we commit.", "exploration", "rambling"),
    # ---- multi-part: dominant intent should win ----
    ("Refactor the auth module and then update its README", "refactor", "multi"),
    ("Optimize the slow report query and add a regression test for it", "perf", "multi"),
    ("Build the new referral feature end to end, including the API and the UI", "feature", "multi"),
    # ---- long + code/data noise around a clear ask ----
    (f"```python\n{_BIG_CODE}\n```\nRefactor all of this into smaller, well-named functions.", "refactor", "long+code"),
    (f"logs:\n{_BIG_TRACE}\nThe upload endpoint is throwing on large files, please fix it.", "fix", "long+trace"),
    ("context dump:\n```json\n{'a':1,'b':[1,2,3,4,5,6,7,8,9]}\n```\nWrite a docstring for the parse function.", "docs", "long+data"),
    # ---- chore variety ----
    ("upgrade React from 17 to 18 across the app", "chore", "terse"),
    ("pin the version of the postgres client", "chore", "terse"),
    ("the github actions workflow is using a deprecated runner, update it", "chore", "terse"),
    # ---- exploration variety ----
    ("evaluate whether we should adopt pnpm over npm", "exploration", "terse"),
    ("what are the options for rate limiting at the edge?", "exploration", "question"),
]


def _classify(prompt: str) -> str:
    return classify_from_prompt(salient_intent(prompt))


def test_intent_corpus_accuracy():
    by_cat: dict[str, list[bool]] = {}
    misses = []
    for prompt, expected, tag in CORPUS:
        got = _classify(prompt)
        ok = got == expected
        by_cat.setdefault(expected, []).append(ok)
        if not ok:
            misses.append((expected, got, tag, prompt[:60]))

    total = sum(len(v) for v in by_cat.values())
    correct = sum(sum(v) for v in by_cat.values())
    print(f"\nintent accuracy: {correct}/{total} = {correct/total*100:.1f}%")
    for cat in sorted(by_cat):
        v = by_cat[cat]
        print(f"  {cat:12} {sum(v)}/{len(v)}")
    if misses:
        print(" misses:")
        for exp, got, tag, p in misses:
            print(f"   [{tag}] expected {exp}, got {got}: {p!r}")

    # net-new vs maintenance is the split management reports - it must be near-perfect
    nn_mn_ok = sum(1 for prompt, exp, _ in CORPUS
                   if (_classify(prompt) in NET_NEW) == (exp in NET_NEW))
    print(f" net-new/maintenance bucket accuracy: {nn_mn_ok}/{total} = {nn_mn_ok/total*100:.1f}%")

    assert correct / total >= 0.90, f"intent accuracy {correct/total:.0%} below 90%"
    assert nn_mn_ok / total >= 0.93, f"net-new/maintenance accuracy {nn_mn_ok/total:.0%} below 93%"


def test_salience_helps_long_prompt_intent():
    # a new-feature ask buried under a pasted error/stack-trace block must NOT be misread as a fix
    p = ("```\nTraceback (most recent call last):\n" + _BIG_TRACE +
         "\nException: payment failed, broken, crash, regression\n```\n"
         "Build a brand new analytics dashboard page with charts for daily active users.")
    assert classify_from_prompt(p) == "fix"                       # raw: the pasted error dominates
    assert classify_from_prompt(salient_intent(p)) == "feature"   # salient drops the block, real ask wins
