---
description: What you need to run Specivo — a host with Docker, the bundled Postgres, Redis, nginx, and Celery, and the two supported install paths.
---

# Overview & requirements

Specivo is self-hosted: you run it on your own server and own the data. This page covers what
the software needs and the two supported ways to install it.

## What you need

- A Linux host (a small cloud VM or your own server is fine) with **Docker** and the **Docker
  Compose** plugin installed.
- Roughly **2 CPU cores** and **2–4 GB RAM** for a small team. Semantic search and the embedding
  model are the heaviest parts; without them Specivo runs comfortably at the lower end.
- A few GB of disk for the database, attachments, and logs. Storage grows with your attachments.

That is the whole list. Everything Specivo needs at runtime runs inside containers — there is no
external search service, message queue appliance, or managed database to stand up first.

## How the pieces fit together

A default install runs a handful of containers, all defined in the `docker-compose.yml` you get
when you clone the repo:

| Service | Image | Role |
|---|---|---|
| nginx | `nginx:1-alpine` | Front door. Listens on `SPECIVO_PORT` (default **9933**) and proxies to the API. |
| api | `specivo/specivo` | The application. Listens on port **8000** inside the network, behind nginx. |
| db | `pgvector/pgvector:pg18` | **PostgreSQL 18** with pgvector for full-text and semantic search. |
| redis | `redis:7-alpine` | Cache and Celery broker. |
| celery-worker / celery-beat | `specivo/specivo` | Background jobs (search indexing, scheduled tasks). |

You reach Specivo at `http://your-specivo-host:9933`. Nothing else is exposed by default.

!!! note "Search runs inside PostgreSQL"
    Both keyword and semantic search use PostgreSQL (tsvector + pgvector). There is no
    Elasticsearch or external search service to operate.

## Pick an install path

**Docker with the published image — recommended.**
The fastest path for almost everyone. You clone the repo for the compose file and nginx config,
then pull the `specivo/specivo` image from Docker Hub. See
[Quick start with Docker](docker.md).

**Air-gapped / offline.**
For hosts with no outbound internet. You pull and save the images on a connected machine, transfer
them, and load them on the offline host. See [Air-gapped install](air-gapped.md).

## From source, for evaluation or contributing

If you want to read the code, hack on Specivo, or evaluate it locally, there is a from-source dev
setup. It needs [uv](https://docs.astral.sh/uv/):

```bash
make install    # install dependencies
make dev-up     # run the dev server with hot-reload
```

This path is for evaluating and contributing. For anything you actually rely on, use the Docker
path above.

## Semantic search is optional

Keyword search and the rest of Specivo work out of the box. Semantic ("by meaning") search needs an
embedding model. You can use the bundled on-device model (runs on CPU, no external calls) or bring
your own provider key (BYOK). Either way it is opt-in — see
[Configuration](configuration.md#semantic-search-model) for how to enable it.

## Next steps

- [Quick start with Docker](docker.md) — get a working instance in a few minutes.
- [Configuration](configuration.md) — environment variables, external database, SMTP, secrets.
- [Backup & restore](backup-restore.md) — protect your data before you depend on it.
