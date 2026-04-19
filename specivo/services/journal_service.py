"""JournalService — record issue field changes and comments."""

from __future__ import annotations

import json
import logging
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

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
    ("sprint_id", "sprint_id"),
    ("issue_metadata", "issue_metadata"),
]

# Cap on journal-detail values for structured (dict/list) fields.
# Prevents a single bulky metadata blob from bloating the history table
# or the UI diff view.
_STRUCTURED_VALUE_MAX_CHARS = 500


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
    if isinstance(value, (dict, list)):
        # Structured values (e.g. issue_metadata) are stored as a
        # compact JSON string, capped to ``_STRUCTURED_VALUE_MAX_CHARS``
        # to avoid blowing up the journal_details table with large
        # blobs.  The full value lives on the issue row itself.
        serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        if len(serialized) > _STRUCTURED_VALUE_MAX_CHARS:
            serialized = serialized[: _STRUCTURED_VALUE_MAX_CHARS - 3] + "..."
        return serialized
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

    async def record_relation_change(
        self,
        session: AsyncSession,
        issue: Issue,
        user: User,
        relation_type: str,
        other_issue_key: str,
        added: bool,
    ) -> Journal:
        """Record a relation add or remove in the issue journal.

        Parameters
        ----------
        relation_type:
            User-facing relation label (e.g. ``blocks``, ``blocked``).
        other_issue_key:
            Display key of the other issue (e.g. ``ACME-45``).
        added:
            ``True`` when the relation was created, ``False`` when removed.
        """
        sequence = await self._next_sequence(session, issue.id)
        journal = Journal(
            issue_id=issue.id,
            wiki_page_id=None,
            project_id=issue.project_id,
            user_id=user.id,
            notes=None,
            is_private=False,
            sequence=sequence,
        )
        session.add(journal)
        await session.flush()

        detail = JournalDetail(
            journal_id=journal.id,
            property="relation",
            prop_key=relation_type,
            old_value=None if added else other_issue_key,
            new_value=other_issue_key if added else None,
        )
        session.add(detail)

        logger.debug(
            "Recorded relation %s journal %d (seq=%d) for issue %s: %s %s",
            "add" if added else "remove",
            journal.id,
            sequence,
            issue.display_key,
            relation_type,
            other_issue_key,
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
            # Flatten reply-to-reply: redirect to the root parent (2-level max)
            if parent_journal.reply_to_id is not None:
                reply_to_id = parent_journal.reply_to_id
            if parent_journal.is_private and not user.is_admin:
                raise ValidationError("Cannot reply to a private note")

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

    async def count_comments(self, session: AsyncSession, issue_id: int) -> int:
        """Count journals for an issue that have a non-empty ``notes`` body.

        Pure field-change journals (``notes`` NULL or empty) are excluded —
        only real user comments are counted.
        """
        stmt = select(func.count(Journal.id)).where(
            Journal.issue_id == issue_id,
            Journal.notes.is_not(None),
            func.length(func.trim(Journal.notes)) > 0,
            Journal.is_private.is_(False),
        )
        result = await session.execute(stmt)
        return int(result.scalar_one())

    async def list_comments(
        self,
        session: AsyncSession,
        issue_id: int,
        limit: int = 10,
        offset: int = 0,
        order: str = "desc",
    ) -> tuple[list[Journal], int]:
        """Return a page of comment journals for an issue plus total count.

        Filters to journals with a non-empty ``notes`` body (real comments,
        not pure field-change entries). Eagerly loads the author.

        ``order`` must be ``"asc"`` or ``"desc"`` (by ``created_at``).
        """
        if order not in ("asc", "desc"):
            raise ValidationError("order must be 'asc' or 'desc'")

        base_where = (
            Journal.issue_id == issue_id,
            Journal.notes.is_not(None),
            func.length(func.trim(Journal.notes)) > 0,
            Journal.is_private.is_(False),
        )

        total_stmt = select(func.count(Journal.id)).where(*base_where)
        total = int((await session.execute(total_stmt)).scalar_one())

        order_col = Journal.created_at.asc() if order == "asc" else Journal.created_at.desc()
        stmt = (
            select(Journal)
            .where(*base_where)
            .options(joinedload(Journal.user))
            .order_by(order_col, Journal.id.asc() if order == "asc" else Journal.id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all()), total

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
                # joinedload collapses 1:1 author load into the main SELECT
                # (saves one round-trip vs. selectinload).
                joinedload(Journal.user),
                joinedload(Journal.resolved_by),
                # details is a collection — keep as selectinload (one extra query).
                selectinload(Journal.details),
            )
            .order_by(Journal.created_at.asc())
        )

        if not include_private:
            stmt = stmt.where(Journal.is_private.is_(False))

        result = await session.execute(stmt)
        return list(result.scalars().all())
