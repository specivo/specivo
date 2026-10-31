"""SCIM ``/Users`` over HTTP: CRUD, filters, pagination, linking, deprovisioning.

Requests modelled on Microsoft Entra ID's and Okta's documented ones come
from ``tests/fixtures/scim/``. Every login and email here carries ``scu`` so
parallel modules never collide.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.utils import utcnow
from specivo.models.auth import ApiKey, PasswordResetToken, RefreshToken
from specivo.models.scim import ScimResource
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.services.api_key_service import ApiKeyService
from specivo.services.user_service import UserService
from specivo.testing.factories.user import TEST_PASSWORD, AdminUserFactory, ServiceAccountFactory, UserFactory
from specivo.testing.sso import link_identity
from tests.integration.scim.conftest import SCIM_JSON, ScimClient, fixture, make_scim_provider, user_body

pytestmark = pytest.mark.integration

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"


async def _user(db: AsyncSession, factory=UserFactory, **fields) -> User:
    """An account with a verified email unless ``email_verified_at`` is given."""
    fields.setdefault("email_verified_at", utcnow())
    user = factory.build(**fields)
    db.add(user)
    await db.flush()
    return user


async def _sign_in(client: AsyncClient, login: str) -> None:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text


async def _sessions(db: AsyncSession, user_id: int) -> int:
    return int(await db.scalar(select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == user_id)))


async def _create(scim: ScimClient, user_name: str, email: str | None = None, **extra) -> dict:
    resp = await scim.post("/Users", user_body(user_name, email, **extra))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _assert_error(resp, status: int, scim_type: str | None = None) -> dict:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith(SCIM_JSON)
    body = resp.json()
    assert body["schemas"] == [ERROR_SCHEMA]
    assert body["status"] == str(status)
    assert body.get("scimType") == scim_type
    return body


# ---------------------------------------------------------------------------
# Create and read
# ---------------------------------------------------------------------------


async def test_entra_create_user(scim: ScimClient, db_session: AsyncSession, scim_provider) -> None:
    resp = await scim.post("/Users", fixture("entra_create_user.json"))

    assert resp.status_code == 201, resp.text
    assert resp.headers["content-type"].startswith(SCIM_JSON)
    body = resp.json()
    assert body["schemas"] == [USER_SCHEMA]
    assert body["userName"] == "Test_User_ab6490ee-1e48-479e-a20b-2d77186b5dd1@example.com"
    assert body["externalId"] == "0a21f0f2-8d2a-4f8e-bf98-7363c4aed4ef"
    assert body["active"] is True
    assert body["emails"][0]["value"] == "Test_User_fd0ea19b-0777-472c-9f96-4f70d2226f2e@example.com"
    assert body["meta"]["resourceType"] == "User"
    assert resp.headers["location"] == body["meta"]["location"]
    assert body["meta"]["location"].endswith(f"/scim/v2/{scim.slug}/Users/{body['id']}")

    res = (await db_session.execute(select(ScimResource).where(ScimResource.scim_id == body["id"]))).scalar_one()
    user = await db_session.get(User, res.user_id)
    assert str(user.id) != body["id"]  # opaque id, not the database id
    assert user.login == "test_user_ab6490ee-1e48-479e-a20b-2d77186b5dd1"
    assert user.email == "Test_User_fd0ea19b-0777-472c-9f96-4f70d2226f2e@example.com"
    assert user.email_verified_at is not None
    assert user.password_hash is None
    assert user.is_admin is False and user.is_service_account is False
    assert user.display_name == "givenName familyName"
    assert user.language == "th"
    assert user.status == "active"


async def test_okta_create_user_ignores_password(scim: ScimClient, db_session: AsyncSession) -> None:
    body = fixture("okta_create_user.json")
    body["userName"] = body["emails"][0]["value"] = "scu.okta@example.com"
    resp = await scim.post("/Users", body)
    assert resp.status_code == 201, resp.text
    user = (await db_session.execute(select(User).where(User.email == "scu.okta@example.com"))).scalar_one()
    assert user.password_hash is None
    assert user.display_name == "Test User"
    assert resp.json()["externalId"] == "00ujl29u0le5T6Aj10h7"


async def test_create_inactive_user(scim: ScimClient, db_session: AsyncSession) -> None:
    body = await _create(scim, "scu-inactive@example.com", active=False)
    assert body["active"] is False
    user = (await db_session.execute(select(User).where(User.email == "scu-inactive@example.com"))).scalar_one()
    assert user.status == "deactivated"


async def test_get_user_and_404(scim: ScimClient) -> None:
    created = await _create(scim, "scu-get@example.com")
    resp = await scim.get(f"/Users/{created['id']}")
    assert resp.status_code == 200
    assert resp.json() == created | {"meta": resp.json()["meta"]}
    _assert_error(await scim.get("/Users/00000000-0000-0000-0000-000000000000"), 404)
    _assert_error(await scim.get("/Users/42"), 404)


async def test_duplicate_user_name_is_409_uniqueness(scim: ScimClient) -> None:
    await _create(scim, "scu-dup@example.com")
    resp = await scim.post("/Users", user_body("SCU-DUP@example.com", "scu-dup-other@example.com"))
    _assert_error(resp, 409, "uniqueness")


async def test_reserved_login_is_refused(scim: ScimClient) -> None:
    _assert_error(await scim.post("/Users", user_body("$git", "scu-git@example.com")), 400, "invalidValue")


async def test_missing_email_is_refused(scim: ScimClient) -> None:
    body = user_body("scu-noemail")
    del body["emails"]
    _assert_error(await scim.post("/Users", body), 400, "invalidValue")


async def test_bad_json_and_content_type(scim: ScimClient) -> None:
    resp = await scim.client.post(
        scim.url("/Users"), content=b"{not json", headers=scim._headers({"Content-Type": SCIM_JSON})
    )
    _assert_error(resp, 400, "invalidSyntax")
    resp = await scim.client.post(
        scim.url("/Users"), content=b"userName=x", headers=scim._headers({"Content-Type": "text/plain"})
    )
    _assert_error(resp, 415)
    resp = await scim.send(
        "POST", "/Users", user_body("scu-json@example.com"), headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 201


async def test_trailing_slash_is_routed(scim: ScimClient) -> None:
    created = await _create(scim, "scu-slash@example.com")
    assert (await scim.get(f"/Users/{created['id']}/")).status_code == 200
    assert (await scim.get("/Users/")).status_code == 200


# ---------------------------------------------------------------------------
# List, filter, pagination
# ---------------------------------------------------------------------------


async def test_list_filters_and_pagination(scim: ScimClient, monkeypatch) -> None:
    for i in range(5):
        await _create(scim, f"scu-list{i}@example.com", externalId=f"ext-scu-{i}")

    resp = await scim.get("/Users")
    body = resp.json()
    assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
    assert body["totalResults"] == 5 and body["itemsPerPage"] == 5 and body["startIndex"] == 1

    page = (await scim.get("/Users", startIndex=2, count=2)).json()
    assert page["totalResults"] == 5 and page["itemsPerPage"] == 2 and page["startIndex"] == 2
    assert [r["userName"] for r in page["Resources"]] == ["scu-list1@example.com", "scu-list2@example.com"]

    assert (await scim.get("/Users", count=0)).json()["Resources"] == []
    assert (await scim.get("/Users", startIndex=0, count=1)).json()["startIndex"] == 1

    from specivo.core.config import get_settings

    monkeypatch.setattr(get_settings(), "scim_max_results", 3)
    capped = (await scim.get("/Users", count=1000)).json()
    assert capped["itemsPerPage"] == 3 and capped["totalResults"] == 5

    one = (await scim.get("/Users", filter='userName eq "SCU-LIST3@example.com"')).json()
    assert one["totalResults"] == 1 and one["Resources"][0]["userName"] == "scu-list3@example.com"
    ext = (await scim.get("/Users", filter='externalId eq "ext-scu-4"')).json()
    assert [r["userName"] for r in ext["Resources"]] == ["scu-list4@example.com"]
    email = (await scim.get("/Users", filter='emails[type eq "work"].value eq "scu-list2@example.com"')).json()
    assert email["totalResults"] == 1
    none = (await scim.get("/Users", filter='userName eq "nobody@example.com"')).json()
    assert none["totalResults"] == 0 and none["Resources"] == []


async def test_unsupported_filter_is_400(scim: ScimClient) -> None:
    _assert_error(await scim.get("/Users", filter='userName co "x"'), 400, "invalidFilter")
    _assert_error(await scim.get("/Users", filter='userName eq "x" or 1 eq 1'), 400, "invalidFilter")
    _assert_error(await scim.get("/Users", startIndex="abc"), 400, "invalidValue")


async def test_attributes_and_excluded_attributes(scim: ScimClient) -> None:
    created = await _create(scim, "scu-attrs@example.com")
    only = (await scim.get(f"/Users/{created['id']}", attributes="userName")).json()
    assert set(only) == {"schemas", "id", "meta", "userName"}
    without = (await scim.get(f"/Users/{created['id']}", excludedAttributes="emails")).json()
    assert "emails" not in without and "userName" in without


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def test_okta_put_user(scim: ScimClient, db_session: AsyncSession) -> None:
    created = await scim.post("/Users", fixture("okta_create_user.json"))
    assert created.status_code == 201, created.text
    scim_id = created.json()["id"]

    resp = await scim.put(f"/Users/{scim_id}", fixture("okta_put_user.json", id=scim_id))
    assert resp.status_code == 200, resp.text
    assert resp.json()["displayName"] == "Another User"


async def test_entra_patch_attributes(scim: ScimClient, db_session: AsyncSession) -> None:
    created = await _create(scim, "scu-entra-patch@example.com")
    resp = await scim.patch(f"/Users/{created['id']}", fixture("entra_patch_user_attributes.json"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["emails"][0]["value"] == "updatedEmail@example.com"
    assert body["displayName"] == "givenName updatedFamilyName"


async def test_put_changes_user_name_and_login(scim: ScimClient, db_session: AsyncSession) -> None:
    created = await _create(scim, "scu-rename", "scu-rename@example.com")
    resp = await scim.put(f"/Users/{created['id']}", user_body("scu-renamed", "scu-rename@example.com"))
    assert resp.status_code == 200, resp.text
    res = (await db_session.execute(select(ScimResource).where(ScimResource.scim_id == created["id"]))).scalar_one()
    user = await db_session.get(User, res.user_id)
    await db_session.refresh(user)
    assert user.login == "scu-renamed"


async def test_email_taken_by_another_account_is_409(scim: ScimClient, db_session: AsyncSession) -> None:
    await _user(db_session, email="scu-taken@example.com", is_admin=True, login="scutaken")
    created = await _create(scim, "scu-mover@example.com")
    resp = await scim.put(f"/Users/{created['id']}", user_body("scu-mover@example.com", "scu-taken@example.com"))
    _assert_error(resp, 409, "uniqueness")


# ---------------------------------------------------------------------------
# Linking existing accounts
# ---------------------------------------------------------------------------


async def test_links_existing_verified_account_by_email_and_keeps_its_credentials(
    scim: ScimClient, db_session: AsyncSession, client: AsyncClient
) -> None:
    existing = await _user(db_session, login="sculinkme", email="scu-link@example.com")
    await ApiKeyService().create_key(db_session, existing.id, "laptop")
    await _sign_in(client, "sculinkme")
    body = await _create(scim, "scu-link-other-name", "SCU-LINK@example.com")
    res = (await db_session.execute(select(ScimResource).where(ScimResource.scim_id == body["id"]))).scalar_one()
    assert res.user_id == existing.id
    await db_session.refresh(existing)
    assert existing.login == "sculinkme"  # an existing login is kept
    assert existing.password_hash is not None  # nothing about the password changes
    assert await _sessions(db_session, existing.id) == 1
    assert await _api_keys_active(db_session, existing.id) == 1


async def _squatter(db: AsyncSession, login: str, **fields) -> User:
    """An account holding an address it never proved, with credentials of its own."""
    user = await _user(db, login=login, email=f"{login}@example.com", email_verified_at=None, **fields)
    await ApiKeyService().create_key(db, user.id, "squatter")
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hashlib.sha256(login.encode()).hexdigest(),
            expires_at=utcnow() + timedelta(hours=1),
        )
    )
    other, _token = await make_scim_provider(db, f"scim-{login}")
    await link_identity(db, user, other, subject=f"sub-{login}")
    await db.flush()
    return user


async def _assert_wiped(db: AsyncSession, client: AsyncClient, user: User) -> None:
    await db.refresh(user)
    assert user.password_hash is None
    assert user.email_verified_at is not None and user.status == "active"
    assert await _sessions(db, user.id) == 0
    assert await _api_keys_active(db, user.id) == 0
    assert (await db.execute(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id))).first() is None
    assert (await db.execute(select(UserIdentity).where(UserIdentity.user_id == user.id))).first() is None
    resp = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert resp.status_code == 401


async def test_adopting_an_unverified_account_wipes_its_credentials(
    scim: ScimClient, db_session: AsyncSession, client: AsyncClient
) -> None:
    # Someone holds the person's address without having proved it, with a
    # password, a session, a key, a reset token and an SSO identity of their own.
    squatter = await _squatter(db_session, "scusquat")
    await _sign_in(client, "scusquat")
    assert await _sessions(db_session, squatter.id) == 1

    body = await _create(scim, "scusquat@example.com")

    res = (await db_session.execute(select(ScimResource).where(ScimResource.scim_id == body["id"]))).scalar_one()
    assert res.user_id == squatter.id  # adopted, so the real person can still be provisioned
    await _assert_wiped(db_session, client, squatter)


async def test_adopting_a_pending_account_wipes_its_credentials(
    scim: ScimClient, db_session: AsyncSession, client: AsyncClient
) -> None:
    pending = await _squatter(db_session, "scupending", status="pending_verification")
    await _create(scim, "scupending@example.com")
    await _assert_wiped(db_session, client, pending)


async def test_links_existing_account_by_exact_login_and_email(scim: ScimClient, db_session: AsyncSession) -> None:
    existing = await _user(db_session, login="sculogin", email="scu-login@example.com")
    body = await _create(scim, "sculogin", "SCU-LOGIN@example.com")
    res = (await db_session.execute(select(ScimResource).where(ScimResource.scim_id == body["id"]))).scalar_one()
    assert res.user_id == existing.id


async def test_login_of_an_account_with_another_email_is_409(scim: ScimClient, db_session: AsyncSession) -> None:
    # The same login says nothing about the same person: linking would hand the
    # account (and a password reset to the new address) to the IdP's user.
    existing = await _user(db_session, login="sculoginx", email="scu-login-old@example.com")
    resp = await scim.post("/Users", user_body("sculoginx", "scu-login-new@example.com"))
    _assert_error(resp, 409, "uniqueness")
    await db_session.refresh(existing)
    assert existing.email == "scu-login-old@example.com"
    assert (await db_session.execute(select(ScimResource).where(ScimResource.user_id == existing.id))).first() is None


async def test_email_local_part_never_links_a_login(scim: ScimClient, db_session: AsyncSession) -> None:
    other = await _user(db_session, login="sculocal", email="scu-someone-else@example.com")
    body = await _create(scim, "sculocal@example.com")
    res = (await db_session.execute(select(ScimResource).where(ScimResource.scim_id == body["id"]))).scalar_one()
    assert res.user_id != other.id
    user = await db_session.get(User, res.user_id)
    assert user.login == "sculocal-2"


@pytest.mark.parametrize(
    ("factory", "fields"),
    [
        (AdminUserFactory, {}),
        (ServiceAccountFactory, {}),
        (UserFactory, {"login": "$scusys", "password_hash": None}),
    ],
    ids=["admin", "service-account", "system-account"],
)
async def test_protected_accounts_are_never_linked(
    scim: ScimClient, db_session: AsyncSession, factory, fields: dict
) -> None:
    await _user(db_session, factory, email="scu-protected@example.com", **fields)
    resp = await scim.post("/Users", user_body("scu-protected-name", "scu-protected@example.com"))
    _assert_error(resp, 409, "uniqueness")


async def test_email_and_login_of_two_accounts_is_409(scim: ScimClient, db_session: AsyncSession) -> None:
    await _user(db_session, login="scutwo", email="scu-two-a@example.com")
    await _user(db_session, login="scutwo-b", email="scu-two-b@example.com")
    _assert_error(await scim.post("/Users", user_body("scutwo", "scu-two-b@example.com")), 409, "uniqueness")


async def test_account_of_another_provider_is_never_linked(
    scim: ScimClient, db_session: AsyncSession, client: AsyncClient
) -> None:
    _other, other_token = await make_scim_provider(db_session, "scim-users-b")
    other = ScimClient(client, "scim-users-b", other_token)
    await _create(other, "scu-shared@example.com")
    _assert_error(await scim.post("/Users", user_body("scu-shared-2", "scu-shared@example.com")), 409, "uniqueness")


# ---------------------------------------------------------------------------
# Deprovisioning
# ---------------------------------------------------------------------------


async def _linked_signed_in_user(
    scim: ScimClient, db_session: AsyncSession, client: AsyncClient, login: str
) -> tuple[User, str]:
    user = await _user(db_session, login=login, email=f"{login}@example.com")
    await ApiKeyService().create_key(db_session, user.id, "laptop")
    await _sign_in(client, login)
    assert await _sessions(db_session, user.id) == 1
    body = await _create(scim, f"{login}@example.com")
    return user, body["id"]


async def _api_keys_active(db: AsyncSession, user_id: int) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(ApiKey).where(ApiKey.user_id == user_id, ApiKey.is_active.is_(True))
        )
    )


@pytest.mark.parametrize("fixture_name", ["entra_patch_user_disable.json", "okta_patch_user_deactivate.json"])
async def test_patch_active_false_deprovisions(
    scim: ScimClient, db_session: AsyncSession, client: AsyncClient, fixture_name: str
) -> None:
    user, scim_id = await _linked_signed_in_user(scim, db_session, client, "scudeact")

    resp = await scim.patch(f"/Users/{scim_id}", fixture(fixture_name))

    assert resp.status_code == 200, resp.text
    assert resp.json()["active"] is False
    await db_session.refresh(user)
    assert user.status == "deactivated"
    assert await _sessions(db_session, user.id) == 0
    assert await _api_keys_active(db_session, user.id) == 0


async def test_put_active_false_then_true_reactivates_without_keys(
    scim: ScimClient, db_session: AsyncSession, client: AsyncClient
) -> None:
    user, scim_id = await _linked_signed_in_user(scim, db_session, client, "scuputdeact")
    off = await scim.put(f"/Users/{scim_id}", user_body("scuputdeact@example.com", active=False))
    assert off.status_code == 200 and off.json()["active"] is False
    await db_session.refresh(user)
    assert user.status == "deactivated" and await _sessions(db_session, user.id) == 0

    on = await scim.put(f"/Users/{scim_id}", user_body("scuputdeact@example.com", active=True))
    assert on.status_code == 200 and on.json()["active"] is True
    await db_session.refresh(user)
    assert user.status == "active"
    assert await _api_keys_active(db_session, user.id) == 0


async def test_delete_deactivates_keeps_account_and_answers_404(
    scim: ScimClient, db_session: AsyncSession, client: AsyncClient
) -> None:
    user, scim_id = await _linked_signed_in_user(scim, db_session, client, "scudelete")
    group = UserGroup(name="scu-delete-group")
    db_session.add(group)
    await db_session.flush()
    provider = (await db_session.execute(select(SsoProvider).where(SsoProvider.slug == scim.slug))).scalar_one()
    db_session.add(UserGroupMember(group_id=group.id, user_id=user.id, managed_by_provider_id=provider.id))
    await db_session.flush()

    resp = await scim.delete(f"/Users/{scim_id}")

    assert resp.status_code == 204
    await db_session.refresh(user)
    assert user.status == "deactivated"
    assert await _sessions(db_session, user.id) == 0
    assert await _api_keys_active(db_session, user.id) == 0
    assert await db_session.get(User, user.id) is not None
    assert (
        await db_session.scalar(
            select(func.count()).select_from(UserGroupMember).where(UserGroupMember.user_id == user.id)
        )
    ) == 0
    _assert_error(await scim.get(f"/Users/{scim_id}"), 404)
    _assert_error(await scim.delete(f"/Users/{scim_id}"), 404)
    listed = (await scim.get("/Users", filter='userName eq "scudelete@example.com"')).json()
    assert listed["totalResults"] == 0


async def test_recreate_after_delete_revives_the_account(scim: ScimClient, db_session: AsyncSession) -> None:
    first = await _create(scim, "scu-revive@example.com")
    assert (await scim.delete(f"/Users/{first['id']}")).status_code == 204
    second = await _create(scim, "scu-revive@example.com")
    assert second["id"] != first["id"] and second["active"] is True
    ids = (
        (
            await db_session.execute(
                select(ScimResource.user_id).where(ScimResource.scim_id.in_([first["id"], second["id"]]))
            )
        )
        .scalars()
        .all()
    )
    assert len(ids) == 1  # the same row, under a new id
    user = await db_session.get(User, ids[0])
    await db_session.refresh(user)
    assert user.status == "active"


async def test_administrator_is_never_deactivated_or_given_another_email(
    scim: ScimClient, db_session: AsyncSession, client: AsyncClient
) -> None:
    # Promoted by a Specivo admin after it was provisioned; another admin exists.
    created = await _create(scim, "scu-admin@example.com")
    res = (await db_session.execute(select(ScimResource).where(ScimResource.scim_id == created["id"]))).scalar_one()
    user = await db_session.get(User, res.user_id)
    await _user(db_session, AdminUserFactory)
    user.is_admin = True
    await db_session.flush()

    for resp in (
        await scim.patch(f"/Users/{created['id']}", fixture("entra_patch_user_disable.json")),
        await scim.put(f"/Users/{created['id']}", user_body("scu-admin@example.com", active=False)),
        await scim.delete(f"/Users/{created['id']}"),
        await scim.put(f"/Users/{created['id']}", user_body("scu-admin@example.com", "scu-admin-new@example.com")),
    ):
        body = _assert_error(resp, 400)
        assert "administrator" in body["detail"]
    await db_session.refresh(user)
    assert user.status == "active" and user.email == "scu-admin@example.com" and user.is_admin
    assert (await scim.get(f"/Users/{created['id']}")).status_code == 200
    # Other attributes still follow the IdP.
    renamed = await scim.put(f"/Users/{created['id']}", user_body("scu-admin@example.com", displayName="Admin Renamed"))
    assert renamed.status_code == 200, renamed.text
    await db_session.refresh(user)
    assert user.display_name == "Admin Renamed"


async def test_service_account_is_never_deactivated(scim: ScimClient, db_session: AsyncSession) -> None:
    created = await _create(scim, "scu-svc@example.com")
    res = (await db_session.execute(select(ScimResource).where(ScimResource.scim_id == created["id"]))).scalar_one()
    user = await db_session.get(User, res.user_id)
    user.is_service_account = True
    await db_session.flush()
    _assert_error(await scim.delete(f"/Users/{created['id']}"), 400)
    await db_session.refresh(user)
    assert user.status == "active"


async def test_account_deactivated_in_specivo_is_not_reactivated_by_scim(
    scim: ScimClient, db_session: AsyncSession, admin_client: AsyncClient
) -> None:
    created = await _create(scim, "scu-banned@example.com")
    res = (await db_session.execute(select(ScimResource).where(ScimResource.scim_id == created["id"]))).scalar_one()
    user = await db_session.get(User, res.user_id)
    resp = await admin_client.post(f"/api/v1/admin/users/{user.id}/deactivate/")
    assert resp.status_code == 200, resp.text

    for attempt in (
        await scim.put(f"/Users/{created['id']}", user_body("scu-banned@example.com", active=True)),
        await scim.patch(
            f"/Users/{created['id']}",
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "replace", "path": "active", "value": True}],
            },
        ),
    ):
        assert attempt.status_code == 200, attempt.text
        assert attempt.json()["active"] is False
    await db_session.refresh(user)
    assert user.status == "deactivated"

    # An administrator reactivates it; from then on SCIM manages it again.
    assert (await admin_client.post(f"/api/v1/admin/users/{user.id}/reactivate/")).status_code == 200
    off = await scim.put(f"/Users/{created['id']}", user_body("scu-banned@example.com", active=False))
    on = await scim.put(f"/Users/{created['id']}", user_body("scu-banned@example.com", active=True))
    assert off.json()["active"] is False and on.json()["active"] is True


async def test_linking_keeps_an_account_deactivated_in_specivo(scim: ScimClient, db_session: AsyncSession) -> None:
    existing = await _user(db_session, login="scubanlink", email="scu-banlink@example.com")
    await UserService().deactivate(db_session, existing, reason="admin", actor_id=None)
    body = await _create(scim, "scu-banlink@example.com")
    assert body["active"] is False
    await db_session.refresh(existing)
    assert existing.status == "deactivated"


async def test_scim_never_sets_admin_or_service_account(scim: ScimClient, db_session: AsyncSession) -> None:
    created = await _create(scim, "scu-noadmin@example.com")
    body = user_body("scu-noadmin@example.com", isAdmin=True, is_admin=True, is_service_account=True, password="x1y2z3")
    assert (await scim.put(f"/Users/{created['id']}", body)).status_code == 200
    res = (await db_session.execute(select(ScimResource).where(ScimResource.scim_id == created["id"]))).scalar_one()
    user = await db_session.get(User, res.user_id)
    assert not user.is_admin and not user.is_service_account and user.password_hash is None
