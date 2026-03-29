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
python3 configure.py

# Start
docker compose up -d
```

The bundle includes:
- Pre-built Docker image (`specivo-image.tar`)
- Docker Compose configuration
- Nginx reverse proxy config
- Setup wizard (`configure.py`)
- Container entrypoint script
- Installation guide

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
| **api** | specivo/specivo | FastAPI application server |
| **celery-worker** | specivo/specivo | Async task processing (emails, embeddings, webhooks) |
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

# Restart (entrypoint runs migrations automatically)
docker compose up -d
```

### Offline

```bash
# Load new image from bundle
docker load < specivo-image.tar

# Restart
docker compose up -d
```

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
