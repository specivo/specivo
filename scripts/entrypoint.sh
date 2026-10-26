#!/bin/sh
set -e

# ---------------------------------------------------------------------------
# Privileges.
#
# The image runs as the unprivileged `specivo` user (SPECIVO_UID/SPECIVO_GID,
# 10001 by default). When the container is started as root instead (the
# `init-data` service in docker-compose.yml, or `docker run --user 0`), this
# script first prepares the app's directories in the data directory, then
# re-runs itself without root. Started as any other user it changes nothing
# and runs as-is, so `user:` in compose and read-only root filesystems work.
#
# `/entrypoint.sh fix-permissions` does the root part and exits; started as
# another user it exits without changing anything.
#
# The data directory itself stays with root (or whoever created it) and is
# not writable by the app, so the app cannot rename, replace or symlink its
# entries. In the default layout it also holds postgresql/, redis/ and
# logs/nginx/, whose host paths Docker mounts into other containers. The app
# owns only the subdirectories in APP_DIRS. The root part is done by
# specivo/ops/fix_ownership.py, which works on file descriptors only (never
# follows a symlink, never acts on a path the app could swap) and never
# changes a file another user owns. Run it with the app containers stopped;
# `fix-permissions --all` also enters directories the app already owns.
# ---------------------------------------------------------------------------
APP_UID="${SPECIVO_UID:-10001}"
APP_GID="${SPECIVO_GID:-10001}"
# SPECIVO_DATA_PATH only lets tests point the root step at another directory.
DATA_DIR="${SPECIVO_DATA_PATH:-/app/data}"
# The directories the app writes to, relative to DATA_DIR. state/ holds the
# one-time admin bootstrap file; logs/app/ the app's log files.
APP_DIRS="attachments avatars models state logs/app"

warn() {
    echo "WARNING: $*" >&2
}

fix_permissions() {
    # -I: ignore PYTHON* variables and the working directory; standard library only.
    python -I -m specivo.ops.fix_ownership --data-dir "$DATA_DIR" --uid "$APP_UID" --gid "$APP_GID" "$@"
}

if [ "$(id -u)" = "0" ]; then
    if [ "$1" = "fix-permissions" ]; then
        shift
        fix_permissions "$@"
        exit 0
    fi
    fix_permissions
    # Drop root for good: the rest of this script and the CMD run as the app
    # user. setpriv is part of util-linux, which the base image already has.
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups "$0" "$@"
fi

if [ "$1" = "fix-permissions" ]; then
    echo "Not running as root: leaving the ownership of $DATA_DIR as it is."
    exit 0
fi

if [ -d "$DATA_DIR" ]; then
    for rel in $APP_DIRS; do
        if [ ! -w "$DATA_DIR/$rel" ]; then
            warn "$DATA_DIR/$rel is missing or not writable by uid $(id -u); uploads, model downloads or logs may fail."
            echo "  Start the container once as root, or create it on the host and run chown -R $APP_UID:$APP_GID on it." >&2
        fi
    done
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
# Root moved it into state/ (see specivo/ops/fix_ownership.py); without a root step it
# may still be at the top of the data directory.
for bootstrap in /app/data/state/bootstrap.json /app/data/.bootstrap.json; do
    [ -f "$bootstrap" ] || continue
    if [ -r "$bootstrap" ]; then
        echo "Creating admin user from bootstrap file..."
        python -m specivo.cli.admin create
    else
        warn "$bootstrap is not readable by uid $(id -u); no admin user created from it."
    fi
    break
done

# ---------------------------------------------------------------------------
# Hand off to CMD (uvicorn / celery / etc.).
# ---------------------------------------------------------------------------
echo "Starting Specivo..."
exec "$@"
