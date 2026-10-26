---
description: Configure Specivo — .env vs .env.local, the key environment variables, external database and Redis, the data directory, SMTP, and the embedding model.
---

# Configuration

Specivo is configured with environment variables. The Docker stack reads two files from the repo
root, and neither is required to start — sensible defaults apply until you override them.

## `.env` vs `.env.local`

- **`.env`** holds non-secret settings: ports, the image version, the data directory, registration
  mode. It is safe to commit to your own infrastructure repo.
- **`.env.local`** holds secrets — most importantly `SECRET_KEY`. Keep it out of version control.

Start from the example file:

```bash
cp .env.example .env
```

Or let the helper generate both files (including a strong `SECRET_KEY`):

```bash
make configure          # or: python scripts/configure.py
```

After changing either file, apply it with:

```bash
docker compose up -d
```

## Key variables

| Variable | Purpose / default |
|---|---|
| `COMPOSE_PROJECT_NAME` | Isolates an instance (containers, networks, volumes) so you can run several on one host. Default `specivo`. |
| `SPECIVO_PORT` | External nginx port. Default `9933`. |
| `SPECIVO_DATA_DIR` | Persistent data directory: database files, logs, attachments, themes. Default `./specivo-data`. |
| `SPECIVO_VERSION` | Docker Hub image tag — `latest`, or a pinned release like `0.1.10`. |
| `DATABASE_URL` | `postgresql+asyncpg://specivo:specivo@db:5432/specivo`. Point it at an external database to use your own. |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | Credentials for the bundled Postgres. |
| `REDIS_URL` | `redis://redis:6379/0`. Point it at an external Redis if you prefer. |
| `SECRET_KEY` | Signs sessions and tokens. **Must** be a strong, unique value in production. Keep in `.env.local`. |
| `DEBUG` | `false` in production. |
| `REGISTRATION_MODE` | `open` lets anyone self-register. Use a closed mode to require invites. |
| `CAPTCHA_ENABLED` | Captcha on the registration form. |
| `CORS_ORIGINS` | Allowed browser origins, e.g. `["https://specivo.example.com"]`. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_TLS` / `SMTP_USER` / `SMTP_PASSWORD` | Outbound email (optional). |

## Secrets (`.env.local`)

!!! warning "Set a strong SECRET_KEY and review registration"
    The default `SECRET_KEY` is insecure and exists only so the stack starts on first run. Before
    you expose Specivo to anyone, put a strong, unique value in `.env.local`:

    ```bash
    # .env.local
    SECRET_KEY=a-long-random-string-you-generated
    ```

    Also review `REGISTRATION_MODE`. With `open`, anyone who can reach the URL can create an
    account. For a private deployment, switch to a closed mode and invite people instead.

## External database and Redis

The bundled `db` and `redis` services are convenient but optional. To use managed services instead,
point the URLs at them:

```bash
# .env
DATABASE_URL=postgresql+asyncpg://user:pass@db.example.com:5432/specivo
REDIS_URL=redis://cache.example.com:6379/0
```

Your PostgreSQL must have the **pgvector** extension available, since search relies on it. Once you
point the URLs elsewhere, you can remove the `db` and `redis` services from `docker-compose.yml` —
they are declared as optional dependencies, so the app starts without them.

## The data directory

Everything that must survive a container restart lives under `SPECIVO_DATA_DIR` (default
`./specivo-data`):

- `postgresql/` — the bundled database files
- attachments uploaded to issues, wiki pages, and comments
- `logs/` — application and nginx logs
- themes and the embedding model, if you fetch it

Back this directory up alongside a database dump. See [Backup & restore](backup-restore.md).

## Email (SMTP)

Specivo can send notifications (mentions, watcher updates, password resets) if you configure SMTP.
It works fine without email — those notifications simply stay in-app.

```bash
# .env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_TLS=true
SMTP_USER=notifications@example.com
SMTP_PASSWORD=...      # put the password in .env.local
```

## Imported passwords

These settings matter only if you import accounts from another system and let
them keep their passwords. See [Migrating from Redmine](migrating-from-redmine.md#keeping-peoples-passwords)
and [Writing a password hasher plugin](password-hasher-plugins.md).

| Variable | Purpose / default |
|---|---|
| `LEGACY_PASSWORD_ALGORITHMS` | Imported password formats allowed to sign in, e.g. `["redmine_sha1"]`. Default `[]`: none. |
| `LEGACY_PASSWORD_HASHES_EXPIRE_AT` | After this moment no imported password works and those people use password recovery, e.g. `2027-03-31T00:00:00Z`. Default unset. |
| `PASSWORD_HASHERS` | Extra hasher classes by dotted path, e.g. `["acme_hashers.AcmeTrackerMd5"]`. |
| `INSTALLED_PLUGINS` | Plugins by dotted path; a plugin can provide password hashers and [import sources](import-source-plugins.md). |

## Semantic search model

Hybrid search combines keyword matching with semantic ("by meaning") matching. The semantic side
needs an embedding model, which you enable in one of two ways:

- **Bundled on-device model.** Download once and it runs on CPU with no external calls:

  ```bash
  make download-model      # ~393 MB, stored under SPECIVO_DATA_DIR
  ```

- **Bring your own key (BYOK).** Use an external embedding provider instead of the local model.

Keyword search and everything else work without either. If you skip this, search still functions —
it just matches on words rather than meaning. For an offline host, pre-fetch the model as described
in [Air-gapped install](air-gapped.md).
