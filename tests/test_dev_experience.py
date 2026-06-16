"""the developer experience for a subscription tool behind the forward proxy: routing and caching reach
it the same as a key tool, and the win shows up in the developer's own feed so no extra ui is needed."""
import http.server
import json
import threading
from types import SimpleNamespace

import httpx

from abenlux.capture import forward_proxy
from abenlux.capture.forward_proxy import make_server
from abenlux.developer.feed import LocalSignalFeed
from abenlux.schema import DerivedRecord


def _settings(monkeypatch, *, route=None, exact_cache=False):
    # the real Settings is frozen, so swap the module reference for the proxy's reads
    ns = SimpleNamespace(compress=None, route=route, exact_cache=exact_cache, exact_cache_ttl_s=300.0)
    monkeypatch.setattr(forward_proxy, "SETTINGS", ns)


def _upstream():
    state = {"n": 0, "body": None}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("content-length", "0") or "0")
            state["body"] = self.rfile.read(n)
            state["n"] += 1
            resp = json.dumps({"id": "m", "type": "message", "role": "assistant",
                               "content": [{"type": "text", "text": "ok"}],
                               "usage": {"input_tokens": 10, "output_tokens": 2}}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


def _proxy(tmp_path):
    up, state = _upstream()
    mport = up.server_address[1]
    srv = make_server(port=0, ca_dir=str(tmp_path / "ca"), capture=False,
                      upstream_override={"api.anthropic.com": f"http://127.0.0.1:{mport}"})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ca = str(tmp_path / "ca" / "abenlux-ca.pem")
    return srv, up, state, srv.server_address[1], ca


def test_an_easy_call_from_a_subscription_tool_is_routed_to_a_cheaper_model(tmp_path, monkeypatch):
    _settings(monkeypatch, route="on")
    srv, up, state, port, ca = _proxy(tmp_path)
    try:
        with httpx.Client(proxy=f"http://127.0.0.1:{port}", verify=ca, timeout=15) as c:
            c.post("https://api.anthropic.com/v1/messages",
                   headers={"x-api-key": "sub-token", "anthropic-version": "2023-06-01"},
                   json={"model": "claude-opus-4-8", "max_tokens": 16,
                         "messages": [{"role": "user", "content": "rename the helper to apply_idempotency_key"}]})
        fwd = json.loads(state["body"])
        assert fwd["model"] == "claude-haiku-4-5"     # the easy call went to the cheaper model
    finally:
        srv.shutdown()
        up.shutdown()


def test_real_work_from_a_subscription_tool_stays_on_the_strong_model(tmp_path, monkeypatch):
    _settings(monkeypatch, route="on")
    srv, up, state, port, ca = _proxy(tmp_path)
    try:
        hard = "design a distributed consensus protocol with leader election and recovery " * 30
        with httpx.Client(proxy=f"http://127.0.0.1:{port}", verify=ca, timeout=15) as c:
            c.post("https://api.anthropic.com/v1/messages",
                   headers={"x-api-key": "sub-token", "anthropic-version": "2023-06-01"},
                   json={"model": "claude-opus-4-8", "max_tokens": 512,
                         "messages": [{"role": "user", "content": hard}]})
        assert json.loads(state["body"])["model"] == "claude-opus-4-8"
    finally:
        srv.shutdown()
        up.shutdown()


def test_an_identical_repeat_is_served_from_the_local_cache(tmp_path, monkeypatch):
    _settings(monkeypatch, exact_cache=True)
    forward_proxy._FP_CACHE.clear()
    srv, up, state, port, ca = _proxy(tmp_path)
    payload = {"model": "claude-haiku-4-5", "max_tokens": 8,
               "messages": [{"role": "user", "content": "what is two plus two"}]}
    try:
        with httpx.Client(proxy=f"http://127.0.0.1:{port}", verify=ca, timeout=15) as c:
            r1 = c.post("https://api.anthropic.com/v1/messages",
                        headers={"x-api-key": "sub-token", "anthropic-version": "2023-06-01"}, json=payload)
            r2 = c.post("https://api.anthropic.com/v1/messages",
                        headers={"x-api-key": "sub-token", "anthropic-version": "2023-06-01"}, json=payload)
        assert r1.json()["content"][0]["text"] == "ok"
        assert r2.json()["content"][0]["text"] == "ok"   # same answer
        assert state["n"] == 1                            # upstream was hit ONCE, the repeat was cached
    finally:
        srv.shutdown()
        up.shutdown()


def _surface(monkeypatch, tmp_path, rec):
    from abenlux.capture import gateway
    feed = LocalSignalFeed(tmp_path / "feed.jsonl")
    toasts = []
    monkeypatch.setattr(gateway, "_feed", feed)
    monkeypatch.setattr(gateway, "_toast", lambda kind, line: toasts.append((kind, line)))
    result = SimpleNamespace(record=rec, waste_signals=[])
    event = SimpleNamespace(work=SimpleNamespace(tool="claude-code"), request_model=rec.request_model)
    gateway._surface_to_developer(result, event)
    return feed.recent(10), toasts


def _rec(**kw):
    base = dict(event_id="e", ts=1.0, tier="tier2_gateway", provider="anthropic",
                actor_pseudonym=None, request_model="claude-opus-4-8", input_tokens=10, output_tokens=2,
                duplicate_history_tokens=0)
    base.update(kw)
    return DerivedRecord(**base)


def test_a_routing_win_reaches_the_developer_feed_so_no_ui_is_needed(tmp_path, monkeypatch):
    rec = _rec(route_target="claude-haiku-4-5", route_saved_usd=0.0123)
    entries, toasts = _surface(monkeypatch, tmp_path, rec)
    kinds = [e["kind"] for e in entries]
    assert "routing" in kinds
    assert any(k == "routing" for k, _ in toasts)         # a desktop toast fired too


def test_a_cache_hit_reaches_the_developer_feed(tmp_path, monkeypatch):
    rec = _rec(served_from_cache=True)
    entries, toasts = _surface(monkeypatch, tmp_path, rec)
    assert "cache_hit" in [e["kind"] for e in entries]
    assert any(k == "cache_hit" for k, _ in toasts)

