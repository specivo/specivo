# Specivo — Installation guide

## Requirements

- Docker 24+ with Docker Compose V2
- 2 GB RAM minimum

## Online installation

```bash
git clone https://github.com/specivo/specivo.git
cd specivo
make configure
make up
```

Open http://localhost:9933

## Offline installation

If you received this as a bundle (specivo-X.Y.Z-bundle.tar.gz):

```bash
# 1. Extract
tar xzf specivo-*-bundle.tar.gz

# 2. Load the Docker image
docker load < specivo-image.tar

# 3. Configure
python3 configure.py

# 4. Start
docker compose up -d
```

Open http://localhost:9933

## What happens on first start

1. PostgreSQL and Redis start
2. Database migrations run automatically
3. Default data (roles, statuses, priorities) is seeded
4. Admin account is created (from your configure answers)
5. API starts on port 8000, nginx proxies on port 9933

## Multiple instances on one host

You can run several Specivo instances on the same machine (one per team, one for staging, etc.). Each instance gets isolated containers, networks, and data.

```bash
# Clone a second copy
cp -r specivo specivo-marketing
cd specivo-marketing

# Set a unique instance name and port
echo "COMPOSE_PROJECT_NAME=specivo-marketing" >> .env
echo "SPECIVO_PORT=9935" >> .env
echo "POSTGRES_DB=specivo_marketing" >> .env

# Start
docker compose --profile bundled-db --profile bundled-redis up -d
```

Each instance uses its own:
- Container names (auto-prefixed with the project name)
- Docker network (isolated)
- Data volumes (separate databases)
- External port

## Using an external database

To use an existing PostgreSQL server instead of the bundled one:

```bash
# In .env — remove bundled-db from profiles
COMPOSE_PROFILES=bundled-redis

# In .env.local — set your database URL
DATABASE_URL=postgresql+asyncpg://user:password@dbhost:5432/specivo
```

Make sure the database has the `ltree` and `vector` extensions:
```sql
CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS vector;
```

## Next steps

- See [docs/deployment.md](deployment.md) for full configuration reference
- Change the port: edit `SPECIVO_PORT` in `.env`
- Enable email: set `SMTP_*` variables in `.env.local`
