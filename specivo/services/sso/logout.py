"""Single sign-on logout.

RP-initiated logout
    The user signs out of Specivo. The caller reads the session with
    :func:`find_sso_logout_session` *before* revoking it (the refresh row,
    which carries the encrypted ID token, is deleted by the revocation), then
    revokes it as for any session, then asks :func:`end_session_url` where to
    send the browser. When the session came from an enabled provider with
    ``rp_logout_enabled`` whose discovery document advertises an
    ``end_session_endpoint``, that is the IdP's end-session URL with
    ``id_token_hint``, ``client_id`` and the fixed ``post_logout_redirect_uri``
    (:func:`post_logout_redirect_uri`, never taken from the request).
    Otherwise it is ``None`` and the caller signs out locally to
    :data:`LOGGED_OUT_PATH`. The IdP never blocks sign-out: an unreachable or
    broken discovery document only means a local sign-out.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.config import Settings, get_settings
from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.services.auth_service import ID_TOKEN_PURPOSE
from specivo.services.sso.discovery import get_provider_metadata
from specivo.services.sso.errors import SsoError
from specivo.services.sso.log import logger
from specivo.services.sso.provisioning import subject_hash

__all__ = [
    "LOGGED_OUT_PATH",
    "SsoLogoutSession",
    "end_session_url",
    "find_sso_logout_session",
    "post_logout_redirect_uri",
]

#: Where a signed-out browser lands, locally or back from the IdP.
LOGGED_OUT_PATH = "/login/?logged_out=1"


@dataclass(frozen=True, slots=True)
class SsoLogoutSession:
    """What RP-initiated logout needs from an SSO session, read before it is revoked."""

    provider: SsoProvider
    subject: str
    id_token_encrypted: str | None = field(repr=False)


def post_logout_redirect_uri(settings: Settings | None = None) -> str:
    """The fixed ``post_logout_redirect_uri``: ``{APP_URL}/login/?logged_out=1``."""
    settings = settings or get_settings()
    return settings.app_url.rstrip("/") + LOGGED_OUT_PATH


async def find_sso_logout_session(
    db: AsyncSession,
    *,
    refresh_token_raw: str | None,
    access_token_raw: str | None,
) -> SsoLogoutSession | None:
    """The SSO session a client holds, or ``None`` (password session, no session).

    The refresh token names its row directly. Without one, the ``sid`` of a
    still-valid access token names the session; an expired access token is
    ignored, by the same rule as ``AuthService.end_browser_session``.
    """
    row: RefreshToken | None = None
    if refresh_token_raw:
        token_hash = hashlib.sha256(refresh_token_raw.encode()).hexdigest()
        row = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    if row is None and access_token_raw:
        session_id = _valid_access_session(access_token_raw)
        if session_id is not None:
            row = await db.scalar(
                select(RefreshToken)
                .where(RefreshToken.session_id == session_id)
                .order_by(RefreshToken.id.desc())
                .limit(1)
            )
    if row is None or row.auth_method != "sso" or row.user_identity_id is None:
        return None

    found = (
        await db.execute(
            select(UserIdentity.subject, SsoProvider)
            .join(SsoProvider, SsoProvider.id == UserIdentity.provider_id)
            .where(UserIdentity.id == row.user_identity_id)
        )
    ).first()
    if found is None:
        return None
    subject, provider = found
    return SsoLogoutSession(provider=provider, subject=subject, id_token_encrypted=row.id_token_encrypted)


async def end_session_url(target: SsoLogoutSession, *, settings: Settings | None = None) -> str | None:
    """The IdP end-session URL for ``target``, or ``None`` to sign out locally.

    Logs ``sso.logout`` either way. ``id_token_hint`` is left out when the
    stored token is missing or unreadable (a rotated ``SECRET_KEY``); the
    request then identifies the client by ``client_id`` alone.
    """
    settings = settings or get_settings()
    provider = target.provider
    url = await _build_url(target, settings) if provider.enabled and provider.rp_logout_enabled else None
    logger.info(
        "sso.logout provider=%s sub=%s idp_redirect=%s",
        provider.slug,
        subject_hash(target.subject),
        "true" if url else "false",
    )
    return url


async def _build_url(target: SsoLogoutSession, settings: Settings) -> str | None:
    provider = target.provider
    try:
        metadata = await get_provider_metadata(provider, settings=settings)
    except SsoError as exc:
        logger.warning("sso.logout.discovery_failed provider=%s code=%s", provider.slug, exc.code)
        return None
    except Exception as exc:  # noqa: BLE001 - sign-out must never fail because of the IdP
        logger.warning("sso.logout.discovery_failed provider=%s error=%s", provider.slug, type(exc).__name__)
        return None
    endpoint = metadata.end_session_endpoint
    if not endpoint:
        return None

    params: dict[str, str] = {}
    hint = _id_token_hint(target)
    if hint:
        params["id_token_hint"] = hint
    params["client_id"] = provider.client_id
    params["post_logout_redirect_uri"] = post_logout_redirect_uri(settings)
    return f"{endpoint}{'&' if '?' in endpoint else '?'}{urlencode(params)}"


def _id_token_hint(target: SsoLogoutSession) -> str | None:
    if not target.id_token_encrypted:
        return None
    try:
        return secret_box.decrypt(target.id_token_encrypted, ID_TOKEN_PURPOSE)
    except secret_box.SecretUnreadableError:
        logger.info("sso.logout.id_token_unreadable provider=%s", target.provider.slug)
        return None


def _valid_access_session(access_token_raw: str) -> uuid.UUID | None:
    from specivo.core.security import access_token_session

    access = access_token_session(access_token_raw)
    if access is None or access.expired:
        return None
    return access.session_id
