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
import math
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.constants import (
    MAX_PAGE_LIMIT,
    MAX_SEARCH_OFFSET,
    PGVECTOR_ITERATIVE_SCAN_VERSION,
    RRF_K,
    SEARCH_FTS_HEADLINE_OPTIONS,
    SEARCH_HYBRID_PREFETCH_LIMIT,
    SEARCH_SNIPPET_MAX_CHARS,
    SEMANTIC_CANDIDATE_GROWTH,
    SEMANTIC_INDEX_DIMENSIONS,
    SEMANTIC_MAX_INDEX_CANDIDATES,
)
from specivo.models.project import PROJECT_STATUS_ACTIVE
from specivo.models.role import RoleBuiltin
from specivo.models.search import EmbeddingModel
from specivo.models.user import User
from specivo.schemas.search import (
    SOURCE_TYPE_TO_DISPLAY,
    SearchFilters,
    SearchResult,
    SearchResultType,
    SearchSourceType,
)
from specivo.services.api_key_scope import scope_project_ids
from specivo.services.permission_service import Permission
from specivo.services.wiki_utils import WIKI_LINK_RE

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

# Whether the database's pgvector supports iterative index scans, read once per
# process by ``_prepare_index_scan`` (None until then).
_pgvector_iterative_scan: bool | None = None


def _parse_extversion(version: str) -> tuple[int, ...]:
    """Turn an extension version such as ``0.8.2`` into ``(0, 8, 2)``; unknown parts become 0."""
    return tuple(int(part) if part.isdigit() else 0 for part in version.split("."))


# ---------------------------------------------------------------------------
# Shared visibility SQL fragments
# ---------------------------------------------------------------------------
#
# The visibility clauses below are near-identical to one another, and every
# copy is a chance for them to drift — which is how the wiki clauses came to
# check less than the issue ones. The two rules that every clause needs are
# defined here once and composed into each.


def _principal_match(alias: str = "m") -> str:
    """Return SQL matching the ``members`` rows that reach the current user.

    A ``members`` row is held by exactly one principal: a user or a user
    group. It reaches the user when it is their own row, or when it belongs
    to a group they are in. This mirrors
    ``permission_service.member_principal_clause()``, which is the ORM-side
    definition of the same rule.
    """
    return (
        f"({alias}.user_id = :current_user_id"
        f" OR {alias}.group_id IN (SELECT ugm.group_id FROM user_group_members ugm"
        f" WHERE ugm.user_id = :current_user_id))"
    )


def _role_grants(permission: str, alias: str = "r") -> str:
    """Return SQL testing whether a role grants *permission* (or the wildcard).

    ``roles.permissions`` is a JSONB array of permission strings, with
    ``["*"]`` meaning all. ``jsonb_exists()`` is used rather than the ``?``
    operator: a bare ``?`` in a raw SQL string sits badly next to parameter
    handling and reads worse.

    *permission* is always a ``Permission`` member, never user input, so
    interpolating it is safe.
    """
    return f"(jsonb_exists({alias}.permissions, '{permission}') OR jsonb_exists({alias}.permissions, '*'))"


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


# ---------------------------------------------------------------------------
# Identifier tokens (KEY-42)
# ---------------------------------------------------------------------------
#
# PostgreSQL's text-search parser reads ``ACME-42`` as the word ``acme`` and
# the signed integer ``-42``, in every regconfig. So the index triggers also
# store each identifier-shaped token as one lowercase lexeme (``'acme-42'``,
# via ``specivo_fts_identifiers()`` from migration 0034), and the query side
# pulls identifier tokens out of the query and matches them against that
# lexeme. The pattern must stay identical to the one in that migration.

IDENTIFIER_PATTERN = r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]{0,63})-([0-9]{1,18})(?![A-Za-z0-9_])"
_IDENTIFIER_RE = re.compile(IDENTIFIER_PATTERN)

# Bounds the per-query lookup; further tokens are searched as plain text.
MAX_QUERY_IDENTIFIERS = 10
# Longest search query (characters) parsed; the rest is ignored. Every term of
# the free text must match, so a longer query finds nothing a shorter one
# would not, and parsing stays cheap whatever the caller sends.
MAX_QUERY_CHARS = 1000

_PG_INT_MAX = 2**31 - 1

# Most rows one keyword-search part ranks: the deepest offset plus one page.
_MAX_PART_ROWS = MAX_SEARCH_OFFSET + MAX_PAGE_LIMIT


def extract_identifiers(query: str) -> list[str]:
    """Return the distinct identifier tokens in *query*, uppercased, in order of appearance."""
    seen: list[str] = []
    for match in _IDENTIFIER_RE.finditer(query):
        ident = f"{match.group(1)}-{match.group(2)}".upper()
        if ident not in seen:
            seen.append(ident)
            if len(seen) == MAX_QUERY_IDENTIFIERS:
                break
    return seen


@dataclass(frozen=True)
class _ParsedQuery:
    """A search query split into identifier tokens and the remaining free text.

    ``text`` has hyphens normalised to spaces, so ``plainto_tsquery`` does not
    build compound tokens (``'jwt-rotat'``) that miss the individual lexemes
    the index holds. Identifiers are taken out first, because for them the
    hyphen is what the index lexeme is built around.
    """

    text: str
    identifiers: list[str]

    @classmethod
    def parse(cls, query: str) -> _ParsedQuery:
        query = query[:MAX_QUERY_CHARS]
        identifiers = extract_identifiers(query)
        wanted = set(identifiers)

        def _strip(match: re.Match[str]) -> str:
            ident = f"{match.group(1)}-{match.group(2)}".upper()
            return " " if ident in wanted else match.group(0)

        remainder = _IDENTIFIER_RE.sub(_strip, query)
        return cls(text=remainder.replace("-", " "), identifiers=identifiers)

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())

    def tsquery_from_sql(self, cfg: str) -> str:
        """Return a ``LATERAL`` FROM item exposing the tsquery as column ``query``.

        *cfg* is the regconfig expression for the row. Free text goes through
        ``plainto_tsquery`` (bound as ``:query``), after ``specivo_fts_text``
        so the parser reads it in linear time, as it does indexed text;
        identifiers are ANDed in as
        exact lexemes (bound as ``:identifier_query``). Each operand is
        emitted only when present, since an empty tsquery operand makes
        PostgreSQL log a notice per row.
        """
        if not self.identifiers:
            return f"plainto_tsquery({cfg}, specivo_fts_text(:query)) AS query"
        identifier_query = "CAST(:identifier_query AS tsquery)"
        if not self.has_text:
            return f"(SELECT {identifier_query} AS query) AS query_src"
        return f"(SELECT plainto_tsquery({cfg}, specivo_fts_text(:query)) && {identifier_query} AS query) AS query_src"

    def bind_params(self) -> dict[str, Any]:
        # Identifiers match [A-Za-z0-9_-] only, so quoting them as tsquery
        # lexemes needs no escaping.
        return {
            "query": self.text,
            "identifier_query": " & ".join(f"'{ident.lower()}'" for ident in self.identifiers),
        }


# Models whose query embedding already failed in this process, so the warning is logged once.
_failed_embedding_models: set[int] = set()


def _warn_embedding_failure(model: EmbeddingModel, exc: BaseException) -> None:
    """Log once per model that embedding a query failed; semantic results are skipped.

    Only the error's class is logged (see ``describe_error``), never its message.
    """
    from specivo.services.search_index_service import describe_error

    if model.id in _failed_embedding_models:
        logger.debug("Query embedding failed again for model %s", model.name, exc_info=True)
        return
    _failed_embedding_models.add(model.id)
    logger.warning(
        "Embedding a search query with model %s failed (%s); semantic search is off and search "
        "falls back to keyword matching",
        model.name,
        describe_error(exc),
    )
    logger.debug("Query embedding failure for model %s", model.name, exc_info=True)


def _page_window(pinned: int, offset: int, limit: int) -> tuple[slice, int, int]:
    """Split a page between *pinned* leading results and the ranked results after them.

    Returns the slice of the pinned list on this page, and the offset and
    limit to apply to the ranked results (which exclude the pinned ones).
    """
    pinned_slice = slice(min(offset, pinned), min(offset + limit, pinned))
    on_page = pinned_slice.stop - pinned_slice.start
    return pinned_slice, max(0, offset - pinned), limit - on_page


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
    clean = WIKI_LINK_RE.sub(
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

    SECURITY: The FTS language (regconfig) is resolved per-project inside
    the database via ``specivo_fts_config(<project_id>)``, which takes an
    integer argument and returns a validated ``regconfig``. The language is
    therefore never interpolated into SQL strings nor passed as a string
    bind parameter, so the query side is immune to language-based SQL
    injection by construction.
    """

    # ------------------------------------------------------------------
    # Visibility SQL
    # ------------------------------------------------------------------
    #
    # One definition, mirroring ``permission_service.get_user_roles``. The
    # ``user_visibility`` CTE holds one row per project, with:
    #
    # - ``visibility_level``: 2 for "all"/"default", 1 for "own", 0 for none,
    #   from the best role that grants ``view_issues``;
    # - ``can_view_wiki``: whether any role grants ``view_wiki``.
    #
    # Its rows come from two disjoint sources joined with UNION ALL:
    #
    # - projects the user holds a membership on, directly or through a group:
    #   the membership's roles and nothing else, even when they grant nothing;
    # - public projects without a membership: the Non member role (never for
    #   the anonymous user), plus the project's anonymous permissions while
    #   the instance switch is on and the project is active.
    #
    # Whether the viewer is anonymous and the switch state are bound
    # parameters (``_visibility_params``). Every clause below reads only the
    # CTE, so no rule is written twice. Admins get no CTE and no clauses.

    async def _visibility_params(self, session: AsyncSession, user: User) -> dict[str, Any]:
        """Return the bind parameters the visibility CTE and clauses expect.

        ``current_user_id`` is NULL for the anonymous user: it holds no
        membership, and must never match an issue's author or assignee.
        """
        if user.is_admin:
            return {"current_user_id": user.id}

        from specivo.services.anonymous_access_service import is_anonymous_access_enabled

        return {
            "current_user_id": None if user.is_anonymous else user.id,
            "viewer_is_anonymous": user.is_anonymous,
            "anonymous_access_enabled": await is_anonymous_access_enabled(session),
        }

    def _visibility_cte_sql(self, user: User) -> str:
        """Return the ``WITH user_visibility AS (...)`` prefix, or an empty string for admins.

        Needs the parameters from :meth:`_visibility_params`.
        """
        if user.is_admin:
            return ""

        grants_issues = _role_grants(Permission.VIEW_ISSUES, "r")
        grants_wiki = _role_grants(Permission.VIEW_WIKI, "r")
        non_member_grants_issues = _role_grants(Permission.VIEW_ISSUES, "nm")
        non_member_grants_wiki = _role_grants(Permission.VIEW_WIKI, "nm")
        signed_in = "NOT CAST(:viewer_is_anonymous AS boolean)"
        anonymous_applies = f"(CAST(:anonymous_access_enabled AS boolean) AND p.status = {PROJECT_STATUS_ACTIVE})"
        non_member = f"FROM roles nm WHERE nm.builtin = {int(RoleBuiltin.NON_MEMBER)}"

        return f"""
            WITH user_visibility AS (
                -- Projects the user holds a membership on: its roles, and only those.
                SELECT m.project_id,
                       COALESCE(MAX(CASE
                           WHEN r.id IS NULL THEN 0
                           WHEN {grants_issues} AND r.issues_visibility IN ('all', 'default') THEN 2
                           WHEN {grants_issues} AND r.issues_visibility = 'own' THEN 1
                           ELSE 0
                       END), 0) AS visibility_level,
                       COALESCE(bool_or(r.id IS NOT NULL AND {grants_wiki}), false) AS can_view_wiki
                FROM members m
                LEFT JOIN member_roles mr ON mr.member_id = m.id
                LEFT JOIN roles r ON r.id = mr.role_id
                WHERE {_principal_match("m")}
                GROUP BY m.project_id
                UNION ALL
                -- Public projects without a membership: the Non member role for a
                -- signed-in user, plus the project's anonymous permissions.
                SELECT p.id AS project_id,
                       GREATEST(
                           CASE WHEN {signed_in} THEN COALESCE((
                               SELECT MAX(CASE
                                   WHEN {non_member_grants_issues} AND nm.issues_visibility IN ('all', 'default') THEN 2
                                   WHEN {non_member_grants_issues} AND nm.issues_visibility = 'own' THEN 1
                                   ELSE 0
                               END)
                               {non_member}
                           ), 0) ELSE 0 END,
                           CASE WHEN {anonymous_applies}
                                     AND jsonb_exists(p.anonymous_permissions, '{Permission.VIEW_ISSUES.value}')
                                THEN 2 ELSE 0 END
                       ) AS visibility_level,
                       ({signed_in} AND EXISTS (SELECT 1 {non_member} AND {non_member_grants_wiki}))
                       OR ({anonymous_applies}
                           AND jsonb_exists(p.anonymous_permissions, '{Permission.VIEW_WIKI.value}')) AS can_view_wiki
                FROM projects p
                WHERE p.is_public = true
                  AND NOT EXISTS (SELECT 1 FROM members m
                                  WHERE {_principal_match("m")} AND m.project_id = p.id)
            )
        """

    @staticmethod
    def _issue_visible_sql(alias: str) -> str:
        """Return a predicate, without a leading AND, admitting issue row *alias* through ``user_visibility``.

        Level 2 admits non-private issues and private ones the user authored
        or is assigned to; level 1 admits only the latter; level 0 or no row
        admits nothing.
        """
        own = f"({alias}.author_id = :current_user_id OR {alias}.assigned_to_id = :current_user_id)"
        return f"""EXISTS (SELECT 1 FROM user_visibility uv
                    WHERE uv.project_id = {alias}.project_id
                    AND ((uv.visibility_level >= 2 AND ({alias}.is_private = false OR {own}))
                         OR (uv.visibility_level = 1 AND {own})))"""

    @staticmethod
    def _wiki_visible_sql(project_id_column: str) -> str:
        """Return a predicate, without a leading AND, admitting the wiki of the project in *project_id_column*."""
        return (
            f"EXISTS (SELECT 1 FROM user_visibility uv WHERE uv.project_id = {project_id_column} AND uv.can_view_wiki)"
        )

    def _issue_visibility_cte_clause(self, user: User, alias: str = "i") -> str:
        """SQL AND clause for issue visibility. Needs the ``_visibility_cte_sql()`` prefix."""
        if user.is_admin:
            return ""
        return f"AND {self._issue_visible_sql(alias)}"

    def _wiki_visibility_cte_clause(self, user: User, alias: str = "w") -> str:
        """SQL AND clause for wiki visibility. Needs the ``_visibility_cte_sql()`` prefix."""
        if user.is_admin:
            return ""
        return f"AND {self._wiki_visible_sql(f'{alias}.project_id')}"

    def _comment_visibility_clause(self, user: User, journal_alias: str = "j", issue_alias: str = "ci") -> str:
        """SQL AND clause for comment visibility. Needs the ``_visibility_cte_sql()`` prefix.

        A comment is visible when its issue is, and private notes only to
        admins — the same rule the issue page and the issues API apply.
        """
        if user.is_admin:
            return ""
        return f"AND {journal_alias}.is_private = false AND {self._issue_visible_sql(issue_alias)}"

    # ------------------------------------------------------------------
    # Attachment visibility
    # ------------------------------------------------------------------

    def _attachment_visibility_sql(self, user: User, attachment: str, issue: str, wiki_page: str, wiki: str) -> str:
        """SQL AND clause for attachments, judged by their container. Needs the CTE prefix.

        *attachment* aliases ``attachments``; *issue*, *wiki_page* and *wiki*
        alias the LEFT JOINed container rows. The anonymous user never sees
        attachments. An attachment on a wiki page in the trash is hidden from
        everyone, administrators included, like the page itself.
        """
        if user.is_admin:
            return f"AND ({attachment}.container_type <> 'WikiPage' OR {wiki_page}.deleted_at IS NULL)"
        if user.is_anonymous:
            return "AND 1=0"

        return f"""
            AND (
                ({attachment}.container_type = 'Issue' AND {issue}.id IS NOT NULL
                 AND {self._issue_visible_sql(issue)})
                OR
                ({attachment}.container_type = 'WikiPage' AND {wiki_page}.id IS NOT NULL
                 AND {wiki_page}.deleted_at IS NULL
                 AND {self._wiki_visible_sql(f"{wiki}.project_id")})
            )
        """

    def _attachment_visibility_cte_clause(self, user: User) -> str:
        """Attachment visibility for keyword search (aliases ``att``, ``ai``, ``awp``, ``aw``)."""
        return self._attachment_visibility_sql(user, "att", "ai", "awp", "aw")

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
        user: User,
        project_filter: str,
        parsed: _ParsedQuery,
    ) -> str:
        """Build the FTS UNION ALL branch for attachment search.

        The FTS analyzer language is resolved per-row from the attachment's
        owning project: ``specivo_fts_config(COALESCE(ai.project_id,
        aw.project_id))`` — issue container via the ``ai`` join, wiki-page
        container via the ``aw`` join.
        """
        att_vis = self._attachment_visibility_cte_clause(user)
        att_project_filter = self._att_project_filter(project_filter)
        tsquery = parsed.tsquery_from_sql("specivo_fts_config(COALESCE(ai.project_id, aw.project_id))")

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
                    ts_headline(specivo_fts_config(COALESCE(ai.project_id, aw.project_id)),
                        specivo_fts_text(sc.content), query,
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
                CROSS JOIN LATERAL {tsquery}
                WHERE sc.search_vector @@ query
                {att_vis}
                {att_project_filter}
                ORDER BY score DESC LIMIT :part_limit
            ) attachments_sub
        """

    def _attachment_fts_count_sql(
        self,
        user: User,
        project_filter: str,
        parsed: _ParsedQuery,
    ) -> str:
        """Build the FTS COUNT query for attachment search.

        Uses the same per-row language resolution as
        :meth:`_attachment_fts_sql` so counts match the result rows.
        """
        att_vis = self._attachment_visibility_cte_clause(user)
        att_project_filter = self._att_project_filter(project_filter)
        tsquery = parsed.tsquery_from_sql("specivo_fts_config(COALESCE(ai.project_id, aw.project_id))")

        return f"""
            SELECT COUNT(*) as cnt
            FROM search_chunks sc
            JOIN search_sources ss ON ss.id = sc.source_id AND ss.source_type = '{_SST_ATTACHMENT}'
            JOIN attachments att ON att.id = ss.entity_id
            LEFT JOIN issues ai ON att.container_type = 'Issue' AND ai.id = att.container_id
            LEFT JOIN wiki_pages awp ON att.container_type = 'WikiPage' AND awp.id = att.container_id
            LEFT JOIN wikis aw ON aw.id = awp.wiki_id
            CROSS JOIN LATERAL {tsquery}
            WHERE sc.search_vector @@ query
            {att_vis}
            {att_project_filter}
        """

    def _attachment_semantic_vis(self, user: User) -> str:
        """Attachment visibility for semantic search (aliases ``att``, ``att_iss``, ``att_wp``, ``att_w``)."""
        return self._attachment_visibility_sql(user, "att", "att_iss", "att_wp", "att_w")

    def _attachment_semantic_count_vis(self, user: User) -> str:
        """Attachment visibility for the semantic count query (aliases ``att_c``, ``att_c_iss``, ...)."""
        return self._attachment_visibility_sql(user, "att_c", "att_c_iss", "att_c_wp", "att_c_w")

    # ------------------------------------------------------------------
    # Metadata filter builder
    # ------------------------------------------------------------------

    def _tag_exists_clauses(
        self,
        filters: SearchFilters | None,
        params: dict[str, Any],
        link_col: str,
        alias: str,
    ) -> str:
        """Build AND-logic tag filter clauses for an issue or wiki page.

        Emits one correlated ``EXISTS`` per selected tag name so the entity must
        carry *every* named tag (AND semantics). Matching is by lowercased name,
        not id, so the filter spans projects (the same name maps to different tag
        ids per project). ``link_col`` is ``issue_id`` or ``wiki_page_id`` and
        ``alias`` is the entity table alias whose ``id`` the link references.

        The param keys (``filter_tag_0`` …) are stable across calls with
        different aliases, so calling this for both the issue and wiki sides of
        one query reuses identical bind values without conflict.
        """
        if filters is None or not filters.tag_names:
            return ""
        out: list[str] = []
        for idx, name in enumerate(filters.tag_names):
            pname = f"filter_tag_{idx}"
            out.append(
                f"AND EXISTS (SELECT 1 FROM tag_links tl JOIN tags t ON t.id = tl.tag_id "
                f"WHERE tl.{link_col} = {alias}.id AND lower(t.name) = lower(:{pname}))"
            )
            params[pname] = name
        return "\n                ".join(out)

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

        tag_sql = self._tag_exists_clauses(filters, params, "issue_id", "i")
        if tag_sql:
            clauses.append(tag_sql)

        return "\n                ".join(clauses)

    # ------------------------------------------------------------------
    # Exact issue references
    # ------------------------------------------------------------------

    async def _identifier_hits(
        self,
        session: AsyncSession,
        identifiers: list[str],
        user: User,
        project_id: int | None = None,
        project_ids: list[int] | None = None,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]:
        """Resolve identifier tokens to the issues they name, in query order.

        A ``KEY-N`` token names an issue by its current reference, by a
        reference it had before moving to another project
        (``issue_ref_aliases``), or under a renamed project's retired key
        (``project_key_aliases``). The hits go through the same visibility,
        project and metadata filters as full-text results, so a reference
        the user cannot see resolves to nothing.
        """
        # Narrow to the authenticating API key's project limit, if any.
        project_id, project_ids = scope_project_ids(project_id, project_ids)
        refs = [(key, int(num)) for key, _, num in (ident.rpartition("-") for ident in identifiers)]
        refs = [(key, num) for key, num in refs if num <= _PG_INT_MAX]
        if not refs:
            return []

        params: dict[str, Any] = {
            "ref_keys": [key for key, _ in refs],
            "ref_seqs": [num for _, num in refs],
        }
        params.update(await self._visibility_params(session, user))

        project_filter = ""
        if project_ids is not None:
            project_filter = "AND i.project_id = ANY(:project_ids)"
            params["project_ids"] = project_ids
        elif project_id is not None:
            project_filter = "AND i.project_id = :project_id"
            params["project_id"] = project_id
        issue_filter_sql = self._build_issue_filters(filters, params)

        cte_prefix = self._visibility_cte_sql(user)
        ref_ctes = """
            refs AS (
                SELECT t.key, t.seq, t.ord
                FROM unnest(CAST(:ref_keys AS text[]), CAST(:ref_seqs AS integer[]))
                     WITH ORDINALITY AS t(key, seq, ord)
            ),
            resolved AS (
                SELECT ri.id AS issue_id, refs.ord
                FROM refs JOIN issues ri ON ri.project_key = refs.key AND ri.sequence_number = refs.seq
                UNION ALL
                SELECT a.issue_id, refs.ord
                FROM refs JOIN issue_ref_aliases a
                    ON a.old_project_key = refs.key AND a.old_sequence_number = refs.seq
                UNION ALL
                SELECT ri.id, refs.ord
                FROM refs
                JOIN project_key_aliases pka ON pka.old_key = refs.key
                JOIN issues ri ON ri.project_id = pka.project_id AND ri.sequence_number = refs.seq
            )
        """
        with_clause = f"{cte_prefix}, {ref_ctes}" if cte_prefix else f"WITH {ref_ctes}"

        sql = f"""
            {with_clause}
            SELECT i.id,
                   i.project_key || '-' || i.sequence_number AS title,
                   i.subject AS subtitle,
                   left(coalesce(i.description, ''), {SEARCH_SNIPPET_MAX_CHARS}) AS snippet,
                   i.project_key,
                   MIN(r.ord) AS ord
            FROM resolved r
            JOIN issues i ON i.id = r.issue_id
            WHERE 1=1
            {project_filter}
            {self._issue_visibility_cte_clause(user, alias="i")}
            {issue_filter_sql}
            GROUP BY i.id
            ORDER BY ord
        """
        result = await session.execute(text(sql), params)
        return [
            SearchResult(
                result_type=_SRT_ISSUE,
                id=row.id,
                title=row.title,
                subtitle=row.subtitle,
                snippet=_clean_snippet(row.snippet) or None,
                score=1.0,
                project_key=row.project_key,
            )
            for row in result
        ]

    @staticmethod
    def _rank_pinned_first(pinned: list[SearchResult], ranked: list[SearchResult]) -> list[SearchResult]:
        """Put *pinned* ahead of *ranked*, raising their score to keep scores non-increasing."""
        top = max([1.0, *(r.score for r in ranked)])
        return [r.model_copy(update={"score": top}) for r in pinned] + ranked

    # ------------------------------------------------------------------
    # Full-text search
    # ------------------------------------------------------------------

    async def search(
        self,
        session: AsyncSession,
        query: str,
        user: User,
        project_id: int | None = None,
        project_ids: list[int] | None = None,
        scope: str = "all",
        offset: int = 0,
        limit: int = 25,
        filters: SearchFilters | None = None,
        skip_count: bool = False,
        *,
        resolve_identifiers: bool = True,
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
            resolve_identifiers: If True, issues named by an identifier token in
                the query (``ACME-42``) are resolved directly and ranked first.
                ``hybrid_search`` passes False and ranks them itself.

        Returns:
            Tuple of (results, total count for current scope, per-type counts dict).
            The type_counts dict has keys: issues, wiki, comments, attachments, all.
        """
        # Narrow to the authenticating API key's project limit, if any.
        project_id, project_ids = scope_project_ids(project_id, project_ids)
        parts: list[str] = []
        # Per-type count SQL fragments, keyed by type name.
        # Always populated for all 4 types (regardless of scope) so that
        # a single query can return per-type totals for filter tabs.
        count_parts: dict[str, str] = {}
        parsed = _ParsedQuery.parse(query)
        params: dict[str, Any] = parsed.bind_params()

        params.update(await self._visibility_params(session, user))

        # Visibility SQL fragments: CTE-optimized visibility for all modes.
        # Comments and attachments always reference the user_visibility CTE,
        # so the CTE prefix is required whenever the user is non-admin (even
        # for single-project searches).
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

        tsq_i = parsed.tsquery_from_sql("specivo_fts_config(i.project_id)")
        tsq_w = parsed.tsquery_from_sql("specivo_fts_config(w.project_id)")
        tsq_ci = parsed.tsquery_from_sql("specivo_fts_config(ci.project_id)")

        # Issues the query names by reference lead the results, and are kept
        # out of the ranked issue rows and their count so they appear once.
        pinned: list[SearchResult] = []
        if resolve_identifiers and parsed.identifiers and scope in ("all", "issues"):
            pinned = await self._identifier_hits(
                session, parsed.identifiers, user, project_id=project_id, project_ids=project_ids, filters=filters
            )
        pinned_exclusion = ""
        if pinned:
            pinned_exclusion = "AND i.id <> ALL(:pinned_issue_ids)"
            params["pinned_issue_ids"] = [r.id for r in pinned]
        # Real-tag AND filter for wiki pages (issue side is folded into
        # issue_filter_sql above). When a tag filter is active, only taggable
        # entities (issues, wiki pages) can match — comments and attachments are
        # excluded entirely.
        wiki_tag_filter = self._tag_exists_clauses(filters, params, "wiki_page_id", "wp")
        tags_active = bool(filters and filters.tag_names)

        # --- Result parts: scope-gated (only fetch rows for the active scope) ---

        if scope in ("all", "issues"):
            issue_sql = f"""
                SELECT * FROM (
                    SELECT
                        '{_SRT_ISSUE}' as result_type,
                        i.id,
                        i.project_key || '-' || i.sequence_number as title,
                        i.subject as subtitle,
                        ts_headline(specivo_fts_config(i.project_id),
                            specivo_fts_text(i.description), query,
                            '{SEARCH_FTS_HEADLINE_OPTIONS}') as snippet,
                        ts_rank_cd(i.search_vector, query) as score,
                        i.project_key
                    FROM issues i
                    CROSS JOIN LATERAL {tsq_i}
                    WHERE i.search_vector @@ query
                    {issue_project_filter}
                    {issue_visibility}
                    {issue_filter_sql}
                    {pinned_exclusion}
                    ORDER BY score DESC LIMIT :part_limit
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
                        ts_headline(specivo_fts_config(w.project_id), specivo_fts_text(wc.text), query,
                            '{SEARCH_FTS_HEADLINE_OPTIONS}') as snippet,
                        ts_rank_cd(wc.search_vector, query) as score,
                        p.key as project_key
                    FROM wiki_contents wc
                    JOIN wiki_pages wp ON wp.id = wc.page_id
                    JOIN wikis w ON w.id = wp.wiki_id
                    JOIN projects p ON p.id = w.project_id
                    CROSS JOIN LATERAL {tsq_w}
                    JOIN (
                        SELECT page_id, MAX(version) as max_ver
                        FROM wiki_contents
                        GROUP BY page_id
                    ) wc_latest ON wc_latest.page_id = wc.page_id AND wc_latest.max_ver = wc.version
                    WHERE wc.search_vector @@ query
                    AND wp.deleted_at IS NULL
                    {wiki_project_filter}
                    {wiki_visibility}
                    {wiki_tag_filter}
                    ORDER BY score DESC LIMIT :part_limit
                ) wiki_sub
            """
            parts.append(wiki_sql)

        # Comment keyword search: search search_chunks content for source_type='comment'
        # Comments inherit visibility from their parent issue via journals table.
        # Skipped when a tag filter is active (comments are not taggable).
        if scope in ("all", "comments") and not tags_active:
            comment_sql = f"""
                SELECT * FROM (
                    SELECT
                        '{_SRT_COMMENT}' as result_type,
                        j.id,
                        ci.project_key || '-' || ci.sequence_number as title,
                        left(sc.content, {SEARCH_SNIPPET_MAX_CHARS}) as subtitle,
                        ts_headline(specivo_fts_config(ci.project_id), specivo_fts_text(sc.content), query,
                            '{SEARCH_FTS_HEADLINE_OPTIONS}') as snippet,
                        ts_rank_cd(sc.search_vector, query) as score,
                        ci.project_key
                    FROM search_chunks sc
                    JOIN search_sources ss ON ss.id = sc.source_id
                    JOIN journals j ON j.id = ss.entity_id AND ss.source_type = '{_SST_JOURNAL}'
                    JOIN issues ci ON ci.id = j.issue_id
                    CROSS JOIN LATERAL {tsq_ci}
                    WHERE sc.search_vector @@ query
                    AND ss.source_type = '{_SST_JOURNAL}'
                    {comment_project_filter}
                    {comment_visibility}
                    ORDER BY score DESC LIMIT :part_limit
                ) comments_sub
            """
            parts.append(comment_sql)

        # Attachment keyword search: search search_chunks for source_type='attachment'
        # Skipped when a tag filter is active (attachments are not taggable).
        att_pf = issue_project_filter  # same params, different alias handled in helper
        if scope in ("all", "attachments") and not tags_active:
            parts.append(self._attachment_fts_sql(user, att_pf, parsed))

        # --- Count parts: ALWAYS build for all 4 types (single-query per-type counts) ---

        count_parts["issues"] = f"""
            SELECT COUNT(*) as cnt
            FROM issues i
            CROSS JOIN LATERAL {tsq_i}
            WHERE i.search_vector @@ query
            {issue_project_filter}
            {issue_visibility}
            {issue_filter_sql}
            {pinned_exclusion}
        """

        count_parts["wiki"] = f"""
            SELECT COUNT(*) as cnt
            FROM wiki_contents wc
            JOIN wiki_pages wp ON wp.id = wc.page_id
            JOIN wikis w ON w.id = wp.wiki_id
            CROSS JOIN LATERAL {tsq_w}
            JOIN (
                SELECT page_id, MAX(version) as max_ver
                FROM wiki_contents
                GROUP BY page_id
            ) wc_latest ON wc_latest.page_id = wc.page_id AND wc_latest.max_ver = wc.version
            WHERE wc.search_vector @@ query
            AND wp.deleted_at IS NULL
            {wiki_project_filter}
            {wiki_visibility}
            {wiki_tag_filter}
        """

        if tags_active:
            # Comments/attachments cannot carry tags — report zero for their tabs.
            count_parts["comments"] = "SELECT 0 as cnt"
            count_parts["attachments"] = "SELECT 0 as cnt"
        else:
            count_parts["comments"] = f"""
                SELECT COUNT(*) as cnt
                FROM search_chunks sc
                JOIN search_sources ss ON ss.id = sc.source_id
                JOIN journals j ON j.id = ss.entity_id AND ss.source_type = '{_SST_JOURNAL}'
                JOIN issues ci ON ci.id = j.issue_id
                CROSS JOIN LATERAL {tsq_ci}
                WHERE sc.search_vector @@ query
                AND ss.source_type = '{_SST_JOURNAL}'
                {comment_project_filter}
                {comment_visibility}
            """

            count_parts["attachments"] = self._attachment_fts_count_sql(user, att_pf, parsed)

        if not parts:
            return [], 0, {}

        # Combined query with UNION ALL, ordered by score desc. The pinned
        # issues take the first places, so the ranked rows start after them.
        pinned_slice, ranked_offset, ranked_limit = _page_window(len(pinned), offset, limit)
        union_sql = " UNION ALL ".join(parts)
        full_sql = f"""
            {cte_prefix}
            SELECT * FROM ({union_sql}) combined
            ORDER BY score DESC
            LIMIT :limit OFFSET :offset
        """
        params["limit"] = ranked_limit
        params["offset"] = ranked_offset
        # Each part must supply every row up to the end of the page, not just
        # one page's worth, or later pages come back short. Capped whatever the
        # caller passes, so no request makes a part rank more than the deepest
        # page search serves; a page past the cap comes back empty.
        params["part_limit"] = min(ranked_offset + ranked_limit, _MAX_PART_ROWS)

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
        if pinned:
            results = self._rank_pinned_first(pinned[pinned_slice], results)

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
            type_counts["issues"] += len(pinned)
            type_counts["all"] = sum(type_counts.values())
            # Total for the current scope: sum of active scope types only
            if scope == "all":
                total = type_counts["all"]
            else:
                total = type_counts.get(scope, 0)

        return results, total, type_counts

    async def filter_issues(
        self,
        session: AsyncSession,
        user: User,
        project_id: int | None = None,
        project_ids: list[int] | None = None,
        offset: int = 0,
        limit: int = 25,
        filters: SearchFilters | None = None,
    ) -> tuple[list[SearchResult], int, dict[str, int]]:
        """List issues by metadata/attribute filters with no full-text term.

        Used for metadata-only discovery (e.g. clicking a metadata tag on an
        issue). Results are ordered by recency. Visibility is enforced with the
        EXACT same clauses as :meth:`search`, so a user can never see issues in
        projects they cannot access — the metadata predicate only narrows the
        already-visible set.

        Returns the same ``(results, total, type_counts)`` shape as
        :meth:`search` (only the ``issues`` scope is meaningful here).
        """
        # Narrow to the authenticating API key's project limit, if any.
        project_id, project_ids = scope_project_ids(project_id, project_ids)
        params: dict[str, Any] = await self._visibility_params(session, user)

        cte_prefix = self._visibility_cte_sql(user)
        issue_visibility = self._issue_visibility_cte_clause(user, alias="i")
        issue_filter_sql = self._build_issue_filters(filters, params)

        issue_project_filter = ""
        if project_ids is not None:
            issue_project_filter = "AND i.project_id = ANY(:project_ids)"
            params["project_ids"] = project_ids
        elif project_id is not None:
            issue_project_filter = "AND i.project_id = :project_id"
            params["project_id"] = project_id

        where_sql = f"""
            WHERE 1=1
            {issue_project_filter}
            {issue_visibility}
            {issue_filter_sql}
        """

        count_sql = f"""
            {cte_prefix}
            SELECT COUNT(*) as cnt
            FROM issues i
            {where_sql}
        """
        count_result = await session.execute(text(count_sql), params)
        total = int(count_result.scalar_one())

        type_counts = {
            "issues": total,
            "wiki": 0,
            "comments": 0,
            "attachments": 0,
            "all": total,
        }

        if total == 0:
            return [], 0, type_counts

        params["limit"] = limit
        params["offset"] = offset
        list_sql = f"""
            {cte_prefix}
            SELECT
                '{_SRT_ISSUE}' as result_type,
                i.id,
                i.project_key || '-' || i.sequence_number as title,
                i.subject as subtitle,
                NULL as snippet,
                0.0 as score,
                i.project_key
            FROM issues i
            {where_sql}
            ORDER BY i.updated_at DESC, i.id DESC
            LIMIT :limit OFFSET :offset
        """
        result = await session.execute(text(list_sql), params)
        results = [
            SearchResult(
                result_type=row._mapping["result_type"],
                id=row._mapping["id"],
                title=row._mapping["title"],
                subtitle=row._mapping["subtitle"],
                snippet=None,
                score=float(row._mapping["score"]),
                project_key=row._mapping["project_key"],
            )
            for row in result
        ]
        return results, total, type_counts

    async def filter_tagged(
        self,
        session: AsyncSession,
        user: User,
        project_id: int | None = None,
        project_ids: list[int] | None = None,
        scope: str = "all",
        offset: int = 0,
        limit: int = 25,
        filters: SearchFilters | None = None,
    ) -> tuple[list[SearchResult], int, dict[str, int]]:
        """List tagged issues and/or wiki pages with no full-text term.

        The filter-only counterpart to :meth:`search` for the real-tag filter.
        Covers issues and wiki pages (the only taggable entities); comments and
        attachments always count zero. Results are ordered by recency. Visibility
        uses the same CTE clauses as :meth:`search`, so the tag predicate only
        narrows the already-visible set.
        """
        # Narrow to the authenticating API key's project limit, if any.
        project_id, project_ids = scope_project_ids(project_id, project_ids)
        params: dict[str, Any] = await self._visibility_params(session, user)

        cte_prefix = self._visibility_cte_sql(user)
        issue_visibility = self._issue_visibility_cte_clause(user, alias="i")
        wiki_visibility = self._wiki_visibility_cte_clause(user, alias="w")
        # Issue side folds in tag clauses (+ any metadata filters); wiki side
        # gets the tag clauses directly.
        issue_filter_sql = self._build_issue_filters(filters, params)
        wiki_tag_filter = self._tag_exists_clauses(filters, params, "wiki_page_id", "wp")

        issue_project_filter = ""
        wiki_project_filter = ""
        if project_ids is not None:
            issue_project_filter = "AND i.project_id = ANY(:project_ids)"
            wiki_project_filter = "AND w.project_id = ANY(:project_ids)"
            params["project_ids"] = project_ids
        elif project_id is not None:
            issue_project_filter = "AND i.project_id = :project_id"
            wiki_project_filter = "AND w.project_id = :project_id"
            params["project_id"] = project_id

        issue_where = f"""
            WHERE 1=1
            {issue_project_filter}
            {issue_visibility}
            {issue_filter_sql}
        """
        wiki_where = f"""
            WHERE wp.deleted_at IS NULL
            {wiki_project_filter}
            {wiki_visibility}
            {wiki_tag_filter}
        """

        # Per-type counts (always computed for both taggable types so the filter
        # tabs are accurate regardless of the active scope).
        issue_count_sql = f"{cte_prefix}\nSELECT COUNT(*) as cnt\nFROM issues i\n{issue_where}"
        wiki_count_sql = (
            f"{cte_prefix}\nSELECT COUNT(*) as cnt\nFROM wiki_pages wp\nJOIN wikis w ON w.id = wp.wiki_id\n{wiki_where}"
        )
        issue_total = int((await session.execute(text(issue_count_sql), params)).scalar_one())
        wiki_total = int((await session.execute(text(wiki_count_sql), params)).scalar_one())

        type_counts = {
            "issues": issue_total,
            "wiki": wiki_total,
            "comments": 0,
            "attachments": 0,
            "all": issue_total + wiki_total,
        }
        total = type_counts["all"] if scope == "all" else type_counts.get(scope, 0)

        if total == 0:
            return [], total, type_counts

        parts: list[str] = []
        if scope in ("all", "issues"):
            parts.append(f"""
                SELECT
                    '{_SRT_ISSUE}' as result_type,
                    i.id,
                    i.project_key || '-' || i.sequence_number as title,
                    i.subject as subtitle,
                    NULL as snippet,
                    0.0 as score,
                    i.project_key,
                    i.updated_at as sort_ts
                FROM issues i
                {issue_where}
            """)
        if scope in ("all", "wiki"):
            parts.append(f"""
                SELECT
                    '{_SRT_WIKI}' as result_type,
                    wp.id,
                    wp.title as title,
                    wp.slug as subtitle,
                    NULL as snippet,
                    0.0 as score,
                    p.key as project_key,
                    wp.updated_at as sort_ts
                FROM wiki_pages wp
                JOIN wikis w ON w.id = wp.wiki_id
                JOIN projects p ON p.id = w.project_id
                {wiki_where}
            """)

        if not parts:
            return [], total, type_counts

        params["limit"] = limit
        params["offset"] = offset
        union_sql = " UNION ALL ".join(parts)
        list_sql = f"""
            {cte_prefix}
            SELECT result_type, id, title, subtitle, snippet, score, project_key
            FROM ({union_sql}) combined
            ORDER BY sort_ts DESC, id DESC
            LIMIT :limit OFFSET :offset
        """
        result = await session.execute(text(list_sql), params)
        results = [
            SearchResult(
                result_type=row._mapping["result_type"],
                id=row._mapping["id"],
                title=row._mapping["title"],
                subtitle=row._mapping["subtitle"],
                snippet=None,
                score=float(row._mapping["score"]),
                project_key=row._mapping["project_key"],
            )
            for row in result
        ]
        return results, total, type_counts

    async def _prepare_index_scan(self, session: AsyncSession) -> bool:
        """Set up this transaction for HNSW index scans; False when they cannot be used.

        Needs pgvector 0.8 or later: its iterative scans let one index scan
        return more rows than ``hnsw.ef_search``, which is what the candidate
        rounds in :meth:`semantic_search` rely on. The settings are
        transaction-local (``set_config(..., true)``).
        """
        global _pgvector_iterative_scan
        if _pgvector_iterative_scan is None:
            version = await session.scalar(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
            _pgvector_iterative_scan = (
                version is not None and _parse_extversion(version) >= PGVECTOR_ITERATIVE_SCAN_VERSION
            )
            if not _pgvector_iterative_scan:
                logger.warning(
                    "pgvector %s does not support iterative index scans (0.8 or later needed); "
                    "semantic search ranks every embedding without the vector index",
                    version,
                )
        if not _pgvector_iterative_scan:
            return False
        await session.execute(
            text(
                "SELECT set_config('hnsw.ef_search', :ef_search, true), "
                "set_config('hnsw.iterative_scan', 'relaxed_order', true)"
            ),
            {"ef_search": str(get_settings().search_hnsw_ef_search)},
        )
        return True

    async def semantic_search(
        self,
        session: AsyncSession,
        query: str,
        user: User,
        project_id: int | None = None,
        project_ids: list[int] | None = None,
        offset: int = 0,
        limit: int = 25,
        skip_count: bool = False,
        filters: SearchFilters | None = None,
        *,
        resolve_identifiers: bool = True,
    ) -> tuple[list[SearchResult], int]:
        """Semantic-only search using pgvector cosine similarity.

        Generates an embedding for the query text, then finds the most
        similar chunks via cosine distance. Returns results mapped back
        to their source entities.

        When ``filters.tag_names`` is set, only tagged issues and wiki pages can
        match (comments and attachments are excluded), keeping hybrid results
        consistent with the keyword path's tag filter.

        An embedding carries no reliable trace of an identifier like
        ``ACME-42``, so issues the query names by reference are resolved
        directly and ranked first (unless *resolve_identifiers* is False), even
        when no embedding model is configured.
        """
        # Narrow to the authenticating API key's project limit, if any.
        project_id, project_ids = scope_project_ids(project_id, project_ids)
        from specivo.services.embedding_service import EmbeddingService

        pinned: list[SearchResult] = []
        if resolve_identifiers:
            identifiers = extract_identifiers(query)
            if identifiers:
                pinned = await self._identifier_hits(
                    session, identifiers, user, project_id=project_id, project_ids=project_ids, filters=filters
                )
        pinned_slice, ranked_offset, ranked_limit = _page_window(len(pinned), offset, limit)

        emb_service = EmbeddingService()
        model = await emb_service.get_default_model(session)
        if model is None:
            return pinned[pinned_slice], len(pinned)

        try:
            query_vector = await emb_service.generate_embedding(query, model, intent="query")
        except Exception as exc:  # noqa: BLE001 - a broken model turns semantic search off, never a 500
            _warn_embedding_failure(model, exc)
            query_vector = None
        if query_vector is None:
            return pinned[pinned_slice], len(pinned)
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

        params.update(await self._visibility_params(session, user))

        # Visibility filters for semantic search (CTE-optimized)
        cte_prefix = self._visibility_cte_sql(user)
        issue_vis = self._issue_visibility_cte_clause(user, alias="i2")
        wiki_vis = self._wiki_visibility_cte_clause(user, alias="w2")
        comment_vis = self._comment_visibility_clause(user, journal_alias="cj", issue_alias="ci2")

        # Semantic search query: find chunks closest to the query vector.
        # The vector is bound as a text literal and cast in SQL, because
        # asyncpg has no codec for pgvector's vector type.
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
        att_sem_vis = self._attachment_semantic_vis(user)

        # Real-tag AND filter (hybrid parity with the keyword path). When active,
        # only tagged issues and wiki pages survive; comments/attachments drop out.
        tags_active = bool(filters and filters.tag_names)
        issue_tag_vis = self._tag_exists_clauses(filters, params, "issue_id", "i2")
        pinned_exclusion = ""
        if pinned:
            pinned_exclusion = "AND i2.id <> ALL(:pinned_issue_ids)"
            params["pinned_issue_ids"] = [r.id for r in pinned]
        wiki_tag_vis = self._tag_exists_clauses(filters, params, "wiki_page_id", "wp2")

        issue_branch = f"""(ss.source_type = '{_SST_ISSUE}' AND EXISTS (
                        SELECT 1 FROM issues i2 WHERE i2.id = ss.entity_id
                        {issue_vis}
                        {issue_tag_vis}
                        {pinned_exclusion}
                    ))"""
        wiki_branch = f"""(ss.source_type = '{_SST_WIKI_PAGE}' AND EXISTS (
                        SELECT 1 FROM wiki_pages wp2
                        JOIN wikis w2 ON w2.id = wp2.wiki_id
                        WHERE wp2.id = ss.entity_id
                        {wiki_vis}
                        {wiki_tag_vis}
                    ))"""
        comment_branch = f"""(ss.source_type = '{_SST_JOURNAL}' AND EXISTS (
                        SELECT 1 FROM journals cj
                        JOIN issues ci2 ON ci2.id = cj.issue_id
                        WHERE cj.id = ss.entity_id
                        {comment_vis}
                    ))"""
        att_branch = f"""(ss.source_type = '{_SST_ATTACHMENT}' AND att.id IS NOT NULL
                     {att_sem_vis})"""
        att_count_branch = f"""(ss.source_type = '{_SST_ATTACHMENT}' AND att_c.id IS NOT NULL
                     {self._attachment_semantic_count_vis(user)})"""

        _or_join = "\n                    OR\n                    "
        _result_branches = [issue_branch, wiki_branch]
        _count_branches = [issue_branch, wiki_branch]
        if not tags_active:
            _result_branches += [comment_branch, att_branch]
            _count_branches += [comment_branch, att_count_branch]
        sem_where_or = "(\n                    " + _or_join.join(_result_branches) + "\n                )"
        count_where_or = "(\n                    " + _or_join.join(_count_branches) + "\n                )"

        # Two phases: the CTE ``nearest`` picks candidate chunks by cosine
        # distance alone, then the outer query joins them to their entities,
        # applies visibility, keeps the best chunk per entity and ranks.
        #
        # Through the HNSW index (a partial index on the cast expression, so the
        # query repeats that expression), ``nearest`` holds the closest
        # ``candidates`` chunks. Visibility is applied afterwards, so when too
        # few candidates are visible the round is repeated with a wider set,
        # and once that grows past SEMANTIC_MAX_INDEX_CANDIDATES (or the index
        # scan returns fewer rows than asked) every embedding is ranked exactly.
        # A visible match therefore never drops out because hidden chunks rank
        # ahead of it. Without the index (another vector size, or a pgvector too
        # old for iterative scans) the exact ranking is used straight away.
        #
        # The exact ranking first narrows the rows to the projects in scope and,
        # for a non-admin, to projects where the user can see something at all
        # (every visibility rule needs such a ``user_visibility`` row), so the
        # planner can skip the distance for chunks that could never be shown.
        # That narrowing stays out of the index rounds: there it makes the
        # planner drop the index for a join plan.
        exact_scope = project_filter.replace("ss.", "ss0.")
        if not user.is_admin:
            exact_scope += """
                  AND ss0.project_id IN (SELECT uv0.project_id FROM user_visibility uv0
                                         WHERE uv0.visibility_level > 0 OR uv0.can_view_wiki)"""

        def nearest_cte(candidates: int | None) -> str:
            if candidates is None:
                body = f"""nearest AS MATERIALIZED (
                SELECT ce.chunk_id,
                       (1 - (ce.embedding <=> CAST(:query_vector AS vector))) AS score
                FROM chunk_embeddings ce
                JOIN search_chunks sc0 ON sc0.id = ce.chunk_id
                JOIN search_sources ss0 ON ss0.id = sc0.source_id
                WHERE ce.model_id = :model_id
                {exact_scope}
            )"""
            else:
                dims = SEMANTIC_INDEX_DIMENSIONS
                distance = f"ce.embedding::vector({dims}) <=> CAST(:query_vector AS vector({dims}))"
                body = f"""nearest AS MATERIALIZED (
                SELECT ce.chunk_id, (1 - ({distance})) AS score
                FROM chunk_embeddings ce
                WHERE ce.model_id = :model_id
                  AND vector_dims(ce.embedding) = {dims}
                ORDER BY {distance}
                LIMIT :candidates
            )"""
            # cte_prefix is "WITH user_visibility AS (...)" or empty (admins).
            return f"{cte_prefix},\n            {body}" if cte_prefix else f"WITH {body}"

        ranked_sql = f"""
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
                AND {sem_where_or}
                ORDER BY ss.source_type, ss.entity_id, n.score DESC
            ) deduped
            ORDER BY score DESC, result_type, id
            LIMIT :needed
        """

        # Rank the first offset + limit entities; the page is cut in Python.
        # The one-row ``fetched`` side reports how many candidates the round
        # read, even when none of them is visible.
        def semantic_sql(candidates: int | None) -> str:
            return f"""
            {nearest_cte(candidates)}
            SELECT fetched.n AS candidate_count, page.*
            FROM (SELECT count(*) AS n FROM nearest) fetched
            LEFT JOIN ({ranked_sql}) page ON true
            ORDER BY page.score DESC NULLS LAST, page.result_type, page.id
        """

        needed = ranked_offset + ranked_limit
        params["needed"] = needed
        rows: list[Any] = []
        if needed > 0:
            candidates: int | None = None
            if model.dimensions == SEMANTIC_INDEX_DIMENSIONS and await self._prepare_index_scan(session):
                candidates = needed * get_settings().search_semantic_overfetch
            while True:
                if candidates is not None and candidates > SEMANTIC_MAX_INDEX_CANDIDATES:
                    candidates = None
                params["candidates"] = candidates
                fetched = (await session.execute(text(semantic_sql(candidates)), params)).all()
                rows = [row for row in fetched if row._mapping["result_type"] is not None]
                logger.debug(
                    "Semantic search round: %s candidates, %d of %d results",
                    "all" if candidates is None else candidates,
                    len(rows),
                    needed,
                )
                if candidates is None or len(rows) >= needed:
                    break
                if fetched[0]._mapping["candidate_count"] < candidates:
                    # The index ran out before the limit: the table holds fewer
                    # rows, or the scan hit pgvector's hnsw.max_scan_tuples.
                    candidates = None
                elif rows:
                    # Widen by at least the share still missing, twice over.
                    candidates *= max(SEMANTIC_CANDIDATE_GROWTH, math.ceil(2 * needed / len(rows)))
                else:
                    candidates *= SEMANTIC_CANDIDATE_GROWTH**2

        results: list[SearchResult] = []
        for row in rows[ranked_offset:]:
            m = row._mapping
            results.append(
                SearchResult(
                    result_type=m["result_type"],
                    id=m["id"],
                    title=m["title"] or "",
                    subtitle=m["subtitle"],
                    # Raw stored content, rendered with ``| safe`` on the search
                    # page: escape it as the keyword path does. Hybrid search
                    # reuses these results, so this covers both modes.
                    snippet=_clean_snippet(m["snippet"]),
                    score=float(m["score"]) if m["score"] is not None else 0.0,
                    project_key=m["project_key"] or "",
                )
            )
        if pinned:
            results = self._rank_pinned_first(pinned[pinned_slice], results)

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
                AND {count_where_or}
            """
            count_result = await session.execute(text(count_sql), params)
            total = count_result.scalar_one() + len(pinned)

        return results, total

    async def hybrid_search(
        self,
        session: AsyncSession,
        query: str,
        user: User,
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

        Issues the query names by reference (``ACME-42``) are resolved
        directly and placed ahead of the fused ranking, rather than fused:
        RRF would let a strong semantic neighbour outrank the exact hit.

        Returns:
            Tuple of (results, total, type_counts). The type_counts are derived
            from the FTS count query (accurate per-type totals from the DB).
        """
        # Narrow to the authenticating API key's project limit, if any.
        project_id, project_ids = scope_project_ids(project_id, project_ids)
        pinned: list[SearchResult] = []
        identifiers = extract_identifiers(query)
        if identifiers and scope in ("all", "issues"):
            pinned = await self._identifier_hits(
                session, identifiers, user, project_id=project_id, project_ids=project_ids, filters=filters
            )

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
            resolve_identifiers=False,
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
            filters=filters,
            resolve_identifiers=False,
        )

        if not fts_results and not sem_results and not pinned:
            return [], 0, _empty_type_counts(include_all=True)

        # The pinned issues lead; drop them from the fused lists.
        pinned_keys = {f"{r.result_type}:{r.id}" for r in pinned}
        fts_results = [r for r in fts_results if f"{r.result_type}:{r.id}" not in pinned_keys]
        sem_results = [r for r in sem_results if f"{r.result_type}:{r.id}" not in pinned_keys]

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

        type_counts["issues"] += len(pinned)
        total = len(pinned) + len(sorted_keys)
        type_counts["all"] = sum(type_counts.values())

        # Apply pagination: the pinned issues take the first places.
        pinned_slice, ranked_offset, ranked_limit = _page_window(len(pinned), offset, limit)
        page_keys = sorted_keys[ranked_offset : ranked_offset + ranked_limit]

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
        if pinned:
            results = self._rank_pinned_first(pinned[pinned_slice], results)

        return results, total, type_counts
