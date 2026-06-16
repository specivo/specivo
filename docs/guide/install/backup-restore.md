---
description: Back up and restore Specivo — dump the PostgreSQL database, back up the data directory, restore both, and schedule regular backups.
---

# Backup & restore

A complete Specivo backup is two parts: a **database dump** (all your issues, wiki pages, comments,
and history) and the **data directory** (attachments and the bundled database files). Back up both
together so they stay consistent.

## Back up

### 1. Dump the database

```bash
docker compose exec db pg_dump -U specivo specivo > backup.sql
```

This writes a plain-SQL dump of the whole database to `backup.sql` on the host. If you changed
`POSTGRES_USER` or `POSTGRES_DB` in your `.env`, use those values instead of `specivo`.

### 2. Back up the data directory

`SPECIVO_DATA_DIR` (default `./specivo-data`) holds attachments, logs, themes, and the bundled
Postgres files. Archive it:

```bash
tar czf specivo-data-backup.tar.gz specivo-data
```

Store `backup.sql` and `specivo-data-backup.tar.gz` together, off the host, with a timestamp in the
name so you can tell snapshots apart.

!!! tip "Quiesce for a fully consistent snapshot"
    For the most consistent backup, take the database dump and the data-directory archive close
    together. For a strict point-in-time snapshot, stop the stack first (`docker compose stop`),
    archive, then start it again.

## Restore

Restoring means loading the dump into the database and putting the data directory back.

### 1. Restore the data directory

With the stack stopped, replace the data directory from your archive:

```bash
docker compose down
tar xzf specivo-data-backup.tar.gz      # restores ./specivo-data
```

### 2. Restore the database

Bring the database service up and load the dump into it:

```bash
docker compose up -d db
docker compose exec -T db psql -U specivo specivo < backup.sql
```

The `-T` flag is required so the dump streams in over stdin. Once the import finishes, start the
rest of the stack:

```bash
docker compose up -d
```

Sign in and confirm your issues, wiki pages, and attachments are all present.

!!! warning "Restore onto a matching version"
    Restore a dump into the **same Specivo version** it came from (the same `SPECIVO_VERSION`). The
    database schema must match the running image. If you need to move data to a newer version,
    restore on the original version first, confirm it works, then [upgrade](upgrading.md) — that
    runs the migrations in the right order.

## Schedule regular backups

Run the database dump on a schedule (for example a nightly cron job) and keep several days of
history off the host. A backup you have never tested is not a backup — periodically practice a
restore on a throwaway instance so you know the process works before you need it.
