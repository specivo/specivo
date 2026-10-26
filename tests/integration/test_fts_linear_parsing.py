"""Full-text indexing reads unclosed ``<!--`` in linear time, with unchanged lexemes.

PostgreSQL's text-search parser rereads the rest of the text for every
``<!--`` without a ``-->`` after it. ``specivo_fts_text`` (migration 0040)
turns those openers into text the parser reads once, and the triggers, the
snippet (``ts_headline``) and the query parser all go through it.

Timings are wall-clock in another process, so they are asserted as scaling:
four times the text must take well under sixteen times as long (the
quadratic case), and absolute times only against a catastrophe bound.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import _ALLOWED_FTS_LANGUAGES
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.search import SearchChunk, SearchSource
from specivo.models.user import User
from specivo.services.markdown_budget import MAX_MARKDOWN_CHARS
from specivo.services.search_service import MAX_QUERY_CHARS, _ParsedQuery
from specivo.services.wiki_service import WikiService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory
from tests.fixtures.markdown_inputs import CATASTROPHE_SECONDS, LINEAR_RATIO_LIMIT, fill

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_GUIDE = Path(__file__).resolve().parents[2] / "docs" / "guide"

# Inputs that made the parser quadratic, and linear ones kept as a control.
_CRAFTED = {
    "unclosed-comment-lines": "<!-- a\n",
    "bare-comment-openers": "<!--",
    "comment-missing-gt": "<!--a--",
    "comment-short-close": "<!--a->",
    "comment-tag-close": "<!--a>",
    "unclosed-tags": "<a ",
    "email-like": "a@",
    "url-like": "http://a",
}

# Normal text, and every way a comment can appear in it.
_NORMAL = [
    "Fix the login redirect for ACME-42 and see https://example.com/docs?a=1.",
    "Plain <b>bold</b> &amp; <i>italic</i>, mail team@example.com, path /etc/hosts.",
    "x <!-- hidden --> shown",
    "x <!-- hidden --> shown <!-- unclosed tail words",
    "a <!--b <!--c d",
    "<!--> after an empty comment <!-- and an open one",
    "--> stray closer then <!-- open",
    "<!-- one --> <!-- two --> three",
]


def _guide_pages() -> list[str]:
    return [p.read_text(encoding="utf-8") for p in sorted(_GUIDE.rglob("*.md"))]


async def _seconds(db: AsyncSession, sql: str, params: dict) -> float:
    """Best of three wall-clock timings of *sql*."""
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        await db.execute(text(sql), params)
        best = min(best, time.perf_counter() - started)
    return best


async def _ratio(db: AsyncSession, sql: str, unit: str) -> float:
    small = fill(unit, size=MAX_MARKDOWN_CHARS // 4)
    large = fill(unit)
    floor = 0.002
    small_s = max(await _seconds(db, sql, {"t": small}), floor)
    large_s = max(await _seconds(db, sql, {"t": large}), floor)
    assert large_s < CATASTROPHE_SECONDS
    return large_s / small_s


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="FTL", identifier="fts-linear", issue_sequence=1)
    db_session.add(proj)
    await db_session.flush()
    return proj


@pytest_asyncio.fixture
async def author(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="ftl_author", status="active")
    db_session.add(user)
    await db_session.flush()
    return user


@pytest_asyncio.fixture
async def issue(db_session: AsyncSession, project: Project, author: User) -> Issue:
    status = StatusFactory.build(name="FTL New", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="FTL Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="FTL Normal", position=1)
    db_session.add_all([tracker, priority])
    await db_session.flush()
    row = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=author.id,
        subject="Linear indexing",
        description="",
    )
    db_session.add(row)
    await db_session.flush()
    return row


# ---------------------------------------------------------------------------
# Same lexemes as before
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cfg", sorted(_ALLOWED_FTS_LANGUAGES))
async def test_sanitised_text_gives_the_same_vector_in_every_language(db_session: AsyncSession, cfg: str) -> None:
    for sample in [*_NORMAL, *_guide_pages()]:
        same = await db_session.scalar(
            text(
                "SELECT to_tsvector(CAST(:cfg AS regconfig), :t) "
                "= to_tsvector(CAST(:cfg AS regconfig), specivo_fts_text(:t))"
            ),
            {"cfg": cfg, "t": sample},
        )
        assert same, sample[:80]


async def test_sanitiser_only_touches_unclosed_openers(db_session: AsyncSession) -> None:
    cases = {
        "no comments at all": "no comments at all",
        "<!-- closed -->": "<!-- closed -->",
        "a <!-- b": "a  !-- b",
        "<!-- x --> y <!-- z": "<!-- x --> y  !-- z",
        "<!-->": "<!-->",
    }
    for source, expected in cases.items():
        assert await db_session.scalar(text("SELECT specivo_fts_text(:t)"), {"t": source}) == expected
    assert await db_session.scalar(text("SELECT specivo_fts_text(NULL)")) == ""


# The 0034 definition of each vector, computed from the raw text.
_OLD_ISSUE_VECTOR = """
    setweight(to_tsvector(specivo_fts_config(i.project_id), replace(coalesce(i.subject, ''), '_', ' ')), 'A') ||
    setweight(specivo_fts_identifiers(i.subject), 'A') ||
    setweight(to_tsvector(specivo_fts_config(i.project_id), coalesce(i.description, '')), 'B') ||
    setweight(specivo_fts_identifiers(i.description), 'B')
"""


async def test_issue_vectors_are_unchanged(db_session: AsyncSession, issue: Issue) -> None:
    for sample in _NORMAL:
        await db_session.execute(
            text("UPDATE issues SET description = :t WHERE id = :id"), {"t": sample, "id": issue.id}
        )
        same = await db_session.scalar(
            text(f"SELECT i.search_vector = ({_OLD_ISSUE_VECTOR}) FROM issues i WHERE i.id = :id"), {"id": issue.id}
        )
        assert same, sample


async def test_wiki_vectors_are_unchanged(db_session: AsyncSession, project: Project, author: User) -> None:
    for i, sample in enumerate(_NORMAL):
        page, content = await WikiService().create_page(
            db_session, project.id, f"Page {i}", sample, author, skip_link_rebuild=True
        )
        same = await db_session.scalar(
            text(
                """
                SELECT wc.search_vector = (
                    setweight(to_tsvector(specivo_fts_config(:pid), replace(coalesce(wp.title, ''), '_', ' ')), 'A') ||
                    setweight(specivo_fts_identifiers(wp.title), 'A') ||
                    setweight(to_tsvector(specivo_fts_config(:pid), coalesce(wc.text, '')), 'B') ||
                    setweight(specivo_fts_identifiers(wc.text), 'B'))
                FROM wiki_contents wc JOIN wiki_pages wp ON wp.id = wc.page_id
                WHERE wc.id = :id
                """
            ),
            {"id": content.id, "pid": project.id},
        )
        assert same, sample


@pytest.mark.parametrize("source_type", ["journal", "attachment"])
async def test_comment_and_attachment_chunk_vectors_are_unchanged(
    db_session: AsyncSession, project: Project, source_type: str
) -> None:
    source = SearchSource(source_type=source_type, entity_id=1, project_id=project.id)
    db_session.add(source)
    await db_session.flush()
    for i, sample in enumerate(_NORMAL):
        chunk = SearchChunk(source_id=source.id, chunk_index=i, content=sample)
        db_session.add(chunk)
        await db_session.flush()
        same = await db_session.scalar(
            text(
                "SELECT sc.search_vector = (to_tsvector(specivo_fts_config(:pid), sc.content) || "
                "specivo_fts_identifiers(sc.content)) FROM search_chunks sc WHERE sc.id = :id"
            ),
            {"id": chunk.id, "pid": project.id},
        )
        assert same, sample


# ---------------------------------------------------------------------------
# Linear time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(_CRAFTED))
async def test_issue_trigger_indexes_crafted_text_in_linear_time(
    db_session: AsyncSession, issue: Issue, name: str
) -> None:
    sql = f"UPDATE issues SET description = :t WHERE id = {issue.id}"
    assert await _ratio(db_session, sql, _CRAFTED[name]) < LINEAR_RATIO_LIMIT


@pytest.mark.parametrize("cfg", ["english", "simple"])
@pytest.mark.parametrize("name", ["unclosed-comment-lines", "bare-comment-openers", "comment-tag-close"])
async def test_vector_snippet_and_query_parse_crafted_text_in_linear_time(
    db_session: AsyncSession, cfg: str, name: str
) -> None:
    unit = _CRAFTED[name]
    for sql in (
        f"SELECT to_tsvector('{cfg}', specivo_fts_text(:t))",
        f"SELECT ts_headline('{cfg}', specivo_fts_text(:t), plainto_tsquery('{cfg}', 'a'))",
    ):
        assert await _ratio(db_session, sql, unit) < LINEAR_RATIO_LIMIT, sql


@pytest.mark.parametrize("name", ["unclosed-comment-lines", "bare-comment-openers"])
async def test_wiki_and_chunk_triggers_index_crafted_text_in_linear_time(
    db_session: AsyncSession, project: Project, author: User, name: str
) -> None:
    source = SearchSource(source_type="journal", entity_id=1, project_id=project.id)
    db_session.add(source)
    await db_session.flush()
    chunk = SearchChunk(source_id=source.id, chunk_index=0, content="")
    db_session.add(chunk)
    _page, content = await WikiService().create_page(
        db_session, project.id, "Crafted", "", author, skip_link_rebuild=True
    )
    await db_session.flush()
    for sql in (
        f"UPDATE search_chunks SET content = :t WHERE id = {chunk.id}",
        f"UPDATE wiki_contents SET text = :t WHERE id = {content.id}",
    ):
        assert await _ratio(db_session, sql, _CRAFTED[name]) < LINEAR_RATIO_LIMIT, sql


async def test_search_query_is_capped_and_parses(db_session: AsyncSession) -> None:
    """A query is cut to MAX_QUERY_CHARS before it reaches plainto_tsquery.

    Uncapped, 256 KB of words does not just parse slowly: ANDing every term
    exceeds PostgreSQL's stack depth.
    """
    parsed = _ParsedQuery.parse("<!-- a " * 40000)
    assert len(parsed.text) <= MAX_QUERY_CHARS
    started = time.perf_counter()
    await db_session.execute(text("SELECT plainto_tsquery('english', specivo_fts_text(:t))"), {"t": parsed.text})
    assert time.perf_counter() - started < CATASTROPHE_SECONDS
