"""SCIM ``/Groups`` over HTTP: CRUD, membership PATCH (Entra ID and Okta forms), isolation.

Group names and logins carry ``scg`` so parallel modules never collide.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member
from specivo.models.scim import ScimResource
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.testing.factories.user import UserFactory
from tests.factories.project import ProjectFactory
from tests.integration.scim.conftest import ScimClient, fixture, make_scim_provider, user_body

pytestmark = pytest.mark.integration

GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"


async def _user_id(scim: ScimClient, user_name: str) -> str:
    resp = await scim.post("/Users", user_body(user_name))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _group(scim: ScimClient, name: str, members: list[str] | None = None) -> dict:
    body = {"schemas": [GROUP_SCHEMA], "displayName": name}
    if members is not None:
        body["members"] = [{"value": m} for m in members]
    resp = await scim.post("/Groups", body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _specivo_group(db: AsyncSession, scim_id: str) -> UserGroup:
    res = (await db.execute(select(ScimResource).where(ScimResource.scim_id == scim_id))).scalar_one()
    group = await db.get(UserGroup, res.user_group_id)
    await db.refresh(group)
    return group


async def _member_rows(db: AsyncSession, group_id: int) -> list[UserGroupMember]:
    rows = await db.execute(select(UserGroupMember).where(UserGroupMember.group_id == group_id))
    return list(rows.scalars().all())


def _values(body: dict) -> list[str]:
    return sorted(m["value"] for m in body.get("members", []))


async def test_entra_create_group(scim: ScimClient, db_session: AsyncSession) -> None:
    body = fixture("entra_create_group.json")
    body["displayName"] = "scg-entra"
    resp = await scim.post("/Groups", body)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["displayName"] == "scg-entra"
    assert created["externalId"] == "8aa1a0c0-c4c3-4bc0-b4a5-2ef676900159"
    assert created["members"] == []
    assert created["meta"]["resourceType"] == "Group"
    group = await _specivo_group(db_session, created["id"])
    assert group.name == "scg-entra"


async def test_okta_create_group_with_members(scim: ScimClient, db_session: AsyncSession) -> None:
    alice = await _user_id(scim, "scg-alice@example.com")
    body = fixture("okta_create_group.json")
    body["displayName"] = "scg-okta"
    body["members"] = [{"value": alice, "display": "scg-alice@example.com"}]
    resp = await scim.post("/Groups", body)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert _values(created) == [alice]
    assert created["members"][0]["display"] == "Name of scg-alice@example.com"
    group = await _specivo_group(db_session, created["id"])
    rows = await _member_rows(db_session, group.id)
    assert len(rows) == 1 and rows[0].managed_by_provider_id is not None


async def test_name_of_an_existing_group_is_409(scim: ScimClient, db_session: AsyncSession) -> None:
    db_session.add(UserGroup(name="scg-Existing"))
    await db_session.flush()
    resp = await scim.post("/Groups", {"schemas": [GROUP_SCHEMA], "displayName": "SCG-existing"})
    assert resp.status_code == 409 and resp.json()["scimType"] == "uniqueness"


async def test_unknown_member_is_400(scim: ScimClient, db_session: AsyncSession) -> None:
    local = UserFactory.build(login="scglocal", email="scg-local@example.com")
    db_session.add(local)
    await db_session.flush()
    for value in ("00000000-0000-0000-0000-000000000000", str(local.id)):
        resp = await scim.post(
            "/Groups", {"schemas": [GROUP_SCHEMA], "displayName": "scg-bad", "members": [{"value": value}]}
        )
        assert resp.status_code == 400 and resp.json()["scimType"] == "invalidValue"


async def test_get_list_filter_and_excluded_members(scim: ScimClient) -> None:
    alice = await _user_id(scim, "scg-list-alice@example.com")
    created = await _group(scim, "scg-list-a", [alice])
    await _group(scim, "scg-list-b")

    listed = (await scim.get("/Groups")).json()
    assert listed["totalResults"] == 2
    one = (await scim.get("/Groups", filter='displayName eq "SCG-LIST-A"')).json()
    assert [g["id"] for g in one["Resources"]] == [created["id"]]
    assert _values(one["Resources"][0]) == [alice]
    lean = (await scim.get("/Groups", excludedAttributes="members")).json()
    assert all("members" not in g for g in lean["Resources"])
    single = (await scim.get(f"/Groups/{created['id']}", excludedAttributes="members")).json()
    assert "members" not in single and single["displayName"] == "scg-list-a"
    assert (await scim.get(f"/Groups/{created['id']}")).json()["members"][0]["value"] == alice
    assert (await scim.get("/Groups", filter='members[value eq "x"]')).status_code == 400


async def test_entra_patch_add_and_remove_member(scim: ScimClient, db_session: AsyncSession) -> None:
    alice = await _user_id(scim, "scg-ea@example.com")
    bob = await _user_id(scim, "scg-eb@example.com")
    created = await _group(scim, "scg-entra-members", [alice])
    group = await _specivo_group(db_session, created["id"])

    resp = await scim.patch(f"/Groups/{created['id']}", fixture("entra_patch_group_add_member.json", member_id=bob))
    assert resp.status_code == 204, resp.text
    assert _values((await scim.get(f"/Groups/{created['id']}")).json()) == sorted([alice, bob])

    resp = await scim.patch(
        f"/Groups/{created['id']}", fixture("entra_patch_group_remove_member.json", member_id=alice)
    )
    assert resp.status_code == 204, resp.text
    assert _values((await scim.get(f"/Groups/{created['id']}")).json()) == [bob]
    assert len(await _member_rows(db_session, group.id)) == 1


async def test_okta_patch_members_and_rename(scim: ScimClient, db_session: AsyncSession) -> None:
    alice = await _user_id(scim, "scg-oa@example.com")
    bob = await _user_id(scim, "scg-ob@example.com")
    created = await _group(scim, "scg-okta-members", [alice])

    resp = await scim.patch(
        f"/Groups/{created['id']}", fixture("okta_patch_group_members.json", remove_id=alice, add_id=bob)
    )
    assert resp.status_code == 204, resp.text
    assert _values((await scim.get(f"/Groups/{created['id']}")).json()) == [bob]

    resp = await scim.patch(f"/Groups/{created['id']}", fixture("okta_patch_group_rename.json", id=created["id"]))
    assert resp.status_code == 204, resp.text
    group = await _specivo_group(db_session, created["id"])
    assert group.name == "Test SCIMv2 renamed"
    assert _values((await scim.get(f"/Groups/{created['id']}")).json()) == [bob]


async def test_patch_adding_unknown_member_is_400_and_changes_nothing(scim: ScimClient) -> None:
    alice = await _user_id(scim, "scg-pu@example.com")
    created = await _group(scim, "scg-patch-unknown", [alice])
    resp = await scim.patch(
        f"/Groups/{created['id']}",
        fixture("entra_patch_group_add_member.json", member_id="00000000-0000-0000-0000-000000000000"),
    )
    assert resp.status_code == 400
    assert _values((await scim.get(f"/Groups/{created['id']}")).json()) == [alice]


async def test_put_replaces_members_only_when_sent(scim: ScimClient) -> None:
    alice = await _user_id(scim, "scg-put-a@example.com")
    bob = await _user_id(scim, "scg-put-b@example.com")
    created = await _group(scim, "scg-put", [alice])

    renamed = await scim.put(f"/Groups/{created['id']}", {"schemas": [GROUP_SCHEMA], "displayName": "scg-put-2"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["displayName"] == "scg-put-2" and _values(renamed.json()) == [alice]

    replaced = await scim.put(
        f"/Groups/{created['id']}",
        {"schemas": [GROUP_SCHEMA], "displayName": "scg-put-2", "members": [{"value": bob}]},
    )
    assert _values(replaced.json()) == [bob]


async def test_manual_membership_is_never_touched(scim: ScimClient, db_session: AsyncSession) -> None:
    alice = await _user_id(scim, "scg-manual@example.com")
    created = await _group(scim, "scg-manual")
    group = await _specivo_group(db_session, created["id"])
    res = (await db_session.execute(select(ScimResource).where(ScimResource.scim_id == alice))).scalar_one()
    db_session.add(UserGroupMember(group_id=group.id, user_id=res.user_id, managed_by_provider_id=None))
    await db_session.flush()

    # Adding keeps the hand-made row, removing leaves it.
    await scim.patch(f"/Groups/{created['id']}", fixture("entra_patch_group_add_member.json", member_id=alice))
    await scim.patch(f"/Groups/{created['id']}", fixture("entra_patch_group_remove_member.json", member_id=alice))
    rows = await _member_rows(db_session, group.id)
    assert len(rows) == 1 and rows[0].managed_by_provider_id is None


async def test_delete_group_removes_memberships_and_project_access(scim: ScimClient, db_session: AsyncSession) -> None:
    alice = await _user_id(scim, "scg-del@example.com")
    created = await _group(scim, "scg-delete", [alice])
    group = await _specivo_group(db_session, created["id"])
    project = ProjectFactory.build(key="SCGDEL", identifier="scgdel", name="SCG delete")
    db_session.add(project)
    await db_session.flush()
    db_session.add(Member(project_id=project.id, group_id=group.id))
    await db_session.flush()

    resp = await scim.delete(f"/Groups/{created['id']}")

    assert resp.status_code == 204
    assert (await scim.get(f"/Groups/{created['id']}")).status_code == 404
    db_session.expunge_all()
    assert await db_session.get(UserGroup, group.id) is None
    count = await db_session.scalar(select(func.count()).select_from(Member).where(Member.group_id == group.id))
    assert count == 0
    # The user stays.
    res = (await db_session.execute(select(ScimResource).where(ScimResource.scim_id == alice))).scalar_one()
    assert (await db_session.get(User, res.user_id)).status == "active"


async def test_provider_isolation(scim: ScimClient, db_session: AsyncSession, client: AsyncClient) -> None:
    _other, token_b = await make_scim_provider(db_session, "scim-groups-b")
    other = ScimClient(client, "scim-groups-b", token_b)
    alice = await _user_id(scim, "scg-iso@example.com")
    group = await _group(scim, "scg-iso", [alice])

    # Provider B sees neither A's user nor A's group, and cannot use A's user as a member.
    assert (await other.get(f"/Users/{alice}")).status_code == 404
    assert (await other.get(f"/Groups/{group['id']}")).status_code == 404
    assert (await other.patch(f"/Users/{alice}", fixture("entra_patch_user_disable.json"))).status_code == 404
    assert (await other.delete(f"/Users/{alice}")).status_code == 404
    assert (await other.delete(f"/Groups/{group['id']}")).status_code == 404
    assert (await other.get("/Users")).json()["totalResults"] == 0
    resp = await other.post(
        "/Groups", {"schemas": [GROUP_SCHEMA], "displayName": "scg-iso-b", "members": [{"value": alice}]}
    )
    assert resp.status_code == 400
    # A's token on B's URL is refused.
    assert (await scim.other(slug="scim-groups-b").get("/Users")).status_code == 401
    assert (await scim.get(f"/Users/{alice}")).json()["active"] is True
