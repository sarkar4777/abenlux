# Shared image for the multi-container real-model E2E: the collector, every edge gateway, the mock
# upstream, and the driver all run this one image, differing only by command + env. Built once from
# the repo root. Keys are NEVER baked in - they are passed at runtime from the host environment.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# the package already depends on fastapi/uvicorn/httpx/pyyaml - install in ONE resolve so a second
# pip pass can't pick a conflicting version (it intermittently did, failing the build).
RUN pip install --no-cache-dir -e .
COPY examples/multi-dev-e2e/driver.py ./driver.py
