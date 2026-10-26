---
description: Import from a tracker Specivo has no built-in importer for, with a small import source plugin, then run it with a dry run first.
---

# Writing an import source plugin

Specivo's importer ships with one source, Redmine (see
[Migrating from Redmine](migrating-from-redmine.md)). To move in from anything
else, such as another tracker's export, an in-house app or a CSV dump, write an
import source plugin. You don't have to patch Specivo. The plugin only reads the
old system. Specivo does all the writing, and the import gets the same dry run,
report and resume as a Redmine import.

Paired with a [password hasher plugin](password-hasher-plugins.md), it can also
let people keep their old passwords.

## How an import works

An import source is a Python class, an *adapter*, that reads the old system and
hands Specivo plain objects: users, projects, issues and so on. Specivo then
loads them in a fixed order of phases. Lookups (trackers, statuses, priorities)
come first, then users, projects, issues, their history, wiki pages,
attachments and logged time. Every row it creates is recorded against the
source's own id, so a second run recognises what already landed and skips it.

You subclass `BaseSourceAdapter`. Every stream it offers is empty until you
override it, so you write code only for the data your source actually has. To
import users and issues, you need four methods:

| Method | Returns |
|---|---|
| `extract_lookups()` | the trackers, statuses and priorities your issues use |
| `extract_users()` | people |
| `extract_projects()` | projects, parents before children |
| `extract_issues(project_ref)` | the issues of one project, parents before children |

## Example

Suppose the old tracker can export everything as one JSON file:

```json
{
  "users": [
    {"id": 1, "login": "dlee", "name": "Dana Lee", "email": "dana@example.com",
     "salt": "NaCl", "digest": "ce48c13da732f51a1bf9c4fac876c638"},
    {"id": 2, "login": "spark", "name": "Sam Park", "email": "sam@example.com"}
  ],
  "projects": [
    {"key": "web", "name": "Website"}
  ],
  "issues": [
    {"id": 101, "project": "web", "title": "Broken link on the pricing page",
     "body": "The **Contact sales** button points at a 404.", "author": 1, "closed": false},
    {"id": 102, "project": "web", "title": "Add a dark theme", "author": 2, "closed": false},
    {"id": 103, "project": "web", "title": "Update the footer year", "author": 1, "closed": true}
  ]
}
```

The `salt` and `digest` are the tracker's stored password, `md5(salt + password)`
in hex. This is the scheme the [password hasher guide](password-hasher-plugins.md)
teaches Specivo to verify. The whole plugin:

```python
import json
from pathlib import Path

from specivo.core.plugin import BasePluginConfig
from specivo.importers.core.ir import (
    IRCredential,
    IRIssue,
    IRLookups,
    IRPriority,
    IRProject,
    IRStatus,
    IRTracker,
    IRUser,
)
from specivo.importers.core.source import BaseSourceAdapter


class AcmeJsonSource(BaseSourceAdapter):
    source_system = "acme"
    description = "Acme Tracker JSON export"
    credential_algorithms = ("acme_md5",)

    def __init__(self, export_path, source_instance=None):
        super().__init__(source_instance)
        self.export_path = Path(export_path)
        self.data = {}

    @classmethod
    def add_arguments(cls, parser):
        group = parser.add_argument_group("acme source")
        group.add_argument("--export", required=True, help="Path to the Acme Tracker JSON export.")

    @classmethod
    def from_args(cls, args):
        return cls(args.export, source_instance=args.source_instance)

    async def connect(self):
        self.data = json.loads(self.export_path.read_text())

    async def extract_lookups(self):
        return IRLookups(
            trackers=[IRTracker(source_ref="task", name="Task", default_status_ref="open")],
            statuses=[
                IRStatus(source_ref="open", name="Open", category="backlog"),
                IRStatus(source_ref="done", name="Done", category="closed"),
            ],
            priorities=[IRPriority(source_ref="normal", name="Normal", is_default=True)],
        )

    async def extract_users(self):
        for user in self.data["users"]:
            credential = None
            if user.get("digest"):
                credential = IRCredential("acme_md5", {"salt": user["salt"], "password_digest": user["digest"]})
            yield IRUser(
                source_ref=str(user["id"]),
                login=user["login"],
                display_name=user["name"],
                email=user.get("email"),
                credential=credential,
            )

    async def extract_projects(self):
        for project in self.data["projects"]:
            yield IRProject(source_ref=project["key"], identifier=project["key"], name=project["name"])

    async def extract_issues(self, project_ref):
        for issue in self.data["issues"]:
            if issue["project"] != project_ref:
                continue
            yield IRIssue(
                source_ref=str(issue["id"]),
                project_ref=project_ref,
                tracker_ref="task",
                status_ref="done" if issue["closed"] else "open",
                priority_ref="normal",
                subject=issue["title"],
                description=issue.get("body"),
                author_ref=str(issue["author"]),
            )


class Plugin(BasePluginConfig):
    name, tier, version = "acme-import", "core", "1.0"

    def get_import_sources(self):
        return [AcmeJsonSource]
```

Save it as `acme_import.py`. Here's what each part does:

- **`source_system`** is the name you pass to `--source`. Use 2 to 30
  characters: a lowercase letter followed by lowercase letters, digits or
  underscores. It also scopes the record of what was imported, so keep it
  stable. Rename it and a re-run imports everything a second time. `redmine`
  is reserved for the built-in source, and two installed sources with the same
  name stop Specivo from starting.
- **`description`** is the one line `--list-sources` shows.
- **`add_arguments(parser)`** adds the source's own command-line options, in a
  group named after the source. `--source acme --help` lists them along with
  the generic options described below.
- **`from_args(args)`** builds the adapter from the parsed command line. `args`
  holds your options and the generic ones. `args.source_instance` is `None`
  unless the operator gave `--source-instance`, and the base class then uses the
  source name. Use a different instance name for each installation when you
  import two installations of the same system into one Specivo.
- **`connect()`** opens the source and fails early if it can't be read.
  `close()` releases whatever `connect()` opened. Both do nothing by default.
- **The `extract_*` methods** are async generators: they `yield` objects one at
  a time. For a large source, read in pages rather than loading a whole table
  into memory.

## What the objects need

Every object carries a `source_ref`, the old system's own id as a string. Objects
point at each other through those refs (`project_ref`, `author_ref`,
`status_ref`), never through Specivo ids. Specivo translates them as it loads.

| Object | Required | Optional |
|---|---|---|
| `IRTracker` | `source_ref`, `name` | `default_status_ref`, `description`, `position` |
| `IRStatus` | `source_ref`, `name`, `category`: one of `backlog`, `active`, `done`, `closed` | `position`, `default_done_ratio` |
| `IRPriority` | `source_ref`, `name` | `is_default`, `position`, `active` |
| `IRUser` | `source_ref`, `login`, `display_name` | `email` (a placeholder is generated if missing), `status` (`active` by default), `is_admin`, `language`, `created_at`, `last_login_at`, `credential`, `must_change_password` |
| `IRProject` | `source_ref`, `identifier`, `name` | `description`, `is_public`, `parent_ref`, `status` (1 active, 5 closed, 9 archived), `created_at` |
| `IRIssue` | `source_ref`, `project_ref`, `tracker_ref`, `status_ref`, `priority_ref`, `subject` | `author_ref` (the import account otherwise), `assigned_to_ref`, `description`, `parent_ref`, `start_date`, `due_date`, `estimated_hours`, `done_ratio`, `is_private`, `created_at`, `closed_at` |

The objects are dataclasses in `specivo.importers.core.ir`, where each field is
documented. Some more things to know:

- **Text is Markdown.** Descriptions and comments are stored as they arrive.
  If your source uses another markup, override `content_converter()` to return
  an object whose `convert(raw, ctx)` turns one field into Markdown.
- **Project keys** come from `identifier`. `web` becomes `WEB`, and the
  operator can override any of them with `--project-key-map`.
- **A ref that points at something not imported** gets the most harmless
  fallback. An issue whose author is unknown is attributed to the import
  account. An unknown assignee or parent is left out, with a warning in the
  report. An issue whose tracker wasn't imported is skipped, with a warning.
- **More streams** are there to override when your source has the data:
  `extract_groups`, `extract_memberships`, `extract_custom_fields`,
  `extract_versions`, `extract_categories`, `extract_journals` (comments and
  history), `extract_relations`, `extract_watchers`, `extract_wiki_pages`,
  `extract_time_entries` and `extract_attachments`. For attachments, also
  override `resolve_attachment_path(attachment)` to return the file's path, and
  raise `ValueError` for a path outside the source's files directory: the
  locator comes from the source data and isn't trusted.

## Passwords

An adapter never does password cryptography. It hands over the source's raw
credential columns in an `IRCredential`, together with the id of the hasher that
understands them, as `extract_users` does above. It also lists those ids in
`credential_algorithms`, so an import that was asked to keep passwords can check
them before it writes anything.

Specivo then decides what to do with them:

- The credential is used only when the import runs with `--import-passwords`
  **and** the algorithm is listed in `LEGACY_PASSWORD_ALGORITHMS`. Without the
  flag, every account has to set a new password. If the flag is given but the
  algorithm isn't enabled, the import stops before it writes anything.
- Administrators never keep their old password.
- The hasher turns the raw columns into a stored value, wrapped in bcrypt, and
  replaces it with a native bcrypt hash at the person's first sign-in.

So for the example, install the `acme_md5` hasher from the
[password hasher guide](password-hasher-plugins.md) next to this plugin, and
enable it.

## Install it

The import runs inside the `api` container, so the plugin, and the export it
reads, have to be there. `INSTALLED_PLUGINS` is read by every service, so mount
the plugin into both Celery workers as well, or they fail to start. For
example, in `docker-compose.override.yml`:

```yaml
services:
  api:
    volumes:
      - ./plugins/acme_import.py:/app/acme_import.py:ro
      - ./plugins/acme_hashers.py:/app/acme_hashers.py:ro
      - ./acme-export.json:/data/acme-export.json:ro
  celery-worker:
    volumes:
      - ./plugins/acme_import.py:/app/acme_import.py:ro
      - ./plugins/acme_hashers.py:/app/acme_hashers.py:ro
  celery-embeddings:
    volumes:
      - ./plugins/acme_import.py:/app/acme_import.py:ro
      - ./plugins/acme_hashers.py:/app/acme_hashers.py:ro
```

Then register both plugins in `.env` and restart the stack:

```bash
INSTALLED_PLUGINS=["acme_hashers.Plugin", "acme_import.Plugin"]
LEGACY_PASSWORD_ALGORITHMS=["acme_md5"]
```

Check that the source is there:

```bash
docker compose exec api python -m specivo.cli.import_data --list-sources
```

```
acme     plugin:acme-import  Acme Tracker JSON export
redmine  core                Redmine 7.x, read from its database and files directory
```

## Start with a dry run

```bash
docker compose exec api python -m specivo.cli.import_data --source acme \
  --export /data/acme-export.json --import-passwords --dry-run
```

From a checkout of the repository, `make import-data SOURCE=acme ARGS='--export /data/acme-export.json --dry-run'`
runs the same command.

A dry run does the whole import inside a transaction and rolls it back, so
nothing is written. Its report is what you came for:

```
DRY RUN (nothing was written) — acme @ acme
run id: 1b1c8b07-add4-428c-87e6-6f080b5e17b9

Created:
  issue                3
  priority             1
  project              1
  status               2
  system               1
  tracker              1
  user                 2

accounts_that_keep_their_existing_password (1):
  dlee

accounts_that_will_be_asked_to_set_a_password_at_first_sign_in (1):
  spark

project_keys_assigned (1):
  web -> WEB
```

- **Created** counts what the run would create. `system` is the import account
  that owns rows with no known author.
- **Matched onto existing rows** appears when a status, tracker or priority of
  the same name already existed and `--merge-duplicate-statuses` reused it.
  Without that flag, a second one is created and listed under warnings.
- **The named lists** are decisions worth checking: the project key chosen for
  each project, accounts given a placeholder email, and who keeps a password and
  who has to set a new one.
- **Warnings** list anything that was adjusted or couldn't be carried across,
  each with the source id to look up. A login that was already taken and got a
  suffix (`dlee-2`) shows up here, for example.

When the report looks right, run the same command without `--dry-run`. Add
`--report-json report.json` to keep a copy.

## Options every source has

| Option | What it does |
|---|---|
| `--source NAME` | The source to import from. |
| `--list-sources` | List the installed sources and where each comes from. |
| `--source-instance NAME` | Name this installation, so two installations of one system can be imported into one Specivo. |
| `--project 1,2` | Import only these projects, by their `source_ref`. Subprojects come with their parent. |
| `--project-key-map web=SITE` | Choose a project's key instead of deriving it. |
| `--merge-duplicate-statuses` | Reuse an existing status, tracker or priority of the same name. |
| `--flatten-excess-depth` | Import projects nested deeper than Specivo allows by moving them higher up. |
| `--import-passwords` | Let accounts keep their password. See [Passwords](#passwords). |
| `--dry-run` | Run everything and roll it back. |
| `--resume RUN_ID` | Continue an interrupted run. Each phase commits when it finishes, and anything already imported is skipped. |
| `--strict` | Treat every warning as a failure. Useful while you develop the plugin. |
| `--report-json PATH` | Also write the report as JSON. |
| `--batch-size N` | Rows per page, for adapters that read in pages (default 500). |

## A word on trust

An import source is ordinary Python running inside Specivo with full access to
its database. Only install one you have read, and remove it from
`INSTALLED_PLUGINS` once the migration is done.
