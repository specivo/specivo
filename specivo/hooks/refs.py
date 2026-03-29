"""Commit message reference parsing and issue linking.

Parses commit messages for patterns like:
- refs #PROJECT-NNN
- fixes #PROJECT-NNN

Creates a journal entry on each referenced issue with commit info.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.user import User

logger = logging.getLogger(__name__)

# Match patterns: refs #KEY-123 or fixes #KEY-123
_REF_PATTERN = re.compile(r"(?:refs|fixes)\s+#([A-Z][A-Z0-9]+-\d+)", re.IGNORECASE)


def extract_issue_refs(message: str) -> list[str]:
    """Extract issue display keys from a commit message.

    Returns a list of display keys like ["ACME-42", "SPV-7"].
    """
    return [m.upper() for m in _REF_PATTERN.findall(message)]


async def link_commit_to_issues(
    session: AsyncSession,
    commit_id: str,
    commit_message: str,
    commit_url: str,
    author_name: str,
) -> list[int]:
    """Parse commit message for issue refs and create journal entries.

    Returns list of issue IDs that were linked.
    """
    refs = extract_issue_refs(commit_message)
    if not refs:
        return []

    linked_ids: list[int] = []

    for display_key in refs:
        parts = display_key.rsplit("-", 1)
        if len(parts) != 2:
            continue
        project_key, seq_str = parts
        try:
            sequence_number = int(seq_str)
        except ValueError:
            continue

        # Find the issue
        stmt = select(Issue).where(
            Issue.project_key == project_key,
            Issue.sequence_number == sequence_number,
        )
        result = await session.execute(stmt)
        issue = result.scalar_one_or_none()
        if issue is None:
            logger.debug("Issue %s not found, skipping commit link", display_key)
            continue

        # Find a system user for the journal entry (use the issue author as fallback)
        user_result = await session.execute(select(User).where(User.id == issue.author_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            logger.warning("Author user %d not found for issue %s", issue.author_id, display_key)
            continue

        # Determine next journal sequence
        from sqlalchemy import func

        seq_result = await session.execute(
            select(func.coalesce(func.max(Journal.sequence), 0)).where(Journal.issue_id == issue.id)
        )
        next_seq = seq_result.scalar_one() + 1

        # Build the commit note
        short_hash = commit_id[:12] if len(commit_id) > 12 else commit_id
        note = f"Commit [{short_hash}]({commit_url}) by {author_name}:\n> {commit_message.strip().splitlines()[0]}"

        journal = Journal(
            issue_id=issue.id,
            wiki_page_id=None,
            project_id=issue.project_id,
            user_id=user.id,
            notes=note,
            is_private=False,
            sequence=next_seq,
        )
        session.add(journal)
        linked_ids.append(issue.id)

        logger.info(
            "Linked commit %s to issue %s (journal seq=%d)",
            short_hash,
            display_key,
            next_seq,
        )

    if linked_ids:
        await session.flush()

    return linked_ids
