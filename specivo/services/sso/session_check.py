"""Re-check an SSO session at its identity provider when Specivo renews it.

Specivo issues its own session tokens and, once a person has signed in,
would never ask the IdP again until the session's maximum length. A person
disabled, deleted or signed out at the IdP would keep working here. Every
IdP that issues refresh tokens refuses them (``invalid_grant``) once that
happens, whether or not it sends a back-channel logout, so a session is
renewed only after the IdP accepted its refresh token:

**At sign-in** (:func:`seal_sign_in_refresh_token`), when the provider has
``verify_session_at_idp`` on and the token response carried a refresh token,
the token is tried once right away. Some IdPs (authentik among them) hand out
a refresh token even when the client may not use the refresh grant and refuse
it later; renewing those sessions against the IdP would end every one of them
after one access-token lifetime. A token refused at sign-in is therefore not
kept: that session is renewed as before, and the provider's
``session_recheck_status`` records why, for Test connection (``ok``,
``no_refresh_token`` or ``refresh_refused``). A sign-in never fails because of
this probe.

**At renewal** (``AuthService.refresh``), at most once per
``SSO_IDP_RECHECK_MINUTES`` (default: one access-token lifetime) per session.
:func:`plan_recheck` reads what the check needs; the renewal then ends its
read transaction and asks the IdP (:func:`run_recheck`) with no database
connection, transaction or session lock held, under a Redis guard
(:func:`recheck_guard`) so the IdP's refresh token is presented once; only
after that does it lock and claim the session and apply the answer:

* the IdP accepts the token: the session is renewed and keeps the rotated
  token, if the IdP sent one;
* ``invalid_grant``: the session ends (reason ``idp_refresh_rejected``), with
  an ``SSO_SESSION_REJECTED`` audit event;
* the IdP cannot be asked (timeout, 5xx, any other refusal, unreadable
  configuration): ``idp_check_failure`` decides. ``allow`` (the default)
  renews the session and logs ``sso.session.idp_check_failed``; the next
  renewal asks again. ``deny`` ends the session (reason ``idp_check_failed``).

The stored token is sealed for the issuer, token endpoint and client that
issued it (:func:`~specivo.services.sso.tokens.idp_refresh_token_purpose`).
If any of them changed since sign-in, the token cannot be decrypted and is
never sent anywhere; the session is renewed as one without a token (not under
``idp_check_failure``: the IdP was not unreachable, the configuration moved,
and ``deny`` would sign everybody out for an edit). Changing the issuer,
discovery URL or client id through the API also drops the stored tokens of
the provider's sessions.

Sessions without a stored IdP refresh token, sessions of providers with
``verify_session_at_idp`` off, and password sessions are renewed as before.
Local password sign-in never depends on the IdP.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.config import Settings, get_settings
from specivo.core.utils import utcnow
from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.services.sso import events
from specivo.services.sso.discovery import ProviderMetadata, get_provider_metadata
from specivo.services.sso.errors import SsoError, SsoTokenExchangeFailedError
from specivo.services.sso.log import logger
from specivo.services.sso.tokens import TokenResponse, idp_refresh_token_purpose, refresh_at_idp

__all__ = [
    "IDP_CHECK_FAILED_REASON",
    "IDP_REFRESH_REJECTED_REASON",
    "RECHECK_GUARD_PREFIX",
    "RecheckPlan",
    "RecheckResult",
    "SignInRefreshToken",
    "plan_recheck",
    "recheck_guard",
    "report_rejected",
    "run_recheck",
    "seal_sign_in_refresh_token",
]

#: Session revocation reason: the IdP refused the session's refresh token.
IDP_REFRESH_REJECTED_REASON = "idp_refresh_rejected"
#: Session revocation reason: the IdP could not be asked and the provider says ``deny``.
IDP_CHECK_FAILED_REASON = "idp_check_failed"

RecheckStatus = Literal["ok", "no_refresh_token", "refresh_refused"]


@dataclass(frozen=True)
class SignInRefreshToken:
    """What a new SSO session stores about the IdP's refresh token."""

    encrypted: str | None = field(default=None, repr=False)
    checked_at: datetime | None = None


@dataclass(frozen=True)
class RecheckResult:
    """The outcome of :func:`run_recheck`.

    ``revoke_reason`` set: end the session and refuse the renewal. Otherwise
    the renewed row stores ``encrypted`` and ``checked_at``.
    """

    encrypted: str | None = field(default=None, repr=False)
    checked_at: datetime | None = None
    revoke_reason: str | None = None
    #: The IdP's sanitised OAuth error code, when it refused.
    idp_error: str | None = None


def _purpose(provider: SsoProvider, metadata: ProviderMetadata) -> str:
    return idp_refresh_token_purpose(provider.issuer, metadata.token_endpoint, provider.client_id)


def _seal(token: str, provider: SsoProvider, metadata: ProviderMetadata) -> str:
    return secret_box.encrypt(token, _purpose(provider, metadata))


async def _record_status(db: AsyncSession, provider: SsoProvider, status: RecheckStatus) -> None:
    """Remember what this sign-in found, for Test connection. Written only when it changes."""
    if provider.session_recheck_status == status:
        return
    provider.session_recheck_status = status
    provider.session_recheck_status_at = utcnow()
    await db.flush()
    logger.info("sso.session.recheck_status provider=%s status=%s", provider.slug, status)


async def seal_sign_in_refresh_token(
    db: AsyncSession,
    provider: SsoProvider,
    metadata: ProviderMetadata,
    tokens: TokenResponse,
    *,
    settings: Settings | None = None,
) -> SignInRefreshToken:
    """Decide what a new session keeps of the IdP's refresh token (see the module docstring)."""
    if not provider.verify_session_at_idp:
        return SignInRefreshToken()
    if tokens.refresh_token is None:
        await _record_status(db, provider, "no_refresh_token")
        return SignInRefreshToken()
    try:
        result = await refresh_at_idp(provider, metadata, tokens.refresh_token, settings=settings)
    except SsoTokenExchangeFailedError as exc:
        logger.warning(
            "sso.session.refresh_unusable provider=%s idp_error=%s: sessions from this provider are not "
            "re-checked at the IdP; allow the refresh_token grant for the client",
            provider.slug,
            exc.idp_error or exc.reason,
        )
        await _record_status(db, provider, "refresh_refused")
        return SignInRefreshToken()
    except SsoError as exc:
        # Could not try it (IdP slow, ...): keep the token; renewal applies idp_check_failure.
        logger.info("sso.session.refresh_probe_failed provider=%s code=%s", provider.slug, exc.code)
        return SignInRefreshToken(encrypted=_seal(tokens.refresh_token, provider, metadata), checked_at=utcnow())
    await _record_status(db, provider, "ok")
    sealed = _seal(result.refresh_token or tokens.refresh_token, provider, metadata)
    return SignInRefreshToken(encrypted=sealed, checked_at=utcnow())


@dataclass(frozen=True)
class RecheckPlan:
    """A renewal that has to ask the IdP: what :func:`run_recheck` needs, read before the IdP is asked."""

    provider: SsoProvider
    subject: str
    user_id: int
    sealed: str = field(repr=False)
    checked_at: datetime | None
    now: datetime


async def plan_recheck(
    db: AsyncSession,
    record: RefreshToken,
    *,
    settings: Settings | None = None,
) -> RecheckPlan | None:
    """Return the IdP check this renewal needs, or ``None`` when it needs none.

    Reads the database only. ``None``: not an SSO session, no stored IdP
    refresh token, the check is off for the provider, the session was checked
    less than ``SSO_IDP_RECHECK_MINUTES`` ago, or the session is past its
    expiry anyway (the renewal is refused without asking the IdP).
    """
    if record.auth_method != "sso" or record.user_identity_id is None or not record.idp_refresh_token_encrypted:
        return None
    now = utcnow()
    if record.expires_at < now or (record.absolute_expires_at is not None and record.absolute_expires_at <= now):
        return None

    found = (
        await db.execute(
            select(SsoProvider, UserIdentity.subject)
            .join(UserIdentity, UserIdentity.provider_id == SsoProvider.id)
            .where(UserIdentity.id == record.user_identity_id)
        )
    ).first()
    if found is None:
        return None
    provider, subject = found
    if not provider.verify_session_at_idp:
        return None

    settings = settings or get_settings()
    interval = timedelta(minutes=settings.sso_idp_recheck_interval_minutes)
    if record.idp_checked_at is not None and now - record.idp_checked_at < interval:
        return None
    return RecheckPlan(
        provider=provider,
        subject=subject,
        user_id=record.user_id,
        sealed=record.idp_refresh_token_encrypted,
        checked_at=record.idp_checked_at,
        now=now,
    )


async def run_recheck(plan: RecheckPlan, *, settings: Settings | None = None) -> RecheckResult:
    """Ask the IdP whether the session of ``plan`` may be renewed.

    Network only: no database access, so the caller must not hold a
    transaction, a connection or the session lock while this runs (a slow IdP
    would hold them for up to twice ``SSO_HTTP_TIMEOUT_SECONDS``). Nothing is
    recorded here except log lines; the caller applies the result once it has
    claimed the session (see :func:`report_rejected`), so a renewal that loses
    a race records nothing.
    """
    provider = plan.provider
    unchanged = RecheckResult(encrypted=plan.sealed, checked_at=plan.checked_at)
    try:
        metadata = await get_provider_metadata(provider, settings=settings)
    except SsoError as exc:
        return _check_failed(plan, unchanged, code=exc.reason)

    # The token is sealed for the issuer, token endpoint and client that
    # issued it. When any of them changed since sign-in (or SECRET_KEY did),
    # it cannot be read and is never sent: the session goes on as one that
    # never had a refresh token, and the dead token is dropped.
    try:
        token = secret_box.decrypt(plan.sealed, _purpose(provider, metadata))
    except secret_box.SecretUnreadableError:
        logger.warning(
            "sso.session.idp_refresh_token_unbound provider=%s user_id=%s: the issuer, token endpoint, client "
            "or SECRET_KEY changed since sign-in; this session is no longer re-checked at the IdP",
            provider.slug,
            plan.user_id,
        )
        return RecheckResult()

    try:
        result = await refresh_at_idp(provider, metadata, token, settings=settings)
    except SsoTokenExchangeFailedError as exc:
        if exc.idp_error == "invalid_grant":
            return RecheckResult(revoke_reason=IDP_REFRESH_REJECTED_REASON, idp_error=exc.idp_error)
        return _check_failed(plan, unchanged, code=exc.idp_error or exc.reason)
    except SsoError as exc:
        return _check_failed(plan, unchanged, code=exc.reason)

    sealed = plan.sealed
    if result.refresh_token:
        sealed = _seal(result.refresh_token, provider, metadata)
    return RecheckResult(encrypted=sealed, checked_at=plan.now)


def report_rejected(plan: RecheckPlan, result: RecheckResult) -> None:
    """Record the ``SSO_SESSION_REJECTED`` event of a session ended by ``result``."""
    if result.revoke_reason is None:
        return
    events.session_rejected(
        provider_slug=plan.provider.slug,
        issuer=plan.provider.issuer,
        user_id=plan.user_id,
        subject=plan.subject,
        reason=result.revoke_reason,
        idp_error=result.idp_error,
    )


#: Redis key prefix of :func:`recheck_guard`; the suffix is the refresh row id.
RECHECK_GUARD_PREFIX = "sso_idp_recheck:"


@asynccontextmanager
async def recheck_guard(refresh_row_id: int, *, settings: Settings | None = None) -> AsyncIterator[bool]:
    """Let one renewal of a refresh row ask the IdP; yields ``False`` to the others.

    The IdP is asked before the session is locked, so two renewals presenting
    the same Specivo refresh token at once would both present the IdP's
    refresh token. With rotation the second one is refused (``invalid_grant``)
    and some IdPs then revoke the whole token family. The key is held for
    the longest the check can take; it is not released after a renewal that
    went through (the row is gone then, so nothing else can use it), only
    when the renewal failed. Fails closed: without Redis nobody gets it.
    """
    settings = settings or get_settings()
    key = f"{RECHECK_GUARD_PREFIX}{refresh_row_id}"
    ttl_ms = int((2 * settings.sso_http_timeout_seconds + 30) * 1000)
    try:
        from specivo.core.redis import get_redis

        redis = await get_redis()
        acquired = bool(await redis.set(key, "1", nx=True, px=ttl_ms))
    except Exception as exc:  # noqa: BLE001 - any Redis failure means no guard
        logger.warning("sso.session.recheck_guard_unavailable error=%s", type(exc).__name__)
        acquired = False
    if not acquired:
        yield False
        return
    try:
        yield True
    except BaseException:
        try:
            await redis.delete(key)
        except Exception:  # noqa: BLE001 - the key expires on its own
            pass
        raise


def _check_failed(plan: RecheckPlan, unchanged: RecheckResult, *, code: str) -> RecheckResult:
    deny = plan.provider.idp_check_failure == "deny"
    logger.warning(
        "sso.session.idp_check_failed provider=%s user_id=%s reason=%s action=%s",
        plan.provider.slug,
        plan.user_id,
        code,
        "deny" if deny else "allow",
        extra={"event": "sso_session_idp_check_failed"},
    )
    if not deny:
        # checked_at stays: the next renewal asks again.
        return unchanged
    return RecheckResult(revoke_reason=IDP_CHECK_FAILED_REASON)
