# -----------------------------------------------------------------------------
# Stage 1: build the esbuild frontend bundles (specivo/static/dist/).
#
# The bundles are generated, never committed. This stage runs on the build
# host's native platform: esbuild emits plain JS/CSS, so a multi-arch release
# builds them once instead of under emulation for every target platform.
# -----------------------------------------------------------------------------
FROM --platform=$BUILDPLATFORM node:22-slim AS frontend

WORKDIR /build/frontend

# Lockfile first so the dependency install stays cached until it changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

# build.js writes to ../specivo/static/dist, i.e. /build/specivo/static/dist.
COPY frontend/ ./
RUN npm run build

# -----------------------------------------------------------------------------
# Stage 2: runtime image. No Node; only the built bundles are copied in.
# -----------------------------------------------------------------------------
FROM python:3.12-slim

# Patch base image packages
RUN apt-get update -qq && apt-get upgrade -y -qq && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv for fast, reproducible dependency resolution.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first (layer caching for deps).
COPY pyproject.toml uv.lock ./

# Copy application code before install so the package resolves correctly.
COPY . .

# Install dependencies + the specivo package itself (no dev deps).
# Remove pip from base image (CVE-2025-8869, CVE-2026-1703) — we use uv.
RUN uv sync --frozen --no-dev \
    && pip3 uninstall -y pip 2>/dev/null; \
    rm -rf /usr/lib/python3.12/ensurepip \
           /usr/local/lib/python3.12/site-packages/pip*
ENV PATH="/app/.venv/bin:$PATH"

# Frontend bundles from stage 1. Copied after the source, so a local
# specivo/static/dist can never win (.dockerignore also keeps it out).
COPY --from=frontend /build/specivo/static/dist /app/specivo/static/dist

# Entrypoint: wait for DB, migrate, seed, bootstrap admin, then exec CMD.
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "specivo.main:app", "--host", "0.0.0.0", "--port", "8000"]
