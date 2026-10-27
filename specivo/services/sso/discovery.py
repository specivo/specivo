"""OpenID Provider discovery: fetch, validate and cache provider metadata.

Implements OpenID Connect Discovery 1.0, section 4 (obtaining the
configuration) and 4.3 (validation: the ``issuer`` in the document must be
identical to the configured issuer, byte for byte), plus the hardening rules
of the Specivo SSO design:

* ``authorization_endpoint``, ``token_endpoint`` and ``jwks_uri`` are required,
  and ``response_types_supported`` must contain ``code``;
* the issuer and every endpoint must be absolute ``https`` URLs, unless
  ``SSO_ALLOW_INSECURE_HTTP`` is on;
* when ``code_challenge_methods_supported`` is advertised it must include
  ``S256`` (RFC 7636, section 4.2: Specivo always sends S256);
* at least one ID token signing algorithm must be in Specivo's allowlist
  (``none`` and HMAC never are).

Transport limits (timeout, 1 MB cap, no cross-host redirects) come from
:func:`specivo.services.sso.http.create_sso_client`.

Caching: the validated document is kept in Redis under
``sso:disco:{provider_id}`` for :data:`DISCOVERY_CACHE_TTL_SECONDS`, and
copied to ``SsoProvider.discovery_cache`` / ``discovery_fetched_at`` as the
last known good document. When the IdP is unreachable, that copy is used (it
is re-validated against the provider's current issuer first). The row is only
modified in memory; the caller's session commits it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from redis.asyncio import Redis

from specivo.core.config import Settings, get_settings
from specivo.core.redis import get_redis
from specivo.models.sso import SsoProvider
from specivo.services.sso._support import cache_delete, cache_get, cache_set, parse_json_object, unreachable_from
from specivo.services.sso.errors import SsoDiscoveryInvalidError, SsoIdpUnreachableError
from specivo.services.sso.http import create_sso_client
from specivo.services.sso.jwks import ALLOWED_ALGORITHMS, jwks_cache_key
from specivo.services.sso.log import logger

__all__ = [
    "DISCOVERY_CACHE_TTL_SECONDS",
    "ProviderMetadata",
    "default_discovery_url",
    "discovery_cache_key",
    "get_provider_metadata",
    "invalidate_provider_cache",
    "parse_discovery",
]

#: Lifetime of the Redis copy of a discovery document.
DISCOVERY_CACHE_TTL_SECONDS = 3600

_REQUIRED_ENDPOINTS = ("authorization_endpoint", "token_endpoint", "jwks_uri")
_OPTIONAL_ENDPOINTS = ("userinfo_endpoint", "end_session_endpoint")


def discovery_cache_key(provider_id: int) -> str:
    return f"sso:disco:{provider_id}"


def default_discovery_url(issuer: str) -> str:
    """``issuer`` + ``/.well-known/openid-configuration`` (Discovery 1.0, section 4)."""
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


@dataclass(frozen=True)
class ProviderMetadata:
    """A validated discovery document. ``raw`` is the document as received."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None = None
    end_session_endpoint: str | None = None
    id_token_signing_alg_values_supported: tuple[str, ...] = ("RS256",)
    token_endpoint_auth_methods_supported: tuple[str, ...] = ()
    code_challenge_methods_supported: tuple[str, ...] = ()
    authorization_response_iss_parameter_supported: bool = False
    backchannel_logout_supported: bool = False
    backchannel_logout_session_supported: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


def _invalid(reason: str) -> SsoDiscoveryInvalidError:
    logger.warning("sso.discovery.invalid reason=%s", reason)
    return SsoDiscoveryInvalidError(reason)


def _check_url(value: Any, name: str, *, allow_insecure_http: bool) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(f"missing_{name}")
    try:
        url = httpx.URL(value)
        scheme, host, fragment = url.scheme, url.host, url.fragment
    except (httpx.InvalidURL, TypeError, ValueError):
        raise _invalid(f"malformed_{name}") from None
    if scheme == "http" and not allow_insecure_http:
        raise _invalid(f"insecure_{name}")
    if scheme not in ("https", "http") or not host or fragment:
        raise _invalid(f"malformed_{name}")
    return value


def _string_tuple(doc: Mapping[str, Any], key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = doc.get(key)
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _invalid(f"malformed_{key}")
    return tuple(value)


def parse_discovery(
    doc: Mapping[str, Any], *, expected_issuer: str, allow_insecure_http: bool = False
) -> ProviderMetadata:
    """Validate a discovery document for the configured ``expected_issuer``.

    Raises :class:`SsoDiscoveryInvalidError` (``sso_discovery_invalid``) with
    a reason such as ``issuer_mismatch`` or ``insecure_token_endpoint``.
    """
    if not isinstance(doc, Mapping):
        raise _invalid("not_an_object")

    issuer = doc.get("issuer")
    if not isinstance(issuer, str) or not issuer:
        raise _invalid("missing_issuer")
    if issuer != expected_issuer:
        raise _invalid("issuer_mismatch")
    _check_url(issuer, "issuer", allow_insecure_http=allow_insecure_http)

    endpoints = {
        name: _check_url(doc.get(name), name, allow_insecure_http=allow_insecure_http) for name in _REQUIRED_ENDPOINTS
    }
    optional: dict[str, str | None] = {}
    for name in _OPTIONAL_ENDPOINTS:
        value = doc.get(name)
        optional[name] = None if value is None else _check_url(value, name, allow_insecure_http=allow_insecure_http)

    if "code" not in _string_tuple(doc, "response_types_supported"):
        raise _invalid("code_flow_unsupported")

    # Discovery 1.0 section 3: RS256 must be supported, so it is the default.
    algorithms = _string_tuple(doc, "id_token_signing_alg_values_supported", ("RS256",))
    if not ALLOWED_ALGORITHMS.intersection(algorithms):
        raise _invalid("no_allowed_signing_alg")

    pkce_methods = _string_tuple(doc, "code_challenge_methods_supported")
    if pkce_methods and "S256" not in pkce_methods:
        raise _invalid("pkce_s256_unsupported")

    return ProviderMetadata(
        issuer=issuer,
        authorization_endpoint=endpoints["authorization_endpoint"],
        token_endpoint=endpoints["token_endpoint"],
        jwks_uri=endpoints["jwks_uri"],
        userinfo_endpoint=optional["userinfo_endpoint"],
        end_session_endpoint=optional["end_session_endpoint"],
        id_token_signing_alg_values_supported=algorithms,
        token_endpoint_auth_methods_supported=_string_tuple(doc, "token_endpoint_auth_methods_supported"),
        code_challenge_methods_supported=pkce_methods,
        authorization_response_iss_parameter_supported=doc.get("authorization_response_iss_parameter_supported")
        is True,
        backchannel_logout_supported=doc.get("backchannel_logout_supported") is True,
        backchannel_logout_session_supported=doc.get("backchannel_logout_session_supported") is True,
        raw=dict(doc),
    )


async def _fetch_document(url: str, settings: Settings) -> dict[str, Any]:
    try:
        async with create_sso_client(settings) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise unreachable_from(exc, what="discovery") from None

    status = response.status_code
    if status >= 500 or status == 429:
        logger.warning("sso.discovery.unreachable reason=http_%s", status)
        raise SsoIdpUnreachableError(f"http_{status}")
    if status != 200:
        raise _invalid(f"http_{status}")
    doc = parse_json_object(response.content)
    if doc is None:
        raise _invalid("not_json")
    return doc


async def get_provider_metadata(
    provider: SsoProvider,
    *,
    redis: Redis | None = None,
    settings: Settings | None = None,
    force_refresh: bool = False,
) -> ProviderMetadata:
    """The provider's validated metadata, from Redis, the IdP or the last good copy.

    Order: the Redis cache (unless ``force_refresh``), then the IdP. If the
    IdP is unreachable (``sso_idp_unreachable``), the copy on the provider row
    is used when it still validates; an *invalid* document from the IdP never
    falls back. A Redis outage only means the document is fetched every time.
    """
    settings = settings or get_settings()
    allow_insecure = settings.sso_allow_insecure_http
    redis = redis if redis is not None else await get_redis()
    key = discovery_cache_key(provider.id)

    if not force_refresh:
        cached = await cache_get(redis, key)
        if cached is not None:
            try:
                return parse_discovery(
                    json.loads(cached), expected_issuer=provider.issuer, allow_insecure_http=allow_insecure
                )
            except (ValueError, SsoDiscoveryInvalidError):
                logger.info("sso.discovery.cache_ignored provider=%s", provider.slug)

    url = provider.discovery_url or default_discovery_url(provider.issuer)
    try:
        doc = await _fetch_document(url, settings)
    except SsoIdpUnreachableError:
        fallback = _last_known_good(provider, allow_insecure)
        if fallback is None:
            raise
        logger.warning("sso.discovery.using_last_known_good provider=%s", provider.slug)
        return fallback

    metadata = parse_discovery(doc, expected_issuer=provider.issuer, allow_insecure_http=allow_insecure)
    await cache_set(redis, key, json.dumps(doc), DISCOVERY_CACHE_TTL_SECONDS)
    provider.discovery_cache = doc
    provider.discovery_fetched_at = datetime.now(UTC)
    logger.info("sso.discovery.fetched provider=%s", provider.slug)
    return metadata


def _last_known_good(provider: SsoProvider, allow_insecure: bool) -> ProviderMetadata | None:
    if not provider.discovery_cache:
        return None
    try:
        return parse_discovery(
            provider.discovery_cache, expected_issuer=provider.issuer, allow_insecure_http=allow_insecure
        )
    except SsoDiscoveryInvalidError:
        return None


async def invalidate_provider_cache(provider_id: int, *, redis: Redis | None = None) -> None:
    """Drop the cached discovery document and JWKS of a provider (after a config change)."""
    redis = redis if redis is not None else await get_redis()
    await cache_delete(redis, discovery_cache_key(provider_id), jwks_cache_key(provider_id))
