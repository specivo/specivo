---
description: Install Specivo with Docker — clone the repo, run docker compose up, create the first admin, and sign in at port 9933.
---

# Quick start with Docker

This is the recommended way to run Specivo. You clone the repo (for the compose file and nginx
config), bring the stack up, and create your first admin user. The application image is pulled from
Docker Hub, so there is nothing to build.

## 1. Clone and start

```bash
git clone https://github.com/specivo/specivo.git
cd specivo
docker compose up -d
```

`docker compose up -d` pulls `specivo/specivo:latest` (and the Postgres, Redis, and nginx images),
then starts every service in the background. The first run takes a moment while images download.

You can watch it come up:

```bash
docker compose ps
docker compose logs -f api
```

!!! note "Migrations and seed data run for you"
    The API container's entrypoint waits for the database, applies all migrations, and seeds the
    default trackers, statuses, priorities, and metadata presets. You do **not** run `migrate` or
    `seed` by hand on the Docker path — by the time the `api` service is healthy, the database is
    ready.

## 2. Create the first admin user

There is no public sign-up for the very first account — you create it from the command line:

```bash
docker compose exec api python -m specivo.cli.admin create \
  --login admin --email you@example.com --password 'choose-a-strong-one'
```

Use a real address you control and a strong password. This account can then create projects, invite
other users, and manage everything from the UI.

## 3. Sign in

Open **http://localhost:9933** (or `http://your-specivo-host:9933`) and sign in with the login and
password you just set. From here, follow
[Your first 10 minutes](../getting-started/first-10-minutes.md) to create a project and your first
issue.

## Before you go to production

The defaults are tuned for a quick local start, not for a public deployment. Two things to change:

!!! warning "Set a real SECRET_KEY"
    The bundled default key is insecure and is meant only for local trials. Before you expose
    Specivo to anyone, set a strong, unique `SECRET_KEY` in `.env.local`. See
    [Configuration](configuration.md#secrets-envlocal).

!!! tip "Pin your version"
    `latest` is convenient for trying things out, but for a real deployment pin a specific release
    so upgrades are deliberate. Set `SPECIVO_VERSION` in `.env` (for example `SPECIVO_VERSION=0.1.10`)
    and re-run `docker compose up -d`.

When you are ready to harden the install, work through [Configuration](configuration.md) — it covers
secrets, registration mode, CORS, SMTP, and pointing at an external database.

## Stopping and restarting

```bash
docker compose stop      # stop the stack, keep data
docker compose up -d     # start it again
docker compose down      # stop and remove containers (data in SPECIVO_DATA_DIR is kept)
```

Your data lives in `SPECIVO_DATA_DIR` (default `./specivo-data`), so `down` does not erase it. See
[Backup & restore](backup-restore.md) before you rely on the install.
