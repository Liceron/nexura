# Single-user local setup, published on loopback only. This is the open-source edition's Docker
# setup. Multi-user, authenticated self-hosting (and publishing) is part of the closed edition
# (EDITIONS_PLAN step 4).
#
# Inside a container the server must listen on 0.0.0.0 (the port is published from there). The
# CMD below opts in to that, and Studio then refuses to start unless NEXURA_STUDIO_TOKEN is set:
# every API request needs it. Run it published on loopback only:
#
#   docker build -f deployment/Dockerfile -t nexura-studio .
#   export NEXURA_STUDIO_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
#   docker run --rm -p 127.0.0.1:8765:8765 -e NEXURA_STUDIO_TOKEN \
#       -v nexura-data:/app/data nexura-studio
#
# then open http://127.0.0.1:8765/#token=<the token>. Never add
# --insecure-allow-unauthenticated-network, and never publish on 0.0.0.0.
# Multi-stage secure build for Nexura AgentKit
# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

WORKDIR /build

# Install uv for fast, deterministic builds
COPY --from=ghcr.io/astral-sh/uv:0.12.8 /uv /bin/uv

# Copy the workspace and each package's README/license/build inputs.
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY.md ./
COPY src/ ./src/
COPY packages/ ./packages/

# This single-user image installs nexura-runtime[studio], nexura-studio and
# nexura-agents. It does not enable optional provider, database or sandbox packages.
RUN uv build --wheel --package nexura-runtime --out-dir /build/dist
RUN uv build --wheel --package nexura-studio --out-dir /build/dist
RUN uv build --wheel --package nexura-agents --out-dir /build/dist
RUN uv export --frozen --no-dev --package nexura-runtime --package nexura-studio --package nexura-agents --extra studio --no-emit-workspace --no-hashes --output-file /build/requirements.txt
RUN uv venv /opt/venv
RUN uv pip install --python /opt/venv/bin/python -r /build/requirements.txt \
    "/build/dist/nexura_runtime-0.4.2-py3-none-any.whl[studio]" \
    /build/dist/nexura_studio-0.4.2-py3-none-any.whl \
    /build/dist/nexura_agents-0.4.2-py3-none-any.whl

# Production Runtime Image
FROM python:3.12-slim AS runtime

# Security hardening: Create non-privileged user and group
RUN groupadd -g 10001 nexura && \
    useradd -u 10001 -g nexura -s /bin/sh -m -d /home/nexura nexura

WORKDIR /app

# The environment contains only workspace wheels and the locked third-party closure.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Setup data directories with appropriate ownership
RUN mkdir -p /app/data /app/artifacts && \
    chown -R nexura:nexura /app

# Switch to non-root user
USER 10001:10001

EXPOSE 8765

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Listens on 0.0.0.0 inside the container (explicit, see the top of this file); needs
# NEXURA_STUDIO_TOKEN at run time, otherwise the bind guard stops it with exit code 2.
CMD ["nexura-studio", "--host", "0.0.0.0", "--port", "8765", "--db", "/app/data/studio.sqlite3"]
