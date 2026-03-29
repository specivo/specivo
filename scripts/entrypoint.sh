#!/bin/sh
set -e

# ---------------------------------------------------------------------------
# Wait for the database to accept connections (up to 60 s).
# ---------------------------------------------------------------------------
echo "Waiting for database..."
python -c "
import asyncio, sys, time
from specivo.core.config import get_settings
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

async def wait():
    engine = create_async_engine(get_settings().database_url)
    for _ in range(30):
        try:
            async with engine.connect() as conn:
                await conn.execute(text('SELECT 1'))
            await engine.dispose()
            return
        except Exception:
            await asyncio.sleep(2)
    await engine.dispose()
    print('Database not available after 60 s', file=sys.stderr)
    sys.exit(1)

asyncio.run(wait())
"

# ---------------------------------------------------------------------------
# Run migrations and seed default data.
# ---------------------------------------------------------------------------
echo "Running database migrations..."
alembic upgrade head

echo "Seeding default data..."
python -m specivo.cli.seed

# ---------------------------------------------------------------------------
# Bootstrap admin user (one-time, file is deleted after creation).
# ---------------------------------------------------------------------------
if [ -f /app/data/.bootstrap.json ]; then
    echo "Creating admin user from bootstrap file..."
    python -m specivo.cli.admin create
fi

# ---------------------------------------------------------------------------
# Hand off to CMD (uvicorn / celery / etc.).
# ---------------------------------------------------------------------------
echo "Starting Specivo..."
exec "$@"
