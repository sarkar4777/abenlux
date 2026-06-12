# Shared image for the feature end to end. The collector, the gateways, and the driver all run this one
# image. Keys are never baked in, they are passed at runtime to the driver only.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e .
COPY examples/features-e2e/feature_driver.py ./feature_driver.py
