---
description: Upgrade Specivo — bump SPECIVO_VERSION, pull, and restart. Migrations run automatically. Back up first and roll back by pinning the previous tag.
---

# Upgrading

Upgrading Specivo means moving to a newer `specivo/specivo` image. The container entrypoint applies
any new database migrations on startup, so an upgrade is usually two commands.

!!! warning "Back up before every upgrade"
    Migrations can change the database schema and are not always reversible. Take a database dump
    and back up `SPECIVO_DATA_DIR` **before** you pull a new image. See
    [Backup & restore](backup-restore.md). If anything goes wrong, that backup is your way back.

## 1. Choose the target version

Pin the release you want in `.env` so the upgrade is deliberate:

```bash
# .env
SPECIVO_VERSION=0.1.11
```

Using `SPECIVO_VERSION=latest` always pulls the newest published image. That is fine for trials, but
for a deployment you rely on, pin an explicit tag so you know exactly what is running.

## 2. Pull and restart

```bash
docker compose pull
docker compose up -d
```

`pull` fetches the new image; `up -d` recreates the containers. On startup the entrypoint waits for
the database and applies any new migrations automatically — you do not run a migrate step yourself.

Watch the API logs to confirm a clean start:

```bash
docker compose logs -f api
```

When the `api` service reports healthy, the upgrade is done.

## Rolling back

If the new version misbehaves, roll back by pinning the previous tag and restarting:

```bash
# .env
SPECIVO_VERSION=0.1.10      # the version you were on before

docker compose pull
docker compose up -d
```

!!! danger "A schema migration may require restoring your backup"
    If the failed upgrade already applied a migration, the database schema may be ahead of the older
    image and the rollback alone will not be enough. In that case, restore the database dump and
    `SPECIVO_DATA_DIR` you took before the upgrade, then start the older image. This is exactly why
    the pre-upgrade backup is mandatory.

## Tips

- Upgrade one minor version at a time on production rather than jumping many releases at once.
- Read the release notes for the version you are moving to before you pull.
- After upgrading, do a quick smoke test: sign in, open an issue, run a search.
