"""The per-project branch name format and the "Copy branch name" button on the issue page."""

from __future__ import annotations

import html

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.project import Project
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="BRN", identifier="brn", name="Branch names")
    db_session.add(proj)
    await db_session.commit()
    return proj


@pytest_asyncio.fixture
async def issue(admin_client: AsyncClient, db_session: AsyncSession, project: Project):
    status = StatusFactory.build(name="Brn New")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Feature", default_status_id=status.id)
    priority = PriorityFactory.build(is_default=True)
    db_session.add_all([tracker, priority])
    await db_session.commit()
    from sqlalchemy import select

    from specivo.models.user import User

    admin = (await db_session.execute(select(User).where(User.login == "admin_fixture_user"))).scalar_one()
    created = await IssueService().create(
        db_session,
        project,
        IssueCreate(project_key=project.key, tracker_id=tracker.id, subject="Add the login form!"),
        admin,
    )
    await db_session.commit()
    return created


def _cookies(client: AsyncClient) -> dict[str, str]:
    return {"access_token": client.state.token}


class TestSetting:
    async def test_defaults_are_returned(self, admin_client, project):
        resp = await admin_client.get(f"/api/v1/projects/{project.key}/", cookies=_cookies(admin_client))
        assert resp.status_code == 200
        assert resp.json()["branch_name"] == {
            "format": "{type}/{key}-{slug}",
            "types": {"bug": "fix", "feature": "feat"},
            "default_type": "chore",
        }

    async def test_save_format_and_types(self, admin_client, project):
        body = {"branch_name": {"format": "{key}/{slug}", "types": {"Feature": "feature"}, "default_type": "task"}}
        resp = await admin_client.patch(f"/api/v1/projects/{project.key}/", json=body, cookies=_cookies(admin_client))
        assert resp.status_code == 200, resp.text
        assert resp.json()["branch_name"] == {
            "format": "{key}/{slug}",
            "types": {"feature": "feature"},
            "default_type": "task",
        }

    @pytest.mark.parametrize(
        "branch_name",
        [
            {"format": "{type}/{slug}"},  # no {key}
            {"format": "{key} {slug}"},
            {"format": "{key}", "types": {"Bug": "has space"}},
            {"format": "{key}", "default_type": ""},
        ],
    )
    async def test_invalid_settings_rejected(self, admin_client, project, branch_name):
        resp = await admin_client.patch(
            f"/api/v1/projects/{project.key}/", json={"branch_name": branch_name}, cookies=_cookies(admin_client)
        )
        assert resp.status_code == 422, resp.text


class TestIssuePage:
    async def test_page_shows_the_computed_branch_name(self, admin_client, issue):
        resp = await admin_client.get(f"/issue/{issue.display_key}/", cookies=_cookies(admin_client))
        assert resp.status_code == 200
        expected = f"feat/{issue.display_key}-add-the-login-form"
        assert expected in html.unescape(resp.text)
        assert "Copy branch name" in resp.text

    async def test_page_follows_the_project_format(self, admin_client, project, issue):
        await admin_client.patch(
            f"/api/v1/projects/{project.key}/",
            json={"branch_name": {"format": "{key}/{slug}"}},
            cookies=_cookies(admin_client),
        )
        resp = await admin_client.get(f"/issue/{issue.display_key}/", cookies=_cookies(admin_client))
        assert f"{issue.display_key}/add-the-login-form" in html.unescape(resp.text)
