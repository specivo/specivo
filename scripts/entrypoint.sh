#!/bin/sh
set -e

# ---------------------------------------------------------------------------
# Privileges.
#
# The image runs as the unprivileged `specivo` user (SPECIVO_UID/SPECIVO_GID,
# 10001 by default). When the container is started as root instead (the
# `init-data` service in docker-compose.yml, or `docker run --user 0`), this
# script first gives the app's paths in the data directory to that user, then
# re-runs itself without root. Started as any other user it does neither and
# runs as-is, so `user:` in compose and read-only root filesystems work.
#
# `/entrypoint.sh fix-permissions` fixes ownership as root and exits; started
# as another user it exits without changing anything.
# ---------------------------------------------------------------------------
APP_UID="${SPECIVO_UID:-10001}"
APP_GID="${SPECIVO_GID:-10001}"
DATA_DIR=/app/data

# Give a path to the app user, unless it already belongs to it. own_tree
# recurses, but checks only the top level first: restarts stay fast on large
# data directories, and an install written by root is fixed once.
own_top() {
    [ -e "$1" ] || return 0
    if [ "$(stat -c '%u:%g' "$1")" != "$APP_UID:$APP_GID" ]; then
        echo "Setting owner of $1 to $APP_UID:$APP_GID"
        chown "$APP_UID:$APP_GID" "$1"
    fi
}

own_tree() {
    [ -e "$1" ] || return 0
    if [ "$(stat -c '%u:%g' "$1")" != "$APP_UID:$APP_GID" ]; then
        echo "Setting owner of $1 (recursively) to $APP_UID:$APP_GID"
        chown -R "$APP_UID:$APP_GID" "$1"
    fi
}

fix_permissions() {
    [ -d "$DATA_DIR" ] || return 0
    # Only the app's own paths. The default layout keeps postgresql/, redis/
    # and logs/nginx/ in the same directory; those belong to other containers
    # and keep their owners.
    own_top "$DATA_DIR"
    for path in attachments avatars models themes errors .bootstrap.json; do
        own_tree "$DATA_DIR/$path"
    done
    if [ -d "$DATA_DIR/logs" ]; then
        own_top "$DATA_DIR/logs"
        for path in "$DATA_DIR"/logs/*; do
            [ "$path" = "$DATA_DIR/logs/nginx" ] && continue
            own_tree "$path"
        done
    fi
}

if [ "$(id -u)" = "0" ]; then
    fix_permissions
    if [ "$1" = "fix-permissions" ]; then
        exit 0
    fi
    # Drop root for good: the rest of this script and the CMD run as the app
    # user. setpriv is part of util-linux, which the base image already has.
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups "$0" "$@"
fi

if [ "$1" = "fix-permissions" ]; then
    echo "Not running as root: leaving the ownership of $DATA_DIR as it is."
    exit 0
fi

if [ -d "$DATA_DIR" ] && [ ! -w "$DATA_DIR" ]; then
    echo "WARNING: $DATA_DIR is not writable by uid $(id -u). Uploads and logs will fail." >&2
    echo "  Start the container once as root, or on the host run:" >&2
    echo "  chown $APP_UID:$APP_GID <SPECIVO_DATA_DIR> and chown -R $APP_UID:$APP_GID its attachments, avatars and models" >&2
fi

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
