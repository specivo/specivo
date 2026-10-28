"""A bundle carries everything a direct import does.

The Redmine fixture is imported twice into an empty database: once directly,
once by exporting it to a bundle (Redmine adapter -> IR -> writer -> zip) and
loading the bundle. The two results are compared row by row, by natural key
rather than id: counts per table, every issue field, history with its authors
and dates, attachments by hash, wiki pages with every revision, and the rest.

Both imports run as dry runs and are captured at the end of their last phase,
just before the rollback. A dry run executes every write and reads every
attachment's bytes (it only skips copying them, and the search index), and its
rollback is what puts the database back to empty for the second import: a real
run commits phase by phase into the test's transaction, which cannot then be
undone half way.

Differences must be none, or explained here:

* **The import account.** Records without a resolvable author belong to an
  account named after the source: ``redmine-import`` for the direct import,
  ``bundle-import`` for the bundle. Both are compared as "the import account".
* **Issue references.** A direct Redmine import rewrites ``#2`` to a display
  key after loading. A bundle uses placeholders instead, so the export writes
  ``{{issue:2}}`` wherever the direct import would rewrite, which is in issue
  descriptions and comments, using the direct import's own matching rule.
* **The description in an issue's creation history.** The bundle import
  expands placeholders there too; the direct import leaves ``#2`` in it as
  written. Description changes are compared after applying the direct
  import's own rewrite to them.

Requires the fixture: ``make redmine-fixture-up && make redmine-fixture-seed``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import func, select

from specivo.importers.bundle.writer import BundleWriter, TextField, export_source, source_converter
from specivo.importers.core.ir import EntityType
from specivo.importers.core.pipeline import ImportPhase
from specivo.importers.load.reference_rewrite import rewrite_text
from specivo.models.attachment import Attachment
from specivo.models.import_id_map import ImportIdMapping
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

pytestmark = [
    pytest.mark.asyncio(loop_scope="function"),
    pytest.mark.integration,
    pytest.mark.redmine,
    pytest.mark.serial,
    pytest.mark.slow,
]

IMPORT_ACCOUNT = "<import account>"

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


async def _export(adapter, target) -> None:
    """Write the fixture as a bundle, as an importer for Redmine would."""
    issue_refs: set[str] = set()
    async for project in adapter.extract_projects():
        async for issue in adapter.extract_issues(project.source_ref):
            issue_refs.add(issue.source_ref)
    placeholders = {ref: f"{{{{issue:{ref}}}}}" for ref in issue_refs}
    markdown = source_converter(adapter)

    def convert(text: str, field: TextField) -> str:
        converted = markdown(text, field)
        if field is TextField.WIKI_TEXT:
            # The direct import leaves #123 in wiki text as written.
            return converted
        return rewrite_text(converted, placeholders)

    with BundleWriter(target, source_system="redmine", source_instance="pytest-fixture") as writer:
        await export_source(adapter, writer, convert)


class _Snapshot:
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
            "users": {self.logins[uid]: self._user(u) for uid, u in users.items() if not u.is_service_account},
            "groups": await self._groups(),
            "projects": {p.identifier: self._project(p) for p in projects.values()},
            "memberships": await self._memberships(),
            "versions": {(self.identifiers[v.project_id], v.name): self._version(v) for v in await self._all(Version)},
            "categories": sorted(
                (self.identifiers[c.project_id], c.name, self.logins.get(c.assigned_to_id))
                for c in await self._all(IssueCategory)
            ),
            "metadata_schemas": sorted(
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
            "relations": sorted(
                (self.keys[r.issue_from_id], self.keys[r.issue_to_id], r.relation_type, r.delay)
                for r in await self._all(IssueRelation)
            ),
            "watchers": sorted(
                (self.keys.get(w.issue_id) or str(self.pages.get(w.wiki_page_id)), self.logins[w.user_id])
                for w in await self._all(Watcher)
            ),
            "attachments": sorted(self._attachment(a) for a in await self._all(Attachment)),
            "wiki": await self._wiki(pages),
            "redirects": sorted(
                (self.identifiers[wikis[r.wiki_id]], r.title_from, r.redirected_to)
                for r in await self._all(WikiRedirect)
            ),
            "time_entries": sorted(self._time_entry(t) for t in await self._all(TimeEntry)),
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
        return {name: sorted(members) for name, members in groups.items()}

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
        return sorted(
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
            i.done_ratio,
            i.is_private,
            self._metadata(i.issue_metadata),
            i.created_at,
            # See the module docstring: a rewritten row is stamped by its run.
            i.updated_at,
            i.closed_on,
        )

    async def _journals(self) -> dict[str, list[tuple]]:
        # Source issue ref -> display key, for the direct import's #2 (see the
        # module docstring).
        refs = {
            source_id: self.keys[target_id]
            for source_id, target_id in (
                await self.session.execute(
                    select(ImportIdMapping.source_id, ImportIdMapping.target_id).where(
                        ImportIdMapping.entity_type == EntityType.ISSUE
                    )
                )
            ).all()
        }

        def value(detail: JournalDetail, text: str | None) -> str | None:
            if text and detail.property == "attr" and detail.prop_key == "description":
                return rewrite_text(text, refs)
            return text

        details: dict[int, list[tuple]] = {}
        for d in await self._all(JournalDetail):
            details.setdefault(d.journal_id, []).append(
                (d.property, d.prop_key, value(d, d.old_value), value(d, d.new_value))
            )
        journals: dict[str, list[tuple]] = {}
        for j in sorted(await self._all(Journal), key=lambda j: (j.issue_id, j.sequence)):
            journals.setdefault(self.keys[j.issue_id], []).append(
                (
                    j.sequence,
                    self.logins[j.user_id],
                    j.created_at,
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
                (c.version, self.logins[c.author_id], c.created_at, c.comments, c.text)
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
            "statuses": sorted((s.name, s.category, s.default_done_ratio) for s in await self._all(IssueStatus)),
            "trackers": sorted(
                (t.name, t.is_in_roadmap, tuple(t.disabled_core_fields), statuses.get(t.default_status_id))
                for t in await self._all(Tracker)
            ),
            "priorities": sorted((p.name, p.is_default, p.active) for p in await self._all(IssuePriority)),
            "activities": sorted((a.name, a.is_default, a.active) for a in await self._all(TimeEntryActivity)),
            "roles": sorted(
                (r.name, r.builtin, tuple(sorted(r.permissions or [])), r.users_visibility, r.time_entries_visibility)
                for r in await self._all(Role)
            ),
        }


def _differences(direct: dict[str, Any], bundle: dict[str, Any]) -> list[str]:
    """Return one readable line per difference, for the assertion message."""
    lines = []
    for section in direct:
        left, right = direct[section], bundle[section]
        if left == right:
            continue
        if isinstance(left, dict):
            for key in sorted(set(left) | set(right), key=str):
                if left.get(key) != right.get(key):
                    lines.append(f"{section}[{key}]:\n  direct {left.get(key)!r}\n  bundle {right.get(key)!r}")
        else:
            lines.append(f"{section}:\n  direct {left!r}\n  bundle {right!r}")
    return lines


async def _run_and_capture(pipeline, snapshot: _Snapshot) -> tuple[Any, dict[str, Any]]:
    """Run a dry-run *pipeline*, capturing what it wrote before it rolls back."""
    captured: dict[str, Any] = {}

    async def capture(ctx) -> None:
        captured.update(await snapshot.take())

    pipeline.register(ImportPhase.SEARCH_BACKFILL, capture)
    summary = await pipeline.run()
    assert captured, "the capture did not run"
    return summary, captured


async def test_a_bundle_import_matches_a_direct_import(
    db_session, redmine_adapter, build_import, build_bundle_import, tmp_path
):
    bundle = tmp_path / "redmine.zip"
    await _export(redmine_adapter, bundle)
    reference_fields = {
        field.key: field.field_format
        async for field in redmine_adapter.extract_custom_fields()
        if field.field_format in ("user", "version")
    }
    snapshot = _Snapshot(db_session, reference_fields)

    direct_summary, direct = await _run_and_capture(build_import(dry_run=True, merge_duplicate_statuses=True), snapshot)
    assert (await db_session.execute(select(func.count()).select_from(Issue))).scalar_one() == 0

    bundle_summary, via_bundle = await _run_and_capture(
        build_bundle_import(bundle, dry_run=True, merge_duplicate_statuses=True), snapshot
    )

    assert direct["counts"]["Issue"] > 0
    assert _differences(direct, via_bundle) == []
    assert sorted(w.message for w in bundle_summary.warnings) == sorted(w.message for w in direct_summary.warnings)


async def test_the_exported_bundle_is_valid(redmine_adapter, tmp_path):
    from specivo.importers.bundle import BundleSourceAdapter

    bundle = tmp_path / "redmine.zip"
    await _export(redmine_adapter, bundle)
    report = await BundleSourceAdapter(bundle).validate()
    assert report.ok, report.format_text()
    assert report.problems == []


async def test_reloading_the_exported_bundle_creates_nothing(
    db_session, redmine_adapter, build_bundle_import, tmp_path
):
    bundle = tmp_path / "redmine.zip"
    await _export(redmine_adapter, bundle)
    await build_bundle_import(bundle, merge_duplicate_statuses=True).run()
    before = {m.__name__: (await db_session.execute(select(func.count()).select_from(m))).scalar_one() for m in COUNTED}

    second = await build_bundle_import(bundle, merge_duplicate_statuses=True).run()

    after = {m.__name__: (await db_session.execute(select(func.count()).select_from(m))).scalar_one() for m in COUNTED}
    assert after == before
    assert sum(second.created.values()) == 0
