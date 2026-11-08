"""Similar issues: the issues of a project whose subject resembles a new issue's title.

Lexical, so it works with no AI or embedding model installed. The title's
terms are OR-ed into a tsquery on the subject part (weight A) of
``issues.search_vector``; the best candidates by ``ts_rank`` are then reranked
by ``pg_trgm``'s ``similarity()`` against the subject, and only those above
``SIMILAR_ISSUES_MIN_SCORE`` are kept.

The ranking statement runs in a savepoint with its own ``statement_timeout``;
a timeout answers with no suggestions. The savepoint is always rolled back, so
the ``SET LOCAL`` never outlives it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Integer, Select, String, bindparam, case, cast, func, literal_column, select, text
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from specivo.models.issue import Issue
from specivo.models.lookups import IssueStatus
from specivo.models.project import Project
from specivo.models.setting import Setting
from specivo.models.user import User
from specivo.services.issue_service import IssueService
from specivo.services.similar_issues import (
    CANDIDATE_LIMIT,
    RECENCY_SCAN_LIMIT,
    SIMILAR_ISSUES_MIN_SCORE,
    SIMILAR_ISSUES_SETTING_KEY,
    STATEMENT_TIMEOUT_MS,
    instance_setting_enabled,
    normalize_query,
    project_setting_enabled,
    uses_recency_scan,
)

logger = logging.getLogger(__name__)

_QUERY_CANCELED = "57014"

# The title's lexemes, built exactly as the index trigger builds the subject's
# (``_`` read as a space, identifier lexemes added), each quoted and limited
# to weight A, joined with OR. NULL when the title has no lexeme at all.
# ``quote_literal`` is not used: for text with a backslash it can return
# ``E'...'``, which is not tsquery syntax. ``\:`` keeps ``text()`` from
# reading ``:A`` as a bind parameter.
_TSQUERY_TEXT = text(
    r"""
    SELECT string_agg('''' || replace(replace(l, '\', '\\'), '''', '''''') || '''\:A', ' | ')
    FROM unnest(tsvector_to_array(
        to_tsvector(specivo_fts_config(:pid), specivo_fts_text(replace(:q, '_', ' ')))
        || specivo_fts_identifiers(:q))) AS l
    """
).bindparams(bindparam("pid", type_=Integer), bindparam("q", type_=String))


@dataclass(frozen=True, slots=True)
class SimilarIssue:
    """One suggestion: an issue, its status and how closely its subject matches."""

    key: str
    subject: str
    status_id: int
    status_name: str
    is_closed: bool
    score: float


class SimilarIssueService:
    """Find issues similar to a title within one project. Stateless."""

    _issue_service = IssueService()

    async def is_enabled(self, session: AsyncSession, project: Project) -> bool:
        """Whether suggestions are on for *project*: the instance switch and the project's; instance off wins."""
        value = await session.scalar(select(Setting.value).where(Setting.key == SIMILAR_ISSUES_SETTING_KEY))
        return instance_setting_enabled(value) and project_setting_enabled(project.settings)

    async def find(self, session: AsyncSession, project: Project, user: User, q: str, limit: int) -> list[SimilarIssue]:
        """Return up to *limit* issues of *project* that *user* may see, most similar to *q* first.

        Ties go to open issues (a "done" status counts as open), then to the
        newest. *q* is never logged: a title can be private.
        """
        q = normalize_query(q)
        if not q:
            return []

        visibility = await self._issue_service.project_visible_issues_clause(session, user, project)

        tsq: str | None = None
        if not uses_recency_scan(q):
            tsq = await session.scalar(_TSQUERY_TEXT, {"pid": project.id, "q": q})
            if tsq is None:
                return []
        stmt = self._statement(project, q, tsq, visibility, limit)

        savepoint = await session.begin_nested()
        try:
            await session.execute(text(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}"))
            rows = (await session.execute(stmt)).all()
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != _QUERY_CANCELED:
                raise
            logger.info("Similar issues query timed out in project %s", project.id)
            rows = []
        finally:
            # Rolled back on success too: a released savepoint would keep the
            # SET LOCAL until the end of the transaction.
            if savepoint.is_active:
                await savepoint.rollback()

        return [
            SimilarIssue(
                key=f"{row.project_key}-{row.sequence_number}",
                subject=row.subject,
                status_id=row.status_id,
                status_name=row.status_name,
                is_closed=row.is_closed,
                score=float(row.score),
            )
            for row in rows
        ]

    def _statement(
        self,
        project: Project,
        q: str,
        tsq: str | None,
        visibility: ColumnElement[bool] | None,
        limit: int,
    ) -> Select[Any]:
        """The candidate subquery (visibility, then the cut) and the similarity rerank around it.

        Candidates are the best full-text matches of *tsq*, or, without one
        (a title in an unsegmented script), the newest issues.
        """
        candidates = select(Issue.id, Issue.project_key, Issue.sequence_number, Issue.subject, Issue.status_id).where(
            Issue.project_id == project.id
        )
        if tsq is None:
            candidates = candidates.order_by(Issue.id.desc()).limit(RECENCY_SCAN_LIMIT)
        else:
            search_vector: ColumnElement[Any] = literal_column("issues.search_vector")
            query = cast(bindparam("tsq", tsq, type_=String), TSQUERY)
            candidates = (
                candidates.where(search_vector.op("@@")(query))
                .order_by(func.ts_rank(search_vector, query).desc(), Issue.id.desc())
                .limit(CANDIDATE_LIMIT)
            )
        # Visibility narrows the candidates before the cut, so issues the
        # user cannot see never take a candidate's place.
        if visibility is not None:
            candidates = candidates.where(visibility)
        cand = candidates.subquery("candidates")

        score = func.similarity(cand.c.subject, bindparam("q", q, type_=String))
        is_closed = case((IssueStatus.category == "closed", True), else_=False)
        return (
            select(
                cand.c.project_key,
                cand.c.sequence_number,
                cand.c.subject,
                IssueStatus.id.label("status_id"),
                IssueStatus.name.label("status_name"),
                is_closed.label("is_closed"),
                score.label("score"),
            )
            .join(IssueStatus, IssueStatus.id == cand.c.status_id)
            .where(score > SIMILAR_ISSUES_MIN_SCORE)
            .order_by(score.desc(), is_closed, cand.c.id.desc())
            .limit(limit)
        )
