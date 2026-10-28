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
| `APP_URL` | The public address of Specivo, for example `https://specivo.example.com`. Links in emails and [single sign-on](single-sign-on/index.md) addresses are built from it. |
| `SECRET_KEY` | Signs sessions and tokens. **Must** be a strong, unique value in production. Keep in `.env.local`. |
| `DEBUG` | `false` in production. |
| `REGISTRATION_MODE` | `open` lets anyone self-register. Use a closed mode to require invites. |
| `CAPTCHA_ENABLED` | Captcha on the registration form. |
| `CORS_ORIGINS` | Allowed browser origins, e.g. `["https://specivo.example.com"]`. |
| `TRUSTED_PROXIES` | Address of the reverse proxy allowed to name the client in `X-Forwarded-For`, as a JSON list of addresses or networks. Default `[]` (the connection's address is the client); the bundled stack sets the bundled nginx, `["10.213.47.2/32"]`. See [Client addresses behind a proxy](#client-addresses-behind-a-proxy). |
| `TRUSTED_PROXY_HOPS` | How many proxies in a row stand in front of Specivo. Default `1`. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_TLS` / `SMTP_USER` / `SMTP_PASSWORD` | Outbound email (optional). |
| `MARKDOWN_RENDER_DEADLINE_SECONDS` | CPU seconds one Markdown field (a description, a comment, a wiki page) may use to render before it is shown as plain text instead. It also may not take more than 4 times this in elapsed time. A backstop: text that would render slowly is caught before rendering. Counts CPU time, so a busy host does not trigger it. Default `2`, scaled by `MARKDOWN_RENDER_SPEED_FACTOR`; the largest realistic pages need under half a second on a fast machine. See [Size and complexity limits](../reference/markdown.md#size-and-complexity-limits). |
| `MARKDOWN_RENDER_REQUEST_BUDGET_SECONDS` | CPU seconds all the Markdown fields on one page or API response may use together (a description, its comments and its history). Once spent, the remaining fields are shown as plain text. Default `5`, scaled by `MARKDOWN_RENDER_SPEED_FACTOR`. |
| `MARKDOWN_RENDER_SPEED_FACTOR` | The two settings above are for a fast current machine. Specivo measures how much slower this host renders Markdown, once per process at first use (logged at INFO as "Markdown render speed factor"), and multiplies both limits by that factor, between 1 and 20, so large pages still render on slow or low-power hardware. Set a fixed value to override the measurement, e.g. `1` to turn scaling off. Default: measured. |

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

Your PostgreSQL must have the **pgvector** extension available, since search relies on it. Use
pgvector **0.8 or later**: semantic search only uses its vector index with 0.8's iterative index
scans, and compares every embedding on older releases (the bundled `pgvector/pgvector:pg18` image
ships 0.8). Once you
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

## Client addresses behind a proxy

Specivo uses the client's IP address for the per-address rate limits (sign-in, single sign-on,
anonymous reads), for the address shown on each session, for API key IP allowlists and for the
security audit log. Behind a reverse proxy, the connection comes from the proxy. Each proxy appends
the address it received the request from to the `X-Forwarded-For` header, so the client's address
is in that header. Two settings decide what Specivo believes:

- **`TRUSTED_PROXIES`**: the address of the proxy that connects to Specivo. Only a connection from
  one of these addresses may name the client. A connection from anywhere else is the client,
  whatever header it sends. Default `[]`: nothing is trusted.
- **`TRUSTED_PROXY_HOPS`**: how many proxies in a row stand in front of Specivo. The client is the
  entry that many places from the right of `X-Forwarded-For`. Everything to the left of it was
  written by the client and is ignored, so nobody can choose their own address by sending the
  header. Default `1`.

List the proxy's exact address, not a whole private range: anything in a trusted range could name
any client.

### The bundled stack

`docker-compose.yml` puts nginx and the api on their own network, `proxy`
(`10.213.47.0/29`), with nginx at the fixed address `10.213.47.2`, and sets
`TRUSTED_PROXIES=["10.213.47.2/32"]`. Nothing else is on that network, the api publishes no port,
and nginx appends the address it saw, so it works without changes. If that range is already used on
your host (`docker compose up` fails with "Pool overlaps"), pick another one in `.env`:

```bash
# .env
SPECIVO_PROXY_SUBNET=10.99.0.0/29
SPECIVO_NGINX_IP=10.99.0.2
```

`TRUSTED_PROXIES` follows `SPECIVO_NGINX_IP` unless you set it yourself. Set these in `.env`, not
`.env.local`: the compose file reads them from `.env`.

### Another proxy in front of nginx

When an HTTPS terminator or a CDN forwards to the bundled nginx, there are two proxies in a row.
The outer one writes the client's address and nginx appends the outer proxy's address, so set:

```bash
# .env
TRUSTED_PROXY_HOPS=2
```

- **Caddy**: `reverse_proxy` sets `X-Forwarded-For` to the client's address and does not pass on
  a header the client sent, unless you configure Caddy's own `trusted_proxies`.
- **Traefik**: appends the client's address. Leave `forwardedHeaders.insecure` off on the
  entry point, so a header the client sent is not passed through as if Traefik had written it.
- **Cloudflare**: appends the visitor's address. Nginx must then only accept connections from
  Cloudflare (firewall the published port to Cloudflare's address ranges), because a request that
  goes around Cloudflare can put anything in the extra entry.

With `TRUSTED_PROXY_HOPS=1` and an outer proxy, every client appears as the outer proxy's address:
safe, but everyone shares one rate limit bucket. With a hop count higher than the real number of
proxies, clients can choose their address. Count exactly.

### Without the bundled nginx

If your own proxy (nginx, Caddy, Traefik) connects straight to the api, set `TRUSTED_PROXIES` to
that proxy's address and keep `TRUSTED_PROXY_HOPS=1`. For nginx, the usual
`proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` is right: it appends the client's
address.

```bash
TRUSTED_PROXIES=["192.0.2.10/32"]
```

## Single sign-on: getting back in

Identity providers are configured in **Administration → Single sign-on**, and the login page
settings in **Administration → Settings → Authentication**. Those settings can hide the password
form, send the login page straight to one provider, and limit password sign-in to administrators.
None of them can lock an administrator out:

- With the password form hidden, the login page still shows a small **Sign in with a local
  account** link. It opens `/login/?local=1`, which always shows the form and never redirects.
- If the identity provider is down, the error on the login page links to the same form.
- If the SSO settings themselves are wrong (a broken provider, every admin refused), turn on the
  environment switch below. It overrides all three settings until you turn it off again:

  ```bash
  # .env
  SSO_FORCE_LOCAL_LOGIN=true
  ```

  ```bash
  docker compose up -d   # recreates the containers with the new value
  ```

  Specivo logs a warning at startup while it is on. Fix the settings, then remove the line and
  recreate the containers again.
- If no administrator knows a password, set one from the host with
  `make reset-password login=... password=...`, or create a new administrator with
  `make create-admin login=... email=... password=...`.

| Variable | Purpose / default |
|---|---|
| `SSO_FORCE_LOCAL_LOGIN` | Break-glass switch: always show the password form, never redirect the login page to a provider, and let every user sign in with a password. Default `false`. |

The other SSO variables are listed in [Single sign-on](single-sign-on/index.md#settings-in-the-environment).

## Imported passwords

These settings matter only if you import accounts from another system and let
them keep their passwords. A bundle can carry the old password hashes (see the
[Redmine migration tool](https://github.com/specivo/redmine-migration-tool/blob/main/docs/migrating-from-redmine.md)). See also
[Writing a password hasher plugin](password-hasher-plugins.md).

| Variable | Purpose / default |
|---|---|
| `LEGACY_PASSWORD_ALGORITHMS` | Imported password formats allowed to sign in, e.g. `["redmine_sha1"]`. Default `[]`: none. |
| `LEGACY_PASSWORD_HASHES_EXPIRE_AT` | After this moment no imported password works and those people use password recovery, e.g. `2027-03-31T00:00:00Z`. Default unset. |
| `PASSWORD_HASHERS` | Extra hasher classes by dotted path, e.g. `["acme_hashers.AcmeTrackerMd5"]`. |
| `INSTALLED_PLUGINS` | Plugins by dotted path; a plugin can provide password hashers. |

## Semantic search model

Hybrid and semantic search match by meaning, which needs an embedding model. Specivo runs one on
your own CPU, `multilingual-e5-small` (about 487 MB, 384-dimensional vectors, about 100 languages).
It is not part of the Docker image and is not downloaded automatically. You fetch it once, with the
stack running:

```bash
docker compose exec api python -m specivo.cli.download_model   # or: make download-model
```

It saves into `SPECIVO_DATA_DIR/models` and checks each file against its published checksum. See
[Semantic search and embeddings](semantic-search.md#turn-it-on).

There is no setting that turns it on: when the model files are in `SPECIVO_DATA_DIR/models`, it is
on. Without them, keyword search and everything else work normally and hybrid search behaves like
keyword search. Only this bundled local model is supported: there are no hosted embedding providers,
no bring-your-own-key (BYOK) option and no way to switch to another model (see
[Changing the model](semantic-search.md#changing-the-model)).

| Variable | Purpose / default |
|---|---|
| `EMBEDDING_ONNX_THREADS` | CPU threads the model may use. Default `2`; `0` uses every core. |
| `EMBEDDING_WORKER_CONCURRENCY` | Processes in the `celery-embeddings` container. Default `1`; each one loads its own copy of the model. |
| `EMBEDDING_QUEUE` | Celery queue for embedding jobs. Default `embeddings`, read by `celery-embeddings`; `celery` runs them in `celery-worker` instead. |
| `SEARCH_INDEX_COMMENTS` | Index comments for search. Default `true`. |
| `SEARCH_MIN_COMMENT_LENGTH` | Comments shorter than this many characters are not indexed. Default `20`. |
| `SEARCH_EXCLUDE_BOT_COMMENTS` | Skip comments made through an API key or by a service account. Default `true`. |
| `SEARCH_HNSW_EF_SEARCH` | Size of pgvector's candidate list for each vector index scan (`hnsw.ef_search`, 10 to 1000). Higher finds the true nearest matches more reliably and is a little slower. Default `100`. |
| `SEARCH_SEMANTIC_OVERFETCH` | Nearest chunks read from the vector index per wanted result, before results the reader may not see are removed (1 to 50). Default `4`. See [Vector index](semantic-search.md#vector-index). |

See [Semantic search and embeddings](semantic-search.md) for what it is used for, how existing
content is indexed, how to check it works, and troubleshooting. For an offline host, see
[Air-gapped install](air-gapped.md#semantic-search-offline).
