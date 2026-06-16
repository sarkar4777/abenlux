"""
The forward proxy. This is the second way to put the agent in front of a tool, and it is the one that
works no matter how the tool signs in.

A tool that points its base url at the agent is one way, but a tool that logs in with a subscription
cannot be redirected like that. So instead the tool routes through this as an ordinary HTTPS proxy. The
proxy terminates the TLS using a small local certificate authority that the agent manages, reads the
request, redacts and measures it on the device, compresses it, then forwards it to the real provider
with the tool's own credentials untouched. The provider sees a valid, smaller request. The tool sees a
normal answer. Only a content-free record ever leaves the machine, exactly as before.

Two rules keep it safe. It only terminates TLS for the known model API hosts, so the developer's other
traffic, the browser, package installs, anything else, is passed straight through without being read.
And every capture and compression step is wrapped so a failure can never break the developer's call.

Because compression happens right here at the interception point, it applies to a subscription tool and
a key tool alike, so a separate tool-output compressor is no longer required for the savings to work.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import socket
import socketserver
import ssl
import threading
import time
from collections import OrderedDict
from pathlib import Path

import httpx

from abenlux.schema import Provider
from abenlux.settings import SETTINGS

# exact-match cache for the forward proxy, mirroring the gateway. a byte-identical non-streamed repeat is
# served from here so the upstream call is avoided. the cached response stays on the device, never leaves.
_FP_CACHE: "OrderedDict[str, tuple]" = OrderedDict()
_FP_CACHE_MAX = 256
_FP_LOCK = threading.Lock()


def _fp_key(host: str, path: str, body: bytes, actor: str) -> str:
    h = hashlib.blake2b(body, digest_size=16)
    h.update(f"\x00{host}\x00{path}\x00{actor}".encode())
    return h.hexdigest()


def _fp_get(key: str):
    with _FP_LOCK:
        ent = _FP_CACHE.get(key)
        if ent is None:
            return None
        if time.time() > ent[0]:
            _FP_CACHE.pop(key, None)
            return None
        _FP_CACHE.move_to_end(key)
        return ent[1]


def _fp_put(key: str, value: tuple) -> None:
    ttl = getattr(SETTINGS, "exact_cache_ttl_s", 300.0)
    with _FP_LOCK:
        _FP_CACHE[key] = (time.time() + ttl, value)
        _FP_CACHE.move_to_end(key)
        while len(_FP_CACHE) > _FP_CACHE_MAX:
            _FP_CACHE.popitem(last=False)

# the model API hosts we terminate TLS for. everything else is tunnelled straight through, unread.
AI_HOSTS: dict[str, Provider] = {
    "api.anthropic.com": Provider.ANTHROPIC,
    "api.openai.com": Provider.OPENAI,
    "generativelanguage.googleapis.com": Provider.GOOGLE,
}


def _is_ai_host(host: str) -> bool:
    return host in AI_HOSTS or host.endswith(".openai.azure.com")


def _provider_for(host: str) -> Provider:
    if host.endswith(".openai.azure.com"):
        return Provider.OPENAI
    return AI_HOSTS.get(host, Provider.OPENAI)


# ----------------------------- the local certificate authority -----------------------------

class LocalCA:
    """A tiny certificate authority kept on the developer's own machine. It signs a short-lived leaf
    certificate for each model API host on demand, so the proxy can present a trusted certificate for
    that host. The developer trusts this one CA once and nothing else changes."""

    def __init__(self, ca_dir: str | Path | None = None):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        self._x509, self._hashes, self._ser, self._rsa, self._oid = x509, hashes, serialization, rsa, NameOID
        self.dir = Path(ca_dir or os.getenv("ABEN_CA_DIR") or (Path.home() / ".abenlux" / "ca"))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cert_path = self.dir / "abenlux-ca.pem"
        self.key_path = self.dir / "abenlux-ca.key"
        self._leaf_lock = threading.Lock()
        self._leaf_ctx: dict[str, ssl.SSLContext] = {}
        self._load_or_make_ca()

    def _load_or_make_ca(self) -> None:
        x509, hashes, ser, rsa, oid = self._x509, self._hashes, self._ser, self._rsa, self._oid
        if self.cert_path.exists() and self.key_path.exists():
            self.ca_key = ser.load_pem_private_key(self.key_path.read_bytes(), password=None)
            self.ca_cert = x509.load_pem_x509_certificate(self.cert_path.read_bytes())
            return
        self.ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(oid.COMMON_NAME, "Abenlux Local CA"),
                          x509.NameAttribute(oid.ORGANIZATION_NAME, "Abenlux")])
        now = datetime.datetime.now(datetime.timezone.utc)
        self.ca_cert = (x509.CertificateBuilder()
                        .subject_name(name).issuer_name(name).public_key(self.ca_key.public_key())
                        .serial_number(x509.random_serial_number())
                        .not_valid_before(now - datetime.timedelta(days=1))
                        .not_valid_after(now + datetime.timedelta(days=3650))
                        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True,
                                                     crl_sign=True, key_encipherment=False,
                                                     content_commitment=False, data_encipherment=False,
                                                     key_agreement=False, encipher_only=False,
                                                     decipher_only=False), critical=True)
                        # a Subject Key Identifier on the CA is what a leaf's Authority Key Identifier
                        # points back to; modern OpenSSL (Python 3.13) rejects the chain without it.
                        .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.ca_key.public_key()),
                                       critical=False)
                        .sign(self.ca_key, hashes.SHA256()))
        self.key_path.write_bytes(self.ca_key.private_bytes(
            ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption()))
        self.cert_path.write_bytes(self.ca_cert.public_bytes(ser.Encoding.PEM))
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            pass

    def context_for(self, hostname: str) -> ssl.SSLContext:
        with self._leaf_lock:
            ctx = self._leaf_ctx.get(hostname)
            if ctx is not None:
                return ctx
            # v2 leaf files carry the key identifiers / EKU that strict verifiers require; the suffix
            # bump makes an upgraded install re-mint rather than reuse a stale pre-v2 leaf.
            leaf = self.dir / f"leaf-{hostname}-v2.pem"
            if not leaf.exists():
                self._mint_leaf(hostname, leaf)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=str(leaf))
            self._leaf_ctx[hostname] = ctx
            return ctx

    def _mint_leaf(self, hostname: str, out: Path) -> None:
        x509, hashes, ser, rsa, oid = self._x509, self._hashes, self._ser, self._rsa, self._oid
        from cryptography.x509.oid import ExtendedKeyUsageOID
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(oid.COMMON_NAME, hostname)]))
                .issuer_name(self.ca_cert.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=825))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                # the key identifiers are mandatory for a strict (Python 3.13 / OpenSSL 3) verifier: the
                # leaf's Authority Key Identifier must point back to the CA, and serverAuth EKU is what
                # macOS in particular insists on for a TLS server certificate.
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.ca_key.public_key()),
                               critical=False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .sign(self.ca_key, hashes.SHA256()))
        out.write_bytes(cert.public_bytes(ser.Encoding.PEM) + key.private_bytes(
            ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption()))


# ----------------------------- request reading and forwarding -----------------------------

_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade", "content-length", "host", "accept-encoding"}


def _read_http_request(sock) -> tuple[str, str, dict, bytes] | None:
    # read one HTTP request off a (decrypted) socket: the request line, the headers, and the body.
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            return None
        buf += chunk
        if len(buf) > 8 * 1024 * 1024:
            return None
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    try:
        method, path, _ = lines[0].decode("latin1").split(" ", 2)
    except ValueError:
        return None
    headers = {}
    for line in lines[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.decode("latin1").strip().lower()] = v.decode("latin1").strip()
    body = rest
    n = int(headers.get("content-length", "0") or "0")
    if n:
        while len(body) < n:
            chunk = sock.recv(65536)
            if not chunk:
                break
            body += chunk
    elif "chunked" in headers.get("transfer-encoding", "").lower():
        # node and claude code send the body chunked with no content-length. read to the final zero
        # length chunk then de-chunk, so we can parse it and forward it with a real length.
        while b"0\r\n\r\n" not in body:
            chunk = sock.recv(65536)
            if not chunk:
                break
            body += chunk
            if len(body) > 16 * 1024 * 1024:
                break
        body = _dechunk(body)
    return method, path, headers, body


def _dechunk(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        j = data.find(b"\r\n", i)
        if j == -1:
            break
        try:
            size = int(data[i:j].split(b";", 1)[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        start = j + 2
        out += data[start:start + size]
        i = start + size + 2          # skip the chunk data and its trailing CRLF
    return bytes(out)


def _provider_path_kind(provider: Provider, path: str) -> bool:
    # the OpenAI Responses API (what Codex speaks) needs a different adapter
    return provider == Provider.OPENAI and path.startswith("/v1/responses")


def _is_model_path(path: str) -> bool:
    # the real model endpoints, not the telemetry, mcp registry and oauth calls a tool also makes
    p = path.split("?", 1)[0].lower()
    return (p == "/v1/messages" or p == "/v1/chat/completions" or p == "/v1/responses"
            or (p.startswith("/v1beta/models/") and "generatecontent" in p)
            or (p.startswith("/openai/deployments/") and p.endswith("/chat/completions")))


class _Forwarder:
    """Forwards one intercepted request to the real provider, streams the answer back to the tool, and
    hands the content-free capture to the gateway pipeline in the background. Reused across requests."""

    def __init__(self, upstream_override: dict | None = None, capture: bool = True):
        self.upstream = upstream_override or {}
        self.capture = capture
        self._client = httpx.Client(timeout=httpx.Timeout(None, connect=15.0, write=30.0, pool=15.0))

    def _base(self, host: str) -> str:
        return self.upstream.get(host) or f"https://{host}"

    def handle(self, tls, host: str) -> None:
        import json as _json
        req = _read_http_request(tls)
        if req is None:
            return
        method, path, headers, body = req
        provider = _provider_for(host)
        response_api = _provider_path_kind(provider, path)
        # a tool hits many non-model endpoints on the same host (telemetry, mcp registry, oauth). only the
        # model endpoints are compressed, routed, cached and captured, the rest are forwarded untouched.
        is_model = _is_model_path(path)
        do_capture = self.capture and is_model
        streamed = b'"stream":true' in body.replace(b" ", b"") or "streamgeneratecontent" in path.lower() \
            or "alt=sse" in path.lower()
        try:
            req_json = _json.loads(body) if body else {}
        except (ValueError, TypeError):
            req_json = {}

        # compression at the interception point. defensive: any failure keeps the original body, so the
        # developer's call can never break, and it runs for a subscription tool and a key tool alike.
        compress_info = {"saved": 0, "applied": "", "cached": False, "per_strategy": {}}
        out_body = body
        try:
            from abenlux.compress import compress_request, enabled_strategies
            strats = enabled_strategies(getattr(SETTINGS, "compress", None))
            if strats and isinstance(req_json, dict) and is_model:
                cres = compress_request(req_json, provider.value, strats)
                if cres.applied:
                    req_json = cres.body
                    out_body = _json.dumps(cres.body).encode()
                    compress_info.update(saved=cres.saved_tokens, applied=",".join(cres.applied),
                                         per_strategy=cres.per_strategy)
        except Exception:
            pass

        # model routing at the interception point, so a subscription tool gets it too
        route_info = None
        try:
            _mode = getattr(SETTINGS, "route", None)
            if _mode in ("on", "shadow") and isinstance(req_json, dict) and is_model:
                from abenlux.route import decide
                _d = decide(req_json, provider.value)
                if _d.target:
                    route_info = {"original": _d.original, "target": _d.target, "mode": _mode}
                    if _mode == "on":
                        req_json["model"] = _d.target
                        out_body = _json.dumps(req_json).encode()
        except Exception:
            pass

        # abenlux-internal headers (a wrapper may stamp the developer, tool, and branch) are read for
        # attribution and then DROPPED, so they never reach the provider.
        ov = {"tool": headers.get("x-aben-tool") or "proxy", "actor": headers.get("x-aben-actor"),
              "branch": headers.get("x-aben-branch"), "repo": headers.get("x-aben-repo"),
              "ticket": headers.get("x-aben-ticket")}
        fwd = {k: v for k, v in headers.items() if k not in _HOP and not k.startswith("x-aben-")}
        fwd["accept-encoding"] = "identity"        # we tee the body, so ask the provider not to compress
        url = self._base(host) + path

        # exact-match cache. a byte-identical non-streamed repeat is served locally, upstream call avoided
        _actor = ov.get("actor") or "local"
        cache_key = (_fp_key(host, path, out_body, _actor)
                     if getattr(SETTINGS, "exact_cache", False) and not streamed and is_model else None)
        if cache_key is not None:
            hit = _fp_get(cache_key)
            if hit is not None:
                status, reason, rhdr, cbody = hit
                try:
                    blob = (f"HTTP/1.1 {status} {reason}\r\n"
                            + "".join(f"{k}: {v}\r\n" for k, v in rhdr.items()) + "\r\n")
                    tls.sendall(blob.encode("latin1"))
                    tls.sendall(cbody)
                except OSError:
                    return
                if do_capture:
                    info = dict(compress_info, cached=True)
                    self._cap_async(provider, req_json, cbody, False, 0.0, ov, response_api, info, route_info)
                return

        captured = bytearray()
        started = time.perf_counter()
        try:
            with self._client.stream(method, url, content=out_body, headers=fwd) as up:
                status, reason = up.status_code, up.reason_phrase
                rhdr = {k: v for k, v in up.headers.items() if k.lower() not in _HOP}
                line = f"HTTP/1.1 {status} {reason}\r\n"
                rhdr["Connection"] = "close"        # read-until-close keeps streaming simple and robust
                blob = line + "".join(f"{k}: {v}\r\n" for k, v in rhdr.items()) + "\r\n"
                tls.sendall(blob.encode("latin1"))
                for chunk in up.iter_raw():
                    tls.sendall(chunk)
                    if len(captured) < 16 * 1024 * 1024:
                        captured.extend(chunk)
        except Exception:
            try:
                tls.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            return

        # remember a clean non-streamed response so an identical repeat is served for free next time
        if cache_key is not None and status < 300:
            _fp_put(cache_key, (status, reason, rhdr, bytes(captured)))

        # capture in the background, reusing the gateway pipeline so spend, attribution, savings, and
        # collaboration all work the same as the base-url path.
        if not do_capture:
            return
        latency = (time.perf_counter() - started) * 1000
        self._cap_async(provider, req_json, bytes(captured), streamed, latency, ov, response_api,
                        compress_info, route_info)

    def _cap_async(self, provider, req_json, raw, streamed, latency, ov, response_api, info, route_info):
        def _cap():
            try:
                from abenlux.capture import gateway as gw
                gw._capture(provider, req_json, raw, streamed, latency, ov, response_api, info, route_info)
            except Exception:
                pass
        threading.Thread(target=_cap, daemon=True).start()


# ----------------------------- the proxy server -----------------------------

class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock = self.request
        try:
            first = b""
            while b"\r\n" not in first:
                chunk = sock.recv(4096)
                if not chunk:
                    return
                first += chunk
                if len(first) > 65536:
                    return
            line = first.split(b"\r\n", 1)[0].decode("latin1")
            if not line.upper().startswith("CONNECT "):
                return                              # only HTTPS tunnelling is handled
            target = line.split(" ", 2)[1]
            host, _, port_s = target.partition(":")
            port = int(port_s or "443")
            sock.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if _is_ai_host(host):
                self._mitm(sock, host)
            else:
                self._tunnel(sock, host, port)      # everything else passes through unread
        except Exception:
            pass

    def _mitm(self, sock, host: str) -> None:
        ca = self.server.ca
        try:
            tls = ca.context_for(host).wrap_socket(sock, server_side=True)
        except Exception:
            return
        try:
            self.server.forwarder.handle(tls, host)
        finally:
            try:
                tls.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                tls.close()
            except OSError:
                pass

    def _tunnel(self, sock, host: str, port: int) -> None:
        # a plain pass-through for non-model traffic. the bytes are never decrypted or read.
        try:
            upstream = socket.create_connection((host, port), timeout=15)
        except OSError:
            return

        def pump(a, b):
            try:
                while True:
                    data = a.recv(65536)
                    if not data:
                        break
                    b.sendall(data)
            except OSError:
                pass
            finally:
                for s in (a, b):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        t = threading.Thread(target=pump, args=(sock, upstream), daemon=True)
        t.start()
        pump(upstream, sock)
        t.join(timeout=1)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(port: int = 8889, ca_dir: str | None = None,
                upstream_override: dict | None = None, capture: bool = True) -> _Server:
    server = _Server(("127.0.0.1", port), _Handler)
    server.ca = LocalCA(ca_dir)
    server.forwarder = _Forwarder(upstream_override, capture=capture)
    return server


def serve(port: int = 8889) -> None:
    server = make_server(port)
    print(f"abenlux forward proxy on http://127.0.0.1:{port}", flush=True)
    print(f"trust this CA once: {server.ca.cert_path}", flush=True)
    print("point a tool at it with:  abenlux run <tool>", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
