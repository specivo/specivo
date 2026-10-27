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
docker compose stop api celery-worker celery-embeddings celery-beat
docker compose up -d
```

`pull` fetches the new image; `stop` stops the app containers so that none is running while the
one-shot `init-data` service prepares the data directory; `up -d` recreates the containers. On startup the entrypoint waits for
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

## Upgrading to 0.10.0

Version 0.10.0 adds single sign-on and three migrations (0041 to 0043), which the entrypoint applies
on startup. Nothing has to be configured to keep working as before: single sign-on is off until you
add a provider, and people who are signed in stay signed in. Check the following.

### The bundled stack gets a `proxy` network

Take the new `docker-compose.yml`. It puts `nginx` and `api` on a new network, `proxy`, with nginx at
the fixed address `10.213.47.2`, so that Specivo can trust exactly that address when it reads the
client's address from `X-Forwarded-For` (see
[Client addresses behind a proxy](configuration.md#client-addresses-behind-a-proxy)).

- If `docker compose up -d` stops with "Pool overlaps", the range is already used on your host. Set
  `SPECIVO_PROXY_SUBNET` and `SPECIVO_NGINX_IP` in `.env` to a free range and address, then run it
  again.
- If you keep your own Compose file, add the network the same way, or set `TRUSTED_PROXIES` to the
  address of your proxy. With `TRUSTED_PROXIES` empty, the address of the connection is the client:
  behind a proxy every person then shares one rate-limit bucket and one address in the audit log.
- If another proxy (Caddy, Traefik, a CDN) sits in front of the bundled nginx, set
  `TRUSTED_PROXY_HOPS=2`. Only the proxies you list in `TRUSTED_PROXIES` can name the client, and
  `TRUSTED_PROXY_HOPS` says how many there are in a row.
- If you replaced `nginx/default.conf` with your own, keep
  `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`. You can also take over the new
  `specivo_no_query` access log format from the bundled file: the access log no longer contains
  query strings, which can carry the code and state of a single sign-on sign-in and one-time tokens
  from links. Delete or rotate old access logs if you want those gone.

### Signing out changed

- The **Sign out** button is a form that posts to `/logout/`. A bookmark or link to `/logout/` opens
  a "Sign out?" page and does not sign anyone out.
- `POST /api/v1/auth/logout/` answers `200` with `{"end_session_url": ...}` instead of `204`. The
  value is `null` unless the session came from a single sign-on provider that supports signing out at
  the provider. A script that checks for exactly `204` must accept `200`.
- Signing out now ends the session on the server and revokes its access token at once. Before, both
  stayed valid until they expired.
- A password reset, by email, by an administrator or from the command line, ends every session of
  the account.

### Administrator API keys do less

An administrator's API key is refused with `403` and the code `api_key_not_allowed` for resetting
passwords, unlocking or reactivating users, creating people or administrators (service accounts are
still allowed), creating keys for other users and changing single sign-on settings. Automation that
did any of this with a key must run as a signed-in administrator instead. See
[What an administrator's key cannot do](../ai-agents/api-keys.md#what-an-administrators-key-cannot-do).

### Deactivated users

Administrators can deactivate and reactivate users (**Administration → Users**, on the user's page).
Deactivating ends every session and switches off the user's API keys; reactivating does not switch
the keys back on.

### If you turn on single sign-on

- Set `APP_URL` first: the redirect and logout addresses are built from it.
- Client secrets stored in Specivo are encrypted with a key derived from `SECRET_KEY`. If you rotate
  `SECRET_KEY`, open each provider and enter its client secret again. Back up `.env.local` with the
  database dump; see [Backup & restore](backup-restore.md#what-depends-on-secret_key).
- Register the redirect URI, and the back-channel logout URI if you use it, at your identity
  provider. See [Single sign-on](single-sign-on/index.md).
- Keep a way back in: [Getting back in](configuration.md#single-sign-on-getting-back-in).

The migrations also remove the unused `github_id` and `google_id` columns of the users table.

## Upgrading to 0.9.0

Version 0.9.0 adds eight migrations (0033 to 0040), which the entrypoint applies on startup like any
other. Most of it needs no action, but a few things are worth doing or checking afterwards.

### The containers no longer run as root

The `api` and Celery containers, and the commands you run with `docker compose exec api ...`, now
run as the unprivileged user `specivo` (UID/GID `10001`). Take the new `docker-compose.yml`: its
`init-data` service runs once as root before `api` starts. It gives the app its own subdirectories
of the data directory, `attachments/`, `avatars/`, `models/`, `state/` and `logs/app/`, including
the files an older version wrote there as root, and moves `.bootstrap.json` into `state/` if it is
still there. On the first start this can take a moment on a large attachments folder; later starts
skip directories that already belong to the app.

- Stop the app containers before `docker compose up -d`, as in
  [Pull and restart](#2-pull-and-restart): `init-data` must not run while an old `celery-worker`,
  `celery-embeddings` or `celery-beat` container is still up.
- It does not enter directories the app already owns. If it reports one that holds files of root,
  stop all app containers and run `docker compose run --rm init-data fix-permissions --all`.

- The data directory itself stays with root (or with you), as do `logs/`, `postgresql/`, `redis/`
  and `logs/nginx/`: the app cannot change entries at the top of the data directory.
- The app's log file moves from `logs/specivo.log` to `logs/app/specivo.log`. The old file is left
  where it is; delete it when you no longer need it.
- If one of the app directories is a symlink, sits on another filesystem or belongs to another
  user, `init-data` leaves it alone and logs a warning. Mount the target at that path instead of
  symlinking, or run `sudo chown -R 10001:10001` on it yourself.
- If you keep your own Compose file, add the `init-data` service and make `api` depend on it, or
  prepare the directories yourself before you start the new image:
  `cd <SPECIVO_DATA_DIR> && sudo mkdir -p attachments avatars models state logs/app` and
  `sudo chown -R 10001:10001 attachments avatars models state logs/app`.
  Without that the api logs a warning at start and uploads fail.
- Other host directories mounted into the app containers (custom theme or error-page folders, a
  Redmine `files` directory for the importer) must be readable by UID `10001`.
- To run with no root step at all, set `user: "10001:10001"` on `init-data`, `api` and the Celery
  services in `docker-compose.override.yml`; `init-data` then changes nothing. The image also
  works with a read-only root filesystem (`read_only: true` plus a `tmpfs` for `/tmp`).
- The image now has its own `HEALTHCHECK`, so `docker ps` shows the health of every Specivo
  container.

### Rebuild the search index

Searching for an issue key such as `ACME-42` now works in every search mode. Older text has to be
re-indexed before a *mention* of a key in it is found, so the migration flags the index as needing a
rebuild. Until you run it, an issue is still found by its own key. Rebuild it once:

- in the web UI, under **Administration → Settings**, with **Reindex now**; or
- from the command line:

  ```bash
  docker compose exec api python -m specivo.cli.reindex_fts
  ```

The reindex runs in small batches, so Specivo stays usable while it works. Text edited after the
upgrade is indexed as you save it. The notice next to the button reads "Search language changed",
which is the same flag the language setting uses; it clears when the reindex finishes.

### Comments become searchable, and indexing follows every change

Before 0.9.0 a comment could not be found by any search until someone ran the embedding backfill,
and edited issues and wiki pages kept their old text in semantic search. Now every change is
indexed as it is saved: keyword search sees it at once, and a new `celery-embeddings` container adds
the embeddings for semantic search a few seconds later. See
[How content is indexed](semantic-search.md#how-content-is-indexed).

- Migration 0037 makes existing comments searchable by keyword. It follows your
  `SEARCH_INDEX_COMMENTS`, `SEARCH_MIN_COMMENT_LENGTH` and `SEARCH_EXCLUDE_BOT_COMMENTS` settings.
  Nothing to do.
- Take the new `docker-compose.yml`: it adds the `celery-embeddings` service, which runs the
  embedding jobs in one process so the model is loaded once (`EMBEDDING_WORKER_CONCURRENCY`, default
  `1`). If you keep your own Compose file, add the service, or set `EMBEDDING_QUEUE=celery` so
  `celery-worker` runs the jobs. It mounts the same data directory, so the model files need no
  moving.
- If you use semantic search, run the backfill once after upgrading so the comments and the edits
  made before the upgrade get embeddings too. It also indexes every attachment (including those
  without a description and those on wiki pages) and drops the chunks of comments your settings
  exclude. Without the model files it writes keyword chunks only and says so, which is harmless:

  ```bash
  docker compose exec api python -m specivo.cli.backfill_embeddings
  ```

  A scheduled backfill, if you set one up, is no longer needed.
- `make download-model`, `make backfill-embeddings`, `make reindex-fts` and `make import-data` now
  run in the `api` container of whichever stack the directory has, so they work from a release
  bundle too. The model download is now `python -m specivo.cli.download_model`, which checks
  checksums; the release bundle's `configure.py` moved to `scripts/configure.py`.

### Semantic search gets a vector index

Migration 0038 builds an HNSW index on the stored embeddings, so semantic and hybrid search no
longer compare the query with every chunk (see [Vector index](semantic-search.md#vector-index)).

- The index is built with `CREATE INDEX CONCURRENTLY`: Specivo keeps working while it builds, but
  the migration, and so the start of the `api` container, waits until it is done. Expect seconds
  for a few tens of thousands of chunks (about 30 seconds for 100,000 on a laptop) and minutes for
  millions. Count yours first with
  `docker compose exec db psql -U specivo -d specivo -c "SELECT count(*) FROM chunk_embeddings"`.
- Building is much faster when the index fits in PostgreSQL's `maintenance_work_mem` (64 MB by
  default; budget roughly 2 KB per chunk). PostgreSQL logs a notice when it does not fit. On a large
  install, raise it for the upgrade, for example with
  `ALTER SYSTEM SET maintenance_work_mem = '1GB'; SELECT pg_reload_conf();`, and set it back after.
- It needs pgvector 0.8 or later to be used, which the bundled `pgvector/pgvector:pg18` image has. On
  an external database, check with `SELECT extversion FROM pg_extension WHERE extname = 'vector'`.
- If the build is interrupted, the next start drops the half-built index and builds it again.

### Only the bundled embedding model

The administrator API can no longer register embedding models: only the bundled local model,
`multilingual-e5-small`, is supported, and hosted (bring-your-own-key) providers are not. Migration
0039 keeps a single default model; if an older release left more than one, it keeps the local model
as the default. If the default is a model with another provider, semantic search stays off and the
logs say so once; see [Changing the model](semantic-search.md#changing-the-model) to put the local
model back.

### Check time tracking permissions

Reading time entries now needs the **View time entries** permission in the project, and logging
time needs **Log time**. Before 0.9.0, anyone who could open a project could read its time entries.
After the upgrade:

- **Reporter** and the built-in **Non member** role no longer see any time. Add **View time
  entries** to a role if its people should see it.
- **Agent** is granted **View time entries** by the migration, so service accounts can read back
  the time they log. This applies only to the role still named *Agent*. If you renamed it, or
  made your own role for agents, grant the permission yourself.
- **Manager**, which holds every permission, and **Developer** are unchanged.
- Any custom role that has **Log time** but not **View time entries** can log time and will not see
  it afterwards. Review your custom roles under **Administration → Roles**.

See [Time tracking](../projects/time-tracking.md#who-can-do-what).

### New role and tracker settings

**Administration → Roles** and **Administration → Trackers** are new. A role now has a *Users
visibility* and a *Time entries visibility* setting, and a tracker can be **Private by default**.
Every existing role and tracker starts with the values that keep today's behaviour (everyone
visible, all time entries visible, not private), so nothing changes until you edit one. See
[What a role can see](../projects/index.md#what-a-role-can-see) and
[Private by default](../issues/creating.md#private-by-default).

### Scripts that use browser cookies need a CSRF token

Calls authenticated by the sign-in cookie must now send the `X-CSRF-Token` header on
change-password, logout, logout-all, session revoke, and any other state-changing request under
`/api/v1/auth/`, except the four that are meant to work without a session: login, token refresh,
forgot-password and reset-password. The value is the `csrf_token` cookie Specivo sets on any `GET`.
A request without it is refused with `403 CSRF validation failed`. The web UI sends the header for
you.

Nothing changes for clients that use an API key or an `Authorization: Bearer` header, which is how
scripts and agents should authenticate anyway.

### Longer Markdown is refused

An issue description, comment, wiki page, version description or recurring-issue description can
now hold at most **262,144 characters**. Saving more is refused with a message giving the limit.
Content already stored that is longer, such as imported content, is kept as it is and shown as plain
text. Text that would take too long to format is also shown as plain text. See
[Size and complexity limits](../reference/markdown.md#size-and-complexity-limits).

### Search paging is capped

The REST search endpoint refuses an `offset` above 1000 with a `422`, and the MCP search tool
accepts a `limit` of 1 to 50. Clients that page deeper than that need to narrow the query instead.
The web search page clamps the offset rather than failing.

### Import command renamed

`python -m specivo.cli.import_redmine` is now `python -m specivo.cli.import_data --source redmine`,
with the same options. `make import-redmine` still works and runs the new command. Update any
script or scheduled job that calls the old module by name. `--list-sources` shows which sources
are installed, which can include ones added by a plugin. See
[Migrating from Redmine](migrating-from-redmine.md) and
[Writing an import source plugin](import-source-plugins.md).

The Redmine import also carries over anonymous and non-member access, a role's visibility settings
and a tracker's private default. It never turns the instance-wide anonymous access switch on.

### Other changes you may notice

- A file attached to an issue you cannot see now answers "not found" instead of downloading, and a
  project's shared versions are hidden from people who cannot open that project.
- In Markdown, a bare filename such as `![chart](chart.png)` links to the attachment of that name.
  See [Link to an attachment by filename](../issues/attachments.md#link-to-an-attachment-by-filename).
- Imported accounts can keep their old passwords. This is off until you list an algorithm in
  `LEGACY_PASSWORD_ALGORITHMS`; see [Configuration](configuration.md#imported-passwords).
