"""SCIM authentication, body cap, discovery documents and audit events.

A missing, wrong, revoked or replaced token, an unknown provider slug and a
provider with SCIM off all get the same 401 SCIM error, byte for byte.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.main import get_plugin_manager
from specivo.models.scim import ScimResource
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.sso import SsoProvider
from specivo.models.user import User
from specivo.services.scim.config_service import ScimConfigService
from specivo.services.security_audit_service import AuditEvent
from tests.integration.scim.conftest import SCIM_JSON, ScimClient, user_body

pytestmark = pytest.mark.integration


async def _unauthorized_body(scim: ScimClient) -> bytes:
    resp = await scim.get("/Users")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith(SCIM_JSON)
    assert resp.headers["www-authenticate"].lower().startswith("bearer")
    assert resp.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    return resp.content


async def test_all_authentication_failures_look_the_same(
    scim: ScimClient, db_session: AsyncSession, scim_provider: tuple[SsoProvider, str]
) -> None:
    provider, token = scim_provider
    assert (await scim.get("/Users")).status_code == 200

    bodies = {
        "missing": await _unauthorized_body(scim.other(token=None)),
        "wrong": await _unauthorized_body(scim.other(token=token + "x")),
        "api key shaped": await _unauthorized_body(scim.other(token="spv_" + token)),
        "unknown provider": await _unauthorized_body(scim.other(slug="scim-nobody", token=token)),
        "bad slug": await _unauthorized_body(scim.other(slug="NOT A SLUG", token=token)),
    }
    resp = await scim.client.get(scim.url("/Users"), headers={"Authorization": f"Basic {token}"})
    assert resp.status_code == 401
    bodies["basic"] = resp.content

    provider.scim_enabled = False
    await db_session.flush()
    bodies["scim off"] = await _unauthorized_body(scim)

    assert len(set(bodies.values())) == 1, bodies


async def test_disabled_provider_is_refused_like_any_other_failure(
    scim: ScimClient, db_session: AsyncSession, scim_provider: tuple[SsoProvider, str]
) -> None:
    provider, _token = scim_provider
    expected = await _unauthorized_body(scim.other(token="wrong"))
    provider.enabled = False
    await db_session.flush()
    assert provider.scim_enabled
    assert await _unauthorized_body(scim) == expected


async def test_rotated_token_replaces_the_old_one(
    scim: ScimClient, db_session: AsyncSession, scim_provider: tuple[SsoProvider, str]
) -> None:
    provider, old = scim_provider
    new = await ScimConfigService().generate_token(db_session, provider, actor=None)
    assert new != old and new.startswith("spvscim_")
    assert provider.scim_token_prefix == new[:14]
    assert (await scim.other(token=old).get("/Users")).status_code == 401
    assert (await scim.other(token=new).get("/Users")).status_code == 200


async def test_revoked_token_is_refused_and_scim_is_off(
    scim: ScimClient, db_session: AsyncSession, scim_provider: tuple[SsoProvider, str]
) -> None:
    provider, _token = scim_provider
    await ScimConfigService().revoke_token(db_session, provider, actor=None)
    assert provider.scim_enabled is False and provider.scim_token_hash is None
    assert (await scim.get("/Users")).status_code == 401


async def test_token_is_stored_hashed_and_last_use_recorded(
    scim: ScimClient, db_session: AsyncSession, scim_provider: tuple[SsoProvider, str]
) -> None:
    provider, token = scim_provider
    assert provider.scim_token_hash != token and len(provider.scim_token_hash or "") == 64
    assert provider.scim_last_used_at is None
    await scim.get("/ServiceProviderConfig")
    await db_session.refresh(provider)
    assert provider.scim_last_used_at is not None


async def test_body_cap(scim: ScimClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "scim_max_body_bytes", 2048)
    body = user_body("sca-big@example.com", displayName="x" * 4000)
    resp = await scim.post("/Users", body)
    assert resp.status_code == 413
    assert resp.json()["status"] == "413"


async def test_body_cap_while_streaming(scim: ScimClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "scim_max_body_bytes", 2048)

    async def chunks():
        for _ in range(10):
            yield b" " * 1024

    resp = await scim.client.post(
        scim.url("/Users"), content=chunks(), headers=scim._headers({"Content-Type": SCIM_JSON})
    )
    assert resp.status_code == 413


async def test_deeply_nested_body_is_400(scim: ScimClient) -> None:
    created = (await scim.post("/Users", user_body("sca-deep@example.com"))).json()
    deep = "[" * 900 + "]" * 900
    body = (
        '{"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],'
        ' "Operations": [{"op": "add", "path": "nickName", "value": ' + deep + "}]}"
    )
    resp = await scim.client.patch(
        scim.url(f"/Users/{created['id']}"), content=body, headers=scim._headers({"Content-Type": SCIM_JSON})
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["scimType"] == "invalidSyntax"


@pytest.mark.parametrize("start", ["99999999999999999999", "9223372036854775807"])
async def test_start_index_past_any_result_is_an_empty_page(scim: ScimClient, start: str) -> None:
    for path in ("/Users", "/Groups"):
        resp = await scim.get(path, startIndex=start)
        assert resp.status_code == 200, resp.text
        assert resp.json()["Resources"] == []


async def test_service_provider_config_and_schemas(scim: ScimClient) -> None:
    config = (await scim.get("/ServiceProviderConfig")).json()
    assert config["patch"]["supported"] is True
    assert config["bulk"]["supported"] is False
    assert config["filter"] == {"supported": True, "maxResults": get_settings().scim_max_results}
    assert config["changePassword"]["supported"] is False
    assert config["sort"]["supported"] is False
    assert config["etag"]["supported"] is False
    assert config["authenticationSchemes"][0]["type"] == "oauthbearertoken"

    types = (await scim.get("/ResourceTypes")).json()
    assert {t["id"] for t in types["Resources"]} == {"User", "Group"}
    assert (await scim.get("/ResourceTypes/User")).json()["endpoint"] == "/Users"
    assert (await scim.get("/ResourceTypes/Nope")).status_code == 404

    schemas = (await scim.get("/Schemas")).json()
    ids = {s["id"] for s in schemas["Resources"]}
    assert ids == {"urn:ietf:params:scim:schemas:core:2.0:User", "urn:ietf:params:scim:schemas:core:2.0:Group"}
    user_schema = (await scim.get("/Schemas/urn:ietf:params:scim:schemas:core:2.0:User")).json()
    assert "userName" in {a["name"] for a in user_schema["attributes"]}


async def test_discovery_needs_the_token_too(scim: ScimClient) -> None:
    assert (await scim.other(token=None).get("/ServiceProviderConfig")).status_code == 401


async def test_cookie_session_is_not_accepted(client: AsyncClient, admin_client: AsyncClient, scim: ScimClient) -> None:
    # An admin's session reaches nothing: the SCIM token is the only credential.
    resp = await admin_client.get(scim.url("/Users"))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_on() -> Iterator[None]:
    """Register the enterprise audit feature (the autouse snapshot fixture removes it again)."""
    get_plugin_manager().feature_registry.register("security_audit_log", "test")
    yield


async def _audit_rows(db: AsyncSession, prefix: str) -> list[SecurityAuditLog]:
    rows = await db.execute(
        select(SecurityAuditLog).where(SecurityAuditLog.event_type.like(f"{prefix}%")).order_by(SecurityAuditLog.id)
    )
    return list(rows.scalars().all())


async def test_audit_events(
    audit_on: None, scim: ScimClient, db_session: AsyncSession, scim_provider: tuple[SsoProvider, str]
) -> None:
    provider, token = scim_provider
    created = (await scim.post("/Users", user_body("sca-audit@example.com"))).json()
    await scim.put(f"/Users/{created['id']}", user_body("sca-audit@example.com", displayName="Renamed"))
    await scim.delete(f"/Users/{created['id']}")
    group = (
        await scim.post("/Groups", {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"], "displayName": "sca-g"})
    ).json()
    await scim.delete(f"/Groups/{group['id']}")

    rows = await _audit_rows(db_session, "scim_")
    kinds = [r.event_type for r in rows]
    assert kinds == [
        AuditEvent.SCIM_USER_CREATED.value,
        AuditEvent.SCIM_USER_UPDATED.value,
        AuditEvent.SCIM_USER_DEPROVISIONED.value,
        AuditEvent.SCIM_GROUP_CREATED.value,
        AuditEvent.SCIM_GROUP_DELETED.value,
    ]
    assert all(r.user_id is None for r in rows)
    assert all(r.details["source"] == "scim" and r.details["provider"] == provider.slug for r in rows)
    assert rows[1].details["fields"] == ["displayName"]
    assert rows[2].details["action"] == "deleted"
    user_id = rows[0].details["user_id"]
    deprovisioned = [r for r in await _audit_rows(db_session, "user_deprovisioned") if r.details["user_id"] == user_id]
    assert [r.details["reason"] for r in deprovisioned] == ["scim_deprovision"]
    assert token not in repr([r.details for r in rows])


async def test_refusal_is_audited(
    audit_on: None, scim: ScimClient, db_session: AsyncSession, scim_provider: tuple[SsoProvider, str]
) -> None:
    provider, _token = scim_provider
    created = (await scim.post("/Users", user_body("sca-refused@example.com"))).json()
    res = (await db_session.execute(select(ScimResource).where(ScimResource.scim_id == created["id"]))).scalar_one()
    user = await db_session.get(User, res.user_id)
    user.is_admin = True
    await db_session.flush()

    assert (await scim.delete(f"/Users/{created['id']}")).status_code == 400

    [row] = await _audit_rows(db_session, AuditEvent.SCIM_REQUEST_REFUSED.value)
    assert row.details["reason"] == "administrator"
    assert row.details["user_id"] == user.id and row.details["provider"] == provider.slug


async def test_adopting_an_unverified_account_is_audited(
    audit_on: None, scim: ScimClient, db_session: AsyncSession, scim_provider: tuple[SsoProvider, str]
) -> None:
    squatter = User(login="scasquat", email="sca-squat@example.com", display_name="Squatter", password_hash="x")
    db_session.add(squatter)
    await db_session.flush()
    assert (await scim.post("/Users", user_body("sca-squat@example.com"))).status_code == 201
    [row] = await _audit_rows(db_session, AuditEvent.SCIM_USER_CREATED.value)
    assert row.details["action"] == "adopted_unverified" and row.details["user_id"] == squatter.id
