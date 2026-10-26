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
python3 scripts/configure.py

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

## Hybrid search (embedding model)

Specivo supports hybrid search (keyword + semantic) using a local embedding model,
`multilingual-e5-small`. No API keys are required; the model runs on the CPU. It is not included
in the image or in this bundle, so you download it once (about 487 MB). Without it, search falls
back to keyword matching and everything else works normally.

The files live in `specivo-data/models/` (your `SPECIVO_DATA_DIR`): `multilingual-e5-small.onnx`
and `tokenizer.json`.

### Online setup

With the stack running, download the model from inside the `api` container. It fetches a pinned
revision and checks each file's SHA-256:

```bash
docker compose exec api python -m specivo.cli.download_model    # or: make download-model
```

No restart is needed. Then index the content you already have:

```bash
docker compose exec api python -m specivo.cli.backfill_embeddings    # or: make backfill-embeddings
```

It reports how many items it chunked and how many chunks it embedded. If the model files are
missing it still writes the keyword-search chunks and says that semantic search stays off. After
that, new and edited content is indexed as it is saved; the `celery-embeddings` container adds the
embeddings a few seconds later, using one copy of the model in memory.

### Air-gapped / offline setup

On a machine with internet, run `bash scripts/download-model.sh ./models` (from this bundle; needs
`curl` and `sha256sum` or `shasum`). Copy the `models` folder to `specivo-data/models/` on the
target machine and run the backfill command.

### Without the embedding model

Search still works — semantic and hybrid modes give keyword results (PostgreSQL full-text search).

Full details: https://specivo.io/docs/specivo/install/semantic-search/

## Next steps

- See [docs/deployment.md](deployment.md) for full configuration reference
- Change the port: edit `SPECIVO_PORT` in `.env`
- Enable email: set `SMTP_*` variables in `.env.local`
