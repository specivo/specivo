"""Project settings > Members tab: group rows alongside user rows.

The tab lists membership *rows*, and a row can be held by a user or by a user
group. These tests pin the three things that break if the tab keeps assuming
every row is a user: the group row reaches the template at all, it is keyed
and marked as a group, and it names the people the grant actually reaches —
which is the only way someone whose access arrives through a group can see
that from this screen.

Group and role names are unique database-wide and test modules run in
parallel inside uncommitted transactions, so names here carry a module-local
prefix.
"""

from __future__ import annotations

import itertools
from html.parser import HTMLParser

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member, MemberRole
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.schemas.project import ProjectCreate
from specivo.services.project_service import ProjectService
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_svc = ProjectService()

# Module-local name prefix — see the module docstring.
_PREFIX = "memtab"
_counter = itertools.count(1)


def _name(stem: str) -> str:
    return f"{_PREFIX}-{stem}-{next(_counter)}"


async def _create_project(db: AsyncSession, user, *, key: str, identifier: str):
    data = ProjectCreate(name=f"Members Tab {key}", identifier=identifier, key=key)
    project = await _svc.create(db, data, user)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_user(db: AsyncSession) -> User:
    obj = UserFactory.build()
    db.add(obj)
    await db.flush()
    return obj


async def _grant_to_group(db: AsyncSession, project, group: UserGroup) -> Role:
    role = Role(name=_name("Viewer"), permissions=["view_issues"], builtin=0, issues_visibility="default")
    db.add(role)
    await db.flush()
    member = Member(project_id=project.id, group_id=group.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()
    return role


async def test_members_tab_renders_group_rows_with_their_people(admin_client: AsyncClient, db_session: AsyncSession):
    project = await _create_project(db_session, admin_client.state.user, key="MTG", identifier="members-tab-group")

    group = UserGroup(name=_name("Developers"))
    db_session.add(group)
    await db_session.flush()
    covered = await _make_user(db_session)
    db_session.add(UserGroupMember(group_id=group.id, user_id=covered.id))
    role = await _grant_to_group(db_session, project, group)

    resp = await admin_client.get(
        f"/projects/{project.key}/settings/",
        cookies={"access_token": admin_client.state.token},
    )

    assert resp.status_code == 200
    # The row itself, self-describing as a group.
    assert '"principal_type": "group"' in resp.text
    assert group.name in resp.text
    assert role.name in resp.text
    # And who the grant reaches — the payoff of showing groups here at all.
    assert covered.login in resp.text


async def test_members_tab_offers_groups_in_the_picker(admin_client: AsyncClient, db_session: AsyncSession):
    """The picker is one field over both kinds, so every group is rendered with the page."""
    project = await _create_project(db_session, admin_client.state.user, key="MTP", identifier="members-tab-picker")

    group = UserGroup(name=_name("Designers"))
    db_session.add(group)
    await db_session.commit()

    resp = await admin_client.get(
        f"/projects/{project.key}/settings/",
        cookies={"access_token": admin_client.state.token},
    )

    assert resp.status_code == 200
    assert "allGroups:" in resp.text
    assert group.name in resp.text


async def test_project_overview_reports_people_reached_by_a_group(admin_client: AsyncClient, db_session: AsyncSession):
    """A project staffed only through a group must not read as unstaffed."""
    project = await _create_project(db_session, admin_client.state.user, key="MTO", identifier="members-tab-overview")

    group = UserGroup(name=_name("Support"))
    db_session.add(group)
    await db_session.flush()
    covered = await _make_user(db_session)
    db_session.add(UserGroupMember(group_id=group.id, user_id=covered.id))
    await _grant_to_group(db_session, project, group)

    resp = await admin_client.get(
        f"/projects/{project.key}/",
        cookies={"access_token": admin_client.state.token},
    )

    assert resp.status_code == 200
    assert "No members yet." not in resp.text


class _XDataGrabber(HTMLParser):
    """Collects the value of every ``x-data`` attribute naming *component*."""

    def __init__(self, component: str) -> None:
        super().__init__()
        self._component = component
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name == "x-data" and value and self._component in value:
                self.values.append(value)


def _x_data(html: str, component: str) -> str:
    grabber = _XDataGrabber(component)
    grabber.feed(html)
    assert grabber.values, f"no x-data attribute for {component!r} in the rendered page"
    return grabber.values[0]


async def test_members_x_data_attribute_survives_html_parsing(admin_client: AsyncClient, db_session: AsyncSession):
    """ADR-0001 section 3: a double-quoted x-data truncates and Alpine fails silently.

    The attribute is single-quoted and every interpolated value goes through
    ``tojson``, which escapes the apostrophe that would otherwise end it. This
    parses the response as HTML and checks the *last* key of the object is
    still inside the attribute, which it would not be if anything had closed
    the attribute early.
    """
    project = await _create_project(db_session, admin_client.state.user, key="MTX", identifier="members-tab-xdata")

    resp = await admin_client.get(
        f"/projects/{project.key}/settings/",
        cookies={"access_token": admin_client.state.token},
    )
    assert resp.status_code == 200

    attr = _x_data(resp.text, "projectMembers(")
    for key in ("members:", "projectKey:", "roles:", "allGroups:", "i18n:", "kindUser:"):
        assert key in attr, f"{key} missing from the x-data attribute — it was truncated"
    assert attr.rstrip().endswith("})"), "the x-data expression is not closed"


async def test_access_summary_labels_its_counts_instead_of_pluralising(
    admin_client: AsyncClient, db_session: AsyncSession
):
    """The summary reads "Groups: 1", never "1 groups".

    Its three numbers are Alpine-reactive, so Jinja's pluralize cannot see
    them, and a hand-written singular/plural pair would still only cover two
    forms — wrong in Russian, which has three. Labelling the number avoids
    plural agreement entirely, so the phrasing has to stay that way.
    """
    project = await _create_project(db_session, admin_client.state.user, key="MTP", identifier="members-tab-plural")

    resp = await admin_client.get(
        f"/projects/{project.key}/settings/",
        cookies={"access_token": admin_client.state.token},
    )
    assert resp.status_code == 200

    for label in ("Direct members:", "Groups:", "People with access:"):
        assert label in resp.text, f"{label} missing from the access summary"

    # The count-first phrasings cannot agree in every locale.
    for banned in ("</span> groups", "</span> direct members", "</span> people with access"):
        assert banned not in resp.text, f"count-first phrasing {banned!r} is back in the summary"
