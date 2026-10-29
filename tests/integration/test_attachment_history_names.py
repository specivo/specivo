"""History names the files an issue gained and lost, by the issue page's rules.

An ``attachment`` detail keeps the attachment's id as ``prop_key`` and the
file name as its value. The issue page, the activity partial, the REST API
and MCP show it as "File report.pdf added" / "deleted": by the file's current
name when the reader may open it, else by the name the detail stored (the file
may be gone, or on an issue the reader cannot see). A visitor without an
account, who may open no file, is shown no such change. Deleting a file from
an issue records the change, under the file's name.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.mcp.tools import _change_line
from specivo.models.attachment import Attachment
from specivo.models.issue import Issue
from specivo.models.journal import ATTACHMENT_DETAIL, Journal, JournalDetail
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.activity_lookups import activity_lookup_maps
from specivo.services.anonymous_access_service import set_anonymous_access_enabled, set_anonymous_permissions
from specivo.services.permission_service import Permission, clear_role_cache
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


async def _project(db: AsyncSession, key: str, *, is_public: bool = False) -> Project:
    project = ProjectFactory.build(key=key, identifier=key.lower(), name=f"Project {key}", is_public=is_public)
    db.add(project)
    await db.flush()
    db.add(EnabledModule(project_id=project.id, name="issue_tracking"))
    await db.flush()
    return project


async def _join(db: AsyncSession, user: User, project: Project, role_name: str) -> None:
    role = Role(name=role_name, permissions=["view_issues"], builtin=0, issues_visibility="default")
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()


async def _lookups(db: AsyncSession, prefix: str) -> tuple:
    status = StatusFactory.build(name=f"{prefix} New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name=f"{prefix} Bug", default_status_id=status.id)
    priority = PriorityFactory.build(name=f"{prefix} Normal", position=1)
    db.add_all([tracker, priority])
    await db.flush()
    return tracker, status, priority


async def _issue(db: AsyncSession, project: Project, lookups: tuple, author: User, number: int = 1) -> Issue:
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


async def _attachment(db: AsyncSession, container_type: str, container_id: int, filename: str, author: User) -> int:
    attachment = Attachment(
        container_type=container_type,
        container_id=container_id,
        filename=filename,
        disk_filename=f"ahn-{container_type}-{container_id}-{filename}",
        content_type="text/plain",
        filesize=1,
        author_id=author.id,
    )
    db.add(attachment)
    await db.flush()
    return attachment.id


async def _history(db: AsyncSession, issue: Issue, author: User, changes: list[tuple[str, str | None, str | None]]):
    journal = Journal(issue_id=issue.id, project_id=issue.project_id, user_id=author.id, sequence=2)
    db.add(journal)
    await db.flush()
    db.add_all(
        JournalDetail(journal_id=journal.id, property=ATTACHMENT_DETAIL, prop_key=key, old_value=old, new_value=new)
        for key, old, new in changes
    )
    await db.commit()


@pytest_asyncio.fixture
async def world(db_session: AsyncSession, auth_client: AsyncClient) -> dict[str, Any]:
    """An issue the reader may see whose history names its own files, a gone file and another project's file."""
    reader = auth_client.state.user
    lookups = await _lookups(db_session, "AHN")
    mine, theirs = await _project(db_session, "AHNA"), await _project(db_session, "AHNB")
    await _join(db_session, reader, mine, "AHN Reader")

    issue = await _issue(db_session, mine, lookups, reader)
    hidden = await _issue(db_session, theirs, lookups, reader)
    own = await _attachment(db_session, "Issue", issue.id, "diagram (2).png", reader)
    secret = await _attachment(db_session, "Issue", hidden.id, "secret-plan.pdf", reader)
    await _history(
        db_session,
        issue,
        reader,
        [
            # Imported under one name, renamed on import: the current name wins.
            (str(own), None, "diagram.png"),
            # The file is gone: the stored name is all there is.
            ("987654321", "report.pdf", None),
            # Another project's file: never named, the stored value stands.
            (str(secret), None, "notes.txt"),
            # An import that could not resolve its file.
            ("", None, "legacy.log"),
        ],
    )
    clear_role_cache(db_session)
    return {"issue": issue, "reader": reader, "own": own, "secret": secret}


async def test_api_names_files_by_their_name(auth_client: AsyncClient, world: dict[str, Any]) -> None:
    resp = await auth_client.get(f"/api/v1/issues/{world['issue'].display_key}/?include=journals")
    assert resp.status_code == 200, resp.text
    details = [d for j in resp.json()["journals"] for d in j["details"] if d["property"] == "attachment"]
    assert {(d["prop_key"], d["old_name"], d["new_name"]) for d in details} == {
        (str(world["own"]), None, "diagram (2).png"),
        ("987654321", "report.pdf", None),
        (str(world["secret"]), None, "notes.txt"),
        ("", None, "legacy.log"),
    }
    assert "secret-plan.pdf" not in resp.text


@pytest.mark.parametrize("partial", [False, True], ids=["page", "partial"])
async def test_issue_page_reads_file_added_and_deleted(
    auth_client: AsyncClient, world: dict[str, Any], partial: bool
) -> None:
    key = world["issue"].display_key
    resp = await auth_client.get(f"/partials/issues/{key}/activity/" if partial else f"/issue/{key}/")
    assert resp.status_code == 200, resp.text
    text = " ".join(resp.text.split())
    for line in (
        "File diagram (2).png added",
        "File report.pdf deleted",
        "File notes.txt added",
        "File legacy.log added",
    ):
        assert line in text, line
    assert "secret-plan.pdf" not in text
    # Never the generic "changed from … to …" form with the id as the label.
    assert f'<strong class="sp-change-label">{world["own"]}</strong>' not in text
    assert "987654321" not in text


async def test_mcp_change_lines_name_the_file(db_session: AsyncSession, world: dict[str, Any]) -> None:
    journals = list(
        (
            await db_session.execute(
                select(Journal).where(Journal.issue_id == world["issue"].id).options(selectinload(Journal.details))
            )
        ).scalars()
    )
    names = await activity_lookup_maps(db_session, world["reader"], journals, people_by_login=False)
    lines = {_change_line(d, names) for j in journals for d in j.details}
    assert lines == {
        "file diagram (2).png added",
        "file report.pdf deleted",
        "file notes.txt added",
        "file legacy.log added",
    }


async def test_file_changes_without_a_name_still_read(db_session: AsyncSession, auth_client: AsyncClient) -> None:
    reader = auth_client.state.user
    project = await _project(db_session, "AHNE")
    await _join(db_session, reader, project, "AHNE Reader")
    issue = await _issue(db_session, project, await _lookups(db_session, "AHNE"), reader)
    await _history(db_session, issue, reader, [("", "", None), ("", None, None)])
    clear_role_cache(db_session)

    resp = await auth_client.get(f"/issue/{issue.display_key}/")
    assert resp.status_code == 200, resp.text
    text = " ".join(resp.text.split())
    assert "File deleted" in text and "File added" in text


async def test_visitor_without_an_account_is_shown_no_file_changes(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = AdminUserFactory.build(login="ahn_admin", status="active")
    db_session.add(admin)
    await db_session.flush()
    public = await _project(db_session, "AHNPUB", is_public=True)
    issue = await _issue(db_session, public, await _lookups(db_session, "AHNP"), admin)
    attachment_id = await _attachment(db_session, "Issue", issue.id, "public-secret.txt", admin)
    await _history(db_session, issue, admin, [(str(attachment_id), None, "public-secret.txt"), ("5", "old.txt", None)])
    await set_anonymous_permissions(db_session, public, [Permission.VIEW_ISSUES], admin)
    await set_anonymous_access_enabled(db_session, True, admin, confirmed_projects=[public.key])
    await db_session.commit()
    clear_role_cache(db_session)

    resp = await client.get(f"/api/v1/issues/{issue.display_key}/?include=journals")
    assert resp.status_code == 200, resp.text
    assert [d for j in resp.json()["journals"] for d in j["details"] if d["property"] == "attachment"] == []
    assert "public-secret.txt" not in resp.text and "old.txt" not in resp.text

    page = await client.get(f"/issue/{issue.display_key}/")
    assert page.status_code == 200, page.text
    assert "public-secret.txt" not in page.text and "old.txt" not in page.text
    text = " ".join(page.text.split())
    assert "File added" not in text and "File deleted" not in text


async def test_deleting_a_file_records_it_under_its_name(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    admin = admin_client.state.user
    project = await _project(db_session, "AHND")
    issue = await _issue(db_session, project, await _lookups(db_session, "AHND"), admin)
    await db_session.commit()

    upload = await admin_client.post(
        "/api/v1/attachments/",
        files={"file": ("quarterly report.txt", io.BytesIO(b"numbers"), "text/plain")},
        data={"container_type": "Issue", "container_id": str(issue.id)},
    )
    assert upload.status_code == 201, upload.text
    attachment_id = upload.json()["id"]

    resp = await admin_client.delete(f"/api/v1/attachments/{attachment_id}/")
    assert resp.status_code == 204, resp.text

    rows = (
        await db_session.execute(
            select(JournalDetail.prop_key, JournalDetail.old_value, JournalDetail.new_value)
            .join(Journal, Journal.id == JournalDetail.journal_id)
            .where(Journal.issue_id == issue.id, JournalDetail.property == ATTACHMENT_DETAIL)
            .order_by(JournalDetail.id)
        )
    ).all()
    # The upload is recorded too (see test_attachment_upload_history).
    assert [tuple(row) for row in rows] == [
        (str(attachment_id), None, "quarterly report.txt"),
        (str(attachment_id), "quarterly report.txt", None),
    ]

    history = await admin_client.get(f"/api/v1/issues/{issue.display_key}/?include=journals")
    details = [d for j in history.json()["journals"] for d in j["details"] if d["property"] == "attachment"]
    assert sorted((d["old_name"] or "", d["new_name"] or "") for d in details) == [
        ("", "quarterly report.txt"),
        ("quarterly report.txt", ""),
    ]

    page = await admin_client.get(f"/issue/{issue.display_key}/")
    assert "File quarterly report.txt deleted" in " ".join(page.text.split())
