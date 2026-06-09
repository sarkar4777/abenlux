"""
Reproducible end-to-end verification that abenlux captures the real CLI coding tools accurately.

Each tool runs inside the `abenlux-tools` Docker image and reaches a gateway running on the host via
host.docker.internal. The gateway forwards to the abenlux mock and persists a derived record, which
we read back and print. Nothing on the host is configured - the tools' settings live in the container.

Usage:
    docker build -t abenlux-tools examples/tool-verification
    python examples/tool-verification/verify_tools.py

aider is verified separately against a real Azure deployment in the docs - it is OpenAI/Anthropic-SDK
based, so its wire format is the same one exercised by tests/test_real_sdk.py.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
WORK = Path(tempfile.mkdtemp(prefix="abenlux-verify-"))
MOCK, GW = 9321, 8369
HOST = "host.docker.internal"
ENV = dict(os.environ, ABEN_NOTIFY="0")


def _wait(port: int) -> bool:
    for _ in range(60):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return True
        except Exception:
            time.sleep(0.25)
    return False


def _gateway(tool: str, db: str) -> subprocess.Popen:
    env = dict(ENV, ABEN_DB=db, ABEN_LOCAL_DB=db, ABEN_HMAC_KEY="verify", ABEN_COLLECTOR_URL="",
               ABEN_TOOL=tool, ABEN_OPENAI_UPSTREAM=f"http://127.0.0.1:{MOCK}",
               ABEN_GOOGLE_UPSTREAM=f"http://127.0.0.1:{MOCK}", ABEN_ANTHROPIC_UPSTREAM=f"http://127.0.0.1:{MOCK}")
    # bind 0.0.0.0 so the container can reach the gateway on the host
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "abenlux.capture.gateway:app",
                          "--host", "0.0.0.0", "--port", str(GW)], env=env, cwd=str(REPO),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _wait(GW)
    return p


def _docker(script: str) -> subprocess.CompletedProcess:
    # --add-host makes host.docker.internal resolve on Linux too (Docker Desktop has it already)
    return subprocess.run(
        ["docker", "run", "--rm", "--add-host=host.docker.internal:host-gateway",
         "abenlux-tools", "bash", "-lc", script],
        capture_output=True, text=True, timeout=240)


GEMINI = (
    'mkdir -p /root/.gemini && '
    'printf \'{"security":{"auth":{"selectedType":"gemini-api-key"}},"telemetry":{"enabled":false}}\' > /root/.gemini/settings.json && '
    f'GEMINI_API_KEY=dummy GOOGLE_GEMINI_BASE_URL=http://{HOST}:{GW} GEMINI_CLI_TRUST_WORKSPACE=true '
    'gemini --skip-trust -m gemini-3.5-flash -p "say hi" 2>&1 | tail -2'
)
CODEX = (
    'mkdir -p /cfg && printf \''
    'model = "gpt-5.5"\\nmodel_provider = "mockp"\\n[model_providers.mockp]\\nname = "mock"\\n'
    f'base_url = "http://{HOST}:{GW}/v1"\\nwire_api = "responses"\\nenv_key = "MOCK_KEY"\\n\' > /cfg/config.toml && '
    'CODEX_HOME=/cfg MOCK_KEY=dummy codex exec --skip-git-repo-check "say hi" 2>&1 | tail -2'
)
OPENCODE = (
    'printf \'{"$schema":"https://opencode.ai/config.json","provider":{"mock":{'
    '"npm":"@ai-sdk/openai-compatible","name":"mock",'
    f'"options":{{"baseURL":"http://{HOST}:{GW}/v1","apiKey":"dummy"}},'
    '"models":{"gpt-5.5":{"name":"gpt-5.5"}}}}}\' > /work/opencode.json && '
    'OPENCODE_CONFIG=/work/opencode.json opencode run -m mock/gpt-5.5 "say hi" 2>&1 | tail -2'
)


def main() -> None:
    mock = subprocess.Popen([sys.executable, "-m", "abenlux.cli", "mock", "--port", str(MOCK)],
                            env=ENV, cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not _wait(MOCK):
        # mock has no /health on older builds, give it a moment anyway
        time.sleep(3)
    failures = 0
    try:
        for tool, script in [("gemini-cli", GEMINI), ("codex", CODEX), ("opencode", OPENCODE)]:
            db = str(WORK / f"{tool}.db")
            gw = _gateway(tool, db)
            print(f"\n== {tool} (Docker) -> abenlux gateway -> mock ==")
            _docker(script)
            time.sleep(2)
            gw.terminate()
            time.sleep(1)
            rows = sqlite3.connect(db).execute(
                "SELECT provider, request_model, input_tokens, output_tokens, ROUND(cost_usd,6), tool "
                "FROM derived ORDER BY ts").fetchall()
            if not rows:
                print("  FAIL: no calls captured")
                failures += 1
            for r in rows:
                print("  OK provider=%s model=%s in=%s out=%s cost=$%s tool=%s" % r)
    finally:
        mock.terminate()
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
