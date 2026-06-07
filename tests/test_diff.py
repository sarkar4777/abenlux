from abenlux.capture.diff import SessionHistoryTracker, unchanged_prefix_chars
from abenlux.schema import Message


def _msgs(*pairs):
    return [Message(role=r, content=c) for r, c in pairs]


def test_unchanged_prefix_counts_only_leading_identical_run():
    prev = _msgs(("user", "a"), ("assistant", "bb"), ("user", "ccc"))
    curr = _msgs(("user", "a"), ("assistant", "bb"), ("user", "different"))
    assert unchanged_prefix_chars(prev, curr) == 1 + 2  # 'a' + 'bb', stops at divergence


def test_tracker_reports_resent_history_then_updates_baseline():
    t = SessionHistoryTracker()
    turn1 = _msgs(("system", "x" * 40), ("user", "first"))
    assert t.duplicate_history_tokens("actor:anthropic", turn1) == 0  # nothing prior
    # turn 2 resends the whole turn-1 prefix unchanged
    turn2 = turn1 + _msgs(("assistant", "answer"), ("user", "second"))
    dup = t.duplicate_history_tokens("actor:anthropic", turn2)
    assert dup > 0  # the 40-char system + 'first' were detected as resent


def test_tracker_bounded_eviction():
    t = SessionHistoryTracker(max_sessions=2)
    for i in range(5):
        t.duplicate_history_tokens(f"k{i}", _msgs(("user", "q")))
    assert len(t._prev) <= 2


def test_sessions_isolated_by_key():
    t = SessionHistoryTracker()
    t.duplicate_history_tokens("a:anthropic", _msgs(("user", "shared" * 10)))
    # different session key -> no prior baseline -> no false bloat
    assert t.duplicate_history_tokens("b:anthropic", _msgs(("user", "shared" * 10))) == 0
