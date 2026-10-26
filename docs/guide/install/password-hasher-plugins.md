---
description: Teach Specivo to verify passwords imported from another system with a small password hasher plugin, then enable it with LEGACY_PASSWORD_ALGORITHMS.
---

# Writing a password hasher plugin

When you move people into Specivo from another system, they can keep their
existing passwords. Specivo verifies the old hash at their first sign-in and
then replaces it with its own bcrypt hash. Nobody has to reset anything.

Three source formats are built in:

| Algorithm id | Source | What it reads |
|---|---|---|
| `redmine_sha1` | Redmine local accounts | `salt`, `hashed_password` |
| `django_pbkdf2_sha256` | Django's default `pbkdf2_sha256$...` hashes | `password` |
| `bcrypt_foreign` | bcrypt from another application (`$2y$`, `$2a$`, `$2b$`) | `hash` |

For anything else, write a hasher. Most of them are about fifteen lines.

## How a hasher works

A hasher does two jobs:

- **At import**, it turns the source system's raw credential columns into the
  value Specivo stores. It never sees a plaintext password.
- **At sign-in**, it checks a password against that stored value.

Weak source formats (unsalted or fast hashes such as MD5 and SHA1) should never
sit in the database as they are. The `WrappedLegacyHasher` base class takes care
of that: at import it wraps the source digest in bcrypt, and the stored value
looks like `<algorithm>$<parameters>$<bcrypt>`. A copy of the database then
holds nothing weaker than a native Specivo hash, and every sign-in costs one
bcrypt whatever the account's history, so response times don't show which
accounts are still on an old hash.

You implement two methods:

- `source_params(fields)` picks the parameters the scheme needs (a salt, say)
  and the stored digest out of the source row;
- `inner_digest(password, params)` recomputes that digest from a password.

The base class handles everything else: the bcrypt wrap, the constant-time
comparison, rejecting malformed values without raising an error, and marking
every such hash for an upgrade at the next sign-in.

## Example

Suppose the old tracker stores `md5(salt + password)` as lowercase hex in a
`password_digest` column, next to a `salt` column. The whole plugin:

```python
import hashlib

from specivo.core.plugin import BasePluginConfig
from specivo.services.password_hashers import WrappedLegacyHasher


class AcmeTrackerMd5(WrappedLegacyHasher):
    algorithm = "acme_md5"

    def source_params(self, fields):  # import side: which source columns matter
        return {"salt": fields["salt"]}, fields["password_digest"]

    def inner_digest(self, password, params):  # login side: recompute the source's digest
        return hashlib.md5((params["salt"] + password).encode()).hexdigest()


class Plugin(BasePluginConfig):
    name, tier, version = "acme-hashers", "core", "1.0"

    def get_password_hashers(self):
        return [AcmeTrackerMd5()]
```

Save it as `acme_hashers.py`.

Get the details of the source scheme exactly right: hex or raw bytes for
intermediate digests, upper or lower case, the text encoding, the order of salt
and password. The `test` command below lets you check them against a real row
before anything goes live.

The algorithm id must be 2 to 32 characters long: a lowercase letter followed by
lowercase letters, digits or underscores. The built-in ids and `bcrypt` are
reserved, and two hashers with the same id stop Specivo from starting.

## Install it

The module has to be importable by the web app, the Celery worker and the
import command. With Docker, mount it into `/app` in every service, for example
in `docker-compose.override.yml`:

```yaml
services:
  api:
    volumes:
      - ./plugins/acme_hashers.py:/app/acme_hashers.py:ro
  celery-worker:
    volumes:
      - ./plugins/acme_hashers.py:/app/acme_hashers.py:ro
```

Then register it in `.env`, using either of these:

```bash
# As a plugin (it can ship other hooks too, such as an import source):
INSTALLED_PLUGINS=["acme_hashers.Plugin"]

# Or just the hasher class, without the Plugin wrapper:
PASSWORD_HASHERS=["acme_hashers.AcmeTrackerMd5"]
```

Registering a hasher does not switch it on.

To bring the accounts over from the source system in the first place, pair the
hasher with an [import source plugin](import-source-plugins.md). The import
source hands over the raw `salt` and `password_digest`, and the hasher does the
rest.

## Check it against a known row

Take one account from the old system whose password you know (a test account
is ideal) and run:

```bash
docker compose exec api python -m specivo.cli.admin password-hashers test acme_md5 \
  --field salt=NaCl \
  --field password_digest=ce48c13da732f51a1bf9c4fac876c638 \
  --password 'open sesame'
```

It prints `MATCH` or `NO MATCH` and doesn't touch any account. Leave out
`--password` to be prompted for it instead, so the password stays out of your
shell history.

## Enable it

Only the algorithms listed in `LEGACY_PASSWORD_ALGORITHMS` ever verify a
sign-in. Built-in hashers are covered by the same rule.

```bash
# .env
LEGACY_PASSWORD_ALGORITHMS=["acme_md5"]
```

Restart the stack, then check the result:

```bash
docker compose exec api python -m specivo.cli.admin password-hashers
```

That lists every registered algorithm, where it came from (core, a plugin or the
setting), whether it is enabled and how many accounts hold a hash under it.

A hash whose algorithm is unknown or not enabled simply doesn't match. The
person sees the usual "Invalid login or password" and can use password recovery.

## Afterwards

Each account moves to a native bcrypt hash at its first successful sign-in.
Every upgrade is recorded in the security audit log as `password_hash_upgraded`,
with the old algorithm id but never the hash. To see who hasn't signed in yet:

```bash
docker compose exec api python -m specivo.cli.admin legacy-passwords
```

Old hashes are only as strong as the passwords behind them, so don't leave them
around forever. Either set a cutoff, after which no legacy hash verifies and the
remaining people use password recovery:

```bash
LEGACY_PASSWORD_HASHES_EXPIRE_AT=2027-03-31T00:00:00Z
```

or expire them at once:

```bash
docker compose exec api python -m specivo.cli.admin legacy-passwords expire --yes
```

Once no account holds the old format, remove the algorithm from
`LEGACY_PASSWORD_ALGORITHMS`.
