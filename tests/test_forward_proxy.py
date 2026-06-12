"""The forward TLS-terminating proxy. It must present a trusted certificate for a model API host,
compress the request at the interception point before forwarding, return the answer, and pass any other
host straight through without reading it. This is the path that works for a subscription tool, where a
base url override does not."""
import http.server
import json
import socket
import threading

import httpx
import pytest

from abenlux.capture.forward_proxy import LocalCA, make_server


def _echo_upstream():
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("content-length", "0") or "0")
            seen["body"] = self.rfile.read(n)
            resp = json.dumps({"id": "msg", "type": "message", "role": "assistant",
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
    return srv, seen


def test_ca_mints_a_loadable_leaf_certificate(tmp_path):
    import ssl
    ca = LocalCA(tmp_path / "ca")
    assert ca.cert_path.exists()
    ctx = ca.context_for("api.anthropic.com")          # a usable server context for that host
    assert isinstance(ctx, ssl.SSLContext)
    assert ca.context_for("api.anthropic.com") is ctx  # cached


def test_forward_proxy_terminates_tls_compresses_and_forwards(tmp_path):
    up, seen = _echo_upstream()
    mport = up.server_address[1]
    srv = make_server(port=0, ca_dir=str(tmp_path / "ca"), capture=False,
                      upstream_override={"api.anthropic.com": f"http://127.0.0.1:{mport}"})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    pport = srv.server_address[1]
    try:
        big = "You are a senior engineer. Follow the house style. " * 80   # over the cache-breakpoint guard
        with httpx.Client(proxy=f"http://127.0.0.1:{pport}",
                          verify=str(tmp_path / "ca" / "abenlux-ca.pem"), timeout=15) as c:
            r = c.post("https://api.anthropic.com/v1/messages",
                       headers={"x-api-key": "secret-key", "anthropic-version": "2023-06-01"},
                       json={"model": "claude-haiku-4-5", "max_tokens": 8, "system": big,
                             "messages": [{"role": "user", "content": "hi"}]})
        # the tool gets a normal answer back, proxied through our own trusted certificate
        assert r.status_code == 200 and r.json()["content"][0]["text"] == "ok"
        # compression happened AT the proxy: the steady system prompt was marked for caching before forward
        fwd = json.loads(seen["body"])
        assert isinstance(fwd["system"], list) and fwd["system"][-1].get("cache_control")
    finally:
        srv.shutdown()
        up.shutdown()


def test_forward_proxy_passes_non_model_traffic_through_unread(tmp_path):
    # a tiny echo server on a NON model host. the proxy must tunnel to it without terminating TLS.
    echo = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    echo.bind(("127.0.0.1", 0))
    echo.listen(1)
    eport = echo.getsockname()[1]

    def serve_echo():
        conn, _ = echo.accept()
        data = conn.recv(1024)
        conn.sendall(b"echo:" + data)
        conn.close()
    threading.Thread(target=serve_echo, daemon=True).start()

    srv = make_server(port=0, ca_dir=str(tmp_path / "ca"), capture=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    pport = srv.server_address[1]
    try:
        s = socket.create_connection(("127.0.0.1", pport), timeout=5)
        s.sendall(f"CONNECT 127.0.0.1:{eport} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        assert b"200" in s.recv(1024)                  # tunnel established
        s.sendall(b"hello raw bytes")
        assert s.recv(1024) == b"echo:hello raw bytes"  # bytes flowed straight through, never decrypted
        s.close()
    finally:
        srv.shutdown()
        echo.close()


@pytest.mark.parametrize("host,is_ai", [("api.anthropic.com", True), ("api.openai.com", True),
                                        ("generativelanguage.googleapis.com", True),
                                        ("my-res.openai.azure.com", True),
                                        ("github.com", False), ("claude.ai", False)])
def test_only_model_hosts_are_intercepted(host, is_ai):
    from abenlux.capture.forward_proxy import _is_ai_host
    assert _is_ai_host(host) is is_ai
