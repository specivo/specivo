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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from specivo.importers.core.ir import IRAttachment, IRGroup, IRLookups, IRUser
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
        batch_size: int = 500,
    ) -> None:
        self._url = source_db_url
        self.source_files_dir = Path(source_files_dir) if source_files_dir else None
        # Defaults to the database host, which is what distinguishes two
        # Redmine installations imported into the same Specivo database.
        self.source_instance = source_instance or db.safe_url(source_db_url)
        self._status_overrides = {k.strip().lower(): v for k, v in (status_category_overrides or {}).items()}
        self._batch_size = batch_size
        self._engine: AsyncEngine | None = None
        self._source_format: str | None = None

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
    # Not yet implemented — added with the loaders that consume them
    # ------------------------------------------------------------------

    def _not_yet(self, what: str) -> AsyncIterator[Any]:
        raise NotImplementedError(f"Redmine {what} extraction is not implemented yet")

    def extract_projects(self) -> AsyncIterator[Any]:
        return self._not_yet("project")

    def extract_memberships(self, project_ref: str) -> AsyncIterator[Any]:
        return self._not_yet("membership")

    def extract_custom_fields(self) -> AsyncIterator[Any]:
        return self._not_yet("custom field")

    def extract_versions(self, project_ref: str) -> AsyncIterator[Any]:
        return self._not_yet("version")

    def extract_categories(self, project_ref: str) -> AsyncIterator[Any]:
        return self._not_yet("category")

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
