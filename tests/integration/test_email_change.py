"""Self-service email change confirmed by link, and the administrator's direct change.

Covers the API (``/api/v1/auth/me/email/``, ``/api/v1/auth/confirm-email/``),
the service rules (latest link only, expiry, races, SCIM-managed accounts),
commit-command matching, and the admin routes. Logins and addresses carry an
``emc`` prefix because modules run in parallel inside uncommitted
transactions and ``users.email`` is unique.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.utils import utcnow
from specivo.models.auth import EmailChangeToken, PasswordResetToken, RefreshToken
from specivo.models.scim import ScimResource
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.sso import SsoProvider
from specivo.models.user import User
from specivo.services.email_change_service import EmailChangeService, mask_email
from specivo.services.repository_hooks import _matched_users
from specivo.services.security_audit_service import AuditEvent
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = pytest.mark.integration

ME_EMAIL = "/api/v1/auth/me/email/"
CONFIRM = "/api/v1/auth/confirm-email/"
DELAY = "specivo.tasks.notifications.send_notification_email.delay"
_TOKEN_RE = re.compile(r"/confirm-email/\?token=([A-Za-z0-9_\-]+)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mails(mock: Any) -> list[dict[str, str]]:
    """The queued mails as dicts: to, subject, html, text."""
    out = []
    for call in mock.call_args_list:
        to, subject, html, text = call.args[:4]
        out.append({"to": to, "subject": subject, "html": html, "text": text})
    return out


def _token_in(mail: dict[str, str]) -> str:
    match = _TOKEN_RE.search(mail["text"])
    assert match, mail["text"]
    return match.group(1)


async def _request(client: AsyncClient, new_email: str, password: str = TEST_PASSWORD) -> tuple[Any, list]:
    with patch(DELAY) as delay:
        resp = await client.post(ME_EMAIL, json={"new_email": new_email, "current_password": password})
    return resp, _mails(delay)


async def _user(db: AsyncSession, **fields: Any) -> User:
    user = UserFactory.build(**fields)
    db.add(user)
    await db.flush()
    return user


async def _events(db: AsyncSession, event: AuditEvent) -> list[SecurityAuditLog]:
    rows = await db.execute(select(SecurityAuditLog).where(SecurityAuditLog.event_type == event.value))
    return list(rows.scalars().all())


async def _second_session(client: AsyncClient, login: str) -> None:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text


async def _session_ids(db: AsyncSession, user_id: int) -> set[uuid.UUID]:
    rows = await db.execute(select(RefreshToken.session_id).where(RefreshToken.user_id == user_id))
    return set(rows.scalars().all())


# ---------------------------------------------------------------------------
# Request, then confirm
# ---------------------------------------------------------------------------


async def test_request_then_confirm_swaps_the_address(
    auth_client: AsyncClient, client: AsyncClient, db_session: AsyncSession
) -> None:
    user: User = auth_client.state.user  # type: ignore[attr-defined]
    old_email = user.email
    await _second_session(client, user.login)
    reset = PasswordResetToken(user_id=user.id, token_hash="a" * 64, expires_at=utcnow() + timedelta(hours=1))
    db_session.add(reset)
    await db_session.flush()
    sessions_before = await _session_ids(db_session, user.id)
    assert len(sessions_before) == 2

    resp, mails = await _request(auth_client, "emc-new@example.com")
    assert resp.status_code == 202, resp.text
    assert resp.json()["pending"]["new_email"] == "emc-new@example.com"
    [mail] = mails
    assert mail["to"] == "emc-new@example.com"
    assert "<" not in mail["text"]

    # Nothing changes until the link is used.
    await db_session.refresh(user)
    assert user.email == old_email
    state = (await auth_client.get(ME_EMAIL)).json()
    assert state["email"] == old_email and state["pending"]["new_email"] == "emc-new@example.com"

    with patch(DELAY) as delay:
        confirmed = await auth_client.post(CONFIRM, json={"token": _token_in(mail)})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["email"] == "emc-new@example.com"

    await db_session.refresh(user)
    assert user.email == "emc-new@example.com"
    assert user.email_verified_at is not None
    # The old address hears about it, with the new one masked.
    [notice] = _mails(delay)
    assert notice["to"] == old_email
    assert mask_email("emc-new@example.com") in notice["text"]
    assert "emc-new@example.com" not in notice["text"]
    assert "/forgot-password/" in notice["text"]
    # The caller's session stays, the other one ends.
    assert len(sessions_before - await _session_ids(db_session, user.id)) == 1
    assert len(await _session_ids(db_session, user.id)) == 1
    assert (await auth_client.get(ME_EMAIL)).status_code == 200
    # Pending password resets are dropped.
    await db_session.refresh(reset)
    assert reset.used_at is not None
    [event] = await _events(db_session, AuditEvent.EMAIL_CHANGED)
    assert event.user_id == user.id
    assert event.details["old_email"] == old_email
    assert event.details["new_email"] == "emc-new@example.com"
    assert event.details["by"] == "user"
    assert (await auth_client.get(ME_EMAIL)).json()["pending"] is None


async def test_confirm_from_another_browser_ends_every_session(
    auth_client: AsyncClient, client: AsyncClient, db_session: AsyncSession
) -> None:
    user: User = auth_client.state.user  # type: ignore[attr-defined]
    _resp, [mail] = await _request(auth_client, "emc-elsewhere@example.com")
    with patch(DELAY):
        confirmed = await client.post(CONFIRM, json={"token": _token_in(mail)})
    assert confirmed.status_code == 200, confirmed.text
    assert await _session_ids(db_session, user.id) == set()


async def test_wrong_current_password_is_refused(auth_client: AsyncClient, db_session: AsyncSession) -> None:
    resp, mails = await _request(auth_client, "emc-wrongpw@example.com", password="not-the-password")
    assert resp.status_code == 400
    error = resp.json()["errors"][0]
    assert error["code"] == "email_change_password_invalid"
    assert error["field"] == "current_password"
    assert mails == []
    assert (await auth_client.get(ME_EMAIL)).json()["pending"] is None
    assert len(await _events(db_session, AuditEvent.EMAIL_CHANGE_FAILED)) == 1


@pytest.mark.parametrize("address", ["not-an-address", "emc@", "a" * 250 + "@example.com"])
async def test_invalid_address_is_refused(auth_client: AsyncClient, address: str) -> None:
    resp, mails = await _request(auth_client, address)
    assert resp.status_code == 422, resp.text
    assert resp.json()["errors"][0]["field"] == "new_email"
    assert mails == []


async def test_current_address_is_refused_case_insensitively(auth_client: AsyncClient) -> None:
    user: User = auth_client.state.user  # type: ignore[attr-defined]
    resp, mails = await _request(auth_client, user.email.upper())
    assert resp.status_code == 400
    assert resp.json()["errors"][0]["code"] == "email_change_same_address"
    assert mails == []


# ---------------------------------------------------------------------------
# An address another account uses
# ---------------------------------------------------------------------------


async def test_taken_address_answers_the_same_and_sends_no_link(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    user: User = auth_client.state.user  # type: ignore[attr-defined]
    await _user(db_session, login="emcowner", email="emc-owner@example.com", language="th")

    free, [free_mail] = await _request(auth_client, "emc-free@example.com")
    taken, taken_mails = await _request(auth_client, "EMC-Owner@example.com")

    assert taken.status_code == free.status_code == 202
    assert taken.json()["detail"] == free.json()["detail"]
    assert set(taken.json()) == set(free.json())
    assert taken.json()["pending"]["new_email"] == "EMC-Owner@example.com"
    # The pending state looks like any other.
    state = (await auth_client.get(ME_EMAIL)).json()
    assert state["pending"]["new_email"] == "EMC-Owner@example.com"
    # One mail, to the address, without a link.
    [note] = taken_mails
    assert note["to"] == "EMC-Owner@example.com"
    assert not _TOKEN_RE.search(note["text"])
    assert "/confirm-email/" not in note["html"]
    assert "/confirm-email/" in free_mail["text"]
    await db_session.refresh(user)
    assert user.email != "EMC-Owner@example.com"
    [_first, second] = sorted(await _events(db_session, AuditEvent.EMAIL_CHANGE_REQUESTED), key=lambda e: e.id)
    assert second.details["address_in_use"] is True


async def test_address_taken_between_request_and_confirmation(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    user: User = auth_client.state.user  # type: ignore[attr-defined]
    _resp, [mail] = await _request(auth_client, "emc-race@example.com")
    await _user(db_session, login="emcracer", email="emc-race@example.com")

    with patch(DELAY) as delay:
        confirmed = await auth_client.post(CONFIRM, json={"token": _token_in(mail)})
    assert confirmed.status_code == 409
    assert confirmed.json()["errors"][0]["code"] == "email_change_address_taken"
    delay.assert_not_called()
    await db_session.refresh(user)
    assert user.email != "emc-race@example.com"
    row = await db_session.scalar(select(EmailChangeToken).where(EmailChangeToken.user_id == user.id))
    assert row is not None and row.invalidation_reason == "address_taken"


# ---------------------------------------------------------------------------
# Links: expired, used, tampered, superseded, cancelled
# ---------------------------------------------------------------------------


async def _confirm_code(client: AsyncClient, token: str) -> str:
    with patch(DELAY):
        resp = await client.post(CONFIRM, json={"token": token})
    assert resp.status_code in (400, 409), resp.text
    return resp.json()["errors"][0]["code"]


async def test_expired_link_is_refused(auth_client: AsyncClient, db_session: AsyncSession) -> None:
    user: User = auth_client.state.user  # type: ignore[attr-defined]
    _resp, [mail] = await _request(auth_client, "emc-expired@example.com")
    row = await db_session.scalar(select(EmailChangeToken).where(EmailChangeToken.user_id == user.id))
    assert row is not None
    row.expires_at = utcnow() - timedelta(minutes=1)
    await db_session.flush()
    assert await _confirm_code(auth_client, _token_in(mail)) == "email_change_link_expired"
    assert (await auth_client.get(ME_EMAIL)).json()["pending"]["expired"] is True


async def test_used_link_is_refused(auth_client: AsyncClient) -> None:
    _resp, [mail] = await _request(auth_client, "emc-used@example.com")
    with patch(DELAY):
        assert (await auth_client.post(CONFIRM, json={"token": _token_in(mail)})).status_code == 200
    assert await _confirm_code(auth_client, _token_in(mail)) == "email_change_link_used"


async def test_tampered_link_is_refused(auth_client: AsyncClient, db_session: AsyncSession) -> None:
    user: User = auth_client.state.user  # type: ignore[attr-defined]
    _resp, [mail] = await _request(auth_client, "emc-tamper@example.com")
    token = _token_in(mail)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert await _confirm_code(auth_client, tampered) == "email_change_link_invalid"
    await db_session.refresh(user)
    assert user.email != "emc-tamper@example.com"


async def test_only_the_latest_link_works(auth_client: AsyncClient, db_session: AsyncSession) -> None:
    user: User = auth_client.state.user  # type: ignore[attr-defined]
    _resp, [first] = await _request(auth_client, "emc-first@example.com")
    _resp, [second] = await _request(auth_client, "emc-second@example.com")
    with patch(DELAY) as delay:
        resent = await auth_client.post(f"{ME_EMAIL}resend/")
    assert resent.status_code == 202, resent.text
    [third] = _mails(delay)
    assert third["to"] == "emc-second@example.com"

    assert await _confirm_code(auth_client, _token_in(first)) == "email_change_link_superseded"
    assert await _confirm_code(auth_client, _token_in(second)) == "email_change_link_superseded"
    with patch(DELAY):
        assert (await auth_client.post(CONFIRM, json={"token": _token_in(third)})).status_code == 200
    await db_session.refresh(user)
    assert user.email == "emc-second@example.com"


async def test_cancel_ends_the_pending_change(auth_client: AsyncClient) -> None:
    _resp, [mail] = await _request(auth_client, "emc-cancel@example.com")
    assert (await auth_client.delete(ME_EMAIL)).status_code == 204
    assert (await auth_client.get(ME_EMAIL)).json()["pending"] is None
    assert await _confirm_code(auth_client, _token_in(mail)) == "email_change_link_superseded"
    resend = await auth_client.post(f"{ME_EMAIL}resend/")
    assert resend.status_code == 404
    assert resend.json()["errors"][0]["code"] == "email_change_no_pending"


# ---------------------------------------------------------------------------
# SCIM-managed and service accounts
# ---------------------------------------------------------------------------


async def test_scim_managed_account_is_refused(
    auth_client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    user: User = auth_client.state.user  # type: ignore[attr-defined]
    db_session.add(
        ScimResource(provider_id=sso_provider.id, resource_type="User", scim_id=str(uuid.uuid4()), user_id=user.id)
    )
    await db_session.flush()

    resp, mails = await _request(auth_client, "emc-scim@example.com")
    assert resp.status_code == 403
    assert resp.json()["errors"][0]["code"] == "email_change_managed_by_idp"
    assert mails == []
    state = (await auth_client.get(ME_EMAIL)).json()
    assert state["managed_by_idp"] is True and state["available"] is False


async def test_service_account_cannot_change_its_address(agent_client: AsyncClient) -> None:
    with patch(DELAY) as delay:
        resp = await agent_client.post(ME_EMAIL, json={"new_email": "emc-agent@example.com"})
    assert resp.status_code == 400
    assert resp.json()["errors"][0]["code"] == "email_change_unavailable"
    delay.assert_not_called()


# ---------------------------------------------------------------------------
# Commit-command matching
# ---------------------------------------------------------------------------


async def test_commit_matching_uses_the_new_address_only_after_confirmation(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    user: User = auth_client.state.user  # type: ignore[attr-defined]
    user.email_verified_at = utcnow()
    old_email = user.email
    await db_session.flush()

    _resp, [mail] = await _request(auth_client, "emc-commits@example.com")
    assert await _matched_users(db_session, ["emc-commits@example.com"]) == {}
    assert await _matched_users(db_session, [old_email]) == {old_email.lower(): user.id}

    with patch(DELAY):
        assert (await auth_client.post(CONFIRM, json={"token": _token_in(mail)})).status_code == 200
    assert await _matched_users(db_session, ["EMC-Commits@example.com"]) == {"emc-commits@example.com": user.id}
    assert await _matched_users(db_session, [old_email]) == {}


# ---------------------------------------------------------------------------
# Service: languages of the mails
# ---------------------------------------------------------------------------


async def test_mails_render_without_a_request(db_session: AsyncSession) -> None:
    user = await _user(db_session, login="emcsvc", email="emc-svc@example.com", language="th")
    service = EmailChangeService()
    with patch(DELAY) as delay:
        pending = await service.request_change(
            db_session, user, new_email="emc-svc-new@example.com", current_password=TEST_PASSWORD, session_id=None
        )
    assert pending.new_email == "emc-svc-new@example.com"
    [mail] = _mails(delay)
    with patch(DELAY):
        result = await service.confirm(db_session, _token_in(mail))
    assert result.old_email == "emc-svc@example.com"
    assert result.new_email == "emc-svc-new@example.com"
    count = await db_session.scalar(
        select(func.count()).select_from(EmailChangeToken).where(EmailChangeToken.user_id == user.id)
    )
    assert count == 1


# ---------------------------------------------------------------------------
# Administrators
# ---------------------------------------------------------------------------


async def test_admin_change_audits_and_notifies_both_addresses(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin: User = admin_client.state.user  # type: ignore[attr-defined]
    target = await _user(db_session, login="emctarget", email="emc-target-old@example.com")
    with patch(DELAY) as delay:
        resp = await admin_client.patch(
            f"/api/v1/admin/users/{target.id}/email/", json={"email": "emc-target-new@example.com"}
        )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(target)
    assert target.email == "emc-target-new@example.com"
    assert target.email_verified_at is not None
    assert {m["to"] for m in _mails(delay)} == {"emc-target-old@example.com", "emc-target-new@example.com"}
    [event] = await _events(db_session, AuditEvent.EMAIL_CHANGED)
    assert event.user_id == admin.id
    assert event.details == {
        "by": "admin",
        "target_user_id": target.id,
        "target_login": "emctarget",
        "old_email": "emc-target-old@example.com",
        "new_email": "emc-target-new@example.com",
    }


async def test_admin_change_refuses_a_taken_address(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await _user(db_session, login="emctarget2", email="emc-target2@example.com")
    await _user(db_session, login="emcother2", email="emc-other2@example.com")
    with patch(DELAY) as delay:
        resp = await admin_client.patch(
            f"/api/v1/admin/users/{target.id}/email/", json={"email": "EMC-other2@example.com"}
        )
    assert resp.status_code == 409
    delay.assert_not_called()
    await db_session.refresh(target)
    assert target.email == "emc-target2@example.com"


async def test_admin_web_form_changes_the_address(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await _user(db_session, login="emctarget3", email="emc-target3@example.com")
    with patch(DELAY) as delay:
        resp = await admin_client.post(
            f"/admin/users/{target.id}/email/", data={"email": "emc-target3-new@example.com"}, follow_redirects=False
        )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"].endswith("?account=email_changed")
    assert len(_mails(delay)) == 2
    await db_session.refresh(target)
    assert target.email == "emc-target3-new@example.com"
    page = await admin_client.get(f"/admin/users/{target.id}/?account=email_changed")
    assert "A notice went to the old and the new address." in page.text
