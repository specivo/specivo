"""JournalService — record issue field changes and comments."""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.core.exceptions import NotFoundError, ValidationError
from specivo.core.i18n import gettext as _
from specivo.core.utils import utcnow
from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.user import User

logger = logging.getLogger(__name__)

# Fields on Issue that are journalized when changed.
# Each tuple is (issue_attribute_name, prop_key_in_journal_details).
_JOURNALIZED_ATTRS: list[tuple[str, str]] = [
    ("tracker_id", "tracker_id"),
    ("status_id", "status_id"),
    ("priority_id", "priority_id"),
    ("subject", "subject"),
    ("description", "description"),
    ("assigned_to_id", "assigned_to_id"),
    ("category_id", "category_id"),
    ("start_date", "start_date"),
    ("due_date", "due_date"),
    ("estimated_hours", "estimated_hours"),
    ("done_ratio", "done_ratio"),
    ("is_private", "is_private"),
    ("fixed_version_id", "fixed_version_id"),
]


def _to_str(value: object) -> str | None:
    """Serialize a field value to string for storage in journal_details.

    None is preserved as None (means "field was not set / cleared").
    Empty string remains empty string (different from None).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


class JournalService:
    """Service layer for creating and listing journals."""

    async def _next_sequence(self, session: AsyncSession, issue_id: int) -> int:
        """Return the next sequence number for this issue's journals.

        Uses a PostgreSQL advisory lock keyed on ``issue_id`` to prevent
        race conditions when multiple transactions create journals for the
        same issue concurrently.  The lock is released automatically when
        the surrounding transaction commits or rolls back.
        """
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:issue_id)"),
            {"issue_id": issue_id},
        )
        result = await session.execute(
            select(func.coalesce(func.max(Journal.sequence), 0)).where(Journal.issue_id == issue_id)
        )
        current_max = result.scalar_one()
        return current_max + 1

    async def record_change(
        self,
        session: AsyncSession,
        issue: Issue,
        user: User,
        old_attrs: dict,
        new_attrs: dict,
        notes: str | None = None,
        api_key_id: int | None = None,
    ) -> Journal | None:
        """Diff old vs new issue attributes and create a journal entry.

        Creates ``JournalDetail`` rows for each field that changed.
        For description changes, stores the FULL old and new text (not a diff).

        Returns the created ``Journal``, or ``None`` when there are no changes
        and no notes (pure no-op update — nothing to record).

        Parameters
        ----------
        old_attrs:
            Snapshot of journalized attribute values taken BEFORE the update
            was applied to the issue.  Keys are issue attribute names.
        new_attrs:
            Snapshot taken AFTER the update.  Keys match old_attrs.
        """
        details: list[tuple[str, str, str | None, str | None]] = []

        for attr, prop_key in _JOURNALIZED_ATTRS:
            old_raw = old_attrs.get(attr)
            new_raw = new_attrs.get(attr)

            old_str = _to_str(old_raw)
            new_str = _to_str(new_raw)

            if old_str != new_str:
                details.append(("attr", prop_key, old_str, new_str))

        if not details and not notes:
            return None

        sequence = await self._next_sequence(session, issue.id)

        journal = Journal(
            issue_id=issue.id,
            wiki_page_id=None,
            project_id=issue.project_id,
            user_id=user.id,
            notes=notes,
            is_private=False,
            sequence=sequence,
            api_key_id=api_key_id,
        )
        session.add(journal)
        await session.flush()  # obtain journal.id for JournalDetail FKs

        for prop_type, prop_key, old_val, new_val in details:
            detail = JournalDetail(
                journal_id=journal.id,
                property=prop_type,
                prop_key=prop_key,
                old_value=old_val,
                new_value=new_val,
            )
            session.add(detail)

        logger.debug(
            "Recorded journal %d (seq=%d) for issue %s: %d detail(s), notes=%r",
            journal.id,
            sequence,
            issue.display_key,
            len(details),
            notes,
        )
        return journal

    async def add_comment(
        self,
        session: AsyncSession,
        issue: Issue,
        user: User,
        notes: str,
        api_key_id: int | None = None,
        reply_to_id: int | None = None,
    ) -> Journal:
        """Add a comment (notes-only journal, no field changes).

        Parameters
        ----------
        reply_to_id:
            Optional journal ID to reply to (1-level threading only).
            The referenced journal must exist, belong to the same issue,
            and itself not be a reply.
        Returns the created ``Journal``.
        """
        if reply_to_id is not None:
            result = await session.execute(select(Journal).where(Journal.id == reply_to_id))
            parent_journal = result.scalar_one_or_none()
            if parent_journal is None:
                raise NotFoundError(f"Journal {reply_to_id} not found")
            if parent_journal.issue_id != issue.id:
                raise ValidationError(_("Cannot reply to a journal on a different issue"))
            if parent_journal.reply_to_id is not None:
                raise ValidationError(_("Cannot reply to a reply (only 1-level threading is supported)"))

        sequence = await self._next_sequence(session, issue.id)

        journal = Journal(
            issue_id=issue.id,
            wiki_page_id=None,
            project_id=issue.project_id,
            user_id=user.id,
            notes=notes,
            is_private=False,
            sequence=sequence,
            api_key_id=api_key_id,
            reply_to_id=reply_to_id,
        )
        session.add(journal)
        await session.flush()

        logger.debug(
            "Added comment journal %d (seq=%d) for issue %s",
            journal.id,
            sequence,
            issue.display_key,
        )
        return journal

    async def resolve_thread(
        self,
        session: AsyncSession,
        journal_id: int,
        issue_id: int,
        user: User,
        summary: str,
    ) -> Journal:
        """Mark a journal thread as resolved.

        Parameters
        ----------
        journal_id:
            The journal to resolve.
        issue_id:
            The issue the journal must belong to (for authorization).
        user:
            The user performing the resolution.
        summary:
            A short description of the resolution.

        Raises ``NotFoundError`` if the journal does not exist or belongs
        to a different issue.
        """
        result = await session.execute(
            select(Journal)
            .where(Journal.id == journal_id, Journal.issue_id == issue_id)
            .options(selectinload(Journal.user))
        )
        journal = result.scalar_one_or_none()
        if journal is None:
            raise NotFoundError(f"Journal {journal_id} not found on this issue")

        journal.is_resolved = True
        journal.resolved_by_id = user.id
        journal.resolved_at = utcnow()
        journal.resolved_summary = summary
        await session.flush()
        return journal

    async def unresolve_thread(
        self,
        session: AsyncSession,
        journal_id: int,
        issue_id: int,
        user: User,
    ) -> Journal:
        """Clear resolution on a journal thread.

        Raises ``NotFoundError`` if the journal does not exist or belongs
        to a different issue.
        """
        result = await session.execute(
            select(Journal)
            .where(Journal.id == journal_id, Journal.issue_id == issue_id)
            .options(selectinload(Journal.user))
        )
        journal = result.scalar_one_or_none()
        if journal is None:
            raise NotFoundError(f"Journal {journal_id} not found on this issue")

        journal.is_resolved = False
        journal.resolved_by_id = None
        journal.resolved_at = None
        journal.resolved_summary = None
        await session.flush()
        return journal

    async def list_for_issue(
        self,
        session: AsyncSession,
        issue_id: int,
        include_private: bool = False,
    ) -> list[Journal]:
        """List journals for an issue, ordered by created_at ascending.

        Eagerly loads ``user`` and ``details`` relationships.
        """
        stmt = (
            select(Journal)
            .where(Journal.issue_id == issue_id)
            .options(
                selectinload(Journal.user),
                selectinload(Journal.details),
            )
            .order_by(Journal.created_at.asc())
        )

        if not include_private:
            stmt = stmt.where(Journal.is_private.is_(False))

        result = await session.execute(stmt)
        return list(result.scalars().all())
