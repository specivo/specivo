---
description: Install Specivo on a host with no internet — pull and save the images on a connected machine, transfer them, load them, and run docker compose up.
---

# Air-gapped install

You can run Specivo on a host with no outbound internet. The idea is simple: do the downloading on a
connected machine, carry everything across, and load it on the offline host. At runtime Specivo
needs no internet at all.

## What you need to transfer

Two things move from the connected machine to the offline host:

1. The **container images**, saved as a single tar file.
2. The **cloned repo**, which contains `docker-compose.yml`, the nginx config, and `.env.example`.

## 1. On a connected machine — pull and save the images

Pull the four images Specivo uses. Pin `specivo/specivo` to the release you intend to run instead of
`latest`, so the offline host gets a known version.

```bash
TAG=0.1.10   # the Specivo release you want

docker pull specivo/specivo:$TAG
docker pull pgvector/pgvector:pg18
docker pull redis:7-alpine
docker pull nginx:1-alpine
```

Save them all into one archive:

```bash
docker save \
  specivo/specivo:$TAG \
  pgvector/pgvector:pg18 \
  redis:7-alpine \
  nginx:1-alpine \
  -o images.tar
```

Also clone the repo so you have the compose file and nginx config to carry over:

```bash
git clone https://github.com/specivo/specivo.git
```

## 2. Transfer to the offline host

Copy both `images.tar` and the cloned `specivo/` directory to the offline host by whatever means you
use to cross the air gap (removable media, an internal file share, and so on).

## 3. On the offline host — load and start

```bash
docker load -i images.tar
```

In the repo directory, pin the version you transferred so Compose uses your local image instead of
trying to pull:

```bash
# .env
SPECIVO_VERSION=0.1.10
```

Then bring the stack up:

```bash
docker compose up -d
```

Create the first admin user and sign in exactly as on a normal install:

```bash
docker compose exec api python -m specivo.cli.admin create \
  --login admin --email you@example.com --password 'choose-a-strong-one'
```

Open `http://your-specivo-host:9933` and sign in.

!!! note "Migrations still run automatically"
    The entrypoint applies migrations and seeds defaults from inside the loaded image. There is
    nothing to download for this step.

## Semantic search offline

The bundled embedding model is normally fetched over the network. To use semantic search on an
air-gapped host, pre-fetch the model on the connected machine and copy it into `SPECIVO_DATA_DIR`
on the offline host (default `./specivo-data`). Once the file is in place, Specivo loads it locally
and runs the model on CPU — no external calls.

Without the model, keyword search and the rest of Specivo work normally; only meaning-based search
is unavailable.

!!! tip "No runtime internet required"
    Once the images are loaded and the optional model is in place, Specivo needs no outbound
    internet. The only time you go online again is to fetch a newer image for an
    [upgrade](upgrading.md) — repeat the pull/save/transfer/load cycle with the new tag.
