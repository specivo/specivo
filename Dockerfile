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

# The app runs as this unprivileged user. Fixed IDs, so a host directory can be
# given to it with `chown 10001:10001`; override with --build-arg if needed.
ARG SPECIVO_UID=10001
ARG SPECIVO_GID=10001

# Patch base image packages
RUN apt-get update -qq && apt-get upgrade -y -qq && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid "$SPECIVO_GID" specivo \
    && useradd --system --uid "$SPECIVO_UID" --gid specivo \
       --home-dir /home/specivo --create-home --shell /usr/sbin/nologin specivo

WORKDIR /app

# Install uv for fast, reproducible dependency resolution.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first (layer caching for deps).
COPY pyproject.toml uv.lock ./

# Copy application code before install so the package resolves correctly.
COPY . .

# Install dependencies + the specivo package itself (no dev deps), compiled to
# bytecode at build time: the app user cannot write __pycache__ under /app.
# Remove pip from base image (CVE-2025-8869, CVE-2026-1703) — we use uv.
RUN UV_COMPILE_BYTECODE=1 uv sync --frozen --no-dev \
    && python -m compileall -q /app/specivo /app/alembic \
    && pip3 uninstall -y pip 2>/dev/null; \
    rm -rf /usr/lib/python3.12/ensurepip \
           /usr/local/lib/python3.12/site-packages/pip* \
           /root/.cache
ENV PATH="/app/.venv/bin:$PATH"

# Frontend bundles from stage 1. Copied after the source, so a local
# specivo/static/dist can never win (.dockerignore also keeps it out).
COPY --from=frontend /build/specivo/static/dist /app/specivo/static/dist

# Entrypoint: fix data ownership and drop root if started as root, wait for
# DB, migrate, seed, bootstrap admin, then exec CMD. See scripts/entrypoint.sh.
COPY scripts/entrypoint.sh /entrypoint.sh
COPY scripts/healthcheck.py /usr/local/bin/specivo-healthcheck

# App files stay owned by root and read-only for the app user. The data
# directory stays with root too; the app owns only the subdirectories it
# writes to (the same layout the entrypoint sets up on a mounted directory,
# and what Docker copies into a new named volume). The app's private state
# (celery beat's schedule) is in its home. See scripts/entrypoint.sh.
RUN chmod 0755 /entrypoint.sh /usr/local/bin/specivo-healthcheck \
    && chmod -R u+rwX,go+rX,go-w /app \
    && mkdir -p /app/data/attachments /app/data/avatars /app/data/models \
                /app/data/state /app/data/logs/app \
                /home/specivo/.local/state/specivo \
    && chown "$SPECIVO_UID:$SPECIVO_GID" /app/data/attachments /app/data/avatars \
             /app/data/models /app/data/state /app/data/logs/app \
    && chown -R "$SPECIVO_UID:$SPECIVO_GID" /home/specivo \
    && chmod 0700 /app/data/state /home/specivo /home/specivo/.local/state/specivo
ENV SPECIVO_UID=$SPECIVO_UID \
    SPECIVO_GID=$SPECIVO_GID \
    HOME=/home/specivo \
    XDG_CACHE_HOME=/tmp/.cache \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

USER $SPECIVO_UID:$SPECIVO_GID

# Probes /health/ when the container runs the web app; passes for the Celery
# containers, which serve no HTTP. See scripts/healthcheck.py.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["specivo-healthcheck"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "specivo.main:app", "--host", "0.0.0.0", "--port", "8000"]
