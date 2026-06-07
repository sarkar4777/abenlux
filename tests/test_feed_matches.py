from abenlux.collaborate.broker import Match
from abenlux.developer.feed import FeedEntry, LocalSignalFeed
from abenlux.developer.matches import MatchStore
from abenlux.processing.waste import WasteSignal


def test_feed_appends_and_reads_recent(tmp_path):
    feed = LocalSignalFeed(tmp_path / "feed.jsonl")
    sig = WasteSignal(kind="retry_loop", severity="warn", similarity=0.95,
                      detail="near-identical", suggestion="add a failing test", recoverable_tokens=1200)
    feed.append_waste(sig, tool="aider", recoverable_usd=0.006)
    rows = feed.recent(10)
    assert len(rows) == 1
    assert rows[0]["kind"] == "retry_loop" and rows[0]["tool"] == "aider"
    assert rows[0]["recoverable_usd"] == 0.006


def test_feed_collab_entry(tmp_path):
    feed = LocalSignalFeed(tmp_path / "feed.jsonl")
    feed.append_collab(Match("a", "b", 0.9, "approval saga", "solved_reuse"), tool="claude-code")
    row = feed.recent(1)[0]
    assert row["kind"] == "collab_solved_reuse"
    assert "already solved" in row["line"]


def test_feed_trims_to_bound(tmp_path):
    feed = LocalSignalFeed(tmp_path / "feed.jsonl", max_entries=5)
    for i in range(20):
        feed.append(FeedEntry(ts=float(i), kind="routing_hint", severity="info", line=f"n{i}"))
    rows = feed.recent(100)
    assert len(rows) == 5
    assert rows[-1]["line"] == "n19"  # most recent kept


def test_match_store_is_owner_scoped(tmp_path):
    ms = MatchStore(tmp_path / "m.db")
    ms.record("alice", "bob", "topic-x", 0.9, "live_duplication", ts=1.0)
    ms.record("bob", "alice", "topic-x", 0.9, "live_duplication", ts=1.0)
    ms.record("carol", "dave", "topic-y", 0.8, "solved_reuse", ts=2.0)
    alice = ms.for_owner("alice")
    assert len(alice) == 1 and alice[0]["peer"] == "bob"
    # alice cannot see carol/dave's match - owner scoping is the privacy boundary
    assert all(r["peer"] != "dave" for r in alice)


def test_match_mutual_consent(tmp_path):
    ms = MatchStore(tmp_path / "m.db")
    ms.record("alice", "bob", "t", 0.9, "live_duplication", ts=1.0)
    assert ms.mutually_consented("alice", "bob") is False
    ms.record_consent("alice", "bob")
    assert ms.mutually_consented("alice", "bob") is False
    ms.record_consent("bob", "alice")
    assert ms.mutually_consented("alice", "bob") is True
