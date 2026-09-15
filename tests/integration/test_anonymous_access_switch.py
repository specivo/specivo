"""The instance-wide anonymous access switch: ``anonymous_access_enabled``.

Covers that the switch is off on a fresh instance and whenever the setting is
absent or holds anything other than ``"true"``, that a change applies to the
next request, that only instance administrators can read or change it, that
turning it on must confirm exactly the projects opted in at that moment (a
stale list is refused) while turning it off needs no confirmation, that the
switch and per-project opt-ins share one transaction-scoped lock, that every
change is audited, and that the generic settings endpoint cannot bypass any of
that.

The lock tests check from a second connection that each write path holds the
lock until its transaction ends. They are ``serial``: the lock is global to the
database, so a test on another xdist worker holding it would skew the check. Two requests genuinely racing cannot be
staged here: each test's data lives in a transaction that is never committed,
so a second connection could not see the projects it would race over.

Whether the switch changes what anybody can see is covered in
``test_anonymous_access_inert.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import specivo.importers
from specivo.core.config import get_settings
from specivo.core.exceptions import PermissionDeniedError
from specivo.models.project import Project
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.setting import Setting
from specivo.models.user import User
from specivo.schemas.project import ProjectUpdate
from specivo.services.anonymous_access_service import (
    ANONYMOUS_ACCESS_LOCK_KEY,
    ANONYMOUS_ACCESS_SETTING_KEY,
    is_anonymous_access_enabled,
    set_anonymous_access_enabled,
    set_anonymous_permissions,
)
from specivo.services.auth_service import _make_access_token
from specivo.services.project_service import ProjectService
from specivo.services.security_audit_service import AuditEvent
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

URL = "/api/v1/admin/settings/anonymous-access/"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="anonsw_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def regular(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="anonsw_regular", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def projects(db_session: AsyncSession, admin: User) -> dict[str, Project]:
    """Two opted-in public projects, one public project not opted in, one private project."""
    specs = [
        ("SWONE", "switch-one", "Switch One", True),
        ("SWTWO", "switch-two", "Switch Two", True),
        ("SWPUB", "switch-public", "Switch Public", True),
        ("SWPRIV", "switch-private", "Switch Private", False),
    ]
    built = {}
    for key, identifier, name, is_public in specs:
        proj = ProjectFactory.build(key=key, identifier=identifier, name=name, is_public=is_public)
        db_session.add(proj)
        built[key] = proj
    await db_session.flush()
    await set_anonymous_permissions(db_session, built["SWONE"], ["view_issues"], admin)
    await set_anonymous_permissions(db_session, built["SWTWO"], ["view_issues", "view_wiki"], admin)
    await db_session.commit()
    return built


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


async def _set_raw(db: AsyncSession, value: str | None) -> None:
    await db.execute(text("DELETE FROM settings WHERE key = :k"), {"k": ANONYMOUS_ACCESS_SETTING_KEY})
    db.add(Setting(key=ANONYMOUS_ACCESS_SETTING_KEY, value=value))
    await db.commit()


async def _audit_rows(db: AsyncSession) -> list[SecurityAuditLog]:
    result = await db.execute(
        select(SecurityAuditLog)
        .where(SecurityAuditLog.event_type == AuditEvent.ANONYMOUS_ACCESS_SWITCH_CHANGED)
        .order_by(SecurityAuditLog.id)
    )
    return list(result.scalars().all())


async def _csrf(client: AsyncClient, user: User) -> str:
    resp = await client.get("/admin/settings/", cookies=_cookies(user))
    for key, value in resp.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith("csrf_token="):
            return value.split("=", 1)[1].split(";")[0].strip()
    raise AssertionError("no csrf_token cookie on the settings page")


async def _post_form(client: AsyncClient, user: User, data: dict[str, str | list[str]]):
    csrf = await _csrf(client, user)
    return await client.post(
        "/admin/settings/anonymous-access/",
        data={**data, "csrf_token": csrf},
        cookies={**_cookies(user), "csrf_token": csrf},
        headers={"x-csrf-token": csrf},
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Default and stored values
# ---------------------------------------------------------------------------


async def test_off_on_a_fresh_instance_with_no_setting_row(client: AsyncClient, db_session: AsyncSession, admin):
    rows = await db_session.scalar(
        select(func.count()).select_from(Setting).where(Setting.key == ANONYMOUS_ACCESS_SETTING_KEY)
    )

    resp = await client.get(URL, headers=_bearer(admin))

    assert rows == 0
    assert await is_anonymous_access_enabled(db_session) is False
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False


@pytest.mark.parametrize("value", [None, "", "false", "0", "1", "yes", "on", "TRUE", " true"])
async def test_anything_but_exactly_true_is_off(db_session: AsyncSession, value: str | None) -> None:
    await _set_raw(db_session, value)

    assert await is_anonymous_access_enabled(db_session) is False


async def test_exactly_true_is_on(db_session: AsyncSession) -> None:
    await _set_raw(db_session, "true")

    assert await is_anonymous_access_enabled(db_session) is True


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------


async def test_read_lists_exactly_the_opted_in_projects(client: AsyncClient, admin: User, projects) -> None:
    resp = await client.get(URL, headers=_bearer(admin))

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "enabled": False,
        "projects": [
            {"key": "SWONE", "name": "Switch One", "anonymous_permissions": ["view_issues"]},
            {"key": "SWTWO", "name": "Switch Two", "anonymous_permissions": ["view_issues", "view_wiki"]},
        ],
    }


async def test_turning_it_on_without_confirmation_names_exactly_the_opted_in_projects(
    client: AsyncClient, db_session: AsyncSession, admin: User, projects
) -> None:
    resp = await client.patch(URL, json={"enabled": True}, headers=_bearer(admin))

    assert resp.status_code == 409, resp.text
    error = resp.json()["errors"][0]
    assert error["code"] == "confirmation_required"
    assert [p["key"] for p in error["details"]["projects"]] == ["SWONE", "SWTWO"]
    assert await is_anonymous_access_enabled(db_session) is False
    assert await _audit_rows(db_session) == []


async def test_confirmation_is_required_even_with_no_opted_in_project(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    resp = await client.patch(URL, json={"enabled": True}, headers=_bearer(admin))

    assert resp.status_code == 409, resp.text
    assert resp.json()["errors"][0]["details"]["projects"] == []


async def test_confirmed_change_takes_effect_on_the_next_request_and_is_audited(
    client: AsyncClient, db_session: AsyncSession, admin: User, projects
) -> None:
    resp = await client.patch(
        URL, json={"enabled": True, "confirmed_projects": ["SWTWO", "swone"]}, headers=_bearer(admin)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True

    follow_up = await client.get(URL, headers=_bearer(admin))
    assert follow_up.json()["enabled"] is True
    assert await is_anonymous_access_enabled(db_session) is True

    rows = await _audit_rows(db_session)
    assert len(rows) == 1
    assert rows[0].user_id == admin.id
    assert rows[0].details == {
        "setting": "anonymous_access_enabled",
        "old": False,
        "new": True,
        "opted_in_projects": ["SWONE", "SWTWO"],
        "confirmed_projects": ["SWONE", "SWTWO"],
    }


async def test_turning_it_off_needs_no_confirmation_and_is_audited(
    client: AsyncClient, db_session: AsyncSession, admin: User, projects
) -> None:
    await set_anonymous_access_enabled(db_session, True, admin, confirmed_projects=["SWONE", "SWTWO"])
    await db_session.commit()

    resp = await client.patch(URL, json={"enabled": False}, headers=_bearer(admin))
    follow_up = await client.get(URL, headers=_bearer(admin))

    assert resp.status_code == 200, resp.text
    assert follow_up.json()["enabled"] is False
    rows = await _audit_rows(db_session)
    assert [(r.details["old"], r.details["new"]) for r in rows] == [(False, True), (True, False)]


async def test_saving_the_current_state_writes_nothing(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    resp = await client.patch(URL, json={"enabled": False}, headers=_bearer(admin))

    assert resp.status_code == 200, resp.text
    assert await _audit_rows(db_session) == []
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Setting).where(Setting.key == ANONYMOUS_ACCESS_SETTING_KEY)
        )
        == 0
    )


async def test_non_admin_can_neither_read_nor_change_it(
    client: AsyncClient, db_session: AsyncSession, regular: User
) -> None:
    await _set_raw(db_session, "true")

    responses = [
        await client.get(URL, headers=_bearer(regular)),
        await client.patch(URL, json={"enabled": False}, headers=_bearer(regular)),
        await client.get("/api/v1/admin/settings/", headers=_bearer(regular)),
        await client.patch(
            "/api/v1/admin/settings/", json={ANONYMOUS_ACCESS_SETTING_KEY: "false"}, headers=_bearer(regular)
        ),
    ]

    assert [r.status_code for r in responses] == [403, 403, 403, 403]
    assert await is_anonymous_access_enabled(db_session) is True


async def test_unauthenticated_request_is_refused(client: AsyncClient) -> None:
    assert (await client.get(URL)).status_code == 401
    assert (await client.patch(URL, json={"enabled": True, "confirmed_projects": []})).status_code == 401


async def test_service_refuses_a_non_admin(db_session: AsyncSession, regular: User) -> None:
    with pytest.raises(PermissionDeniedError):
        await set_anonymous_access_enabled(db_session, True, regular, confirmed_projects=[])


@pytest.mark.parametrize("value", ["true", "false", None])
async def test_generic_settings_endpoint_cannot_bypass_the_switch_endpoint(
    client: AsyncClient, db_session: AsyncSession, admin: User, value: str | None
) -> None:
    resp = await client.patch(
        "/api/v1/admin/settings/",
        json={"brand_name": "Lighthouse", ANONYMOUS_ACCESS_SETTING_KEY: value},
        headers=_bearer(admin),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["errors"][0]["code"] == "setting_managed_elsewhere"
    assert await is_anonymous_access_enabled(db_session) is False
    assert await _audit_rows(db_session) == []


async def test_the_importer_never_touches_the_switch() -> None:
    importer_root = Path(specivo.importers.__file__).parent
    names = (ANONYMOUS_ACCESS_SETTING_KEY, "ANONYMOUS_ACCESS_SETTING_KEY", "set_anonymous_access_enabled")

    offenders = [
        str(path.relative_to(importer_root))
        for path in importer_root.rglob("*.py")
        if any(name in path.read_text(encoding="utf-8") for name in names)
    ]

    assert offenders == []


# ---------------------------------------------------------------------------
# Admin settings page
# ---------------------------------------------------------------------------


async def test_settings_page_shows_the_switch_and_the_opted_in_projects(
    client: AsyncClient, admin: User, projects
) -> None:
    resp = await client.get("/admin/settings/", cookies=_cookies(admin))

    assert resp.status_code == 200
    html = resp.text
    assert 'data-testid="anonymous-access-state"' in html
    assert "Anonymous access is off." in html
    assert "exposes nothing by itself" in html
    assert "Switch One" in html and "Switch Two" in html
    assert "Switch Public" not in html
    assert 'name="enabled" value="1"' in html


async def test_settings_page_keeps_the_switch_out_of_the_generic_table(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    await _set_raw(db_session, "false")

    resp = await client.get("/admin/settings/", cookies=_cookies(admin))

    assert resp.status_code == 200
    assert ANONYMOUS_ACCESS_SETTING_KEY not in resp.text


async def test_turning_it_on_from_the_page_asks_for_confirmation_first(
    client: AsyncClient, db_session: AsyncSession, admin: User, projects
) -> None:
    resp = await _post_form(client, admin, {"enabled": "1"})

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/settings/anonymous-access/confirm/"
    assert await is_anonymous_access_enabled(db_session) is False


async def test_confirmation_page_lists_exactly_the_opted_in_projects(
    client: AsyncClient, admin: User, projects
) -> None:
    resp = await client.get("/admin/settings/anonymous-access/confirm/", cookies=_cookies(admin))

    assert resp.status_code == 200
    assert re.findall(r'data-project-key="([^"]+)"', resp.text) == ["SWONE", "SWTWO"]
    assert re.findall(r'name="confirmed_projects" value="([^"]+)"', resp.text) == ["SWONE", "SWTWO"]
    assert 'name="confirm" value="1"' in resp.text
    assert 'data-testid="anonymous-access-confirm-stale"' not in resp.text
    assert "The switch alone exposes nothing." in resp.text


async def test_confirming_turns_it_on_and_turning_it_off_needs_no_confirmation(
    client: AsyncClient, db_session: AsyncSession, admin: User, projects
) -> None:
    on = await _post_form(client, admin, {"enabled": "1", "confirm": "1", "confirmed_projects": ["SWONE", "SWTWO"]})
    assert on.status_code == 303
    assert on.headers["location"] == "/admin/settings/"
    assert await is_anonymous_access_enabled(db_session) is True

    confirm_page = await client.get(
        "/admin/settings/anonymous-access/confirm/", cookies=_cookies(admin), follow_redirects=False
    )
    assert confirm_page.status_code == 303

    off = await _post_form(client, admin, {"enabled": "0"})
    assert off.status_code == 303
    assert off.headers["location"] == "/admin/settings/"
    assert await is_anonymous_access_enabled(db_session) is False
    assert len(await _audit_rows(db_session)) == 2


async def test_pages_refuse_a_non_admin(client: AsyncClient, db_session: AsyncSession, regular: User) -> None:
    confirm_page = await client.get("/admin/settings/anonymous-access/confirm/", cookies=_cookies(regular))
    csrf = await _csrf(client, regular)
    post = await client.post(
        "/admin/settings/anonymous-access/",
        data={"enabled": "1", "confirm": "1", "csrf_token": csrf},
        cookies={**_cookies(regular), "csrf_token": csrf},
        headers={"x-csrf-token": csrf},
        follow_redirects=False,
    )

    assert confirm_page.status_code == 403
    assert post.status_code == 403
    assert await is_anonymous_access_enabled(db_session) is False


# ---------------------------------------------------------------------------
# Project settings card
# ---------------------------------------------------------------------------


async def test_project_settings_card_reflects_the_switch(
    client: AsyncClient, db_session: AsyncSession, admin: User, projects
) -> None:
    off = await client.get("/projects/SWONE/settings/", cookies=_cookies(admin))
    await set_anonymous_access_enabled(db_session, True, admin, confirmed_projects=["SWONE", "SWTWO"])
    await db_session.commit()
    on = await client.get("/projects/SWONE/settings/", cookies=_cookies(admin))

    assert 'data-testid="anonymous-access-switch-off"' in off.text
    assert 'data-testid="anonymous-access-switch-on"' in on.text
    assert "Anonymous access is turned on for the whole instance." in on.text
    assert "these settings apply" not in on.text


# ---------------------------------------------------------------------------
# Confirmation tied to the exact opted-in list
# ---------------------------------------------------------------------------


async def test_empty_confirmation_turns_it_on_when_nothing_is_opted_in(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    resp = await client.patch(URL, json={"enabled": True, "confirmed_projects": []}, headers=_bearer(admin))

    assert resp.status_code == 200, resp.text
    assert await is_anonymous_access_enabled(db_session) is True
    assert (await _audit_rows(db_session))[0].details["confirmed_projects"] == []


@pytest.mark.parametrize("confirmed", [[], ["SWONE"], ["SWONE", "SWTWO", "SWPUB"], ["SWONE", "SWPRIV"]])
async def test_stale_confirmation_is_refused_and_the_switch_stays_off(
    client: AsyncClient, db_session: AsyncSession, admin: User, projects, confirmed: list[str]
) -> None:
    resp = await client.patch(URL, json={"enabled": True, "confirmed_projects": confirmed}, headers=_bearer(admin))

    assert resp.status_code == 409, resp.text
    error = resp.json()["errors"][0]
    assert error["code"] == "confirmation_stale"
    assert [p["key"] for p in error["details"]["projects"]] == ["SWONE", "SWTWO"]
    assert await is_anonymous_access_enabled(db_session) is False
    assert await _audit_rows(db_session) == []


async def test_a_project_opted_in_after_the_first_attempt_makes_the_confirmation_stale(
    client: AsyncClient, db_session: AsyncSession, admin: User, projects
) -> None:
    first = await client.patch(URL, json={"enabled": True}, headers=_bearer(admin))
    shown = [p["key"] for p in first.json()["errors"][0]["details"]["projects"]]

    opt_in = await client.patch(
        "/api/v1/admin/projects/SWPUB/anonymous-permissions/",
        json={"anonymous_permissions": ["view_wiki"]},
        headers=_bearer(admin),
    )
    assert opt_in.status_code == 200, opt_in.text

    stale = await client.patch(URL, json={"enabled": True, "confirmed_projects": shown}, headers=_bearer(admin))
    assert stale.status_code == 409, stale.text
    error = stale.json()["errors"][0]
    assert error["code"] == "confirmation_stale"
    current = [p["key"] for p in error["details"]["projects"]]
    assert current == ["SWONE", "SWPUB", "SWTWO"]
    assert await is_anonymous_access_enabled(db_session) is False

    ok = await client.patch(URL, json={"enabled": True, "confirmed_projects": current}, headers=_bearer(admin))
    assert ok.status_code == 200, ok.text


async def test_stale_confirmation_from_the_page_re_renders_it_with_the_current_list(
    client: AsyncClient, db_session: AsyncSession, admin: User, projects
) -> None:
    await set_anonymous_permissions(db_session, projects["SWPUB"], ["view_issues"], admin)
    await db_session.commit()

    resp = await _post_form(client, admin, {"enabled": "1", "confirm": "1", "confirmed_projects": ["SWONE", "SWTWO"]})

    assert resp.status_code == 409
    assert 'data-testid="anonymous-access-confirm-stale"' in resp.text
    assert re.findall(r'data-project-key="([^"]+)"', resp.text) == ["SWONE", "SWPUB", "SWTWO"]
    assert re.findall(r'name="confirmed_projects" value="([^"]+)"', resp.text) == ["SWONE", "SWPUB", "SWTWO"]
    assert await is_anonymous_access_enabled(db_session) is False


async def test_page_confirmation_of_an_empty_list_turns_it_on(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    page = await client.get("/admin/settings/anonymous-access/confirm/", cookies=_cookies(admin))
    assert 'name="confirmed_projects"' not in page.text

    resp = await _post_form(client, admin, {"enabled": "1", "confirm": "1"})

    assert resp.status_code == 303
    assert await is_anonymous_access_enabled(db_session) is True


# ---------------------------------------------------------------------------
# Serialisation: one transaction-scoped lock for the switch and the opt-ins
# ---------------------------------------------------------------------------


async def _held_by_another_transaction(engine: AsyncEngine) -> bool:
    """Try the lock from a second connection; True if some other transaction holds it."""
    async with engine.connect() as conn:
        acquired = await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": ANONYMOUS_ACCESS_LOCK_KEY})
        if acquired:
            await conn.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": ANONYMOUS_ACCESS_LOCK_KEY})
        return not acquired


@pytest.mark.serial
async def test_turning_the_switch_on_holds_the_lock_until_the_transaction_ends(
    db_session: AsyncSession, db_engine: AsyncEngine, admin: User
) -> None:
    assert await _held_by_another_transaction(db_engine) is False

    await set_anonymous_access_enabled(db_session, True, admin, confirmed_projects=[])
    await db_session.commit()  # a savepoint here; the test transaction stays open

    assert await _held_by_another_transaction(db_engine) is True


@pytest.mark.serial
async def test_opting_a_project_in_takes_the_same_lock(
    db_session: AsyncSession, db_engine: AsyncEngine, admin: User
) -> None:
    project = ProjectFactory.build(key="SWLOCK", identifier="switch-lock", name="Switch Lock", is_public=True)
    db_session.add(project)
    await db_session.flush()
    assert await _held_by_another_transaction(db_engine) is False

    await set_anonymous_permissions(db_session, project, ["view_issues"], admin)

    assert await _held_by_another_transaction(db_engine) is True


@pytest.mark.serial
async def test_making_a_project_private_takes_the_same_lock(
    db_session: AsyncSession, db_engine: AsyncEngine, admin: User
) -> None:
    project = ProjectFactory.build(key="SWLOCKP", identifier="switch-lock-p", name="Lock P", is_public=True)
    db_session.add(project)
    await db_session.flush()
    assert await _held_by_another_transaction(db_engine) is False

    await ProjectService().update(db_session, project, ProjectUpdate(is_public=False), actor=admin)

    assert await _held_by_another_transaction(db_engine) is True
