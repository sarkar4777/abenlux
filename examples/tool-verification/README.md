# Tool capture verification (Docker)

Reproduce the README's claim that abenlux captures the real CLI coding tools accurately. Each tool
runs inside a container and talks to an abenlux gateway on the host, which forwards to the mock and
persists a derived record we read back.

```bash
docker build -t abenlux-tools examples/tool-verification
python examples/tool-verification/verify_tools.py
```

Expected output (token counts come from the mock, cost is abenlux's own pricing):

```
== gemini-cli (Docker) -> abenlux gateway -> mock ==
  OK provider=google model=gemini-3.5-flash in=1820 out=227 cost=$0.004773 tool=gemini-cli
== codex (Docker) -> abenlux gateway -> mock ==
  OK provider=openai model=gpt-5.5 in=1820 out=227 cost=$0.01591 tool=codex
== opencode (Docker) -> abenlux gateway -> mock ==
  OK provider=openai model=gpt-5.5 in=1820 out=227 cost=$0.01591 tool=opencode
```

What this exercises, per tool:

- **Gemini CLI** — its real wire format: streaming via `:streamGenerateContent?alt=sse` (no body
  `stream` flag) with the model in the URL, not the body.
- **Codex** — the OpenAI **Responses API** (`/v1/responses`), a different shape from chat/completions.
- **opencode** — a custom OpenAI-compatible provider over chat/completions.

`aider` is verified against a **real Azure gpt-4o** deployment (see the README), where abenlux's
captured cost matched aider's own report. **Claude Code** is verified from genuine Tier-1 OTel session
telemetry (it self-instruments rather than going through a base-URL proxy). This harness is not part
of `make test` because it needs Docker and pulls the tool images.

Networking note: the container reaches the host gateway at `host.docker.internal`. That name is
built in on Docker Desktop (macOS/Windows); on Linux the `--add-host=host.docker.internal:host-gateway`
flag (already used by the script) makes it resolve.
