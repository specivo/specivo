"""Token endpoint client (authorization code exchange) and userinfo fetch.

Implements OpenID Connect Core 1.0, section 3.1.3 (token request and
response) and 5.3 (userinfo, including the rule in 5.3.2 that the userinfo
``sub`` must equal the ID token ``sub``), with:

* PKCE (RFC 7636, section 4.5): the ``code_verifier`` is always sent;
* client authentication (Core 1.0, section 9): ``client_secret_basic`` with
  the client id and secret form-urlencoded before base64 (RFC 6749,
  section 2.3.1), ``client_secret_post``, or ``none`` for public clients;
* the client secret from ``SsoProvider.client_secret_encrypted``
  (``specivo.core.secret_box``, purpose ``sso-client-secret``) or from the
  environment variable named by ``client_secret_env``;
* error responses (RFC 6749, section 5.2): only the ``error`` code is kept
  (sanitised); ``error_description`` is logged truncated and scrubbed, and is
  never shown to users.

All requests go through :func:`specivo.services.sso.http.create_sso_client`.
The token POST never follows redirects. The IdP's access and refresh tokens
are returned to the caller and never stored or logged here.
"""

from __future__ import annotations

import base64
import hmac
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus

import httpx

from specivo.core import secret_box
from specivo.core.config import Settings, get_settings
from specivo.models.sso import SsoProvider
from specivo.services.sso._support import parse_json_object, unreachable_from
from specivo.services.sso.discovery import ProviderMetadata
from specivo.services.sso.errors import (
    SsoConfigurationError,
    SsoIdpUnreachableError,
    SsoTokenExchangeFailedError,
    SsoTokenInvalidError,
    TokenRejection,
)
from specivo.services.sso.http import create_sso_client
from specivo.services.sso.log import logger, sanitize_idp_text

__all__ = [
    "CLIENT_SECRET_PURPOSE",
    "TokenResponse",
    "exchange_code",
    "fetch_userinfo",
    "resolve_client_secret",
]

#: ``secret_box`` purpose of ``SsoProvider.client_secret_encrypted``.
CLIENT_SECRET_PURPOSE = "sso-client-secret"

# Registered OAuth error codes are lowercase words joined by underscores.
_ERROR_CODE_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")


@dataclass(frozen=True)
class TokenResponse:
    """A successful token response. The tokens are kept out of ``repr``/``str``."""

    id_token: str = field(repr=False)
    access_token: str = field(repr=False)
    token_type: str = "Bearer"
    expires_in: int | None = None
    scope: str | None = None


def resolve_client_secret(provider: SsoProvider) -> str | None:
    """The plaintext client secret, or ``None`` for a public client (``none``).

    Raises :class:`SsoConfigurationError` when a confidential client has no
    readable secret (``SECRET_KEY`` rotated, env var unset, nothing set).
    """
    method = provider.token_endpoint_auth_method
    if method == "none":
        return None
    if method not in ("client_secret_basic", "client_secret_post"):
        raise SsoConfigurationError("unsupported_auth_method")
    if provider.client_secret_encrypted:
        try:
            return secret_box.decrypt(provider.client_secret_encrypted, CLIENT_SECRET_PURPOSE)
        except secret_box.SecretUnreadableError:
            logger.error("sso.client_secret.unreadable provider=%s", provider.slug)
            raise SsoConfigurationError("client_secret_unreadable") from None
    if provider.client_secret_env:
        value = os.environ.get(provider.client_secret_env)
        if not value:
            logger.error("sso.client_secret.env_unset provider=%s", provider.slug)
            raise SsoConfigurationError("client_secret_env_unset")
        return value
    raise SsoConfigurationError("client_secret_missing")


def _basic_credentials(client_id: str, secret: str) -> str:
    # RFC 6749, section 2.3.1: form-urlencode both parts, then base64 "id:secret".
    pair = f"{quote_plus(client_id, safe='')}:{quote_plus(secret, safe='')}"
    return "Basic " + base64.b64encode(pair.encode("ascii")).decode("ascii")


def _scrub(text: str, secrets: tuple[str | None, ...]) -> str:
    for value in secrets:
        if value:
            text = text.replace(value, "[redacted]")
    return text


def _token_failure(response: httpx.Response, provider: SsoProvider, scrub: tuple[str | None, ...]) -> Exception:
    body = parse_json_object(response.content)
    raw_error = body.get("error") if body else None
    if raw_error is None:
        logger.warning(
            "sso.token.failed provider=%s status=%s reason=unexpected_status", provider.slug, response.status_code
        )
        return SsoTokenExchangeFailedError("unexpected_status")
    error = raw_error if isinstance(raw_error, str) and _ERROR_CODE_RE.fullmatch(raw_error) else "invalid_error_code"
    description = body.get("error_description") if body else None
    logger.warning(
        "sso.token.failed provider=%s status=%s error=%s description=%s",
        provider.slug,
        response.status_code,
        error,
        sanitize_idp_text(_scrub(str(description), scrub)) if description is not None else "-",
    )
    return SsoTokenExchangeFailedError("idp_error", idp_error=error)


def _parse_token_response(content: bytes) -> TokenResponse:
    body = parse_json_object(content)
    if body is None:
        raise SsoTokenInvalidError(TokenRejection.TOKEN_RESPONSE_INVALID, detail="not a JSON object")
    id_token = body.get("id_token")
    access_token = body.get("access_token")
    token_type = body.get("token_type")
    if not isinstance(id_token, str) or not id_token:
        raise SsoTokenInvalidError(TokenRejection.TOKEN_RESPONSE_INVALID, detail="id_token")
    if not isinstance(access_token, str) or not access_token:
        raise SsoTokenInvalidError(TokenRejection.TOKEN_RESPONSE_INVALID, detail="access_token")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise SsoTokenInvalidError(TokenRejection.TOKEN_RESPONSE_INVALID, detail="token_type")
    expires_in = body.get("expires_in")
    scope = body.get("scope")
    return TokenResponse(
        id_token=id_token,
        access_token=access_token,
        token_type=token_type,
        expires_in=expires_in if isinstance(expires_in, int) and not isinstance(expires_in, bool) else None,
        scope=scope if isinstance(scope, str) else None,
    )


async def exchange_code(
    provider: SsoProvider,
    metadata: ProviderMetadata,
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    settings: Settings | None = None,
) -> TokenResponse:
    """Exchange an authorization code at the token endpoint.

    ``redirect_uri`` must be byte-for-byte the value sent in the authorization
    request. Raises ``sso_idp_unreachable`` (transport failure, 5xx, 429),
    ``sso_token_exchange_failed`` (the IdP refused, e.g. ``invalid_grant``) or
    ``sso_token_invalid`` (a 200 response without a usable ID token).
    """
    settings = settings or get_settings()
    secret = resolve_client_secret(provider)
    method = provider.token_endpoint_auth_method

    form: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    headers = {"Accept": "application/json"}
    if method == "client_secret_basic" and secret is not None:
        headers["Authorization"] = _basic_credentials(provider.client_id, secret)
    else:
        form["client_id"] = provider.client_id
        if method == "client_secret_post" and secret is not None:
            form["client_secret"] = secret

    try:
        async with create_sso_client(settings) as client:
            response = await client.post(metadata.token_endpoint, data=form, headers=headers, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise unreachable_from(exc, what="token") from None

    status = response.status_code
    if status >= 500 or status == 429:
        logger.warning("sso.token.unreachable provider=%s reason=http_%s", provider.slug, status)
        raise SsoIdpUnreachableError(f"http_{status}")
    if status != 200:
        raise _token_failure(response, provider, (code, code_verifier, secret))
    try:
        result = _parse_token_response(response.content)
    except SsoTokenInvalidError as exc:
        logger.warning("sso.token.invalid_response provider=%s detail=%s", provider.slug, exc.detail)
        raise
    logger.info("sso.token.exchanged provider=%s", provider.slug)
    return result


def _userinfo_rejected(detail: str) -> SsoTokenInvalidError:
    logger.warning("sso.userinfo.rejected detail=%s", detail)
    return SsoTokenInvalidError(TokenRejection.USERINFO_REJECTED, detail=detail)


async def fetch_userinfo(
    metadata: ProviderMetadata,
    access_token: str,
    *,
    expected_sub: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Fetch userinfo claims; their ``sub`` must equal the ID token ``sub``.

    Only JSON userinfo responses are supported; a signed (``application/jwt``)
    response is refused. Raises ``sso_idp_unreachable`` or ``sso_token_invalid``.
    """
    if not metadata.userinfo_endpoint:
        raise SsoConfigurationError("userinfo_endpoint_missing")
    settings = settings or get_settings()
    try:
        async with create_sso_client(settings) as client:
            response = await client.get(
                metadata.userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise unreachable_from(exc, what="userinfo") from None

    status = response.status_code
    if status >= 500 or status == 429:
        logger.warning("sso.userinfo.unreachable reason=http_%s", status)
        raise SsoIdpUnreachableError(f"http_{status}")
    if status != 200:
        raise _userinfo_rejected(f"http_{status}")
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type == "application/jwt":
        raise _userinfo_rejected("signed userinfo is not supported")
    claims = parse_json_object(response.content)
    if claims is None:
        raise _userinfo_rejected("not a JSON object")

    sub = claims.get("sub")
    if not isinstance(sub, str) or not hmac.compare_digest(sub.encode("utf-8"), expected_sub.encode("utf-8")):
        logger.warning("sso.userinfo.rejected reason=sub_mismatch")
        raise SsoTokenInvalidError(TokenRejection.SUB_MISMATCH)
    return claims
