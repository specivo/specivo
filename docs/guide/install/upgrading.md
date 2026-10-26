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

## Upgrading to 0.9.0

Version 0.9.0 adds four migrations (0033 to 0036), which the entrypoint applies on startup like any
other. Most of it needs no action, but a few things are worth doing or checking afterwards.

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
