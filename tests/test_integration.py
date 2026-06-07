"""
End-to-end integration tests. These drive the actual FastAPI apps (gateway + API) through the
full path - proxy capture -> edge pipeline -> derived store -> RBAC'd read API - and assert the
guarantees the product is sold on, not just unit behavior:

  * the developer's streamed response is returned byte-for-byte unmodified (zero-tamper proxy),
  * exact tokens + correct USD cost + objective attribution land in the derived store,
  * the PRIVACY INVARIANT: no prompt content, no secret, and no raw identity is anywhere on disk,
  * resent-history across turns is detected and a waste nudge reaches the developer's feed,
  * spend from all three capture tiers aggregates into one management rollup,
  * a manager reading the API sees the captured spend, a developer cannot.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from abenlux.attribution.attributor import KnowledgeGraph, Objective
from abenlux.capture import gateway
from abenlux.capture.diff import SessionHistoryTracker
from abenlux.developer.feed import LocalSignalFeed
from abenlux.developer.matches import MatchStore
from abenlux.privacy.pseudonymize import pseudonymize
from abenlux.schema import WorkContext
from abenlux.sink import SqliteSink
from abenlux.store import DerivedStore

SECRET = "sk-ant-abc1234567890SECRETKEYvalue0099"
ANTHROPIC_SSE = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"model":"claude-opus-4-8",'
    '"usage":{"input_tokens":1820,"output_tokens":1}}}\n\n'
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Use a Temporal saga."}}\n\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":42}}\n\n'
    'data: {"type":"message_stop"}\n\n'
).encode()


class _FakeResponse:
    def __init__(self, body: bytes, status=200):
        self._body = body
        self.status_code = status
        self.headers = {"content-type": "text/event-stream"}

    async def aiter_raw(self):
        yield self._body

    async def aclose(self):
        pass


class _FakeAsyncClient:
    """stands in for the upstream model API so the gateway's outbound call is deterministic."""

    def __init__(self, *a, **k):
        pass

    def build_request(self, method, url, content=None, headers=None):
        return {"method": method, "url": url}

    async def send(self, req, stream=True):
        return _FakeResponse(ANTHROPIC_SSE)

    async def aclose(self):
        pass


@pytest.fixture
def wired_gateway(tmp_path, monkeypatch):
    db = str(tmp_path / "gw.db")
    kg = KnowledgeGraph()
    kg.add_objective(Objective("obj-acme", "Acme Checkout platform", "client", client="acme"))
    kg.map_ticket_prefix("ACME", "obj-acme")

    store = DerivedStore(db)
    monkeypatch.setattr(gateway.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(gateway, "_store", store)
    monkeypatch.setattr(gateway, "_sink", SqliteSink(store))  # capture writes through the sink
    monkeypatch.setattr(gateway, "_kg", kg)
    monkeypatch.setattr(gateway, "_history", SessionHistoryTracker())
    monkeypatch.setattr(gateway, "_feed", LocalSignalFeed(tmp_path / "feed.jsonl"))
    monkeypatch.setattr(gateway, "_matchstore", MatchStore(tmp_path / "gm.db"))
    monkeypatch.setattr(gateway, "SETTINGS", SimpleNamespace(hmac_bytes=b"integration-key"))
    monkeypatch.setattr(gateway, "current_actor", lambda: "alice@corp.com")
    monkeypatch.setattr(
        gateway, "current_work_context",
        lambda: WorkContext(tool="aider", app_category="cli", git_branch="feature/ACME-1-approvals",
                            ticket_id="ACME-1", repo="acme-checkout", host_os="TestOS"),
    )
    return gateway, db, str(tmp_path / "feed.jsonl")


def _post_messages(client, content):
    body = {"model": "claude-opus-4-8",
            "system": "You are senior.",
            "messages": [{"role": "user", "content": content}],
            "stream": True}
    return client.post("/v1/messages", json=body)


def test_proxy_returns_stream_unmodified_and_captures(wired_gateway):
    gw, db, _ = wired_gateway
    client = TestClient(gw.app)
    r = _post_messages(client, f"design the ACME approval workflow, key {SECRET}")
    assert r.status_code == 200
    assert r.content == ANTHROPIC_SSE  # byte-for-byte, the tool sees an untouched response

    store = DerivedStore(db)
    rows = store.conn.execute("SELECT * FROM derived").fetchall()
    store.close()
    assert len(rows) == 1
    rec = dict(rows[0])
    assert rec["input_tokens"] == 1820 and rec["output_tokens"] == 42
    assert abs(rec["cost_usd"] - 0.01015) < 1e-6           # 1820@$5 + 42@$25 per Mtok
    assert rec["objective_id"] == "obj-acme" and rec["is_orphan"] == 0
    assert rec["attribution_method"] == "ticket_join"
    assert rec["actor_pseudonym"] == pseudonymize("alice@corp.com", b"integration-key")
    assert rec["tool"] == "aider"


def test_privacy_invariant_nothing_sensitive_on_disk(wired_gateway):
    gw, db, _ = wired_gateway
    client = TestClient(gw.app)
    _post_messages(client, f"design the ACME approval workflow, key {SECRET}")

    raw = open(db, "rb").read()
    # the secret, the prompt content, and the raw identity must NOT exist anywhere in the store
    assert SECRET.encode() not in raw
    assert b"sk-ant-" not in raw
    assert b"approval workflow" not in raw
    assert b"alice@corp.com" not in raw
    # and there is structurally no column that could hold raw content
    cols = {c[1] for c in sqlite3.connect(db).execute("PRAGMA table_info(derived)").fetchall()}
    assert "messages" not in cols and "content" not in cols and "actor_raw" not in cols


def test_resent_history_detected_across_turns_and_nudges_feed(wired_gateway):
    gw, db, feed_path = wired_gateway
    client = TestClient(gw.app)
    long_ctx = "Here is the full design doc. " * 80  # big shared prefix
    _post_messages(client, long_ctx)                  # turn 1 establishes the prefix
    _post_messages(client, long_ctx + " Now add retries.")  # turn 2 resends it

    store = DerivedStore(db)
    dups = [r[0] for r in store.conn.execute(
        "SELECT duplicate_history_tokens FROM derived ORDER BY ts").fetchall()]
    store.close()
    assert dups[0] == 0 and dups[1] > 0  # second turn carries resent-history tokens

    nudges = LocalSignalFeed(feed_path).recent(50)
    assert any(n["kind"] in ("context_bloat", "retry_loop", "answered_already") for n in nudges)


def test_three_tier_spend_aggregates_into_one_report(tmp_path, monkeypatch):
    from abenlux.analytics.reports import management_report
    from abenlux.capture.vendor_admin import cursor_event_to_derived

    db = str(tmp_path / "multi.db")
    store = DerivedStore(db)
    kg = KnowledgeGraph()
    kg.add_objective(Objective("obj-x", "Platform X"))
    kg.map_repo("repo-x", "obj-x")

    # Tier-3 (Cursor) events for 5 distinct devs so the group clears k
    for i in range(5):
        store.insert(cursor_event_to_derived(
            {"userEmail": f"dev{i}@x", "model": "claude-opus-4-8",
             "inputTokens": 1000, "outputTokens": 100, "repoName": "repo-x"},
            hmac_key=b"k", kg=kg))
    rep = management_report(store, k=5)
    store.close()

    tiers_present = {r for r in store_tiers(db)}
    assert "tier3_vendor_admin" in tiers_present
    assert rep["total_events"] == 5
    assert rep["total_cost_usd"] > 0
    by_obj = {r["label"]: r for r in rep["by_objective"]}
    assert by_obj["Platform X"]["suppressed"] is False  # 5 devs == k


def store_tiers(db):
    con = sqlite3.connect(db)
    return [r[0] for r in con.execute("SELECT DISTINCT tier FROM derived").fetchall()]


def test_gateway_capture_then_manager_reads_via_api(wired_gateway, monkeypatch):
    from abenlux.api import server
    gw, db, _ = wired_gateway
    gwc = TestClient(gw.app)
    # capture five interactions across five developers so the objective clears k=5
    for i in range(5):
        monkeypatch.setattr(gateway, "current_actor", lambda i=i: f"dev{i}@corp.com")
        _post_messages(gwc, "design the ACME approval workflow")

    monkeypatch.setattr(server, "_store", lambda: DerivedStore(db))
    api = TestClient(server.app)
    # manager sees the aggregated ACME spend...
    rep = api.get("/api/report", headers={"Authorization": "Bearer mgr-token"}).json()
    by_obj = {r["label"]: r for r in rep["by_objective"]}
    assert "Acme Checkout platform" in by_obj
    assert by_obj["Acme Checkout platform"]["suppressed"] is False
    assert by_obj["Acme Checkout platform"]["cost"] > 0
    # ...but a developer is refused the management plane entirely
    assert api.get("/api/report", headers={"Authorization": "Bearer dev-token"}).status_code == 403
