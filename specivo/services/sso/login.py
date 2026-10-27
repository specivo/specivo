"""The SSO browser login, start to session: what the web routes delegate to.

:meth:`SsoLoginService.start` stores a transaction and returns the authorize
URL. :meth:`SsoLoginService.finish` runs the callback checks in this order,
stopping at the first failure:

1. consume the transaction for ``state`` and check the browser binding
   (``sso_state_invalid``; ``sso_unavailable`` without Redis);
2. the transaction must belong to this provider and its issuer must still be
   the provider's (mix-up defence; the issuer comes from the record, never
   from the request);
3. RFC 9207: an ``iss`` parameter, mandatory when the IdP advertises
   ``authorization_response_iss_parameter_supported``, must equal that
   issuer (``sso_token_invalid``);
4. an ``error`` response becomes ``sso_idp_error``; its ``error_description``
   is logged truncated and never shown;
5. exchange the code (PKCE verifier, the stored ``redirect_uri``), validate
   the ID token (nonce from the record, ``at_hash``), and fetch userinfo when
   ``claim_config.use_userinfo`` is on (its ``sub`` must match);
6. resolve the account (:func:`specivo.services.sso.provisioning.resolve_user`);
7. start a new Specivo session (:meth:`AuthService.create_sso_session`).

A **link** transaction (``intent="link"``, started by a signed-in user from
their profile with :meth:`SsoLoginService.start_link`) must be finished by
the browser still signed in as that user; step 6 then links the identity and
step 7 is skipped: the user's existing sessions stay, no new one starts.

No session row exists unless every step passed. The IdP's access token is
used for userinfo only and never stored. An ``SsoError`` raised after the
transaction was consumed carries its ``intent``, so the route knows where to
report it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.sso import SsoProvider, UserIdentity
from specivo.services.auth_service import AuthService
from specivo.services.sso.discovery import get_provider_metadata
from specivo.services.sso.errors import (
    SsoError,
    SsoIdpRefusedError,
    SsoProviderAlreadyLinkedError,
    SsoStateInvalidError,
    SsoTokenInvalidError,
    TokenRejection,
)
from specivo.services.sso.flow import AuthorizationRequest, LoginTransaction, begin, complete
from specivo.services.sso.id_token import verify_id_token
from specivo.services.sso.log import logger, sanitize_idp_text
from specivo.services.sso.provisioning import resolve_user, subject_hash
from specivo.services.sso.tokens import exchange_code, fetch_userinfo

__all__ = ["LINK_RETURN_PATH", "SsoLinkResult", "SsoLoginResult", "SsoLoginService"]

#: Where a link flow starts from and returns to.
LINK_RETURN_PATH = "/my/profile/"

# Claims that describe the token rather than the user; userinfo never overrides them.
_TOKEN_CLAIMS = frozenset({"iss", "sub", "aud", "exp", "iat", "nbf", "nonce", "azp", "auth_time", "sid", "at_hash"})


@dataclass(frozen=True)
class SsoLoginResult:
    """A finished SSO login: the new session's tokens and where to go next."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    user_id: int
    next_path: str
    absolute_expires_at: datetime
    #: The hashed subject (never the raw ``sub``), for the audit trail.
    sub_hash: str = ""


@dataclass(frozen=True)
class SsoLinkResult:
    """A finished link: the identity is connected to the signed-in user; no new session."""

    user_id: int
    identity_id: int
    provider_slug: str


class SsoLoginService:
    """Stateless; the provider, Redis and the DB session carry all state."""

    async def get_enabled_provider(self, db: AsyncSession, slug: str) -> SsoProvider | None:
        """The enabled provider with ``slug``, or ``None`` (the routes answer 404)."""
        return (
            await db.execute(select(SsoProvider).where(SsoProvider.slug == slug, SsoProvider.enabled.is_(True)))
        ).scalar_one_or_none()

    async def list_enabled_providers(self, db: AsyncSession) -> list[SsoProvider]:
        """The enabled providers in login-button order (``position``, then name)."""
        rows = await db.execute(
            select(SsoProvider)
            .where(SsoProvider.enabled.is_(True))
            .order_by(SsoProvider.position, SsoProvider.display_name, SsoProvider.id)
        )
        return list(rows.scalars().all())

    async def start(self, provider: SsoProvider, *, next_path: str | None) -> AuthorizationRequest:
        """Begin a login transaction at ``provider``; ``next_path`` is sanitised by the flow."""
        metadata = await get_provider_metadata(provider)
        return await begin(provider, metadata, intent="login", next_path=next_path)

    async def start_link(self, db: AsyncSession, provider: SsoProvider, *, user_id: int) -> AuthorizationRequest:
        """Begin a link transaction for the signed-in ``user_id`` at ``provider``.

        Refused up front with ``sso_provider_already_linked`` when the user
        already has an identity there, so no round trip to the IdP is wasted.
        """
        existing = await db.scalar(
            select(UserIdentity.id).where(UserIdentity.user_id == user_id, UserIdentity.provider_id == provider.id)
        )
        if existing is not None:
            raise SsoProviderAlreadyLinkedError("provider_already_linked")
        metadata = await get_provider_metadata(provider)
        return await begin(provider, metadata, intent="link", next_path=LINK_RETURN_PATH, user_id=user_id)

    async def finish(
        self,
        db: AsyncSession,
        provider: SsoProvider,
        *,
        params: Mapping[str, str],
        binding_token: str | None,
        device_info: str | None = None,
        ip: str | None = None,
        current_user_id: Callable[[], Awaitable[int | None]] | None = None,
    ) -> SsoLoginResult | SsoLinkResult:
        """Run the callback for ``provider`` with the query ``params``; raises an ``SsoError`` on refusal.

        ``current_user_id`` returns the user this browser is signed in as; it
        is called only for a link transaction, which that user must have
        started.
        """
        tx = await complete(state=params.get("state"), binding_token=binding_token)
        try:
            return await self._finish(
                db, provider, tx, params=params, device_info=device_info, ip=ip, current_user_id=current_user_id
            )
        except SsoError as exc:
            exc.intent = tx.intent
            raise

    async def _finish(
        self,
        db: AsyncSession,
        provider: SsoProvider,
        tx: LoginTransaction,
        *,
        params: Mapping[str, str],
        device_info: str | None,
        ip: str | None,
        current_user_id: Callable[[], Awaitable[int | None]] | None,
    ) -> SsoLoginResult | SsoLinkResult:
        if tx.provider_id != provider.id or tx.issuer != provider.issuer:
            logger.warning(
                "sso.callback.rejected provider=%s reason=provider_mismatch started_at=%s",
                provider.slug,
                tx.provider_slug,
            )
            raise SsoStateInvalidError("provider_mismatch")

        if tx.intent == "link":
            signed_in = await current_user_id() if current_user_id is not None else None
            if tx.user_id is None or signed_in != tx.user_id:
                logger.warning("sso.callback.rejected provider=%s reason=link_user_mismatch", provider.slug)
                raise SsoStateInvalidError("link_user_mismatch")

        metadata = await get_provider_metadata(provider)

        iss = params.get("iss")
        if iss is not None or metadata.authorization_response_iss_parameter_supported:
            if iss != tx.issuer:
                logger.warning(
                    "sso.callback.rejected provider=%s reason=%s",
                    provider.slug,
                    "iss_missing" if iss is None else "iss_mismatch",
                )
                raise SsoTokenInvalidError(TokenRejection.WRONG_ISSUER, detail="authorization response iss")

        error = params.get("error")
        if error is not None:
            description = params.get("error_description")
            logger.warning(
                "sso.callback.idp_error provider=%s error=%s description=%s",
                provider.slug,
                sanitize_idp_text(error, 64),
                sanitize_idp_text(description) if description is not None else "-",
            )
            raise SsoIdpRefusedError(f"idp_error:{sanitize_idp_text(error, 64)}")

        code = params.get("code")
        if not code:
            logger.warning("sso.callback.rejected provider=%s reason=missing_code", provider.slug)
            raise SsoIdpRefusedError("missing_code")

        tokens = await exchange_code(
            provider, metadata, code=code, redirect_uri=tx.redirect_uri, code_verifier=tx.code_verifier
        )
        id_token = await verify_id_token(
            tokens.id_token,
            provider=provider,
            metadata=metadata,
            nonce=tx.nonce,
            access_token=tokens.access_token,
        )

        claims: dict[str, Any] = dict(id_token.claims)
        if (provider.claim_config or {}).get("use_userinfo") is True and metadata.userinfo_endpoint:
            userinfo = await fetch_userinfo(metadata, tokens.access_token, expected_sub=id_token.subject)
            claims.update({k: v for k, v in userinfo.items() if k not in _TOKEN_CLAIMS})

        account = await resolve_user(
            db,
            provider=provider,
            id_token=id_token,
            claims=claims,
            intent=tx.intent,
            link_user_id=tx.user_id,
        )
        if tx.intent == "link":
            return SsoLinkResult(
                user_id=account.user.id,
                identity_id=account.identity.id,
                provider_slug=provider.slug,
            )
        session = await AuthService().create_sso_session(
            db,
            user=account.user,
            identity=account.identity,
            provider=provider,
            id_token=tokens.id_token,
            idp_sid=id_token.sid,
            device_info=device_info,
            ip=ip,
        )
        sub_hash = subject_hash(id_token.subject)
        logger.info("sso.login.success provider=%s user_id=%s sub=%s", provider.slug, account.user.id, sub_hash)
        return SsoLoginResult(
            access_token=session.access_token,
            refresh_token=session.refresh_token,
            user_id=session.user_id,
            next_path=tx.next_path,
            absolute_expires_at=session.absolute_expires_at,
            sub_hash=sub_hash,
        )
