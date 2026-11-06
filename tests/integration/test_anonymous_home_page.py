"""Where the site root sends a visitor without an account.

The dashboard at ``/`` is never served anonymously; it is deliberately not in
``ANONYMOUS_WEB_ROUTES``. What depends on anonymous access is only where a
refused visitor goes: to the project list while anonymous access is live (the
instance switch on and at least one project readable without an account), and
to the login screen otherwise. Before this, a public instance's main URL sent
everyone to the login screen although its projects were readable signed out.
"""

from __future__ import annotations

from datetime import timedelta

import jwt
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.constants import JWT_ALGORITHM
from specivo.core.utils import utcnow
from specivo.models.project import EnabledModule, Project
from specivo.models.user import User
from specivo.services.anonymous_access_service import (
    set_anonymous_access_enabled,
    set_anonymous_permissions,
)
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import Permission
from specivo.web.deps import ANONYMOUS_WEB_ROUTES
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


async def _project(db: AsyncSession, key: str, *, is_public: bool = True, status: int = 1) -> Project:
    project = ProjectFactory.build(key=key, identifier=key.lower(), is_public=is_public, status=status)
    db.add(project)
    await db.flush()
    for module in ("issue_tracking", "wiki"):
        db.add(EnabledModule(project_id=project.id, name=module))
    await db.commit()
    return project


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="anonhome_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def opted_in(db_session: AsyncSession, admin: User) -> Project:
    """A public project opted in to anonymous reading, with the switch still off."""
    project = await _project(db_session, "ANONHOME")
    await set_anonymous_permissions(db_session, project, [Permission.VIEW_ISSUES, Permission.VIEW_WIKI], admin)
    await db_session.commit()
    return project


async def _switch_on(db: AsyncSession, admin: User, keys: list[str]) -> None:
    await set_anonymous_access_enabled(db, True, admin, confirmed_projects=keys)
    await db.commit()


def assert_sent_to(resp, location: str) -> None:
    assert resp.status_code == 302, resp.status_code
    assert resp.headers["location"] == location
    assert resp.text == ""
    # The answer depends on cookies and on settings that change at any time.
    assert resp.headers["cache-control"] == "no-store"
    assert "Cookie" in resp.headers["vary"]


async def test_the_dashboard_stays_out_of_the_anonymous_pages() -> None:
    assert "/" not in ANONYMOUS_WEB_ROUTES


async def test_live_anonymous_access_sends_the_visitor_to_the_project_list(
    client: AsyncClient, db_session: AsyncSession, admin: User, opted_in: Project
) -> None:
    await _switch_on(db_session, admin, [opted_in.key])
    resp = await client.get("/", follow_redirects=False)
    assert_sent_to(resp, "/projects/")

    # And the project list it lands on is served, not refused.
    landed = await client.get("/projects/", follow_redirects=False)
    assert landed.status_code == 200
    assert opted_in.name in landed.text


async def test_switch_off_sends_the_visitor_to_the_login_screen(client: AsyncClient, opted_in: Project) -> None:
    resp = await client.get("/", follow_redirects=False)
    assert_sent_to(resp, "/login/")


async def test_switch_on_without_an_opted_in_project_sends_the_visitor_to_the_login_screen(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    # A public project that is not opted in does not count.
    await _project(db_session, "ANONHPUB")
    await _switch_on(db_session, admin, [])
    resp = await client.get("/", follow_redirects=False)
    assert_sent_to(resp, "/login/")


async def test_an_archived_opted_in_project_does_not_count(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    """Only a project the visitor could actually read makes the site public."""
    archived = await _project(db_session, "ANONHARCH")
    await set_anonymous_permissions(db_session, archived, [Permission.VIEW_ISSUES], admin)
    await db_session.commit()
    archived.status = 9
    await db_session.commit()
    await _switch_on(db_session, admin, [archived.key])

    resp = await client.get("/", follow_redirects=False)
    assert_sent_to(resp, "/login/")


async def test_changes_apply_on_the_next_request(
    client: AsyncClient, db_session: AsyncSession, admin: User, opted_in: Project
) -> None:
    await _switch_on(db_session, admin, [opted_in.key])
    assert_sent_to(await client.get("/", follow_redirects=False), "/projects/")

    await set_anonymous_permissions(db_session, opted_in, [], admin)
    await db_session.commit()
    assert_sent_to(await client.get("/", follow_redirects=False), "/login/")

    await set_anonymous_permissions(db_session, opted_in, [Permission.VIEW_WIKI], admin)
    await db_session.commit()
    assert_sent_to(await client.get("/", follow_redirects=False), "/projects/")

    await set_anonymous_access_enabled(db_session, False, admin)
    await db_session.commit()
    assert_sent_to(await client.get("/", follow_redirects=False), "/login/")


async def test_signed_in_users_still_get_the_dashboard(
    client: AsyncClient, db_session: AsyncSession, admin: User, opted_in: Project
) -> None:
    await _switch_on(db_session, admin, [opted_in.key])
    member = UserFactory.build(login="anonhome_member", status="active")
    db_session.add(member)
    await db_session.commit()

    resp = await client.get(
        "/", cookies={"access_token": _make_access_token(member, get_settings())}, follow_redirects=False
    )
    assert resp.status_code == 200
    assert "<html" in resp.text.lower()


async def test_a_failed_session_is_asked_to_sign_in_again(
    client: AsyncClient, db_session: AsyncSession, admin: User, opted_in: Project
) -> None:
    """A visitor whose cookie no longer works is not quietly shown the public site instead."""
    await _switch_on(db_session, admin, [opted_in.key])
    settings = get_settings()
    expired = jwt.encode(
        {
            "sub": str(admin.id),
            "type": "access",
            "iat": utcnow() - timedelta(hours=2),
            "exp": utcnow() - timedelta(hours=1),
        },
        settings.secret_key,
        algorithm=JWT_ALGORITHM,
    )
    for cookie in (expired, "not-a-token"):
        resp = await client.get("/", cookies={"access_token": cookie}, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login/"
