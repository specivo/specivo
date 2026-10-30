"""Tagging at creation: ``tags`` on specivo_create_issue and specivo_create_wiki.

The tags are applied in the same tool call as the create, with the rules and
limits of ``specivo_tag(op="add")``: missing names are created, existing ones
are reused case-insensitively, and invalid input is an error string that
leaves nothing behind.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp.tools import _create_issue, _create_tag, _create_wiki
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.tag import Tag
from specivo.models.user import User
from specivo.models.wiki import WikiPage
from specivo.schemas.tag import MAX_TAG_NAMES, TAG_NAME_MAX
from specivo.services.tag_service import TagService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession):
    status = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    priority = PriorityFactory.build(name="Normal", is_default=True, position=2)
    t = TrackerFactory.build(name="Task", default_status_id=status.id)
    db_session.add_all([priority, t])
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="MCTG", name="MCP Create Tags", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="mctg_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _count(db: AsyncSession, model, *where) -> int:
    return await db.scalar(select(func.count()).select_from(model).where(*where)) or 0


async def _issue_by_subject(db: AsyncSession, subject: str) -> Issue:
    return (await db.execute(select(Issue).where(Issue.subject == subject))).scalar_one()


async def _page_by_title(db: AsyncSession, title: str) -> WikiPage:
    return (await db.execute(select(WikiPage).where(WikiPage.title == title))).scalar_one()


_INVALID = [
    pytest.param(["ok", "x" * (TAG_NAME_MAX + 1)], f"at most {TAG_NAME_MAX} characters", id="too-long"),
    pytest.param([f"tag-{i}" for i in range(MAX_TAG_NAMES + 1)], f"at most {MAX_TAG_NAMES} tag names", id="too-many"),
]


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------


async def test_create_issue_with_tags_applies_them(db_session, project, tracker, admin) -> None:
    await _create_tag(db_session, admin, project.key, "Backend")
    result = await _create_issue(
        db_session,
        admin,
        project.key,
        tracker.id,
        "Tagged issue",
        tags=["backend", " needs-review ", "NEEDS-REVIEW", ""],
    )
    assert result.startswith("Created issue"), result
    assert "Tags: Backend, needs-review" in result

    issue = await _issue_by_subject(db_session, "Tagged issue")
    tags = await TagService().tags_for_issue(db_session, issue.id)
    assert sorted(t.name for t in tags) == ["Backend", "needs-review"]
    # The existing tag is reused, only the missing one is created.
    assert await _count(db_session, Tag, Tag.project_id == project.id) == 2
    added = await _count(
        db_session,
        SecurityAuditLog,
        SecurityAuditLog.event_type == "tag_added",
        SecurityAuditLog.project_id == project.id,
    )
    assert added == 2


async def test_create_issue_without_tags_adds_none(db_session, project, tracker, admin) -> None:
    result = await _create_issue(db_session, admin, project.key, tracker.id, "Plain issue", tags=[])
    assert "Tags:" not in result
    issue = await _issue_by_subject(db_session, "Plain issue")
    assert await TagService().tags_for_issue(db_session, issue.id) == []


@pytest.mark.parametrize(("names", "message"), _INVALID)
async def test_create_issue_with_invalid_tags_creates_nothing(
    db_session, project, tracker, admin, names, message
) -> None:
    result = await _create_issue(db_session, admin, project.key, tracker.id, "Refused issue", tags=names)
    assert result.startswith("Error: ") and message in result
    assert await _count(db_session, Issue, Issue.subject == "Refused issue") == 0
    assert await _count(db_session, Tag, Tag.project_id == project.id) == 0


# ---------------------------------------------------------------------------
# Wiki pages
# ---------------------------------------------------------------------------


async def test_create_wiki_with_tags_applies_them(db_session, project, admin) -> None:
    result = await _create_wiki(db_session, admin, project.key, "Tagged page", "Body", tags=["runbook", "Ops"])
    assert result.startswith("Created wiki page"), result
    assert "Tags: runbook, Ops" in result

    page = await _page_by_title(db_session, "Tagged page")
    tags = await TagService().tags_for_wiki_page(db_session, page.id)
    assert sorted(t.name for t in tags) == ["Ops", "runbook"]


@pytest.mark.parametrize(("names", "message"), _INVALID)
async def test_create_wiki_with_invalid_tags_creates_nothing(db_session, project, admin, names, message) -> None:
    result = await _create_wiki(db_session, admin, project.key, "Refused page", "Body", tags=names)
    assert result.startswith("Error: ") and message in result
    assert await _count(db_session, WikiPage, WikiPage.title == "Refused page") == 0
    assert await _count(db_session, Tag, Tag.project_id == project.id) == 0
