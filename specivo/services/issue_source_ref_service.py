"""The number an imported issue had in its source system.

Written by the importer. Read by the issue page, the issue API and MCP, and by
the ``/imported/{system}/{ref}/`` route, which only ever answers for issues the
reader may see. Search reads the table in its own SQL, under its own visibility
rules (``SearchService._identifier_hits``).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from specivo.models.issue import Issue
from specivo.models.issue_source_ref import IssueSourceRef
from specivo.schemas.issue import ImportedFromOut
from specivo.services.issue_service import IssueService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from specivo.models.user import User

SOURCE_SYSTEM_MAX = 30
SOURCE_REF_MAX = 255

_issue_svc = IssueService()


def is_plain_number(key: str) -> bool:
    """Return whether *key* is a bare ticket number, as opposed to a key like ``PROJ-7``."""
    return key.isascii() and key.isdigit()


def source_ref_label(source_label: str, key: str) -> str:
    """Return how a number is shown: ``RT #123`` for a bare number, otherwise the key as it stands.

    The migration's backfill applies the same rule in SQL.
    """
    return f"{source_label} #{key}" if is_plain_number(key) else key


async def record_source_refs(
    session: AsyncSession,
    issue_id: int,
    *,
    source_system: str,
    source_label: str,
    source_ref: str,
    source_key: str | None,
    aliases: Iterable[str],
) -> None:
    """Store the numbers of one imported issue, refreshing what an earlier run stored.

    The number the bundle gives now is the primary one. One stored as primary
    before stays as an alias, since a key an issue used to have still means it,
    with one exception: the source's own id once a key replaces it (a run from
    before bundles could name the key). Nobody knows that id, and a search for
    the same digits should not find this issue.

    Aliases are only added: a bundle covering part of a source says nothing
    about the numbers it leaves out. An issue deleted since an earlier run gets
    nothing, while its identifier map row remains.
    """
    if await session.scalar(select(Issue.id).where(Issue.id == issue_id)) is None:
        return

    key = (source_key or source_ref).strip()
    numbers = {number: False for number in (alias.strip() for alias in aliases) if number}
    numbers[key] = True

    this_system = (IssueSourceRef.issue_id == issue_id, IssueSourceRef.source_system == source_system)
    if key != source_ref:
        await session.execute(
            delete(IssueSourceRef)
            .where(*this_system, IssueSourceRef.is_primary.is_(True), IssueSourceRef.ref == source_ref)
            .execution_options(synchronize_session=False)
        )
    await session.execute(
        update(IssueSourceRef)
        .where(*this_system, IssueSourceRef.is_primary.is_(True), IssueSourceRef.ref != key)
        .values(is_primary=False)
        .execution_options(synchronize_session=False)
    )
    stmt = pg_insert(IssueSourceRef).values(
        [
            {
                "issue_id": issue_id,
                "source_system": source_system,
                "ref": number,
                "label": source_ref_label(source_label, number),
                "is_primary": primary,
            }
            for number, primary in numbers.items()
        ]
    )
    await session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_issue_source_refs_issue_system_ref",
            set_={"label": stmt.excluded.label, "is_primary": stmt.excluded.is_primary},
        )
    )


async def imported_from(session: AsyncSession, issue_id: int) -> ImportedFromOut | None:
    """Return where an issue came from, or None for one that was not imported.

    Callers have already decided the reader may see the issue. The label is no
    secret on such an issue, and the table holds no source host name to leak.
    """
    rows = (
        (
            await session.execute(
                select(IssueSourceRef)
                .where(IssueSourceRef.issue_id == issue_id)
                .order_by(IssueSourceRef.is_primary.desc(), IssueSourceRef.id)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    primary, *others = rows
    return ImportedFromOut(
        system=primary.source_system,
        ref=primary.ref,
        label=primary.label,
        aliases=[row.ref for row in others],
    )


async def visible_issue_keys(session: AsyncSession, user: User, system: str, ref: str) -> list[str]:
    """Return the display keys of the issues *ref* of *system* means, among those *user* may see.

    Oldest first. A value no column could hold names nothing, and is not sent
    to the database at all, so a NUL byte in a URL is an ordinary miss.
    """
    if not 0 < len(system) <= SOURCE_SYSTEM_MAX or not 0 < len(ref) <= SOURCE_REF_MAX:
        return []
    if "\x00" in system or "\x00" in ref:
        return []

    stmt = (
        select(Issue.project_key, Issue.sequence_number)
        .join(IssueSourceRef, IssueSourceRef.issue_id == Issue.id)
        .where(IssueSourceRef.source_system == system.lower(), func.lower(IssueSourceRef.ref) == func.lower(ref))
        .group_by(Issue.id)
        .order_by(Issue.id)
    )
    # Private issues, private projects and an API key's project limit all land
    # here, and leave the same empty answer as a number nobody ever had.
    visible = await _issue_svc.visible_issues_clause(session, user)
    if visible is not None:
        stmt = stmt.where(visible)
    return [f"{project_key}-{sequence}" for project_key, sequence in (await session.execute(stmt)).all()]
