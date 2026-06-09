# Deep end-to-end verification image. Boots the WHOLE stack (mock upstream + central collector +
# one edge gateway per tenant) inside one container and drives many developers across multiple
# tenants/orgs through multi-turn model calls, then asserts every role's view and every guarantee.
#
#   docker build -t abenlux-deep-e2e -f examples/deep-e2e/Dockerfile .
#   docker run --rm abenlux-deep-e2e
#
# Exits non-zero if any check fails.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e . && pip install --no-cache-dir uvicorn httpx pyyaml
COPY examples/deep-e2e/deep_e2e.py ./deep_e2e.py
CMD ["python", "-u", "deep_e2e.py"]
