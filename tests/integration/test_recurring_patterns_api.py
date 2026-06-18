"""Integration tests for the recurring patterns API.

Covers:
- create (201) with a valid weekly pattern; bad timezone / incoherent rule -> 422
- list, detail (200); detail of another project's pattern -> 404
- update (PATCH); delete (204) then detail -> 404
- permission enforcement: view_issues-only member can read but not mutate (403);
  non-member -> 404 (require_project_access first)
- occurrences preview returns UTC datetimes and reflects a skip exception
- skip / override / split endpoints succeed and have the expected effect
"""

from __future__ import annotations

import datetime
import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, login: str, password: str = "testpassword") -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _grant_membership(
    db: AsyncSession, project: Project, user: User, role: Role
) -> None:
    member = Member(project_id=project.id, user_id=user.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()


def _weekly_payload(tracker_id: int, **overrides) -> dict:
    """A valid weekly-on-Monday pattern body."""
    payload = {
        "name": "Weekly standup notes",
        "template_tracker_id": tracker_id,
        "template_subject": "Standup notes",
        "freq": "weekly",
        "rrule_interval": 1,
        "byday": ["MO"],
        "dtstart": "2026-01-05T09:00:00+00:00",  # a Monday
        "timezone": "UTC",
        "creation_lead_time_days": 60,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="rec_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="REC", identifier="rec-project", is_public=False)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def other_project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="REO", identifier="rec-other", is_public=False)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def status_open(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, status_open: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Task", default_status_id=status_open.id)
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
async def viewer_role(db_session: AsyncSession) -> Role:
    """A role that can view issues but not manage recurring tasks."""
    role = Role(
        name=f"RecViewer-{uuid.uuid4().hex[:8]}",
        position=3,
        assignable=True,
        builtin=0,
        permissions=["view_issues"],
        issues_visibility="default",
        settings={},
    )
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def viewer_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="rec_viewer", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def viewer_token(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
    viewer_user: User,
    viewer_role: Role,
) -> str:
    await _grant_membership(db_session, project, viewer_user, viewer_role)
    return await _login(client, viewer_user.login)


async def _create_pattern(
    client: AsyncClient, project: Project, admin_token: str, tracker_id: int, **overrides
) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project.key}/recurring-patterns/",
        json=_weekly_payload(tracker_id, **overrides),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_weekly_pattern(
    client: AsyncClient, db_session: AsyncSession, admin_token: str, project: Project, tracker: Tracker
) -> None:
    data = await _create_pattern(client, project, admin_token, tracker.id)
    assert data["name"] == "Weekly standup notes"
    assert data["freq"] == "weekly"
    assert data["byday"] == ["MO"]
    assert data["project_key"] == project.key
    assert data["enabled"] is True
    assert data["anchor_mode"] == "fixed"
    assert "id" in data
    assert "created_at" in data
    assert "lock_version" in data


@pytest.mark.asyncio
async def test_create_with_naive_dtstart_anchors_to_timezone(
    client: AsyncClient, db_session: AsyncSession, admin_token: str, project: Project, tracker: Tracker
) -> None:
    """A naive ``dtstart`` (as an HTML ``datetime-local`` input sends) plus a
    separate ``timezone`` must be anchored to that timezone.

    The recurrence engine requires a timezone-aware anchor; a naive value used
    to fail with "spec.dtstart must be timezone-aware". The service interprets
    the naive wall-clock in the supplied IANA ``timezone``.
    """
    resp = await client.post(
        f"/api/v1/projects/{project.key}/recurring-patterns/",
        json=_weekly_payload(
            tracker.id,
            freq="daily",
            byday=None,
            dtstart="2026-06-18T14:15:00",  # naive — no offset, like datetime-local
            timezone="Asia/Bangkok",
        ),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    stored = datetime.datetime.fromisoformat(resp.json()["dtstart"])
    assert stored.tzinfo is not None, "dtstart must be stored timezone-aware"
    # 14:15 wall-clock in Asia/Bangkok (UTC+7) is the instant 07:15 UTC.
    assert stored.astimezone(datetime.UTC) == datetime.datetime(
        2026, 6, 18, 7, 15, tzinfo=datetime.UTC
    )


@pytest.mark.asyncio
async def test_create_bad_timezone_rejected(
    client: AsyncClient, db_session: AsyncSession, admin_token: str, project: Project, tracker: Tracker
) -> None:
    resp = await client.post(
        f"/api/v1/projects/{project.key}/recurring-patterns/",
        json=_weekly_payload(tracker.id, timezone="Mars/Phobos"),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_incoherent_rule_rejected(
    client: AsyncClient, db_session: AsyncSession, admin_token: str, project: Project, tracker: Tracker
) -> None:
    """count and until are mutually exclusive -> service ValidationError (400)."""
    resp = await client.post(
        f"/api/v1/projects/{project.key}/recurring-patterns/",
        json=_weekly_payload(
            tracker.id,
            rrule_count=5,
            until="2026-12-31T09:00:00+00:00",
        ),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code in (400, 422), resp.text


@pytest.mark.asyncio
async def test_list_and_detail(
    client: AsyncClient, db_session: AsyncSession, admin_token: str, project: Project, tracker: Tracker
) -> None:
    created = await _create_pattern(client, project, admin_token, tracker.id)

    list_resp = await client.get(
        f"/api/v1/projects/{project.key}/recurring-patterns/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert list_resp.status_code == 200, list_resp.text
    ids = [p["id"] for p in list_resp.json()]
    assert created["id"] in ids

    detail_resp = await client.get(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert detail_resp.status_code == 200, detail_resp.text
    assert detail_resp.json()["id"] == created["id"]


@pytest.mark.asyncio
async def test_detail_other_project_is_404(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    other_project: Project,
    tracker: Tracker,
) -> None:
    created = await _create_pattern(client, project, admin_token, tracker.id)
    resp = await client.get(
        f"/api/v1/projects/{other_project.key}/recurring-patterns/{created['id']}/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_update_pattern(
    client: AsyncClient, db_session: AsyncSession, admin_token: str, project: Project, tracker: Tracker
) -> None:
    created = await _create_pattern(client, project, admin_token, tracker.id)
    resp = await client.patch(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/",
        json={
            "name": "Renamed",
            "enabled": False,
            "rrule_interval": 2,
            "lock_version": created["lock_version"],
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "Renamed"
    assert data["enabled"] is False
    assert data["rrule_interval"] == 2


@pytest.mark.asyncio
async def test_update_requires_lock_version(
    client: AsyncClient, db_session: AsyncSession, admin_token: str, project: Project, tracker: Tracker
) -> None:
    """A PATCH without ``lock_version`` is rejected at validation (422)."""
    created = await _create_pattern(client, project, admin_token, tracker.id)
    resp = await client.patch(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/",
        json={"name": "No lock"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_update_stale_lock_version_conflict(
    client: AsyncClient, db_session: AsyncSession, admin_token: str, project: Project, tracker: Tracker
) -> None:
    """A concurrent edit with a stale ``lock_version`` is rejected with 409.

    The first writer succeeds and bumps the version; the second writer, still
    holding the original version, must get a 409 Conflict instead of silently
    overwriting the first change.
    """
    created = await _create_pattern(client, project, admin_token, tracker.id)
    stale_version = created["lock_version"]
    headers = {"Authorization": f"Bearer {admin_token}"}

    # First writer wins and bumps the lock_version.
    first = await client.patch(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/",
        json={"name": "First writer", "lock_version": stale_version},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert first.json()["lock_version"] != stale_version

    # Second writer holds the now-stale version -> 409 Conflict.
    second = await client.patch(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/",
        json={"name": "Second writer", "lock_version": stale_version},
        headers=headers,
    )
    assert second.status_code == 409, second.text

    # The first writer's change is intact.
    detail = await client.get(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/",
        headers=headers,
    )
    assert detail.json()["name"] == "First writer"


@pytest.mark.asyncio
async def test_delete_pattern(
    client: AsyncClient, db_session: AsyncSession, admin_token: str, project: Project, tracker: Tracker
) -> None:
    created = await _create_pattern(client, project, admin_token, tracker.id)
    resp = await client.delete(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 204, resp.text

    detail = await client.get(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert detail.status_code == 404


@pytest.mark.asyncio
async def test_create_requires_auth(
    client: AsyncClient, db_session: AsyncSession, project: Project, tracker: Tracker
) -> None:
    resp = await client.post(
        f"/api/v1/projects/{project.key}/recurring-patterns/",
        json=_weekly_payload(tracker.id),
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Permission enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_viewer_can_read_but_not_mutate(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    viewer_token: str,
    project: Project,
    tracker: Tracker,
) -> None:
    """A view_issues-only member reads (200) but gets 403 on every mutation."""
    created = await _create_pattern(client, project, admin_token, tracker.id)
    viewer_headers = {"Authorization": f"Bearer {viewer_token}"}

    # Reads succeed.
    assert (
        await client.get(
            f"/api/v1/projects/{project.key}/recurring-patterns/",
            headers=viewer_headers,
        )
    ).status_code == 200
    assert (
        await client.get(
            f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/",
            headers=viewer_headers,
        )
    ).status_code == 200
    assert (
        await client.get(
            f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/occurrences/",
            headers=viewer_headers,
        )
    ).status_code == 200

    # Mutations are forbidden.
    create_resp = await client.post(
        f"/api/v1/projects/{project.key}/recurring-patterns/",
        json=_weekly_payload(tracker.id),
        headers=viewer_headers,
    )
    assert create_resp.status_code == 403, create_resp.text

    update_resp = await client.patch(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/",
        json={"name": "nope", "lock_version": created["lock_version"]},
        headers=viewer_headers,
    )
    assert update_resp.status_code == 403

    delete_resp = await client.delete(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/",
        headers=viewer_headers,
    )
    assert delete_resp.status_code == 403

    skip_resp = await client.post(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/skip/",
        json={"occurrence_at": "2026-01-12T09:00:00+00:00"},
        headers=viewer_headers,
    )
    assert skip_resp.status_code == 403

    override_resp = await client.post(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/override/",
        json={"occurrence_at": "2026-01-12T09:00:00+00:00", "payload": {"subject": "x"}},
        headers=viewer_headers,
    )
    assert override_resp.status_code == 403

    split_resp = await client.post(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/split/",
        json={
            "occurrence_at": "2026-02-02T09:00:00+00:00",
            "new_pattern": _weekly_payload(tracker.id),
        },
        headers=viewer_headers,
    )
    assert split_resp.status_code == 403


@pytest.mark.asyncio
async def test_non_member_gets_404(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
) -> None:
    """A non-member on a private project gets 404 (enumeration guard)."""
    created = await _create_pattern(client, project, admin_token, tracker.id)
    outsider = UserFactory.build(login="rec_outsider", status="active")
    db_session.add(outsider)
    await db_session.commit()
    token = await _login(client, outsider.login)

    resp = await client.get(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Occurrences preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_occurrences_preview(
    client: AsyncClient, db_session: AsyncSession, admin_token: str, project: Project, tracker: Tracker
) -> None:
    """Preview returns upcoming UTC datetimes within the look-ahead window."""
    # A daily pattern anchored well in the past so occurrences land from "now".
    created = await _create_pattern(
        client,
        project,
        admin_token,
        tracker.id,
        freq="daily",
        byday=None,
        dtstart="2020-01-01T09:00:00+00:00",
        creation_lead_time_days=10,
    )
    resp = await client.get(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/occurrences/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == len(body["occurrences"])
    assert body["count"] >= 1
    # All occurrences are tz-aware UTC datetimes >= now.
    now = datetime.datetime.now(datetime.UTC)
    for raw in body["occurrences"]:
        occ = datetime.datetime.fromisoformat(raw)
        assert occ.tzinfo is not None
        assert occ >= now - datetime.timedelta(seconds=5)


@pytest.mark.asyncio
async def test_occurrences_preview_reflects_skip(
    client: AsyncClient, db_session: AsyncSession, admin_token: str, project: Project, tracker: Tracker
) -> None:
    """A skipped occurrence is absent from the preview."""
    created = await _create_pattern(
        client,
        project,
        admin_token,
        tracker.id,
        freq="daily",
        byday=None,
        dtstart="2020-01-01T09:00:00+00:00",
        creation_lead_time_days=10,
    )
    headers = {"Authorization": f"Bearer {admin_token}"}

    before = await client.get(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/occurrences/",
        headers=headers,
    )
    assert before.status_code == 200
    occurrences = before.json()["occurrences"]
    assert occurrences, "expected at least one occurrence to skip"
    target = occurrences[0]

    skip_resp = await client.post(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/skip/",
        json={"occurrence_at": target},
        headers=headers,
    )
    assert skip_resp.status_code == 200, skip_resp.text
    assert skip_resp.json()["kind"] == "skip"

    after = await client.get(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/occurrences/",
        headers=headers,
    )
    assert after.status_code == 200
    after_occ = after.json()["occurrences"]
    # The skipped instant must be gone.
    skipped = datetime.datetime.fromisoformat(target)
    remaining = {datetime.datetime.fromisoformat(o) for o in after_occ}
    assert skipped not in remaining


# ---------------------------------------------------------------------------
# Edit-scope: override and split
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_occurrence(
    client: AsyncClient, db_session: AsyncSession, admin_token: str, project: Project, tracker: Tracker
) -> None:
    created = await _create_pattern(client, project, admin_token, tracker.id)
    resp = await client.post(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/override/",
        json={
            "occurrence_at": "2026-01-12T09:00:00+00:00",
            "payload": {"subject": "Overridden subject"},
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "override"
    assert body["override_payload"]["subject"] == "Overridden subject"


@pytest.mark.asyncio
async def test_split_creates_new_pattern_and_terminates_old(
    client: AsyncClient, db_session: AsyncSession, admin_token: str, project: Project, tracker: Tracker
) -> None:
    created = await _create_pattern(client, project, admin_token, tracker.id)
    headers = {"Authorization": f"Bearer {admin_token}"}
    boundary = "2026-02-02T09:00:00+00:00"  # a later Monday

    resp = await client.post(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/split/",
        json={
            "occurrence_at": boundary,
            "new_pattern": _weekly_payload(tracker.id, name="Split future"),
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    new_pattern = resp.json()
    assert new_pattern["id"] != created["id"]
    assert new_pattern["name"] == "Split future"
    # The new series is anchored at the boundary occurrence.
    assert datetime.datetime.fromisoformat(new_pattern["dtstart"]) == datetime.datetime.fromisoformat(
        boundary
    )

    # The old series now has an UNTIL set just before the boundary.
    old = await client.get(
        f"/api/v1/projects/{project.key}/recurring-patterns/{created['id']}/",
        headers=headers,
    )
    assert old.status_code == 200
    assert old.json()["until"] is not None
