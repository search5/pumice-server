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

# ---- Runtime stage ------------------------------------------------------------------------------
FROM python:3.13-slim

RUN useradd --create-home --shell /usr/sbin/nologin pumice

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY pyproject.toml README.md ./

ENV PATH="/app/.venv/bin:${PATH}" \
    DATA_DIR=/data \
    HTTP_PORT=8080

# DATA_DIR holds everything that has to survive a container restart: the DB (when DB_TYPE=sqlite),
# synced vault content, version-history backups, and published sites.
RUN mkdir -p /data && chown -R pumice:pumice /data /app
VOLUME ["/data"]
EXPOSE 8080

USER pumice

# ADMIN_USER/ADMIN_PASSWORD/DB_* etc. are read from the environment (docker run --env-file .env,
# or docker-compose's env_file) -- never baked into the image.
CMD ["server"]
