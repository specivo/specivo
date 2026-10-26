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
# owns only the subdirectories in APP_DIRS. Root never follows a symlink and
# never changes a file another user owns; see own_tree.
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

# A directory an earlier version gave to the app (data dir, logs/) goes back to
# root, and nobody else may write to it.
reclaim_dir() {
    [ -d "$1" ] && [ ! -L "$1" ] || return 0
    if [ "$(stat -c '%u' "$1")" = "$APP_UID" ]; then
        echo "Giving $1 back to root"
        chown -h 0:0 "$1"
    fi
    if [ -n "$(find "$1" -maxdepth 0 -perm -0002)" ]; then
        warn "$1 was world-writable; removing that."
        chmod o-w "$1"
    fi
}

# Give one app directory to the app user.
#
# - Never through a symlink, and only for a real directory on the same
#   filesystem as the data directory.
# - Cheap when nothing is to do: a directory already owned by the app user is
#   not walked, so restarts stay fast however many attachments there are.
# - Otherwise only entries owned by root or by the data directory's owner (the
#   uid that ran the installer) change; files of other users, such as the
#   database's uid 999, keep their owner.
# - Only directories and regular files with a single link. chown -h does not
#   follow symlinks, but a hard link is the file itself: changing the owner of
#   a hard-linked file would change it everywhere it is linked, so those are
#   left alone and reported. Upload and model files are never hard-linked.
# - -depth changes a directory only after everything in it, so nothing in a
#   tree becomes writable by the app while it is still being walked.
own_tree() {
    dir="$DATA_DIR/$1"
    if [ -L "$dir" ]; then
        warn "$dir is a symlink; not changing its owner. Mount the target there instead."
        return 0
    fi
    if [ ! -e "$dir" ]; then
        echo "Creating $dir"
        mkdir -p "$dir"
        [ "$1" = state ] && chmod 0700 "$dir"
        chown -h "$APP_UID:$APP_GID" "$dir"
        return 0
    fi
    if [ ! -d "$dir" ]; then
        warn "$dir is not a directory; leaving it alone."
        return 0
    fi
    if [ "$(stat -c '%d' "$dir")" != "$DATA_DEV" ]; then
        warn "$dir is on another filesystem; not changing its owner. Run: chown -R $APP_UID:$APP_GID on it."
        return 0
    fi
    owner=$(stat -c '%u' "$dir")
    [ "$owner" = "$APP_UID" ] && return 0
    if [ "$owner" != 0 ] && [ "$owner" != "$DATA_OWNER" ]; then
        warn "$dir belongs to uid $owner, not root or the data directory's owner; not changing it."
        echo "  If the app should use it, run chown -R $APP_UID:$APP_GID on it on the host." >&2
        return 0
    fi
    echo "Setting owner of $dir to $APP_UID:$APP_GID"
    find "$dir" -xdev -depth \( -uid 0 -o -uid "$owner" \) \
        \( -type d -o \( -type f -links 1 \) \) \
        -exec chown -h "$APP_UID:$APP_GID" {} +
    linked=$(find "$dir" -xdev -type f -links +1 \( -uid 0 -o -uid "$owner" \) | head -n 5)
    if [ -n "$linked" ]; then
        warn "hard-linked files in $dir kept their owner (fix them by hand if the app needs them):"
        echo "$linked" >&2
    fi
}

# The installer writes the first admin's credentials to .bootstrap.json at the
# top of the data directory. The app cannot delete files there, so it is moved
# into state/, which the app owns.
move_bootstrap_file() {
    src="$DATA_DIR/.bootstrap.json"
    dst="$DATA_DIR/state/bootstrap.json"
    [ -e "$src" ] || [ -L "$src" ] || return 0
    if [ -L "$src" ] || [ ! -f "$src" ] || [ "$(stat -c '%h' "$src")" != 1 ]; then
        warn "$src is not a plain file; ignoring it."
        return 0
    fi
    if [ -L "$DATA_DIR/state" ] || [ ! -d "$DATA_DIR/state" ]; then
        return 0
    fi
    echo "Moving $src to $dst"
    mv -f "$src" "$dst"
    chown -h "$APP_UID:$APP_GID" "$dst"
    chmod 0600 "$dst"
}

fix_permissions() {
    [ -d "$DATA_DIR" ] || return 0
    reclaim_dir "$DATA_DIR"
    DATA_DEV=$(stat -c '%d' "$DATA_DIR")
    DATA_OWNER=$(stat -c '%u' "$DATA_DIR")
    if [ -L "$DATA_DIR/logs" ]; then
        warn "$DATA_DIR/logs is a symlink; not using it."
    else
        mkdir -p "$DATA_DIR/logs"
        reclaim_dir "$DATA_DIR/logs"
    fi
    for rel in $APP_DIRS; do
        if [ "$rel" = logs/app ] && [ -L "$DATA_DIR/logs" ]; then
            continue
        fi
        own_tree "$rel"
    done
    move_bootstrap_file
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
# Root moved it into state/ (see move_bootstrap_file); without a root step it
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
