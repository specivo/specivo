"""Rewrite source issue references to Specivo display keys.

Redmine writes a reference to another issue as ``#123``; Specivo writes it as
``ACME-15``. The translation cannot happen while issues are being created,
because ``#123`` may point at an issue in a project imported later, so
descriptions and comments keep the source's form until every issue exists and
this pass rewrites them.

Only references that resolve to an imported issue are touched. A number that
matches nothing is left exactly as written: it may be a version number, a
quantity, or an issue that was deleted years ago, and turning it into a link to
something unrelated would be worse than leaving it alone.

Text inside fenced code blocks is skipped, so a shell prompt or a colour
literal in a code sample is not silently rewritten.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.importers.core.ir import EntityType
from specivo.importers.core.pipeline import PhaseContext
from specivo.models.issue import Issue
from specivo.models.journal import Journal

logger = logging.getLogger(__name__)

# Redmine's issue reference. The lookbehind keeps it from matching a CSS colour
# or an anchor in a URL.
_REFERENCE_RE = re.compile(r"(?<![\w&#])#(\d+)\b")

# Splits text on fenced code blocks, keeping the fences so they can be rejoined.
_FENCE_RE = re.compile(r"(```.*?```|~~~.*?~~~)", re.DOTALL)


async def rewrite_issue_references(ctx: PhaseContext) -> None:
    """Rewrite source references in issue descriptions and comments."""
    mapping = await _display_keys(ctx)
    if not mapping:
        return

    rewritten = await _rewrite_issues(ctx.session, mapping)
    rewritten += await _rewrite_journals(ctx.session, mapping)
    if rewritten:
        logger.info("Rewrote issue references in %d records", rewritten)
    ctx.tick(rewritten)


async def _display_keys(ctx: PhaseContext) -> dict[str, str]:
    """Return source issue reference to Specivo display key, for the whole run."""
    id_map = await ctx.id_map.preload(ctx.session, EntityType.ISSUE)
    if not id_map:
        return {}

    rows = (
        await ctx.session.execute(
            select(Issue.id, Issue.project_key, Issue.sequence_number).where(Issue.id.in_(list(id_map.values())))
        )
    ).all()
    by_id = {issue_id: f"{project_key}-{sequence}" for issue_id, project_key, sequence in rows}
    return {source_ref: by_id[target_id] for source_ref, target_id in id_map.items() if target_id in by_id}


def rewrite_text(text: str, mapping: dict[str, str]) -> str:
    """Return *text* with known references rewritten, code blocks untouched."""

    def replace(match: re.Match[str]) -> str:
        return mapping.get(match.group(1), match.group(0))

    parts = _FENCE_RE.split(text)
    for index, part in enumerate(parts):
        # Odd positions are the fenced blocks themselves.
        if index % 2 == 0:
            parts[index] = _REFERENCE_RE.sub(replace, part)
    return "".join(parts)


async def _rewrite_issues(session: AsyncSession, mapping: dict[str, str]) -> int:
    """Rewrite references in issue descriptions."""
    rows = (
        await session.execute(
            select(Issue.id, Issue.description).where(Issue.description.is_not(None), Issue.description.like("%#%"))
        )
    ).all()

    changed = 0
    for issue_id, description in rows:
        updated = rewrite_text(description, mapping)
        if updated == description:
            continue
        await session.execute(
            update(Issue)
            .where(Issue.id == issue_id)
            .values(description=updated)
            .execution_options(synchronize_session=False)
        )
        changed += 1
    return changed


async def _rewrite_journals(session: AsyncSession, mapping: dict[str, str]) -> int:
    """Rewrite references in comments."""
    rows = (
        await session.execute(
            select(Journal.id, Journal.notes).where(Journal.notes.is_not(None), Journal.notes.like("%#%"))
        )
    ).all()

    changed = 0
    for journal_id, notes in rows:
        updated = rewrite_text(notes, mapping)
        if updated == notes:
            continue
        await session.execute(
            update(Journal)
            .where(Journal.id == journal_id)
            .values(notes=updated)
            .execution_options(synchronize_session=False)
        )
        changed += 1
    return changed
