"""Login and link transactions: state, nonce, PKCE verifier and browser binding.

:func:`begin` starts an authorization code flow (OpenID Connect Core 1.0,
section 3.1.2.1) and :func:`complete` finishes the relying party's part of
the callback checks. Every transaction:

* has a 256-bit random ``state`` (CSRF protection, Core 1.0 section 3.1.2.1)
  and ``nonce`` (replay protection, section 15.5.2);
* uses PKCE with ``S256`` (RFC 7636), for confidential clients too; ``plain``
  is never sent;
* is stored in Redis under ``sso:tx:{sha256(state)}`` for
  :data:`TX_TTL_SECONDS`, so the state itself is never stored, and is
  consumed with ``GETDEL`` (single use);
* is bound to the browser that started it: a random binding token goes into
  the ``spv_sso_tx`` cookie and only its SHA-256 into the record, so a state
  delivered to another browser (login CSRF, session swapping) is refused;
* fixes the ``redirect_uri`` (``APP_URL`` + stealth prefix + slug) at start,
  so the token request sends byte for byte the value of the authorization
  request;
* records the provider and its issuer, so the callback can refuse a response
  meant for another provider (mix-up, RFC 9207) without trusting the request;
* forces re-authentication for a link (``prompt=login`` and ``max_age=0``,
  over any configured value), and records the ``max_age`` it sent, so the
  callback can require a fresh ``auth_time`` in the ID token (Core 1.0,
  section 3.1.3.7, item 13).

Redis is the source of truth here, so a Redis failure is
``sso_unavailable``: the flow fails closed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from redis.asyncio import Redis

from specivo.core.config import Settings, get_settings
from specivo.core.redis import get_redis
from specivo.models.sso import SsoProvider
from specivo.services.sso._support import REDIS_ERRORS
from specivo.services.sso.discovery import ProviderMetadata
from specivo.services.sso.errors import SsoStateInvalidError, SsoUnavailableError
from specivo.services.sso.log import logger

__all__ = [
    "ALLOWED_EXTRA_AUTH_PARAMS",
    "TX_COOKIE_NAME",
    "LINK_REAUTH_PARAMS",
    "TX_TTL_SECONDS",
    "AuthorizationRequest",
    "Intent",
    "LoginTransaction",
    "auth_params_for",
    "backchannel_logout_uri_for",
    "begin",
    "build_authorize_url",
    "complete",
    "redirect_uri_for",
    "tx_cookie_path",
    "tx_key",
]

#: Lifetime of a transaction in Redis and of the binding cookie.
TX_TTL_SECONDS = 600
#: The HttpOnly cookie carrying the browser-binding token.
TX_COOKIE_NAME = "spv_sso_tx"
#: The only ``SsoProvider.extra_auth_params`` keys sent to the IdP.
ALLOWED_EXTRA_AUTH_PARAMS: tuple[str, ...] = ("prompt", "domain_hint", "login_hint", "acr_values", "max_age")

Intent = Literal["login", "link"]
_INTENTS: frozenset[str] = frozenset({"login", "link"})
_RECORD_VERSION = 1
# token_urlsafe(32) is 43 characters; anything far longer is not ours.
_MAX_STATE_LENGTH = 128
_MAX_EXTRA_PARAM_LENGTH = 512
#: Sent with a link: the person proves at the IdP, now, that they own the account.
LINK_REAUTH_PARAMS: dict[str, str] = {"prompt": "login", "max_age": "0"}


def tx_key(state: str) -> str:
    """Redis key of the transaction for ``state`` (the state is stored only hashed)."""
    return "sso:tx:" + _sha256(state)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _prefix(settings: Settings) -> str:
    return settings.stealth_prefix.rstrip("/")


def _provider_url(slug: str, endpoint: str, settings: Settings | None) -> str:
    """``APP_URL`` + stealth prefix + ``/auth/oidc/{slug}/{endpoint}/``.

    Built from configuration only, never from the request's ``Host``. The one
    source for every address an admin registers at the IdP, so what the admin
    copies is what the flow sends and what the routes answer on.
    """
    settings = settings or get_settings()
    return f"{settings.app_url.rstrip('/')}{_prefix(settings)}/auth/oidc/{slug}/{endpoint}/"


def redirect_uri_for(slug: str, settings: Settings | None = None) -> str:
    """The callback URL registered at the IdP: ``APP_URL`` + stealth prefix + slug."""
    return _provider_url(slug, "callback", settings)


def backchannel_logout_uri_for(slug: str, settings: Settings | None = None) -> str:
    """The back-channel logout URL registered at the IdP: ``APP_URL`` + stealth prefix + slug."""
    return _provider_url(slug, "backchannel-logout", settings)


def tx_cookie_path(settings: Settings | None = None) -> str:
    """``Path`` of the binding cookie: only the SSO routes ever receive it."""
    settings = settings or get_settings()
    return f"{_prefix(settings)}/auth/oidc/"


@dataclass(frozen=True)
class AuthorizationRequest:
    """Where to send the browser, and the binding token for its cookie."""

    url: str = field(repr=False)
    binding_token: str = field(repr=False)


@dataclass(frozen=True)
class LoginTransaction:
    """A completed (consumed) transaction. Secrets are kept out of ``repr``."""

    provider_id: int
    provider_slug: str
    issuer: str
    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)
    redirect_uri: str
    next_path: str
    intent: Intent
    user_id: int | None
    #: The ``max_age`` sent in the authorization request, or ``None``.
    max_age: int | None = None


def _scopes(provider: SsoProvider) -> str:
    scopes = [s for s in (provider.scopes or "").split() if s]
    if "openid" not in scopes:
        scopes.insert(0, "openid")
    return " ".join(dict.fromkeys(scopes))


def _extra_params(provider: SsoProvider) -> dict[str, str]:
    """The allowlisted ``extra_auth_params`` as strings; anything else is dropped."""
    result: dict[str, str] = {}
    configured = provider.extra_auth_params or {}
    for key in ALLOWED_EXTRA_AUTH_PARAMS:
        value = configured.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            result[key] = str(value)
        elif isinstance(value, str) and value and len(value) <= _MAX_EXTRA_PARAM_LENGTH:
            result[key] = value
    return result


def auth_params_for(provider: SsoProvider, intent: Intent) -> dict[str, str]:
    """The optional authorization parameters for ``intent``: allowlisted extras, link overrides on top."""
    params = _extra_params(provider)
    if intent == "link":
        params.update(LINK_REAUTH_PARAMS)
    return params


def _max_age(params: dict[str, str]) -> int | None:
    """The ``max_age`` these parameters send, as an int (``None`` when absent or not a number)."""
    value = params.get("max_age")
    if value is None or not value.isdigit():
        return None
    return int(value)


def build_authorize_url(
    provider: SsoProvider,
    metadata: ProviderMetadata,
    *,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
    extra_params: dict[str, str] | None = None,
) -> str:
    """The authorization request URL (Core 1.0 section 3.1.2.1, RFC 7636 section 4.3).

    ``extra_params`` defaults to the provider's allowlisted extras. A query
    already present in the ``authorization_endpoint`` is kept. The protocol
    parameters are set last, so no configured value can replace them.
    """
    params: dict[str, str] = {
        **(_extra_params(provider) if extra_params is None else extra_params),
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "scope": _scopes(provider),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return str(httpx.URL(metadata.authorization_endpoint).copy_merge_params(params))


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def begin(
    provider: SsoProvider,
    metadata: ProviderMetadata,
    *,
    intent: Intent = "login",
    next_path: str | None = None,
    user_id: int | None = None,
    redis: Redis | None = None,
    settings: Settings | None = None,
) -> AuthorizationRequest:
    """Store a new transaction and return the authorize URL and binding token.

    ``next_path`` goes through ``safe_next_path``: anything but a path on
    this site becomes "" (the dashboard). Raises ``sso_unavailable`` when the
    transaction cannot be stored.
    """
    from specivo.web.deps import safe_next_path

    if intent not in _INTENTS:
        raise ValueError(f"unknown SSO intent {intent!r}")
    settings = settings or get_settings()

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    # 64 random bytes -> 86 characters, inside RFC 7636's 43..128.
    code_verifier = secrets.token_urlsafe(64)
    binding_token = secrets.token_urlsafe(32)
    redirect_uri = redirect_uri_for(provider.slug, settings)
    extra_params = auth_params_for(provider, intent)

    record: dict[str, Any] = {
        "v": _RECORD_VERSION,
        "provider_id": provider.id,
        "provider_slug": provider.slug,
        "issuer": provider.issuer,
        "nonce": nonce,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "next": safe_next_path(next_path),
        "intent": intent,
        "user_id": user_id,
        "max_age": _max_age(extra_params),
        "binding_hash": _sha256(binding_token),
    }
    try:
        redis = redis if redis is not None else await get_redis()
        await redis.set(tx_key(state), json.dumps(record), ex=TX_TTL_SECONDS)
    except REDIS_ERRORS as exc:
        logger.error("sso.tx.store_failed provider=%s error=%s", provider.slug, type(exc).__name__)
        raise SsoUnavailableError("redis_unavailable") from None

    url = build_authorize_url(
        provider,
        metadata,
        redirect_uri=redirect_uri,
        state=state,
        nonce=nonce,
        code_challenge=_pkce_challenge(code_verifier),
        extra_params=extra_params,
    )
    logger.info("sso.login.started provider=%s intent=%s", provider.slug, intent)
    return AuthorizationRequest(url=url, binding_token=binding_token)


def _parse_record(raw: str) -> tuple[LoginTransaction, str] | None:
    """The transaction and its binding hash, or ``None`` for a record we did not write."""
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("v") != _RECORD_VERSION or data.get("intent") not in _INTENTS:
            return None
        binding_hash = data.get("binding_hash")
        if not isinstance(binding_hash, str) or not binding_hash:
            return None
        user_id = data.get("user_id")
        max_age = data.get("max_age")
        if max_age is not None and (isinstance(max_age, bool) or not isinstance(max_age, int) or max_age < 0):
            return None
        tx = LoginTransaction(
            provider_id=int(data["provider_id"]),
            provider_slug=str(data["provider_slug"]),
            issuer=str(data["issuer"]),
            nonce=str(data["nonce"]),
            code_verifier=str(data["code_verifier"]),
            redirect_uri=str(data["redirect_uri"]),
            next_path=str(data.get("next") or ""),
            intent=data["intent"],
            user_id=int(user_id) if user_id is not None else None,
            max_age=max_age,
        )
    except (ValueError, TypeError, KeyError):
        return None
    return tx, binding_hash


async def complete(
    *,
    state: str | None,
    binding_token: str | None,
    redis: Redis | None = None,
) -> LoginTransaction:
    """Consume the transaction for ``state`` and check the browser binding.

    The record is deleted whatever the outcome, so a state can never be
    presented twice. Raises ``sso_state_invalid`` for a missing, expired,
    reused or corrupt transaction and for a missing or wrong binding cookie,
    and ``sso_unavailable`` when Redis cannot be reached.
    """
    if not state or len(state) > _MAX_STATE_LENGTH:
        logger.warning("sso.tx.rejected reason=missing_state")
        raise SsoStateInvalidError("missing_state")
    try:
        redis = redis if redis is not None else await get_redis()
        raw = await redis.getdel(tx_key(state))
    except REDIS_ERRORS as exc:
        logger.error("sso.tx.load_failed error=%s", type(exc).__name__)
        raise SsoUnavailableError("redis_unavailable") from None

    if raw is None:
        logger.warning("sso.tx.rejected reason=unknown_or_used_state")
        raise SsoStateInvalidError("unknown_or_used_state")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    parsed = _parse_record(raw)
    if parsed is None:
        logger.warning("sso.tx.rejected reason=corrupt_record")
        raise SsoStateInvalidError("corrupt_record")
    tx, binding_hash = parsed
    if not binding_token:
        logger.warning("sso.tx.rejected provider=%s reason=missing_binding_cookie", tx.provider_slug)
        raise SsoStateInvalidError("missing_binding_cookie")
    if not hmac.compare_digest(_sha256(binding_token), binding_hash):
        logger.warning("sso.tx.rejected provider=%s reason=binding_mismatch", tx.provider_slug)
        raise SsoStateInvalidError("binding_mismatch")
    return tx
