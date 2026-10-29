"""Uploading a file to an issue records it in the issue's history.

The detail is ``attachment`` with the attachment id as ``prop_key`` and the
file name as ``new_value``, the mirror of a deletion. An upload sent with the
uploader's own edit or comment (``journal_id``) joins that entry, and so do
the later files of a batch that pass the ``journal_id`` the first upload
returned; otherwise each upload gets an entry of its own, and so does one
sent with a private entry, since the file is not private. The check runs under
the per-issue journal lock. Files attached to a wiki page are not issue
history. A visitor without an account is shown no
file changes.
"""

from __future__ import annotations

import io

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from specivo.models.issue import Issue
from specivo.models.journal import ATTACHMENT_DETAIL, Journal, JournalDetail
from specivo.models.project import EnabledModule, Project
from specivo.models.user import User
from specivo.services.anonymous_access_service import set_anonymous_access_enabled, set_anonymous_permissions
from specivo.services.journal_service import JournalService
from specivo.services.permission_service import Permission, clear_role_cache
from specivo.services.wiki_service import WikiService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


async def _issue(db: AsyncSession, key: str, author: User, *, is_public: bool = False, number: int = 1) -> Issue:
    project = (await db.execute(select(Project).where(Project.key == key))).scalar_one_or_none()
    if project is None:
        project = ProjectFactory.build(key=key, identifier=key.lower(), name=f"Project {key}", is_public=is_public)
        db.add(project)
        await db.flush()
        db.add_all([EnabledModule(project_id=project.id, name=m) for m in ("issue_tracking", "wiki")])
        await db.flush()
    status = StatusFactory.build(name=f"{key} New {number}", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name=f"{key} Bug {number}", default_status_id=status.id)
    priority = PriorityFactory.build(name=f"{key} Normal {number}", position=1)
    db.add_all([tracker, priority])
    await db.flush()
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=number,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=author.id,
        subject=f"{key} issue {number}",
    )
    db.add(issue)
    await db.commit()
    return issue


async def _upload(
    client: AsyncClient, container_type: str, container_id: int, name: str, journal_id: int | None = None
) -> dict:
    data = {"container_type": container_type, "container_id": str(container_id)}
    if journal_id is not None:
        data["journal_id"] = str(journal_id)
    resp = await client.post(
        "/api/v1/attachments/", files={"file": (name, io.BytesIO(name.encode()), "text/plain")}, data=data
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _file_changes(db: AsyncSession, issue: Issue) -> list[tuple[int, str, str | None, str | None]]:
    rows = await db.execute(
        select(Journal.id, JournalDetail.prop_key, JournalDetail.old_value, JournalDetail.new_value)
        .join(JournalDetail, JournalDetail.journal_id == Journal.id)
        .where(Journal.issue_id == issue.id, JournalDetail.property == ATTACHMENT_DETAIL)
        .order_by(JournalDetail.id)
    )
    return [tuple(row) for row in rows.all()]


async def _journal_count(db: AsyncSession, issue: Issue) -> int:
    return (await db.execute(select(func.count()).where(Journal.issue_id == issue.id))).scalar_one()


async def test_an_upload_on_its_own_gets_an_entry(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    issue = await _issue(db_session, "AUHA", admin_client.state.user)

    uploaded = await _upload(admin_client, "Issue", issue.id, "build log.txt")

    changes = await _file_changes(db_session, issue)
    assert changes == [(uploaded["journal_id"], str(uploaded["id"]), None, "build log.txt")]
    journal = await db_session.get(Journal, uploaded["journal_id"])
    assert journal.notes is None and journal.user_id == admin_client.state.user.id

    page = await admin_client.get(f"/issue/{issue.display_key}/")
    assert "File build log.txt added" in " ".join(page.text.split())


async def test_a_batch_shares_one_entry(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    issue = await _issue(db_session, "AUHB", admin_client.state.user)

    first = await _upload(admin_client, "Issue", issue.id, "one.txt")
    second = await _upload(admin_client, "Issue", issue.id, "two.txt", journal_id=first["journal_id"])

    assert second["journal_id"] == first["journal_id"]
    assert await _journal_count(db_session, issue) == 1
    assert [c[2:] for c in await _file_changes(db_session, issue)] == [(None, "one.txt"), (None, "two.txt")]


async def test_an_upload_with_a_comment_joins_the_comment(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    issue = await _issue(db_session, "AUHC", admin_client.state.user)
    resp = await admin_client.post(f"/api/v1/issues/{issue.display_key}/journals/", json={"notes": "Logs attached."})
    assert resp.status_code == 201, resp.text
    comment_id = resp.json()["id"]

    uploaded = await _upload(admin_client, "Issue", issue.id, "trace.txt", journal_id=comment_id)

    assert uploaded["journal_id"] == comment_id
    assert await _journal_count(db_session, issue) == 1
    assert await _file_changes(db_session, issue) == [(comment_id, str(uploaded["id"]), None, "trace.txt")]


async def test_an_upload_does_not_join_an_entry_once_another_followed(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    issue = await _issue(db_session, "AUHD", admin_client.state.user)
    first = await _upload(admin_client, "Issue", issue.id, "early.txt")
    resp = await admin_client.post(f"/api/v1/issues/{issue.display_key}/journals/", json={"notes": "In between."})
    assert resp.status_code == 201

    late = await _upload(admin_client, "Issue", issue.id, "late.txt", journal_id=first["journal_id"])

    assert late["journal_id"] not in (first["journal_id"], resp.json()["id"])
    assert await _journal_count(db_session, issue) == 3


async def test_an_upload_cannot_join_another_issues_or_another_users_entry(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = admin_client.state.user
    issue = await _issue(db_session, "AUHE", admin)
    other_issue = await _issue(db_session, "AUHE", admin, number=2)
    other = await _upload(admin_client, "Issue", other_issue.id, "elsewhere.txt")

    somebody = UserFactory.build(login="auhe_somebody", status="active")
    db_session.add(somebody)
    await db_session.flush()
    theirs = Journal(issue_id=issue.id, project_id=issue.project_id, user_id=somebody.id, notes="Mine.", sequence=1)
    db_session.add(theirs)
    await db_session.commit()

    for journal_id in (other["journal_id"], theirs.id, 987654321):
        resp = await admin_client.post(
            "/api/v1/attachments/",
            files={"file": ("x.txt", io.BytesIO(b"x"), "text/plain")},
            data={"container_type": "Issue", "container_id": str(issue.id), "journal_id": str(journal_id)},
        )
        assert resp.status_code == 422, resp.text
    assert await _file_changes(db_session, issue) == []


async def test_a_wiki_upload_is_not_issue_history(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    admin = admin_client.state.user
    issue = await _issue(db_session, "AUHW", admin)
    page, _content = await WikiService().create_page(db_session, issue.project_id, "Runbook", "Steps.", admin)
    await db_session.commit()
    before = (await db_session.execute(select(func.count()).select_from(Journal))).scalar_one()

    uploaded = await _upload(admin_client, "WikiPage", page.id, "diagram.txt")

    assert uploaded["journal_id"] is None
    after = (await db_session.execute(select(func.count()).select_from(Journal))).scalar_one()
    assert after == before


async def test_a_visitor_without_an_account_sees_no_upload(
    admin_client: AsyncClient, client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = admin_client.state.user
    issue = await _issue(db_session, "AUHP", admin, is_public=True)
    project = await db_session.get(Project, issue.project_id)
    await set_anonymous_permissions(db_session, project, [Permission.VIEW_ISSUES], admin)
    await set_anonymous_access_enabled(db_session, True, admin, confirmed_projects=[project.key])
    await db_session.commit()
    clear_role_cache(db_session)

    await _upload(admin_client, "Issue", issue.id, "public-upload.txt")
    assert len(await _file_changes(db_session, issue)) == 1

    resp = await client.get(f"/api/v1/issues/{issue.display_key}/?include=journals")
    assert resp.status_code == 200, resp.text
    assert [d for j in resp.json()["journals"] for d in j["details"] if d["property"] == "attachment"] == []
    assert "public-upload.txt" not in resp.text
    page = await client.get(f"/issue/{issue.display_key}/")
    assert page.status_code == 200
    assert "public-upload.txt" not in page.text and "File added" not in " ".join(page.text.split())


async def test_an_upload_with_a_private_entry_gets_its_own_public_entry(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The file is visible to every reader of the issue, so its history entry must be too."""
    admin = admin_client.state.user
    issue = await _issue(db_session, "AUHV", admin)
    private = Journal(
        issue_id=issue.id, project_id=issue.project_id, user_id=admin.id, notes="Internal.", is_private=True, sequence=1
    )
    db_session.add(private)
    await db_session.commit()

    uploaded = await _upload(admin_client, "Issue", issue.id, "shared.txt", journal_id=private.id)

    assert uploaded["journal_id"] != private.id
    entry = await db_session.get(Journal, uploaded["journal_id"], populate_existing=True)
    assert entry.is_private is False
    assert await _file_changes(db_session, issue) == [(entry.id, str(uploaded["id"]), None, "shared.txt")]


async def test_joining_an_entry_takes_the_issue_lock_before_reading_it(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    author = UserFactory.build(login="auhl_author", status="active")
    db_session.add(author)
    await db_session.flush()
    issue = await _issue(db_session, "AUHL", author)
    entry = Journal(issue_id=issue.id, project_id=issue.project_id, user_id=author.id, notes="Logs.", sequence=1)
    db_session.add(entry)
    await db_session.commit()

    calls: list[str] = []
    svc = JournalService()
    real_lock, real_get, real_scalar = svc._lock_issue, db_session.get, db_session.scalar

    async def lock(session, issue_id):
        calls.append("lock")
        await real_lock(session, issue_id)

    async def get(*args, **kwargs):
        calls.append("read entry")
        return await real_get(*args, **kwargs)

    async def scalar(*args, **kwargs):
        calls.append("read latest")
        return await real_scalar(*args, **kwargs)

    monkeypatch.setattr(svc, "_lock_issue", lock)
    monkeypatch.setattr(db_session, "get", get)
    monkeypatch.setattr(db_session, "scalar", scalar)

    assert await svc.upload_journal(db_session, issue, author, entry.id) is entry
    assert calls == ["lock", "read entry", "read latest"]


async def test_another_transaction_cannot_add_an_entry_while_an_upload_joins_one(
    db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    """Until the upload's transaction ends, the per-issue journal lock is held."""
    author = UserFactory.build(login="auhr_author", status="active")
    db_session.add(author)
    await db_session.flush()
    issue = await _issue(db_session, "AUHR", author)
    entry = Journal(issue_id=issue.id, project_id=issue.project_id, user_id=author.id, notes="Logs.", sequence=1)
    db_session.add(entry)
    await db_session.commit()

    async with db_engine.connect() as other:
        free = text("SELECT pg_try_advisory_xact_lock(:issue_id)")
        assert (await other.execute(free, {"issue_id": issue.id})).scalar_one() is True
        await other.rollback()

        assert await JournalService().upload_journal(db_session, issue, author, entry.id) is entry

        assert (await other.execute(free, {"issue_id": issue.id})).scalar_one() is False
        await other.rollback()
