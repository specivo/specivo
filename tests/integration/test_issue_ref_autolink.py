"""Integration tests: issue-reference autolinks only resolve existing refs.

Covers IssueService.resolve_known_issue_refs (current issues + move aliases)
and the preview endpoint linking real refs while leaving bogus ones plain.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from specivo.services.markdown_service import render_wiki_markdown
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = pytest.mark.integration

_svc = IssueService()


@pytest_asyncio.fixture
async def open_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, open_status: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Bug", default_status_id=open_status.id)
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def priority(db_session: AsyncSession) -> IssuePriority:
    p = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="autolink_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def projects(db_session: AsyncSession) -> tuple[Project, Project]:
    a = ProjectFactory.build(key="ACME", identifier="acme-al")
    b = ProjectFactory.build(key="BCME", identifier="bcme-al")
    db_session.add_all([a, b])
    await db_session.commit()
    await db_session.refresh(a)
    await db_session.refresh(b)
    return a, b


@pytest.mark.asyncio
async def test_resolve_known_refs_includes_current_and_alias_excludes_bogus(
    db_session, projects, tracker, priority, admin_user
):
    acme, bcme = projects
    # ACME-1 exists.
    await _svc.create(
        db_session, acme, IssueCreate(project_key="ACME", tracker_id=tracker.id, subject="one"), admin_user
    )
    # Create ACME-2 then move it to BCME -> BCME-1, leaving ACME-2 as a move alias.
    movable = await _svc.create(
        db_session, acme, IssueCreate(project_key="ACME", tracker_id=tracker.id, subject="two"), admin_user
    )
    await db_session.flush()
    moved_old_ref = movable.display_key  # ACME-2
    await _svc.move(db_session, movable, bcme, admin_user)
    await db_session.flush()
    moved_new_ref = movable.display_key  # BCME-1

    text = f"Refs: ACME-1 (current), {moved_old_ref} (moved/alias), {moved_new_ref} (current), ZZZ-99 (bogus)."
    known = await _svc.resolve_known_issue_refs(db_session, text)

    assert "ACME-1" in known
    assert moved_old_ref in known  # old ref still resolves via alias
    assert moved_new_ref in known
    assert "ZZZ-99" not in known

    # And the renderer links only the known ones.
    html = str(render_wiki_markdown(text, known_issue_refs=known))
    assert "/issue/ACME-1/" in html
    assert f"/issue/{moved_new_ref}/" in html
    assert "/issue/ZZZ-99/" not in html
    assert "ZZZ-99" in html  # plain text


@pytest.mark.asyncio
async def test_preview_links_existing_ref_not_bogus(
    client: AsyncClient, db_session, projects, tracker, priority, admin_user
):
    acme, _bcme = projects
    await _svc.create(
        db_session, acme, IssueCreate(project_key="ACME", tracker_id=tracker.id, subject="real"), admin_user
    )
    await db_session.commit()

    token_resp = await client.post(
        "/api/v1/auth/login/", json={"login": admin_user.login, "password": "testpassword"}
    )
    token = token_resp.json()["access_token"]

    resp = await client.post(
        "/api/v1/markdown/preview/",
        json={"text": "Done in ACME-1, unlike NOPE-7.", "context": "issue"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    html = resp.json()["html"]
    assert "/issue/ACME-1/" in html
    assert "/issue/NOPE-7/" not in html
    assert "NOPE-7" in html
