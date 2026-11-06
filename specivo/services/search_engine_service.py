"""Search engine indexing of the pages anonymous visitors can read.

Indexing is off by default and opt-in twice over:

- the instance setting ``search_engine_indexing`` (admin setting, then the
  ``SEARCH_ENGINE_INDEXING`` environment variable, default ``false``) must be
  on; and
- the content must be readable without an account: the anonymous access
  switch is on and the project is public, active and opted in. That part is
  not decided here. Every query below runs as the anonymous principal through
  the same definitions the pages use (``resolve_anonymous_principal``,
  ``ProjectService.accessible_projects_clause``, ``check_permission`` and
  ``IssueService.visible_issues_clause``), so nothing can be listed that the
  page itself would refuse.

An instance behind a ``STEALTH_PREFIX`` is never indexed: ``robots.txt`` and
the sitemap are served at the root and would give the prefix away.

Everything is read from the database on every call, so turning the setting,
the switch or a project's opt-in on or off applies to the next request.

What this module provides:

- :func:`indexing_reader` — the anonymous principal when indexing applies;
- :func:`robots_txt` — the generated ``robots.txt`` body;
- the sitemap: an index (:func:`sitemap_index`) pointing at one sitemap of
  project pages and at per-project chunks of issues and wiki pages of at most
  :data:`SITEMAP_CHUNK_SIZE` URLs each, so a sitemap stays far below the
  protocol's 50,000 URL limit however big a project grows, and no request
  reads more than one chunk of rows.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote
from xml.sax.saxutils import escape

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.setting import Setting
from specivo.models.user import User
from specivo.models.wiki import Wiki, WikiPage
from specivo.services.issue_service import IssueService
from specivo.services.permission_service import Permission, check_permission
from specivo.services.project_service import ProjectService

logger = logging.getLogger(__name__)

# The instance setting. Only "true" turns indexing on, only "false" turns it
# off; any other stored value falls back to the environment.
SEARCH_ENGINE_INDEXING_SETTING_KEY = "search_engine_indexing"

# URLs per issue or wiki sitemap. The protocol allows 50,000; a smaller chunk
# keeps every sitemap response small and its query short.
SITEMAP_CHUNK_SIZE = 5000

SITEMAP_PATH = "/sitemap.xml"
SITEMAPS_PREFIX = "/sitemaps/"

_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"

_issue_svc = IssueService()
_project_svc = ProjectService()


# ---------------------------------------------------------------------------
# Is indexing on?
# ---------------------------------------------------------------------------


async def is_indexing_enabled(session: AsyncSession) -> bool:
    """Return whether the instance allows search engines to index its public pages.

    The admin setting wins over the ``SEARCH_ENGINE_INDEXING`` environment
    variable; both default to off. Always off behind a stealth prefix. This
    says nothing about *which* pages: only those an anonymous visitor can read
    ever become indexable.
    """
    settings = get_settings()
    if settings.stealth_prefix.strip("/"):
        return False
    value = await session.scalar(select(Setting.value).where(Setting.key == SEARCH_ENGINE_INDEXING_SETTING_KEY))
    if value in ("true", "false"):
        return value == "true"
    return settings.search_engine_indexing


async def indexing_reader(session: AsyncSession) -> User | None:
    """Return the anonymous principal when indexing applies, else None.

    None when the indexing setting is off, the anonymous access switch is
    off, or the anonymous row is missing.
    """
    if not await is_indexing_enabled(session):
        return None
    from specivo.core.security import resolve_anonymous_principal

    return await resolve_anonymous_principal(session)


@dataclass(frozen=True)
class ReadableProject:
    """A project anonymous visitors can read, and what they can read in it."""

    id: int
    key: str
    updated_at: datetime | None
    issues: bool
    wiki: bool


async def readable_projects(session: AsyncSession, anonymous: User) -> list[ReadableProject]:
    """Return the projects *anonymous* can read, ordered by key, with the permissions it holds there.

    The projects are those the project list shows the visitor; issues and
    wiki are decided per project by ``check_permission``, as the pages do.
    """
    readable = await _project_svc.accessible_projects_clause(session, anonymous)
    stmt = select(Project).order_by(Project.key)
    if readable is not None:
        stmt = stmt.where(readable)
    out: list[ReadableProject] = []
    for project in (await session.execute(stmt)).scalars():
        issues = await check_permission(anonymous, project.id, Permission.VIEW_ISSUES, session)
        wiki = await check_permission(anonymous, project.id, Permission.VIEW_WIKI, session)
        if issues or wiki:
            out.append(ReadableProject(project.id, project.key, project.updated_at, issues, wiki))
    return out


async def readable_project(session: AsyncSession, anonymous: User, key: str) -> ReadableProject | None:
    """Return the readable project with *key* (case-insensitive), or None."""
    wanted = key.upper()
    for project in await readable_projects(session, anonymous):
        if project.key.upper() == wanted:
            return project
    return None


# ---------------------------------------------------------------------------
# Page description
# ---------------------------------------------------------------------------

# Longest ``<meta name="description">``; search engines show about this much.
META_DESCRIPTION_LENGTH = 160
# Bounds the work on a long description: only its start can be shown anyway.
_META_SOURCE_CHARS = 4000

_FENCED_CODE = re.compile(r"```.*?(```|$)", re.S)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^>]{0,200}>")
_MARKUP = re.compile(r"[#*_>`|~]+")
_SPACES = re.compile(r"\s+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([.,;:!?])")


def meta_description(markdown_text: str | None) -> str:
    """A short plain-text summary of *markdown_text* for ``<meta name="description">``.

    Strips the common Markdown markup (code blocks, images, link targets,
    emphasis, headings, tables, inline HTML) and cuts the result at a word
    boundary. The template escapes the result; this only makes it readable.
    """
    if not markdown_text:
        return ""
    text = markdown_text[:_META_SOURCE_CHARS]
    text = _FENCED_CODE.sub(" ", text)
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _HTML_TAG.sub(" ", text)
    text = _MARKUP.sub(" ", text)
    text = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", _SPACES.sub(" ", text)).strip()
    if len(text) <= META_DESCRIPTION_LENGTH:
        return text
    cut = text[: META_DESCRIPTION_LENGTH - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" .,;:") + "\u2026"


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------


def _base_url() -> str:
    return get_settings().app_url.rstrip("/")


def robots_txt(projects: list[ReadableProject]) -> str:
    """The generated ``robots.txt`` for *projects*, the anonymously readable ones.

    Everything is disallowed except the project list, the readable projects'
    pages (``/projects/KEY/``: the project page, issue list and wiki pages)
    and their issues (``/issue/KEY-``). Search, sign-in, the API, admin and
    every other route fall under ``Disallow: /``; crawlers apply the longest
    matching rule, so the ``Allow`` lines win only for the paths they name.
    """
    lines = ["User-agent: *", "Allow: /projects/$"]
    for project in projects:
        lines.append(f"Allow: /projects/{project.key}/")
        if project.issues:
            lines.append(f"Allow: /issue/{project.key}-")
    lines += [
        f"Allow: {SITEMAP_PATH}",
        f"Allow: {SITEMAPS_PREFIX}",
        "Allow: /static/",
        "Disallow: /",
        "",
        f"Sitemap: {_base_url()}{SITEMAP_PATH}",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sitemaps
# ---------------------------------------------------------------------------


def _lastmod(value: datetime | None) -> str:
    if value is None:
        return ""
    return f"<lastmod>{value.isoformat(timespec='seconds')}</lastmod>"


def _urlset(entries: list[tuple[str, datetime | None]]) -> str:
    body = "".join(f"<url><loc>{escape(loc)}</loc>{_lastmod(mod)}</url>" for loc, mod in entries)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="{_SITEMAP_NS}">{body}</urlset>\n'


def _sitemapindex(entries: list[tuple[str, datetime | None]]) -> str:
    body = "".join(f"<sitemap><loc>{escape(loc)}</loc>{_lastmod(mod)}</sitemap>" for loc, mod in entries)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="{_SITEMAP_NS}">{body}</sitemapindex>\n'


async def _issue_scope(session: AsyncSession, anonymous: User, project: ReadableProject):
    """The predicate over ``issues`` selecting *project*'s issues *anonymous* can see."""
    clause = await _issue_svc.visible_issues_clause(session, anonymous)
    conditions = [Issue.project_id == project.id]
    if clause is not None:
        conditions.append(clause)
    return conditions


async def _issue_chunks(
    session: AsyncSession, anonymous: User, project: ReadableProject
) -> list[tuple[int, datetime | None]]:
    """The non-empty issue chunks of *project*: chunk number and newest change.

    A chunk holds the issues numbered ``chunk * SIZE + 1`` to
    ``(chunk + 1) * SIZE``, so a chunk's URL is stable while issues are added.
    """
    chunk = ((Issue.sequence_number - 1) // SITEMAP_CHUNK_SIZE).label("chunk")
    stmt = (
        select(chunk, func.max(Issue.updated_at))
        .where(*await _issue_scope(session, anonymous, project))
        .group_by(chunk)
        .order_by(chunk)
    )
    return [(int(n), mod) for n, mod in (await session.execute(stmt)).all()]


def _wiki_pages_stmt(project: ReadableProject):
    return (
        select(WikiPage.slug, WikiPage.updated_at)
        .join(Wiki, Wiki.id == WikiPage.wiki_id)
        .where(Wiki.project_id == project.id, WikiPage.deleted_at.is_(None))
    )


async def _wiki_chunks(session: AsyncSession, project: ReadableProject) -> list[tuple[int, datetime | None]]:
    """The wiki chunks of *project*, pages ordered by id: chunk number and newest change."""
    numbered = (
        select(
            ((func.row_number().over(order_by=WikiPage.id) - 1) // SITEMAP_CHUNK_SIZE).label("chunk"),
            WikiPage.updated_at.label("updated_at"),
        )
        .join(Wiki, Wiki.id == WikiPage.wiki_id)
        .where(Wiki.project_id == project.id, WikiPage.deleted_at.is_(None))
        .subquery()
    )
    stmt = (
        select(numbered.c.chunk, func.max(numbered.c.updated_at)).group_by(numbered.c.chunk).order_by(numbered.c.chunk)
    )
    return [(int(n), mod) for n, mod in (await session.execute(stmt)).all()]


def _issues_sitemap_path(key: str, chunk: int) -> str:
    return f"{SITEMAPS_PREFIX}{key}/issues-{chunk}.xml"


def _wiki_sitemap_path(key: str, chunk: int) -> str:
    return f"{SITEMAPS_PREFIX}{key}/wiki-{chunk}.xml"


async def sitemap_index(session: AsyncSession, anonymous: User) -> str | None:
    """The sitemap index, or None when no project is readable."""
    projects = await readable_projects(session, anonymous)
    if not projects:
        return None
    base = _base_url()
    newest = max((p.updated_at for p in projects if p.updated_at is not None), default=None)
    entries: list[tuple[str, datetime | None]] = [(f"{base}{SITEMAPS_PREFIX}projects.xml", newest)]
    for project in projects:
        if project.issues:
            entries += [
                (f"{base}{_issues_sitemap_path(project.key, n)}", mod)
                for n, mod in await _issue_chunks(session, anonymous, project)
            ]
        if project.wiki:
            entries += [
                (f"{base}{_wiki_sitemap_path(project.key, n)}", mod) for n, mod in await _wiki_chunks(session, project)
            ]
    return _sitemapindex(entries)


async def projects_sitemap(session: AsyncSession, anonymous: User) -> str | None:
    """The project list, and each readable project's page and issue list."""
    projects = await readable_projects(session, anonymous)
    if not projects:
        return None
    base = _base_url()
    entries: list[tuple[str, datetime | None]] = [(f"{base}/projects/", None)]
    for project in projects:
        entries.append((f"{base}/projects/{project.key}/", project.updated_at))
        if project.issues:
            entries.append((f"{base}/projects/{project.key}/issues/", None))
    return _urlset(entries)


async def issues_sitemap(session: AsyncSession, anonymous: User, key: str, chunk: int) -> str | None:
    """One chunk of a project's readable issues, or None when there is nothing to list."""
    project = await readable_project(session, anonymous, key)
    if project is None or not project.issues or chunk < 0:
        return None
    first = chunk * SITEMAP_CHUNK_SIZE + 1
    stmt = (
        select(Issue.project_key, Issue.sequence_number, Issue.updated_at)
        .where(
            *await _issue_scope(session, anonymous, project),
            Issue.sequence_number >= first,
            Issue.sequence_number < first + SITEMAP_CHUNK_SIZE,
        )
        .order_by(Issue.sequence_number)
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return None
    base = _base_url()
    return _urlset([(f"{base}/issue/{pk}-{seq}/", mod) for pk, seq, mod in rows])


async def wiki_sitemap(session: AsyncSession, anonymous: User, key: str, chunk: int) -> str | None:
    """One chunk of a project's wiki pages, or None when there is nothing to list."""
    project = await readable_project(session, anonymous, key)
    if project is None or not project.wiki or chunk < 0:
        return None
    stmt = _wiki_pages_stmt(project).order_by(WikiPage.id).offset(chunk * SITEMAP_CHUNK_SIZE).limit(SITEMAP_CHUNK_SIZE)
    rows = (await session.execute(stmt)).all()
    if not rows:
        return None
    base = _base_url()
    return _urlset([(f"{base}/projects/{project.key}/wiki/{quote(slug)}/", mod) for slug, mod in rows])
