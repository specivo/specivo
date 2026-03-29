FROM python:3.12-alpine

WORKDIR /app

# Install uv for fast, reproducible dependency resolution.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first (layer caching for deps).
COPY pyproject.toml uv.lock ./

# Copy application code before install so the package resolves correctly.
COPY . .

# Install dependencies + the specivo package itself (no dev deps).
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

# Entrypoint: wait for DB, migrate, seed, bootstrap admin, then exec CMD.
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "specivo.main:app", "--host", "0.0.0.0", "--port", "8000"]
