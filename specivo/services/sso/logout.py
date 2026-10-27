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
    :func:`logged_out_path`. The IdP never blocks sign-out: an unreachable or
    broken discovery document only means a local sign-out.

Back-channel logout
    The IdP ends sessions server to server (OpenID Connect Back-Channel
    Logout 1.0). The route hands the raw ``logout_token`` to
    :func:`backchannel_logout`, which

    1. validates it with :func:`~specivo.services.sso.id_token.verify_logout_token`
       (signature via JWKS, ``iss``, ``aud``, ``iat``/``exp``, ``jti``,
       ``events``, ``sid`` or ``sub``, no ``nonce``, ``typ``);
    2. claims its ``jti``: ``SET NX sso:bcl:jti:{provider_id}:{sha256(jti)}``
       for as long as the validator would still accept the token
       (:data:`BACKCHANNEL_JTI_TTL_SECONDS` plus twice the clock-skew leeway),
       so a replay is refused (``sso_token_invalid``, reason ``replayed``);
    3. revokes the sessions the token names, with reason ``backchannel``.
       ``sub`` finds the identity by ``(iss, sub)`` at this provider; with
       ``sid`` as well only the sessions of that IdP session are revoked,
       otherwise every session of the identity. ``sid`` alone revokes the
       sessions of that IdP session across this provider's identities.
       Nothing matching is not an error (no information leak).

    The replay cache fails closed: when Redis is unavailable the token is not
    acted on and :class:`~specivo.services.sso.errors.SsoUnavailableError` is
    raised, so the route answers 503 and the IdP may retry (Back-Channel
    Logout 1.0, section 2.8, lets the RP answer other than 200/400 when it
    could not log out). If the revocation fails after the ``jti`` was claimed,
    :func:`release_logout_jti` frees it again for the same reason.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlencode

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.config import Settings, get_settings
from specivo.core.redis import get_redis
from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.services.auth_service import ID_TOKEN_PURPOSE
from specivo.services.session_service import SessionService
from specivo.services.sso import events
from specivo.services.sso.discovery import get_provider_metadata
from specivo.services.sso.errors import SsoError, SsoTokenInvalidError, SsoUnavailableError, TokenRejection
from specivo.services.sso.id_token import MAX_TOKEN_AGE_SECONDS, LogoutTokenClaims, verify_logout_token
from specivo.services.sso.log import logger

__all__ = [
    "BACKCHANNEL_JTI_TTL_SECONDS",
    "BACKCHANNEL_REVOKE_REASON",
    "LOGGED_OUT_PATH",
    "BackchannelLogoutResult",
    "SsoLogoutSession",
    "backchannel_logout",
    "end_session_url",
    "find_sso_logout_session",
    "logged_out_path",
    "post_logout_redirect_uri",
    "release_logout_jti",
]

#: Where a signed-out browser lands, locally or back from the IdP, before the
#: stealth prefix (:func:`logged_out_path` adds it).
LOGGED_OUT_PATH = "/login/?logged_out=1"


@dataclass(frozen=True, slots=True)
class SsoLogoutSession:
    """What RP-initiated logout needs from an SSO session, read before it is revoked."""

    provider: SsoProvider
    subject: str
    id_token_encrypted: str | None = field(repr=False)
    user_id: int | None = None


def logged_out_path(settings: Settings | None = None) -> str:
    """The signed-out landing path on this site: stealth prefix + :data:`LOGGED_OUT_PATH`."""
    settings = settings or get_settings()
    return settings.stealth_prefix.rstrip("/") + LOGGED_OUT_PATH


def post_logout_redirect_uri(settings: Settings | None = None) -> str:
    """The fixed ``post_logout_redirect_uri``: ``{APP_URL}{prefix}/login/?logged_out=1``."""
    settings = settings or get_settings()
    return settings.app_url.rstrip("/") + logged_out_path(settings)


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
    return SsoLogoutSession(
        provider=provider, subject=subject, id_token_encrypted=row.id_token_encrypted, user_id=row.user_id
    )


async def end_session_url(target: SsoLogoutSession, *, settings: Settings | None = None) -> str | None:
    """The IdP end-session URL for ``target``, or ``None`` to sign out locally.

    Logs ``sso.logout`` either way. ``id_token_hint`` is left out when the
    stored token is missing or unreadable (a rotated ``SECRET_KEY``); the
    request then identifies the client by ``client_id`` alone.
    """
    settings = settings or get_settings()
    provider = target.provider
    url = await _build_url(target, settings) if provider.enabled and provider.rp_logout_enabled else None
    events.logout(
        provider_slug=provider.slug,
        issuer=provider.issuer,
        user_id=target.user_id,
        subject=target.subject,
        idp_redirect=url is not None,
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


# ---------------------------------------------------------------------------
# Back-channel logout
# ---------------------------------------------------------------------------

#: Session revocation reason for back-channel logout.
BACKCHANNEL_REVOKE_REASON = "backchannel"

#: Base lifetime of a claimed ``jti`` (the design's ``EX 600``). Twice the
#: clock-skew leeway is added on top: the validator accepts a token from
#: ``iat - leeway`` until ``iat + MAX_TOKEN_AGE_SECONDS + leeway``.
BACKCHANNEL_JTI_TTL_SECONDS = MAX_TOKEN_AGE_SECONDS

_JTI_KEY_PREFIX = "sso:bcl:jti:"


@dataclass(frozen=True, slots=True)
class BackchannelLogoutResult:
    """What a back-channel logout did; ``jti_key`` is for :func:`release_logout_jti`."""

    by: Literal["sid", "sub"]
    revoked: int
    jti_key: str = field(repr=False)


def _jti_key(provider: SsoProvider, jti: str) -> str:
    # Hashed: the jti is IdP-supplied and of any length.
    return f"{_JTI_KEY_PREFIX}{provider.id}:{hashlib.sha256(jti.encode('utf-8')).hexdigest()}"


async def _claim_jti(key: str, *, ttl: int, redis: Redis | None) -> bool:
    """``SET NX`` the key; ``False`` when it already exists (a replay)."""
    redis = redis if redis is not None else await get_redis()
    return bool(await redis.set(key, "1", nx=True, ex=ttl))


async def release_logout_jti(jti_key: str, *, redis: Redis | None = None) -> None:
    """Free a claimed ``jti`` after the revocation failed, so the IdP's retry is not a replay."""
    try:
        redis = redis if redis is not None else await get_redis()
        await redis.delete(jti_key)
    except Exception as exc:  # noqa: BLE001 - best effort; the key expires on its own
        logger.warning("sso.backchannel_logout.release_failed error=%s", type(exc).__name__)


async def backchannel_logout(
    db: AsyncSession,
    provider: SsoProvider,
    logout_token: str,
    *,
    redis: Redis | None = None,
    settings: Settings | None = None,
) -> BackchannelLogoutResult:
    """Validate ``logout_token``, refuse a replay and revoke the sessions it names.

    Raises :class:`SsoTokenInvalidError` for an invalid or replayed token,
    :class:`SsoUnavailableError` when the replay cache is down, and the
    discovery/JWKS errors (``sso_idp_unreachable``, ``sso_discovery_invalid``)
    when the provider's keys cannot be read, or when the token names an
    unknown key while the JWKS refetch window is spent
    (:class:`~specivo.services.sso.errors.SsoSigningKeyRefreshLimitedError`;
    the route answers 503 so the IdP retries). Does not commit: a caller whose
    commit fails calls :func:`release_logout_jti` with ``result.jti_key``.
    """
    settings = settings or get_settings()
    metadata = await get_provider_metadata(provider, redis=redis, settings=settings)
    claims = await verify_logout_token(
        logout_token, provider=provider, metadata=metadata, redis=redis, settings=settings
    )

    key = _jti_key(provider, claims.jti)
    ttl = BACKCHANNEL_JTI_TTL_SECONDS + 2 * settings.sso_clock_skew_seconds
    try:
        fresh = await _claim_jti(key, ttl=ttl, redis=redis)
    except Exception as exc:
        logger.warning(
            "sso.backchannel_logout.failed provider=%s reason=replay_cache_unavailable error=%s",
            provider.slug,
            type(exc).__name__,
        )
        raise SsoUnavailableError("replay_cache_unavailable") from exc
    if not fresh:
        logger.warning("sso.logout_token.rejected iss=%s reason=%s detail=-", provider.issuer, TokenRejection.REPLAYED)
        raise SsoTokenInvalidError(TokenRejection.REPLAYED)

    try:
        revoked = await _revoke_named_sessions(db, provider, claims)
    except BaseException:
        await release_logout_jti(key, redis=redis)
        raise

    by: Literal["sid", "sub"] = "sid" if claims.sid is not None else "sub"
    events.backchannel_logout(
        provider_slug=provider.slug, issuer=provider.issuer, subject=claims.subject, by=by, revoked=revoked
    )
    return BackchannelLogoutResult(by=by, revoked=revoked, jti_key=key)


async def _revoke_named_sessions(db: AsyncSession, provider: SsoProvider, claims: LogoutTokenClaims) -> int:
    sessions = SessionService()
    if claims.subject is not None:
        identity_id = await db.scalar(
            select(UserIdentity.id).where(
                UserIdentity.provider_id == provider.id,
                UserIdentity.issuer == claims.issuer,
                UserIdentity.subject == claims.subject,
            )
        )
        if identity_id is None:
            return 0
        if claims.sid is not None:
            return await sessions.revoke_by_idp_sid(
                db, user_identity_id=identity_id, idp_sid=claims.sid, reason=BACKCHANNEL_REVOKE_REASON
            )
        return await sessions.revoke_by_identity(db, identity_id, reason=BACKCHANNEL_REVOKE_REASON)

    # sid without sub: that IdP session, across this provider's identities.
    sid = claims.sid
    if sid is None:  # pragma: no cover - the validator requires sid or sub
        return 0
    identity_ids = (
        await db.scalars(
            select(RefreshToken.user_identity_id)
            .join(UserIdentity, UserIdentity.id == RefreshToken.user_identity_id)
            .where(UserIdentity.provider_id == provider.id, RefreshToken.idp_sid == sid)
            .distinct()
        )
    ).all()
    revoked = 0
    for identity_id in sorted(i for i in identity_ids if i is not None):
        revoked += await sessions.revoke_by_idp_sid(
            db, user_identity_id=identity_id, idp_sid=sid, reason=BACKCHANNEL_REVOKE_REASON
        )
    return revoked
