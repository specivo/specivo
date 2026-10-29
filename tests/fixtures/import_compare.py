"""Compare two imports of the same source, row by row and by natural key.

Each import runs into a freshly migrated database of its own
(:func:`import_into_fresh_database`), committing phase by phase as the import
command does. :class:`Snapshot` then reads back everything it wrote, keyed by
what a person would recognise it by rather than by id: counts per table, every
issue field, history with its authors and dates, attachments by hash, wiki
pages with every revision, memberships with their roles, time entries,
relations, watchers and lookups. :func:`raw_differences` lists where two
snapshots disagree.

Used by the bundle snapshot test and by anything else that has to show two
ways of importing one bundle give the same database.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from specivo.cli.import_data import build_pipeline, parse_args
from specivo.importers.core.progress import NullProgressReporter
from specivo.importers.core.source import build_source_registry
from specivo.models.attachment import Attachment
from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.lookups import IssueCategory, IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.metadata_schema import MetadataSchema
from specivo.models.project import Project
from specivo.models.relation import IssueRelation
from specivo.models.role import Role
from specivo.models.time_entry import TimeEntry, TimeEntryActivity
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.models.version import Version
from specivo.models.watcher import Watcher
from specivo.models.wiki import Wiki, WikiContent, WikiPage, WikiRedirect

REPO_ROOT = Path(__file__).resolve().parents[2]

IMPORT_ACCOUNT = "<import account>"
IMPORT_TIME = "<import time>"

COUNTED = (
    User,
    UserGroup,
    UserGroupMember,
    Project,
    Member,
    MemberRole,
    Version,
    IssueCategory,
    MetadataSchema,
    Issue,
    Journal,
    JournalDetail,
    IssueRelation,
    Watcher,
    Attachment,
    WikiPage,
    WikiContent,
    WikiRedirect,
    TimeEntry,
    IssueStatus,
    Tracker,
    IssuePriority,
    TimeEntryActivity,
    Role,
)


def _ordered(items):
    """Sort *items* for comparison; entries may hold ``None`` beside strings, so order by their repr."""
    return sorted(items, key=repr)


class Snapshot:
    """Everything an import wrote, keyed by what a person would recognise it by."""

    def __init__(self, session, reference_fields: dict[str, str]) -> None:
        self.session = session
        # Custom-field key -> "user" or "version", for values holding an id.
        self.reference_fields = reference_fields

    async def take(self) -> dict[str, Any]:
        s = self.session
        users = {u.id: u for u in (await s.execute(select(User))).scalars()}
        self.logins = {uid: (IMPORT_ACCOUNT if u.is_service_account else u.login) for uid, u in users.items()}
        projects = {p.id: p for p in (await s.execute(select(Project))).scalars()}
        self.identifiers = {pid: p.identifier for pid, p in projects.items()}
        issues = {i.id: i for i in (await s.execute(select(Issue))).scalars()}
        self.keys = {iid: f"{i.project_key}-{i.sequence_number}" for iid, i in issues.items()}
        pages = {p.id: p for p in (await s.execute(select(WikiPage))).scalars()}
        wikis = {w.id: w.project_id for w in (await s.execute(select(Wiki))).scalars()}
        self.pages = {pid: (self.identifiers[wikis[p.wiki_id]], p.title) for pid, p in pages.items()}
        names = {}
        for model in (IssueStatus, Tracker, IssuePriority, TimeEntryActivity, Role, IssueCategory, Version, UserGroup):
            names[model] = {row.id: row.name for row in (await s.execute(select(model))).scalars()}
        self.names = names

        return {
            "counts": {
                m.__name__: (await s.execute(select(func.count()).select_from(m))).scalar_one() for m in COUNTED
            },
            # The import account's own name and address differ by design; it is
            # counted above and compared everywhere it owns something.
            # The anonymous account is seeded by the migrations, not imported.
            "users": {
                self.logins[uid]: self._user(u)
                for uid, u in users.items()
                if not u.is_service_account and not u.is_anonymous
            },
            "groups": await self._groups(),
            "projects": {p.identifier: self._project(p) for p in projects.values()},
            "memberships": await self._memberships(),
            "versions": {(self.identifiers[v.project_id], v.name): self._version(v) for v in await self._all(Version)},
            "categories": _ordered(
                (self.identifiers[c.project_id], c.name, self.logins.get(c.assigned_to_id))
                for c in await self._all(IssueCategory)
            ),
            "metadata_schemas": _ordered(
                (
                    self.identifiers[m.project_id],
                    names[Tracker].get(m.tracker_id),
                    m.name,
                    json.dumps(m.schema_definition, sort_keys=True),
                )
                for m in await self._all(MetadataSchema)
            ),
            "issues": {self.keys[iid]: self._issue(i) for iid, i in issues.items()},
            "journals": await self._journals(),
            "relations": _ordered(
                (self.keys[r.issue_from_id], self.keys[r.issue_to_id], r.relation_type, r.delay)
                for r in await self._all(IssueRelation)
            ),
            "watchers": _ordered(
                (self.keys.get(w.issue_id) or str(self.pages.get(w.wiki_page_id)), self.logins[w.user_id])
                for w in await self._all(Watcher)
            ),
            "attachments": _ordered(self._attachment(a) for a in await self._all(Attachment)),
            "wiki": await self._wiki(pages),
            "redirects": _ordered(
                (self.identifiers[wikis[r.wiki_id]], r.title_from, r.redirected_to)
                for r in await self._all(WikiRedirect)
            ),
            "time_entries": _ordered(self._time_entry(t) for t in await self._all(TimeEntry)),
            "lookups": await self._lookups(),
        }

    async def _all(self, model) -> list[Any]:
        return list((await self.session.execute(select(model))).scalars())

    def _user(self, u: User) -> tuple:
        return (u.email, u.display_name, u.status, u.is_admin, u.language, u.created_at, u.must_change_password)

    async def _groups(self) -> dict[str, list[str]]:
        rows = (await self.session.execute(select(UserGroupMember.group_id, UserGroupMember.user_id))).all()
        groups: dict[str, list[str]] = {name: [] for name in self.names[UserGroup].values()}
        for group_id, user_id in rows:
            groups[self.names[UserGroup][group_id]].append(self.logins[user_id])
        return {name: _ordered(members) for name, members in groups.items()}

    def _project(self, p: Project) -> tuple:
        return (
            p.key,
            p.name,
            p.description,
            p.is_public,
            p.status,
            self.identifiers.get(p.parent_id),
            p.created_at,
            sorted(p.anonymous_permissions or []),
        )

    async def _memberships(self) -> list[tuple]:
        rows = (
            await self.session.execute(
                select(Member.project_id, Member.user_id, Member.group_id, MemberRole.role_id).join(
                    MemberRole, MemberRole.member_id == Member.id
                )
            )
        ).all()
        return _ordered(
            (
                self.identifiers[project_id],
                self.logins.get(user_id) or self.names[UserGroup][group_id],
                self.names[Role][role_id],
            )
            for project_id, user_id, group_id, role_id in rows
        )

    def _version(self, v: Version) -> tuple:
        return (v.status, v.sharing, v.effective_date, v.description, v.wiki_page_title, v.created_at)

    def _metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        resolved = dict(metadata or {})
        for key, kind in self.reference_fields.items():
            if key in resolved:
                names = self.logins if kind == "user" else self.names[Version]
                value = resolved[key]
                resolved[key] = [names.get(v, v) for v in value] if isinstance(value, list) else names.get(value, value)
        return resolved

    def _issue(self, i: Issue) -> tuple:
        return (
            i.subject,
            i.description,
            self.names[Tracker][i.tracker_id],
            self.names[IssueStatus][i.status_id],
            self.names[IssuePriority][i.priority_id],
            self.logins[i.author_id],
            self.logins.get(i.assigned_to_id),
            self.keys.get(i.parent_id),
            self.names[IssueCategory].get(i.category_id),
            self.names[Version].get(i.fixed_version_id),
            i.start_date,
            i.due_date,
            i.estimated_hours,
            i.remaining_estimate,
            i.done_ratio,
            i.is_private,
            self._metadata(i.issue_metadata),
            i.created_at,
            i.updated_at,
            i.closed_on,
        )

    async def _journals(self) -> dict[str, list[tuple]]:
        details: dict[int, list[tuple]] = {}
        for d in await self._all(JournalDetail):
            details.setdefault(d.journal_id, []).append((d.property, d.prop_key, d.old_value, d.new_value))
        journals: dict[str, list[tuple]] = {}
        for j in sorted(await self._all(Journal), key=lambda j: (j.issue_id, j.sequence)):
            journals.setdefault(self.keys[j.issue_id], []).append(
                (
                    j.sequence,
                    self.logins[j.user_id],
                    j.created_at,
                    j.updated_at,
                    j.notes,
                    j.is_private,
                    sorted(details.get(j.id, [])),
                )
            )
        return journals

    def _attachment(self, a: Attachment) -> tuple:
        if a.container_type == "Issue":
            container = self.keys[a.container_id]
        else:
            container = str(self.pages[a.container_id])
        return (
            container,
            a.filename,
            a.content_hash,
            a.filesize,
            a.content_type,
            a.description,
            self.logins[a.author_id],
            a.created_at,
        )

    async def _wiki(self, pages: dict[int, WikiPage]) -> dict[str, tuple]:
        revisions: dict[int, list[tuple]] = {}
        for c in sorted(await self._all(WikiContent), key=lambda c: (c.page_id, c.version)):
            revisions.setdefault(c.page_id, []).append(
                (c.version, self.logins[c.author_id], c.created_at, c.updated_at, c.comments, c.text)
            )
        return {
            str(self.pages[pid]): (
                self.pages[p.parent_id][1] if p.parent_id else None,
                p.protected,
                revisions.get(pid, []),
            )
            for pid, p in pages.items()
        }

    def _time_entry(self, t: TimeEntry) -> tuple:
        return (
            self.identifiers[t.project_id],
            self.keys.get(t.issue_id),
            self.logins[t.user_id],
            self.names[TimeEntryActivity][t.activity_id],
            t.hours,
            t.spent_on,
            t.comments,
            t.created_at,
        )

    async def _lookups(self) -> dict[str, list[tuple]]:
        statuses = {s.id: s.name for s in await self._all(IssueStatus)}
        return {
            "statuses": _ordered((s.name, s.category, s.default_done_ratio) for s in await self._all(IssueStatus)),
            "trackers": _ordered(
                (t.name, t.is_in_roadmap, tuple(t.disabled_core_fields), statuses.get(t.default_status_id))
                for t in await self._all(Tracker)
            ),
            "priorities": _ordered((p.name, p.is_default, p.active) for p in await self._all(IssuePriority)),
            "activities": _ordered((a.name, a.is_default, a.active) for a in await self._all(TimeEntryActivity)),
            "roles": _ordered(
                (r.name, r.builtin, tuple(sorted(r.permissions or [])), r.users_visibility, r.time_entries_visibility)
                for r in await self._all(Role)
            ),
        }


@asynccontextmanager
async def fresh_database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Create, migrate and finally drop a database of its own for one import."""
    base = make_url(os.environ["DATABASE_URL"])
    name = f"specivo_snapshot_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name)
    engine = create_async_engine(url)
    try:
        environment = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)}
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            check=True,
            env=environment,
            capture_output=True,
            cwd=REPO_ROOT,
        )
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def run_import(argv: list[str], session_factory: async_sessionmaker[AsyncSession]) -> Any:
    """Run the import command's pipeline for *argv* against *session_factory*, and return its summary."""
    args, adapter_cls = parse_args(argv, build_source_registry())
    pipeline = build_pipeline(args, adapter_cls.from_args(args), session_factory, NullProgressReporter())
    return await pipeline.run()


async def import_into_fresh_database(
    argv: list[str], reference_fields, storage: Path, snapshot_cls: type[Snapshot] = Snapshot
) -> tuple[Any, dict]:
    """Run the import command's pipeline for *argv* into a database of its own, and snapshot it."""
    import specivo.services.attachment_service as attachment_service

    storage.mkdir()
    attachment_service._upload_dir = storage  # restored by the test's monkeypatch
    async with fresh_database() as session_factory:
        summary = await run_import(argv, session_factory)
        async with session_factory() as session:
            snapshot = await snapshot_cls(session, reference_fields).take()
    return summary, snapshot


def mask_import_time(value: Any, since: datetime) -> Any:
    """Return *value* with every timestamp at or after *since* replaced by a marker.

    A record the source gave no date gets the moment it was imported, which
    differs between two imports of the same source. Masking those leaves the
    dates the source did state to be compared.
    """
    if isinstance(value, datetime):
        return IMPORT_TIME if value >= since else value
    if isinstance(value, dict):
        return {key: mask_import_time(item, since) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_import_time(item, since) for item in value]
    if isinstance(value, tuple):
        return tuple(mask_import_time(item, since) for item in value)
    return value


def raw_differences(first: dict[str, Any], second: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Return ``{"section[key]": (first, second)}`` for every entry the two snapshots disagree on."""
    found: dict[str, tuple[Any, Any]] = {}
    for section in first:
        left, right = first[section], second[section]
        if left == right:
            continue
        if isinstance(left, dict):
            for key in sorted(set(left) | set(right), key=str):
                if left.get(key) != right.get(key):
                    found[f"{section}[{key}]"] = (left.get(key), right.get(key))
        else:
            found[section] = (left, right)
    return found
