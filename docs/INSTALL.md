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

## Hybrid search (embedding model)

Specivo supports hybrid search (keyword + semantic) using a local embedding model.
No API keys required — the model runs locally inside the Celery worker container.

### Online setup

```bash
make download-model
```

This downloads `multilingual-e5-small` (~393 MB) to `data/models/`.
After downloading, restart the application and backfill existing data:

```bash
make dev-up           # or: docker compose up -d
make backfill-embeddings
```

Hybrid search is now active. New issues and wiki pages are embedded automatically.

### Air-gapped / offline setup

If you cannot download from the internet:

1. On a machine with internet, download the model files:
   - `https://huggingface.co/intfloat/multilingual-e5-small/resolve/main/onnx/model.onnx` → rename to `multilingual-e5-small.onnx`
   - `https://huggingface.co/intfloat/multilingual-e5-small/resolve/main/tokenizer.json`
   - `https://huggingface.co/intfloat/multilingual-e5-small/resolve/main/tokenizer_config.json`
   - `https://huggingface.co/intfloat/multilingual-e5-small/resolve/main/special_tokens_map.json`
   - `https://huggingface.co/intfloat/multilingual-e5-small/resolve/main/sentencepiece.bpe.model`

   Or download `specivo-models.tar.gz` from the GitHub releases page.

2. Copy the files to `specivo-data/models/` on the target machine:
   ```bash
   mkdir -p specivo-data/models
   cp multilingual-e5-small.onnx tokenizer.json tokenizer_config.json \
      special_tokens_map.json sentencepiece.bpe.model specivo-data/models/
   ```

3. Restart and backfill:
   ```bash
   docker compose up -d
   make backfill-embeddings
   ```

### Without the embedding model

Search still works — it falls back to keyword-only mode (PostgreSQL full-text search).
Hybrid and semantic modes require the embedding model.

## Next steps

- See [docs/deployment.md](deployment.md) for full configuration reference
- Change the port: edit `SPECIVO_PORT` in `.env`
- Enable email: set `SMTP_*` variables in `.env.local`
