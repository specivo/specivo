"""The example import source plugin from the guide, kept here so it is tested.

``docs/guide/install/import-source-plugins.md`` shows this module verbatim,
and ``acme_export.json`` next to it is the guide's sample export. A
hypothetical tracker exports its users, projects and issues as one JSON file;
the plugin reads it and hands passwords over for the ``acme_md5`` hasher in
``acme_hashers.py``. Change the guide and these files in step.
"""

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
