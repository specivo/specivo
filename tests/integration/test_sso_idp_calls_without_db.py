"""The SSO sign-in asks the IdP with no database transaction open.

Every IdP call (discovery, token endpoint, JWKS, userinfo, the refresh-token
probe) can take up to ``SSO_HTTP_TIMEOUT_SECONDS``. A sign-in that held its
pooled connection (and an open transaction) across them would let a slow IdP
drain the pool for every other request. The routes therefore end the read
transaction before the first call, and only start writing once the IdP has
answered.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoProvider
from specivo.services.sso.discovery import invalidate_provider_cache
from specivo.services.sso.http import override_transport
from specivo.testing.factories.user import UserFactory
from specivo.testing.oidc_mock import MockIdP
from specivo.testing.sso import SsoLoginDriver, link_identity

pytestmark = pytest.mark.integration


class _IdpRecorder(httpx.AsyncBaseTransport):
    """Passes every request to ``idp`` and records it as an ``idp:<path>`` step."""

    def __init__(self, idp: MockIdP, steps: list[str]) -> None:
        self.idp = idp
        self.steps = steps

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.steps.append(f"idp:{request.url.path}")
        return self.idp.transport.handle_request(request)


def _record_db_steps(monkeypatch: pytest.MonkeyPatch, steps: list[str]) -> None:
    """Record every statement a session runs as ``db`` and every commit as ``commit``."""
    for name in ("execute", "scalar", "scalars", "get", "flush", "refresh"):
        original = getattr(AsyncSession, name)

        def _wrap(original=original):
            async def _recorded(self, *args, **kwargs):
                steps.append("db")
                return await original(self, *args, **kwargs)

            return _recorded

        monkeypatch.setattr(AsyncSession, name, _wrap())

    commit = AsyncSession.commit

    async def _commit(self):
        result = await commit(self)
        steps.append("commit")
        return result

    monkeypatch.setattr(AsyncSession, "commit", _commit)


def _idp_calls_inside_a_transaction(steps: list[str]) -> list[str]:
    """The IdP calls made after a statement that no commit has ended yet."""
    open_transaction = False
    offenders = []
    for step in steps:
        if step == "db":
            open_transaction = True
        elif step == "commit":
            open_transaction = False
        elif open_transaction:
            offenders.append(step)
    return offenders


async def test_sign_in_holds_no_transaction_while_the_idp_answers(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every kind of IdP call: discovery and JWKS (cache cleared), the token
    # endpoint, userinfo, and the refresh-token probe (verify_session_at_idp).
    sso_provider.claim_config = {"use_userinfo": True}
    assert sso_provider.verify_session_at_idp
    user = UserFactory.build()
    db_session.add(user)
    await db_session.flush()
    await link_identity(db_session, user, sso_provider, subject=mock_idp.default_sub)
    await db_session.commit()
    await invalidate_provider_cache(sso_provider.id)

    steps: list[str] = []
    _record_db_steps(monkeypatch, steps)
    with override_transport(_IdpRecorder(mock_idp, steps)):
        response = await sso_login(client)

    assert response.status_code == 302, response.text
    assert response.headers["location"] == "/"
    idp_paths = {s for s in steps if s.startswith("idp:")}
    # discovery, JWKS, token (code and refresh grants), userinfo
    assert len(idp_paths) >= 4, steps
    assert mock_idp.calls["userinfo"] == 1
    assert _idp_calls_inside_a_transaction(steps) == [], steps

    monkeypatch.undo()
    row = (await db_session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))).scalar_one()
    assert row.auth_method == "sso"
    assert row.idp_refresh_token_encrypted  # the probe's answer still reached the session
    await db_session.refresh(sso_provider)
    assert sso_provider.session_recheck_status == "ok"
