# syntax=docker/dockerfile:1.7

# ─── Builder: install deps into a separate stage so the runtime image is lean ──
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Build deps for any wheels we don't get from PyPI prebuilt.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install -U pip \
    && /opt/venv/bin/pip install -r requirements.txt


# ─── Runtime: copy the venv + app, run as non-root ─────────────────────────────
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app"

# Build metadata surfaced by /version
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
ENV GIT_SHA=$GIT_SHA \
    BUILD_TIME=$BUILD_TIME

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# App sources. .dockerignore filters out .venv, __pycache__, tests, etc.
COPY . /app/

# Persistent state lands here. Mount a volume at /data in production so
# queue.sqlite3, run_registry.json, and logs/ survive deploys.
ENV LOG_DIR=/data/logs \
    QUEUE_DB_PATH=/data/queue.sqlite3 \
    RUN_REGISTRY_PATH=/data/run_registry.json
RUN mkdir -p /data/logs && \
    addgroup --system --gid 1001 agent && \
    adduser --system --uid 1001 --gid 1001 --home /app agent && \
    chown -R agent:agent /app /data

USER agent

EXPOSE 8000

# /health is cheap and unconditional — perfect for an orchestrator probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["python", "main.py"]
