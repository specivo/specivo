"""``SimilarIssueService.find``: lexical candidates, trigram rerank, visibility and the timeout.

Issues are inserted through the ORM, so the index trigger fills
``search_vector`` exactly as it does in production. The searching user is a
member with a ``view_issues`` role; admins bypass visibility and are not used.
"""

from __future__ import annotations

import re
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services import similar_issue_service
from specivo.services.permission_service import clear_role_cache
from specivo.services.search_service import SearchService
from specivo.services.similar_issue_service import SimilarIssueService
from specivo.services.similar_issues import SIMILAR_ISSUES_MIN_SCORE
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_service = SimilarIssueService()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def statuses(db_session: AsyncSession) -> dict[str, IssueStatus]:
    made = {
        "open": StatusFactory.build(name="Simnew", position=1, category="backlog"),
        "done": StatusFactory.build(name="Simdone", position=2, category="done"),
        "closed": StatusFactory.build(name="Simclosed", position=3, category="closed"),
    }
    db_session.add_all(made.values())
    await db_session.commit()
    return made


@pytest_asyncio.fixture
async def lookups(db_session: AsyncSession, statuses) -> tuple[Tracker, IssuePriority]:
    tracker = TrackerFactory.build(name="Simbug", default_status_id=statuses["open"].id)
    priority = PriorityFactory.build(name="Simnormal", is_default=False, position=2)
    db_session.add_all([tracker, priority])
    await db_session.commit()
    return tracker, priority


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="SIMSVC", identifier="sim-svc", is_public=False)
    db_session.add(proj)
    await db_session.commit()
    return proj


@pytest_asyncio.fixture
async def other_project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="SIMOTH", identifier="sim-other", is_public=False)
    db_session.add(proj)
    await db_session.commit()
    return proj


@pytest_asyncio.fixture
async def user(db_session: AsyncSession, project: Project, other_project: Project) -> User:
    u = UserFactory.build(login=f"sim_{uuid.uuid4().hex[:8]}", status="active")
    db_session.add(u)
    await db_session.commit()
    await grant(db_session, project, u)
    await grant(db_session, other_project, u)
    return u


@pytest_asyncio.fixture
async def author(db_session: AsyncSession) -> User:
    u = UserFactory.build(login=f"simauth_{uuid.uuid4().hex[:8]}", status="active")
    db_session.add(u)
    await db_session.commit()
    return u


async def grant(db: AsyncSession, project: Project, user: User, visibility: str = "default") -> None:
    role = Role(
        name=f"Sim-{uuid.uuid4().hex[:8]}",
        position=2,
        assignable=True,
        builtin=0,
        permissions=["view_issues", "add_issues"],
        issues_visibility=visibility,
        settings={},
    )
    db.add(role)
    await db.flush()
    member = Member(project_id=project.id, user_id=user.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()
    clear_role_cache(db)


_seq = iter(range(1, 100_000))


async def make_issue(
    db: AsyncSession,
    project: Project,
    lookups,
    author: User,
    subject: str,
    *,
    status: IssueStatus | None = None,
    description: str | None = None,
    is_private: bool = False,
    commit: bool = True,
) -> Issue:
    tracker, priority = lookups
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=next(_seq),
        tracker_id=tracker.id,
        status_id=(status.id if status else tracker.default_status_id),
        priority_id=priority.id,
        author_id=author.id,
        subject=subject,
        description=description,
        is_private=is_private,
    )
    db.add(issue)
    if commit:
        await db.commit()
    else:
        await db.flush()
    return issue


async def find(db: AsyncSession, project: Project, user: User, q: str, limit: int = 5):
    return await _service.find(db, project, user, q, limit)


# ---------------------------------------------------------------------------
# Matching and ranking
# ---------------------------------------------------------------------------


async def test_paraphrased_title_found_where_and_search_is_not(db_session, project, lookups, user, author) -> None:
    issue = await make_issue(db_session, project, lookups, author, "Login page shows a timeout error")
    q = "Timeout when opening the login page"

    results, _total, _counts = await SearchService().search(db_session, q, user, project_id=project.id, scope="issues")
    assert results == []

    found = await find(db_session, project, user, q)
    assert [r.key for r in found] == [issue.display_key]
    assert found[0].score > SIMILAR_ISSUES_MIN_SCORE


async def test_ranks_by_similarity_and_limits_to_five(db_session, project, lookups, user, author) -> None:
    subjects = [
        "Login page timeout error seen on older mobile devices during peak hours in the evening after the release",
        "Login page timeout error",
        "Login page timeout error seen on older mobile devices during peak hours",
        "Login page timeout error on mobile",
        "Login page timeout error seen on older mobile devices during peak hours in the evening",
        "Login page timeout error on mobile devices",
        "Login page timeout error seen on older mobile devices",
    ]
    for subject in subjects:
        await make_issue(db_session, project, lookups, author, subject)

    found = await find(db_session, project, user, "Login page timeout error")

    assert [r.subject for r in found] == sorted(subjects, key=len)[:5]
    scores = [r.score for r in found]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == pytest.approx(1.0)


async def test_closed_issue_included_and_marked(db_session, project, lookups, user, author, statuses) -> None:
    issue = await make_issue(
        db_session, project, lookups, author, "Export button does nothing", status=statuses["closed"]
    )

    found = await find(db_session, project, user, "Export button does nothing on click")

    assert [r.key for r in found] == [issue.display_key]
    assert found[0].is_closed is True
    assert found[0].status_name == "Simclosed"
    assert found[0].status_id == statuses["closed"].id


async def test_open_issue_ranks_above_closed_on_equal_score(db_session, project, lookups, user, author, statuses):
    opened = await make_issue(db_session, project, lookups, author, "Export button does nothing")
    closed = await make_issue(
        db_session, project, lookups, author, "Export button does nothing", status=statuses["closed"]
    )

    found = await find(db_session, project, user, "Export button does nothing")

    assert [r.key for r in found] == [opened.display_key, closed.display_key]


async def test_done_status_counts_as_open(db_session, project, lookups, user, author, statuses) -> None:
    closed = await make_issue(
        db_session, project, lookups, author, "Export button does nothing", status=statuses["closed"]
    )
    done = await make_issue(db_session, project, lookups, author, "Export button does nothing", status=statuses["done"])

    found = await find(db_session, project, user, "Export button does nothing")

    assert [r.key for r in found] == [done.display_key, closed.display_key]
    assert found[0].is_closed is False


async def test_unrelated_title_returns_nothing(db_session, project, lookups, user, author) -> None:
    await make_issue(db_session, project, lookups, author, "Login page shows a timeout error")

    assert await find(db_session, project, user, "Banana recipe for smoothies") == []


async def test_single_shared_word_below_threshold_returns_nothing(db_session, project, lookups, user, author) -> None:
    subject = "Weekly status report for the operations team dashboard and the sprint review meeting"
    q = "Export report to PDF"
    await make_issue(db_session, project, lookups, author, subject)

    score = await db_session.scalar(select(func.similarity(subject, q)))
    assert score < SIMILAR_ISSUES_MIN_SCORE
    assert await find(db_session, project, user, q) == []


@pytest.mark.parametrize("q", ["the and of to", "!!!???---", "   ", "a"])
async def test_stop_word_or_punctuation_input_returns_empty(db_session, project, lookups, user, author, q) -> None:
    await make_issue(db_session, project, lookups, author, "The and of to a")
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        assert await find(db_session, project, user, q) == []
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert not [s for s in statements if re.search(r"\bissues\b", s)]


@pytest.mark.parametrize(
    "q",
    ["foo:* & !bar | (baz", "' | 'x", "a<->b", r"C:\temp\foo.txt", "--;drop table issues", "x' || 'y"],
)
async def test_tsquery_syntax_in_input_is_harmless(db_session, project, lookups, user, author, q) -> None:
    await make_issue(db_session, project, lookups, author, "Temp foo text file missing")
    before = await db_session.scalar(select(func.count()).select_from(Issue))

    found = await find(db_session, project, user, q)

    assert isinstance(found, list)
    assert await db_session.scalar(select(func.count()).select_from(Issue)) == before


async def test_nul_byte_does_not_raise(db_session, project, lookups, user, author) -> None:
    await make_issue(db_session, project, lookups, author, "Login page broken")

    found = await find(db_session, project, user, "login\x00 page")

    assert isinstance(found, list)


async def test_description_only_match_is_not_a_candidate(db_session, project, lookups, user, author) -> None:
    # Created first, so it has the lowest id and loses every rank tie.
    target = await make_issue(db_session, project, lookups, author, "Webhook delivery retries missing")
    for n in range(60):
        await make_issue(
            db_session,
            project,
            lookups,
            author,
            f"Item {n}",
            description="webhook delivery retries missing " * 5,
            commit=False,
        )
    await db_session.commit()

    found = await find(db_session, project, user, "Webhook delivery retries missing", limit=10)

    assert [r.key for r in found] == [target.display_key]


async def test_query_mirrors_index_for_underscores(db_session, project, lookups, user, author) -> None:
    issue = await make_issue(db_session, project, lookups, author, "Token expired error on refresh")

    found = await find(db_session, project, user, "token_expired_error")

    assert [r.key for r in found] == [issue.display_key]


async def test_uses_project_fts_config(db_session, project, lookups, user, author) -> None:
    # Every word is an English stop word: under ``english`` neither side has a
    # lexeme, under ``simple`` all of them are kept.
    project.fts_language = "simple"
    await db_session.commit()
    issue = await make_issue(db_session, project, lookups, author, "Where is it then")

    found = await find(db_session, project, user, "where is it")

    assert [r.key for r in found] == [issue.display_key]


async def test_english_project_drops_stop_word_query(db_session, project, lookups, user, author) -> None:
    project.fts_language = "english"
    await db_session.commit()
    await make_issue(db_session, project, lookups, author, "Where is it then")

    assert await find(db_session, project, user, "where is it") == []


async def test_other_project_is_excluded(db_session, project, other_project, lookups, user, author) -> None:
    await make_issue(db_session, other_project, lookups, author, "Login page shows a timeout error")

    assert await find(db_session, project, user, "Login page shows a timeout error") == []


async def test_other_users_private_issue_is_hidden(db_session, project, lookups, user, author) -> None:
    shown = await make_issue(db_session, project, lookups, author, "Login page shows a timeout error")
    await make_issue(db_session, project, lookups, author, "Login page shows a timeout error", is_private=True)

    found = await find(db_session, project, user, "Login page shows a timeout error")

    assert [r.key for r in found] == [shown.display_key]


# ---------------------------------------------------------------------------
# Statement timeout
# ---------------------------------------------------------------------------


async def test_timeout_returns_empty_and_session_stays_usable(
    db_session, project, lookups, user, author, monkeypatch
) -> None:
    await make_issue(db_session, project, lookups, author, "Login page shows a timeout error")
    before = await db_session.scalar(text("SHOW statement_timeout"))
    monkeypatch.setattr(similar_issue_service, "STATEMENT_TIMEOUT_MS", 10)
    monkeypatch.setattr(SimilarIssueService, "_statement", lambda self, *a, **k: text("SELECT pg_sleep(0.2)"))

    assert await find(db_session, project, user, "Login page shows a timeout error") == []

    assert await db_session.scalar(text("SELECT 1")) == 1
    assert await db_session.scalar(text("SHOW statement_timeout")) == before
    assert project.key == "SIMSVC"
    await db_session.commit()


async def test_other_database_errors_are_raised(db_session, project, lookups, user, author, monkeypatch) -> None:
    await make_issue(db_session, project, lookups, author, "Login page shows a timeout error")
    monkeypatch.setattr(
        SimilarIssueService, "_statement", lambda self, *a, **k: text("SELECT no_such_column FROM issues")
    )

    with pytest.raises(DBAPIError):
        await find(db_session, project, user, "Login page shows a timeout error")

    await db_session.rollback()
    assert await db_session.scalar(text("SELECT 1")) == 1


async def test_success_path_restores_statement_timeout(db_session, project, lookups, user, author) -> None:
    await make_issue(db_session, project, lookups, author, "Login page shows a timeout error")
    before = await db_session.scalar(text("SHOW statement_timeout"))

    assert await find(db_session, project, user, "Login page shows a timeout error")

    assert await db_session.scalar(text("SHOW statement_timeout")) == before


# ---------------------------------------------------------------------------
# Unsegmented scripts: the recency scan
# ---------------------------------------------------------------------------


async def _skip_on_c_ctype(db: AsyncSession) -> None:
    ctype = await db.scalar(text("SELECT datctype FROM pg_database WHERE datname = current_database()"))
    if ctype in ("C", "POSIX"):
        pytest.skip("similarity() scores 0 on non-ASCII text under lc_ctype=C")


@pytest.mark.parametrize(
    ("subject", "q"),
    [("หน้าล็อกอินโหลดไม่ขึ้น", "หน้าเข้าสู่ระบบโหลดไม่ได้"), ("登录页面打不开", "登录页面无法加载")],
)
async def test_unsegmented_title_found_by_recency_scan(db_session, project, lookups, user, author, subject, q):
    await _skip_on_c_ctype(db_session)
    issue = await make_issue(db_session, project, lookups, author, subject)
    await make_issue(db_session, project, lookups, author, "支付接口超时")

    found = await find(db_session, project, user, q)

    assert [r.key for r in found] == [issue.display_key]


async def test_recency_scan_ignores_issues_beyond_limit(db_session, project, lookups, user, author, monkeypatch):
    await _skip_on_c_ctype(db_session)
    monkeypatch.setattr(similar_issue_service, "RECENCY_SCAN_LIMIT", 3)
    await make_issue(db_session, project, lookups, author, "登录页面打不开")
    for n in range(3):
        await make_issue(db_session, project, lookups, author, f"Unrelated {n}")

    assert await find(db_session, project, user, "登录页面无法加载") == []


async def test_recency_scan_applies_visibility_before_limit(db_session, project, lookups, user, author, monkeypatch):
    await _skip_on_c_ctype(db_session)
    monkeypatch.setattr(similar_issue_service, "RECENCY_SCAN_LIMIT", 3)
    target = await make_issue(db_session, project, lookups, author, "登录页面打不开")
    for _ in range(3):
        await make_issue(db_session, project, lookups, author, "登录页面打不开", is_private=True)

    found = await find(db_session, project, user, "登录页面无法加载")

    assert [r.key for r in found] == [target.display_key]
