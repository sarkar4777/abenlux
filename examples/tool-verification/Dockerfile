# Reproducible image with the real CLI coding tools, used to verify that abenlux captures each one
# accurately. Build once, then run verify_tools.py (which boots a gateway on the host and drives
# each tool inside a container against it).
FROM node:22-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @openai/codex @google/gemini-cli opencode-ai
WORKDIR /work
