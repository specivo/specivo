"""What writing SCIM attributes to a linked account does (``ScimUserService._apply``), case by case.

Every login and email here carries ``sca`` so parallel modules never collide.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.sso import SsoProvider
from specivo.services.scim.errors import ScimError
from specivo.services.scim.users import ScimUserService
from specivo.services.user_service import UserService
from specivo.testing.factories.user import UserFactory
from tests.integration.scim.conftest import user_body

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_service = ScimUserService()


async def _linked(db: AsyncSession, provider: SsoProvider, name: str, **extra: Any):
    res, user = await _service.create(db, provider, user_body(f"sca-{name}", f"sca-{name}@example.com", **extra))
    attrs = _service._attributes(user_body(f"sca-{name}", f"sca-{name}@example.com", **extra))
    return res, user, attrs


def _state(res, user) -> tuple:
    return (
        res.user_name,
        res.external_id,
        user.login,
        user.email,
        user.email_verified_at is not None,
        user.display_name,
        user.language,
        user.timezone,
        user.status,
    )


async def _run(db: AsyncSession, provider: SsoProvider, res, user, attrs, **kw: Any) -> tuple:
    try:
        changed = await _service._apply(db, provider, res, user, attrs, **kw)
    except ScimError as exc:
        return ("error", exc.status, exc.scim_type, _state(res, user))
    return ("ok", changed, _state(res, user))


async def test_apply_each_attribute(db_session: AsyncSession, scim_provider) -> None:
    db = db_session
    provider, _token = scim_provider
    results: dict[str, tuple] = {}

    res, user, attrs = await _linked(db, provider, "same")
    results["unchanged"] = await _run(db, provider, res, user, attrs)

    res, user, attrs = await _linked(db, provider, "case")
    results["user_name_case"] = await _run(db, provider, res, user, dataclasses.replace(attrs, user_name="SCA-Case"))

    res, user, attrs = await _linked(db, provider, "rename")
    renamed = dataclasses.replace(attrs, user_name="sca-renamed", login="sca-renamed")
    results["rename"] = await _run(db, provider, res, user, renamed)

    await _linked(db, provider, "taken")
    res, user, attrs = await _linked(db, provider, "clash")
    clash = dataclasses.replace(attrs, user_name="sca-taken", login="sca-taken")
    results["user_name_taken"] = await _run(db, provider, res, user, clash)

    owner = UserFactory.build(login="sca-owned", email="sca-owner@example.com", status="active")
    db.add(owner)
    await db.flush()
    res, user, attrs = await _linked(db, provider, "follow")
    results["login_owned"] = await _run(
        db, provider, res, user, dataclasses.replace(attrs, user_name="sca-owned", login="sca-owned")
    )

    res, user, attrs = await _linked(db, provider, "nofollow")
    results["no_follow"] = await _run(
        db,
        provider,
        res,
        user,
        dataclasses.replace(attrs, user_name="sca-nofollow2", login="sca-nofollow2"),
        follow_login=False,
    )

    res, user, attrs = await _linked(db, provider, "ext")
    results["external_id"] = await _run(db, provider, res, user, dataclasses.replace(attrs, external_id="ext-9"))

    res, user, attrs = await _linked(db, provider, "mailcase")
    user.email_verified_at = None
    results["email_case"] = await _run(
        db, provider, res, user, dataclasses.replace(attrs, email="SCA-Mailcase@example.com")
    )

    res, user, attrs = await _linked(db, provider, "mail")
    results["email"] = await _run(db, provider, res, user, dataclasses.replace(attrs, email="sca-mail2@example.com"))

    res, user, attrs = await _linked(db, provider, "pw")
    user.password_hash = "x"
    results["email_with_password"] = await _run(
        db, provider, res, user, dataclasses.replace(attrs, email="sca-pw2@example.com")
    )

    res, user, attrs = await _linked(db, provider, "mailtaken")
    results["email_taken"] = await _run(
        db, provider, res, user, dataclasses.replace(attrs, email="sca-owner@example.com")
    )

    res, user, attrs = await _linked(db, provider, "domain")
    provider.allowed_email_domains = ["example.com"]
    results["email_domain"] = await _run(
        db, provider, res, user, dataclasses.replace(attrs, email="sca-domain@example.org")
    )
    provider.allowed_email_domains = []

    res, user, attrs = await _linked(db, provider, "profile")
    results["profile"] = await _run(
        db,
        provider,
        res,
        user,
        dataclasses.replace(attrs, display_name="Profile Name", language="th", timezone="Asia/Bangkok"),
    )
    results["profile_unset"] = await _run(
        db, provider, res, user, dataclasses.replace(attrs, display_name="Profile Name", language=None, timezone=None)
    )

    res, user, attrs = await _linked(db, provider, "off")
    results["deactivate"] = await _run(db, provider, res, user, dataclasses.replace(attrs, active=False))
    results["reactivate"] = await _run(db, provider, res, user, attrs)

    res, user, attrs = await _linked(db, provider, "adminoff")
    await UserService().deactivate(db, user, reason="admin", actor_id=None)
    results["deactivated_elsewhere"] = await _run(db, provider, res, user, attrs)

    res, user, attrs = await _linked(db, provider, "pending")
    user.status = "pending_verification"
    results["pending"] = await _run(db, provider, res, user, attrs)

    res, user, attrs = await _linked(db, provider, "admin")
    user.is_admin = True
    results["admin_email"] = await _run(
        db, provider, res, user, dataclasses.replace(attrs, email="sca-admin2@example.com")
    )
    results["admin_rename"] = await _run(
        db, provider, res, user, dataclasses.replace(attrs, user_name="sca-admin2", login="sca-admin2")
    )
    results["admin_off"] = await _run(db, provider, res, user, dataclasses.replace(attrs, active=False))

    def state(name: str, **changes: Any) -> tuple:
        base = {
            "user_name": f"sca-{name}",
            "external_id": None,
            "login": f"sca-{name}",
            "email": f"sca-{name}@example.com",
            "verified": True,
            "display_name": f"Name of sca-{name}",
            "language": "en",
            "timezone": "UTC",
            "status": "active",
        }
        base.update(changes)
        return tuple(base.values())

    assert results == {
        "unchanged": ("ok", [], state("same")),
        "user_name_case": ("ok", ["userName"], state("case", user_name="SCA-Case")),
        "rename": ("ok", ["userName"], state("rename", user_name="sca-renamed", login="sca-renamed")),
        "user_name_taken": ("error", 409, "uniqueness", state("clash")),
        "login_owned": ("ok", ["userName"], state("follow", user_name="sca-owned")),
        "no_follow": ("ok", ["userName"], state("nofollow", user_name="sca-nofollow2")),
        "external_id": ("ok", ["externalId"], state("ext", external_id="ext-9")),
        "email_case": ("ok", ["emails"], state("mailcase", email="SCA-Mailcase@example.com")),
        "email": ("ok", ["emails"], state("mail", email="sca-mail2@example.com")),
        "email_with_password": ("error", 400, "mutability", state("pw")),
        "email_taken": ("error", 409, "uniqueness", state("mailtaken")),
        "email_domain": ("error", 400, "invalidValue", state("domain")),
        "profile_unset": (
            "ok",
            [],
            state("profile", display_name="Profile Name", language="th", timezone="Asia/Bangkok"),
        ),
        "profile": (
            "ok",
            ["displayName", "preferredLanguage", "timezone"],
            state("profile", display_name="Profile Name", language="th", timezone="Asia/Bangkok"),
        ),
        "deactivate": ("ok", ["active"], state("off", status="deactivated")),
        "reactivate": ("ok", ["active"], state("off")),
        "deactivated_elsewhere": ("ok", [], state("adminoff", status="deactivated")),
        "pending": ("ok", ["active"], state("pending")),
        "admin_email": ("error", 400, None, state("admin")),
        "admin_rename": ("ok", ["userName"], state("admin", user_name="sca-admin2")),
        # The userName goes back before the deactivation is refused.
        "admin_off": ("error", 400, None, state("admin")),
    }
