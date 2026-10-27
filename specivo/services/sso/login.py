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

No session row exists unless every step passed. The IdP's access token is
used for userinfo only and never stored.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.sso import SsoProvider
from specivo.services.auth_service import AuthService
from specivo.services.sso.discovery import get_provider_metadata
from specivo.services.sso.errors import (
    SsoIdpRefusedError,
    SsoStateInvalidError,
    SsoTokenInvalidError,
    TokenRejection,
)
from specivo.services.sso.flow import AuthorizationRequest, begin, complete
from specivo.services.sso.id_token import verify_id_token
from specivo.services.sso.log import logger, sanitize_idp_text
from specivo.services.sso.provisioning import resolve_user, subject_hash
from specivo.services.sso.tokens import exchange_code, fetch_userinfo

__all__ = ["SsoLoginResult", "SsoLoginService"]

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


class SsoLoginService:
    """Stateless; the provider, Redis and the DB session carry all state."""

    async def get_enabled_provider(self, db: AsyncSession, slug: str) -> SsoProvider | None:
        """The enabled provider with ``slug``, or ``None`` (the routes answer 404)."""
        return (
            await db.execute(select(SsoProvider).where(SsoProvider.slug == slug, SsoProvider.enabled.is_(True)))
        ).scalar_one_or_none()

    async def start(self, provider: SsoProvider, *, next_path: str | None) -> AuthorizationRequest:
        """Begin a login transaction at ``provider``; ``next_path`` is sanitised by the flow."""
        metadata = await get_provider_metadata(provider)
        return await begin(provider, metadata, intent="login", next_path=next_path)

    async def finish(
        self,
        db: AsyncSession,
        provider: SsoProvider,
        *,
        params: Mapping[str, str],
        binding_token: str | None,
        device_info: str | None = None,
        ip: str | None = None,
    ) -> SsoLoginResult:
        """Run the callback for ``provider`` with the query ``params``; raises an ``SsoError`` on refusal."""
        tx = await complete(state=params.get("state"), binding_token=binding_token)

        if tx.provider_id != provider.id or tx.issuer != provider.issuer:
            logger.warning(
                "sso.callback.rejected provider=%s reason=provider_mismatch started_at=%s",
                provider.slug,
                tx.provider_slug,
            )
            raise SsoStateInvalidError("provider_mismatch")

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
        logger.info(
            "sso.login.success provider=%s user_id=%s sub=%s",
            provider.slug,
            account.user.id,
            subject_hash(id_token.subject),
        )
        return SsoLoginResult(
            access_token=session.access_token,
            refresh_token=session.refresh_token,
            user_id=session.user_id,
            next_path=tx.next_path,
            absolute_expires_at=session.absolute_expires_at,
        )
