"""Redmine source adapter.

Connects to a Redmine database, reads it, and emits intermediate-representation
objects. It is the only place in the importer that knows Redmine's schema; the
loaders downstream see IR and nothing else.

Extraction of lookups, users and groups lives here. The issue, wiki, attachment
and time-entry streams are added by the tasks that build their loaders, so the
methods for them raise until then rather than returning empty results that would
look like an empty source.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncEngine

from specivo.importers.core.ir import (
    IRAttachment,
    IRCategory,
    IRCustomField,
    IRGroup,
    IRLookups,
    IRMembership,
    IRProject,
    IRUser,
    IRVersion,
    PrincipalKind,
)
from specivo.importers.redmine import db, extract

logger = logging.getLogger(__name__)

# Redmine's ``text_formatting`` setting. An instance switched to Markdown needs
# no Textile conversion, only link rewriting.
TEXTILE_FORMAT = "textile"
_MARKDOWN_FORMATS = frozenset({"markdown", "common_mark"})

# enumerations.type discriminators.
_PRIORITY_TYPE = "IssuePriority"
_ACTIVITY_TYPE = "TimeEntryActivity"


class RedmineSourceAdapter:
    """Reads a Redmine 7.x database.

    ``status_category_overrides`` maps a status name, lower-cased, to one of
    Specivo's status categories. Redmine records only whether a status closes an
    issue, so the rest is inferred and the operator gets the final say.
    """

    source_system: ClassVar[str] = "redmine"

    def __init__(
        self,
        source_db_url: str,
        source_files_dir: str | Path | None = None,
        source_instance: str | None = None,
        status_category_overrides: dict[str, str] | None = None,
        cf_key_overrides: dict[str, str] | None = None,
        batch_size: int = 500,
    ) -> None:
        self._url = source_db_url
        self.source_files_dir = Path(source_files_dir) if source_files_dir else None
        # Defaults to the database host, which is what distinguishes two
        # Redmine installations imported into the same Specivo database.
        self.source_instance = source_instance or db.safe_url(source_db_url)
        self._status_overrides = {k.strip().lower(): v for k, v in (status_category_overrides or {}).items()}
        self._cf_key_overrides = {k.strip().lower(): v for k, v in (cf_key_overrides or {}).items()}
        self._batch_size = batch_size
        self._engine: AsyncEngine | None = None
        self._source_format: str | None = None
        # Filled while streaming, for the import report.
        self._dropped_modules: dict[str, list[str]] = {}
        self.relaxed_required_fields: list[str] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def engine(self) -> AsyncEngine:
        """Return the connected engine, or explain that connect was not called."""
        if self._engine is None:
            raise RuntimeError("RedmineSourceAdapter.connect() must be called before reading the source")
        return self._engine

    async def connect(self) -> None:
        """Open the connection and read the settings the import depends on."""
        self._engine = db.create_source_engine(self._url)
        async with self._engine.connect() as conn:
            # Doubles as a reachability check: a bad URL or missing table fails
            # here rather than half way through a long import.
            self._source_format = (await db.fetch_setting(conn, "text_formatting")) or TEXTILE_FORMAT
        logger.info("Source text formatting: %s", self._source_format)

    async def close(self) -> None:
        """Dispose of the engine. Safe when connect failed or never ran."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    @property
    def source_format(self) -> str:
        """Return the markup Redmine stores, e.g. ``textile`` or ``markdown``."""
        return self._source_format or TEXTILE_FORMAT

    @property
    def needs_textile_conversion(self) -> bool:
        """Whether the source stores Textile rather than Markdown."""
        return self.source_format not in _MARKDOWN_FORMATS

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    async def extract_lookups(self) -> IRLookups:
        """Read trackers, statuses, priorities, activities and roles.

        These are instance-wide and small, so they are read in one pass rather
        than streamed.

        Priorities and activities live in Redmine's shared ``enumerations``
        table. Only the instance-wide rows are taken: a row with a
        ``project_id`` is a per-project override, which Specivo cannot express
        because its priorities and activities are global.
        """
        async with self.engine.connect() as conn:
            tracker_rows = (await conn.execute(select(db.trackers))).mappings().all()
            status_rows = (await conn.execute(select(db.issue_statuses))).mappings().all()
            priority_rows = (
                (
                    await conn.execute(
                        select(db.enumerations).where(
                            db.enumerations.c.type == _PRIORITY_TYPE,
                            db.enumerations.c.project_id.is_(None),
                        )
                    )
                )
                .mappings()
                .all()
            )
            activity_rows = (
                (
                    await conn.execute(
                        select(db.enumerations).where(
                            db.enumerations.c.type == _ACTIVITY_TYPE,
                            db.enumerations.c.project_id.is_(None),
                        )
                    )
                )
                .mappings()
                .all()
            )
            role_rows = (await conn.execute(select(db.roles))).mappings().all()

        return IRLookups(
            trackers=[extract.extract_tracker(dict(row)) for row in tracker_rows],
            statuses=[extract.extract_status(dict(row), self._status_overrides) for row in status_rows],
            priorities=[extract.extract_priority(dict(row)) for row in priority_rows],
            activities=[extract.extract_activity(dict(row)) for row in activity_rows],
            roles=[extract.extract_role(dict(row)) for row in role_rows],
        )

    async def count_project_enumerations(self) -> int:
        """Count per-project priority and activity overrides, which are dropped.

        Reported so an operator who relies on them learns that they did not
        survive, instead of discovering it later.
        """
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(db.enumerations.c.id).where(
                        db.enumerations.c.type.in_([_PRIORITY_TYPE, _ACTIVITY_TYPE]),
                        db.enumerations.c.project_id.is_not(None),
                    )
                )
            ).all()
        return len(rows)

    # ------------------------------------------------------------------
    # Principals
    # ------------------------------------------------------------------

    async def extract_users(self) -> AsyncIterator[IRUser]:
        """Stream people, with their default email address.

        Groups and the anonymous account share this table and are excluded:
        groups come from :meth:`extract_groups`, and Specivo represents
        anonymous access with a role rather than an account.
        """
        async with self.engine.connect() as conn:
            emails = await self._default_emails(conn)
            stmt = select(db.users).where(db.users.c.type == extract.TYPE_USER)
            async for row in db.stream(conn, stmt, db.users.c.id, self._batch_size):
                yield extract.extract_user(row, emails.get(row["id"]))

    async def extract_groups(self) -> AsyncIterator[IRGroup]:
        """Stream groups with their members.

        Specivo cannot hold project roles on a group, so these exist only to be
        flattened into per-user memberships. Membership is read up front: a
        Redmine instance has few groups, and the join table has no primary key
        to page on.
        """
        async with self.engine.connect() as conn:
            members: dict[int, list[str]] = {}
            membership_rows = (await conn.execute(select(db.groups_users))).mappings().all()
            for membership in membership_rows:
                members.setdefault(membership["group_id"], []).append(str(membership["user_id"]))

            stmt = select(db.users).where(db.users.c.type == extract.TYPE_GROUP)
            async for row in db.stream(conn, stmt, db.users.c.id, self._batch_size):
                yield extract.extract_group(row, members.get(row["id"], []))

    async def _default_emails(self, conn: Any) -> dict[int, str]:
        """Return each user's default address.

        Redmine allows several addresses per account; the default one is the
        login address. Read in one pass rather than a query per user.
        """
        stmt = select(db.email_addresses.c.user_id, db.email_addresses.c.address).where(
            db.email_addresses.c.is_default.is_(True)
        )
        return {user_id: address for user_id, address in (await conn.execute(stmt)).all()}

    # ------------------------------------------------------------------
    # Projects and project-scoped entities
    # ------------------------------------------------------------------

    async def extract_projects(self) -> AsyncIterator[IRProject]:
        """Stream projects with their enabled modules, parents before children.

        Redmine keeps projects in a nested set, so ordering by ``lft`` yields a
        parent before any of its descendants and no topological sort is needed.
        Read in one pass rather than paged: an instance has hundreds of
        projects, not hundreds of thousands, and paging by key would break the
        ordering that makes the parent guarantee hold.
        """
        async with self.engine.connect() as conn:
            modules: dict[int, list[str]] = {}
            for row in (await conn.execute(select(db.enabled_modules))).mappings().all():
                modules.setdefault(row["project_id"], []).append(row["name"])

            rows = (await conn.execute(select(db.projects).order_by(db.projects.c.lft))).mappings().all()

        for row in rows:
            project = extract.extract_project(dict(row))
            mapped, dropped = extract.map_modules(modules.get(row["id"], []))
            project.modules = mapped
            self._dropped_modules[project.source_ref] = dropped
            yield project

    def dropped_modules(self, project_ref: str) -> list[str]:
        """Return the modules of *project_ref* that Specivo has no equivalent for."""
        return self._dropped_modules.get(project_ref, [])

    async def extract_versions(self, project_ref: str) -> AsyncIterator[IRVersion]:
        """Stream a project's versions."""
        async with self.engine.connect() as conn:
            stmt = select(db.versions).where(db.versions.c.project_id == int(project_ref))
            for row in (await conn.execute(stmt.order_by(db.versions.c.id))).mappings().all():
                yield extract.extract_version(dict(row))

    async def extract_categories(self, project_ref: str) -> AsyncIterator[IRCategory]:
        """Stream a project's issue categories."""
        async with self.engine.connect() as conn:
            stmt = select(db.issue_categories).where(db.issue_categories.c.project_id == int(project_ref))
            for row in (await conn.execute(stmt.order_by(db.issue_categories.c.id))).mappings().all():
                yield extract.extract_category(dict(row))

    async def extract_memberships(self, project_ref: str) -> AsyncIterator[IRMembership]:
        """Stream a project's memberships, for people and for groups.

        Only directly granted roles are emitted. Redmine also stores the grants
        a user inherits from a group as rows with ``inherited_from`` set;
        emitting those as well would double-count, since the importer derives
        them itself when it flattens the group.
        """
        async with self.engine.connect() as conn:
            member_rows = (
                (
                    await conn.execute(
                        select(db.members).where(db.members.c.project_id == int(project_ref)).order_by(db.members.c.id)
                    )
                )
                .mappings()
                .all()
            )
            if not member_rows:
                return

            member_ids = [row["id"] for row in member_rows]
            role_rows = (
                (
                    await conn.execute(
                        select(db.member_roles).where(
                            db.member_roles.c.member_id.in_(member_ids),
                            db.member_roles.c.inherited_from.is_(None),
                        )
                    )
                )
                .mappings()
                .all()
            )
            roles_by_member: dict[int, list[str]] = {}
            for role_row in role_rows:
                roles_by_member.setdefault(role_row["member_id"], []).append(str(role_row["role_id"]))

            principal_ids = [row["user_id"] for row in member_rows]
            kinds = {
                user_id: principal_type
                for user_id, principal_type in (
                    await conn.execute(select(db.users.c.id, db.users.c.type).where(db.users.c.id.in_(principal_ids)))
                ).all()
            }

        for row in member_rows:
            principal_type = kinds.get(row["user_id"])
            if principal_type == extract.TYPE_USER:
                kind = PrincipalKind.USER
            elif principal_type == extract.TYPE_GROUP:
                kind = PrincipalKind.GROUP
            else:
                # The builtin non-member and anonymous group principals, whose
                # access Specivo expresses with a role rather than a membership.
                continue

            roles = roles_by_member.get(row["id"], [])
            if not roles:
                continue
            yield extract.extract_membership(dict(row), roles, kind)

    async def extract_custom_fields(self) -> AsyncIterator[IRCustomField]:
        """Stream issue custom fields with their scope and choices.

        Fields defined on users, projects, versions, groups and time entries are
        skipped: Specivo's metadata schemas only target issues today. They are
        counted so the report can say what was dropped.
        """
        async with self.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(db.custom_fields)
                        .where(db.custom_fields.c.type == extract.ISSUE_CUSTOM_FIELD_TYPE)
                        .order_by(db.custom_fields.c.id)
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                return

            field_ids = [row["id"] for row in rows]
            trackers_by_field: dict[int, list[str]] = {}
            for link in (
                (
                    await conn.execute(
                        select(db.custom_fields_trackers).where(
                            db.custom_fields_trackers.c.custom_field_id.in_(field_ids)
                        )
                    )
                )
                .mappings()
                .all()
            ):
                trackers_by_field.setdefault(link["custom_field_id"], []).append(str(link["tracker_id"]))

            projects_by_field: dict[int, list[str]] = {}
            for link in (
                (
                    await conn.execute(
                        select(db.custom_fields_projects).where(
                            db.custom_fields_projects.c.custom_field_id.in_(field_ids)
                        )
                    )
                )
                .mappings()
                .all()
            ):
                projects_by_field.setdefault(link["custom_field_id"], []).append(str(link["project_id"]))

            choices_by_field: dict[int, list[str]] = {}
            for choice in (
                (
                    await conn.execute(
                        select(db.custom_field_enumerations)
                        .where(db.custom_field_enumerations.c.custom_field_id.in_(field_ids))
                        .order_by(db.custom_field_enumerations.c.position)
                    )
                )
                .mappings()
                .all()
            ):
                choices_by_field.setdefault(choice["custom_field_id"], []).append(choice["name"])

            # A field marked required in Redmine may still have issues with no
            # value: it was made required later, or the value was cleared by an
            # import. Declaring it required in the schema would then reject the
            # very data being imported, so requiredness is only kept when every
            # existing value is filled in.
            blank_fields = set(
                (
                    await conn.execute(
                        select(db.custom_values.c.custom_field_id)
                        .where(
                            db.custom_values.c.custom_field_id.in_(field_ids),
                            db.custom_values.c.customized_type == "Issue",
                            or_(db.custom_values.c.value.is_(None), db.custom_values.c.value == ""),
                        )
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )

        for row in rows:
            data = dict(row)
            choices = choices_by_field.get(row["id"]) or extract.parse_possible_values(row.get("possible_values"))
            field = extract.extract_custom_field(
                data,
                tracker_refs=trackers_by_field.get(row["id"], []),
                project_refs=projects_by_field.get(row["id"], []),
                choices=choices,
                key=self._cf_key_overrides.get((row.get("name") or "").strip().lower()),
            )
            if field.is_required and row["id"] in blank_fields:
                field.is_required = False
                self.relaxed_required_fields.append(field.name)
            yield field

    async def count_non_issue_custom_fields(self) -> int:
        """Count custom fields on entities Specivo cannot attach metadata to."""
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(db.custom_fields.c.id).where(db.custom_fields.c.type != extract.ISSUE_CUSTOM_FIELD_TYPE)
                )
            ).all()
        return len(rows)

    # ------------------------------------------------------------------
    # Not yet implemented — added with the loaders that consume them
    # ------------------------------------------------------------------

    def _not_yet(self, what: str) -> AsyncIterator[Any]:
        raise NotImplementedError(f"Redmine {what} extraction is not implemented yet")

    def extract_issues(self, project_ref: str) -> AsyncIterator[Any]:
        return self._not_yet("issue")

    def extract_journals(self, project_ref: str) -> AsyncIterator[Any]:
        return self._not_yet("journal")

    def extract_relations(self, project_ref: str) -> AsyncIterator[Any]:
        return self._not_yet("relation")

    def extract_watchers(self, project_ref: str) -> AsyncIterator[Any]:
        return self._not_yet("watcher")

    def extract_attachments(self, project_ref: str) -> AsyncIterator[Any]:
        return self._not_yet("attachment")

    def extract_wiki_pages(self, project_ref: str) -> AsyncIterator[Any]:
        return self._not_yet("wiki page")

    def extract_time_entries(self, project_ref: str) -> AsyncIterator[Any]:
        return self._not_yet("time entry")

    def resolve_attachment_path(self, attachment: IRAttachment) -> Path:
        raise NotImplementedError("Attachment path resolution is not implemented yet")
