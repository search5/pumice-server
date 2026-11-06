# syntax=docker/dockerfile:1

# ---- Build stage: resolve dependencies into a venv --------------------------------------------
FROM python:3.13-slim AS builder

# git is required at build time only, to fetch the sqlalchemy-cubrid dependency (a git source --
# see [tool.uv.sources] in pyproject.toml), not at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.21 /uv /uvx /usr/local/bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Dependencies first, in their own layer -- this is cached across source-only changes (the common
# case) since it doesn't depend on src/ at all. --no-install-project installs everything the
# project depends on but not the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Now the actual source, and the project itself on top of the already-resolved dependencies.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- Quartz stage: the optional Quartz publish renderer (docs/QUARTZ_RENDERER.ko.md) ----------
# Pinned to an exact commit and fully installed at image build time: nothing is downloaded when a
# site is built at runtime. Bump QUARTZ_REF deliberately (re-run tests/test_quartz_renderer.py
# against it first) -- Quartz 5 is young and its config/plugin surface still moves.
FROM node:22-bookworm-slim AS quartz
ARG QUARTZ_REF=97a2d05f80c4c50534959b1d0d41cc4b3895625e
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/quartz
RUN git init -q . \
    && git remote add origin https://github.com/jackyzha0/quartz.git \
    && git fetch -q --depth 1 origin "${QUARTZ_REF}" \
    && git checkout -q FETCH_HEAD \
    && rm -rf .git
RUN npm ci --no-audit --no-fund

# ---- Runtime stage ------------------------------------------------------------------------------
FROM python:3.13-slim

RUN useradd --create-home --shell /usr/sbin/nologin pumice

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY pyproject.toml README.md ./

# Node + the pinned Quartz checkout (read-only; every site build copies it into a throwaway workspace).
COPY --from=quartz /usr/local/bin/node /usr/local/bin/node
COPY --from=quartz /opt/quartz /opt/quartz

ENV PATH="/app/.venv/bin:${PATH}" \
    DATA_DIR=/data \
    HTTP_PORT=8080 \
    PUMICE_QUARTZ_DIR=/opt/quartz

# DATA_DIR holds everything that has to survive a container restart: the DB (when DB_TYPE=sqlite),
# synced vault content, version-history backups, and published sites.
RUN mkdir -p /data && chown -R pumice:pumice /data /app
VOLUME ["/data"]
EXPOSE 8080

USER pumice

# ADMIN_USER/ADMIN_PASSWORD/DB_* etc. are read from the environment (docker run --env-file .env,
# or docker-compose's env_file) -- never baked into the image.
CMD ["server"]
