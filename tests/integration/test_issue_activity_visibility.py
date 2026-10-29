"""The activity feed names only what its reader may see.

History (imported history above all) can name a version, a category, a sprint
or a parent issue of another project. A member of one project reading an
issue there must not learn the names of another project's records, or the key
of an issue they cannot see, from it.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.lookups import IssueCategory
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.sprint import Sprint
from specivo.models.user import User
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.version import VersionFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


async def _project(db: AsyncSession, key: str) -> Project:
    project = ProjectFactory.build(key=key, identifier=key.lower(), name=f"Project {key}", is_public=False)
    db.add(project)
    await db.flush()
    db.add(EnabledModule(project_id=project.id, name="issue_tracking"))
    await db.flush()
    return project


async def _issue(db: AsyncSession, project: Project, lookups: tuple, author: User, number: int) -> Issue:
    tracker, status, priority = lookups
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=number,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=author.id,
        subject=f"{project.key} issue {number}",
    )
    db.add(issue)
    await db.flush()
    return issue


@pytest_asyncio.fixture
async def history(db_session: AsyncSession, auth_client: AsyncClient) -> dict:
    """An issue of project VISA, read by its member, whose history names records of VISB too."""
    reader: User = auth_client.state.user
    status = StatusFactory.build(name="VisNew", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="VisBug", default_status_id=status.id)
    priority = PriorityFactory.build(name="VisNormal", position=1)
    db_session.add_all([tracker, priority])
    await db_session.flush()
    lookups = (tracker, status, priority)

    mine, theirs = await _project(db_session, "VISA"), await _project(db_session, "VISB")
    role = Role(name="VisReader", permissions=["view_issues"], builtin=0)
    db_session.add(role)
    await db_session.flush()
    member = Member(user_id=reader.id, project_id=mine.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))

    records = {
        "own_version": VersionFactory.build(project_id=mine.id, name="Vis own release"),
        # Shared with every project, yet its project is one the reader cannot open.
        "their_version": VersionFactory.build(project_id=theirs.id, name="Vis secret release", sharing="system"),
        "their_category": IssueCategory(project_id=theirs.id, name="Vis secret area"),
        "their_sprint": Sprint(project_id=theirs.id, name="Vis secret sprint"),
    }
    db_session.add_all(records.values())
    await db_session.flush()

    issue = await _issue(db_session, mine, lookups, reader, 1)
    own_parent = await _issue(db_session, mine, lookups, reader, 2)
    their_parent = await _issue(db_session, theirs, lookups, reader, 1)
    journal = Journal(issue_id=issue.id, project_id=mine.id, user_id=reader.id, sequence=1)
    db_session.add(journal)
    await db_session.flush()
    changes = [
        ("fixed_version_id", str(records["their_version"].id), str(records["own_version"].id)),
        ("category_id", None, str(records["their_category"].id)),
        ("sprint_id", None, str(records["their_sprint"].id)),
        ("parent_id", own_parent.display_key, None),
        ("parent_id", None, their_parent.display_key),
        # The source's text for a parent that was not imported names no Specivo issue.
        ("parent_id", None, "JIRA-77"),
    ]
    db_session.add_all(
        JournalDetail(journal_id=journal.id, property="attr", prop_key=key, old_value=old, new_value=new)
        for key, old, new in changes
    )
    await db_session.commit()
    return {"issue": issue, "own_parent": own_parent, "their_parent": their_parent, **records}


@pytest.mark.parametrize("partial", [False, True], ids=["page", "partial"])
async def test_history_names_no_record_of_a_project_the_reader_cannot_open(
    auth_client: AsyncClient, history: dict, partial: bool
) -> None:
    key = history["issue"].display_key
    url = f"/partials/issues/{key}/activity/" if partial else f"/issue/{key}/"
    resp = await auth_client.get(url)
    assert resp.status_code == 200, resp.text
    text = " ".join(resp.text.split())

    for secret in ("Vis secret release", "Vis secret area", "Vis secret sprint", history["their_parent"].display_key):
        assert secret not in text, secret
    # What the reader may see is still named.
    assert '<span class="sp-change-new">Vis own release</span>' in text
    assert f'<span class="sp-change-old">{history["own_parent"].display_key}</span>' in text
    assert '<span class="sp-change-new">JIRA-77</span>' in text
