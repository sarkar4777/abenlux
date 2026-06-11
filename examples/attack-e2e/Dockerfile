# Image for the adversarial thrash harness: collector, edge gateways, and the attack driver all run
# this one image, differing only by command + env. Built from the repo root. Keys are NEVER baked in,
# they are passed at runtime from the host environment to the driver only.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e .
COPY examples/attack-e2e/attack_driver.py ./attack_driver.py
