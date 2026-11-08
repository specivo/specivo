"""Currencies and the reporting currency: the seeded table, the APIs, the admin page and the project override.

Covers the migration seed as tests see it, the read list for pickers (enabled
currencies, localised names), the admin switch (audited, refused to
non-admins, references keep working), the instance reporting currency (only
enabled currencies, audited, kept out of the generic settings endpoint), the
per-project override (manage_project, clear to inherit, audited) and the
resolver behind it.

Every test here locks the shared ``reporting_currency`` settings row or a
currency row for the length of its transaction, which is why they stay in one
module (``--dist loadfile`` keeps a module on one worker).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.currency import Currency
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.setting import Setting
from specivo.models.user import User
from specivo.services.auth_service import _make_access_token
from specivo.services.currency_service import (
    REPORTING_CURRENCY_SETTING_KEY,
    CurrencyService,
    reporting_currency_for,
)
from specivo.services.permission_service import clear_role_cache
from specivo.services.security_audit_service import AuditEvent
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_service = CurrencyService()


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="money_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def regular(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="money_regular", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="MONEYP", identifier="money-project", name="Money Project")
    db_session.add(proj)
    await db_session.commit()
    return proj


async def _grant(db: AsyncSession, user: User, project: Project, permissions: list[str]) -> None:
    role = Role(name=f"Money-{user.login}", permissions=permissions, builtin=0, issues_visibility="default")
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()
    clear_role_cache(db)


@pytest_asyncio.fixture
async def manager(db_session: AsyncSession, project: Project) -> User:
    user = UserFactory.build(login="money_manager", status="active")
    db_session.add(user)
    await db_session.commit()
    await _grant(db_session, user, project, ["manage_project", "view_issues"])
    return user


@pytest_asyncio.fixture
async def member(db_session: AsyncSession, project: Project) -> User:
    user = UserFactory.build(login="money_member", status="active")
    db_session.add(user)
    await db_session.commit()
    await _grant(db_session, user, project, ["view_issues"])
    return user


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


async def _audit_rows(db: AsyncSession, event: AuditEvent) -> list[SecurityAuditLog]:
    result = await db.execute(
        select(SecurityAuditLog).where(SecurityAuditLog.event_type == event).order_by(SecurityAuditLog.id)
    )
    return list(result.scalars().all())


async def _set_active(db: AsyncSession, code: str, is_active: bool) -> None:
    await db.execute(text("UPDATE currencies SET is_active = :a WHERE code = :c"), {"a": is_active, "c": code})
    await db.commit()


async def _set_reporting_raw(db: AsyncSession, value: str | None) -> None:
    await db.execute(text("DELETE FROM settings WHERE key = :k"), {"k": REPORTING_CURRENCY_SETTING_KEY})
    if value is not None:
        db.add(Setting(key=REPORTING_CURRENCY_SETTING_KEY, value=value))
    await db.commit()


async def _csrf(client: AsyncClient, user: User) -> str:
    resp = await client.get("/admin/currencies/", cookies=_cookies(user))
    for key, value in resp.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith("csrf_token="):
            return value.split("=", 1)[1].split(";")[0].strip()
    raise AssertionError("no csrf_token cookie on the currencies page")


async def _post_form(client: AsyncClient, user: User, url: str, data: dict[str, str]):
    csrf = await _csrf(client, user)
    return await client.post(
        url,
        data={**data, "csrf_token": csrf},
        cookies={**_cookies(user), "csrf_token": csrf},
        headers={"x-csrf-token": csrf},
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------


async def test_migration_seeds_the_iso_list(db_session: AsyncSession) -> None:
    rows = {c.code: c for c in (await db_session.execute(select(Currency))).scalars().all()}

    assert len(rows) >= 170
    assert rows["USD"].is_active and rows["USD"].minor_units == 2 and rows["USD"].numeric_code == 840
    assert rows["THB"].is_active and rows["THB"].name == "Baht"
    assert rows["JPY"].minor_units == 0
    assert rows["BHD"].minor_units == 3
    # Funds, metals, test and special codes are seeded inactive.
    for code in ("CLF", "XAU", "XTS", "XXX", "XDR"):
        assert not rows[code].is_active, code
    assert rows["XAU"].minor_units is None


async def test_code_format_is_enforced_by_the_database(db_session: AsyncSession) -> None:
    from sqlalchemy.exc import IntegrityError

    db_session.add(Currency(code="ab1", name="Bad", kind="national"))
    with pytest.raises(IntegrityError, match="ck_currencies_code_format"):
        await db_session.flush()


# ---------------------------------------------------------------------------
# Read list for pickers
# ---------------------------------------------------------------------------


async def test_any_signed_in_user_reads_enabled_currencies(client: AsyncClient, regular: User) -> None:
    resp = await client.get("/api/v1/currencies/", headers=_bearer(regular))

    assert resp.status_code == 200, resp.text
    items = {c["code"]: c for c in resp.json()}
    assert items["USD"] == {"code": "USD", "name": "US Dollar", "symbol": "$", "minor_units": 2}
    assert items["JPY"]["minor_units"] == 0
    assert "XAU" not in items and "XTS" not in items
    assert [c["code"] for c in resp.json()] == sorted(items)


async def test_names_follow_the_reader_language(client: AsyncClient, regular: User) -> None:
    resp = await client.get("/api/v1/currencies/", headers={**_bearer(regular), "Accept-Language": "th"})

    thb = next(c for c in resp.json() if c["code"] == "THB")
    assert thb["name"] == "บาท"
    assert thb["symbol"] == "฿"


async def test_include_returns_a_disabled_code_already_held(
    client: AsyncClient, db_session: AsyncSession, regular: User
) -> None:
    await _set_active(db_session, "EUR", False)

    plain = await client.get("/api/v1/currencies/", headers=_bearer(regular))
    with_held = await client.get("/api/v1/currencies/?include=eur", headers=_bearer(regular))

    assert "EUR" not in {c["code"] for c in plain.json()}
    assert "EUR" in {c["code"] for c in with_held.json()}


async def test_reading_currencies_needs_an_account(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/currencies/")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Admin switch
# ---------------------------------------------------------------------------


async def test_admin_lists_every_currency(client: AsyncClient, admin: User) -> None:
    resp = await client.get("/api/v1/admin/currencies/", headers=_bearer(admin))

    assert resp.status_code == 200, resp.text
    items = {c["code"]: c for c in resp.json()}
    assert items["XAU"]["is_active"] is False and items["XAU"]["kind"] == "metal"
    assert items["THB"]["iso_name"] == "Baht" and items["THB"]["name"] == "Thai Baht"

    inactive = await client.get("/api/v1/admin/currencies/?active=false", headers=_bearer(admin))
    assert {c["is_active"] for c in inactive.json()} == {False}


async def test_admin_disables_and_enables_a_currency_with_audit(
    client: AsyncClient, db_session: AsyncSession, admin: User, regular: User
) -> None:
    off = await client.patch("/api/v1/admin/currencies/thb/", json={"is_active": False}, headers=_bearer(admin))
    picker = await client.get("/api/v1/currencies/", headers=_bearer(regular))
    on = await client.patch("/api/v1/admin/currencies/THB/", json={"is_active": True}, headers=_bearer(admin))
    again = await client.patch("/api/v1/admin/currencies/THB/", json={"is_active": True}, headers=_bearer(admin))

    assert off.status_code == 200, off.text
    assert off.json()["is_active"] is False
    assert "THB" not in {c["code"] for c in picker.json()}
    assert on.json()["is_active"] is True
    assert again.status_code == 200
    rows = await _audit_rows(db_session, AuditEvent.CURRENCY_TOGGLED)
    # Saving the current state writes nothing.
    assert [r.details for r in rows] == [{"code": "THB", "is_active": False}, {"code": "THB", "is_active": True}]
    assert {r.user_id for r in rows} == {admin.id}
    assert {r.resource_type for r in rows} == {"currency"}


async def test_non_admins_cannot_list_or_switch_currencies(
    client: AsyncClient, db_session: AsyncSession, regular: User
) -> None:
    listing = await client.get("/api/v1/admin/currencies/", headers=_bearer(regular))
    switch = await client.patch("/api/v1/admin/currencies/USD/", json={"is_active": False}, headers=_bearer(regular))

    assert listing.status_code == 403
    assert switch.status_code == 403
    assert (await db_session.get(Currency, "USD")).is_active is True  # type: ignore[union-attr]


async def test_switching_an_unknown_currency_is_404(client: AsyncClient, admin: User) -> None:
    resp = await client.patch("/api/v1/admin/currencies/ZZZ/", json={"is_active": True}, headers=_bearer(admin))
    assert resp.status_code == 404


async def test_disabling_keeps_references_working(
    client: AsyncClient, db_session: AsyncSession, admin: User, project: Project
) -> None:
    await _service.set_project_reporting_currency(db_session, project, "EUR", admin)
    await _service.set_instance_reporting_currency(db_session, "THB", admin)
    await db_session.commit()

    resp_eur = await client.patch("/api/v1/admin/currencies/EUR/", json={"is_active": False}, headers=_bearer(admin))
    resp_thb = await client.patch("/api/v1/admin/currencies/THB/", json={"is_active": False}, headers=_bearer(admin))
    await db_session.refresh(project)

    assert resp_eur.status_code == resp_thb.status_code == 200
    assert project.reporting_currency_code == "EUR"
    assert await reporting_currency_for(db_session, project) == "EUR"
    assert await reporting_currency_for(db_session, None) == "THB"
    # Saving the project's current, now disabled, override is still allowed.
    resp = await client.patch(
        f"/api/v1/projects/{project.key}/", json={"reporting_currency_code": "EUR"}, headers=_bearer(admin)
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Instance reporting currency
# ---------------------------------------------------------------------------


async def test_reporting_currency_defaults_to_usd(db_session: AsyncSession) -> None:
    await _set_reporting_raw(db_session, None)
    assert await _service.get_instance_reporting_currency(db_session) == "USD"
    await _set_reporting_raw(db_session, "not a code")
    assert await _service.get_instance_reporting_currency(db_session) == "USD"


async def test_admin_sets_the_reporting_currency_with_audit(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    await _set_reporting_raw(db_session, "USD")

    resp = await client.patch(
        "/api/v1/admin/settings/reporting-currency/", json={"code": "thb"}, headers=_bearer(admin)
    )
    read = await client.get("/api/v1/admin/settings/reporting-currency/", headers=_bearer(admin))

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"code": "THB"}
    assert read.json() == {"code": "THB"}
    assert await reporting_currency_for(db_session, None) == "THB"
    rows = await _audit_rows(db_session, AuditEvent.REPORTING_CURRENCY_CHANGED)
    assert [r.details for r in rows] == [{"scope": "instance", "old": "USD", "new": "THB"}]
    assert rows[0].user_id == admin.id


@pytest.mark.parametrize(("code", "error"), [("XAU", "currency_inactive"), ("ZZZ", "currency_unknown")])
async def test_reporting_currency_must_be_an_enabled_currency(
    client: AsyncClient, db_session: AsyncSession, admin: User, code: str, error: str
) -> None:
    await _set_reporting_raw(db_session, "USD")

    resp = await client.patch("/api/v1/admin/settings/reporting-currency/", json={"code": code}, headers=_bearer(admin))

    assert resp.status_code == 422, resp.text
    assert resp.json()["errors"][0]["code"] == error
    assert await _service.get_instance_reporting_currency(db_session) == "USD"
    assert await _audit_rows(db_session, AuditEvent.REPORTING_CURRENCY_CHANGED) == []


async def test_reporting_currency_payload_is_validated(client: AsyncClient, admin: User) -> None:
    resp = await client.patch(
        "/api/v1/admin/settings/reporting-currency/", json={"code": "EURO"}, headers=_bearer(admin)
    )
    assert resp.status_code == 422


async def test_non_admins_cannot_read_or_set_the_reporting_currency(
    client: AsyncClient, db_session: AsyncSession, regular: User
) -> None:
    await _set_reporting_raw(db_session, "USD")

    read = await client.get("/api/v1/admin/settings/reporting-currency/", headers=_bearer(regular))
    write = await client.patch(
        "/api/v1/admin/settings/reporting-currency/", json={"code": "EUR"}, headers=_bearer(regular)
    )

    assert read.status_code == 403
    assert write.status_code == 403
    assert await _service.get_instance_reporting_currency(db_session) == "USD"


async def test_generic_settings_endpoint_refuses_the_reporting_currency(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    await _set_reporting_raw(db_session, "USD")

    resp = await client.patch("/api/v1/admin/settings/", json={"reporting_currency": "XAU"}, headers=_bearer(admin))

    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "setting_managed_elsewhere"
    assert await _service.get_instance_reporting_currency(db_session) == "USD"


# ---------------------------------------------------------------------------
# Project override
# ---------------------------------------------------------------------------


async def test_project_follows_the_instance_until_it_overrides(
    client: AsyncClient, db_session: AsyncSession, manager: User, project: Project
) -> None:
    await _set_reporting_raw(db_session, "USD")

    before = await client.get(f"/api/v1/projects/{project.key}/", headers=_bearer(manager))
    resp = await client.patch(
        f"/api/v1/projects/{project.key}/", json={"reporting_currency_code": "thb"}, headers=_bearer(manager)
    )

    assert before.json()["reporting_currency_code"] is None
    assert before.json()["reporting_currency"] == "USD"
    assert resp.status_code == 200, resp.text
    assert resp.json()["reporting_currency_code"] == "THB"
    assert resp.json()["reporting_currency"] == "THB"
    await db_session.refresh(project)
    assert await reporting_currency_for(db_session, project) == "THB"
    rows = await _audit_rows(db_session, AuditEvent.REPORTING_CURRENCY_CHANGED)
    assert [r.details for r in rows] == [{"scope": "project", "project_key": "MONEYP", "old": None, "new": "THB"}]
    assert rows[0].project_id == project.id and rows[0].user_id == manager.id


async def test_clearing_the_override_inherits_the_instance(
    client: AsyncClient, db_session: AsyncSession, manager: User, project: Project
) -> None:
    await _set_reporting_raw(db_session, "EUR")
    await _service.set_project_reporting_currency(db_session, project, "THB", manager)
    await db_session.commit()

    resp = await client.patch(
        f"/api/v1/projects/{project.key}/", json={"reporting_currency_code": None}, headers=_bearer(manager)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["reporting_currency_code"] is None
    assert resp.json()["reporting_currency"] == "EUR"


async def test_other_project_updates_leave_the_override_alone(
    client: AsyncClient, db_session: AsyncSession, manager: User, project: Project
) -> None:
    await _service.set_project_reporting_currency(db_session, project, "THB", manager)
    await db_session.commit()

    resp = await client.patch(f"/api/v1/projects/{project.key}/", json={"name": "Renamed"}, headers=_bearer(manager))

    assert resp.json()["reporting_currency_code"] == "THB"


@pytest.mark.parametrize(("code", "status"), [("XAU", 422), ("ZZZ", 422), ("EURO", 422)])
async def test_project_override_must_be_an_enabled_currency(
    client: AsyncClient, db_session: AsyncSession, manager: User, project: Project, code: str, status: int
) -> None:
    resp = await client.patch(
        f"/api/v1/projects/{project.key}/", json={"reporting_currency_code": code}, headers=_bearer(manager)
    )

    assert resp.status_code == status, resp.text
    await db_session.refresh(project)
    assert project.reporting_currency_code is None


async def test_members_without_manage_project_cannot_set_the_override(
    client: AsyncClient, db_session: AsyncSession, member: User, project: Project
) -> None:
    resp = await client.patch(
        f"/api/v1/projects/{project.key}/", json={"reporting_currency_code": "THB"}, headers=_bearer(member)
    )

    assert resp.status_code == 403
    await db_session.refresh(project)
    assert project.reporting_currency_code is None
    assert await _audit_rows(db_session, AuditEvent.REPORTING_CURRENCY_CHANGED) == []


async def test_project_currency_reference_is_restricted(db_session: AsyncSession, project: Project) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError, match="projects_reporting_currency_code_fkey"):
        await db_session.execute(
            text("UPDATE projects SET reporting_currency_code = 'ZZZ' WHERE id = :id"), {"id": project.id}
        )


# ---------------------------------------------------------------------------
# Sync from the CSV
# ---------------------------------------------------------------------------


async def test_sync_from_csv_keeps_admin_choices_and_restores_facts(db_session: AsyncSession) -> None:
    await db_session.execute(text("UPDATE currencies SET is_active = false, name = 'Changed' WHERE code = 'THB'"))
    await db_session.execute(
        text(
            "INSERT INTO currencies (code, name, kind, is_active) VALUES ('QQQ', 'Withdrawn sample', 'national', true)"
        )
    )
    await db_session.commit()

    counts = await _service.sync_from_csv(db_session)
    await db_session.commit()

    thb = await db_session.get(Currency, "THB")
    qqq = await db_session.get(Currency, "QQQ")
    assert counts == {"inserted": 0, "updated": 1, "deactivated": 1}
    assert thb is not None and thb.name == "Baht" and thb.is_active is False
    assert qqq is not None and qqq.is_active is False


async def test_sync_from_csv_inserts_missing_codes(db_session: AsyncSession) -> None:
    await db_session.execute(text("DELETE FROM currencies WHERE code = 'XTS'"))
    await db_session.commit()

    counts = await _service.sync_from_csv(db_session)

    xts = await db_session.get(Currency, "XTS")
    assert counts["inserted"] == 1
    assert xts is not None and xts.kind == "test" and xts.is_active is False


# ---------------------------------------------------------------------------
# Web pages
# ---------------------------------------------------------------------------


async def test_admin_page_lists_currencies_and_reporting_currency(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    await _set_reporting_raw(db_session, "THB")

    resp = await client.get("/admin/currencies/?show=inactive&q=gold", cookies=_cookies(admin))

    assert resp.status_code == 200
    assert 'data-code="XAU"' in resp.text
    assert 'data-code="USD"' not in resp.text
    assert '<option value="THB" selected>' in resp.text


async def test_admin_page_is_for_admins_only(client: AsyncClient, regular: User) -> None:
    resp = await client.get("/admin/currencies/", cookies=_cookies(regular))
    assert resp.status_code == 403


async def test_admin_page_forms_switch_and_set(client: AsyncClient, db_session: AsyncSession, admin: User) -> None:
    await _set_reporting_raw(db_session, "USD")

    toggle = await _post_form(client, admin, "/admin/currencies/XAU/", {"is_active": "1", "show": "inactive"})
    reporting = await _post_form(client, admin, "/admin/currencies/reporting/", {"code": "XAU"})
    refused = await _post_form(client, admin, "/admin/currencies/reporting/", {"code": "XTS"})

    assert toggle.status_code == 303
    assert toggle.headers["location"] == "/admin/currencies/?show=inactive"
    assert (await db_session.get(Currency, "XAU")).is_active is True  # type: ignore[union-attr]
    assert reporting.status_code == 303
    assert await _service.get_instance_reporting_currency(db_session) == "XAU"
    assert refused.headers["location"] == "/admin/currencies/?error=currency_inactive"
    assert await _service.get_instance_reporting_currency(db_session) == "XAU"


async def test_project_settings_page_offers_the_override(
    client: AsyncClient, db_session: AsyncSession, manager: User, project: Project
) -> None:
    await _set_reporting_raw(db_session, "USD")

    resp = await client.get(f"/projects/{project.key}/settings/", cookies=_cookies(manager))

    assert resp.status_code == 200, resp.text
    assert 'data-testid="project-reporting-currency"' in resp.text
    assert "Instance default (USD)" in resp.text
    assert '<option value="THB">THB · Thai Baht</option>' in resp.text
