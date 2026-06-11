"""The gateway must request an UNCOMPRESSED upstream response, else the tee'd raw bytes are gzip/br
encoded and capture silently fails against real providers (the mock never compresses, hiding it)."""
from abenlux.capture.gateway import _HOP_BY_HOP, _forward_headers


def test_forward_headers_force_identity_encoding():
    fwd = _forward_headers({"Accept-Encoding": "gzip, br", "x-api-key": "k", "Host": "localhost"})
    assert fwd["accept-encoding"] == "identity"      # upstream returns plain bytes we can parse
    assert "host" not in {k.lower() for k in fwd}     # hop-by-hop dropped
    assert fwd["x-api-key"] == "k"                    # auth + other headers relayed unchanged


def test_forward_headers_adds_identity_when_absent():
    # httpx would otherwise inject Accept-Encoding: gzip on the upstream request
    fwd = _forward_headers({"content-type": "application/json"})
    assert fwd["accept-encoding"] == "identity"


def test_forward_headers_drops_all_hop_by_hop():
    src = {h: "v" for h in _HOP_BY_HOP}
    src["authorization"] = "Bearer t"
    fwd = _forward_headers(src)
    assert all(h not in {k.lower() for k in fwd} for h in _HOP_BY_HOP)
    assert fwd["authorization"] == "Bearer t"


def test_decode_body_gunzips_a_compressed_response():
    import gzip
    from abenlux.capture.gateway import _decode_body
    payload = b'{"id":"chatcmpl","usage":{"prompt_tokens":10,"completion_tokens":2}}'
    gz = gzip.compress(payload)
    assert _decode_body(gz, "gzip") == payload            # real providers gzip behind a CDN
    assert _decode_body(payload, "") == payload           # identity passes through unchanged
    assert _decode_body(gz, "br") == gz                   # unavailable codec -> raw bytes (fail loud later)
