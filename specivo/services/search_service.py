"""Full-text search service — unified search across issues and wiki pages.

Extended with:
- Semantic search via pgvector cosine similarity
- Hybrid search via Reciprocal Rank Fusion (RRF, k=60)

Extended in M7.2/M7.3 with:
- Per-user visibility filtering (access control)
- Multi-project search
- Metadata filtering (tracker, status, priority, dates, JSONB)
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.constants import (
    RRF_K,
    SEARCH_FTS_HEADLINE_OPTIONS,
    SEARCH_HYBRID_PREFETCH_LIMIT,
    SEARCH_SNIPPET_MAX_CHARS,
)
from specivo.models.user import User
from specivo.schemas.search import (
    SOURCE_TYPE_TO_DISPLAY,
    SearchFilters,
    SearchResult,
    SearchResultType,
    SearchSourceType,
)

# Shortcuts for f-string interpolation into raw SQL. These expand to the
# enum member's string value (e.g. ``_SST_ATTACHMENT == "attachment"``). Values
# are constants — never user input — so f-string interpolation is safe.
_SRT_ISSUE = SearchResultType.ISSUE.value
_SRT_WIKI = SearchResultType.WIKI.value
_SRT_COMMENT = SearchResultType.COMMENT.value
_SRT_ATTACHMENT = SearchResultType.ATTACHMENT.value

_SST_ISSUE = SearchSourceType.ISSUE.value
_SST_WIKI_PAGE = SearchSourceType.WIKI_PAGE.value
_SST_JOURNAL = SearchSourceType.JOURNAL.value
_SST_ATTACHMENT = SearchSourceType.ATTACHMENT.value

logger = logging.getLogger(__name__)


def rrf_fuse(fts_ids: list[int], sem_ids: list[int], k: int = RRF_K) -> list[int]:
    """Merge two ranked ID lists using Reciprocal Rank Fusion.

    RRF score for item i = sum over lists L of: 1 / (k + rank_L(i))
    where rank_L(i) is the 1-based rank in list L (0 if absent).

    Note: hybrid_search uses an inline version for string-keyed dedup.
    This function is retained for unit test coverage of the RRF algorithm.
    """
    scores: dict[int, float] = {}
    for rank_0, item_id in enumerate(fts_ids):
        scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank_0 + 1)
    for rank_0, item_id in enumerate(sem_ids):
        scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank_0 + 1)
    return sorted(scores, key=lambda x: scores[x], reverse=True)


# Complete wiki links: [[target|display]] or [[target]]
_WIKI_LINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")
# Partial wiki links truncated by ts_headline:
#   "slug|Display Text]]"  (missing [[)
#   "[[slug|Display Text"  (missing ]])
#   "slug|Display Text"    (missing both)
_PARTIAL_LINK_OPEN_RE = re.compile(r"\[\[([a-z0-9_-]+(?:\|[^\]]*?)?)$")
_PARTIAL_LINK_CLOSE_RE = re.compile(r"^([^\[|]*?\|)?([^\]]*?)\]\]")
_PARTIAL_LINK_MID_RE = re.compile(r"([a-z0-9_-]+)\|([A-Z][^\]]{0,60})\]\]")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_HEADING_RE = re.compile(r"^#{1,3}\s+", re.MULTILINE)


def _clean_snippet(snippet: str | None) -> str | None:
    """Strip wiki link markup and markdown formatting from search snippets.

    Handles complete ``[[target|display]]`` links and partial links
    truncated by ``ts_headline`` (e.g. ``slug|Display Text]]`` without
    opening brackets, or ``[[slug|Display`` without closing brackets).

    The result is HTML-escaped with only ``<mark>`` tags preserved for
    search-term highlighting (safe for ``| safe`` in templates).
    """
    if not snippet:
        return snippet
    # Replace <mark> tags with unique placeholders so wiki link regex
    # matches cleanly and HTML-escaping doesn't destroy them.
    clean = snippet.replace("<mark>", "\x00MARK_OPEN\x00").replace("</mark>", "\x00MARK_CLOSE\x00")
    # Complete links: [[target|display]] → display; [[target]] → target
    clean = _WIKI_LINK_RE.sub(
        lambda m: m.group(2) or m.group(1).replace("_", " "),
        clean,
    )
    # Partial link at end: [[slug|Display... → Display...
    clean = _PARTIAL_LINK_OPEN_RE.sub(
        lambda m: m.group(1).split("|")[-1] if "|" in m.group(1) else "",
        clean,
    )
    # Partial link at start: ...Display]] → Display
    clean = _PARTIAL_LINK_CLOSE_RE.sub(
        lambda m: m.group(2) if m.group(2) else "",
        clean,
    )
    # Mid-snippet partial: slug|Display Text]] → Display Text
    clean = _PARTIAL_LINK_MID_RE.sub(lambda m: m.group(2), clean)
    # Strip bold/italic markdown and heading markers
    clean = _BOLD_RE.sub(r"\1", clean)
    clean = _ITALIC_RE.sub(r"\1", clean)
    clean = _HEADING_RE.sub("", clean)
    # HTML-escape to prevent XSS, then restore safe <mark> tags
    clean = html.escape(clean)
    clean = clean.replace("\x00MARK_OPEN\x00", "<mark>").replace("\x00MARK_CLOSE\x00", "</mark>")
    return clean


def _empty_type_counts(*, include_all: bool = False) -> dict[str, int]:
    """Return a zeroed type_counts dict."""
    counts = {
        "issues": 0,
        "wiki": 0,
        "comments": 0,
        "attachments": 0,
    }
    if include_all:
        counts["all"] = 0
    return counts


class SearchService:
    """Unified full-text search across issues and wiki pages.

    Uses PostgreSQL tsvector columns (maintained by triggers) with
    plainto_tsquery for user input, ts_rank_cd for ranking, and
    ts_headline for snippet highlighting.

    Weight A (title/subject) ranks higher than weight B (description/text).

    SECURITY: The FTS language (regconfig) is always passed as a bind
    parameter (``CAST(:fts_lang AS regconfig)``) rather than interpolated
    into SQL strings. Although the language value is validated against an
    allowlist at config load time, parameterized queries provide defense
    in depth against SQL injection if the validation is ever bypassed.
    """

    # ------------------------------------------------------------------
    # Visibility SQL builders
    # ------------------------------------------------------------------

    def _issue_visibility_clause(self, user: User, alias: str = "i") -> str:
        """Generate SQL fragment that enforces issue visibility for non-admin users.

        Args:
            user: The authenticated user.
            alias: Table alias for the issues table (e.g. "i" or "i2").

        Returns:
            SQL AND clause (including the leading AND), or empty string for admins.
        """
        if user.is_admin:
            return ""

        return f"""
            AND (
                -- Member with "all" or "default" visibility: see non-private + own private
                (EXISTS (SELECT 1 FROM members m
                         JOIN member_roles mr ON mr.member_id = m.id
                         JOIN roles r ON r.id = mr.role_id
                         WHERE m.user_id = :current_user_id AND m.project_id = {alias}.project_id
                         AND r.issues_visibility IN ('all', 'default'))
                 AND ({alias}.is_private = false
                      OR {alias}.author_id = :current_user_id
                      OR {alias}.assigned_to_id = :current_user_id))
                OR
                -- Member with "own" only: author/assignee only
                (EXISTS (SELECT 1 FROM members m
                         JOIN member_roles mr ON mr.member_id = m.id
                         JOIN roles r ON r.id = mr.role_id
                         WHERE m.user_id = :current_user_id AND m.project_id = {alias}.project_id
                         AND r.issues_visibility = 'own'
                         AND NOT EXISTS (SELECT 1 FROM members m2
                                         JOIN member_roles mr2 ON mr2.member_id = m2.id
                                         JOIN roles r2 ON r2.id = mr2.role_id
                                         WHERE m2.user_id = :current_user_id AND m2.project_id = {alias}.project_id
                                         AND r2.issues_visibility IN ('all', 'default')))
                 AND ({alias}.author_id = :current_user_id OR {alias}.assigned_to_id = :current_user_id))
                OR
                -- Non-member on public project: non-private only
                (NOT EXISTS (SELECT 1 FROM members m
                             WHERE m.user_id = :current_user_id
                             AND m.project_id = {alias}.project_id)
                 AND EXISTS (SELECT 1 FROM projects p
                             WHERE p.id = {alias}.project_id AND p.is_public = true)
                 AND {alias}.is_private = false)
            )
        """

    def _wiki_visibility_clause(self, user: User, alias: str = "w") -> str:
        """Generate SQL fragment that enforces wiki page visibility.

        Args:
            user: The authenticated user.
            alias: Table alias for the wiki-owning entity with a project_id column.

        Returns:
            SQL AND clause (including the leading AND), or empty string for admins.
        """
        if user.is_admin:
            return ""

        return f"""
            AND (
                EXISTS (SELECT 1 FROM members m
                        WHERE m.user_id = :current_user_id
                        AND m.project_id = {alias}.project_id)
                OR EXISTS (SELECT 1 FROM projects p2 WHERE p2.id = {alias}.project_id AND p2.is_public = true)
            )
        """

    # Keep backward-compatible aliases
    def _visibility_sql(self, user: User) -> str:
        """Issue visibility clause with default alias 'i'."""
        return self._issue_visibility_clause(user, alias="i")

    def _wiki_visibility_sql(self, user: User) -> str:
        """Wiki visibility clause with default alias 'w'."""
        return self._wiki_visibility_clause(user, alias="w")

    def _comment_visibility_clause(self, user: User | None, journal_alias: str = "j", issue_alias: str = "ci") -> str:
        """Generate SQL AND clause for comment visibility via parent issue.

        Comments inherit visibility from their parent issue. Uses the same
        CTE-based approach as issue visibility.
        """
        if user is None or user.is_admin:
            return ""

        return f"""
            AND (
                -- Member with all/default visibility: see non-private + own private
                (EXISTS (SELECT 1 FROM user_visibility uv
                         WHERE uv.project_id = {issue_alias}.project_id AND uv.visibility_level >= 2)
                 AND ({issue_alias}.is_private = false
                      OR {issue_alias}.author_id = :current_user_id
                      OR {issue_alias}.assigned_to_id = :current_user_id))
                OR
                -- Member with own-only visibility: author/assignee only
                (EXISTS (SELECT 1 FROM user_visibility uv
                         WHERE uv.project_id = {issue_alias}.project_id AND uv.visibility_level = 1)
                 AND NOT EXISTS (SELECT 1 FROM user_visibility uv2
                                 WHERE uv2.project_id = {issue_alias}.project_id AND uv2.visibility_level >= 2)
                 AND ({issue_alias}.author_id = :current_user_id
                      OR {issue_alias}.assigned_to_id = :current_user_id))
                OR
                -- Non-member on public project: non-private only
                (NOT EXISTS (SELECT 1 FROM user_visibility uv
                             WHERE uv.project_id = {issue_alias}.project_id)
                 AND EXISTS (SELECT 1 FROM public_projects pp
                             WHERE pp.project_id = {issue_alias}.project_id)
                 AND {issue_alias}.is_private = false)
            )
        """

    # ------------------------------------------------------------------
    # Visibility CTE optimization
    # ------------------------------------------------------------------

    def _visibility_cte_sql(self, user: User | None) -> str:
        """Generate WITH clauses that pre-compute user visibility per project.

        Returns SQL CTE prefix (WITH ... AS ...) for non-admin users,
        or empty string for admins / anonymous.

        CTEs:
        - ``user_visibility``: projects where the user is a member, with max visibility level
        - ``public_projects``: projects where is_public = true
        """
        if user is None or user.is_admin:
            return ""

        return """
            WITH user_visibility AS (
                SELECT m.project_id,
                       MAX(CASE
                           WHEN r.issues_visibility IN ('all', 'default') THEN 2
                           WHEN r.issues_visibility = 'own' THEN 1
                           ELSE 0
                       END) AS visibility_level
                FROM members m
                JOIN member_roles mr ON mr.member_id = m.id
                JOIN roles r ON r.id = mr.role_id
                WHERE m.user_id = :current_user_id
                GROUP BY m.project_id
            ),
            public_projects AS (
                SELECT id AS project_id FROM projects WHERE is_public = true
            )
        """

    def _issue_visibility_cte_clause(self, user: User | None, alias: str = "i") -> str:
        """Generate SQL AND clause referencing pre-computed visibility CTEs.

        Must be used with ``_visibility_cte_sql()`` as a CTE prefix.
        """
        if user is None or user.is_admin:
            return ""

        return f"""
            AND (
                -- Member with all/default visibility: see non-private + own private
                (EXISTS (SELECT 1 FROM user_visibility uv
                         WHERE uv.project_id = {alias}.project_id AND uv.visibility_level >= 2)
                 AND ({alias}.is_private = false
                      OR {alias}.author_id = :current_user_id
                      OR {alias}.assigned_to_id = :current_user_id))
                OR
                -- Member with own-only visibility: author/assignee only
                (EXISTS (SELECT 1 FROM user_visibility uv
                         WHERE uv.project_id = {alias}.project_id AND uv.visibility_level = 1)
                 AND NOT EXISTS (SELECT 1 FROM user_visibility uv2
                                 WHERE uv2.project_id = {alias}.project_id AND uv2.visibility_level >= 2)
                 AND ({alias}.author_id = :current_user_id
                      OR {alias}.assigned_to_id = :current_user_id))
                OR
                -- Non-member on public project: non-private only
                (NOT EXISTS (SELECT 1 FROM user_visibility uv
                             WHERE uv.project_id = {alias}.project_id)
                 AND EXISTS (SELECT 1 FROM public_projects pp
                             WHERE pp.project_id = {alias}.project_id)
                 AND {alias}.is_private = false)
            )
        """

    def _wiki_visibility_cte_clause(self, user: User | None, alias: str = "w") -> str:
        """Generate SQL AND clause for wiki visibility referencing CTEs."""
        if user is None or user.is_admin:
            return ""

        return f"""
            AND (
                EXISTS (SELECT 1 FROM user_visibility uv
                        WHERE uv.project_id = {alias}.project_id)
                OR EXISTS (SELECT 1 FROM public_projects pp
                           WHERE pp.project_id = {alias}.project_id)
            )
        """

    # ------------------------------------------------------------------
    # Attachment visibility
    # ------------------------------------------------------------------

    def _attachment_visibility_cte_clause(self, user: User | None) -> str:
        """SQL AND clause for attachment access control via container joins.

        Expects the following table aliases in scope:
        - ``att``  — attachments
        - ``ai``   — LEFT JOIN issues (container_type='Issue')
        - ``awp``  — LEFT JOIN wiki_pages (container_type='WikiPage')
        - ``aw``   — LEFT JOIN wikis (via awp.wiki_id)

        Must be used with ``_visibility_cte_sql()`` as a CTE prefix.
        """
        if user is None:
            return "AND 1=0"
        if user.is_admin:
            return ""

        return """
            AND (
                -- Issue attachments: user has project access + issue visibility
                (att.container_type = 'Issue' AND ai.id IS NOT NULL AND (
                    (EXISTS (SELECT 1 FROM user_visibility uv
                             WHERE uv.project_id = ai.project_id AND uv.visibility_level >= 2)
                     AND (ai.is_private = false
                          OR ai.author_id = :current_user_id
                          OR ai.assigned_to_id = :current_user_id))
                    OR
                    (EXISTS (SELECT 1 FROM user_visibility uv
                             WHERE uv.project_id = ai.project_id AND uv.visibility_level = 1)
                     AND NOT EXISTS (SELECT 1 FROM user_visibility uv2
                                     WHERE uv2.project_id = ai.project_id AND uv2.visibility_level >= 2)
                     AND (ai.author_id = :current_user_id
                          OR ai.assigned_to_id = :current_user_id))
                    OR
                    (NOT EXISTS (SELECT 1 FROM user_visibility uv
                                 WHERE uv.project_id = ai.project_id)
                     AND EXISTS (SELECT 1 FROM public_projects pp
                                 WHERE pp.project_id = ai.project_id)
                     AND ai.is_private = false)
                ))
                OR
                -- WikiPage attachments: user has project access or project is public
                (att.container_type = 'WikiPage' AND awp.id IS NOT NULL AND (
                    EXISTS (SELECT 1 FROM user_visibility uv
                            WHERE uv.project_id = aw.project_id)
                    OR EXISTS (SELECT 1 FROM public_projects pp
                               WHERE pp.project_id = aw.project_id)
                ))
            )
        """

    @staticmethod
    def _att_project_filter(project_filter: str) -> str:
        """Adapt an issue/wiki project filter to attachment-resolved project.

        The input ``project_filter`` always uses ``i.project_id`` as the
        column reference (from the caller).  We replace it with
        ``COALESCE(ai.project_id, aw.project_id)`` for the attachment query.
        """
        att_col = "COALESCE(ai.project_id, aw.project_id)"
        # Only replace the exact issue alias — avoid cascading replacements
        # (e.g. replacing "w.project_id" inside "aw.project_id").
        return project_filter.replace("i.project_id", att_col)

    def _attachment_fts_sql(
        self,
        fts_lang: str,
        user: User | None,
        project_filter: str,
    ) -> str:
        """Build the FTS UNION ALL branch for attachment search."""
        att_vis = self._attachment_visibility_cte_clause(user)
        att_project_filter = self._att_project_filter(project_filter)

        return f"""
            SELECT * FROM (
                SELECT
                    '{_SRT_ATTACHMENT}' as result_type,
                    att.id,
                    CASE
                        WHEN att.container_type = 'Issue' THEN
                            ai_p.key || '-' || CAST(ai.sequence_number AS text)
                        WHEN att.container_type = 'WikiPage' THEN awp.title
                        ELSE CAST(att.container_id AS text)
                    END as title,
                    att.filename as subtitle,
                    ts_headline(CAST(:fts_lang AS regconfig), sc.content, query,
                        '{SEARCH_FTS_HEADLINE_OPTIONS}') as snippet,
                    ts_rank_cd(sc.search_vector, query) as score,
                    COALESCE(ai_p.key, aw_p.key) as project_key
                FROM search_chunks sc
                JOIN search_sources ss ON ss.id = sc.source_id AND ss.source_type = '{_SST_ATTACHMENT}'
                JOIN attachments att ON att.id = ss.entity_id
                LEFT JOIN issues ai ON att.container_type = 'Issue' AND ai.id = att.container_id
                LEFT JOIN projects ai_p ON ai_p.id = ai.project_id
                LEFT JOIN wiki_pages awp ON att.container_type = 'WikiPage' AND awp.id = att.container_id
                LEFT JOIN wikis aw ON aw.id = awp.wiki_id
                LEFT JOIN projects aw_p ON aw_p.id = aw.project_id
                CROSS JOIN plainto_tsquery(CAST(:fts_lang AS regconfig), :query) query
                WHERE sc.search_vector @@ query
                {att_vis}
                {att_project_filter}
                ORDER BY score DESC LIMIT :limit
            ) attachments_sub
        """

    def _attachment_fts_count_sql(
        self,
        fts_lang: str,
        user: User | None,
        project_filter: str,
    ) -> str:
        """Build the FTS COUNT query for attachment search."""
        att_vis = self._attachment_visibility_cte_clause(user)
        att_project_filter = self._att_project_filter(project_filter)

        return f"""
            SELECT COUNT(*) as cnt
            FROM search_chunks sc
            JOIN search_sources ss ON ss.id = sc.source_id AND ss.source_type = '{_SST_ATTACHMENT}'
            JOIN attachments att ON att.id = ss.entity_id
            LEFT JOIN issues ai ON att.container_type = 'Issue' AND ai.id = att.container_id
            LEFT JOIN wiki_pages awp ON att.container_type = 'WikiPage' AND awp.id = att.container_id
            LEFT JOIN wikis aw ON aw.id = awp.wiki_id
            CROSS JOIN plainto_tsquery(CAST(:fts_lang AS regconfig), :query) query
            WHERE sc.search_vector @@ query
            {att_vis}
            {att_project_filter}
        """

    def _attachment_semantic_vis(self, user: User | None) -> str:
        """Attachment visibility for semantic search (main query).

        Uses aliases from the main semantic query LEFT JOINs:
        att, att_iss, att_wp, att_w.
        """
        if user is None:
            return "AND 1=0"
        if user.is_admin:
            return ""

        return """
            AND (
                (att.container_type = 'Issue' AND att_iss.id IS NOT NULL AND (
                    (EXISTS (SELECT 1 FROM user_visibility uv
                             WHERE uv.project_id = att_iss.project_id AND uv.visibility_level >= 2)
                     AND (att_iss.is_private = false
                          OR att_iss.author_id = :current_user_id
                          OR att_iss.assigned_to_id = :current_user_id))
                    OR
                    (EXISTS (SELECT 1 FROM user_visibility uv
                             WHERE uv.project_id = att_iss.project_id AND uv.visibility_level = 1)
                     AND NOT EXISTS (SELECT 1 FROM user_visibility uv2
                                     WHERE uv2.project_id = att_iss.project_id AND uv2.visibility_level >= 2)
                     AND (att_iss.author_id = :current_user_id
                          OR att_iss.assigned_to_id = :current_user_id))
                    OR
                    (NOT EXISTS (SELECT 1 FROM user_visibility uv
                                 WHERE uv.project_id = att_iss.project_id)
                     AND EXISTS (SELECT 1 FROM public_projects pp
                                 WHERE pp.project_id = att_iss.project_id)
                     AND att_iss.is_private = false)
                ))
                OR
                (att.container_type = 'WikiPage' AND att_wp.id IS NOT NULL AND (
                    EXISTS (SELECT 1 FROM user_visibility uv
                            WHERE uv.project_id = att_w.project_id)
                    OR EXISTS (SELECT 1 FROM public_projects pp
                               WHERE pp.project_id = att_w.project_id)
                ))
            )
        """

    def _attachment_semantic_count_vis(self, user: User | None) -> str:
        """Attachment visibility for semantic count query.

        Uses aliases from the count query LEFT JOINs:
        att_c, att_c_iss, att_c_wp, att_c_w.
        """
        if user is None:
            return "AND 1=0"
        if user.is_admin:
            return ""

        return """
            AND (
                (att_c.container_type = 'Issue' AND att_c_iss.id IS NOT NULL AND (
                    (EXISTS (SELECT 1 FROM user_visibility uv
                             WHERE uv.project_id = att_c_iss.project_id AND uv.visibility_level >= 2)
                     AND (att_c_iss.is_private = false
                          OR att_c_iss.author_id = :current_user_id
                          OR att_c_iss.assigned_to_id = :current_user_id))
                    OR
                    (EXISTS (SELECT 1 FROM user_visibility uv
                             WHERE uv.project_id = att_c_iss.project_id AND uv.visibility_level = 1)
                     AND NOT EXISTS (SELECT 1 FROM user_visibility uv2
                                     WHERE uv2.project_id = att_c_iss.project_id AND uv2.visibility_level >= 2)
                     AND (att_c_iss.author_id = :current_user_id
                          OR att_c_iss.assigned_to_id = :current_user_id))
                    OR
                    (NOT EXISTS (SELECT 1 FROM user_visibility uv
                                 WHERE uv.project_id = att_c_iss.project_id)
                     AND EXISTS (SELECT 1 FROM public_projects pp
                                 WHERE pp.project_id = att_c_iss.project_id)
                     AND att_c_iss.is_private = false)
                ))
                OR
                (att_c.container_type = 'WikiPage' AND att_c_wp.id IS NOT NULL AND (
                    EXISTS (SELECT 1 FROM user_visibility uv
                            WHERE uv.project_id = att_c_w.project_id)
                    OR EXISTS (SELECT 1 FROM public_projects pp
                               WHERE pp.project_id = att_c_w.project_id)
                ))
            )
        """

    # ------------------------------------------------------------------
    # Metadata filter builder
    # ------------------------------------------------------------------

    def _build_issue_filters(self, filters: SearchFilters | None, params: dict[str, Any]) -> str:
        """Build SQL WHERE clauses for issue metadata filtering.

        Returns SQL fragment string and mutates params dict with filter values.
        """
        if filters is None:
            return ""

        clauses: list[str] = []

        if filters.tracker_id is not None:
            clauses.append("AND i.tracker_id = :filter_tracker_id")
            params["filter_tracker_id"] = filters.tracker_id

        if filters.status_id is not None:
            clauses.append("AND i.status_id = :filter_status_id")
            params["filter_status_id"] = filters.status_id

        if filters.priority_id is not None:
            clauses.append("AND i.priority_id = :filter_priority_id")
            params["filter_priority_id"] = filters.priority_id

        if filters.assigned_to_id is not None:
            clauses.append("AND i.assigned_to_id = :filter_assigned_to_id")
            params["filter_assigned_to_id"] = filters.assigned_to_id

        if filters.author_id is not None:
            clauses.append("AND i.author_id = :filter_author_id")
            params["filter_author_id"] = filters.author_id

        if filters.category_id is not None:
            clauses.append("AND i.category_id = :filter_category_id")
            params["filter_category_id"] = filters.category_id

        if filters.fixed_version_id is not None:
            clauses.append("AND i.fixed_version_id = :filter_fixed_version_id")
            params["filter_fixed_version_id"] = filters.fixed_version_id

        if filters.created_after is not None:
            clauses.append("AND i.created_at >= :filter_created_after")
            params["filter_created_after"] = filters.created_after

        if filters.created_before is not None:
            clauses.append("AND i.created_at < :filter_created_before")
            params["filter_created_before"] = filters.created_before

        if filters.updated_after is not None:
            clauses.append("AND i.updated_at >= :filter_updated_after")
            params["filter_updated_after"] = filters.updated_after

        if filters.updated_before is not None:
            clauses.append("AND i.updated_at < :filter_updated_before")
            params["filter_updated_before"] = filters.updated_before

        if filters.metadata is not None:
            clauses.append("AND i.issue_metadata @> CAST(:filter_metadata AS jsonb)")
            params["filter_metadata"] = json.dumps(filters.metadata)

        return "\n                ".join(clauses)

    # ------------------------------------------------------------------
    # Full-text search
    # ------------------------------------------------------------------

    async def search(
        self,
        session: AsyncSession,
        query: str,
        user: User | None = None,
        project_id: int | None = None,
        project_ids: list[int] | None = None,
        scope: str = "all",
        offset: int = 0,
        limit: int = 25,
        filters: SearchFilters | None = None,
        skip_count: bool = False,
    ) -> tuple[list[SearchResult], int, dict[str, int]]:
        """Execute a full-text search and return (results, total_count, type_counts).

        Args:
            session: Async database session.
            query: User search query (processed via plainto_tsquery).
            user: Authenticated user for visibility filtering.
            project_id: Optional single project ID to scope the search.
            project_ids: Optional list of project IDs for multi-project.
            scope: "all", "issues", or "wiki".
            offset: Pagination offset.
            limit: Pagination limit.
            filters: Optional metadata filters for issues.
            skip_count: If True, skip COUNT queries and return total=0 / empty counts.

        Returns:
            Tuple of (results, total count for current scope, per-type counts dict).
            The type_counts dict has keys: issues, wiki, comments, attachments, all.
        """
        settings = get_settings()
        fts_lang = settings.search_fts_language
        parts: list[str] = []
        # Per-type count SQL fragments, keyed by type name.
        # Always populated for all 4 types (regardless of scope) so that
        # a single query can return per-type totals for filter tabs.
        count_parts: dict[str, str] = {}
        # Normalize hyphens to spaces so plainto_tsquery doesn't generate
        # compound tokens (e.g. 'jwt-rotat') that fail to match individual
        # tsvector lexemes.
        normalized_query = query.replace("-", " ")
        params: dict[str, Any] = {"query": normalized_query, "fts_lang": fts_lang}

        # Add user ID for visibility checks
        if user is not None:
            params["current_user_id"] = user.id

        # Visibility SQL fragments: CTE-optimized visibility for all modes.
        # Comments and attachments always reference the user_visibility /
        # public_projects CTEs, so the CTE prefix is required whenever the
        # user is non-admin (even for single-project searches).
        cte_prefix = self._visibility_cte_sql(user)
        issue_visibility = self._issue_visibility_cte_clause(user, alias="i")
        wiki_visibility = self._wiki_visibility_cte_clause(user, alias="w")
        comment_visibility = self._comment_visibility_clause(user, journal_alias="j", issue_alias="ci")

        # Project filtering
        issue_project_filter = ""
        wiki_project_filter = ""
        comment_project_filter = ""
        if project_ids is not None:
            # Multi-project: use IN clause
            issue_project_filter = "AND i.project_id = ANY(:project_ids)"
            wiki_project_filter = "AND w.project_id = ANY(:project_ids)"
            comment_project_filter = "AND ci.project_id = ANY(:project_ids)"
            params["project_ids"] = project_ids
        elif project_id is not None:
            issue_project_filter = "AND i.project_id = :project_id"
            wiki_project_filter = "AND w.project_id = :project_id"
            comment_project_filter = "AND ci.project_id = :project_id"
            params["project_id"] = project_id

        # Issue metadata filters
        issue_filter_sql = self._build_issue_filters(filters, params)

        # --- Result parts: scope-gated (only fetch rows for the active scope) ---

        if scope in ("all", "issues"):
            issue_sql = f"""
                SELECT * FROM (
                    SELECT
                        '{_SRT_ISSUE}' as result_type,
                        i.id,
                        i.project_key || '-' || i.sequence_number as title,
                        i.subject as subtitle,
                        ts_headline(CAST(:fts_lang AS regconfig), coalesce(i.description, ''), query,
                            '{SEARCH_FTS_HEADLINE_OPTIONS}') as snippet,
                        ts_rank_cd(i.search_vector, query) as score,
                        i.project_key
                    FROM issues i, plainto_tsquery(CAST(:fts_lang AS regconfig), :query) query
                    WHERE i.search_vector @@ query
                    {issue_project_filter}
                    {issue_visibility}
                    {issue_filter_sql}
                    ORDER BY score DESC LIMIT :limit
                ) issues_sub
            """
            parts.append(issue_sql)

        if scope in ("all", "wiki"):
            wiki_sql = f"""
                SELECT * FROM (
                    SELECT
                        '{_SRT_WIKI}' as result_type,
                        wp.id,
                        wp.title,
                        wp.slug as subtitle,
                        ts_headline(CAST(:fts_lang AS regconfig), wc.text, query,
                            '{SEARCH_FTS_HEADLINE_OPTIONS}') as snippet,
                        ts_rank_cd(wc.search_vector, query) as score,
                        p.key as project_key
                    FROM wiki_contents wc
                    JOIN wiki_pages wp ON wp.id = wc.page_id
                    JOIN wikis w ON w.id = wp.wiki_id
                    JOIN projects p ON p.id = w.project_id
                    CROSS JOIN plainto_tsquery(CAST(:fts_lang AS regconfig), :query) query
                    JOIN (
                        SELECT page_id, MAX(version) as max_ver
                        FROM wiki_contents
                        GROUP BY page_id
                    ) wc_latest ON wc_latest.page_id = wc.page_id AND wc_latest.max_ver = wc.version
                    WHERE wc.search_vector @@ query
                    {wiki_project_filter}
                    {wiki_visibility}
                    ORDER BY score DESC LIMIT :limit
                ) wiki_sub
            """
            parts.append(wiki_sql)

        # Comment keyword search: search search_chunks content for source_type='comment'
        # Comments inherit visibility from their parent issue via journals table.
        if scope in ("all", "comments"):
            comment_sql = f"""
                SELECT * FROM (
                    SELECT
                        '{_SRT_COMMENT}' as result_type,
                        j.id,
                        ci.project_key || '-' || ci.sequence_number as title,
                        left(sc.content, {SEARCH_SNIPPET_MAX_CHARS}) as subtitle,
                        ts_headline(CAST(:fts_lang AS regconfig), sc.content, query,
                            '{SEARCH_FTS_HEADLINE_OPTIONS}') as snippet,
                        ts_rank_cd(sc.search_vector, query) as score,
                        ci.project_key
                    FROM search_chunks sc
                    JOIN search_sources ss ON ss.id = sc.source_id
                    JOIN journals j ON j.id = ss.entity_id AND ss.source_type = '{_SST_JOURNAL}'
                    JOIN issues ci ON ci.id = j.issue_id
                    CROSS JOIN plainto_tsquery(CAST(:fts_lang AS regconfig), :query) query
                    WHERE sc.search_vector @@ query
                    AND ss.source_type = '{_SST_JOURNAL}'
                    {comment_project_filter}
                    {comment_visibility}
                    ORDER BY score DESC LIMIT :limit
                ) comments_sub
            """
            parts.append(comment_sql)

        # Attachment keyword search: search search_chunks for source_type='attachment'
        att_pf = issue_project_filter  # same params, different alias handled in helper
        if scope in ("all", "attachments"):
            parts.append(self._attachment_fts_sql(fts_lang, user, att_pf))

        # --- Count parts: ALWAYS build for all 4 types (single-query per-type counts) ---

        count_parts["issues"] = f"""
            SELECT COUNT(*) as cnt
            FROM issues i, plainto_tsquery(CAST(:fts_lang AS regconfig), :query) query
            WHERE i.search_vector @@ query
            {issue_project_filter}
            {issue_visibility}
            {issue_filter_sql}
        """

        count_parts["wiki"] = f"""
            SELECT COUNT(*) as cnt
            FROM wiki_contents wc
            JOIN wiki_pages wp ON wp.id = wc.page_id
            JOIN wikis w ON w.id = wp.wiki_id
            CROSS JOIN plainto_tsquery(CAST(:fts_lang AS regconfig), :query) query
            JOIN (
                SELECT page_id, MAX(version) as max_ver
                FROM wiki_contents
                GROUP BY page_id
            ) wc_latest ON wc_latest.page_id = wc.page_id AND wc_latest.max_ver = wc.version
            WHERE wc.search_vector @@ query
            {wiki_project_filter}
            {wiki_visibility}
        """

        count_parts["comments"] = f"""
            SELECT COUNT(*) as cnt
            FROM search_chunks sc
            JOIN search_sources ss ON ss.id = sc.source_id
            JOIN journals j ON j.id = ss.entity_id AND ss.source_type = '{_SST_JOURNAL}'
            JOIN issues ci ON ci.id = j.issue_id
            CROSS JOIN plainto_tsquery(CAST(:fts_lang AS regconfig), :query) query
            WHERE sc.search_vector @@ query
            AND ss.source_type = '{_SST_JOURNAL}'
            {comment_project_filter}
            {comment_visibility}
        """

        count_parts["attachments"] = self._attachment_fts_count_sql(fts_lang, user, att_pf)

        if not parts:
            return [], 0, {}

        # Combined query with UNION ALL, ordered by score desc
        union_sql = " UNION ALL ".join(parts)
        full_sql = f"""
            {cte_prefix}
            SELECT * FROM ({union_sql}) combined
            ORDER BY score DESC
            LIMIT :limit OFFSET :offset
        """
        params["limit"] = limit
        params["offset"] = offset

        result = await session.execute(text(full_sql), params)

        results = [
            SearchResult(
                result_type=row._mapping["result_type"],
                id=row._mapping["id"],
                title=row._mapping["title"],
                subtitle=row._mapping["subtitle"],
                snippet=_clean_snippet(row._mapping["snippet"]),
                score=float(row._mapping["score"]),
                project_key=row._mapping["project_key"],
            )
            for row in result
        ]

        # Per-type counts in a single query (skipped when called from hybrid_search)
        type_counts: dict[str, int] = {}
        total = 0
        if not skip_count:
            # Build a single SELECT with scalar subqueries for each type.
            # PostgreSQL can parallelize these subqueries internally.
            subquery_parts = [f"({count_sql}) as {type_name}_count" for type_name, count_sql in count_parts.items()]
            combined_count_sql = f"""
                {cte_prefix}
                SELECT {", ".join(subquery_parts)}
            """
            count_result = await session.execute(text(combined_count_sql), params)
            row = count_result.one()
            for type_name in count_parts:
                type_counts[type_name] = row._mapping[f"{type_name}_count"]
            type_counts["all"] = sum(type_counts.values())
            # Total for the current scope: sum of active scope types only
            if scope == "all":
                total = type_counts["all"]
            else:
                total = type_counts.get(scope, 0)

        return results, total, type_counts

    async def semantic_search(
        self,
        session: AsyncSession,
        query: str,
        user: User | None = None,
        project_id: int | None = None,
        project_ids: list[int] | None = None,
        offset: int = 0,
        limit: int = 25,
        skip_count: bool = False,
    ) -> tuple[list[SearchResult], int]:
        """Semantic-only search using pgvector cosine similarity.

        Generates an embedding for the query text, then finds the most
        similar chunks via cosine distance. Returns results mapped back
        to their source entities.
        """
        from specivo.services.embedding_service import EmbeddingService

        emb_service = EmbeddingService()
        model = await emb_service.get_default_model(session)
        if model is None:
            return [], 0

        query_vector = await emb_service.generate_embedding(query, model, intent="query")
        if query_vector is None:
            return [], 0
        # Format vector as PostgreSQL literal: '[0.1,0.2,...]'
        vec_literal = "[" + ",".join(str(v) for v in query_vector) + "]"

        params: dict[str, Any] = {"model_id": model.id, "query_vector": vec_literal}
        project_filter = ""
        if project_ids is not None:
            project_filter = "AND ss.project_id = ANY(:project_ids)"
            params["project_ids"] = project_ids
        elif project_id is not None:
            project_filter = "AND ss.project_id = :project_id"
            params["project_id"] = project_id

        # Add user ID for visibility checks
        if user is not None:
            params["current_user_id"] = user.id

        # Visibility filters for semantic search (CTE-optimized)
        cte_prefix = self._visibility_cte_sql(user)
        issue_vis = self._issue_visibility_cte_clause(user, alias="i2")
        wiki_vis = self._wiki_visibility_cte_clause(user, alias="w2")
        comment_vis = self._comment_visibility_clause(user, journal_alias="cj", issue_alias="ci2")

        # Semantic search query: find chunks closest to the query vector.
        # The vector literal is interpolated directly (not a bind param) because
        # asyncpg does not support pgvector's vector type as a bind parameter,
        # and the value is fully derived from our embedding model (not user input).
        #
        # Uses LEFT JOINs for title/subtitle/project_key resolution.
        #
        # Deny-by-default: only explicitly allowed source types pass visibility
        # (issue, wiki_page, comment). Unknown source types are excluded.
        #
        # SQL-level dedup via DISTINCT ON (source_type, entity_id) with
        # ORDER BY cosine distance, so only the best chunk per entity is returned.
        #
        # Comment support: joins to journals + issues for metadata and
        # inherits parent issue visibility.
        # Two-phase semantic search:
        # CTE 1 (nearest): fast HNSW index-only scan for top-N closest chunks
        # CTE 2: join to entity tables, apply visibility, dedup, limit
        prefetch_limit = SEARCH_HYBRID_PREFETCH_LIMIT * 2  # over-fetch for visibility filtering

        att_sem_vis = self._attachment_semantic_vis(user)

        # Build CTE prefix: if user has visibility CTEs, comma-separate with nearest
        if cte_prefix:
            # cte_prefix starts with "WITH user_visibility AS (...), public_projects AS (...)"
            # We append nearest as another CTE
            nearest_cte = f"""
            {cte_prefix},
            nearest AS (
                SELECT ce.chunk_id,
                       (1 - (ce.embedding <=> CAST(:query_vector AS vector))) as score
                FROM chunk_embeddings ce
                WHERE ce.model_id = :model_id
                ORDER BY ce.embedding <=> CAST(:query_vector AS vector)
                LIMIT :prefetch_limit
            )
            """
        else:
            nearest_cte = """
            WITH nearest AS (
                SELECT ce.chunk_id,
                       (1 - (ce.embedding <=> CAST(:query_vector AS vector))) as score
                FROM chunk_embeddings ce
                WHERE ce.model_id = :model_id
                ORDER BY ce.embedding <=> CAST(:query_vector AS vector)
                LIMIT :prefetch_limit
            )
            """

        sem_sql = f"""
            {nearest_cte}
            SELECT result_type, id, title, subtitle, snippet, score, project_key
            FROM (
                SELECT DISTINCT ON (ss.source_type, ss.entity_id)
                    CASE ss.source_type
                        WHEN '{_SST_WIKI_PAGE}' THEN '{_SRT_WIKI}'
                        WHEN '{_SST_JOURNAL}' THEN '{_SRT_COMMENT}'
                        ELSE ss.source_type
                    END as result_type,
                    ss.entity_id as id,
                    CASE
                        WHEN ss.source_type = '{_SST_ISSUE}' THEN
                            iss.project_key || '-' || iss.sequence_number
                        WHEN ss.source_type = '{_SST_WIKI_PAGE}' THEN
                            wp.title
                        WHEN ss.source_type = '{_SST_JOURNAL}' THEN
                            cmt_iss.project_key || '-' || cmt_iss.sequence_number
                        WHEN ss.source_type = '{_SST_ATTACHMENT}' THEN
                            CASE
                                WHEN att.container_type = 'Issue' THEN
                                    att_iss.project_key || '-' || CAST(att_iss.sequence_number AS text)
                                WHEN att.container_type = 'WikiPage' THEN att_wp.title
                                ELSE CAST(att.container_id AS text)
                            END
                        ELSE CAST(ss.entity_id AS text)
                    END as title,
                    CASE
                        WHEN ss.source_type = '{_SST_ISSUE}' THEN
                            iss.subject
                        WHEN ss.source_type = '{_SST_WIKI_PAGE}' THEN
                            wp.slug
                        WHEN ss.source_type = '{_SST_JOURNAL}' THEN
                            left(sc.content, {SEARCH_SNIPPET_MAX_CHARS})
                        WHEN ss.source_type = '{_SST_ATTACHMENT}' THEN
                            att.filename
                        ELSE NULL
                    END as subtitle,
                    left(sc.content, {SEARCH_SNIPPET_MAX_CHARS}) as snippet,
                    n.score,
                    CASE
                        WHEN ss.source_type = '{_SST_ISSUE}' THEN
                            iss.project_key
                        WHEN ss.source_type = '{_SST_WIKI_PAGE}' THEN
                            wp_proj.key
                        WHEN ss.source_type = '{_SST_JOURNAL}' THEN
                            cmt_iss.project_key
                        WHEN ss.source_type = '{_SST_ATTACHMENT}' THEN
                            COALESCE(att_iss_p.key, att_wp_p.key)
                        ELSE ''
                    END as project_key
                FROM nearest n
                JOIN search_chunks sc ON sc.id = n.chunk_id
                JOIN search_sources ss ON ss.id = sc.source_id
                LEFT JOIN issues iss ON ss.source_type = '{_SST_ISSUE}' AND iss.id = ss.entity_id
                LEFT JOIN wiki_pages wp ON ss.source_type = '{_SST_WIKI_PAGE}' AND wp.id = ss.entity_id
                LEFT JOIN wikis wp_w ON wp_w.id = wp.wiki_id
                LEFT JOIN projects wp_proj ON wp_proj.id = wp_w.project_id
                LEFT JOIN journals cmt_j ON ss.source_type = '{_SST_JOURNAL}' AND cmt_j.id = ss.entity_id
                LEFT JOIN issues cmt_iss ON cmt_iss.id = cmt_j.issue_id
                LEFT JOIN attachments att ON ss.source_type = '{_SST_ATTACHMENT}' AND att.id = ss.entity_id
                LEFT JOIN issues att_iss ON att.container_type = 'Issue' AND att_iss.id = att.container_id
                LEFT JOIN projects att_iss_p ON att_iss_p.id = att_iss.project_id
                LEFT JOIN wiki_pages att_wp ON att.container_type = 'WikiPage' AND att_wp.id = att.container_id
                LEFT JOIN wikis att_w ON att_w.id = att_wp.wiki_id
                LEFT JOIN projects att_wp_p ON att_wp_p.id = att_w.project_id
                WHERE 1=1
                {project_filter}
                AND (
                    (ss.source_type = '{_SST_ISSUE}' AND EXISTS (
                        SELECT 1 FROM issues i2 WHERE i2.id = ss.entity_id
                        {issue_vis}
                    ))
                    OR
                    (ss.source_type = '{_SST_WIKI_PAGE}' AND EXISTS (
                        SELECT 1 FROM wiki_pages wp2
                        JOIN wikis w2 ON w2.id = wp2.wiki_id
                        WHERE wp2.id = ss.entity_id
                        {wiki_vis}
                    ))
                    OR
                    (ss.source_type = '{_SST_JOURNAL}' AND EXISTS (
                        SELECT 1 FROM journals cj
                        JOIN issues ci2 ON ci2.id = cj.issue_id
                        WHERE cj.id = ss.entity_id
                        {comment_vis}
                    ))
                    OR
                    (ss.source_type = '{_SST_ATTACHMENT}' AND att.id IS NOT NULL
                     {att_sem_vis})
                )
                ORDER BY ss.source_type, ss.entity_id, n.score DESC
            ) deduped
            ORDER BY score DESC
            LIMIT :limit OFFSET :offset
        """
        params["limit"] = limit
        params["offset"] = offset
        params["prefetch_limit"] = prefetch_limit

        result = await session.execute(text(sem_sql), params)

        results: list[SearchResult] = []
        for row in result:
            m = row._mapping
            results.append(
                SearchResult(
                    result_type=m["result_type"],
                    id=m["id"],
                    title=m["title"] or "",
                    subtitle=m["subtitle"],
                    snippet=m["snippet"],
                    score=float(m["score"]) if m["score"] is not None else 0.0,
                    project_key=m["project_key"] or "",
                )
            )

        # Count total semantic results (skipped when called from hybrid_search)
        total = 0
        if not skip_count:
            count_sql = f"""
                {cte_prefix}
                SELECT COUNT(DISTINCT (ss.source_type, ss.entity_id))
                FROM chunk_embeddings ce
                JOIN search_chunks sc ON sc.id = ce.chunk_id
                JOIN search_sources ss ON ss.id = sc.source_id
                LEFT JOIN attachments att_c ON ss.source_type = '{_SST_ATTACHMENT}' AND att_c.id = ss.entity_id
                LEFT JOIN issues att_c_iss ON att_c.container_type = 'Issue' AND att_c_iss.id = att_c.container_id
                LEFT JOIN wiki_pages att_c_wp ON att_c.container_type = 'WikiPage' AND att_c_wp.id = att_c.container_id
                LEFT JOIN wikis att_c_w ON att_c_w.id = att_c_wp.wiki_id
                WHERE ce.model_id = :model_id
                {project_filter}
                AND (
                    (ss.source_type = '{_SST_ISSUE}' AND EXISTS (
                        SELECT 1 FROM issues i2 WHERE i2.id = ss.entity_id
                        {issue_vis}
                    ))
                    OR
                    (ss.source_type = '{_SST_WIKI_PAGE}' AND EXISTS (
                        SELECT 1 FROM wiki_pages wp2
                        JOIN wikis w2 ON w2.id = wp2.wiki_id
                        WHERE wp2.id = ss.entity_id
                        {wiki_vis}
                    ))
                    OR
                    (ss.source_type = '{_SST_JOURNAL}' AND EXISTS (
                        SELECT 1 FROM journals cj
                        JOIN issues ci2 ON ci2.id = cj.issue_id
                        WHERE cj.id = ss.entity_id
                        {comment_vis}
                    ))
                    OR
                    (ss.source_type = '{_SST_ATTACHMENT}' AND att_c.id IS NOT NULL
                     {self._attachment_semantic_count_vis(user)})
                )
            """
            count_result = await session.execute(text(count_sql), params)
            total = count_result.scalar_one()

        return results, total

    async def hybrid_search(
        self,
        session: AsyncSession,
        query: str,
        user: User | None = None,
        project_id: int | None = None,
        project_ids: list[int] | None = None,
        scope: str = "all",
        offset: int = 0,
        limit: int = 25,
        filters: SearchFilters | None = None,
    ) -> tuple[list[SearchResult], int, dict[str, int]]:
        """Hybrid search: semantic (pgvector) + keyword (tsvector), RRF fusion.

        1. Run FTS query (existing search logic) -> ranked list with IDs
        2. Run semantic query (pgvector cosine similarity) -> ranked list with IDs
        3. Merge using RRF (k=60): score = 1/(k+rank_fts) + 1/(k+rank_semantic)
        4. Sort by combined score, apply offset/limit

        Returns:
            Tuple of (results, total, type_counts). The type_counts are derived
            from the FTS count query (accurate per-type totals from the DB).
        """
        # Get both result sets (unpaginated for RRF merging).
        # skip_count=True for both — hybrid computes its own counts from merged results.
        fts_results, _fts_total, _ = await self.search(
            session,
            query,
            user=user,
            project_id=project_id,
            project_ids=project_ids,
            scope="all",
            offset=0,
            limit=SEARCH_HYBRID_PREFETCH_LIMIT,
            filters=filters,
            skip_count=True,
        )
        sem_results, sem_total = await self.semantic_search(
            session,
            query,
            user=user,
            project_id=project_id,
            project_ids=project_ids,
            offset=0,
            limit=SEARCH_HYBRID_PREFETCH_LIMIT,
            skip_count=True,
        )

        if not fts_results and not sem_results:
            return [], 0, _empty_type_counts(include_all=True)

        # Build ID lists and rank maps for RRF (using composite key: "type:id")
        fts_keys = [f"{r.result_type}:{r.id}" for r in fts_results]
        sem_keys = [f"{r.result_type}:{r.id}" for r in sem_results]
        fts_rank_map = {f"{r.result_type}:{r.id}": i + 1 for i, r in enumerate(fts_results)}
        sem_rank_map = {f"{r.result_type}:{r.id}": i + 1 for i, r in enumerate(sem_results)}

        # Build lookup maps
        result_map: dict[str, SearchResult] = {}
        for r in fts_results:
            key = f"{r.result_type}:{r.id}"
            result_map[key] = r
        for r in sem_results:
            key = f"{r.result_type}:{r.id}"
            if key not in result_map:
                result_map[key] = r

        # RRF fusion on string keys
        scores: dict[str, float] = {}
        k = RRF_K
        for rank_0, key in enumerate(fts_keys):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank_0 + 1)
        for rank_0, key in enumerate(sem_keys):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank_0 + 1)

        sorted_keys = sorted(scores, key=lambda x: scores[x], reverse=True)

        # Compute per-type counts from the merged RRF results (not FTS alone)
        _type_to_count_key = {
            SearchResultType.ISSUE: "issues",
            SearchResultType.WIKI: "wiki",
            SearchResultType.COMMENT: "comments",
            SearchResultType.ATTACHMENT: "attachments",
        }
        type_counts: dict[str, int] = _empty_type_counts()
        for key in sorted_keys:
            display = SOURCE_TYPE_TO_DISPLAY.get(result_map[key].result_type)
            if display:
                count_key = _type_to_count_key.get(display)
                if count_key:
                    type_counts[count_key] += 1

        # Apply scope filter to sorted_keys if not "all"
        if scope != "all":
            scope_types: dict[str, set[str]] = {
                "issues": {SearchResultType.ISSUE},
                "wiki": {SearchResultType.WIKI},
                "comments": {SearchResultType.COMMENT},
                "attachments": {SearchResultType.ATTACHMENT},
            }
            allowed = scope_types.get(scope, set())
            sorted_keys = [k for k in sorted_keys if SOURCE_TYPE_TO_DISPLAY.get(result_map[k].result_type) in allowed]

        total = len(sorted_keys)
        type_counts["all"] = sum(type_counts.values())

        # Apply pagination
        page_keys = sorted_keys[offset : offset + limit]

        results = []
        for key in page_keys:
            r = result_map[key]
            results.append(
                SearchResult(
                    result_type=r.result_type,
                    id=r.id,
                    title=r.title,
                    subtitle=r.subtitle,
                    snippet=r.snippet,
                    score=scores[key],
                    project_key=r.project_key,
                    fts_rank=fts_rank_map.get(key),
                    sem_rank=sem_rank_map.get(key),
                )
            )

        return results, total, type_counts
