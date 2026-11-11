"""A bulk update refused for one issue leaves nothing of it behind.

IssueService.update applies fields one by one and can refuse late (a version
or category of another project). Without a savepoint the fields it had already
set on the refused issue stayed in the session and were written, without a
journal entry, when the next issue of the batch was flushed.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.project import EnabledModule
from specivo.models.version import Version
from specivo.services.auth_service import _make_access_token
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


async def test_refused_issue_keeps_none_of_the_batch_changes(client: AsyncClient, db_session: AsyncSession) -> None:
    db = db_session
    status = StatusFactory.build(name="BUR New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="BUR Task", default_status_id=status.id)
    normal = PriorityFactory.build(name="BUR Normal", position=1, is_default=True)
    high = PriorityFactory.build(name="BUR High", position=2)
    db.add_all([tracker, normal, high])
    await db.flush()
    a = ProjectFactory.build(key="BURA", identifier="bur-a")
    b = ProjectFactory.build(key="BURB", identifier="bur-b")
    db.add_all([a, b])
    await db.flush()
    db.add_all([EnabledModule(project_id=p.id, name="issue_tracking") for p in (a, b)])
    admin = AdminUserFactory.build(login="bur_admin", status="active")
    db.add(admin)
    version = Version(project_id=a.id, name="1.0", status="open")
    db.add(version)
    await db.flush()

    def issue(project, seq: int) -> Issue:
        return IssueFactory.build(
            project_id=project.id,
            project_key=project.key,
            sequence_number=seq,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=normal.id,
            author_id=admin.id,
            subject=f"{project.key} issue",
        )

    refused, accepted = issue(b, 1), issue(a, 1)
    db.add_all([refused, accepted])
    await db.commit()

    resp = await client.post(
        "/api/v1/issues/bulk-update/",
        # The refused issue comes first, so the accepted one is flushed after it.
        json={
            "issue_ids": [refused.id, accepted.id],
            "updates": {"priority_id": high.id, "fixed_version_id": version.id},
        },
        headers={"Authorization": f"Bearer {_make_access_token(admin, get_settings())}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [item["id"] for item in body["failed"]] == [refused.id]
    assert body["failed"][0]["key"] == "BURB-1"
    assert [item["id"] for item in body["succeeded"]] == [accepted.id]

    rows = {
        row.id: row
        for row in (
            await db.execute(
                select(Issue).where(Issue.id.in_([refused.id, accepted.id])).execution_options(populate_existing=True)
            )
        ).scalars()
    }
    assert rows[accepted.id].priority_id == high.id
    assert rows[accepted.id].fixed_version_id == version.id
    assert rows[refused.id].priority_id == normal.id
    assert rows[refused.id].fixed_version_id is None
    assert await db.scalar(select(Journal.id).where(Journal.issue_id == refused.id)) is None
