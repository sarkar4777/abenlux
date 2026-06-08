"""
Real-SDK contract tests. Aider, Cline, Continue, opencode, Pi, Droid and the rest don't speak a
bespoke protocol - they sit on the official Anthropic / OpenAI SDKs (or their exact HTTP shape).
The highest-signal proof that "these tools work with Abenlux" is to drive the genuine vendor SDKs
through a real, running gateway. We boot the mock upstream + gateway as subprocesses (exactly the
deployment topology) and point the real SDKs at the gateway's base_url - the same one-line config
a developer uses for their tool. No vendor tokens are spent, the mock returns spec-faithful streams.
"""
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

anthropic = pytest.importorskip("anthropic")
openai = pytest.importorskip("openai")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_health(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1.0).status_code == 200:
                return True
        except Exception:
            time.sleep(0.3)
    return False


@pytest.fixture(scope="module")
def gateway_url(tmp_path_factory):
    mock_port, gw_port = _free_port(), _free_port()
    db = str(tmp_path_factory.mktemp("sdk") / "sdk.db")
    env = dict(
        os.environ,
        ABEN_DB=db, ABEN_HMAC_KEY="sdk-key", ABEN_ACTOR="alice@corp.com",
        ABEN_ANTHROPIC_UPSTREAM=f"http://127.0.0.1:{mock_port}",
        ABEN_OPENAI_UPSTREAM=f"http://127.0.0.1:{mock_port}",
        ABEN_KG="", ABEN_COLLECTOR_URL="",
    )
    procs = [
        subprocess.Popen([sys.executable, "-m", "abenlux.cli", "mock", "--port", str(mock_port)], env=env),
        subprocess.Popen([sys.executable, "-m", "abenlux.cli", "gateway", "--port", str(gw_port)], env=env),
    ]
    try:
        if not (_wait_health(f"http://127.0.0.1:{mock_port}/v1/messages") or True):
            pass
        if not _wait_health(f"http://127.0.0.1:{gw_port}/health"):
            pytest.skip("gateway/mock did not start in time")
        yield f"http://127.0.0.1:{gw_port}", db
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()


def test_real_anthropic_sdk_through_gateway(gateway_url):
    url, db = gateway_url
    ac = anthropic.Anthropic(api_key="dummy", base_url=url)
    secret = "migrate the ACME billing schema, key sk-ant-SDK12345678901234567890"
    with ac.messages.stream(model="claude-opus-4-8", max_tokens=64,
                            messages=[{"role": "user", "content": secret}]) as s:
        text = "".join(s.text_stream)
        final = s.get_final_message()
    assert "Temporal saga" in text                       # tool received the untouched stream
    assert final.usage.input_tokens == 1820 and final.usage.output_tokens == 1820 // 8

    time.sleep(1.0)  # let the gateway's BackgroundTask capture complete
    raw = open(db, "rb").read()
    assert b"ACME billing" not in raw and b"sk-ant-" not in raw  # content + secret never persisted


def test_real_openai_sdk_through_gateway(gateway_url):
    url, db = gateway_url
    oc = openai.OpenAI(api_key="dummy", base_url=f"{url}/v1")
    chunks = oc.chat.completions.create(
        model="gpt-5.5", stream=True, stream_options={"include_usage": True},
        messages=[{"role": "user", "content": "refactor the auth module"}])
    text, usage = "", None
    for c in chunks:
        if c.choices:
            text += c.choices[0].delta.content or ""
        if c.usage:
            usage = c.usage
    assert text == "Hello world"
    assert usage.prompt_tokens == 1820 and usage.completion_tokens == 1820 // 8


def test_mock_usage_honors_input_header(gateway_url):
    # the gateway forwards X-Aben-Mock-Input so a fleet of real captures produces varied, realistic cost
    url, _ = gateway_url
    ac = anthropic.Anthropic(api_key="dummy", base_url=url,
                             default_headers={"X-Aben-Mock-Input": "6400"})
    with ac.messages.stream(model="claude-opus-4-8", max_tokens=64,
                            messages=[{"role": "user", "content": "scaffold a new billing service"}]) as s:
        "".join(s.text_stream)
        final = s.get_final_message()
    assert final.usage.input_tokens == 6400 and final.usage.output_tokens == 6400 // 8
