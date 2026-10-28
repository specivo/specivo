# Deployment Guide

## Requirements

- Docker 24+ with Docker Compose V2
- 2 GB RAM minimum (4 GB recommended)
- PostgreSQL 14+ with `ltree` and `pgvector` extensions (bundled or external, tested on 16 and 18)
- Redis 7+ (bundled or external)

## Quick Start (Online)

```bash
# 1. Download the project
git clone https://github.com/specivo/specivo.git
cd specivo

# 2. Run the setup wizard
make configure

# 3. Start all services
make up

# 4. Open in browser
open http://localhost:9933
```

The setup wizard creates `.env` and `.env.local` files, initializes the data directory, and configures the admin account.

## Quick Start (Docker Hub)

If you already have Docker Compose configured:

```bash
docker pull specivo/specivo:latest
make configure
make up
```

Pin to a specific version:

```bash
# In .env or .env.local:
SPECIVO_VERSION=0.8.0
```

## Offline / Airgapped Installation

For networks without internet access:

1. **On a machine with internet**: download the release bundle from [GitHub Releases](https://github.com/specivo/specivo/releases)

2. **Transfer** `specivo-X.Y.Z-bundle.tar.gz` to the target machine (USB, internal network, etc.)

3. **On the target machine**:

```bash
# Extract the bundle
tar xzf specivo-X.Y.Z-bundle.tar.gz
cd specivo-*

# Load the Docker image (no internet needed)
docker load < specivo-image.tar

# Run the setup wizard
python3 scripts/configure.py

# Start
docker compose up -d
```

The bundle includes:
- Pre-built Docker image (`specivo-image.tar`)
- Docker Compose configuration
- Nginx reverse proxy config
- Setup wizard (`scripts/configure.py`)
- Container entrypoint script
- Host-side model download script (`scripts/download-model.sh`), for air-gapped setups
- `Makefile` (`make help`; the `download-model`, `backfill-embeddings`, `reindex-fts` and
  `import-data` targets work against the bundle's compose file)
- Installation guide (`docs/INSTALL.md`)

## Architecture

```
[Browser] → :9933 → [Nginx] → :8000 → [FastAPI API]
                                            ↕
                                    [PostgreSQL] [Redis]
                                            ↕
                                    [Celery Worker] [Celery Beat]
```

### Services

| Service | Image | Purpose |
|---------|-------|---------|
| **nginx** | nginx:1-alpine | Reverse proxy, static files, TLS termination |
| **init-data** | specivo/specivo | Runs once as root before `api` starts, prepares the app's directories in the data directory for the app user, then exits |
| **api** | specivo/specivo | FastAPI application server |
| **celery-worker** | specivo/specivo | Async task processing (emails, webhooks, search reindex) |
| **celery-embeddings** | specivo/specivo | Embedding jobs for semantic search, on the `embeddings` queue; one process, so one copy of the model in memory |
| **celery-beat** | specivo/specivo | Scheduled task runner |
| **db** | pgvector/pgvector:pg18 | PostgreSQL with pgvector (optional — use external) |
| **redis** | redis:7-alpine | Cache, task broker, pub/sub (optional — use external) |

## Configuration

### Environment Variables

All configuration is via environment variables. Set in `.env` (committed) or `.env.local` (secrets, gitignored).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `COMPOSE_PROJECT_NAME` | No | `specivo` | Instance name (prefix for containers, networks, volumes) |
| `SECRET_KEY` | Yes | — | JWT signing key (min 32 bytes) |
| `DATABASE_URL` | Yes | — | PostgreSQL connection string |
| `REDIS_URL` | Yes | — | Redis connection string |
| `SPECIVO_PORT` | No | `9933` | External port (nginx) |
| `SPECIVO_VERSION` | No | `latest` | Docker image tag |
| `SPECIVO_DATA_DIR` | No | `./specivo-data` | Persistent data directory |
| `DEBUG` | No | `false` | Enable debug mode |
| `REGISTRATION_MODE` | No | `open` | `open`, `invite_only`, or `disabled` |
| `CORS_ORIGINS` | No | `[]` | Allowed CORS origins (JSON array) |
| `SMTP_HOST` | No | `localhost` | Email server |
| `SMTP_PORT` | No | `587` | Email server port |
| `SMTP_FROM` | No | `noreply@specivo.dev` | Sender address |
| `KILL_TOKEN` | No | — | Emergency kill switch token |
| `EMBEDDING_ONNX_THREADS` | No | `2` | CPU threads for the embedding model (`0` = all cores) |
| `EMBEDDING_WORKER_CONCURRENCY` | No | `1` | Processes in `celery-embeddings`; each loads its own copy of the model |
| `EMBEDDING_QUEUE` | No | `embeddings` | Celery queue for embedding jobs; `celery` runs them in `celery-worker` instead |
| `SEARCH_INDEX_COMMENTS` | No | `true` | Index comments for search |
| `SEARCH_MIN_COMMENT_LENGTH` | No | `20` | Comments shorter than this are not indexed |
| `SEARCH_EXCLUDE_BOT_COMMENTS` | No | `true` | Skip comments made by API keys or service accounts |
| `INSTALLED_PLUGINS` | No | `[]` | Plugins by dotted path (JSON array). A plugin can add password hashers and import sources |
| `PASSWORD_HASHERS` | No | `[]` | Extra password hasher classes by dotted path (JSON array) |
| `LEGACY_PASSWORD_ALGORITHMS` | No | `[]` | Imported password formats allowed to sign in, e.g. `["redmine_sha1"]` |
| `LEGACY_PASSWORD_HASHES_EXPIRE_AT` | No | — | After this time no imported password works, e.g. `2027-03-31T00:00:00Z` |

The last three are explained in the user guide under
[Configuration](https://specivo.io/docs/specivo/install/configuration/#imported-passwords).

### Semantic search

Semantic and hybrid search need the `multilingual-e5-small` embedding model, which is not in the
image or the release bundle. Download it into `$SPECIVO_DATA_DIR/models/` with
`docker compose exec api python -m specivo.cli.download_model` (or `make download-model`; pinned
revision, SHA-256 checked), then run
`docker compose exec api python -m specivo.cli.backfill_embeddings` (or `make backfill-embeddings`)
to index existing content. The `celery-embeddings` service embeds new content as it is saved. See
[Semantic search and embeddings](https://specivo.io/docs/specivo/install/semantic-search/) for
what is indexed when, offline setup, and troubleshooting.

### Container user

The app containers (`api`, the Celery services and anything you run with
`docker compose exec api ...`) run as the unprivileged user `specivo`, UID/GID `10001`. The
application files in the image are read-only for that user. In the data directory
(`/app/data`, i.e. `SPECIVO_DATA_DIR`) it owns only these subdirectories:

| Directory | Holds |
|-----------|-------|
| `attachments/` | Uploaded files |
| `avatars/` | Profile photos |
| `models/` | The embedding model |
| `logs/app/` | `specivo.log` and its rotations (earlier versions wrote `logs/specivo.log`) |
| `state/` | The one-time admin bootstrap file (mode 0700) |

The data directory itself, `logs/`, `postgresql/`, `redis/` and `logs/nginx/` are **not** the
app's. The data directory stays with root, or with the user who created it, and the app cannot
create, rename or replace entries in it. That matters because Docker mounts the host paths of
`postgresql/`, `redis/` and `logs/nginx/` into other containers. Celery beat keeps its schedule in
the app user's home (`/home/specivo/.local/state/specivo/`, mode 0700).

- **Ownership is set on start.** The one-shot `init-data` service runs as root before `api`. It
  creates the directories above when they are missing, gives them to `10001:10001`, and moves the
  installer's `.bootstrap.json` into `state/`. The image's entrypoint does the same when a container
  is started as root (`docker run --user 0 ...`) and then drops to `10001` with `setpriv`. The root
  step is careful about what it changes:
    - A directory that already belongs to `10001` is not walked again, so restarts stay fast.
    - It never follows a symlink. If one of these directories is a symlink, it warns and leaves it
      alone: mount the target there instead. It also skips a directory on another filesystem (for
      example a separate volume) or owned by another user; `chown` those yourself.
    - Inside a directory it changes only entries owned by root or by the data directory's owner,
      and only directories and regular files with a single link. A hard-linked file keeps its owner
      and is reported, because changing it would change every name it is linked under.
    - If an earlier 0.9.0 build gave the data directory or `logs/` to the app, it gives them back
      to root, and it removes world-write permission from the data directory.
    - It works through file descriptors (`specivo/ops/fix_ownership.py`): each entry is opened
      without following symlinks and checked again before its owner changes, so renaming or
      swapping entries while it runs cannot point it elsewhere.
    - It does not enter a directory the app user already owns, because a running app container
      could change it meanwhile. If such a directory holds files of root, it says so: stop all app
      containers and run `docker compose run --rm init-data fix-permissions --all`, which does
      enter them.
- **Run `init-data` with the app containers stopped.** `docker compose up` starts it before `api`,
  but on an upgrade the old `celery-worker`, `celery-embeddings` and `celery-beat` containers can
  still be running at that moment. Stop them first (see [Upgrade](#upgrade)).
- **Host directories you mount yourself** (another data path, extra theme or error-page
  directories, a directory of attachment files for `import_data --files-path`) must be readable by `10001`, and
  writable where the app writes to them. Give such a directory to the app user on the host:

  ```bash
  sudo chown -R 10001:10001 /srv/specivo-uploads
  ```

- **Running without root at all.** Set `user:` on the app services and the entrypoint skips the
  ownership step and runs as given; this also works with a read-only root filesystem. Create the
  app directories and give them to that user on the host first, because nothing will do it for
  you, e.g. `cd <SPECIVO_DATA_DIR> && sudo mkdir -p attachments avatars models state logs/app &&
  sudo chown 10001:10001 attachments avatars models state logs/app && sudo chmod 700 state`
  (plus `chown -R` for directories that already hold files). A minimal
  `docker-compose.override.yml`:

  ```yaml
  services:
    init-data:
      user: "10001:10001"   # not root: it leaves ownership alone and exits
    api:
      user: "10001:10001"
      read_only: true       # optional
      tmpfs: [/tmp]
    celery-worker:
      user: "10001:10001"
    celery-embeddings:
      user: "10001:10001"
    celery-beat:
      user: "10001:10001"
  ```

  With a read-only root filesystem the home directory is read-only too, so Celery beat keeps its
  schedule in a private directory under `/tmp` (hence the `tmpfs`), which starts over on every
  restart: the periodic jobs then run once soon after a restart, which is harmless. To keep it, mount
  a volume at `/home/specivo/.local/state/specivo` or set `CELERY_BEAT_SCHEDULE_FILE`.

  For a different UID, build the image with `--build-arg SPECIVO_UID=... --build-arg
  SPECIVO_GID=...`, or use that UID in `user:` and own the data directory accordingly.
- **Health check.** The image has a `HEALTHCHECK` that requests `/health/` (behind
  `STEALTH_PREFIX` when set) in the container that runs the web server; in the Celery containers it
  passes without probing.

### Using External Database

To use your own PostgreSQL instead of the bundled one:

1. Ensure extensions are installed: `CREATE EXTENSION IF NOT EXISTS ltree; CREATE EXTENSION IF NOT EXISTS vector;`
2. Set `DATABASE_URL` in `.env.local`
3. Remove `bundled-db` from `COMPOSE_PROFILES`

```env
# .env.local
DATABASE_URL=postgresql+asyncpg://specivo:password@your-db-host:5432/specivo
```

### Using External Redis

```env
# .env.local
REDIS_URL=redis://your-redis-host:6379/0
```

Remove `bundled-redis` from `COMPOSE_PROFILES`.

## Multiple instances on one host

You can run several Specivo instances on the same machine, each with its own containers, network, database, and port. This is useful for:

- Separate instances per team or department
- Staging + production on the same server
- Multi-tenant setups with isolated data

### How it works

Set `COMPOSE_PROJECT_NAME` to a unique value per instance. Docker Compose uses this to prefix all container names, networks, and volumes automatically. No container name conflicts.

### Example: three instances

```bash
# Copy the project for each instance
cp -r specivo specivo-engineering
cp -r specivo specivo-marketing
cp -r specivo specivo-staging
```

Each instance gets its own `.env`:

**specivo-engineering/.env:**
```env
COMPOSE_PROJECT_NAME=specivo-eng
SPECIVO_PORT=9933
DATABASE_URL=postgresql+asyncpg://specivo:specivo@db:5432/specivo_eng
POSTGRES_DB=specivo_eng
REDIS_URL=redis://redis:6379/0
```

**specivo-marketing/.env:**
```env
COMPOSE_PROJECT_NAME=specivo-mkt
SPECIVO_PORT=9934
DATABASE_URL=postgresql+asyncpg://specivo:specivo@db:5432/specivo_mkt
POSTGRES_DB=specivo_mkt
REDIS_URL=redis://redis:6379/0
```

**specivo-staging/.env:**
```env
COMPOSE_PROJECT_NAME=specivo-stg
SPECIVO_PORT=9935
DATABASE_URL=postgresql+asyncpg://specivo:specivo@db:5432/specivo_stg
POSTGRES_DB=specivo_stg
REDIS_URL=redis://redis:6379/0
```

Start each:
```bash
cd specivo-engineering && docker compose --profile bundled-db --profile bundled-redis up -d
cd specivo-marketing  && docker compose --profile bundled-db --profile bundled-redis up -d
cd specivo-staging    && docker compose --profile bundled-db --profile bundled-redis up -d
```

Each instance gets isolated containers:
```
specivo-eng-api-1, specivo-eng-nginx-1, specivo-eng-redis-1, ...
specivo-mkt-api-1, specivo-mkt-nginx-1, specivo-mkt-redis-1, ...
specivo-stg-api-1, specivo-stg-nginx-1, specivo-stg-redis-1, ...
```

### Shared external database

If you use an external PostgreSQL server, create a separate database for each instance:

```sql
CREATE DATABASE specivo_eng OWNER specivo;
CREATE DATABASE specivo_mkt OWNER specivo;
CREATE DATABASE specivo_stg OWNER specivo;

-- Each database needs extensions
\c specivo_eng
CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS vector;
-- repeat for other databases
```

Then point each instance's `DATABASE_URL` to its database, and remove `bundled-db` from `COMPOSE_PROFILES`.

### Shared external Redis

You can share one Redis server across instances by using different database numbers:

```env
# Instance 1
REDIS_URL=redis://redis-host:6379/0

# Instance 2
REDIS_URL=redis://redis-host:6379/1

# Instance 3
REDIS_URL=redis://redis-host:6379/2
```

Remove `bundled-redis` from `COMPOSE_PROFILES` for all instances.

### Reverse proxy for multiple instances

Put all instances behind one nginx with different subdomains:

```nginx
server {
    listen 443 ssl;
    server_name eng.specivo.example.com;
    location / { proxy_pass http://localhost:9933; }
}

server {
    listen 443 ssl;
    server_name mkt.specivo.example.com;
    location / { proxy_pass http://localhost:9934; }
}

server {
    listen 443 ssl;
    server_name staging.specivo.example.com;
    location / { proxy_pass http://localhost:9935; }
}
```

## SSL/TLS

### Option 1: Reverse Proxy (recommended)

Place Specivo behind your existing reverse proxy (Nginx, Caddy, Traefik):

```nginx
# Your main Nginx
server {
    listen 443 ssl;
    server_name specivo.example.com;

    ssl_certificate /etc/ssl/certs/specivo.pem;
    ssl_certificate_key /etc/ssl/private/specivo.key;

    location / {
        proxy_pass http://localhost:9933;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # WebSocket support
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

### Option 2: Caddy (auto-HTTPS)

```Caddyfile
specivo.example.com {
    reverse_proxy localhost:9933
}
```

## Backup

### Database

```bash
# Dump
docker compose exec db pg_dump -U specivo specivo > backup.sql

# Restore
docker compose exec -T db psql -U specivo specivo < backup.sql
```

### Data Directory

Back up the entire `SPECIVO_DATA_DIR`:

```bash
tar czf specivo-backup-$(date +%Y%m%d).tar.gz specivo-data/
```

## Upgrade

### Online

```bash
# Pull new image
docker compose pull

# Stop the app containers, so none runs while init-data prepares the data directory
docker compose stop api celery-worker celery-embeddings celery-beat

# Start (init-data prepares the app directories, the entrypoint runs migrations)
docker compose up -d
```

Since 0.9.0 the containers run as UID `10001` instead of root. Take the new
`docker-compose.yml` with the upgrade: on the first start its `init-data` service gives the app
directories, with the files an older version wrote as root, to that user. See [Container user](#container-user).

### Offline

```bash
# Load new image from bundle
docker load < specivo-image.tar

# Stop the app containers, then start
docker compose stop api celery-worker celery-embeddings celery-beat
docker compose up -d
```

### Release-specific steps

Some releases need a step after the containers start, such as rebuilding the search index.
The user guide lists them per release under
[Upgrading](https://specivo.io/docs/specivo/install/upgrading/).

### Pin Version

```env
SPECIVO_VERSION=0.8.0
```

## Admin Management

```bash
# Create admin user
make create-admin login=admin email=admin@example.com password=secret

# Reset password
make reset-password login=admin password=newpassword
```

Accounts imported with their old passwords can be inspected and retired with the same command
(see [Migrating from Redmine](https://specivo.io/docs/specivo/install/migrating-from-redmine/)):

```bash
# Registered password hashers, whether each is enabled, and how many accounts use it
docker compose exec api python -m specivo.cli.admin password-hashers

# Accounts that still hold an imported password hash
docker compose exec api python -m specivo.cli.admin legacy-passwords

# Make them use password recovery from now on
docker compose exec api python -m specivo.cli.admin legacy-passwords expire --yes
```

## Troubleshooting

### Database connection failed

Check that PostgreSQL is running and the extensions are installed:

```bash
docker compose exec db psql -U specivo -d specivo -c "SELECT * FROM pg_extension;"
```

### Migrations failed

Run migrations manually:

```bash
docker compose exec api alembic upgrade head
```

### Container health check failing

Check logs:

```bash
docker compose logs api --tail 50
```

### Port conflict

Change the port in `.env`:

```env
SPECIVO_PORT=8080
```
