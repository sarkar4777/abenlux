# Shared image for the scale end to end. Keys are passed at runtime to the driver only.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e .
COPY examples/scale-e2e/scale_driver.py ./scale_driver.py
