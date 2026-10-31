"""SCIM provisioning from the disposable authentik (``make sso-authentik-up``).

The authentik blueprint (``tests/fixtures/sso/authentik/``) has a SCIM provider
that pushes the members of ``specivo-scim`` to
``http://host.docker.internal:9945/scim/v2/authentik``. This test runs a small
HTTP forwarder on that port that hands each request to the in-process app (on
the test database), then:

1. creates a user in authentik, in ``specivo-scim``: the account appears in
   Specivo, with the email marked verified; a user created next to it outside
   ``specivo-scim`` never does (the SCIM application is bound to ``specivo-scim``);
2. creates a group with that user: the user group appears, with the member;
3. gives the account an SSO session and an API key, deactivates the user in
   authentik: the account is deactivated, the session ends, the key is off.

The forwarder and the test share one database connection, so both take
``app_lock`` before using it. Needs Docker on the machine running the tests
(the worker reaches the host through ``host.docker.internal``); skipped when
authentik is not up.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.auth import ApiKey, RefreshToken
from specivo.models.scim import ScimResource
from specivo.models.sso import SsoProvider
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.services.api_key_service import ApiKeyService
from specivo.services.auth_service import AuthService
from specivo.services.scim.config_service import hash_scim_token
from specivo.testing.sso import link_identity
from tests.conftest import _issuer_is_up
from tests.fixtures.sso import seed
from tests.integration.sso_idp.conftest import build_idp_provider
from tests.integration.sso_idp.idps import Authentik, _username

pytestmark = [pytest.mark.sso_idp, pytest.mark.integration]

#: How long authentik's worker may take to push a change.
SYNC_TIMEOUT = 90.0


class ScimForwarder:
    """Receives authentik's SCIM requests on the host and replays them into the in-process app."""

    def __init__(self, app: httpx.AsyncClient, loop: asyncio.AbstractEventLoop, lock: asyncio.Lock) -> None:
        self.app = app
        self.loop = loop
        self.lock = lock
        self.requests: list[tuple[str, str, int]] = []
        forwarder = self

        class Handler(BaseHTTPRequestHandler):
            def _forward(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")}
                future = asyncio.run_coroutine_threadsafe(
                    forwarder._send(self.command, self.path, headers, body), forwarder.loop
                )
                status, out_headers, content = future.result(timeout=30)
                forwarder.requests.append((self.command, self.path, status))
                self.send_response(status)
                for key, value in out_headers.items():
                    if key.lower() not in ("content-length", "transfer-encoding", "connection"):
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _forward  # noqa: N815 - BaseHTTPRequestHandler API

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - BaseHTTPRequestHandler API
                return

        self.server = ThreadingHTTPServer(("0.0.0.0", seed.SCIM_PORT), Handler)  # noqa: S104 - the IdP container calls in
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    async def _send(self, method: str, path: str, headers: dict[str, str], body: bytes | None):
        async with self.lock:
            resp = await self.app.request(method, path, headers=headers, content=body)
        return resp.status_code, dict(resp.headers), resp.content

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def authentik() -> Authentik:
    driver = Authentik()
    if not _issuer_is_up(driver.issuer):
        pytest.skip(f"authentik test IdP is not serving {driver.issuer} (start it with: make sso-authentik-up)")
    return driver


@pytest_asyncio.fixture
async def scim_provider(db_session: AsyncSession, authentik: Authentik) -> SsoProvider:
    provider = build_idp_provider(authentik)
    provider.scim_token_hash = hash_scim_token(seed.SCIM_TOKEN)
    provider.scim_token_prefix = seed.SCIM_TOKEN[:14]
    provider.scim_enabled = True
    db_session.add(provider)
    await db_session.flush()
    return provider


@pytest_asyncio.fixture
async def forwarder(sso_app: httpx.AsyncClient, scim_provider: SsoProvider) -> AsyncIterator[ScimForwarder]:
    running = ScimForwarder(sso_app, asyncio.get_running_loop(), asyncio.Lock())
    running.start()
    try:
        yield running
    finally:
        running.stop()


def _admin() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{seed.AUTHENTIK_BASE_URL}/api/v3",
        headers={"Authorization": f"Bearer {seed.AUTHENTIK_API_TOKEN}"},
        timeout=10.0,
    )


async def _group_pk(admin: httpx.AsyncClient, name: str) -> str:
    found = await admin.get("/core/groups/", params={"name": name})
    [group] = found.json()["results"]
    return str(group["pk"])


async def _wait_for(forwarder: ScimForwarder, check: Callable[[], Awaitable[Any]], what: str) -> Any:
    """Poll ``check`` (under the forwarder's lock) until it returns something truthy."""
    deadline = time.monotonic() + SYNC_TIMEOUT
    while time.monotonic() < deadline:
        async with forwarder.lock:
            result = await check()
        if result:
            return result
        await asyncio.sleep(1.0)
    raise AssertionError(f"authentik never provisioned {what}; SCIM requests seen: {forwarder.requests}")


async def test_authentik_provisions_and_deprovisions(
    db_session: AsyncSession, scim_provider: SsoProvider, forwarder: ScimForwarder
) -> None:
    username = _username()
    email = f"{username}@example.com"
    async with _admin() as admin:
        scim_group = await _group_pk(admin, seed.GROUP_SCIM)
        created = await admin.post(
            "/core/users/",
            json={"username": username, "name": "SCIM Test", "email": email, "path": "users", "is_active": True},
        )
        assert created.status_code == 201, created.text
        user_pk = created.json()["pk"]
        outsider_name = f"{username}-out"
        outsider = await admin.post(
            "/core/users/",
            json={
                "username": outsider_name,
                "name": "Not Provisioned",
                "path": "users",
                "is_active": True,
                "email": f"{outsider_name}@example.com",
            },
        )
        assert outsider.status_code == 201, outsider.text
        group_pk: str | None = None
        try:
            added = await admin.post(f"/core/groups/{scim_group}/add_user/", json={"pk": user_pk})
            assert added.status_code == 204, added.text

            async def provisioned() -> User | None:
                return (
                    await db_session.execute(
                        select(User)
                        .join(ScimResource, ScimResource.user_id == User.id)
                        .where(ScimResource.provider_id == scim_provider.id, func.lower(User.email) == email)
                    )
                ).scalar_one_or_none()

            account = await _wait_for(forwarder, provisioned, "the user")
            assert account.status == "active" and account.email_verified_at is not None
            assert account.password_hash is None and not account.is_admin

            group_name = f"scim-{username}"
            made = await admin.post("/core/groups/", json={"name": group_name, "users": [user_pk]})
            assert made.status_code == 201, made.text
            group_pk = str(made.json()["pk"])

            async def group_with_member() -> bool:
                group = (
                    await db_session.execute(select(UserGroup).where(UserGroup.name == group_name))
                ).scalar_one_or_none()
                if group is None:
                    return False
                member = await db_session.scalar(
                    select(func.count())
                    .select_from(UserGroupMember)
                    .where(UserGroupMember.group_id == group.id, UserGroupMember.user_id == account.id)
                )
                return bool(member)

            await _wait_for(forwarder, group_with_member, "the group and its member")

            async with forwarder.lock:
                identity = await link_identity(db_session, account, scim_provider, subject=created.json()["uid"])
                await AuthService().create_sso_session(
                    db_session,
                    user=account,
                    identity=identity,
                    provider=scim_provider,
                    id_token="not-a-real-token",
                    idp_sid=None,
                )
                await ApiKeyService().create_key(db_session, account.id, "scim-test")

            disabled = await admin.patch(f"/core/users/{user_pk}/", json={"is_active": False})
            assert disabled.status_code == 200, disabled.text

            async def deactivated() -> bool:
                await db_session.refresh(account)
                return account.status == "deactivated"

            await _wait_for(forwarder, deactivated, "the deactivation")
            async with forwarder.lock:
                sessions = await db_session.scalar(
                    select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == account.id)
                )
                keys = await db_session.scalar(
                    select(func.count())
                    .select_from(ApiKey)
                    .where(ApiKey.user_id == account.id, ApiKey.is_active.is_(True))
                )
            assert sessions == 0
            assert keys == 0

            # Only members of specivo-scim are pushed: the outsider (created
            # before the deactivation was synced) never reached Specivo.
            async with forwarder.lock:
                provisioned_names = (
                    (
                        await db_session.execute(
                            select(ScimResource.user_name).where(
                                ScimResource.provider_id == scim_provider.id, ScimResource.resource_type == "User"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            assert outsider_name not in provisioned_names, forwarder.requests
        finally:
            await admin.delete(f"/core/users/{outsider.json()['pk']}/")
            if group_pk is not None:
                await admin.delete(f"/core/groups/{group_pk}/")
            await admin.delete(f"/core/users/{user_pk}/")
