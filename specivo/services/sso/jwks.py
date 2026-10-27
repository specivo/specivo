"""JWKS cache, key rotation and key selection for ID and logout token signatures.

Implements the key handling of OpenID Connect Core 1.0, section 10.1.1
(rotation of asymmetric signing keys: a token with an unknown ``kid`` makes
the RP refetch the JWKS) and RFC 7517 (JSON Web Key), with the limits of the
Specivo SSO design:

* the JWKS is fetched with httpx through
  :func:`specivo.services.sso.http.create_sso_client` and handed to PyJWT as
  a dict (PyJWT's ``PyJWKClient`` does blocking I/O and is not used);
* it is cached in Redis under ``sso:jwks:{provider_id}`` for the response's
  ``Cache-Control: max-age``, clamped to [:data:`JWKS_MIN_TTL_SECONDS`,
  :data:`JWKS_MAX_TTL_SECONDS`];
* an unknown ``kid`` triggers at most one refetch per provider per
  :data:`JWKS_REFETCH_INTERVAL_SECONDS` (a Redis ``SET NX EX`` lock, with an
  in-process fallback when Redis is down), so a flood of tokens with random
  kids cannot make Specivo hammer the IdP;
* keys with ``use=enc``, symmetric (``oct``) keys and keys for algorithms
  outside the allowlist are dropped when the JWKS is read;
* the algorithm allowlist is :data:`ALLOWED_ALGORITHMS` intersected with the
  provider's ``id_token_signing_alg_values_supported``. ``none`` and HS* are
  never accepted, whatever the IdP advertises (no key confusion).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

import httpx
from jwt import PyJWK
from jwt.exceptions import PyJWTError
from redis.asyncio import Redis

from specivo.core.config import Settings, get_settings
from specivo.core.redis import get_redis
from specivo.services.sso._support import acquire_once, cache_get, cache_set, parse_json_object, unreachable_from
from specivo.services.sso.errors import (
    SsoDiscoveryInvalidError,
    SsoIdpUnreachableError,
    SsoTokenInvalidError,
    SsoUnknownSigningKeyError,
    TokenRejection,
)
from specivo.services.sso.http import create_sso_client
from specivo.services.sso.log import logger

if TYPE_CHECKING:
    from specivo.services.sso.discovery import ProviderMetadata

__all__ = [
    "ALLOWED_ALGORITHMS",
    "JWKS_MAX_TTL_SECONDS",
    "JWKS_MIN_TTL_SECONDS",
    "JWKS_REFETCH_INTERVAL_SECONDS",
    "get_jwks",
    "jwks_cache_key",
    "refetch_jwks_for_unknown_kid",
    "select_signing_key",
    "signing_algorithms",
    "ttl_from_cache_control",
    "usable_signing_keys",
]

#: Signature algorithms Specivo accepts at all. ``none`` and HS* are never in
#: it: with HMAC the verifier's "key" would be the public key.
ALLOWED_ALGORITHMS: frozenset[str] = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)

JWKS_MIN_TTL_SECONDS = 5 * 60
JWKS_MAX_TTL_SECONDS = 24 * 60 * 60
#: Used when the JWKS response has no ``max-age``.
JWKS_DEFAULT_TTL_SECONDS = 60 * 60
#: At most one refetch per provider in this window when an unknown kid shows up.
JWKS_REFETCH_INTERVAL_SECONDS = 60

_EC_CURVES = {"ES256": "P-256", "ES384": "P-384", "ES512": "P-521"}
_OKP_CURVES = frozenset({"Ed25519", "Ed448"})
_KEY_TYPES = frozenset({"RSA", "EC", "OKP"})

# Last unknown-kid refetch per provider (time.monotonic), used only while Redis is down.
_local_refetch_at: dict[int, float] = {}


def jwks_cache_key(provider_id: int) -> str:
    return f"sso:jwks:{provider_id}"


def _refetch_lock_key(provider_id: int) -> str:
    return f"sso:jwks:refetch:{provider_id}"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def ttl_from_cache_control(header: str | None) -> int:
    """Cache lifetime for a JWKS response, clamped to [5 min, 24 h].

    ``no-store``/``no-cache`` or an unparsable ``max-age`` give the minimum;
    no ``max-age`` at all gives :data:`JWKS_DEFAULT_TTL_SECONDS`.
    """
    ttl: int | None = None
    for directive in (header or "").split(","):
        name, _, value = directive.strip().partition("=")
        name = name.strip().lower()
        if name in ("no-store", "no-cache"):
            return JWKS_MIN_TTL_SECONDS
        if name == "max-age":
            try:
                ttl = int(value.strip().strip('"'))
            except ValueError:
                return JWKS_MIN_TTL_SECONDS
    if ttl is None:
        ttl = JWKS_DEFAULT_TTL_SECONDS
    return max(JWKS_MIN_TTL_SECONDS, min(JWKS_MAX_TTL_SECONDS, ttl))


def signing_algorithms(metadata: ProviderMetadata) -> frozenset[str]:
    """The allowlist for this provider: Specivo's algorithms ∩ the discovery list."""
    return ALLOWED_ALGORITHMS.intersection(metadata.id_token_signing_alg_values_supported)


def _is_usable(jwk: Any) -> bool:
    if not isinstance(jwk, dict) or jwk.get("kty") not in _KEY_TYPES:
        return False
    if jwk.get("use", "sig") != "sig":
        return False
    key_ops = jwk.get("key_ops")
    if key_ops is not None and (not isinstance(key_ops, list) or "verify" not in key_ops):
        return False
    alg = jwk.get("alg")
    return alg is None or alg in ALLOWED_ALGORITHMS


def usable_signing_keys(document: Any) -> list[dict[str, Any]]:
    """The signature-verification keys of a JWKS document (RFC 7517, section 5)."""
    if not isinstance(document, dict):
        return []
    keys = document.get("keys")
    if not isinstance(keys, list):
        return []
    return [jwk for jwk in keys if _is_usable(jwk)]


def _fits(jwk: Mapping[str, Any], alg: str) -> bool:
    """Whether a JWK can verify ``alg`` (key type, curve and declared alg)."""
    declared = jwk.get("alg")
    if declared is not None and declared != alg:
        return False
    kty = jwk.get("kty")
    if alg.startswith(("RS", "PS")):
        return kty == "RSA"
    if alg in _EC_CURVES:
        return kty == "EC" and jwk.get("crv") == _EC_CURVES[alg]
    return kty == "OKP" and jwk.get("crv") in _OKP_CURVES  # EdDSA


def select_signing_key(
    keys: Iterable[Mapping[str, Any]], header: Mapping[str, Any], algorithms: Iterable[str]
) -> PyJWK:
    """The key that must verify a token with this JOSE ``header``.

    The header ``alg`` must be in :data:`ALLOWED_ALGORITHMS` and in
    ``algorithms``. With a ``kid``, only keys with that kid are candidates
    (none: :class:`SsoUnknownSigningKeyError`, the caller may refetch). Without
    one, exactly one published key may fit the algorithm. The returned
    ``PyJWK`` is bound to the header algorithm.
    """
    alg = header.get("alg")
    allowed = ALLOWED_ALGORITHMS.intersection(algorithms)
    if not isinstance(alg, str) or alg not in allowed:
        raise SsoTokenInvalidError(TokenRejection.ALG_NOT_ALLOWED, detail=f"alg={str(alg)[:20]!r}")

    kid = header.get("kid")
    if kid is not None and not isinstance(kid, str):
        raise SsoTokenInvalidError(TokenRejection.MALFORMED, detail="kid")

    key_list = list(keys)
    if kid is not None:
        candidates = [jwk for jwk in key_list if jwk.get("kid") == kid]
        if not candidates:
            raise SsoUnknownSigningKeyError(kid)
    else:
        candidates = [jwk for jwk in key_list if _fits(jwk, alg)]
        if len(candidates) != 1:
            raise SsoTokenInvalidError(TokenRejection.UNKNOWN_KID, detail="no kid and no single matching key")

    for jwk in candidates:
        if not _fits(jwk, alg):
            continue
        try:
            return PyJWK(dict(jwk), algorithm=alg)
        except (PyJWTError, ValueError, TypeError, KeyError):
            continue
    raise SsoTokenInvalidError(TokenRejection.KEY_MISMATCH, detail=f"kid={kid!r} alg={alg}")


# ---------------------------------------------------------------------------
# Fetch and cache
# ---------------------------------------------------------------------------


async def _fetch(jwks_uri: str, settings: Settings) -> tuple[list[dict[str, Any]], int]:
    try:
        async with create_sso_client(settings) as client:
            response = await client.get(jwks_uri, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise unreachable_from(exc, what="jwks") from None

    status = response.status_code
    if status >= 500 or status == 429:
        logger.warning("sso.jwks.unreachable reason=http_%s", status)
        raise SsoIdpUnreachableError(f"http_{status}")
    if status != 200:
        logger.warning("sso.jwks.invalid reason=http_%s", status)
        raise SsoDiscoveryInvalidError(f"http_{status}")
    document = parse_json_object(response.content)
    if document is None:
        logger.warning("sso.jwks.invalid reason=jwks_not_json")
        raise SsoDiscoveryInvalidError("jwks_not_json")
    keys = usable_signing_keys(document)
    if not keys:
        logger.warning("sso.jwks.invalid reason=jwks_no_usable_keys")
        raise SsoDiscoveryInvalidError("jwks_no_usable_keys")
    return keys, ttl_from_cache_control(response.headers.get("cache-control"))


async def _fetch_and_store(provider_id: int, jwks_uri: str, redis: Redis, settings: Settings) -> list[dict[str, Any]]:
    keys, ttl = await _fetch(jwks_uri, settings)
    await cache_set(redis, jwks_cache_key(provider_id), json.dumps({"uri": jwks_uri, "keys": keys}), ttl)
    logger.info("sso.jwks.fetched provider_id=%s keys=%d ttl=%d", provider_id, len(keys), ttl)
    return keys


async def get_jwks(
    provider_id: int,
    jwks_uri: str,
    *,
    redis: Redis | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """The provider's usable signing keys, from the Redis cache or the IdP."""
    settings = settings or get_settings()
    redis = redis if redis is not None else await get_redis()
    cached = await cache_get(redis, jwks_cache_key(provider_id))
    if cached is not None:
        try:
            entry = json.loads(cached)
        except ValueError:
            entry = None
        if isinstance(entry, dict) and entry.get("uri") == jwks_uri:
            keys = usable_signing_keys(entry)
            if keys:
                return keys
    return await _fetch_and_store(provider_id, jwks_uri, redis, settings)


async def refetch_jwks_for_unknown_kid(
    provider_id: int,
    jwks_uri: str,
    *,
    redis: Redis | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]] | None:
    """Refetch the JWKS after an unknown ``kid``, at most once per provider per minute.

    Returns the fresh keys, or ``None`` when a refetch already happened in the
    last :data:`JWKS_REFETCH_INTERVAL_SECONDS` (the token is then rejected).
    """
    settings = settings or get_settings()
    redis = redis if redis is not None else await get_redis()
    acquired = await acquire_once(redis, _refetch_lock_key(provider_id), JWKS_REFETCH_INTERVAL_SECONDS)
    if acquired is None:
        # Redis is down: rate-limit in this process instead.
        now = time.monotonic()
        last = _local_refetch_at.get(provider_id)
        acquired = last is None or now - last > JWKS_REFETCH_INTERVAL_SECONDS
        if acquired:
            _local_refetch_at[provider_id] = now
    if not acquired:
        logger.warning("sso.jwks.refetch_rate_limited provider_id=%s", provider_id)
        return None
    return await _fetch_and_store(provider_id, jwks_uri, redis, settings)
